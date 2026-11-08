"""Macro v2 revisions (W1.5): first release vs latest revised values for vintage-enabled series.

Vintage sources
  * 'alfred'     - FRED/ALFRED real-time rows (realtime_start = knowledge_date, knowledge_date_estimated = false):
                   GDP, payrolls, CPI, core CPI, PCE, IP, retail sales, unemployment, trade balance ...;
  * 'first_seen' - any other series whose ingest recorded a revised value (app.ingestion.macro_common
                   .reconcile_observations keeps the old row and dates the new value on the day it was first seen).
For each (series, period): first_value = value of the earliest vintage, latest_value = the latest vintage,
revision = latest - first (native units). The same pair is given on the series' canonical measure (app.macro.indicators:
y/y for index/level growth series, else level): first_value_tf is computed point-in-time (first print of t over the
t-12 value known on t's first-release date), latest_value_tf from the latest vintage.
Caveat: ALFRED's archive starts in the 1990s for most series; where the first vintage of an old period is the archive
start, the ingest imputed its release date (first_estimated = true) and that first value may already be revised.

Rebasing: index/level series change base year across ALFRED vintages (CPI 1967=100 -> 1982-84=100, chained-dollar
GDP 2012 -> 2017 dollars), which makes latest/first level ratios jump. A period is flagged rebased when the series
is an index/level and |latest/first - 1| > REBASE_TOL (5%; genuine level revisions are far smaller). The canonical
measure (y/y from the same vintage) is base-invariant, so it is the primary basis for revision statistics.
`revision_stats(rows, since)` -> mean / mean |revision| / std / share up / max |revision| / n on the canonical basis
and on the native basis restricted to periods whose bases match (rebased excluded); periods whose first release
date was imputed (pre-archive ALFRED artefacts) are excluded from both and counted.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.macro.indicators import CELL_INDICATORS, SeriesInfo, canonical_how, first_release_canonical, to_canonical

REBASE_TOL = 0.05

REV_SCHEMA: dict[str, Any] = {
    "series_id": pl.Utf8, "date": pl.Date, "vintage_kind": pl.Utf8, "first_value": pl.Float64,
    "first_knowledge_date": pl.Date, "first_estimated": pl.Boolean, "latest_value": pl.Float64,
    "latest_knowledge_date": pl.Date, "n_vintages": pl.Int32, "revision": pl.Float64, "transform": pl.Utf8,
    "first_value_tf": pl.Float64, "latest_value_tf": pl.Float64, "revision_tf": pl.Float64, "rebased": pl.Boolean,
    "computed_at": pl.Datetime("us", "UTC"),
}


def vintage_series(obs: pl.DataFrame) -> list[str]:
    """Series with at least one period that has 2+ distinct knowledge dates."""
    if obs.is_empty():
        return []
    v = (obs.group_by("series_id", "date").agg(pl.col("knowledge_date").n_unique().alias("n"))
         .filter(pl.col("n") > 1).get_column("series_id").unique().to_list())
    return sorted(v)


def series_revisions(sid: str, o: pl.DataFrame, info: SeriesInfo | None, now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    o = o.drop_nulls("value").sort("knowledge_date")
    if o.is_empty():
        return pl.DataFrame(schema=REV_SCHEMA)
    observed_share = float((~o.get_column("knowledge_date_estimated")).mean() or 0.0)
    kind = "alfred" if sid.startswith("fred:") and observed_share > 0.5 else "first_seen"
    g = (o.group_by("date").agg(
        pl.col("value").first().alias("first_value"), pl.col("knowledge_date").first().alias("first_knowledge_date"),
        pl.col("knowledge_date_estimated").first().alias("first_estimated"),
        pl.col("value").last().alias("latest_value"), pl.col("knowledge_date").last().alias("latest_knowledge_date"),
        pl.col("knowledge_date").n_unique().cast(pl.Int32).alias("n_vintages")).sort("date")
        .with_columns((pl.col("latest_value") - pl.col("first_value")).alias("revision")))
    ind = next((i for i in (info.indicators if info else []) if i in CELL_INDICATORS), None)
    freq = info.frequency if info else "M"
    how = canonical_how(ind, info.scale if info else "level", info.tf if info else "level")
    if freq in ("D", "W"):
        g = g.with_columns(pl.lit(None, pl.Float64).alias("first_value_tf"), pl.lit(None, pl.Float64).alias("latest_value_tf"))
    else:
        lat = to_canonical(g.select("date", pl.col("latest_value").alias("value")), how, freq).rename({"value": "latest_value_tf"})
        fr = first_release_canonical(o, how, freq).rename({"first_value": "first_value_tf"})
        g = g.join(lat, on="date", how="left").join(fr, on="date", how="left")
    return g.with_columns(
        pl.lit(sid).alias("series_id"), pl.lit(kind).alias("vintage_kind"),
        pl.lit(how.replace("level100", "level")).alias("transform"),
        (pl.col("latest_value_tf") - pl.col("first_value_tf")).alias("revision_tf"),
        (pl.lit((info.scale if info else "level") in ("index", "level"))
         & ((pl.col("latest_value") / pl.col("first_value") - 1.0).abs() > REBASE_TOL)).fill_null(False).alias("rebased"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at")).select(list(REV_SCHEMA)).cast(REV_SCHEMA)  # type: ignore[arg-type]


def build_revisions(meta: dict[str, SeriesInfo], obs: pl.DataFrame) -> pl.DataFrame:
    ids = vintage_series(obs)
    if not ids:
        return pl.DataFrame(schema=REV_SCHEMA)
    now = datetime.now(UTC)
    by = {k[0]: g for k, g in obs.filter(pl.col("series_id").is_in(ids)).group_by("series_id")}
    frames = [series_revisions(sid, by[sid], meta.get(sid), now) for sid in ids if sid in by]
    # ALFRED series keep every period (full first-release vs latest pair); first-seen series only their revised periods
    frames = [f.filter((pl.col("vintage_kind") == "alfred") | (pl.col("n_vintages") > 1)) for f in frames]
    frames = [f for f in frames if f.height]
    return pl.concat(frames) if frames else pl.DataFrame(schema=REV_SCHEMA)


def _stats(vals: list[float]) -> dict[str, Any]:
    v = [x for x in vals if x is not None and math.isfinite(x)]
    n = len(v)
    if not n:
        return {"n": 0, "mean": None, "mean_abs": None, "std": None, "share_up": None, "max_abs": None}
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / (n - 1)) if n > 1 else None
    return {"n": n, "mean": m, "mean_abs": sum(abs(x) for x in v) / n, "std": sd,
            "share_up": sum(1 for x in v if x > 0) / n, "max_abs": max(abs(x) for x in v)}


def revision_stats(rows: list[dict[str, Any]], since: date | None = None) -> dict[str, Any]:
    """Revision-size statistics over periods >= since with 2+ vintages. Canonical basis first (base-invariant);
    native basis only on periods without a base change. Imputed (pre-archive) first releases are excluded."""
    rr = [r for r in rows if (since is None or r["date"] >= since) and (r.get("n_vintages") or 0) > 1]
    n_est = sum(1 for r in rr if r.get("first_estimated"))
    rr = [r for r in rr if not r.get("first_estimated")]
    n_reb = sum(1 for r in rr if r.get("rebased"))
    return {"basis": "canonical", "canonical": _stats([r.get("revision_tf") for r in rr]),
            "native": _stats([r.get("revision") for r in rr if not r.get("rebased")]),
            "n_rebased_excluded": n_reb, "n_estimated_excluded": n_est, "since": since.isoformat() if since else None}
