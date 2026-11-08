"""Price-history workstream (docs/PRICE_HISTORY_PLAN.md): long daily OHLCV history from Yahoo Finance, batched and
resumable, for equities (securities_master) plus market instruments (FX pairs, commodity futures / indices, volatility
indices; app.ingestion.price_history_lists).

Run detached from backend/ (launcher scripts/price_history_launch.ps1; logs + PID under data/logs/price_history/,
state data/state/price_history.json, batch plan + spill files under data/state/price_history/):

    python -m app.ingestion.price_history inventory [--tag before]   # per-security coverage -> parquet + summary
    python -m app.ingestion.price_history plan [--batch 100]          # freeze the batch plan (instruments, full, rest)
    python -m app.ingestion.price_history backfill                    # fetch pending batches (resumable per batch)
    python -m app.ingestion.price_history flush                       # spill files -> prices_staging (append)
    python -m app.ingestion.price_history status

Yahoo pacing: one token of the machine-wide "yfinance" bucket per download, a base pause between batches that grows
x1.5 after a throttled / empty batch (yfinance swallows HTTP 429 into empty frames) and decays x0.9 after a good one,
plus 5/10/20/40-min cool-downs after consecutive throttled batches. Every fetched batch is written to a spill file
(Arrow IPC) and marked `fetched` in the plan; every `flush_every` batches the spill files are appended to
prices_staging / corporate_actions_staging and marked `flushed`, so a crash loses at most the in-flight batch and a
re-run skips every fetched/flushed batch.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.ingestion.price_history")

STATE_NAME = "price_history.json"


# ------------------------------------------------------------------------------------------------ state


def _s(settings: Settings | None = None) -> Settings:
    return settings or get_settings()


def state_path(settings: Settings | None = None) -> Path:
    return _s(settings).data_dir / "state" / STATE_NAME


def work_dir(settings: Settings | None = None) -> Path:
    d = _s(settings).data_dir / "state" / "price_history"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    if not p.exists():
        return {"workstream": "price_history", "plan": "docs/PRICE_HISTORY_PLAN.md", "tasks": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def update_state(task: str, settings: Settings | None = None, **fields: Any) -> dict[str, Any]:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        st = load_state(settings)
        t = st.setdefault("tasks", {}).setdefault(task, {})
        t.update(fields)
        t["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
        return st


# ------------------------------------------------------------------------------------------------ inventory


def inventory(settings: Settings | None = None) -> pl.DataFrame:
    """One row per securities_master security (active + delisted): primary listing / Yahoo symbol, market, prices_daily
    depth (first/last bar, bars, winning sources) and the Yahoo history already staged for the symbol."""
    from app.core.storage import duckdb_connect, register_views  # noqa: PLC0415

    s = _s(settings)
    con = duckdb_connect(settings=s)
    register_views(con, ["securities_master", "listings", "prices_daily", "prices_staging", "universe_membership"], s)
    df = con.execute("""
        WITH lst AS (
            SELECT security_id, arg_max(yf_ticker, coalesce(valid_to, DATE '9999-12-31')) AS yf_ticker,
                   arg_max(polygon_ticker, coalesce(valid_to, DATE '9999-12-31')) AS polygon_ticker,
                   arg_max(mic, coalesce(valid_to, DATE '9999-12-31')) AS mic,
                   arg_max(exchange_country, coalesce(valid_to, DATE '9999-12-31')) AS country,
                   min(valid_from) AS listed_from, max(valid_to) AS listed_to
            FROM listings WHERE is_primary GROUP BY 1),
        px AS (
            SELECT security_id, min(date) AS first_bar, max(date) AS last_bar, count(*) AS bars,
                   string_agg(DISTINCT source_id, ',' ORDER BY source_id) AS sources
            FROM prices_daily GROUP BY 1),
        st AS (
            SELECT symbol, min(date) AS yf_first, max(date) AS yf_last, count(DISTINCT date) AS yf_bars
            FROM prices_staging WHERE source_id = 'yfinance' GROUP BY 1),
        stall AS (
            SELECT symbol, string_agg(DISTINCT source_id, ',' ORDER BY source_id) AS staged_sources
            FROM prices_staging WHERE source_id <> 'polygon' GROUP BY 1),
        full_u AS (SELECT DISTINCT security_id FROM universe_membership WHERE universe_id = 'full' AND valid_to IS NULL)
        SELECT sm.security_id, sm.security_type, sm.is_active, sm.display_ticker, lst.yf_ticker, lst.polygon_ticker,
               lst.mic, lst.country, lst.listed_from, lst.listed_to, sm.first_trade_date,
               (full_u.security_id IS NOT NULL) AS in_full,
               px.first_bar, px.last_bar, coalesce(px.bars, 0) AS bars, px.sources,
               st.yf_first, st.yf_last, coalesce(st.yf_bars, 0) AS yf_bars, stall.staged_sources
        FROM securities_master sm
        LEFT JOIN lst USING (security_id)
        LEFT JOIN px USING (security_id)
        LEFT JOIN full_u USING (security_id)
        LEFT JOIN st ON st.symbol = lst.yf_ticker
        LEFT JOIN stall ON stall.symbol = lst.yf_ticker
    """).pl()
    con.close()
    from app.normalize.prices import country_to_region  # noqa: PLC0415

    df = df.with_columns(
        pl.when(pl.col("security_id").str.contains(r"^(IDX|ETF):"))
        .then(pl.lit("BENCH"))
        .otherwise(pl.col("country").map_elements(country_to_region, return_dtype=pl.Utf8)).alias("market"),
        ((pl.lit(date.today()) - pl.col("first_bar")).dt.total_days() / 365.25).round(1).alias("years"),
    )
    return df


def summarize(inv: pl.DataFrame) -> dict[str, Any]:
    """Coverage by market x security_type: securities, priced, Yahoo-staged, median first bar / years / bars."""
    g = (inv.group_by("market", "security_type")
         .agg(pl.len().alias("securities"), pl.col("is_active").not_().sum().alias("delisted"),
              (pl.col("bars") > 0).sum().alias("priced"), (pl.col("yf_bars") > 0).sum().alias("yahoo_staged"),
              pl.col("yf_ticker").is_not_null().sum().alias("has_yf_symbol"),
              pl.col("first_bar").min().alias("earliest"), pl.col("first_bar").median().alias("median_first"),
              pl.col("years").median().alias("median_years"), pl.col("bars").median().alias("median_bars"),
              (pl.col("years") >= 10).sum().alias("ge_10y"), (pl.col("years") < 3).sum().alias("lt_3y"),
              pl.col("bars").sum().alias("total_bars"))
         .sort("securities", descending=True))
    out = {f"{r['market']}/{r['security_type']}": {k: (str(v) if isinstance(v, (date, datetime)) else v)
                                                    for k, v in r.items() if k not in ("market", "security_type")}
           for r in g.iter_rows(named=True)}
    tot = {"securities": inv.height, "priced": int((inv["bars"] > 0).sum()), "bars": int(inv["bars"].sum()),
           "yahoo_staged": int((inv["yf_bars"] > 0).sum()), "ge_10y": int((inv["years"] >= 10).sum()),
           "median_years": float(inv.filter(pl.col("bars") > 0)["years"].median() or 0)}
    return {"total": tot, "by_market_type": out}


def task_inventory(tag: str = "before", settings: Settings | None = None) -> dict[str, Any]:
    s = _s(settings)
    t0 = time.time()
    inv = inventory(s)
    inv.write_parquet(work_dir(s) / f"inventory_{tag}.parquet", compression="zstd")
    summ = summarize(inv)
    summ["seconds"] = round(time.time() - t0, 1)
    update_state(f"inventory_{tag}", s, status="done", **summ)
    return summ


def task_compare(settings: Settings | None = None) -> dict[str, Any]:
    """inventory_before vs inventory_after by market x type: priced securities, bars, median years, >= 10 y, and the
    securities whose history now starts earlier (median years gained); instruments (IDX:/ETF:) listed by type."""
    s = _s(settings)
    b = pl.read_parquet(work_dir(s) / "inventory_before.parquet")
    a = pl.read_parquet(work_dir(s) / "inventory_after.parquet")
    j = a.join(b.select("security_id", pl.col("first_bar").alias("fb0"), pl.col("bars").alias("n0"),
                        pl.col("years").alias("y0")), on="security_id", how="left")
    j = j.with_columns(pl.col("n0").fill_null(0), pl.col("y0").fill_null(0.0),
                       ((pl.col("fb0").is_null() & (pl.col("bars") > 0))
                        | (pl.col("first_bar") < pl.col("fb0"))).alias("extended"))
    g = (j.group_by("market", "security_type").agg(
        pl.len().alias("securities"), (pl.col("n0") > 0).sum().alias("priced_before"),
        (pl.col("bars") > 0).sum().alias("priced_after"), pl.col("n0").sum().alias("bars_before"),
        pl.col("bars").sum().alias("bars_after"), pl.col("y0").median().alias("median_years_before"),
        pl.col("years").median().alias("median_years_after"), (pl.col("y0") >= 10).sum().alias("ge10y_before"),
        (pl.col("years") >= 10).sum().alias("ge10y_after"), pl.col("extended").sum().alias("extended"),
        (pl.col("years") - pl.col("y0")).filter(pl.col("extended")).median().alias("median_years_gained"),
        pl.col("first_bar").min().alias("earliest_after"))
         .sort("securities", descending=True))
    out = {f"{r['market']}/{r['security_type']}": {k: (str(v) if isinstance(v, (date, datetime)) else
                                                       (round(v, 1) if isinstance(v, float) else v))
                                                    for k, v in r.items() if k not in ("market", "security_type")}
           for r in g.iter_rows(named=True)}
    tot = {"securities": j.height, "priced_before": int((j["n0"] > 0).sum()), "priced_after": int((j["bars"] > 0).sum()),
           "bars_before": int(j["n0"].sum()), "bars_after": int(j["bars"].sum()),
           "extended": int(j["extended"].sum()),
           "median_years_before": float(j.filter(pl.col("n0") > 0)["y0"].median() or 0),
           "median_years_after": float(j.filter(pl.col("bars") > 0)["years"].median() or 0)}
    update_state("compare", s, status="done", total=tot, by_market_type=out)
    return {"total": tot, "by_market_type": out}


# ------------------------------------------------------------------------------------------------ plan


def plan_path(settings: Settings | None = None) -> Path:
    return work_dir(settings) / "plan.json"


def spill_dir(settings: Settings | None = None) -> Path:
    d = work_dir(settings) / "spill"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_plan(settings: Settings | None = None) -> dict[str, Any] | None:
    p = plan_path(settings)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_plan(plan: dict[str, Any], settings: Settings | None = None) -> None:
    p = plan_path(settings)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(plan, indent=0, default=str), encoding="utf-8")
    tmp.replace(p)


def build_plan(settings: Settings | None = None, batch: int = 100, inst_batch: int = 80,
               tag: str = "before") -> dict[str, Any]:
    """Freeze the batch plan: (1) new market instruments, (2) `full` members without Yahoo history (unpriced first,
    then shortest history first), (3) the rest of securities_master with a Yahoo symbol and no Yahoo history
    (delisted names only when no active listing re-uses the symbol)."""
    from app.ingestion.price_history_lists import history_instruments  # noqa: PLC0415

    s = _s(settings)
    p = work_dir(s) / f"inventory_{tag}.parquet"
    inv = pl.read_parquet(p) if p.exists() else inventory(s)
    staged_yf = set(inv.filter(pl.col("yf_bars") > 0).get_column("yf_ticker").to_list())
    inst = [t for t, *_ in history_instruments() if t not in staged_yf]
    inst += [t for t in inv.filter((pl.col("market") == "BENCH") & (pl.col("bars") == 0) & pl.col("yf_ticker").is_not_null())
             .get_column("yf_ticker").to_list() if t not in inst]
    cand = inv.filter(pl.col("yf_ticker").is_not_null() & (pl.col("yf_bars") == 0) & (pl.col("market") != "BENCH"))
    active_syms = set(inv.filter(pl.col("is_active")).get_column("yf_ticker").drop_nulls().to_list())
    recycled = cand.filter(~pl.col("is_active") & pl.col("yf_ticker").is_in(list(active_syms)))
    cand = cand.filter(pl.col("is_active") | ~pl.col("yf_ticker").is_in(list(active_syms)))
    order = [pl.col("bars") > 0, pl.col("first_bar").fill_null(date(2100, 1, 1))]
    full = cand.filter(pl.col("in_full") & pl.col("is_active")).sort(order, descending=[False, True])
    rest = cand.filter(~(pl.col("in_full") & pl.col("is_active"))).sort(["is_active", *order[1:]],
                                                                          descending=[True, True])
    batches: list[dict[str, Any]] = []

    def add(group: str, syms: list[str], n: int) -> None:
        syms = list(dict.fromkeys(syms))
        for i in range(0, len(syms), n):
            batches.append({"id": len(batches), "group": group, "symbols": syms[i:i + n], "status": "pending"})

    add("instruments", inst, inst_batch)
    add("full_gap", full.get_column("yf_ticker").to_list(), batch)
    add("rest", rest.get_column("yf_ticker").to_list(), batch)
    plan = {"created_at": datetime.now(UTC).isoformat(timespec="seconds"), "batch": batch, "batches": batches,
            "skipped_recycled": recycled.get_column("yf_ticker").to_list()}
    save_plan(plan, s)
    by_group: dict[str, int] = {}
    for b in batches:
        by_group[b["group"]] = by_group.get(b["group"], 0) + len(b["symbols"])
    update_state("plan", s, status="done", n_batches=len(batches), symbols_by_group=by_group,
                 skipped_recycled=recycled.height,
                 full_gap_by_market=dict(full.group_by("market").len().iter_rows()),
                 rest_by_market=dict(rest.group_by("market").len().iter_rows()))
    return plan


# ------------------------------------------------------------------------------------------------ backfill


def _canary_ok() -> bool:
    """True when Yahoo serves a known-good symbol (distinguishes throttling from a genuinely empty batch)."""
    import yfinance as yf  # noqa: PLC0415

    from app.core.ratelimit import get_limiter  # noqa: PLC0415

    get_limiter("yfinance").acquire()
    try:
        d = yf.download(["SPY"], period="5d", interval="1d", progress=False, threads=False, timeout=30)
        return d is not None and len(d) > 0
    except Exception:  # noqa: BLE001
        return False


def _stop_requested(s: Settings) -> bool:
    return (work_dir(s) / "STOP").exists()


def task_backfill(settings: Settings | None = None, flush_every: int = 10, base_pause: float = 6.0,
                  max_pause: float = 90.0, groups: list[str] | None = None) -> dict[str, Any]:
    """Fetch every pending batch of the plan (period=max, auto_adjust=False, actions=True); resumable per batch."""
    from app.ingestion.yf_prices import YFinancePricesAdapter, wide_to_long  # noqa: PLC0415

    s = _s(settings)
    plan = load_plan(s) or build_plan(s)
    task = "backfill"
    st0 = load_state(s).get("tasks", {}).get(task, {})
    n429 = int(st0.get("throttle_events", 0))
    t_run = time.time()
    prior_s = float(st0.get("fetch_seconds", 0.0))
    pause = base_pause
    ad = YFinancePricesAdapter(s, batch_size=plan["batch"], pause_s=0)
    todo = [b for b in plan["batches"] if b["status"] in ("pending", "retry") and (not groups or b["group"] in groups)]
    update_state(task, s, status="running", pid=os.getpid(), n_batches=len(plan["batches"]), todo=len(todo),
                 started_at=datetime.now(UTC).isoformat(timespec="seconds"))
    log.info("price_history backfill: %d batches to fetch", len(todo))
    cool = 0
    k = 0
    while k < len(todo):
        if _stop_requested(s):
            update_state(task, s, status="stopped", reason="STOP file")
            return {"status": "stopped"}
        b = todo[k]
        t0 = time.time()
        pdf = ad._download(b["symbols"], "max", None)  # noqa: SLF001 - shared retry/backoff on exceptions
        long = wide_to_long(pdf, b["symbols"]) if pdf is not None else pl.DataFrame()
        got = set(long.get_column("symbol").unique().to_list()) if long.height else set()
        if len(got) < max(1, len(b["symbols"]) // 10) and not _canary_ok():
            n429 += 1
            wait = [300, 600, 1200, 2400][min(cool, 3)]
            cool += 1
            pause = min(max_pause, pause * 1.5)
            log.warning("batch %d: %d/%d symbols and canary failed -> throttled; cool-down %ds", b["id"], len(got),
                        len(b["symbols"]), wait)
            update_state(task, s, status="cooldown", throttle_events=n429, last_throttle=datetime.now(UTC).isoformat(
                timespec="seconds"), cooldown_s=wait, pause_s=round(pause, 1))
            time.sleep(wait)
            continue
        cool = 0
        if long.height:
            long.write_ipc(spill_dir(s) / f"b{b['id']:05d}.arrow", compression="zstd")
        b.update(status="fetched", ok=len(got), nodata=[x for x in b["symbols"] if x not in got], rows=long.height,
                 seconds=round(time.time() - t0, 1), fetched_at=datetime.now(UTC).isoformat(timespec="seconds"))
        save_plan(plan, s)
        k += 1
        pause = max(base_pause, pause * 0.9)
        fetched = [x for x in plan["batches"] if x["status"] in ("fetched", "flushed")]
        el = prior_s + time.time() - t_run
        n_sym = sum(len(x["symbols"]) for x in fetched)
        update_state(task, s, status="running", done=len(fetched), n_batches=len(plan["batches"]),
                     symbols_done=n_sym, symbols_ok=sum(x.get("ok", 0) for x in fetched),
                     rows_fetched=sum(x.get("rows", 0) for x in fetched), throttle_events=n429,
                     pause_s=round(pause, 1), fetch_seconds=round(el),
                     eta_s=round((time.time() - t_run) / max(1, k) * (len(todo) - k)),
                     last_batch={"id": b["id"], "group": b["group"], "ok": len(got), "n": len(b["symbols"]),
                                 "rows": long.height, "s": b["seconds"]})
        log.info("batch %d (%s): %d/%d symbols, %d rows in %.1fs; pause %.1fs", b["id"], b["group"], len(got),
                 len(b["symbols"]), long.height, b["seconds"], pause)
        if sum(1 for x in plan["batches"] if x["status"] == "fetched") >= flush_every:
            task_flush(s)
            plan = load_plan(s) or plan
            todo = [x for x in plan["batches"] if x["status"] in ("pending", "retry")
                    and (not groups or x["group"] in groups)]
            k = 0
        if k < len(todo):
            time.sleep(pause + random.random())
    task_flush(s)
    plan = load_plan(s) or plan
    fetched = [x for x in plan["batches"] if x["status"] in ("fetched", "flushed")]
    update_state(task, s, status="done", done=len(fetched), throttle_events=n429,
                 fetch_seconds=round(prior_s + time.time() - t_run),
                 symbols_ok=sum(x.get("ok", 0) for x in fetched),
                 symbols_nodata=sum(len(x.get("nodata", [])) for x in fetched),
                 nodata_sample=[y for x in fetched for y in x.get("nodata", [])][:40])
    return {"status": "done", "throttle_events": n429}


def task_flush(settings: Settings | None = None) -> dict[str, Any]:
    """Append every fetched batch's spill file to prices_staging (+ corporate_actions_staging) and mark it flushed."""
    from app.core.storage import write_table  # noqa: PLC0415
    from app.ingestion.yf_prices import CurrencyCache, long_to_staging  # noqa: PLC0415
    from app.normalize.prices import BENCHMARKS  # noqa: PLC0415

    s = _s(settings)
    plan = load_plan(s)
    if not plan:
        return {"flushed": 0}
    ready = [b for b in plan["batches"] if b["status"] == "fetched"]
    if not ready:
        return {"flushed": 0}
    t0 = time.time()
    files = [spill_dir(s) / f"b{b['id']:05d}.arrow" for b in ready]
    frames = [pl.read_ipc(f) for f in files if f.exists()]
    rows = acts = 0
    if frames:
        long = pl.concat(frames, how="diagonal_relaxed")
        syms = long.get_column("symbol").unique().to_list()
        cur = CurrencyCache(s).resolve(syms)
        prices, actions = long_to_staging(long, cur, {b.yf_ticker for b in BENCHMARKS})
        if prices.height:
            rows = write_table(prices, "prices_staging", mode="append", settings=s)
        if actions.height:
            acts = write_table(actions, "corporate_actions_staging", settings=s)
    for b in ready:
        b["status"] = "flushed"
    save_plan(plan, s)
    for f in files:
        f.unlink(missing_ok=True)
    st = load_state(s).get("tasks", {}).get("flush", {})
    update_state("flush", s, status="done", flushes=int(st.get("flushes", 0)) + 1,
                 rows_staged=int(st.get("rows_staged", 0)) + rows, actions_staged=int(st.get("actions_staged", 0)) + acts,
                 last_flush={"batches": len(ready), "rows": rows, "actions": acts, "s": round(time.time() - t0, 1)})
    log.info("flush: %d batches -> %d staged rows, %d actions in %.1fs", len(ready), rows, acts, time.time() - t0)
    return {"flushed": len(ready), "rows": rows, "actions": acts}


# ------------------------------------------------------------------------------------------------ reconciliation


def task_reconcile(settings: Settings | None = None, tolerance: float = 0.01) -> dict[str, Any]:
    """Raw close of each official / exchange source vs Yahoo (after Yahoo's split un-adjustment) on overlapping
    (symbol, date): disagreement share at 1% / 5%, median and p99 relative difference, worst symbols."""
    from app.core.storage import duckdb_connect, register_views  # noqa: PLC0415

    s = _s(settings)
    con = duckdb_connect(settings=s)
    register_views(con, ["prices_staging"], s)
    con.execute("""
        CREATE TEMP TABLE ov AS
        SELECT o.source_id, o.symbol, o.date, o.close AS c_off, y.close AS c_yf,
               abs(y.close - o.close) / o.close AS rel
        FROM (SELECT source_id, CASE WHEN source_id = 'polygon' THEN replace(symbol, '.', '-') ELSE symbol END AS symbol,
                     date, close FROM prices_staging WHERE source_id <> 'yfinance' AND close > 0) o
        JOIN (SELECT symbol, date, close FROM prices_staging WHERE source_id = 'yfinance' AND close > 0) y
          USING (symbol, date)""")
    by_src = con.execute(f"""
        SELECT source_id, count(*) AS rows, count(DISTINCT symbol) AS symbols,
               avg(CASE WHEN rel > {tolerance} THEN 1 ELSE 0 END) AS share_gt_1pct,
               avg(CASE WHEN rel > 0.05 THEN 1 ELSE 0 END) AS share_gt_5pct,
               median(rel) AS median_rel, quantile_cont(rel, 0.99) AS p99_rel,
               count(DISTINCT symbol) FILTER (WHERE rel > {tolerance}) AS symbols_with_disagreement
        FROM ov GROUP BY 1 ORDER BY 1""").fetchall()
    out: dict[str, Any] = {"tolerance": tolerance, "by_source": {}}
    for src, rows, syms, s1, s5, med, p99, sd in by_src:
        worst = con.execute(f"""
            SELECT symbol, count(*) FILTER (WHERE rel > {tolerance}) AS days, count(*) AS overlap, max(rel) AS max_rel
            FROM ov WHERE source_id = ? GROUP BY 1 HAVING days > 0 ORDER BY days DESC LIMIT 8""", [src]).fetchall()
        out["by_source"][src] = {"overlap_rows": int(rows), "symbols": int(syms), "share_gt_1pct": round(s1, 5),
                                 "share_gt_5pct": round(s5, 5), "median_rel": float(med or 0), "p99_rel": float(p99 or 0),
                                 "symbols_with_disagreement": int(sd),
                                 "worst": [{"symbol": w[0], "days": int(w[1]), "overlap": int(w[2]),
                                            "max_rel": round(float(w[3]), 4)} for w in worst]}
    con.close()
    update_state("reconcile", s, status="done", **out)
    return out


# ------------------------------------------------------------------------------------------------ storage


def table_footprint(table: str, settings: Settings | None = None) -> dict[str, Any]:
    """Files, bytes, rows (Parquet footers) and bytes/row of a lake table."""
    import pyarrow.parquet as pq  # noqa: PLC0415

    from app.core.storage import _parquet_files, table_dir  # noqa: PLC0415

    files = _parquet_files(table_dir(table, settings))
    size = sum(f.stat().st_size for f in files)
    rows = sum(pq.ParquetFile(f).metadata.num_rows for f in files)
    return {"files": len(files), "bytes": size, "gb": round(size / 1e9, 3), "rows": rows,
            "bytes_per_row": round(size / rows, 2) if rows else None}


def compact_table(table: str, settings: Settings | None = None) -> dict[str, Any]:
    """Rewrite every partition of `table` in its contract's physical layout (narrowed float32 columns, derived columns
    dropped, rows sorted, tuned zstd level), one file per partition, de-duplicated on the primary key (latest
    ingested_at wins). Holds the table lock per partition, so concurrent writers interleave safely."""
    from app.core.schema import get_contract  # noqa: PLC0415
    from app.core.storage import (  # noqa: PLC0415
        _parquet_files,
        _rmtree,
        _swap_in,
        _write_partition_files,
        logical_lazy,
        table_dir,
        table_lock,
    )

    s = _s(settings)
    c = get_contract(table)
    before = table_footprint(table, s)
    root = table_dir(c, s)
    dirs = sorted({f.parent for f in _parquet_files(root)})
    file_pk = [k for k in c.primary_key if k not in c.partition_by]
    t0 = time.time()
    dups = 0
    for i, d in enumerate(dirs):
        with table_lock(table, s):
            files = [f for f in d.glob("*.parquet")]
            if not files:
                continue
            df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
            df = logical_lazy(df.lazy(), c).collect()
            if "ingested_at" in df.columns:
                df = df.sort("ingested_at", nulls_last=False)
            n0 = df.height
            df = df.unique(subset=file_pk, keep="last", maintain_order=True) if file_pk else df
            dups += n0 - df.height
            keep = [x for x in c.column_names if x in df.columns and x not in c.partition_by]
            tmp = d.parent / f".tmp-{d.name}-{random.randrange(16**8):08x}"
            _rmtree(tmp)
            _write_partition_files(tmp, df.select(keep), c)
            _swap_in(tmp, d)
        if i % 25 == 0:
            update_state(f"compact_{table}", s, status="running", partitions_done=i + 1, partitions=len(dirs))
    after = table_footprint(table, s)
    out = {"before": before, "after": after, "duplicates_removed": dups, "partitions": len(dirs),
           "seconds": round(time.time() - t0, 1),
           "saved_gb": round((before["bytes"] - after["bytes"]) / 1e9, 3)}
    update_state(f"compact_{table}", s, status="done", **out)
    return out


# ------------------------------------------------------------------------------------------------ cli


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingestion.price_history")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inventory")
    p.add_argument("--tag", default="before")
    p = sub.add_parser("plan")
    p.add_argument("--batch", type=int, default=100)
    p.add_argument("--force", action="store_true", help="rebuild even if a plan exists")
    p = sub.add_parser("backfill")
    p.add_argument("--flush-every", type=int, default=10)
    p.add_argument("--pause", type=float, default=6.0)
    p.add_argument("--groups", default=None, help="comma list: instruments,full_gap,rest")
    sub.add_parser("flush")
    p = sub.add_parser("compact")
    p.add_argument("--table", action="append", required=True, help="prices_staging and/or prices_daily")
    sub.add_parser("footprint")
    sub.add_parser("reconcile")
    sub.add_parser("compare")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "inventory":
        out = task_inventory(a.tag)
        print(json.dumps(out["total"], indent=1))
    elif a.cmd == "plan":
        if load_plan() and not a.force:
            print("plan exists (use --force to rebuild)")
            return 0
        plan = build_plan(batch=a.batch)
        print(len(plan["batches"]), "batches")
    elif a.cmd == "backfill":
        try:
            out = task_backfill(flush_every=a.flush_every, base_pause=a.pause,
                                groups=a.groups.split(",") if a.groups else None)
        except Exception as e:  # noqa: BLE001 - leave a trace in the state file for the resume protocol
            update_state("backfill", status="failed", error=f"{type(e).__name__}: {e}"[:500])
            raise
        print(json.dumps(out))
    elif a.cmd == "flush":
        print(json.dumps(task_flush()))
    elif a.cmd == "compact":
        for tb in a.table:
            print(tb, json.dumps(compact_table(tb), default=str))
    elif a.cmd == "compare":
        print(json.dumps(task_compare(), indent=1, default=str))
    elif a.cmd == "reconcile":
        print(json.dumps(task_reconcile(), indent=1, default=str))
    elif a.cmd == "footprint":
        out = {tb: table_footprint(tb) for tb in ("prices_daily", "prices_staging")}
        update_state("footprint", **{k: v for k, v in out.items()})
        print(json.dumps(out, indent=1))
    elif a.cmd == "status":
        st = load_state()
        plan = load_plan()
        if plan:
            cnt: dict[str, int] = {}
            for b in plan["batches"]:
                cnt[b["status"]] = cnt.get(b["status"], 0) + 1
            st["plan_status"] = cnt
        print(json.dumps(st, indent=1, default=str)[:20000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
