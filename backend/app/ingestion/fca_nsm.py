"""UK FCA National Storage Mechanism (NSM): director/PDMR dealings (RNS 'DSH') -> insider_transactions, and tagged
ESEF/UKSEF annual financial reports ('ACS' with xBRL-JSON) -> filings_index + fundamentals_raw.

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §6.2 (undocumented JSON API, verified 2026-10-10).
  POST https://api.data.fca.org.uk/search?index=nsm-search
       {"from":0,"size":N,"sort":"submitted_date","sortorder":"desc",
        "criteriaObj":{"criteria":[{"name":"latest_flag","value":"Y"},{"name":"type_code","value":["DSH"]},
                                   {"name":"lei","value":[<LEI>...]}],
                       "dateCriteria":[{"name":"submitted_date","value":{"from":ISO,"to":ISO}}]}}
  GET  https://data.fca.org.uk/artefacts/{download_link | json_link}
PIT: knowledge_date/ts = publication_date (to the minute). Keys: none. source_entity_id = issuer LEI.
The API is undocumented: every response is schema-checked and failures are reported (fail soft).
MAR art.19 notices are free text in a standard table; parse_pdmr_notice() extracts name, role, ISIN, nature,
price/volume pairs, date and venue. Unparseable notices still land in filings_index.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from bs4 import BeautifulSoup

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, insider_row, load_state, parse_text_date,
                                          run_adapter_job, safe_call, save_state, to_date)
from app.ingestion._xbrl_json import numeric_facts, parse_xbrl_json
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

SEARCH_URL = "https://api.data.fca.org.uk/search?index=nsm-search"
ARTEFACTS = "https://data.fca.org.uk/artefacts/"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
_REQUIRED_SRC_KEYS = {"publication_date", "type_code", "company"}

# ----------------------------------------------------------------------------------------------- PDMR parsing

_CCY = [("£", "GBP"), ("€", "EUR"), ("$", "USD"), ("GBP", "GBP"), ("EUR", "EUR"), ("USD", "USD"), ("CHF", "CHF"),
        ("SEK", "SEK"), ("DKK", "DKK"), ("NOK", "NOK"), ("ZAR", "ZAR"), ("AUD", "AUD"), ("CAD", "CAD")]
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"


def parse_price(tok: str) -> tuple[float | None, str | None]:
    t = re.sub(r"^\s*\(?[ivxlc]+\)\s*", "", tok.strip(), flags=re.I)
    if re.match(r"^(nil|n/a|zero|nil consideration|0\.00?)\b", t, re.I):
        return 0.0, None
    ccy = None
    for sym, code in _CCY:
        if sym in t[:12]:
            ccy = code
            break
    m = re.search(_NUM, t)
    if not m:
        return None, ccy
    v = float(m.group(0).replace(",", ""))
    after = t[m.end(): m.end() + 6].lower()
    if re.match(r"\s*(p\b|pence|gbx)", after) or "GBX" in t or "GBp" in t:
        return v / 100.0, "GBP"
    return v, ccy


def parse_volume(tok: str) -> float | None:
    t = re.sub(r"^\s*\(?[ivxlc]+\)\s*", "", tok.strip(), flags=re.I)
    if any(sym in t for sym, _ in _CCY[:3]):
        return None
    m = re.search(_NUM, t)
    return float(m.group(0).replace(",", "")) if m else None


def classify_nature(nature: str, place: str | None = None) -> tuple[str, bool]:
    n = nature.lower()
    plan = re.search(r"dividend reinvest|\bdrip\b|share incentive plan|\bsip\b|partnership share|matching share|"
                     r"save as you earn|\bsaye\b|scrip|sharesave|employee share purchase|\besp[p]?\b", n)
    off_venue = bool(place and re.search(r"outside a trading venue|off[- ]market|n/a", place.lower()))
    if re.search(r"to (cover|satisfy|meet) (the )?(tax|income tax|liabilit)|tax (withholding|liabilit)|sell[- ]to[- ]cover"
                 r"|withheld", n):
        return "tax_withholding", False
    if re.search(r"exercise", n) and not re.search(r"\bsale\b|\bsold\b", n):
        return "option_exercise", False
    if re.search(r"\bgrant|\baward|\bvest|release of|conditional|allotment|\ballot", n) and not re.search(
            r"\bpurchase\b|\bsale\b|\bsold\b|\bbought\b", n):
        return "grant", False
    if re.search(r"\bgift", n):
        return "gift", False
    if re.search(r"\bpurchase|\bacqui|\bbought|\bbuy\b", n):
        return "buy", not plan and not off_venue
    if re.search(r"\bsale\b|\bsold\b|\bsell|\bdispos", n):
        return "sell", not off_venue
    if re.search(r"transfer", n):
        return "other", False
    return "other", False


def _lines(html: bytes | str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    out = []
    for ln in soup.get_text("\n").split("\n"):
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return out


_LABEL = re.compile(r"^(?:[a-g]\)|\d\.?|name|lei|position/status|reason for the notification|initial notification ?/? ?"
                    r"amendment|price\(s\)|volume\(s\)|— ?aggregated volume|— ?price|aggregated volume|price|volume|"
                    r"currency|total|first name\(s\)|last name\(s\)|full name of the entity|legal entity identifier"
                    r"(?: code)?|identification code|price\(s\) and volume\(s\))$", re.I)


def _find(lines: list[str], pat: str, start: int = 0, end: int | None = None) -> int:
    rx = re.compile(pat, re.I)
    for i in range(max(start, 0), end if end is not None else len(lines)):
        if rx.search(lines[i]):
            return i
    return -1


def _value_after(lines: list[str], idx: int, end: int | None = None) -> str | None:
    if idx < 0:
        return None
    for j in range(idx + 1, min(end or len(lines), idx + 6)):
        if not _LABEL.match(lines[j]):
            return lines[j]
    return None


def _split_items(text: str) -> list[str]:
    parts = re.split(r"\(\s*(?:i|ii|iii|iv|v|vi|vii|viii|ix|x)\s*\)", text)
    return [p.strip(" ;,.") for p in parts if p.strip(" ;,.")]


def _first_idx(*idx: int) -> int:
    c = [i for i in idx if i >= 0]
    return min(c) if c else -1


def _pairs_labelled(toks: list[str]) -> list[tuple[float | None, str | None, float | None]]:
    """'Currency / GBP / Price / 27.745 / Volume / 165,975 / Total / ...' layout."""
    ccy = None
    prices: list[tuple[float | None, str | None]] = []
    vols: list[float | None] = []
    i = 0
    while i < len(toks) - 1:
        lab, val = toks[i].lower().rstrip(":"), toks[i + 1]
        if lab == "currency":
            ccy = val.strip().upper()[:3]
            i += 2
            continue
        if lab in ("price", "price(s)", "price per share"):
            prices.append(parse_price(val))
            i += 2
            continue
        if lab in ("volume", "volume(s)", "number of shares"):
            vols.append(parse_volume(val))
            i += 2
            continue
        i += 1
    out = []
    for k, v in enumerate(vols):
        p, c = prices[k] if k < len(prices) else (None, None)
        out.append((p, c or ccy, v))
    return out


def _pairs_table(toks: list[str]) -> list[tuple[float | None, str | None, float | None]]:
    """'Price(s) / Volume(s)' header followed by alternating price, volume cells (or 'price volume' on one line)."""
    toks = [t for t in toks if not (_LABEL.match(t) and not re.fullmatch(r"\d+", t.strip()))]
    pairs: list[tuple[float | None, str | None, float | None]] = []
    i = 0
    while i < len(toks):
        t = toks[i]
        p, ccy = parse_price(t)
        if i + 1 < len(toks) and p is not None and parse_volume(toks[i + 1]) is not None:
            pairs.append((p, ccy, parse_volume(toks[i + 1])))
            i += 2
            continue
        m2 = re.match(rf"^(.*?(?:{_NUM}).*?)\s+((?:{_NUM}))\s*$", t)
        if m2 and p is not None:
            pairs.append((p, ccy, parse_volume(m2.group(2))))
        i += 1
    return pairs


def _summary_table_txns(lines: list[str], s: int, e: int) -> list[dict[str, Any]]:
    """'Transaction(s) summary table' layout (RNS Reach/EQS): header row of 5 labels + 5 values, 'Nature of
    Transaction:' text, then 'Price/Volume/Total' rows until 'Aggregated'."""
    out: list[dict[str, Any]] = []
    i = s
    while True:
        i = _find(lines, r"^date of transaction$", i, e)
        if i < 0:
            break
        vals = lines[i + 5:i + 10]
        if len(vals) < 5:
            break
        tdate, instrument, isin, place, ccy = vals
        n_i = _find(lines, r"^nature of transaction", i, e)
        p_i = _find(lines, r"^price$", n_i if n_i >= 0 else i, e)
        a_i = _find(lines, r"^aggregated", p_i if p_i >= 0 else i, e)
        nature = " ".join(lines[n_i + 1:p_i]) if 0 <= n_i < p_i else ""
        rows = lines[p_i + 3:a_i] if 0 <= p_i < a_i else []
        for k in range(0, len(rows) - 1, 3):
            pr, pc = parse_price(rows[k])
            out.append({"date": parse_text_date(tdate), "instrument": instrument, "isin": isin if re.fullmatch(
                r"[A-Z]{2}[A-Z0-9]{9}\d", isin.strip()) else None, "place": place, "nature": nature, "price": pr,
                "currency": pc or (ccy.strip()[:3].upper() if re.match(r"^[A-Z]{3}", ccy.strip()) else None),
                "volume": parse_volume(rows[k + 1])})
        i = (a_i if a_i > i else i) + 1
    return out


def parse_pdmr_notice(html: bytes | str) -> list[dict[str, Any]]:
    """Parse a MAR art.19 notification (RNS/NSM HTML). Returns one dict per (person, price/volume line).
    Handles the two common layouts: the ESMA template table ('a) Name', 'c) Price(s) and volume(s)' with alternating
    cells) and the labelled-row layout ('First Name(s)', 'Currency', 'Price', 'Volume'). Free-text notices -> []."""
    lines = _lines(html)
    starts = [i for i, ln in enumerate(lines) if re.search(r"details of the person discharging managerial", ln, re.I)]
    if not starts:
        return []
    starts.append(len(lines))
    out: list[dict[str, Any]] = []
    for b in range(len(starts) - 1):
        s, e = starts[b], starts[b + 1]
        iss_i = _find(lines, r"details of the issuer", s, e)
        fn_i = _find(lines, r"^first name", s, iss_i if iss_i > 0 else e)
        if fn_i >= 0:
            parts = (_value_after(lines, fn_i, e), _value_after(lines, _find(lines, r"^last name", s, e), e))
            name = " ".join(x for x in parts if x)
        else:
            name = _value_after(lines, _find(lines, r"^(full )?name(\(s\))?$|^name of", s, iss_i if iss_i > 0 else e), e)
        pos = _value_after(lines, _find(lines, r"^position/status", s, e), e)
        amend_v = _value_after(lines, _find(lines, r"^initial notification", s, e), e) or ""
        issuer = _value_after(lines, _find(lines, r"^name$|^full name of the entity", iss_i, e), e) if iss_i >= 0 else None
        lei_i = _find(lines, r"^lei\b|^legal entity identifier", s, e)
        lei_m = re.search(r"\b[A-Z0-9]{18}[0-9]{2}\b", " ".join(lines[lei_i:lei_i + 3])) if lei_i >= 0 else None
        desc_i = _find(lines, r"description of the financial", s, e)
        nat_i = _find(lines, r"^nature of the transaction", s, e)
        pv_i = _find(lines, r"^price\(s\) and volume\(s\)|^currency$|^price$|^price\(s\)$", nat_i if nat_i >= 0 else s, e)
        agg_i = _find(lines, r"^aggregated information", s, e)
        date_i = _find(lines, r"^date of (the )?transaction", s, e)
        place_i = _find(lines, r"^place of (the )?transaction", s, e)
        isin_src = " ".join(lines[desc_i:nat_i]) if desc_i >= 0 and nat_i > desc_i else ""
        isin_m = re.search(r"\b[A-Z]{2}[A-Z0-9]{9}[0-9]\b", isin_src)
        instrument = " ".join(lines[desc_i + 1:nat_i]) if desc_i >= 0 and nat_i > desc_i else ""
        instrument = re.sub(r"^.*?identification code\s*", "", instrument, flags=re.I)
        nature = " ".join(lines[nat_i + 1:pv_i]) if nat_i >= 0 and pv_i > nat_i else ""
        place = " ".join(lines[place_i + 1:place_i + 2]) if place_i >= 0 else None
        date_end = place_i if place_i > date_i else date_i + 3
        tdate = parse_text_date(" ".join(lines[date_i + 1:date_end])) if date_i >= 0 else None
        stop = _first_idx(agg_i if agg_i > pv_i else -1, date_i if date_i > pv_i else -1)
        toks = lines[pv_i:stop if stop > pv_i else min(e, pv_i + 20)] if pv_i >= 0 else []
        pairs = _pairs_labelled(toks) if any(t.lower() in ("currency", "price", "volume") for t in toks) else []
        if not pairs:
            pairs = _pairs_table(toks[1:] if toks else [])
        if not pairs and agg_i >= 0:
            agg = lines[agg_i + 1:date_i if date_i > agg_i else agg_i + 8]
            pairs = _pairs_labelled(agg)
            if not pairs:
                agg = [t for t in agg if not _LABEL.match(t)]
                v = next((parse_volume(t) for t in agg if parse_volume(t) is not None), None)
                p = next((parse_price(t) for t in agg if parse_price(t)[1] is not None), (None, None))
                if v is not None:
                    pairs.append((p[0], p[1], v))
        if _find(lines, r"transaction\(s\) summary table", s, e) >= 0:
            for t in _summary_table_txns(lines, s, e):
                if t["volume"] is None:
                    continue
                txn_type, open_mkt = classify_nature(t["nature"], t["place"])
                out.append({"name": (name or "").strip(), "position": pos, "is_amendment": "amend" in amend_v.lower(),
                            "issuer": issuer, "lei": lei_m.group(0) if lei_m else None, "isin": t["isin"],
                            "instrument": t["instrument"], "nature": t["nature"], "price": t["price"],
                            "currency": t["currency"], "volume": t["volume"], "date": t["date"], "place": t["place"],
                            "txn_type": txn_type, "is_open_market": open_mkt,
                            "is_derivative": bool(re.search(r"option|warrant|derivative|cfd|swap|right", t["instrument"], re.I))})
            continue
        items = _split_items(nature)
        if len(items) != len(pairs):
            alt = [x.strip(" ;,.") for x in re.split(r";\s*(?:and\s+)?", nature) if x.strip(" ;,.")]
            items = alt if len(alt) == len(pairs) else items
        for k, (price, ccy, vol) in enumerate(pairs):
            if vol is None:
                continue
            nat_k = items[k] if len(items) == len(pairs) else nature
            txn_type, open_mkt = classify_nature(nat_k, place)
            out.append({"name": (name or "").strip(), "position": pos, "is_amendment": "amend" in amend_v.lower(),
                        "issuer": issuer, "lei": lei_m.group(0) if lei_m else None,
                        "isin": isin_m.group(0) if isin_m else None, "instrument": instrument, "nature": nat_k,
                        "price": price, "currency": ccy, "volume": vol, "date": tdate, "place": place,
                        "txn_type": txn_type, "is_open_market": open_mkt,
                        "is_derivative": bool(re.search(r"option|warrant|derivative|cfd|swap|right", instrument, re.I)
                                              or re.search(r"grant of (nil[- ]cost )?options", nat_k, re.I))})
    return out


# ----------------------------------------------------------------------------------------------- adapter


@register_adapter
class FcaNsmAdapter(SourceAdapter):
    source_id = "fca_nsm"
    regions = ("UK",)
    produces = ("filings_index", "fundamentals_raw", "insider_transactions")
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, max_docs: int = 300, include_esef: bool = True, include_dsh: bool = True, **kw: Any):
        super().__init__(*args, **kw)
        self.http.headers["User-Agent"] = BROWSER_UA
        self.http._client.headers["User-Agent"] = BROWSER_UA
        self.max_docs = int(max_docs)
        self.include_esef = str(include_esef).lower() not in ("0", "false")
        self.include_dsh = str(include_dsh).lower() not in ("0", "false")

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def search(self, criteria: list[dict[str, Any]], since: date | None, until: date | None = None,
               size: int = 500, max_hits: int = 2000) -> list[dict[str, Any]]:
        crit = [{"name": "latest_flag", "value": "Y"}] + criteria
        date_crit = None
        if since:
            date_crit = [{"name": "submitted_date", "value": {
                "from": f"{since.isoformat()}T00:00:00Z", "to": f"{(until or datetime.now(UTC).date()).isoformat()}T23:59:59Z"}}]
        hits: list[dict[str, Any]] = []
        frm = 0
        while frm < max_hits:
            body = {"from": frm, "size": size, "sort": "submitted_date", "sortorder": "desc",
                    "criteriaObj": {"criteria": crit, "dateCriteria": date_crit}}
            j = self.http.post(SEARCH_URL, json_body=body).json()
            hh = ((j.get("hits") or {}).get("hits")) if isinstance(j, dict) else None
            if hh is None:
                raise ValueError(f"NSM schema change: keys={list(j)[:5] if isinstance(j, dict) else type(j)}")
            for h in hh:
                src = h.get("_source") or {}
                if not _REQUIRED_SRC_KEYS <= set(src):
                    raise ValueError(f"NSM _source schema change: {sorted(src)[:12]}")
                hits.append(src)
            if len(hh) < size:
                break
            frm += size
        return hits

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        since_d = to_date(since) if since else None
        if since_d is None:
            since_d = (to_date(state.get("watermark")) - timedelta(days=2)) if state.get("watermark") and not entities \
                else datetime.now(UTC).date() - timedelta(days=365 if entities else 7)
        lei_crit = [{"name": "lei", "value": list(entities)}] if entities else []
        docs: list[dict[str, Any]] = []
        if self.include_dsh:
            docs += safe_call(lambda: self.search([{"name": "type_code", "value": ["DSH"]}] + lei_crit, since_d), res,
                              "search:DSH", self.log) or []
        if self.include_esef:
            acs = safe_call(lambda: self.search([{"name": "type_code", "value": ["ACS"]}] + lei_crit,
                                                since_d if not entities else since_d - timedelta(days=3 * 365)),
                            res, "search:ACS", self.log) or []
            docs += [d for d in acs if d.get("json_link")]
        self.save_raw("search", f"hits_{datetime.now(UTC):%H%M%S}", docs)
        landed: list[dict[str, Any]] = []
        for d in docs[: self.max_docs]:
            key = d.get("disclosure_id") or d.get("seq_id")
            if not key:
                continue
            if d.get("json_link"):
                def get_esef(d=d, key=key) -> None:
                    doc = self.http.get(ARTEFACTS + d["json_link"], cache_ttl=-1).json()
                    landed.append({**d, "_kind": "esef", "_raw_path": str(self.save_raw("esef_json", key, numeric_facts(doc)))})
                safe_call(get_esef, res, f"esef:{key}", self.log)
            elif d.get("download_link"):
                def get_dsh(d=d, key=key) -> None:
                    r = self.http.get(ARTEFACTS + d["download_link"], cache_ttl=-1)
                    landed.append({**d, "_kind": "dsh", "_raw_path": str(self.save_raw("dsh_html", key, r.content))})
                safe_call(get_dsh, res, f"dsh:{key}", self.log)
        res.meta["docs"] = landed
        pubs = [to_date(d.get("publication_date")) for d in docs if d.get("publication_date")]
        if pubs and not entities:
            res.watermark = max(p for p in pubs if p).isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frows: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        ins: list[dict[str, Any]] = []
        n_unparsed = 0
        for d in fetched.meta.get("docs", []):
            key = d.get("disclosure_id") or d.get("seq_id")
            pub_ts = datetime.fromisoformat(d["publication_date"].replace("Z", "+00:00")).astimezone(UTC)
            kd = pub_ts.date()
            lei = d.get("lei") or ""
            entity = lei or f"name:{(d.get('company') or '').strip(';').strip()}"
            rel = str(Path(d["_raw_path"]).relative_to(self.settings.data_dir))
            fid = f"{self.source_id}:{key}"
            if d["_kind"] == "esef":
                m = re.search(r"(\d{4}-\d{2}-\d{2})_ixbrl\.json", d.get("json_link", ""))
                pe = to_date(m.group(1)) if m else None
                frows.append(filing_row(source_id=self.source_id, native_key=key, source_entity_id=entity, form_type="ACS",
                                        form_class="annual", knowledge_date=kd, knowledge_ts=pub_ts, period_end=pe,
                                        fiscal_year=pe.year if pe else None, fiscal_period="FY",
                                        url=ARTEFACTS + (d.get("html_link") or d["json_link"]), doc_path=rel,
                                        has_xbrl=True, language="en"))
                facts += parse_xbrl_json(self.load_raw_json(Path(d["_raw_path"])), source_id=self.source_id,
                                         source_entity_id=entity, filing_id=fid, form_type="ACS", knowledge_date=kd,
                                         report_period_end=pe)
                continue
            frows.append(filing_row(source_id=self.source_id, native_key=key, source_entity_id=entity, form_type="DSH",
                                    form_class="insider", knowledge_date=kd, knowledge_ts=pub_ts,
                                    url=ARTEFACTS + d["download_link"], doc_path=rel, language="en",
                                    items=[d.get("headline") or ""]))
            try:
                parsed = parse_pdmr_notice(self.load_raw(Path(d["_raw_path"])))
            except Exception:  # noqa: BLE001
                parsed = []
            if not parsed:
                n_unparsed += 1
            for i, p in enumerate(parsed):
                if not p["name"] or p["volume"] is None:
                    continue
                ins.append(insider_row(
                    source_id=self.source_id, filing_id=fid, line=i, region="UK", source_entity_id=p.get("lei") or entity,
                    insider_name=p["name"], officer_title=p.get("position"), transaction_date=p.get("date") or kd,
                    knowledge_date=kd, knowledge_ts=pub_ts, txn_code=None, txn_type=p["txn_type"],
                    is_open_market=p["is_open_market"], is_derivative=p["is_derivative"], shares=p["volume"],
                    price=p["price"], currency=p["currency"], is_amendment=p["is_amendment"],
                    form_type="DSH", security_title=(p.get("instrument") or None)))
        fetched.meta["dsh_unparsed"] = n_unparsed
        return {"filings_index": frame(frows, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw"),
                "insider_transactions": frame(ins, "insider_transactions")}


def run(ctx: Any) -> Any:
    return run_adapter_job(FcaNsmAdapter, ctx)
