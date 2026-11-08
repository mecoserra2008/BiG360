"""Confluence weight of a level (owner: technicals). Shared by tech_levels (weight column) and confluence.

weight = reliability x recency x touches x timeframe x volume-node strength x role-flip
  reliability  (type_rank_score / 0.5)^2 (level_type_stats.score 0..1; neutral 0.5 -> 1.0; missing -> 1.0)
  recency      swing/structure/fibonacci/gap levels: 0.5 ** (age_days / 180); others 1
  touches      1 + 0.1 x min(touches, 5)
  timeframe    taxonomy.TF_WEIGHT (D 0.55 ... Y 1.3)
  volume node  volume_profile levels: 0.5 + 0.5 x node_strength
  role flip    0.7 when still_valid is false (level was closed through; it now acts from the other side)
"""

from __future__ import annotations

from datetime import date

import polars as pl

from .taxonomy import TF_WEIGHT

RECENCY_FAMILIES = ("swing_pivot", "market_structure", "fibonacci", "gap")
RECENCY_HALF_LIFE_DAYS = 180.0


def level_weight_expr(as_of: date) -> pl.Expr:
    rel = (pl.col("type_rank_score").fill_null(0.5).clip(0.0, 1.0) / 0.5) ** 2
    age = (pl.lit(as_of) - pl.col("anchor_date")).dt.total_days().cast(pl.Float64).clip(lower_bound=0.0)
    rec = pl.when(pl.col("family").is_in(list(RECENCY_FAMILIES))).then(0.5 ** (age / RECENCY_HALF_LIFE_DAYS)).otherwise(1.0)
    tch = 1.0 + 0.1 * pl.col("touches").cast(pl.Float64).fill_null(0.0).clip(0.0, 5.0)
    tfw = pl.col("timeframe").replace_strict(TF_WEIGHT, default=1.0, return_dtype=pl.Float64)
    vol = pl.when(pl.col("family") == "volume_profile").then(0.5 + 0.5 * pl.col("node_strength").fill_null(0.5).clip(0.0, 1.0)).otherwise(1.0)
    flip = pl.when(pl.col("still_valid").fill_null(True)).then(1.0).otherwise(0.7)
    return rel * rec * tch * tfw * vol * flip
