"""Jobs rates_curves (nightly + startup sync) and rates_curves_backfill (manual / filler campaign).

rates_curves: per source (app.rates.registry.SOURCES) fetch from (last stored date - overlap) - or from the curve's
history start when nothing is stored - and upsert yield_curves (+ yield_curve_params for Svensson sources).
rates_curves_backfill: the same with backfill=True per source from the history start, resumable: a source whose
backfill finished is skipped (data/state/rates.json backfill.<source>.done), so a cancelled / OOM-killed run picks
up at the next source. One source at a time keeps the peak RAM low (BoE archive ~0.6 GB, others < 0.2 GB).

params (both): sources [list], curves [list of curve ids -> their sources], start (ISO date, forces the window),
backfill (bool), force (bool: redo finished backfills), overlap_days (int).
PIT: knowledge_date = min(date + publication lag (Publish rule), retrieval date); estimated unless first seen today.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger

from . import registry as R
from .common import now_utc, read_state, update_state
from .sources import LOADERS, Loaded

log = get_logger("app.rates.curves_job")

OVERLAP_DAYS = {"D": 10, "M": 150, "batch": 70}


def _add_bdays(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def publication_date(c: R.Curve, d: date) -> date:
    p = c.publish
    if p.freq in ("M", "batch"):
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return nxt - timedelta(days=1) + timedelta(days=p.lag_days)
    return _add_bdays(d, p.lag) if p.lag else d


def last_dates(settings: Settings) -> dict[str, date]:
    """curve_id -> last stored date (DuckDB over the files; only the last two years are scanned)."""
    from app.core.storage import _parquet_files, table_dir  # noqa: PLC0415

    root = table_dir("yield_curves", settings)
    files = [f for f in _parquet_files(root)]
    if not files:
        return {}
    import duckdb  # noqa: PLC0415

    years = sorted({int(p.parent.name.split("=")[1]) for p in files if p.parent.name.startswith("year=")})
    keep = [f for f in files if f.parent.name.startswith("year=") and int(f.parent.name.split("=")[1]) >= (years[-1] - 1 if years else 0)]
    out: dict[str, date] = {}
    con = duckdb.connect()
    try:
        con.execute("SET memory_limit = '512MB'; SET threads = 2")
        lst = ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in keep)
        for cid, d in con.execute(f"SELECT curve_id, max(date) FROM read_parquet([{lst}], union_by_name = true) GROUP BY 1").fetchall():
            out[cid] = d
        # curves without rows in the last two years (monthly LT10 with long lags): one more full pass
        missing = [c.curve_id for c in R.ALL_CURVES if c.curve_id not in out]
        if missing and len(keep) < len(files):
            lst = ", ".join("'" + f.as_posix().replace("'", "''") + "'" for f in files)
            q = ", ".join("'" + m + "'" for m in missing)
            for cid, d in con.execute(f"SELECT curve_id, max(date) FROM read_parquet([{lst}], union_by_name = true) "
                                      f"WHERE curve_id IN ({q}) GROUP BY 1").fetchall():
                out[cid] = d
    finally:
        con.close()
    return out


def source_start(source: str, last: dict[str, date], overlap: int | None = None) -> tuple[date, bool]:
    """(window start, has_history) for a source: min over its curves of last - overlap; history start when a curve
    has nothing stored (that curve then needs the backfill)."""
    curves = R.by_source(source)
    starts, fresh = [], True
    for c in curves:
        d = last.get(c.curve_id)
        if d is None:
            fresh = False
            starts.append(date.fromisoformat(c.history_start))
            continue
        key = c.publish.freq if c.publish.freq in OVERLAP_DAYS else "D"
        starts.append(d - timedelta(days=overlap if overlap is not None else OVERLAP_DAYS[key]))
    return (min(starts) if starts else date.today() - timedelta(days=10)), fresh


def frames(res: Loaded, today: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    if res.rows:
        df = pl.DataFrame(res.rows, schema={"date": pl.Date, "curve_id": pl.Utf8, "tenor_months": pl.Int16,
                                            "type": pl.Utf8, "kind": pl.Utf8, "value_bp_x100": pl.Int32,
                                            "source": pl.Utf8})
        df = df.unique(subset=["date", "curve_id", "tenor_months", "type"], keep="last", maintain_order=True)
        pub = {(cid, d): publication_date(R.BY_ID[cid], d) for cid, d in
               df.select("curve_id", "date").unique().iter_rows() if cid in R.BY_ID}
        kd = pl.DataFrame([{"curve_id": k[0], "date": k[1], "kd": min(v, today), "est": v < today}
                           for k, v in pub.items()], schema={"curve_id": pl.Utf8, "date": pl.Date, "kd": pl.Date,
                                                             "est": pl.Boolean})
        df = (df.join(kd, on=["curve_id", "date"], how="left")
              .with_columns(pl.col("date").dt.year().cast(pl.Int16).alias("year"),
                            pl.col("kd").fill_null(pl.col("date")).alias("knowledge_date"),
                            pl.col("est").fill_null(True).alias("knowledge_date_estimated"),
                            pl.lit(now_utc()).alias("ingested_at"))
              .drop("kd", "est"))
    else:
        df = pl.DataFrame()
    if res.params:
        pf = pl.DataFrame(res.params).unique(subset=["curve_id", "date"], keep="last", maintain_order=True)
        pf = pf.with_columns(
            pl.struct("curve_id", "date").map_elements(
                lambda r: min(publication_date(R.BY_ID[r["curve_id"]], r["date"]), today), return_dtype=pl.Date
            ).alias("knowledge_date"),
            pl.lit(now_utc()).alias("ingested_at"))
    else:
        pf = pl.DataFrame()
    return df, pf


def write_frames(df: pl.DataFrame, pf: pl.DataFrame, settings: Settings) -> tuple[int, int]:
    from app.core.storage import write_table  # noqa: PLC0415

    n = write_table(df, "yield_curves", mode="upsert", settings=settings) if df.height else 0
    m = write_table(pf, "yield_curve_params", mode="upsert", settings=settings) if pf.height else 0
    return n, m


def _wanted_sources(params: dict[str, Any]) -> list[str]:
    srcs = list(params.get("sources") or [])
    for cid in params.get("curves") or []:
        c = R.BY_ID.get(cid)
        if c is not None:
            srcs.append("ecb_irs" if (c.group == "lt10" and c.source_id == "ecb") else c.source_id)
    srcs = [s for s in dict.fromkeys(srcs) if s in LOADERS]
    return srcs or list(R.SOURCES)


def run_curves(settings: Settings, params: dict[str, Any] | None = None, cancel: Any = None) -> dict[str, Any]:
    params = params or {}
    backfill = bool(params.get("backfill"))
    force = bool(params.get("force"))
    today = date.today()
    st = read_state(settings)
    done_bf = (st.get("backfill") or {})
    last = last_dates(settings)
    report: dict[str, Any] = {"sources": {}, "rows": 0, "params_rows": 0, "requests": 0, "failed": [], "notes": []}
    for src in _wanted_sources(params):
        if cancel is not None:
            cancel()
        if backfill and not force and (done_bf.get(src) or {}).get("done"):
            report["sources"][src] = {"status": "skipped", "detail": "backfill already done"}
            continue
        t0 = time.perf_counter()
        if params.get("start"):
            start = date.fromisoformat(str(params["start"]))
        elif backfill:
            start = min(date.fromisoformat(c.history_start) for c in R.by_source(src))
        else:
            start, _ = source_start(src, last, params.get("overlap_days"))
        try:
            res = LOADERS[src](settings, start, backfill or start < today - timedelta(days=40))
        except Exception as e:  # noqa: BLE001 - one failing source never blocks the others
            log.warning("rates_curves: %s failed: %s", src, e)
            report["failed"].append(src)
            report["sources"][src] = {"status": "failed", "detail": f"{type(e).__name__}: {e}"[:300]}
            continue
        df, pf = frames(res, today)
        n, m = write_frames(df, pf, settings)
        curves = {}
        if df.height:
            for cid, d0, d1, k in df.group_by("curve_id").agg(pl.col("date").min().alias("d0"), pl.col("date").max().alias("d1"),
                                                              pl.len()).iter_rows():
                curves[cid] = {"from": d0.isoformat(), "to": d1.isoformat(), "rows": k}
        status = "ok" if n else ("no_data" if not res.notes else "failed")
        if status == "failed":
            report["failed"].append(src)
        report["sources"][src] = {"status": status, "rows": n, "params_rows": m, "start": start.isoformat(),
                                  "requests": res.requests, "curves": curves, "notes": res.notes[:10],
                                  "seconds": round(time.perf_counter() - t0, 1)}
        report["rows"] += n
        report["params_rows"] += m
        report["requests"] += res.requests
        report["notes"] += [f"{src}: {x}" for x in res.notes[:5]]
        patch: dict[str, Any] = {"curves": {cid: {"last_date": v["to"], "last_run_rows": v["rows"],
                                                  "updated_at": now_utc().isoformat(timespec="seconds")}
                                            for cid, v in curves.items()}}
        if backfill and status == "ok":
            patch["backfill"] = {src: {"done": True, "at": now_utc().isoformat(timespec="seconds"), "rows": n,
                                       "from": start.isoformat()}}
        update_state(patch, settings)
        log.info("rates_curves: %s %s rows=%d params=%d in %.1fs", src, status, n, m, time.perf_counter() - t0)
    update_state({"last_run": {"at": now_utc().isoformat(timespec="seconds"), "backfill": backfill,
                               "rows": report["rows"], "failed": report["failed"]}}, settings)
    return report


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_curves(ctx.settings, dict(ctx.params or {}), ctx.check_cancelled)
    n_ok = sum(1 for v in rep["sources"].values() if v.get("status") in ("ok", "skipped", "no_data"))
    status = "success" if not rep["failed"] else ("partial" if n_ok else "failed")
    return JobResult(status=status, rows_written={"yield_curves": rep["rows"], "yield_curve_params": rep["params_rows"]},
                     entities_ok=n_ok, entities_failed=len(rep["failed"]), http_requests=rep["requests"],
                     message=("; ".join(rep["notes"][:6]) or None) if rep["failed"] or rep["notes"] else None)


def run_backfill_job(ctx):  # noqa: ANN001
    from app.scheduler.base import JobResult  # noqa: PLC0415

    params = {**dict(ctx.params or {}), "backfill": True}
    rep = run_curves(ctx.settings, params, ctx.check_cancelled)
    n_ok = sum(1 for v in rep["sources"].values() if v.get("status") in ("ok", "skipped", "no_data"))
    status = "success" if not rep["failed"] else ("partial" if n_ok else "failed")
    return JobResult(status=status, rows_written={"yield_curves": rep["rows"], "yield_curve_params": rep["params_rows"]},
                     entities_ok=n_ok, entities_failed=len(rep["failed"]), http_requests=rep["requests"],
                     message="; ".join(rep["notes"][:6]) or None)


def probe_progress(settings: Settings | None = None) -> dict[str, Any]:
    """Filler campaign probe (kind rates_backfill): sources whose backfill finished / all sources."""
    st = read_state(settings)
    bf = st.get("backfill") or {}
    done = [s for s in R.SOURCES if (bf.get(s) or {}).get("done")]
    total = len(R.SOURCES)
    last = max((v.get("at") for v in bf.values() if isinstance(v, dict) and v.get("at")), default=None)
    return {"done": len(done) == total, "pct": round(100 * len(done) / total, 1), "done_units": len(done),
            "total_units": total, "unit": "sources", "last_run_at": last,
            "detail": f"backfilled: {', '.join(done) or '-'}; pending: {', '.join(s for s in R.SOURCES if s not in done) or '-'}"}
