"""Germany BaFin directors' dealings (MAR art.19) database -> insider_transactions (+ filings_index).

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §5.5 (verified live 2026-10-10).
  GET https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do?emittentName=<substring>&d-4000784-e=1&6578706f7274=1
  -> text/csv (UTF-8 BOM, ';', German numbers): Emittent;Bafin-ID;ISIN;Meldepflichtiger;Position / Status;
     Art des Instruments;Art des Geschäfts;Durchschnittspreis;Aggregiertes Volumen;Mitteilungsdatum;Datum des Geschäfts;
     Ort des Geschäfts;Datum der Aktivierung
'Aggregiertes Volumen' is the aggregated VALUE (price x shares, in currency) => shares = value / average price.
PIT: knowledge_ts = 'Datum der Aktivierung' (publication, Europe/Berlin -> UTC). Retention on the portal is limited,
so the nightly job archives raw CSVs. Keys: none. source_entity_id = issuer share ISIN (falls back to row ISIN).

DISABLED 2026-11-07 (docs/COVERAGE_V3_PLAN.md A9, docs/it/ROBOTS_COMPLIANCE.md): portal.mvp.bafin.de robots.txt is
`User-agent: *` / `Disallow: /`. The job eu_pdmr_bafin returns skipped (reason code source_disallowed) without any
request, app.core.robots_policy blocks the host in app.core.http; notices ingested before the switch stay in
filings_index / insider_transactions (source_id eu_pdmr_bafin). The parsers stay for those rows and the tests.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, insider_row, parse_num, run_adapter_job, safe_call,
                                          seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

URL = "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do"
BERLIN = ZoneInfo("Europe/Berlin")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
EXPECTED_HEADER = ["Emittent", "Bafin-ID", "ISIN", "Meldepflichtiger", "Position / Status", "Art des Instruments",
                   "Art des Geschäfts", "Durchschnittspreis", "Aggregiertes Volumen", "Mitteilungsdatum",
                   "Datum des Geschäfts", "Ort des Geschäfts", "Datum der Aktivierung"]

TXN_MAP = {"kauf": "buy", "verkauf": "sell", "zeichnung": "buy", "schenkung": "gift", "ausübung": "option_exercise",
           "zuteilung": "grant", "erwerb": "buy", "veräußerung": "sell", "verpfändung": "other", "sonstiges": "other"}


def _ts_berlin(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.strip(), "%d.%m.%Y %H:%M:%S").replace(tzinfo=BERLIN).astimezone(UTC)
    except (ValueError, AttributeError):
        return None


def _money(s: str) -> tuple[float | None, str | None]:
    s = (s or "").strip()
    if not s:
        return None, None
    parts = s.rsplit(" ", 1)
    ccy = parts[1].upper() if len(parts) == 2 and len(parts[1]) == 3 and parts[1].isalpha() else None
    return parse_num(parts[0], decimal=","), ccy


def parse_bafin_csv(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace")
    rd = csv.reader(io.StringIO(text), delimiter=";")
    rows = list(rd)
    if not rows:
        return []
    header = [h.strip() for h in rows[0]]
    if header[: len(EXPECTED_HEADER)] != EXPECTED_HEADER:
        raise ValueError(f"BaFin CSV header changed: {header[:6]}")
    return [dict(zip(header, r)) for r in rows[1:] if len(r) >= len(EXPECTED_HEADER)]


def bafin_rows_to_tables(rows: list[dict[str, str]], source_id: str = "eu_pdmr_bafin",
                         raw_rel: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    share_isin: dict[str, str] = {}
    for r in rows:
        if r["Art des Instruments"].strip().lower() == "aktie" and r["ISIN"].strip():
            share_isin.setdefault(r["Bafin-ID"].strip(), r["ISIN"].strip())
    filings: dict[str, dict[str, Any]] = {}
    ins: list[dict[str, Any]] = []
    line_no: dict[str, int] = {}
    for r in rows:
        ts = _ts_berlin(r["Datum der Aktivierung"])
        kd = ts.date() if ts else to_date(r["Mitteilungsdatum"])
        tdate = to_date(r["Datum des Geschäfts"]) or kd
        if kd is None or tdate is None:
            continue
        entity = share_isin.get(r["Bafin-ID"].strip()) or r["ISIN"].strip() or f"bafin:{r['Bafin-ID'].strip()}"
        person = r["Meldepflichtiger"].strip()
        key = hashlib.sha1(f"{r['Bafin-ID']}|{person}|{r['Datum der Aktivierung']}".encode()).hexdigest()[:16]
        fid = f"{source_id}:{key}"
        if fid not in filings:
            filings[fid] = filing_row(source_id=source_id, native_key=key, source_entity_id=entity, form_type="MAR19",
                                      form_class="insider", knowledge_date=kd, knowledge_ts=ts, language="de",
                                      url=URL, doc_path=raw_rel, items=[r["Emittent"].strip()])
        price, ccy = _money(r["Durchschnittspreis"])
        value, ccy2 = _money(r["Aggregiertes Volumen"])
        if not price or value is None:
            continue  # zero-price grants carry no share count
        shares = abs(value) / price
        art = r["Art des Geschäfts"].strip().lower()
        txn = TXN_MAP.get(art, "other")
        venue = r["Ort des Geschäfts"].strip()
        pos = r["Position / Status"].strip()
        pl_ = pos.lower()
        is_dir = "aufsichtsrat" in pl_ or "verwaltungsrat" in pl_
        is_off = "vorstand" in pl_ or "geschäftsführ" in pl_ or "führungsaufgaben" in pl_
        role = "director" if is_dir else ("officer_other" if is_off else "other")
        line_no[fid] = line_no.get(fid, -1) + 1
        ins.append(insider_row(
            source_id=source_id, filing_id=fid, line=line_no[fid], region="EU", source_entity_id=entity,
            insider_name=person, officer_title=pos, is_director=is_dir, is_officer=is_off, role_norm=role,
            transaction_date=tdate, knowledge_date=kd, knowledge_ts=ts, txn_code=r["Art des Geschäfts"].strip(),
            txn_type=txn, is_open_market=txn in ("buy", "sell") and art in ("kauf", "verkauf") and bool(venue)
            and "außerhalb" not in venue.lower(),
            is_derivative=r["Art des Instruments"].strip().lower() not in ("aktie", "aktien"),
            shares=shares, price=price, currency=ccy or ccy2, form_type="MAR19",
            security_title=r["Art des Instruments"].strip() or None))
    return list(filings.values()), ins


@register_adapter
class BafinDealingsAdapter(SourceAdapter):
    source_id = "eu_pdmr_bafin"
    regions = ("EU",)
    produces = ("filings_index", "insider_transactions")
    rate_limit = RateLimit(0.5, 1)

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = BROWSER_UA

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def default_entities(self) -> list[str]:
        names = seed_universe(self.settings, countries=["DE"]).get_column("name").to_list()
        # first word is the most robust substring (e.g. 'Siemens' matches Siemens AG, Siemens Energy, Healthineers)
        out = []
        for n in names:
            w = n.replace("Bayerische Motoren Werke", "Bayerische Motoren").split(" ")[0]
            if len(w) >= 3 and w not in out:
                out.append(w)
        return out

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        from app.core.robots_policy import disabled_note  # noqa: PLC0415

        note = disabled_note(self.source_id)
        if note:  # A9 2026-11-07: robots.txt disallows the portal -> never request it
            res.meta.update(files=[], disabled="source_disallowed", note=note)
            return res
        names = list(entities) if entities else self.default_entities()
        self.http.get("https://portal.mvp.bafin.de/database/DealingsInfo/", cache_ttl=0)  # session cookie
        since_d = to_date(since) if since else None
        files = []
        for n in names:
            def one(n=n) -> None:
                r = self.http.get(URL, params={"emittentName": n, "d-4000784-e": "1", "6578706f7274": "1"}, cache_ttl=3600)
                if "text/csv" not in r.headers.get("content-type", ""):
                    raise ValueError(f"unexpected content-type {r.headers.get('content-type')}")
                parse_bafin_csv(r.content)  # schema check before landing
                files.append(self.save_raw("csv", f"emittent_{n.replace(' ', '_')[:40]}", r.content))
            safe_call(one, res, n, self.log)
        res.meta["files"] = [str(p) for p in files]
        res.meta["since"] = since_d.isoformat() if since_d else None
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        from pathlib import Path

        since = to_date(fetched.meta.get("since"))
        fil: list[dict[str, Any]] = []
        ins: list[dict[str, Any]] = []
        for p in fetched.meta.get("files", []):
            rows = parse_bafin_csv(self.load_raw(Path(p)))
            f, i = bafin_rows_to_tables(rows, self.source_id, str(Path(p).relative_to(self.settings.data_dir)))
            fil += f
            ins += i
        if since:
            fil = [r for r in fil if r["knowledge_date"] >= since]
            ins = [r for r in ins if r["knowledge_date"] >= since]
        return {"filings_index": frame(fil, "filings_index"), "insider_transactions": frame(ins, "insider_transactions")}


def run(ctx: Any) -> Any:
    """Job eu_pdmr_bafin: disabled (A9 2026-11-07, robots.txt `Disallow: /`) -> skipped, no request."""
    from app.core.robots_policy import skipped_job  # noqa: PLC0415

    return skipped_job(BafinDealingsAdapter.source_id) or run_adapter_job(BafinDealingsAdapter, ctx)
