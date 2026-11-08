"""Per-section staleness metadata of a serving snapshot (F16 / R2): table `serving_sections`.

build_serving_db no longer waits on the analytics chain: market data (prices, FX, macro, filings, events) publishes
even when an analytics job failed, and the previous analytics outputs stay in the lake. This table tells the API /
UI which sections of the published snapshot are current:

    section | grp | data_as_of | status (fresh | stale | failed | missing) | source_job | last_job_status | detail

Computed from the snapshot's own `_freshness` table (max data date per lake table), the `sec` table (per-region
price as-of: the date at least half the region's securities reached) and the latest job_runs status of each
section's writer job. Written by app.api.serving (_Builder.meta) - never fails the build.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

#: (section, group, table, source job, tolerance in calendar days vs the snapshot as_of)
SECTION_SPECS: tuple[tuple[str, str, str, str, int], ...] = (
    ("fx", "market", "fx_rates", "fx_rates", 4),
    ("macro", "market", "macro_observations", "macro_global", 7),
    ("filings", "market", "filings_index", "sec_filings_index", 4),
    ("insider", "market", "insider_transactions", "sec_form4", 5),
    ("events", "market", "event_daily_agg", "gdelt_stream", 4),
    ("news", "market", "news_items", "news_ingest", 4),
    ("cftc", "market", "cftc_positions", "cftc_cot", 10),
    ("fundamentals", "analytics", "fundamentals_canonical", "fundamentals_normalize", 10),
    ("valuation", "analytics", "valuation_daily", "valuation_daily", 4),
    ("scores", "analytics", "composite_scores", "scoring", 4),
    ("technicals", "analytics", "tech_levels", "tech_levels", 4),
    ("ml", "analytics", "ml_predictions", "ml_predict", 4),
    ("sentiment", "analytics", "sentiment_daily", "sentiment_daily", 4),
    ("cross_asset", "analytics", "cross_asset_signals", "cross_asset_signals", 4),
    ("moat", "analytics", "moat", "moat", 10),
    ("conformal", "analytics", "conformal_envelopes", "conformal_envelopes", 4),
    ("conformal_peers", "analytics", "conformal_peer_scores", "conformal_peer_groups", 4),
)
PRICE_TOLERANCE_DAYS = 4
BAD_JOB = ("failed",)


def compute_sections(fresh: dict[str, date | None], regions: dict[str, date], job_status: dict[str, str],
                     as_of: date) -> list[dict[str, Any]]:
    """fresh: {table: max data date}; regions: {region: price as-of}; job_status: {job_id: latest status}."""
    out: list[dict[str, Any]] = []
    for reg, d in sorted(regions.items()):
        lag = (as_of - d).days
        st = job_status.get("prices_finalize")
        status = "failed" if st in BAD_JOB and lag > 0 else ("fresh" if lag <= PRICE_TOLERANCE_DAYS else "stale")
        out.append({"section": f"prices:{reg}", "grp": "market", "data_as_of": d, "status": status,
                    "source_job": "prices_finalize", "last_job_status": st, "detail": f"{lag} d behind the snapshot as_of"})
    for section, grp, table, job, tol in SECTION_SPECS:
        d = fresh.get(table)
        st = job_status.get(job)
        if d is not None and d > as_of:
            d = as_of  # forward-looking rows (calendars) never make a section look newer than the snapshot
        if d is None:
            status, detail = "missing", "no data in the lake"
        else:
            lag = (as_of - d).days
            if st in BAD_JOB:
                status, detail = "failed", f"last {job} run failed; serving the previous output ({lag} d old)"
            elif lag > tol:
                status, detail = "stale", f"{lag} d behind the snapshot as_of (tolerance {tol} d)"
            else:
                status, detail = "fresh", f"{lag} d behind the snapshot as_of"
        out.append({"section": section, "grp": grp, "data_as_of": d, "status": status, "source_job": job,
                    "last_job_status": st, "detail": detail})
    return out


def write_sections(con: Any, target: str, as_of: date, settings: Any = None) -> int:
    """CREATE {target}serving_sections from the snapshot's _freshness / sec tables + job_runs. Returns row count."""
    from app.ops.health import region_as_of

    T = target
    fresh: dict[str, date | None] = {}
    try:
        for t, d in con.execute(f"SELECT table_name, max(last_date) FROM {T}_freshness GROUP BY 1").fetchall():
            fresh[t] = d
    except Exception:
        pass
    regions: dict[str, date] = {}
    try:
        rows = con.execute(f"""SELECT region, px_date, count(*) FROM {T}sec WHERE px_date IS NOT NULL
                               AND px_date >= DATE '{(as_of - timedelta(days=30)).isoformat()}' GROUP BY 1, 2""").fetchall()
        regions = {k: date.fromisoformat(v) for k, v in region_as_of(rows).items()}
    except Exception:
        pass
    status: dict[str, str] = {}
    try:
        import polars as pl

        from app.core.storage import read_table

        jr = read_table("job_runs", columns=["job_id", "status", "started_at"], settings=settings)
        if not jr.is_empty():
            jr = jr.filter(pl.col("status") != "running").sort("started_at", descending=True).unique("job_id", keep="first")
            status = dict(zip(jr["job_id"].to_list(), jr["status"].to_list(), strict=False))
    except Exception:
        pass
    rows = compute_sections(fresh, regions, status, as_of)
    con.execute(f"CREATE OR REPLACE TABLE {T}serving_sections (section VARCHAR, grp VARCHAR, data_as_of DATE, "
                "status VARCHAR, source_job VARCHAR, last_job_status VARCHAR, detail VARCHAR)")
    if rows:
        con.executemany(f"INSERT INTO {T}serving_sections VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [(r["section"], r["grp"], r["data_as_of"], r["status"], r["source_job"], r["last_job_status"],
                          r["detail"]) for r in rows])
    return len(rows)
