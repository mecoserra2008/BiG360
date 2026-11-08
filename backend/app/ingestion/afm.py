"""Netherlands AFM register 'Transacties leidinggevenden MAR 19' -> insider_transactions (+ filings_index).

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §5.5 (verified live 2026-10-10).
  Full export (XML, ~4.8 MB): https://www.afm.nl/export.aspx?type=0ee836dc-5520-459d-bcf4-a4a689de6614&format=xml
     <vermelding><meldingid/><transactiedatum>10/8/2026 12:00:00 AM</transactiedatum><uitgevendeinstelling/>
                 <meldingsplichtige/><functie/><lei/></vermelding>
  The export has no price/volume/direction; those come from the per-notice detail page (HTML table):
     https://www.afm.nl/nl-nl/sector/registers/meldingenregisters/transacties-leidinggevenden-mar19-/details?id=<meldingid>
     columns: Type instrument | ISIN | Aard transactie | Soort transactie | Aandelenoptie programma | Plaats van handel |
              Prijs | Aantal | Eenheid
PIT: the meldingid starts with the notification date (YYYYMMDD); knowledge_date = max(that, transaction date).
Keys: none. source_entity_id = issuer LEI.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from bs4 import BeautifulSoup
from lxml import etree

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, insider_row, load_state, parse_num, run_adapter_job,
                                          safe_call, save_state, to_date, to_date_us)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

EXPORT = "https://www.afm.nl/export.aspx?type=0ee836dc-5520-459d-bcf4-a4a689de6614&format=xml"
DETAIL = "https://www.afm.nl/nl-nl/sector/registers/meldingenregisters/transacties-leidinggevenden-mar19-/details"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
DETAIL_COLS = ["Type instrument", "ISIN", "Aard transactie", "Soort transactie", "Aandelenoptie programma",
               "Plaats van handel", "Prijs", "Aantal", "Eenheid"]


def parse_afm_export(content: bytes) -> list[dict[str, Any]]:
    root = etree.fromstring(content.lstrip(b"\xef\xbb\xbf"))
    out = []
    for v in root.iter("vermelding"):
        mid = v.findtext("meldingid")
        if not mid:
            continue  # nested <vermelding> text node
        out.append({"meldingid": mid.strip(), "transactiedatum": to_date_us(v.findtext("transactiedatum")),
                    "issuer": (v.findtext("uitgevendeinstelling") or "").strip(),
                    "person": (v.findtext("meldingsplichtige") or "").strip(),
                    "related_to": (v.findtext("nauwgelieerdaan") or "").strip() or None,
                    "functie": (v.findtext("functie") or "").strip() or None,
                    "lei": (v.findtext("lei") or "").strip() or None})
    return out


def notice_date(meldingid: str) -> date | None:
    m = re.match(r"^(\d{8})", meldingid)
    return to_date(m.group(1)) if m else None


def parse_afm_detail(html: bytes | str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return []
    heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    if heads[: len(DETAIL_COLS)] != DETAIL_COLS:
        raise ValueError(f"AFM detail table changed: {heads[:5]}")
    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = []
        for td in tr.find_all("td"):
            for sp in td.find_all("span", class_="cc-mobile-title"):
                sp.decompose()
            cells.append(td.get_text(" ", strip=True))
        if len(cells) >= len(DETAIL_COLS):
            rows.append(dict(zip(DETAIL_COLS, cells)))
    return rows


def afm_txn(d: dict[str, str]) -> tuple[str, bool]:
    soort = d["Soort transactie"].strip().lower()
    aard = d["Aard transactie"].strip().lower()
    option_plan = d["Aandelenoptie programma"].strip().lower() == "ja"
    venue = d["Plaats van handel"].strip().lower()
    if option_plan:
        return ("option_exercise" if "uitoef" in soort else "grant"), False
    if soort == "koop":
        return "buy", venue not in ("", "otc", "buiten handelsplatform")
    if soort == "verkoop":
        return "sell", venue not in ("", "otc", "buiten handelsplatform")
    if "schenk" in soort:
        return "gift", False
    if "uitoef" in soort:
        return "option_exercise", False
    if "toeken" in soort or "toewijz" in soort:
        return "grant", False
    return ("other", False) if aard not in ("verwerving", "vervreemding") else ("other", False)


@register_adapter
class AfmMar19Adapter(SourceAdapter):
    source_id = "eu_pdmr_afm"
    regions = ("EU",)
    produces = ("filings_index", "insider_transactions")
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, max_details: int = 200, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = BROWSER_UA
        self.max_details = int(max_details)

    def discover(self) -> pl.DataFrame:
        r = self.http.get(EXPORT, cache_ttl=6 * 3600)
        seen: dict[str, str] = {}
        for x in parse_afm_export(r.content):
            if x["lei"]:
                seen.setdefault(x["lei"], x["issuer"])
        return self.discover_frame([{"source_entity_id": k, "lei": k, "name": v, "country": "NL"} for k, v in seen.items()],
                                   self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        since_d = to_date(since) if since else None
        if since_d is None:
            since_d = to_date(state.get("watermark")) - timedelta(days=3) if state.get("watermark") and not entities \
                else datetime.now(UTC).date() - timedelta(days=365 if entities else 14)
        r = self.http.get(EXPORT, cache_ttl=3 * 3600)
        export = parse_afm_export(r.content)
        if not export:
            raise ValueError("AFM export empty / schema change")
        self.save_raw("export", "mar19", r.content)
        ents = {e.upper() for e in entities} if entities else None
        todo = [x for x in export if (notice_date(x["meldingid"]) or date.min) >= since_d
                and (ents is None or (x["lei"] or "").upper() in ents)]
        landed = []
        for x in todo[: self.max_details]:
            def one(x=x) -> None:
                d = self.http.get(DETAIL, params={"id": x["meldingid"]}, cache_ttl=-1)
                parse_afm_detail(d.content)
                landed.append({**x, "_raw_path": str(self.save_raw("detail", x["meldingid"], d.content))})
            safe_call(one, res, x["meldingid"], self.log)
        res.meta["notices"] = landed
        dates = [notice_date(x["meldingid"]) for x in todo]
        if dates and not entities:
            res.watermark = max(d for d in dates if d).isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        fil: list[dict[str, Any]] = []
        ins: list[dict[str, Any]] = []
        for x in fetched.meta.get("notices", []):
            tdate = x["transactiedatum"]
            kd = max(d for d in (notice_date(x["meldingid"]), tdate) if d)
            entity = x["lei"] or f"name:{x['issuer']}"
            fid = f"{self.source_id}:{x['meldingid']}"
            p = Path(x["_raw_path"])
            fil.append(filing_row(source_id=self.source_id, native_key=x["meldingid"], source_entity_id=entity,
                                  form_type="MAR19", form_class="insider", knowledge_date=kd, language="nl",
                                  url=f"{DETAIL}?id={x['meldingid']}", doc_path=str(p.relative_to(self.settings.data_dir)),
                                  items=[x["issuer"]]))
            for i, d in enumerate(parse_afm_detail(self.load_raw(p))):
                shares = parse_num(d["Aantal"], decimal=",")
                price = parse_num(d["Prijs"], decimal=",")
                if not shares:
                    continue
                txn, open_mkt = afm_txn(d)
                ins.append(insider_row(
                    source_id=self.source_id, filing_id=fid, line=i, region="EU", source_entity_id=entity,
                    insider_name=x["person"] if not x["related_to"] else f"{x['person']} (PCA of {x['related_to']})",
                    officer_title=x["functie"], transaction_date=tdate or kd, knowledge_date=kd,
                    txn_code=f"{d['Aard transactie']}/{d['Soort transactie']}", txn_type=txn, is_open_market=open_mkt,
                    is_derivative=not re.search(r"aandel|share|certificaat", d["Type instrument"], re.I),
                    shares=shares, price=price, currency=(d["Eenheid"] or "").strip()[:3].upper() or None,
                    form_type="MAR19", security_title=d["Type instrument"] or None))
        return {"filings_index": frame(fil, "filings_index"), "insider_transactions": frame(ins, "insider_transactions")}


def run(ctx: Any) -> Any:
    return run_adapter_job(AfmMar19Adapter, ctx)
