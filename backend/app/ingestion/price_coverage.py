"""Price-coverage expansion (docs/MODEL_STUDIO_PLAN.md, Progress P1-P3): resumable bulk loaders.

Run detached from backend/ (each task is resumable: per-item done-files under data/state/price_coverage/, summary in
data/state/price_coverage.json, logs under data/logs/price_coverage/):

    python -m app.ingestion.price_coverage cn --worker 0 --workers 3     # BaoStock A-shares, sharded by worker
    python -m app.ingestion.price_coverage tw --market twse              # TWSE daily archive (newest first)
    python -m app.ingestion.price_coverage tw --market tpex              # TPEx daily archive
    python -m app.ingestion.price_coverage br --first-year 2000          # B3 COTAHIST yearly files
    python -m app.ingestion.price_coverage seeds                         # seeds/price_coverage.csv for ref_universe
    python -m app.ingestion.price_coverage yf --group jp                 # P2 paced yfinance backfill (jp|kr|hk|etf|idx)
    python -m app.ingestion.price_coverage delisted                      # P3 Polygon inactive tickers -> registry
    python -m app.ingestion.price_coverage status

Bulk loads append to prices_staging (mode=append; the in-flight batch of a crashed run may be appended twice, which
prices_finalize tolerates: the winner per (security, date) is unique). Corporate actions are upserted.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import write_table

log = get_logger("app.ingestion.price_coverage")


# ------------------------------------------------------------------------------------------------ state


def _s(settings: Settings | None = None) -> Settings:
    return settings or get_settings()


def state_path(settings: Settings | None = None) -> Path:
    return _s(settings).data_dir / "state" / "price_coverage.json"


def done_dir(settings: Settings | None = None) -> Path:
    d = _s(settings).data_dir / "state" / "price_coverage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    if not p.exists():
        return {"workstream": "price_coverage", "plan": "docs/MODEL_STUDIO_PLAN.md (P1-P3)", "tasks": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def update_state(task: str, settings: Settings | None = None, **fields: Any) -> dict[str, Any]:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=120):
        st = load_state(settings)
        t = st.setdefault("tasks", {}).setdefault(task, {})
        t.update(fields)
        t["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        st["updated_at"] = t["updated_at"]
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
        return st


def read_done(name: str, settings: Settings | None = None) -> set[str]:
    p = done_dir(settings) / f"{name}.done"
    return set(p.read_text(encoding="utf-8").split()) if p.exists() else set()


def mark_done(name: str, items: list[str], settings: Settings | None = None) -> None:
    if items:
        with (done_dir(settings) / f"{name}.done").open("a", encoding="utf-8") as fh:
            fh.write("\n".join(items) + "\n")


def _append(df: pl.DataFrame, table: str, s: Settings) -> int:
    if df is None or df.is_empty():
        return 0
    return write_table(df, table, mode="append" if table == "prices_staging" else None, settings=s)


# ------------------------------------------------------------------------------------------------ P1 China (BaoStock)


def cn_list(settings: Settings | None = None, max_age_h: float = 20.0) -> pl.DataFrame:
    """A-share list (listed, type=stock) + CSRC industry, cached under data/raw/baostock/stock_list/."""
    s = _s(settings)
    p = s.raw_dir / "baostock" / "stock_list" / "a_shares.parquet"
    if p.exists() and time.time() - p.stat().st_mtime < max_age_h * 3600:
        return pl.read_parquet(p)
    import baostock as bs  # noqa: PLC0415

    from app.ingestion.baostock_prices import fetch_stock_list  # noqa: PLC0415

    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login {lg.error_code} {lg.error_msg}")
    try:
        df = fetch_stock_list(bs)
    finally:
        bs.logout()
    p.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p)
    return df


def task_cn(worker: int = 0, workers: int = 1, start: str = "2000-01-01", batch: int = 100,
            settings: Settings | None = None, reverse: bool = False) -> dict[str, Any]:
    """BaoStock bulk history for shard `worker` of `workers`. reverse=True: helper that walks the same shard from the
    end (own done-file `<task>_rev`, skips what the forward worker finished); stop the forward process once they meet."""
    import baostock as bs  # noqa: PLC0415

    from app.core.ratelimit import get_limiter  # noqa: PLC0415
    from app.ingestion.baostock_prices import FIELDS, bs_rows, derive_cn_actions, yf_to_bs  # noqa: PLC0415

    s = _s(settings)
    task = f"p1_cn_w{worker}" + ("_rev" if reverse else "")
    lst = None
    for attempt in range(5):
        try:
            lst = cn_list(s)
            break
        except Exception as e:  # noqa: BLE001
            log.warning("cn list failed (%s); retry", e)
            time.sleep(60 * (attempt + 1))
    if lst is None:
        update_state(task, s, status="failed", error="stock list unavailable")
        return {"status": "failed"}
    syms = sorted(lst.get_column("yf_ticker").to_list())
    mine = [x for i, x in enumerate(syms) if i % workers == worker]
    done: set[str] = set()  # every CN done-file (any sharding / reverse helper): re-sharding never redoes symbols
    for f in done_dir(s).glob("p1_cn_w*.done"):
        done |= read_done(f.stem, s)
    todo = [x for x in mine if x not in done]
    if reverse:
        todo = todo[::-1]
    update_state(task, s, status="running", pid=_pid(), n_total=len(mine), n_done=len(mine) - len(todo), start=start)
    lim = get_limiter("baostock")
    end = datetime.now(UTC).date().isoformat()
    t0 = time.time()
    rows_total = acts_total = 0
    failed: dict[str, str] = {}

    def login() -> None:
        for k in range(10):
            lg = bs.login()
            if lg.error_code == "0":
                return
            log.warning("baostock login %s %s; retry", lg.error_code, lg.error_msg)
            time.sleep(30 * (k + 1))
        raise RuntimeError("baostock login failed")

    login()
    try:
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            frames = []
            ok: list[str] = []
            for sym in part:
                for attempt in range(4):
                    try:
                        lim.acquire()
                        rs = bs.query_history_k_data_plus(yf_to_bs(sym), FIELDS, start_date=start, end_date=end,
                                                          frequency="d", adjustflag="3")
                        if rs.error_code != "0":
                            raise RuntimeError(f"{rs.error_code} {rs.error_msg}")
                        data = []
                        while rs.next():
                            data.append(rs.get_row_data())
                        frames.append(bs_rows(sym, data, rs.fields, keep_preclose=True))
                        ok.append(sym)
                        failed.pop(sym, None)
                        break
                    except Exception as e:  # noqa: BLE001 - network / session drop -> relogin with backoff
                        failed[sym] = f"{type(e).__name__}: {e}"[:200]
                        log.warning("cn %s attempt %d: %s", sym, attempt + 1, e)
                        time.sleep(10 * (attempt + 1))
                        try:
                            bs.logout()
                        except Exception:  # noqa: BLE001
                            pass
                        login()
            df = pl.concat([f for f in frames if f.height], how="vertical_relaxed") if any(f.height for f in frames) \
                else pl.DataFrame()
            if df.height:
                acts = derive_cn_actions(df.select("symbol", "date", "close", "preclose"))
                rows_total += _append(df.drop("preclose"), "prices_staging", s)
                acts_total += _append(acts, "corporate_actions_staging", s)
            mark_done(task, ok, s)
            n_done = len(mine) - len(todo) + i + len(part)
            el = time.time() - t0
            update_state(task, s, n_done=n_done, rows=rows_total, actions=acts_total, n_failed=len(failed),
                         failed_sample=dict(list(failed.items())[:10]), elapsed_s=round(el),
                         eta_s=round(el / max(1, i + len(part)) * (len(todo) - i - len(part))))
            log.info("cn w%d %d/%d rows=%d", worker, n_done, len(mine), rows_total)
    finally:
        try:
            bs.logout()
        except Exception:  # noqa: BLE001
            pass
    st = "done" if not failed else "done_with_failures"
    update_state(task, s, status=st, rows=rows_total, actions=acts_total, runtime_s=round(time.time() - t0),
                 failed=failed)
    return {"status": st, "rows": rows_total}


# ------------------------------------------------------------------------------------------------ P1 Taiwan


def task_tw(market: str, start: date | None = None, batch_days: int = 20,
            settings: Settings | None = None) -> dict[str, Any]:
    from app.ingestion.twse_prices import FIRST_DAY, _client, fetch_day, fetch_exrights, sessions  # noqa: PLC0415

    s = _s(settings)
    task = f"p1_tw_{market}"
    http = _client(market, s)
    first = max(start or FIRST_DAY[market], FIRST_DAY[market])
    end = datetime.now(UTC).date() - timedelta(days=1)
    days = sorted(sessions(first, end), reverse=True)  # newest first: recent years usable early
    done = read_done(task, s)
    todo = [d for d in days if d.isoformat() not in done]
    update_state(task, s, status="running", pid=_pid(), first_day=first.isoformat(), n_days=len(days),
                 n_done=len(days) - len(todo))
    t0 = time.time()
    rows_total = 0
    empty: list[str] = []
    errors: dict[str, str] = {}
    for i in range(0, len(todo), batch_days):
        part = todo[i:i + batch_days]
        frames, ok = [], []
        for d in part:
            for attempt in range(5):
                try:
                    f = fetch_day(market, d, http)
                    frames.append(f)
                    ok.append(d.isoformat())
                    if f.is_empty():
                        empty.append(d.isoformat())
                    errors.pop(d.isoformat(), None)
                    break
                except Exception as e:  # noqa: BLE001 - block / timeout: back off hard (IP block lasts minutes)
                    errors[d.isoformat()] = f"{type(e).__name__}: {e}"[:200]
                    wait = 120 * (attempt + 1)
                    log.warning("tw %s %s attempt %d: %s; sleep %ds", market, d, attempt + 1, e, wait)
                    time.sleep(wait)
        df = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else pl.DataFrame()
        rows_total += _append(df, "prices_staging", s)
        mark_done(task, ok, s)
        el = time.time() - t0
        update_state(task, s, n_done=len(days) - len(todo) + i + len(part), rows=rows_total, n_empty=len(empty),
                     empty_sample=empty[-5:], n_errors=len(errors), errors_sample=dict(list(errors.items())[:5]),
                     oldest_done=part[-1].isoformat(), elapsed_s=round(el),
                     eta_s=round(el / max(1, i + len(part)) * (len(todo) - i - len(part))))
    # ex-rights / ex-dividend tables, one call per year
    acts = 0
    ydone = read_done(task + "_exr", s)
    for y in range(end.year, first.year - 1, -1):
        if str(y) in ydone and y < end.year:
            continue
        try:
            ca = fetch_exrights(market, max(date(y, 1, 1), first), min(date(y, 12, 31), end), http)
            acts += _append(ca, "corporate_actions_staging", s)
            mark_done(task + "_exr", [str(y)], s)
        except Exception as e:  # noqa: BLE001
            errors[f"exr{y}"] = f"{type(e).__name__}: {e}"[:200]
    st = "done" if not errors else "done_with_failures"
    update_state(task, s, status=st, rows=rows_total, actions=acts, runtime_s=round(time.time() - t0),
                 errors_sample=dict(list(errors.items())[:10]), n_errors=len(errors))
    return {"status": st, "rows": rows_total, "actions": acts}


# ------------------------------------------------------------------------------------------------ P1 Brazil


def task_br(first_year: int = 2000, last_year: int | None = None, settings: Settings | None = None) -> dict[str, Any]:
    from app.ingestion.b3_cotahist import run_b3  # noqa: PLC0415

    s = _s(settings)
    task = "p1_br"
    last_year = last_year or datetime.now(UTC).year
    done = read_done(task, s)
    years = [y for y in range(last_year, first_year - 1, -1) if str(y) not in done or y == last_year]
    update_state(task, s, status="running", pid=_pid(), years=f"{first_year}-{last_year}")
    t0 = time.time()
    rows: dict[str, int] = {}
    failed: dict[str, str] = {}
    for y in years:
        for attempt in range(3):
            rep = run_b3(s, year=y)
            if rep["status"] != "failed":
                rows[str(y)] = rep.get("rows_written", 0)
                if y != last_year:
                    mark_done(task, [str(y)], s)
                failed.pop(str(y), None)
                break
            failed[str(y)] = json.dumps(rep.get("failed"))[:200]
            time.sleep(60 * (attempt + 1))
        update_state(task, s, rows_by_year=rows, failed=failed, elapsed_s=round(time.time() - t0))
    st = "done" if not failed else "done_with_failures"
    update_state(task, s, status=st, runtime_s=round(time.time() - t0))
    return {"status": st, "rows": sum(rows.values())}


# ------------------------------------------------------------------------------------------------ P3 Polygon 2y grouped


def task_polygon(days: int = 504, chunk: int = 10, settings: Settings | None = None) -> dict[str, Any]:
    """Polygon grouped-daily for every missing XNYS session of the free-tier window (~2 y): all US tickers incl. the
    ones delisted since (survivorship). Shared machine-wide 4/min bucket; resumable (loaded dates are skipped)."""
    from app.ingestion.polygon_prices import PolygonPricesAdapter  # noqa: PLC0415

    s = _s(settings)
    task = "p3_polygon_grouped"
    ad = PolygonPricesAdapter(s)
    ad.check_keys()
    todo = sorted(ad.target_dates(None, days), reverse=True)  # newest first: the free-tier edge (~2 y) comes last
    update_state(task, s, status="running", pid=_pid(), n_missing_at_start=len(todo))
    t0 = time.time()
    rows, failed, n429 = 0, {}, 0
    for i in range(0, len(todo), chunk):
        part = todo[i:i + chunk]
        fr = ad.fetch(dates=part)
        failed.update(fr.entities_failed)
        n429 += int(fr.meta.get("polygon_429") or 0)
        for k, v in ad.normalize(fr).items():
            rows += _append(v, k, s)
        el = time.time() - t0
        update_state(task, s, n_done=i + len(part), rows=rows, n_failed=len(failed), polygon_429=n429,
                     failed_sample=dict(list(failed.items())[:5]), elapsed_s=round(el),
                     eta_s=round(el / max(1, i + len(part)) * (len(todo) - i - len(part))))
        if any("polygon 40" in str(v) for v in fr.entities_failed.values()):  # 401/403: key or free-tier depth edge
            update_state(task, s, status="stopped_at_plan_limit", oldest_tried=min(part).isoformat(),
                         runtime_s=round(time.time() - t0))
            return {"status": "stopped_at_plan_limit", "rows": rows}
    st = "done" if not failed else "done_with_failures"
    update_state(task, s, status=st, runtime_s=round(time.time() - t0))
    return {"status": st, "rows": rows}


# ------------------------------------------------------------------------------------------------ helpers


def _pid() -> int:
    import os  # noqa: PLC0415

    return os.getpid()


def status(settings: Settings | None = None) -> dict[str, Any]:
    st = load_state(settings)
    return {k: {kk: v.get(kk) for kk in ("status", "n_done", "n_total", "n_days", "rows", "actions", "eta_s",
                                         "updated_at") if kk in v} for k, v in st.get("tasks", {}).items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.ingestion.price_coverage")
    sub = ap.add_subparsers(dest="task", required=True)
    a = sub.add_parser("cn")
    a.add_argument("--worker", type=int, default=0)
    a.add_argument("--workers", type=int, default=1)
    a.add_argument("--start", default="2000-01-01")
    a.add_argument("--reverse", action="store_true")
    a = sub.add_parser("tw")
    a.add_argument("--market", choices=["twse", "tpex"], required=True)
    a.add_argument("--start", default=None)
    a = sub.add_parser("br")
    a.add_argument("--first-year", type=int, default=2000)
    a.add_argument("--last-year", type=int, default=None)
    sub.add_parser("status")
    a = sub.add_parser("polygon")
    a.add_argument("--days", type=int, default=504)
    for name in ("seeds", "yf", "delisted"):
        a = sub.add_parser(name)
        a.add_argument("--group", default=None)
        a.add_argument("--markets", default=None)
    args = ap.parse_args(argv)
    if args.task == "cn":
        out = task_cn(args.worker, args.workers, args.start, reverse=args.reverse)
    elif args.task == "tw":
        out = task_tw(args.market, date.fromisoformat(args.start) if args.start else None)
    elif args.task == "br":
        out = task_br(args.first_year, args.last_year)
    elif args.task == "status":
        out = status()
    elif args.task == "polygon":
        out = task_polygon(args.days)
    else:
        from app.ingestion import price_coverage_ext as ext  # noqa: PLC0415

        out = ext.dispatch(args.task, args)
    print(json.dumps(out, indent=1, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
