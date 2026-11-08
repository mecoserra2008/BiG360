"""Job `earnings_dates_fetch`: dated earnings announcements -> earnings_announcements.

Sources
* jpx_schedule (official, company-announced): JPX "Scheduled Dates for Earnings Announcements" workbooks
  (https://www.jpx.co.jp/listing/event-schedules/financial-announcement/), one per quarter-end month, ~3k TSE
  companies each. Rows: date, 4-char code, fiscal year end, quarter -> period end. Undetermined dates are skipped.
* yahoo (provider): yfinance `Ticker.get_earnings_dates(limit)` (Yahoo earnings calendar page) per primary listing,
  past and upcoming dates with time of day. ONLY dates / times are kept (Yahoo's EPS estimate columns are dropped:
  no consensus is stored). Upcoming Yahoo dates are labelled "provider estimate".

* hkex_bmn / cninfo / fca_nsm (official, app.earnings.announce): HKEX board-meeting notifications, CNINFO
  periodic-report booking schedule (A-shares), FCA NSM results announcements and notices of results (UK / EU LEIs).

Universe (COVERAGE_V2 W1, 2026-10-31): every security of `full` (~21k), MVP universes first.
Incremental - a security is due when (in this order of priority):
  1. one of its known dates lies within [-3, +10] days of today and the last fetch is >= `near_refresh_h` (20 h) old
     (confirms the date / time and picks up the release);
  2. it was never fetched;
  3. its next known date is > 45 days ahead: after `far_refresh_days` (30); otherwise after `refresh_days` (14);
  4. its last answer was empty: after `empty_refresh_days` (30) (no Yahoo calendar for the listing).
Bounded by `max_minutes`; rows are written every 150 securities (a timeout never loses work, the next run resumes).
params: universes (comma list, default mvp_us,mvp_global,eu_broad,full = priority order), limit (Yahoo rows,
default 40), refresh_days, near_refresh_h, empty_refresh_days, far_refresh_days, max_minutes (default 20), yahoo /
jpx / hkex / cninfo / nsm (default true), nsm_since (NSM back-fill start), rate (Yahoo requests/s, default 1.5).
"""

from __future__ import annotations

import gzip
import io
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.storage import scan_table
from app.earnings import common as C

log = C.log
JPX_INDEX = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
JPX_BASE = "https://www.jpx.co.jp"
JPX_Q = {"First quarter": "Q1", "Second quarter": "Q2", "Third quarter": "Q3", "Fourth quarter": "Q4",
         "Fiscal year": "Q4"}
DEFAULT_UNIVERSES = ("mvp_us", "mvp_global", "eu_broad", "full")


#: sentinel event_date of the marker row older versions wrote when Yahoo returned no dates (records the fetch time).
#: F26: no longer written - the fetch time of an empty answer goes to the fetch log (state file below) and existing
#: marker rows are purged from the yahoo partition on the next run (purge_sentinels)
SENTINEL = date(1900, 1, 1)
FETCH_LOG = "earnings/yahoo_fetch_log.json"


def _fetch_log_path(s: Settings) -> Any:
    return s.state_dir / FETCH_LOG


def read_fetch_log(s: Settings) -> dict[str, str]:
    """security_id -> ISO time of the last Yahoo answer WITHOUT dates (dated answers are in the table itself)."""
    from app.ops.paths import read_json  # noqa: PLC0415

    return read_json(_fetch_log_path(s), {}) or {}


def write_fetch_log(s: Settings, log_: dict[str, str]) -> None:
    from app.ops.paths import write_json_atomic  # noqa: PLC0415

    write_json_atomic(_fetch_log_path(s), log_)


def purge_sentinels(s: Settings) -> int:
    """Move the fetch times of legacy 1900-01-01 marker rows into the fetch log and rewrite the yahoo partition
    without them (F26: 11k sentinel rows made the table look dirty and broke date-range readers). Idempotent."""
    try:
        y = scan_table("earnings_announcements", s).filter(pl.col("source_id") == "yahoo").collect()
    except Exception as e:  # noqa: BLE001
        log.warning("earnings sentinel purge: table unreadable (%s); skipped", e)
        return 0
    sent = y.filter(pl.col("event_date") == SENTINEL)
    if sent.is_empty():
        return 0
    fl = read_fetch_log(s)
    for r in sent.group_by("security_id").agg(pl.col("fetched_at").max()).iter_rows(named=True):
        if r["fetched_at"] is not None:
            iso_ts = r["fetched_at"].isoformat()
            if iso_ts > fl.get(r["security_id"], ""):
                fl[r["security_id"]] = iso_ts
    write_fetch_log(s, fl)
    keep = y.filter(pl.col("event_date") != SENTINEL)
    C.write(keep, "earnings_announcements", s, mode="overwrite_partition")
    log.info("earnings sentinel purge: %d marker rows removed, %d fetch times kept in %s", sent.height, len(fl), FETCH_LOG)
    return sent.height


def _raw_dir(s: Settings, source: str, day: date) -> Any:
    p = s.raw_dir / "earnings" / source / f"dt={day.isoformat()}"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------------------------------------ JPX schedule


def jpx_period_end(fye: date, q: str) -> date:
    """Quarter end of quarter q (Q1..Q4) of the fiscal year ending `fye`."""
    back = {"Q1": 9, "Q2": 6, "Q3": 3, "Q4": 0}[q]
    if C.is_month_end(fye):
        return C.add_months_end(fye, -back)
    m = fye.month - back
    y = fye.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    try:
        return date(y, m, fye.day)
    except ValueError:
        return C.month_end(y, m)


def parse_jpx_workbook(content: bytes) -> list[dict[str, Any]]:
    import pandas as pd  # noqa: PLC0415

    x = pd.read_excel(io.BytesIO(content), header=None)
    hdr = next((i for i in range(min(20, len(x))) if str(x.iat[i, 1]).startswith("コード")), 4)
    out = []
    for r in x.iloc[hdr + 1:].itertuples(index=False):
        d, code, fye, qlab = r[0], r[1], r[4], r[8]
        if not isinstance(d, datetime) or not isinstance(fye, datetime) or pd.isna(code):
            continue
        q = JPX_Q.get(str(qlab).strip())
        if q is None:
            continue
        code = str(code).strip()
        code = code.removesuffix(".0")
        out.append({"code": code, "event_date": d.date(), "period_end": jpx_period_end(fye.date(), q),
                    "period_hint": f"{q} FYE {fye.date().isoformat()}", "fye": fye.date(), "quarter": q})
    return out


def fetch_jpx(s: Settings, meta: pl.DataFrame) -> pl.DataFrame:
    import httpx  # noqa: PLC0415

    from app.core.robots_policy import httpx_request_hook  # noqa: PLC0415 - A9 robots deny list

    hdrs = {"User-Agent": "Mozilla/5.0 (BiG Equity 360 research; local)"}
    with httpx.Client(timeout=60, headers=hdrs, follow_redirects=True, event_hooks={"request": [httpx_request_hook]}) as cl:
        page = cl.get(JPX_INDEX).text
        links = sorted(set(re.findall(r'href="([^"]*kessan[^"]*\.xlsx?)"', page)))
        rows: list[dict[str, Any]] = []
        today = C.now_utc().date()
        for ln in links:
            url = ln if ln.startswith("http") else JPX_BASE + ln
            content = cl.get(url).content
            (_raw_dir(s, "jpx_schedule", today) / url.rsplit("/", 1)[-1]).write_bytes(content)
            rows += parse_jpx_workbook(content)
            time.sleep(1.0)
    if not rows:
        return pl.DataFrame()
    jp = meta.filter(pl.col("timezone") == "Asia/Tokyo").select("issuer_id", "security_id", "local_symbol") \
        .drop_nulls("local_symbol").unique("local_symbol")
    df = pl.DataFrame(rows).join(jp, left_on="code", right_on="local_symbol", how="inner")
    now = C.now_utc()
    return df.select(pl.lit("jpx_schedule").alias("source_id"), "security_id", "issuer_id", "event_date",
                     pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("event_ts"), pl.lit(False).alias("time_known"),
                     "period_hint", pl.col("code").alias("provider_symbol"),
                     pl.lit(today).alias("knowledge_date"), pl.lit(now).alias("fetched_at")).unique(
        ["security_id", "event_date"])


# ------------------------------------------------------------------------------------------------ Yahoo


def yahoo_dates(tk: str, limit: int = 40) -> list[dict[str, Any]]:
    import yfinance as yf  # noqa: PLC0415

    df = yf.Ticker(tk).get_earnings_dates(limit=limit)
    if df is None or len(df) == 0:
        return []
    out = []
    for ts in df.index:
        if ts is None:
            continue
        t = ts.to_pydatetime()
        known = not (t.hour == 0 and t.minute == 0)
        out.append({"ts": t.astimezone(UTC), "local_date": t.date(), "time_known": known})
    return out


def _yahoo_targets(s: Settings, meta: pl.DataFrame, universes: list[str], refresh_days: int,
                   near_refresh_h: float = 20.0, empty_refresh_days: int = 30,
                   far_refresh_days: int = 30) -> list[dict[str, Any]]:
    secs = C.universe_securities(universes, s)
    m = meta.filter(pl.col("security_id").is_in(list(secs)) & pl.col("yf_ticker").is_not_null())
    order = {u: i for i, u in enumerate(universes)}
    um = (scan_table("universe_membership", s).filter(pl.col("universe_id").is_in(universes) & pl.col("valid_to").is_null())
          .select("security_id", "universe_id").collect()
          .with_columns(pl.col("universe_id").replace_strict(order, default=99).alias("prio"))
          .group_by("security_id").agg(pl.col("prio").min()))
    m = m.join(um, on="security_id", how="left")
    today = C.now_utc().date()
    near = (pl.col("event_date") >= today - timedelta(days=3)) & (pl.col("event_date") <= today + timedelta(days=10))
    try:
        have = (scan_table("earnings_announcements", s).filter(pl.col("source_id") == "yahoo")
                .group_by("security_id").agg(pl.col("fetched_at").max().alias("last_fetch"),
                                             pl.col("event_date").filter(near).count().alias("near"),
                                             pl.col("event_date").filter(pl.col("event_date") >= today).min()
                                             .alias("next_date")).collect())
    except Exception:  # noqa: BLE001 - table empty
        have = pl.DataFrame(schema={"security_id": pl.Utf8, "last_fetch": pl.Datetime("us", "UTC"), "near": pl.UInt32,
                                    "next_date": pl.Date})
    have = have.with_columns(pl.lit(None, pl.Datetime("us", "UTC")).alias("empty_fetch"))
    fl = read_fetch_log(s)  # empty answers (F26: no sentinel rows any more)
    if fl:
        lf = pl.DataFrame({"security_id": list(fl), "log_fetch": list(fl.values())}).with_columns(
            pl.col("log_fetch").str.to_datetime(time_zone="UTC", strict=False))
        have = (have.drop("empty_fetch").join(lf, on="security_id", how="full", coalesce=True)
                .with_columns(
                    # empty = the newest fetch returned no dates (no dated row fetched as recently)
                    pl.when(pl.col("log_fetch").is_not_null()
                            & (pl.col("last_fetch").is_null() | (pl.col("log_fetch") >= pl.col("last_fetch"))))
                    .then(pl.col("log_fetch")).otherwise(None).alias("empty_fetch"),
                    pl.max_horizontal("last_fetch", "log_fetch").alias("last_fetch"),
                    pl.col("near").fill_null(0)).drop("log_fetch"))
    m = m.join(have, on="security_id", how="left")
    now = C.now_utc()
    last = pl.col("last_fetch")
    is_near = (pl.col("near").fill_null(0) > 0) & (last < now - timedelta(hours=near_refresh_h))
    empty = pl.col("empty_fetch").is_not_null()
    far = pl.col("next_date").is_not_null() & (pl.col("next_date") > today + timedelta(days=45))
    regular = ~empty & pl.when(far).then(last < now - timedelta(days=far_refresh_days)) \
        .otherwise(last < now - timedelta(days=refresh_days))
    empty_due = empty & (last < now - timedelta(days=empty_refresh_days))
    rank = (pl.when(is_near.fill_null(False)).then(0).when(last.is_null()).then(1)
            .when(regular.fill_null(False)).then(2).when(empty_due.fill_null(False)).then(3).otherwise(None))
    m = m.with_columns(rank.alias("_rank")).filter(pl.col("_rank").is_not_null())
    m = m.sort(["_rank", "prio", "last_fetch"], nulls_last=False)
    return m.select("security_id", "issuer_id", "yf_ticker", "timezone", "region").to_dicts()


def _flush(s: Settings, rows: list[dict[str, Any]], raw: dict[str, Any]) -> int:
    """Write a chunk (rows + compact raw landing) so a timeout never loses fetched work (resumable)."""
    n = 0
    if raw:
        p = _raw_dir(s, "yahoo", C.now_utc().date()) / f"dates_{time.time_ns()}.json.gz"
        p.write_bytes(gzip.compress(orjson.dumps(raw)))
    if rows:
        f = pl.DataFrame(rows, infer_schema_length=None).unique(["source_id", "security_id", "event_date"], keep="last")
        n = C.write(keep_first_knowledge(f, s), "earnings_announcements", s, mode="upsert")
    rows.clear()
    raw.clear()
    return n


def fetch_yahoo(s: Settings, meta: pl.DataFrame, universes: list[str], limit: int, refresh_days: int,
                max_minutes: float, rate: float, ctx: Any = None, chunk: int = 150,
                due: dict[str, Any] | None = None) -> dict[str, Any]:
    """Fetch Yahoo dates for the due securities; rows are written every `chunk` securities."""
    from app.core.ratelimit import RateLimit, get_limiter  # noqa: PLC0415

    lim = get_limiter("yfinance_earnings", RateLimit(rate, 2))
    purged = purge_sentinels(s)
    targets = _yahoo_targets(s, meta, universes, refresh_days, **(due or {}))
    fetch_log = read_fetch_log(s)
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    raw: dict[str, Any] = {}
    ok = fail = empty = written = 0
    consecutive_fail = 0
    for k, t in enumerate(targets):
        if k and k % chunk == 0:
            written += _flush(s, rows, raw)
            write_fetch_log(s, fetch_log)  # resumable: empty answers survive a timeout too
        if time.perf_counter() - t0 > max_minutes * 60:
            break
        if ctx is not None:
            ctx.check_cancelled()
        lim.acquire()
        try:
            got = yahoo_dates(t["yf_ticker"], limit)
            consecutive_fail = 0
        except Exception as e:  # noqa: BLE001 - one ticker must not stop the run
            fail += 1
            consecutive_fail += 1
            if "Too Many Requests" in str(e) or consecutive_fail >= 25:
                log.warning("yahoo earnings dates: backing off after %s (%d consecutive failures)", type(e).__name__,
                            consecutive_fail)
                time.sleep(min(300, 30 * consecutive_fail))
                if consecutive_fail >= 40:
                    break
            continue
        now = C.now_utc()
        # an empty answer still counts as fetched: its time goes to the fetch log (no sentinel row in the table)
        if not got:
            empty += 1
            raw[t["yf_ticker"]] = []
            fetch_log[t["security_id"]] = now.isoformat()
            continue
        ok += 1
        raw[t["yf_ticker"]] = [[g["ts"].isoformat(), g["time_known"]] for g in got]
        for g in got:
            ld = C.local_dt(g["ts"], t["timezone"], t["region"])
            rows.append({"source_id": "yahoo", "security_id": t["security_id"], "issuer_id": t["issuer_id"],
                         "event_date": ld.date() if g["time_known"] else g["local_date"],
                         "event_ts": g["ts"] if g["time_known"] else None, "time_known": g["time_known"],
                         "period_hint": None, "provider_symbol": t["yf_ticker"], "knowledge_date": now.date(),
                         "fetched_at": now})
    written += _flush(s, rows, raw)
    write_fetch_log(s, fetch_log)
    return {"targets": len(targets), "ok": ok, "empty": empty, "failed": fail, "rows_written": written,
            "sentinels_purged": purged,
            "seconds": round(time.perf_counter() - t0, 1)}


def keep_first_knowledge(new: pl.DataFrame, s: Settings) -> pl.DataFrame:
    """Carry the earliest knowledge_date of an existing (source, security, date) row (PIT: first seen)."""
    if new.is_empty():
        return new
    try:
        old = (scan_table("earnings_announcements", s).select("source_id", "security_id", "event_date",
                                                             pl.col("knowledge_date").alias("_k0")).collect())
    except Exception:  # noqa: BLE001
        return new
    out = new.join(old, on=["source_id", "security_id", "event_date"], how="left")
    return out.with_columns(pl.min_horizontal("knowledge_date", "_k0").alias("knowledge_date")).drop("_k0")


def run_fetch(settings: Settings | None = None, params: dict[str, Any] | None = None, ctx: Any = None) -> dict[str, Any]:
    s = settings or get_settings()
    p = params or {}
    meta = C.issuer_meta(s)
    rep: dict[str, Any] = {}
    frames = []
    from app.earnings import announce as A  # noqa: PLC0415

    ann_frames, rep["announce"] = A.fetch_all(s, meta, p)
    frames += ann_frames
    if str(p.get("jpx", "true")).lower() not in ("0", "false", "no"):
        try:
            j = fetch_jpx(s, meta)
            rep["jpx_rows"] = j.height
            if j.height:
                frames.append(j)
        except Exception as e:  # noqa: BLE001 - optional source
            log.warning("JPX schedule fetch failed: %s", e)
            rep["jpx_error"] = str(e)[:200]
    if str(p.get("yahoo", "true")).lower() not in ("0", "false", "no"):
        unis = [u.strip() for u in str(p.get("universes") or ",".join(DEFAULT_UNIVERSES)).split(",") if u.strip()]
        due = {"near_refresh_h": float(p.get("near_refresh_h", 20)),
               "empty_refresh_days": int(p.get("empty_refresh_days", 30)),
               "far_refresh_days": int(p.get("far_refresh_days", 30))}
        yr = fetch_yahoo(s, meta, unis, int(p.get("limit", 40)), int(p.get("refresh_days", 14)),
                         float(p.get("max_minutes", 20)), float(p.get("rate", 1.5)), ctx, due=due)
        rep["yahoo"] = yr
    n = int((rep.get("yahoo") or {}).get("rows_written", 0))
    for f in frames:
        f = keep_first_knowledge(f.unique(["source_id", "security_id", "event_date"], keep="last"), s)
        n += C.write(f, "earnings_announcements", s, mode="upsert")
    rep["rows_written"] = n
    C.update_state("earnings_dates_fetch", rep | {"at": C.now_utc().isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_fetch(ctx.settings, ctx.params or {}, ctx)
    y = rep.get("yahoo") or {}
    ann_err = any("error" in (v or {}) for v in (rep.get("announce") or {}).values())
    status = "success" if not rep.get("jpx_error") and not y.get("failed") and not ann_err else "partial"
    return JobResult(status=status, rows_written={"earnings_announcements": rep["rows_written"]},
                     entities_ok=int(y.get("ok", 0)) + int(rep.get("jpx_rows", 0) > 0),
                     entities_failed=int(y.get("failed", 0)), message=str(rep)[:2000])
