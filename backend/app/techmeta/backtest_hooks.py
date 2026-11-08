"""Backtest Lab trigger `meta_setups`: the v2 weekly setups filtered / sized by the meta-labelling model (Technicals v3
#2; gate docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"). Labelled with the gate verdict read from
data/reports/technicals_v3/meta/gate.json.

Sources: walk_forward = the purged walk-forward OOS predictions 2013 -> 2026 (data/reports/technicals_v3/meta/
meta_oos_h<H>.parquet: the gate universe mvp US + major EU / UK / JP names); live = the tech_meta_setups table (latest +
month-end snapshots only); auto = walk_forward when the file exists. Modes: all (every primary setup), take (meta
take only), sized (take, position weight x size_rel = quarter-Kelly relative to the median taken setup, 0..3).
Each event carries its own stop_loss / take_profit at the setup's invalidation / T1 (close-based exits in the engine).
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

EMPTY = {"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int8, "strength": pl.Float64}


def gate_note(data_dir: Path, horizon: int) -> tuple[str, bool]:
    """(note, production) for the trigger at this horizon."""
    try:
        g = json.loads((data_dir / "reports" / "technicals_v3" / "meta" / "gate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        g = {}
    o = (g.get("outputs") or {})
    m = (o.get(f"meta_h{horizon}") or {}).get("verdict", "not_validated")
    d = (o.get(f"decision_h{horizon}") or {}).get("verdict", "not_validated")
    prod = m == "pass" and d == "pass"
    txt = (f"meta-labelled setups H={horizon}: calibration gate {m.upper()}, economic check {d.upper()} "
           "(docs/TECHNICALS_V3_VALIDATION.md, Meta-labelling)")
    if not prod:
        txt += " - EXPERIMENTAL: model-based, not a validated strategy"
    return txt, prod


def meta_events(ids: list[str], horizon: int = 20, mode: str = "take", source: str = "auto", direction: str = "long",
                data_dir: Path | None = None, con: Any = None, base_weight: float = 0.05,
                warns: list[str] | None = None) -> tuple[pl.DataFrame, str]:
    warns = warns if warns is not None else []
    if horizon not in (10, 20, 40):
        raise ValueError("meta_setups: horizon must be 10, 20 or 40")
    if mode not in ("all", "take", "sized"):
        raise ValueError("meta_setups: mode must be all, take or sized")
    f = (data_dir / "reports" / "technicals_v3" / "meta" / f"meta_oos_h{horizon}.parquet") if data_dir else None
    use = source if source != "auto" else ("walk_forward" if f is not None and f.exists() else "live")
    if use == "walk_forward":
        if f is None or not f.exists():
            warns.append("meta_setups: no walk-forward file (data/reports/technicals_v3/meta); using the live table")
            use = "live"
        else:
            df = pl.read_parquet(f, columns=["security_id", "date", "direction", "er_sel", "take", "size_rel", "x_up", "x_dn"])
    if use == "live":
        if con is None:
            return pl.DataFrame(schema=EMPTY), use
        try:
            df = con.execute("""SELECT m.security_id, CAST(m.as_of_date AS DATE) AS date, m.direction, m.expected_r AS er_sel,
                                       m.take, m.size_rel, ln(s.target1 / s.close) AS x_up_l, ln(s.close / s.invalidation) AS x_dn_l,
                                       ln(s.invalidation / s.close) AS x_up_s, ln(s.close / s.target1) AS x_dn_s
                                FROM tech_meta_setups m JOIN tech_prob_setups s
                                  ON s.security_id = m.security_id AND s.as_of_date = m.as_of_date AND s.horizon = m.horizon
                                WHERE m.horizon = ?""", [horizon]).pl()
        except Exception as e:  # noqa: BLE001 - tables missing in an old lake
            warns.append(f"meta_setups (live): tables unavailable ({type(e).__name__})")
            return pl.DataFrame(schema=EMPTY), use
        df = df.with_columns(pl.when(pl.col("direction") > 0).then(pl.col("x_up_l")).otherwise(pl.col("x_up_s")).alias("x_up"),
                             pl.when(pl.col("direction") > 0).then(pl.col("x_dn_l")).otherwise(pl.col("x_dn_s")).alias("x_dn"))
        if df.height and df["date"].n_unique() < 30:
            warns.append(f"meta_setups (live): only {df['date'].n_unique()} as-of dates stored (latest + month-ends); use "
                         "source=walk_forward for 2013-2026")
    if df.is_empty():
        return pl.DataFrame(schema=EMPTY), use
    df = df.filter(pl.col("security_id").is_in(ids))
    if direction != "both":
        df = df.filter(pl.col("direction") > 0)
    if mode in ("take", "sized"):
        df = df.filter(pl.col("take").fill_null(False))
    long = pl.col("direction") > 0
    out = df.select(
        pl.col("security_id").alias("entity_id"), pl.col("date").cast(pl.Date), pl.col("direction").cast(pl.Int8),
        pl.col("er_sel").cast(pl.Float64).alias("strength"),
        pl.when(long).then(1 - (-pl.col("x_dn")).exp()).otherwise(pl.col("x_up").exp() - 1).clip(0.002, 0.9).alias("stop_loss"),
        pl.when(long).then(pl.col("x_up").exp() - 1).otherwise(1 - (-pl.col("x_dn")).exp()).clip(0.002, 5.0).alias("take_profit"),
        *([(pl.col("size_rel").cast(pl.Float64).fill_null(0.0) * base_weight).clip(0.0, 3 * base_weight).alias("weight")]
          if mode == "sized" else []))
    out = out.filter(pl.col("stop_loss").is_finite() & pl.col("take_profit").is_finite())
    return out.sort(["date", "entity_id"]), use


def base_weight_of(ev: dict[str, Any]) -> float:
    bw = ev.get("base_weight")
    try:
        x = float(bw)
        return x if math.isfinite(x) and x > 0 else 0.05
    except (TypeError, ValueError):
        return 0.05


def in_range(df: pl.DataFrame, lo: date | None, hi: date | None) -> pl.DataFrame:
    if lo:
        df = df.filter(pl.col("date") >= lo)
    if hi:
        df = df.filter(pl.col("date") <= hi)
    return df
