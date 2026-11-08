"""China A-share fundamentals from BaoStock (primary CN source; PIT pubDate) -> filings_index + fundamentals_raw.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §3.2 (live-tested 2026-10-10, no key; baostock 0.9.4 on
Python 3.13 / pandas 2.x).
  bs.login() (anonymous) then per stock/year/quarter:
  query_profit_data     pubDate, statDate, roeAvg, npMargin, gpMargin, netProfit, epsTTM, MBRevenue, totalShare, liqaShare
  query_balance_data    currentRatio, quickRatio, cashRatio, YOYLiability, liabilityToAsset, assetToEquity
  query_cash_flow_data  CAToAsset, NCAToAsset, tangibleAssetToAsset, ebitToInterest, CFOToOR, CFOToNP, CFOToGr
  query_growth_data     YOYEquity, YOYAsset, YOYNI, YOYEPSBasic, YOYPNI
  query_operation_data  NRTurnRatio, NRTurnDays, INVTurnRatio, INVTurnDays, CATurnRatio, AssetTurnRatio
  query_dupont_data     dupontROE, dupontAssetStoEquity, dupontAssetTurn, dupontPnitoni, dupontNitogr, ...
  query_stock_industry  CSRC industry (discover)
The client is a single TCP connection without timeouts, so every call runs in a killable child process
(HardTimeoutRunner, 45 s). Values are YTD cumulative for flow items (statDate = period end; Q4 = FY).
PIT: knowledge_date = pubDate (original publication). Accounting standard CAS (China Accounting Standards).
Concepts: 'baostock:<field>' (taxonomy 'other'); absolute amounts in CNY, share counts in shares, ratios 'pure'.
source_entity_id = 'SSE:600519' / 'SZSE:000001' / 'BSE:8xxxxx' (identity normalize_id('cn_code') spelling).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (CallTimeout, HardTimeoutRunner, fact_row, filing_row, frame, parse_num,
                                          run_adapter_job, safe_call, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

QUERIES = ("query_profit_data", "query_balance_data", "query_cash_flow_data", "query_growth_data",
           "query_operation_data", "query_dupont_data")
MONEY = {"netProfit": "CNY", "MBRevenue": "CNY", "totalShare": "shares", "liqaShare": "shares", "epsTTM": "CNY/shares"}
INSTANT = {"totalShare", "liqaShare", "currentRatio", "quickRatio", "cashRatio", "liabilityToAsset", "assetToEquity",
           "CAToAsset", "NCAToAsset", "tangibleAssetToAsset"}

# ------------------------------------------------------------------------------------------- child-process side
_BS_LOGGED_IN = False


def bs_call(fn: str, **kwargs: Any) -> tuple[list[str], list[list[str]]]:
    """Runs inside the HardTimeoutRunner child. Returns (fields, rows) or raises on BaoStock error."""
    global _BS_LOGGED_IN
    import contextlib
    import io

    import baostock as bs

    with contextlib.redirect_stdout(io.StringIO()):
        for attempt in range(2):
            if not _BS_LOGGED_IN:
                lg = bs.login()
                if lg.error_code != "0":
                    raise RuntimeError(f"baostock login {lg.error_code} {lg.error_msg}")
                _BS_LOGGED_IN = True
            rs = getattr(bs, fn)(**kwargs)
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rs.error_code == "10001001" and attempt == 0:  # session expired server-side -> re-login once
                _BS_LOGGED_IN = False
                continue
            break
    if rs.error_code != "0":
        _BS_LOGGED_IN = False if rs.error_code.startswith("1000") else _BS_LOGGED_IN
        raise RuntimeError(f"baostock {fn} {rs.error_code} {rs.error_msg}")
    return list(rs.fields), rows


# ------------------------------------------------------------------------------------------- helpers


def to_bs_code(code: str) -> str:
    c = code.strip().upper().replace("SSE:", "SH.").replace("SZSE:", "SZ.").replace("BSE:", "BJ.")
    if "." in c:
        a, b = c.split(".", 1)
        if b in ("SS", "SH"):
            return f"sh.{a}"
        if b == "SZ":
            return f"sz.{a}"
        if b == "BJ":
            return f"bj.{a}"
        if a in ("SH", "SZ", "BJ"):
            return f"{a.lower()}.{b}"
    if c.isdigit() and len(c) == 6:
        return f"{'sh' if c[0] in '69' else ('bj' if c[0] in '48' else 'sz')}.{c}"
    return c.lower()


def cn_entity(bs_code: str) -> str:
    ex, num = bs_code.split(".", 1)
    return f"{ {'sh': 'SSE', 'sz': 'SZSE', 'bj': 'BSE'}[ex] }:{num}".replace(" ", "")


def bs_rows_to_facts(fields: list[str], rows: list[list[str]], *, source_id: str = "baostock") -> tuple[list, list]:
    facts, fil = [], {}
    for r in rows:
        d = dict(zip(fields, r))
        pub, stat = to_date(d.get("pubDate")), to_date(d.get("statDate"))
        if pub is None or stat is None:
            continue
        ent = cn_entity(d["code"])
        native = f"{ent}:{stat.isoformat()}"
        fid = f"{source_id}:{native}"
        q = (stat.month - 1) // 3 + 1
        fp = {1: "Q1", 2: "H1", 3: "9M", 4: "FY"}[q]
        if fid not in fil:
            fil[fid] = filing_row(source_id=source_id, native_key=native, source_entity_id=ent, form_type="CN_PERIODIC",
                                  form_class={4: "annual", 2: "semiannual"}.get(q, "quarterly"), knowledge_date=pub,
                                  period_end=stat, fiscal_year=stat.year, fiscal_period={1: "Q1", 2: "H1", 3: "Q3", 4: "FY"}[q],
                                  language="zh", items=["accounting_standard=CAS"])
        for k, v in d.items():
            if k in ("code", "pubDate", "statDate"):
                continue
            val = parse_num(v)
            if val is None:
                continue
            inst = k in INSTANT
            facts.append(fact_row(source_id=source_id, source_entity_id=ent, filing_id=fid, form_type="CN_PERIODIC",
                                  taxonomy="other", concept=f"baostock:{k}", label=k, period_start=None if inst else date(stat.year, 1, 1),
                                  period_end=stat, value=val, unit=MONEY.get(k, "pure"), knowledge_date=pub,
                                  fiscal_year=stat.year, fiscal_period=None if inst else fp, consolidated=True))
    return list(fil.values()), facts


@register_adapter
class BaostockAdapter(SourceAdapter):
    source_id = "baostock"
    regions = ("CN",)
    produces = ("filings_index", "fundamentals_raw")
    rate_limit = RateLimit(3.0, 3)

    def __init__(self, *args: Any, years: str | None = None, timeout_s: float = 45.0, **kw: Any):
        super().__init__(*args, **kw)
        y = datetime.now(UTC).year
        self.years = [int(v) for v in str(years).split(",")] if years else [y - 1, y]
        self.timeout_s = float(timeout_s)

    def discover(self) -> pl.DataFrame:
        with HardTimeoutRunner(120) as runner:
            fields, rows = runner.call("app.ingestion.baostock_cn:bs_call", fn="query_stock_industry")
        out = []
        for r in rows:
            d = dict(zip(fields, r))
            out.append({"source_entity_id": cn_entity(d["code"]), "cn_code": cn_entity(d["code"]), "name": d["code_name"],
                        "name_local": d["code_name"], "country": "CN", "ticker": d["code"].split(".")[1],
                        "mic": "XSHG" if d["code"].startswith("sh") else ("XSHE" if d["code"].startswith("sz") else "BJSE"),
                        "yf_ticker": d["code"].split(".")[1] + (".SS" if d["code"].startswith("sh") else ".SZ"),
                        "industry_code": d.get("industry"), "industry_scheme": "csrc", "is_active": True})
        return self.discover_frame(out, self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        codes = [to_bs_code(e) for e in (entities or seed_universe(self.settings, countries=["CN"]).filter(
            pl.col("mic").is_in(["XSHG", "XSHE"])).get_column("ticker").to_list())]
        since_d = to_date(since)
        years = sorted({y for y in self.years if since_d is None or y >= since_d.year - 1})
        os.environ.setdefault("TQDM_DISABLE", "1")
        landed = []
        with HardTimeoutRunner(self.timeout_s) as runner:
            consecutive_timeouts = 0
            for code in codes:
                if consecutive_timeouts >= 3:
                    res.entities_failed[code] = "skipped: baostock unreachable (3 consecutive timeouts)"
                    continue

                def one(code=code) -> None:
                    nonlocal consecutive_timeouts
                    payload: dict[str, Any] = {"code": code, "queries": {}}
                    for y in years:
                        for q in (1, 2, 3, 4):
                            if date(y, q * 3, 1) > datetime.now(UTC).date():
                                continue
                            for fn in QUERIES:
                                self.http.limiter.acquire()
                                try:
                                    fields, rows = runner.call("app.ingestion.baostock_cn:bs_call", fn=fn, code=code,
                                                               year=y, quarter=q)
                                    consecutive_timeouts = 0
                                except CallTimeout:
                                    consecutive_timeouts += 1
                                    raise
                                if rows:
                                    payload["queries"].setdefault(fn, {"fields": fields, "rows": []})["rows"] += rows
                    landed.append(self.save_raw("fundamentals", code.replace(".", "_"), payload))
                safe_call(one, res, code, self.log)
        res.raw_paths = landed
        res.meta["since"] = since_d.isoformat() if since_d else None
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        fil, facts = [], []
        since = to_date(fetched.meta.get("since"))
        for p in fetched.raw_paths:
            payload = self.load_raw_json(p)
            for fn, blk in payload["queries"].items():
                f, x = bs_rows_to_facts(blk["fields"], blk["rows"], source_id=self.source_id)
                fil += f
                facts += x
        if since:
            fil = [r for r in fil if r["knowledge_date"] >= since]
            facts = [r for r in facts if r["knowledge_date"] >= since]
        return {"filings_index": frame(fil, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def run(ctx: Any) -> Any:
    # sporadic per-call hard timeouts (a few symbols per night) are retried next run: warnings, not partial
    return run_adapter_job(BaostockAdapter, ctx, warn_if=lambda err: err.startswith("CallTimeout"))
