"""Taiwan official daily all-stock quotes (TWSE + TPEx, no key) -> prices_staging. Job `prices_tw` (nightly).

Sources (live-tested 2026-10-11):
  TWSE  https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=YYYYMMDD&type=ALLBUT0999&response=json
        one call = whole market for one day (archive from 2004-02-11); table '每日收盤行情' (證券代號, 成交股數, 成交筆數,
        成交金額, 開盤價, 最高價, 最低價, 收盤價, ...). Non-trading days -> stat '很抱歉，沒有符合條件的資料!'.
  TPEx  https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date=YYYY/MM/DD&type=EW&response=json
        tables '上櫃股票行情' + '管理股票' (archive from 2007; the column set changed over time -> mapped by header).
  Ex-rights / ex-dividend result tables (corporate_actions_staging):
  TWSE  https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate=YYYYMMDD&endDate=YYYYMMDD&response=json  (from 2003-05)
  TPEx  https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate=YYYY/MM/DD&endDate=YYYY/MM/DD&response=json
Both hosts block bursts (~3 req / 5 s): shared machine-wide buckets `twse` / `tpex` at 0.3 req/s.

Only ordinary shares are kept (4-digit codes 1000-9999; ETFs 00xx, warrants, preferreds 2881A, TDR 91xxxx dropped).
Staged symbols use the Yahoo form (2330.TW / 6488.TWO) so they resolve via listings.yf_ticker. Prices are RAW TWD.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import write_table

log = get_logger(__name__)

TWSE_DAY = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json"
TPEX_DAY = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date={d}&type=EW&response=json"
TWSE_EXR = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U?startDate={a}&endDate={b}&response=json"
TPEX_EXR = "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ?startDate={a}&endDate={b}&response=json"
FIRST_DAY = {"twse": date(2004, 2, 11), "tpex": date(2007, 1, 2)}
SUFFIX = {"twse": ".TW", "tpex": ".TWO"}
_STOCK = re.compile(r"^[1-9]\d{3}$")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")


def _num(x: Any) -> float | None:
    if x is None:
        return None
    t = re.sub(r"<[^>]*>", "", str(x)).replace(",", "").strip()
    if t in ("", "--", "---", "----", "X", "除權息", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def roc_date(s: str) -> date | None:
    """'113年01月04日' / '113/07/01' / '1151008' -> date (ROC year + 1911)."""
    t = re.sub(r"[年月/]", "-", str(s).strip()).replace("日", "")
    m = re.fullmatch(r"(\d{2,3})-(\d{1,2})-(\d{1,2})", t)
    if not m and re.fullmatch(r"\d{7}", t):
        m = re.fullmatch(r"(\d{3})(\d{2})(\d{2})", t)
    if not m:
        return None
    return date(int(m.group(1)) + 1911, int(m.group(2)), int(m.group(3)))


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema={
        "source_id": pl.Utf8, "year": pl.Int16, "symbol": pl.Utf8, "date": pl.Date, "open": pl.Float64,
        "high": pl.Float64, "low": pl.Float64, "close": pl.Float64, "adj_close": pl.Float64, "volume": pl.Float64,
        "vwap": pl.Float64, "trades": pl.Int64, "currency": pl.Utf8, "price_basis": pl.Utf8, "is_benchmark": pl.Boolean,
        "ingested_at": pl.Datetime("us", "UTC")})


def _bar(src: str, code: str, day: date, o: Any, h: Any, lo: Any, c: Any, vol: Any, val: Any, n: Any,
         avg: Any = None, now: datetime | None = None) -> dict[str, Any] | None:
    close = _num(c)
    if not close or close <= 0:  # no trade that day ('--')
        return None
    v, amt = _num(vol), _num(val)
    vwap = _num(avg) if avg is not None else ((amt / v) if amt and v else None)
    nn = _num(n)
    return {"source_id": src, "year": day.year, "symbol": code + SUFFIX[src], "date": day, "open": _num(o),
            "high": _num(h), "low": _num(lo), "close": close, "adj_close": None, "volume": v, "vwap": vwap,
            "trades": int(nn) if nn is not None else None, "currency": "TWD", "price_basis": "raw",
            "is_benchmark": False, "ingested_at": now or datetime.now(UTC)}


def parse_twse_day(j: dict[str, Any], day: date) -> pl.DataFrame:
    """MI_INDEX JSON -> staged bars (ordinary shares only). Empty frame on holidays."""
    if not isinstance(j, dict) or j.get("stat") != "OK":
        return pl.DataFrame()
    tables = j.get("tables") or []
    if not tables and j.get("fields9"):  # legacy layout (data9/fields9)
        tables = [{"fields": j.get("fields9"), "data": j.get("data9")}]
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for t in tables:
        f = t.get("fields") or []
        if "證券代號" not in f or "收盤價" not in f:
            continue
        ix = {k: f.index(k) for k in f}
        for r in t.get("data") or []:
            code = str(r[ix["證券代號"]]).strip()
            if not _STOCK.match(code):
                continue
            b = _bar("twse", code, day, r[ix["開盤價"]], r[ix["最高價"]], r[ix["最低價"]], r[ix["收盤價"]],
                     r[ix["成交股數"]], r[ix["成交金額"]], r[ix["成交筆數"]], now=now)
            if b:
                rows.append(b)
    return _frame(rows)


def parse_tpex_day(j: dict[str, Any], day: date) -> pl.DataFrame:
    """TPEx dailyQuotes JSON -> staged bars. Verifies the echoed date (old endpoints silently return the latest day)."""
    if not isinstance(j, dict):
        return pl.DataFrame()
    echoed = str(j.get("date") or "")
    if echoed and echoed != day.strftime("%Y%m%d"):
        return pl.DataFrame()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for t in j.get("tables") or []:
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if "代號" not in f or "收盤" not in f:
            continue
        ix = {k: f.index(k) for k in f}
        val_col = next((k for k in f if k.startswith("成交金額")), None)
        vol_col = next((k for k in f if k.startswith("成交股數")), None)
        for r in t.get("data") or []:
            code = str(r[ix["代號"]]).strip()
            if not _STOCK.match(code):
                continue
            b = _bar("tpex", code, day, r[ix["開盤"]], r[ix["最高"]], r[ix["最低"]], r[ix["收盤"]],
                     r[ix[vol_col]] if vol_col else None, r[ix[val_col]] if val_col else None,
                     r[ix["成交筆數"]] if "成交筆數" in ix else None,
                     r[ix["均價"]] if "均價" in ix else None, now=now)
            if b:
                rows.append(b)
    return _frame(rows).unique(subset=["symbol", "date"], keep="first") if rows else pl.DataFrame()


def _action_rows(src: str, code: str, ex: date, before: float | None, ref: float | None, cash: float | None,
                 kind: str, extra: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    """One ex-event -> corporate_actions_staging rows. cash = 息值 (cash dividend deducted from the reference price);
    the remaining step (stock dividend / rights) becomes a stock_dividend ratio so that the total adjustment equals
    ref/before exactly (compute_adjustment_factors: (1 - cash/before) / ratio)."""
    if not before or not ref or before <= 0 or ref <= 0 or ref >= before:
        return []
    base = {"source_id": src, "symbol": code + SUFFIX[src], "ex_date": ex, "currency": "TWD", "record_date": None,
            "pay_date": None, "declared_date": None, "ingested_at": now,
            "details": json.dumps({"kind": kind, "close_before": before, "ref_price": ref, **extra}, ensure_ascii=False)}
    out: list[dict[str, Any]] = []
    cash = cash if cash and cash > 1e-9 else 0.0
    if kind == "息" or kind == "除息":
        cash = before - ref
    if cash >= before:
        return []
    if cash:
        out.append({**base, "action_type": "cash_dividend", "ratio": None, "amount": round(cash, 6), "amount_basis": "raw"})
    stock_ratio = (before - cash) / ref
    if stock_ratio > 1.0 + 1e-4:
        out.append({**base, "action_type": "stock_dividend", "ratio": round(stock_ratio, 6), "amount": None,
                    "amount_basis": None})
    return out


def parse_twse_exrights(j: dict[str, Any]) -> pl.DataFrame:
    """TWT49U: 權/息 = 息 (cash), 權 (stock/rights), 權息 (both; only the sum 權值+息值 is published, so the whole step is
    booked as stock_dividend - adj_factor stays exact, split_factor slightly overstated)."""
    if not isinstance(j, dict) or j.get("stat") != "OK":
        return pl.DataFrame()
    f = j.get("fields") or []
    if "股票代號" not in f:
        return pl.DataFrame()
    ix = {k: f.index(k) for k in f}
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for r in j.get("data") or []:
        code = str(r[ix["股票代號"]]).strip()
        ex = roc_date(r[ix["資料日期"]])
        if not _STOCK.match(code) or ex is None:
            continue
        kind = str(r[ix["權/息"]]).strip()
        rows += _action_rows("twse", code, ex, _num(r[ix["除權息前收盤價"]]), _num(r[ix["除權息參考價"]]), None, kind,
                             {"value": _num(r[ix["權值+息值"]])}, now)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def parse_tpex_exrights(j: dict[str, Any]) -> pl.DataFrame:
    """exDailyQ: separate 權值 / 息值 (cash dividend) / 每仟股無償配股 -> exact cash + stock split of the step."""
    if not isinstance(j, dict):
        return pl.DataFrame()
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for t in j.get("tables") or []:
        f = [str(x).strip() for x in (t.get("fields") or [])]
        if "代號" not in f or "除權息參考價" not in f:
            continue
        ix = {k: f.index(k) for k in f}
        for r in t.get("data") or []:
            code = str(r[ix["代號"]]).strip()
            ex = roc_date(r[ix["除權息日期"]])
            if not _STOCK.match(code) or ex is None:
                continue
            cash = _num(r[ix["息值"]]) if "息值" in ix else None
            extra = {"stock_value": _num(r[ix["權值"]]) if "權值" in ix else None,
                     "bonus_per_1000": _num(r[ix["每仟股無償配股"]]) if "每仟股無償配股" in ix else None}
            rows += _action_rows("tpex", code, ex, _num(r[ix["除權息前收盤價"]]), _num(r[ix["除權息參考價"]]), cash,
                                 str(r[ix["權/息"]]).strip(), extra, now)
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


# ------------------------------------------------------------------------------------------------ fetch


def _client(src: str, settings: Settings | None = None):  # noqa: ANN202
    from app.core.http import HttpClient  # noqa: PLC0415

    return HttpClient(src, settings=settings, timeout=90, headers={"User-Agent": _UA}, cache=False)


def fetch_day(src: str, day: date, http: Any) -> pl.DataFrame:
    if src == "twse":
        r = http.get(TWSE_DAY.format(d=day.strftime("%Y%m%d")), cache_ttl=0)
        return parse_twse_day(r.json(), day)
    r = http.get(TPEX_DAY.format(d=day.strftime("%Y/%m/%d")), cache_ttl=0)
    return parse_tpex_day(r.json(), day)


def fetch_exrights(src: str, start: date, end: date, http: Any) -> pl.DataFrame:
    if src == "twse":
        r = http.get(TWSE_EXR.format(a=start.strftime("%Y%m%d"), b=end.strftime("%Y%m%d")), cache_ttl=0)
        return parse_twse_exrights(r.json())
    r = http.get(TPEX_EXR.format(a=start.strftime("%Y/%m/%d"), b=end.strftime("%Y/%m/%d")), cache_ttl=0)
    return parse_tpex_exrights(r.json())


def sessions(start: date, end: date) -> list[date]:
    from app.normalize.prices import trading_sessions  # noqa: PLC0415

    ss = trading_sessions("XTAI", start, end)
    if ss is None:
        ss = {start + timedelta(days=i) for i in range((end - start).days + 1)}
        ss = {d for d in ss if d.weekday() < 5}
    return sorted(ss)


def run_tw(settings: Settings | None = None, start: date | None = None, end: date | None = None,
           markets: tuple[str, ...] = ("twse", "tpex"), write: bool = True) -> dict[str, Any]:
    """Incremental window (default: last 10 days) for both markets + ex-rights of the window."""
    s = settings or get_settings()
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=10)
    rep: dict[str, Any] = {"rows": 0, "actions": 0, "failed": {}}
    for src in markets:
        http = _client(src, s)
        frames = []
        for d in sessions(max(start, FIRST_DAY[src]), end):
            try:
                frames.append(fetch_day(src, d, http))
            except Exception as e:  # noqa: BLE001
                rep["failed"][f"{src}:{d}"] = f"{type(e).__name__}: {e}"[:200]
        df = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else pl.DataFrame()
        rep["rows"] += df.height
        if write and df.height:
            write_table(df, "prices_staging", settings=s)
        try:
            ca = fetch_exrights(src, start, end, http)
        except Exception as e:  # noqa: BLE001
            rep["failed"][f"{src}:exrights"] = f"{type(e).__name__}: {e}"[:200]
            ca = pl.DataFrame()
        rep["actions"] += ca.height
        if write and ca.height:
            write_table(ca, "corporate_actions_staging", settings=s)
    rep["status"] = "partial" if rep["failed"] else "success"
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    start = date.fromisoformat(p["start"]) if p.get("start") else (
        date.fromisoformat(ctx.watermark[:10]) - timedelta(days=7) if ctx.watermark else None)
    rep = run_tw(ctx.settings, start)
    return JobResult(status=rep["status"], rows_written={"prices_staging": rep["rows"],
                                                         "corporate_actions_staging": rep["actions"]},
                     entities_failed=len(rep["failed"]), watermark=datetime.now(UTC).date().isoformat(),
                     message=f"rows={rep['rows']} actions={rep['actions']} failed={len(rep['failed'])}")
