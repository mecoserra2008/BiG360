"""Sector-specific KPI sets -> `sector_kpis` (owner: analytics-scoring). Computed by job `ratios_kpis`
(app.analytics.ratios.run) on the same PIT vintage panels as the generic ratios.

Each issuer gets ONLY the KPIs of the kpi set its subsector belongs to (contracts/sector_kpis.yaml kpi_sets), e.g.
banks: NIM / efficiency / loans-to-deposits / NPL / cost of risk / RoTE / CET1; insurers: loss/expense/combined
ratio; REITs: FFO margin, FFO/AFFO per-share growth, AFFO payout; software: Rule of 40, FCF margin after SBC;
semis: R&D intensity, inventory-days change; utilities: FFO/debt; energy/materials: through-cycle ROIC, reinvestment;
retail: inventory turns change, GMROI; distributors (incl. Japanese trading houses): gross profit / op. working capital.

"Most relevant metrics" for peer comparison = kpi_set.peer_metrics (ordered by relevance). They are exposed by
`peer_metric_rank()` and stored on each KPI row as `peer_rank` (1 = most relevant; null = not a headline metric).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from app.core.config import Settings
from app.analytics.ratios import dedupe_versions, eval_metrics, registry, to_long, topo_order


def kpi_set(kpi_set_id: str | None, settings: Settings | None = None) -> dict[str, Any] | None:
    return registry(settings)["kpi_sets"].get(kpi_set_id or "")


def peer_metric_rank(kpi_set_id: str | None, settings: Settings | None = None) -> dict[str, int]:
    ks = kpi_set(kpi_set_id, settings)
    return {m: i + 1 for i, m in enumerate((ks or {}).get("peer_metrics") or [])}


def excluded_generic(kpi_set_id: str | None, settings: Settings | None = None) -> set[str]:
    ks = kpi_set(kpi_set_id, settings)
    return set((ks or {}).get("excluded_generic") or [])


def kpi_rows(pan: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """Evaluate each kpi set's KPIs on the rows of issuers in that set (pan from ratios.build_frames)."""
    reg = registry(settings)
    out = []
    if pan.is_empty() or "kpi_set_id" not in pan.columns:
        return pl.DataFrame()
    for set_id in pan["kpi_set_id"].drop_nulls().unique().to_list():
        ks = reg["kpi_sets"].get(set_id)
        if not ks or not ks.get("kpis"):
            continue
        sub = pan.filter(pl.col("kpi_set_id") == set_id)
        forms = {k["id"]: k["formula"] for k in ks["kpis"]}
        units = {k["id"]: k.get("unit", "ratio") for k in ks["kpis"]}
        usd = [k for k, u in units.items() if u == "currency_usd"]
        # KPI ids can shadow nothing in the generic set; drop stale columns of the same name first
        sub = sub.drop([c for c in forms if c in sub.columns])
        sub = eval_metrics(sub, forms, topo_order(forms), usd)
        lng = to_long(sub, list(forms), units, extra_keys=("sector_id", "subsector_id"))
        if lng.is_empty():
            continue
        ranks = peer_metric_rank(set_id, settings)
        out.append(lng.with_columns(
            pl.lit(set_id).alias("kpi_set_id"),
            pl.col("metric_id").replace_strict(ranks, default=None, return_dtype=pl.Int16).alias("peer_rank"),
        ))
    if not out:
        return pl.DataFrame()
    df = dedupe_versions(pl.concat(out, how="vertical_relaxed"))
    return df.rename({"metric_id": "kpi_id"}).filter(pl.col("sector_id").is_not_null(), pl.col("subsector_id").is_not_null())
