"""Job rates_auctions: sovereign auction calendars and results -> sovereign_auctions.

  US Treasury  Fiscal Data API (api.fiscaldata.treasury.gov, official US Treasury; robots allow; no key):
               auctions_query (results, 1979 ->; incremental by record_date) and upcoming_auctions (calendar).
               TreasuryDirect TA_WS is NOT used: treasurydirect.gov robots.txt disallows every agent but usasearch.
  UK DMO       XML data report D2.1E (gilt issuance history: auctions, syndications, tenders). The DMO site runs a
               bot-protection challenge (HTTP 302 to a JS check) after a few requests: at most one request per run,
               and a challenge stops the source for the run (never bypassed).
Germany (Finanzagentur issuance calendar = PDF), France (AFT: Cloudflare challenge), Italy / Spain / Portugal
(calendars and results as PDF / HTML news) are not machine-readable -> not ingested (documented in REJECTED).
params: full (bool: refetch the whole US history), sources [us_treasury, uk_dmo].
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger

from .common import client, now_utc, read_state, to_bp100, update_state

log = get_logger("app.rates.auctions")

FD = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/"
DMO_XML = "https://www.dmo.gov.uk/data/XmlDataReport"
SCHEMA = {"year": pl.Int16, "country": pl.Utf8, "agency": pl.Utf8, "auction_date": pl.Date, "security_key": pl.Utf8,
          "security_id": pl.Utf8, "security_type": pl.Utf8, "security_term": pl.Utf8, "term_months": pl.Int16,
          "method": pl.Utf8, "status": pl.Utf8, "announcement_date": pl.Date, "issue_date": pl.Date,
          "maturity_date": pl.Date, "reopening": pl.Boolean, "coupon_bp_x100": pl.Int32,
          "high_yield_bp_x100": pl.Int32, "offering_amount": pl.Float64, "tendered_amount": pl.Float64,
          "accepted_amount": pl.Float64, "bid_to_cover": pl.Float32, "indirect_share": pl.Float32,
          "direct_share": pl.Float32, "dealer_share": pl.Float32, "currency": pl.Utf8, "source": pl.Utf8,
          "knowledge_date": pl.Date}
REJECTED = {
    "DE Finanzagentur": "issuance calendar and results are published as PDF only",
    "FR AFT": "aft.gouv.fr answers a Cloudflare bot challenge",
    "IT MEF / ES Tesoro / PT IGCP": "calendars and results as PDF / HTML press releases (no machine-readable feed)",
    "US TreasuryDirect TA_WS": "robots.txt disallows all agents except usasearch (Fiscal Data API used instead)",
}


def _n(v: Any) -> float | None:
    if v in (None, "", "null", "NULL"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _d(v: Any) -> date | None:
    if v in (None, "", "null"):
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def term_months(term: str | None) -> int | None:
    """'13-Week' -> 3, '10-Year' -> 120, '9-Year 10-Month' -> 118, '6-Week' -> 1."""
    if not term:
        return None
    t = term.lower()
    months = 0.0
    found = False
    for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*-?\s*(year|month|week|day)", t):
        found = True
        x = float(n)
        months += {"year": 12 * x, "month": x, "week": x / 4.345, "day": x / 30.44}[unit]
    return int(round(months)) if found else None


def _ratio(a: Any, b: Any) -> float | None:
    x, y = _n(a), _n(b)
    return x / y if x is not None and y not in (None, 0) else None


def parse_fiscaldata_auction(r: dict[str, Any]) -> dict[str, Any] | None:
    ad = _d(r.get("auction_date"))
    if ad is None:
        return None
    st = str(r.get("security_type") or "").strip()
    tips = str(r.get("inflation_index_security") or "").lower() == "yes"
    frn = str(r.get("floating_rate") or "").lower() == "yes"
    cmb = str(r.get("cash_management_bill_cmb") or "").lower() == "yes"
    stype = "TIPS" if tips else ("FRN" if frn else ("CMB" if cmb else st))
    term = r.get("security_term") or r.get("original_security_term")
    hy = None
    for k in ("high_yield", "high_discnt_rate", "high_discnt_margin"):
        hy = _n(r.get(k))
        if hy is not None:
            break
    acc = _n(r.get("total_accepted"))
    comp = _n(r.get("comp_accepted"))
    has_results = acc is not None or hy is not None
    cusip = (r.get("cusip") or "").strip() or None
    return {"country": "US", "agency": "us_treasury", "auction_date": ad,
            "security_key": cusip or f"{stype}-{term}", "security_id": cusip, "security_type": stype,
            "security_term": term, "term_months": term_months(term), "method": r.get("auction_format") or None,
            "status": "results" if has_results else "announced", "announcement_date": _d(r.get("announcemt_date")),
            "issue_date": _d(r.get("issue_date")), "maturity_date": _d(r.get("maturity_date")),
            "reopening": (str(r.get("reopening") or "").lower() == "yes") if r.get("reopening") not in (None, "null") else None,
            "coupon_bp_x100": to_bp100(_n(r.get("int_rate"))), "high_yield_bp_x100": to_bp100(hy),
            "offering_amount": _n(r.get("offering_amt")), "tendered_amount": _n(r.get("total_tendered")),
            "accepted_amount": acc, "bid_to_cover": _n(r.get("bid_to_cover_ratio")),
            "indirect_share": _ratio(r.get("indirect_bidder_accepted"), comp),
            "direct_share": _ratio(r.get("direct_bidder_accepted"), comp),
            "dealer_share": _ratio(r.get("primary_dealer_accepted"), comp), "currency": "USD", "source": "fiscaldata",
            "knowledge_date": ad if has_results else (_d(r.get("announcemt_date")) or ad)}


def parse_fiscaldata_upcoming(r: dict[str, Any]) -> dict[str, Any] | None:
    ad = _d(r.get("auction_date"))
    if ad is None:
        return None
    term = r.get("security_term")
    st = str(r.get("security_type") or "").strip()
    cusip = (r.get("cusip") or "").strip() or None
    return {"country": "US", "agency": "us_treasury", "auction_date": ad, "security_key": cusip or f"{st}-{term}",
            "security_id": cusip, "security_type": st, "security_term": term, "term_months": term_months(term),
            "method": None, "status": "announced", "announcement_date": _d(r.get("announcemt_date")),
            "issue_date": _d(r.get("issue_date")), "maturity_date": None,
            "reopening": (str(r.get("reopening") or "").lower() == "yes") if r.get("reopening") else None,
            "coupon_bp_x100": None, "high_yield_bp_x100": None, "offering_amount": _n(r.get("offering_amt")),
            "tendered_amount": None, "accepted_amount": None, "bid_to_cover": None, "indirect_share": None,
            "direct_share": None, "dealer_share": None, "currency": "USD", "source": "fiscaldata",
            "knowledge_date": _d(r.get("announcemt_date")) or _d(r.get("record_date")) or ad}


_DMO_ROW = re.compile(r"<View_Gilt_Issuance_History\s+([^>]*?)/>")
_ATTR = re.compile(r'([A-Z_]+)="([^"]*)"')


def gilt_coupon(name: str) -> float | None:
    """'4 1/8% Treasury Gilt 2027' -> 4.125; '0 1/8% Index-linked ...' -> 0.125; '4.25% ...' -> 4.25."""
    m = re.match(r"^\s*(\d+(?:\.\d+)?)(?:\s+(\d+)/(\d+))?%", name or "")
    if not m:
        return None
    v = float(m.group(1))
    if m.group(2):
        v += int(m.group(2)) / int(m.group(3))
    return v


def parse_dmo_issuance(xml: str) -> list[dict[str, Any]]:
    out = []
    for blk in _DMO_ROW.findall(xml):
        a = dict(_ATTR.findall(blk))
        d = _d(a.get("ACTUAL_DATE"))
        isin = (a.get("ISIN_CODE") or "").strip()
        if d is None or not isin:
            continue
        name = a.get("INSTRUMENT_NAME") or ""
        il = "index-linked" in name.lower()
        out.append({"country": "GB", "agency": "uk_dmo", "auction_date": d, "security_key": isin, "security_id": isin,
                    "security_type": "Index-linked gilt" if il else "Conventional gilt", "security_term": name,
                    "term_months": None, "method": (a.get("ISSUANCE_TYPE") or "").strip() or None, "status": "results",
                    "announcement_date": None, "issue_date": None,
                    "maturity_date": None, "reopening": None,
                    "coupon_bp_x100": to_bp100(gilt_coupon(name)), "high_yield_bp_x100": to_bp100(_n(a.get("ISSUE_YIELD"))),
                    "offering_amount": None, "tendered_amount": None,
                    "accepted_amount": (_n(a.get("NOMINAL_ISSUED")) or 0) * 1e6 or None, "bid_to_cover": _n(a.get("COVER")),
                    "indirect_share": None, "direct_share": None, "dealer_share": None, "currency": "GBP",
                    "source": "dmo", "knowledge_date": d})
    return out


def fetch_us(s: Settings, full: bool) -> tuple[list[dict[str, Any]], list[str], int]:
    rows, notes, n = [], [], 0
    st = read_state(s).get("auctions") or {}
    since = None if full or not st.get("us_record_date") else (date.fromisoformat(st["us_record_date"]) - timedelta(days=21))
    c = client("fiscaldata", s)
    page = 1
    while True:
        params: dict[str, Any] = {"sort": "-auction_date", "page[size]": 5000, "page[number]": page}
        if since:
            params["filter"] = f"record_date:gte:{since.isoformat()}"
        try:
            j = c.get(FD + "auctions_query", params=params, cache_ttl=3 * 3600).json()
            n += 1
        except Exception as e:  # noqa: BLE001
            notes.append(f"fiscaldata auctions page {page}: {type(e).__name__}: {e}"[:200])
            break
        data = j.get("data") or []
        rows += [x for x in (parse_fiscaldata_auction(r) for r in data) if x]
        pages = int((j.get("meta") or {}).get("total-pages") or 1)
        if page >= pages or not data:
            break
        page += 1
    try:
        j = c.get(FD + "upcoming_auctions", params={"sort": "-record_date", "page[size]": 500}, cache_ttl=3 * 3600).json()
        n += 1
        rec = [r for r in (j.get("data") or [])]
        latest = max((r.get("record_date") or "" for r in rec), default="")
        rows += [x for x in (parse_fiscaldata_upcoming(r) for r in rec if (r.get("record_date") or "") == latest) if x
                 and x["auction_date"] >= date.today() - timedelta(days=3)]
    except Exception as e:  # noqa: BLE001
        notes.append(f"fiscaldata upcoming: {type(e).__name__}: {e}"[:200])
    return rows, notes, n


def fetch_uk(s: Settings) -> tuple[list[dict[str, Any]], list[str], int]:
    try:
        resp = client("dmo", s, browser=True).get(DMO_XML, params={"reportCode": "D2.1E"}, cache_ttl=12 * 3600)
        text = resp.text
    except Exception as e:  # noqa: BLE001
        return [], [f"dmo: {type(e).__name__}: {e}"[:200]], 1
    if "<View_Gilt_Issuance_History" not in text:
        return [], ["dmo: bot-protection challenge / unexpected page (not bypassed); retry next run"], 1
    return parse_dmo_issuance(text), [], 1


def to_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=SCHEMA)
    df = pl.DataFrame([{**r, "year": r["auction_date"].year} for r in rows], schema=SCHEMA)
    # results win over the announced row of the same key; later rows win otherwise
    df = df.sort(pl.col("status") == "results").unique(subset=["agency", "auction_date", "security_key"], keep="last")
    return df.with_columns(pl.lit(now_utc()).alias("ingested_at"))


def run_auctions(s: Settings, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    params = params or {}
    want = set(params.get("sources") or ["us_treasury", "uk_dmo"])
    rep: dict[str, Any] = {"rows": 0, "requests": 0, "notes": [], "sources": {}}
    for src, fn in (("us_treasury", lambda: fetch_us(s, bool(params.get("full")))), ("uk_dmo", lambda: fetch_uk(s))):
        if src not in want:
            continue
        rows, notes, n = fn()
        df = to_frame(rows)
        k = write_table(df, "sovereign_auctions", mode="upsert", settings=s) if df.height else 0
        rep["rows"] += k
        rep["requests"] += n
        rep["notes"] += notes
        rep["sources"][src] = {"rows": k, "requests": n, "notes": notes,
                               "last_auction": df["auction_date"].max().isoformat() if df.height else None}
        if src == "us_treasury" and df.height and not notes:
            update_state({"auctions": {"us_record_date": date.today().isoformat()}}, s)
    update_state({"auctions": {"last_run": now_utc().isoformat(timespec="seconds"), "rows": rep["rows"]}}, s)
    return rep


def run(ctx):  # noqa: ANN001
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_auctions(ctx.settings, dict(ctx.params or {}))
    failed = [k for k, v in rep["sources"].items() if not v["rows"] and v["notes"]]
    status = "success" if not failed else ("partial" if len(failed) < len(rep["sources"]) else "failed")
    return JobResult(status=status, rows_written={"sovereign_auctions": rep["rows"]}, http_requests=rep["requests"],
                     entities_ok=len(rep["sources"]) - len(failed), entities_failed=len(failed),
                     message="; ".join(rep["notes"])[:500] or None)


_ = datetime
