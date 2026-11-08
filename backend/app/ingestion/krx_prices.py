"""KRX Open API (official, free key KRX_OPENAPI_KEY) -> prices_staging. Job `prices_kr_krx` (key-gated).

Each call returns the whole market for one date (research §2.3): `stk_bydd_trd` (KOSPI) and `ksq_bydd_trd` (KOSDAQ)
at https://data-dbg.krx.co.kr/svc/apis/sto/<svc>?basDd=YYYYMMDD with header AUTH_KEY. 10,000 calls/day cap (tracked
with app.core.ratelimit.get_daily_quota("krx")); data from 2010, published ~08:00 KST next day.
Staged symbols use the Yahoo form (005930.KS / 086520.KQ) so resolution matches listings.yf_ticker. Without the
key the job returns `skipped` (yfinance .KS/.KQ covers KR meanwhile).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient
from app.core.logsetup import get_logger
from app.core.ratelimit import get_daily_quota
from app.core.storage import write_table
from app.normalize.prices import trading_sessions

log = get_logger(__name__)
BASE = "https://data-dbg.krx.co.kr/svc/apis/sto"
SERVICES = {"stk_bydd_trd": ".KS", "ksq_bydd_trd": ".KQ"}


def _num(x: Any) -> float | None:
    if x in (None, "", "-"):
        return None
    try:
        return float(str(x).replace(",", ""))
    except ValueError:
        return None


def krx_rows(payload: dict[str, Any], suffix: str, day: date) -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = []
    for r in payload.get("OutBlock_1", []) or []:
        code, close = r.get("ISU_CD") or r.get("ISU_SRT_CD"), _num(r.get("TDD_CLSPRC"))
        if not code or not close:
            continue
        rows.append({"source_id": "krx", "year": day.year, "symbol": f"{str(code).strip()[-6:]}{suffix}", "date": day,
                     "open": _num(r.get("TDD_OPNPRC")), "high": _num(r.get("TDD_HGPRC")), "low": _num(r.get("TDD_LWPRC")),
                     "close": close, "adj_close": None, "volume": _num(r.get("ACC_TRDVOL")), "vwap": None, "trades": None,
                     "currency": "KRW", "price_basis": "raw", "is_benchmark": False, "ingested_at": now})
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def run_krx(settings: Settings | None = None, days: list[date] | None = None, write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    key = s.secret("krx_openapi_key")
    if not key:
        return {"status": "skipped", "reason": "KRX_OPENAPI_KEY missing"}
    http = HttpClient("krx", settings=s, headers={"AUTH_KEY": key})
    quota = get_daily_quota("krx")
    today = datetime.now(UTC).date()
    days = days or sorted(trading_sessions("XKRX", today - timedelta(days=10), today - timedelta(days=1)) or [])[-3:]
    frames, failed = [], {}
    for d in days:
        for svc, suf in SERVICES.items():
            if quota is not None and not quota.try_consume():
                failed[f"{d}:{svc}"] = "daily quota reached"
                continue
            try:
                j = http.get(f"{BASE}/{svc}", {"basDd": d.strftime("%Y%m%d")}, cache_ttl=0).json()
                frames.append(krx_rows(j, suf, d))
            except Exception as e:  # noqa: BLE001
                failed[f"{d}:{svc}"] = f"{type(e).__name__}: {e}"[:200]
    df = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else pl.DataFrame()
    rep: dict[str, Any] = {"status": "partial" if failed else "success", "rows": df.height, "failed": failed}
    if write and df.height:
        rep["rows_written"] = write_table(df, "prices_staging", settings=s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    days = [date.fromisoformat(x) for x in p["days"]] if p.get("days") else None
    rep = run_krx(ctx.settings, days)
    if rep["status"] == "skipped":
        return JobResult(status="skipped", message=rep["reason"])
    return JobResult(status=rep["status"], rows_written={"prices_staging": rep.get("rows_written", 0)},
                     entities_failed=len(rep["failed"]), message=str(rep["failed"])[:500])
