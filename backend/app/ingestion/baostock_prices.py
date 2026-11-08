"""BaoStock (free, no key) A-share daily k-line -> prices_staging. Job `prices_cn_baostock`.

Research §3.2: `query_history_k_data_plus(code, fields, start, end, frequency="d", adjustflag="3")` (3 = unadjusted),
data from 1990, anonymous login, single-connection client -> strictly sequential (~0.1-0.3 s per call), no
whole-market endpoint. Only CN listings of the universe (yf .SS/.SZ) are fetched; staged symbols use the Yahoo
form (600519.SS) so they resolve via listings.yf_ticker and reconcile against yfinance. The CSI 300 benchmark
(000300.SS -> sh.000300) is always included because Yahoo only serves its latest bar.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.ratelimit import get_limiter
from app.core.storage import write_table
from app.normalize.prices import load_universe

log = get_logger(__name__)
_LOCK = threading.Lock()  # baostock keeps one global socket
FIELDS = "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus"
BENCHMARK_SYMBOLS = ("000300.SS",)  # sh.000300 = CSI 300 index


def bs_to_yf(code: str) -> str | None:
    """'sh.600519' -> '600519.SS'; 'sz.000858' -> '000858.SZ'; anything else (bj.) -> None."""
    if code.startswith("sh."):
        return f"{code[3:]}.SS"
    if code.startswith("sz."):
        return f"{code[3:]}.SZ"
    return None


def fetch_stock_list(bs_mod: Any, listed_only: bool = True) -> pl.DataFrame:
    """BaoStock query_stock_basic() + query_stock_industry() (CSRC industry) -> A-share list
    [code, yf_ticker, name, ipo_date, out_date, status, csrc_industry]. type 1 = stock (indices/ETFs/bonds dropped)."""
    rs = bs_mod.query_stock_basic()
    rows = []
    while rs.next():
        rows.append(rs.get_row_data())
    basic = pl.DataFrame(rows, schema=rs.fields, orient="row") if rows else pl.DataFrame(
        schema={f: pl.Utf8 for f in ["code", "code_name", "ipoDate", "outDate", "type", "status"]})
    basic = basic.filter(pl.col("type") == "1")
    if listed_only:
        basic = basic.filter(pl.col("status") == "1")
    rs = bs_mod.query_stock_industry()
    ind = []
    while rs.next():
        ind.append(rs.get_row_data())
    ind_df = (pl.DataFrame(ind, schema=rs.fields, orient="row").select("code", pl.col("industry").alias("csrc_industry"))
              if ind else pl.DataFrame(schema={"code": pl.Utf8, "csrc_industry": pl.Utf8}))
    out = basic.join(ind_df, on="code", how="left").select(
        "code", pl.col("code").map_elements(bs_to_yf, return_dtype=pl.Utf8).alias("yf_ticker"),
        pl.col("code_name").alias("name"), pl.col("ipoDate").alias("ipo_date"),
        pl.col("outDate").replace("", None).alias("out_date"), "status",
        pl.col("csrc_industry").replace("", None))
    return out.filter(pl.col("yf_ticker").is_not_null())


#: A-share bonus/transfer issues are quoted per 10 shares ("10送3转5"), i.e. stock ratios on a 0.1 grid
_CN_STOCK_GRID = [k / 10 for k in range(1, 31)]


def derive_cn_actions(bars: pl.DataFrame, min_stock_r: float = 1.08, max_cash_yield: float = 0.12) -> pl.DataFrame:
    """Corporate actions implied by BaoStock's exchange reference price (`preclose`) on ex-dates.

    bars: [symbol, date, close, preclose] RAW traded days (sorted per symbol). On an ex-date the exchange sets
    preclose_t = (close_{t-1} - D + rights) / (1 + S) != close_{t-1}; r = close_{t-1} / preclose_t is exactly the
    total back-adjustment step BaoStock uses for its factors, so adj_factor is exact. The split (S) vs cash (D)
    allocation is heuristic: r < min_stock_r -> pure cash D = close_{t-1} * (1 - 1/r); otherwise the largest S on the
    0.1 grid whose implied cash yield 1 - (1+S)/r lies in [-0.3%, max_cash_yield] (stock_dividend ratio 1+S, plus the
    residual cash dividend); no fit -> stock_dividend ratio r. Returns corporate_actions_staging-shaped rows
    (source_id baostock, details.derived = 'preclose')."""
    if bars.is_empty() or "preclose" not in bars.columns:
        return pl.DataFrame()
    b = bars.sort("symbol", "date").with_columns(pl.col("close").shift(1).over("symbol").alias("__pc"))
    ev = b.filter(pl.col("__pc").is_not_null() & pl.col("preclose").is_not_null() & (pl.col("preclose") > 0)
                  & ((pl.col("__pc") - pl.col("preclose")).abs() >= 0.005)
                  & ((pl.col("__pc") / pl.col("preclose") - 1.0).abs() > 1e-4))
    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for r in ev.select("symbol", "date", "__pc", "preclose").iter_rows(named=True):
        ratio = r["__pc"] / r["preclose"]
        if ratio <= 1.0:  # reference price above the last close: data quirk, not a distribution
            continue
        det = {"derived": "preclose", "prev_close": r["__pc"], "preclose": r["preclose"], "r": round(ratio, 6)}
        base = {"source_id": "baostock", "symbol": r["symbol"], "ex_date": r["date"], "currency": "CNY",
                "record_date": None, "pay_date": None, "declared_date": None, "ingested_at": now}
        stock = None
        if ratio >= min_stock_r:
            fits = [s for s in _CN_STOCK_GRID if -0.003 <= 1.0 - (1.0 + s) / ratio <= max_cash_yield]
            stock = max(fits) if fits else ratio - 1.0
        if stock:
            out.append({**base, "action_type": "stock_dividend", "ratio": round(1.0 + stock, 6), "amount": None,
                        "amount_basis": None, "details": json.dumps({**det, "split_part": round(stock, 4)})})
            cash = r["__pc"] * (1.0 - (1.0 + stock) / ratio) if fits else 0.0
        else:
            cash = r["__pc"] * (1.0 - 1.0 / ratio)
        if cash > 1e-6:
            out.append({**base, "action_type": "cash_dividend", "ratio": None, "amount": round(cash, 6),
                        "amount_basis": "raw", "details": json.dumps(det)})
    return pl.DataFrame(out, infer_schema_length=None) if out else pl.DataFrame()


def yf_to_bs(sym: str) -> str | None:
    if sym.endswith(".SS"):
        return f"sh.{sym[:-3]}"
    if sym.endswith(".SZ"):
        return f"sz.{sym[:-3]}"
    return None


def bs_rows(sym: str, rows: list[list[str]], fields: list[str], keep_preclose: bool = False) -> pl.DataFrame:
    """BaoStock k-data rows -> prices_staging rows (+ `preclose`, the exchange reference price, when keep_preclose:
    input of derive_cn_actions; drop it before writing)."""
    now = datetime.now(UTC)
    out = []
    for r in rows:
        d = dict(zip(fields, r, strict=False))
        try:
            close = float(d["close"]) if d.get("close") else None
        except ValueError:
            close = None
        if not close or d.get("tradestatus") == "0":  # suspended days carry the previous close
            continue
        day = date.fromisoformat(d["date"])
        f = lambda k: float(d[k]) if d.get(k) not in (None, "") else None  # noqa: E731
        out.append({"source_id": "baostock", "year": day.year, "symbol": sym, "date": day, "open": f("open"),
                    "high": f("high"), "low": f("low"), "close": close, "adj_close": None, "volume": f("volume"),
                    "vwap": (f("amount") / f("volume")) if f("amount") and f("volume") else None, "trades": None,
                    "currency": "CNY", "price_basis": "raw", "is_benchmark": sym in BENCHMARK_SYMBOLS,
                    "ingested_at": now, **({"preclose": f("preclose")} if keep_preclose else {})})
    return pl.DataFrame(out) if out else pl.DataFrame()


def run_baostock(settings: Settings | None = None, symbols: list[str] | None = None, start: date | None = None,
                 end: date | None = None, write: bool = True) -> dict[str, Any]:
    import baostock as bs  # noqa: PLC0415

    s = settings or get_settings()
    if symbols is None:
        uni = load_universe(s, include_benchmarks=False)
        symbols = [x for x in uni.get_column("yf_ticker").drop_nulls().to_list() if yf_to_bs(x)]
        # CSI 300 benchmark: Yahoo's 000300.SS only serves the latest bar (2026-10), BaoStock has it from 2005
        symbols = list(dict.fromkeys([*symbols, *BENCHMARK_SYMBOLS]))
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=10)
    lim = get_limiter("baostock")
    frames, failed = [], {}
    t0 = time.perf_counter()
    with _LOCK:
        lg = bs.login()
        if lg.error_code != "0":
            return {"status": "failed", "error": f"login {lg.error_code} {lg.error_msg}"}
        try:
            for sym in symbols:
                lim.acquire()
                rs = bs.query_history_k_data_plus(yf_to_bs(sym), FIELDS, start_date=start.isoformat(),
                                                  end_date=end.isoformat(), frequency="d", adjustflag="3")
                if rs.error_code != "0":
                    failed[sym] = f"{rs.error_code} {rs.error_msg}"
                    continue
                data = []
                while rs.next():
                    data.append(rs.get_row_data())
                frames.append(bs_rows(sym, data, rs.fields, keep_preclose=True))
        finally:
            bs.logout()
    df = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else pl.DataFrame()
    acts = pl.DataFrame()
    if df.height:
        acts = derive_cn_actions(df.filter(~pl.col("is_benchmark")).select("symbol", "date", "close", "preclose"))
    df = df.drop("preclose", strict=False)
    rep: dict[str, Any] = {"status": "partial" if failed else "success", "symbols": len(symbols), "rows": df.height,
                           "failed": failed, "seconds": round(time.perf_counter() - t0, 1)}
    if write and df.height:
        rep["rows_written"] = write_table(df, "prices_staging", settings=s)
    if write and acts.height:
        rep["actions_written"] = write_table(acts, "corporate_actions_staging", settings=s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    start = date.fromisoformat(p["start"]) if p.get("start") else (
        date.fromisoformat(ctx.watermark[:10]) - timedelta(days=7) if ctx.watermark else None)
    rep = run_baostock(ctx.settings, p.get("symbols"), start)
    if rep["status"] == "failed":
        return JobResult(status="failed", message=rep.get("error"))
    return JobResult(status=rep["status"], rows_written={"prices_staging": rep.get("rows_written", 0)},
                     entities_ok=rep["symbols"] - len(rep["failed"]), entities_failed=len(rep["failed"]),
                     watermark=datetime.now(UTC).date().isoformat(), message=f"rows={rep['rows']} {rep['seconds']}s")
