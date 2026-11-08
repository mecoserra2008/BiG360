"""EU / UK / EEA official net short-position disclosures -> eu_short_positions (roadmap #7; job `eu_short_positions`,
nightly). Contract: contracts/schemas/eu_short_positions.yaml; source formats verified live 2026-10-19 and documented
in docs/SMART_MONEY.md.

Every register below is a public file or open API published by the competent authority (robots.txt allows the path;
one request per file per run, >= 2 s apart; FSMA asks for a 30 s crawl delay and is fetched once):

  fca    UK FCA   individual positions: short-positions-daily-update.xlsx (frozen 'Historic Disclosures 10.07.2026'
                  sheet - the UK regime stopped naming holders on 10 Jul 2026); aggregated net short positions per
                  issuer since then: aggregated-current-net-short-positions.csv + aggregated-historic-...csv
  amf    AMF      data.gouv.fr dataset 62738e33f1be79935d3e5553 (all published positions since 2012-11-01; holder LEI,
                  start / end of publication) - resource URL resolved through the data.gouv API
  cnmv   CNMV     DocPortal/Posiciones-Cortas/NetShortPositions.xls (sheets Current, Series, Previous; issuer LEI)
  afm    AFM      export.aspx CSV of the current and the historic register
  cbi    CBI      table-of-significant-net-short-positions-in-shares.xlsx (Current + Historical; values as fractions)
  fsma   FSMA     /en/de-shortselling?page&_format=csv (current positions only; history accumulates in the table)
  fi_se  FI       BlankningsRegister/GetAktuellFile + GetHistFile (ODS; '<0,5' when a position fell below 0.5%)
  ftno   Finanstilsynet  ssr.finanstilsynet.no/api/v2/instruments/export-json (open API; aggregate short % per
                  instrument and day + active holder positions, last 2 years; a holder dropping out of the active
                  list is recorded as a below-threshold row on that date)

Skipped (reported): CONSOB (Radware bot challenge on PncPubbl.xlsx), BaFin / Bundesanzeiger (robots.txt
'Disallow: /nlp' - the net short position pages), Denmark (monthly xlsx snapshots only), Finland (HTML tables only).

Raw files: only the latest download per source is kept (data/raw/eu_short/<regulator>/latest.<ext>), no daily copies.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.http import HttpClient
from app.core.ratelimit import RateLimit
from app.core.schema import empty_frame
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

SOURCE_ID = "eu_short"
AGG = "(aggregate)"
URLS = {
    "fca_individual": "https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx",
    "fca_agg_current": "https://www.fca.org.uk/publication/documents/aggregated-current-net-short-positions.csv",
    "fca_agg_hist": "https://www.fca.org.uk/publication/documents/aggregated-historic-net-short-positions.csv",
    "amf_dataset": "https://www.data.gouv.fr/api/1/datasets/62738e33f1be79935d3e5553/",
    "cnmv": "https://www.cnmv.es/DocPortal/Posiciones-Cortas/NetShortPositions.xls",
    "afm_current": "https://www.afm.nl/export.aspx?type=8a46a4ef-f196-4467-a7ab-1ae1cb58f0e7&format=csv",
    "afm_hist": "https://www.afm.nl/export.aspx?type=3ca31b3d-23d9-4fa2-b846-29c7e3f0e5ff&format=csv",
    "cbi": ("https://www.centralbank.ie/docs/default-source/regulation/industry-market-sectors/securities-markets/"
            "short-selling-regulation/public-net-short-positions/table-of-significant-net-short-positions-in-shares.xlsx"),
    "fsma": "https://www.fsma.be/en/de-shortselling?page&_format=csv",
    "fi_se_current": "https://www.fi.se/BlankningsRegister/GetAktuellFile",
    "fi_se_hist": "https://www.fi.se/BlankningsRegister/GetHistFile",
    "ftno": "https://ssr.finanstilsynet.no/api/v2/instruments/export-json",
}
COUNTRY = {"fca": "GB", "amf": "FR", "cnmv": "ES", "afm": "NL", "cbi": "IE", "fsma": "BE", "fi_se": "SE", "ftno": "NO"}
#: business days added to the position date when the regulator publishes no date (notification is due T+1 15:30;
#: FI and Finanstilsynet publish automatically on receipt)
LAG_BDAYS = {"fca": 2, "cnmv": 2, "afm": 2, "cbi": 2, "fsma": 2, "fi_se": 1, "ftno": 1, "amf": 2}
SKIPPED = {
    "consob": "Italy CONSOB: PncPubbl.xlsx is behind a Radware bot challenge (captcha) - automated access refused",
    "bafin": "Germany BaFin / Bundesanzeiger: robots.txt 'Disallow: /nlp' (the net short position pages) - not fetched",
    "dfsa": "Denmark Finanstilsynet: monthly xlsx snapshots only (not a current / historic register)",
    "fin_fsa": "Finland FIN-FSA: HTML tables only, no machine-readable file",
}


# ------------------------------------------------------------------------------------------------ helpers


def add_bdays(d: date, n: int) -> date:
    x = d
    while n > 0:
        x += timedelta(days=1)
        if x.weekday() < 5:
            n -= 1
    return x


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace("%", "").replace("\xa0", "").replace(" ", "")
    if not s or s.startswith("<"):
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt).date()
        except ValueError:
            continue
    return None


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def row(regulator: str, holder: str | None, issuer: str | None, isin: str | None, pct: float | None,
        pos_date: date | None, *, ptype: str = "individual", pub: date | None = None, holder_lei: str | None = None,
        issuer_lei: str | None = None, shares: float | None = None, below: bool | None = None,
        current: bool | None = None, url: str = "") -> dict[str, Any] | None:
    isin = (_clean(isin) or "").upper().replace(" ", "")
    if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin) or pos_date is None:
        return None
    holder = _clean(holder) or (AGG if ptype == "aggregate" else None)
    if holder is None:
        return None
    if below is None:
        below = (pct is None or pct < 0.5) if ptype == "individual" else (pct is not None and pct <= 0)
    return {"regulator": regulator, "country": COUNTRY[regulator], "position_type": ptype, "holder_name": holder,
            "holder_lei": _clean(holder_lei), "issuer_name": _clean(issuer), "isin": isin, "issuer_lei": _clean(issuer_lei),
            "position_pct": pct, "below_threshold": bool(below), "shares": shares, "position_date": pos_date,
            "publication_date": pub, "in_current_list": current, "source_url": url}


def _csv_rows(content: bytes, delimiter: str, encoding: str = "utf-8-sig") -> list[list[str]]:
    text = content.decode(encoding, errors="replace")
    return [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]


def _xlsx_sheets(content: bytes) -> dict[str, list[tuple]]:
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        return {ws.title: list(ws.iter_rows(values_only=True)) for ws in wb.worksheets}
    finally:
        wb.close()


def _ods_rows(content: bytes) -> list[list[str]]:
    """First table of an ODS document as text cells (repeated cells expanded, max 32 columns)."""
    from lxml import etree  # noqa: PLC0415

    ns = {"table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
          "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0"}
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(content)).read("content.xml"))
    tb = next(root.iter(f"{{{ns['table']}}}table"))
    out = []
    for r in tb.iter(f"{{{ns['table']}}}table-row"):
        vals: list[str] = []
        for cell in r.findall("table:table-cell", ns):
            rep = min(int(cell.get(f"{{{ns['table']}}}number-columns-repeated", "1")), 32)
            v = (cell.get(f"{{{ns['office']}}}date-value") or "".join(cell.itertext())).strip()
            vals += [v] * rep
            if len(vals) >= 32:
                break
        if any(vals):
            out.append(vals)
    return out


def _header_index(rows: Sequence[Sequence[Any]], needle: str) -> int:
    for i, r in enumerate(rows):
        if any(needle.lower() in str(c or "").lower() for c in r):
            return i
    raise ValueError(f"header containing {needle!r} not found (format change?)")


# ------------------------------------------------------------------------------------------------ parsers


def parse_fca_individual(content: bytes, url: str = URLS["fca_individual"]) -> list[dict[str, Any]]:
    out = []
    for name, rows in _xlsx_sheets(content).items():
        if not rows:
            continue
        h = _header_index(rows, "Position Holder")
        cur = "current" in name.lower()
        for r in rows[h + 1:]:
            if len(r) < 5:
                continue
            x = row("fca", r[0], r[1], r[2], _num(r[3]), _date(r[4]), current=cur, url=url)
            if x:
                out.append(x)
    return out


def parse_fca_aggregate(content: bytes, current: bool, url: str) -> list[dict[str, Any]]:
    rows = _csv_rows(content, ",")
    h = _header_index(rows, "ISIN")
    out = []
    for r in rows[h + 1:]:
        if len(r) < 4:
            continue
        x = row("fca", None, r[0], r[1], _num(r[2]), _date(r[3]), ptype="aggregate", current=current, url=url)
        if x:
            out.append(x)
    return out


def parse_amf(content: bytes, url: str) -> list[dict[str, Any]]:
    rows = _csv_rows(content, ";")
    h = _header_index(rows, "code ISIN")
    hdr = [c.strip().lower() for c in rows[h]]

    def col(sub: str) -> int:
        return next(i for i, c in enumerate(hdr) if sub in c)

    ih, il, ie, ir, ii = col("detenteur de la position"), col("legal entity"), col("emetteur"), col("ratio"), col("isin")
    ip, ipub, iend = col("debut position"), col("debut de publication"), col("fin de publication")
    out = []
    for r in rows[h + 1:]:
        if len(r) <= max(ih, il, ie, ir, ii, ip, ipub, iend):
            continue
        end = _date(r[iend])
        x = row("amf", r[ih], r[ie], r[ii], _num(r[ir]), _date(r[ip]), pub=_date(r[ipub]), holder_lei=r[il],
                current=end is None, url=url)
        if x:
            out.append(x)
    return out


def xls_sheets(content: bytes) -> dict[str, list[list[Any]]]:
    import xlrd  # noqa: PLC0415

    wb = xlrd.open_workbook(file_contents=content)
    return {sh.name: [sh.row_values(i) for i in range(sh.nrows)] for sh in wb.sheets()}


def parse_cnmv(content: bytes, url: str = URLS["cnmv"]) -> list[dict[str, Any]]:
    return parse_cnmv_sheets(xls_sheets(content), url)


def parse_cnmv_sheets(sheets: dict[str, list[list[Any]]], url: str = URLS["cnmv"]) -> list[dict[str, Any]]:
    pub = None
    out = []
    for name, rows in sheets.items():
        if name.lower().startswith("fecha"):
            pub = next((d for r in rows for d in (_date(c) for c in r) if d), None)
            continue
        if not rows:
            continue
        try:
            h = _header_index(rows, "ISIN")
        except ValueError:
            continue
        cur = "current" in name.lower()
        for r in rows[h + 1:]:
            if len(r) < 6:
                continue
            x = row("cnmv", r[3], r[2], r[1], _num(r[5]), _date(r[4]), issuer_lei=r[0], current=cur, url=url)
            if x:
                out.append(x)
    for x in out:
        x["publication_date"] = pub if x["in_current_list"] else None
        x["_file_date"] = pub
    return out


def parse_afm(content: bytes, current: bool, url: str) -> list[dict[str, Any]]:
    try:
        text_bytes = content.decode("utf-8").encode("utf-8")
    except UnicodeDecodeError:
        text_bytes = content.decode("cp1252", errors="replace").encode("utf-8")
    rows = _csv_rows(text_bytes, ";")
    h = _header_index(rows, "ISIN")
    out = []
    for r in rows[h + 1:]:
        if len(r) < 5:
            continue
        x = row("afm", r[0], r[1], r[2], _num(r[3]), _date(r[4]), current=current, url=url)
        if x:
            out.append(x)
    return out


def parse_cbi(content: bytes, url: str = URLS["cbi"]) -> list[dict[str, Any]]:
    out = []
    for name, rows in _xlsx_sheets(content).items():
        cur = name.lower().startswith("current")
        for r in rows[1:]:
            if len(r) < 6 or r[1] is None or r[3] is None:
                continue
            pct = _num(r[4])
            pct = pct * 100.0 if pct is not None and pct < 1.0 else pct   # published as fractions (0.0105 = 1.05%)
            x = row("cbi", r[1], r[2], r[3], pct, _date(r[5]), current=cur, url=url)
            if x:
                out.append(x)
    return out


def parse_fsma(content: bytes, url: str = URLS["fsma"]) -> list[dict[str, Any]]:
    rows = _csv_rows(content, ",")
    h = _header_index(rows, "ISIN")
    out = []
    for r in rows[h + 1:]:
        if len(r) < 6:
            continue
        x = row("fsma", r[4], r[2], r[3], _num(r[5]), _date(r[0]), current=True, url=url)
        if x:
            out.append(x)
    return out


def parse_fi_se(content: bytes, current: bool, url: str) -> list[dict[str, Any]]:
    rows = _ods_rows(content)
    h = _header_index(rows, "ISIN")
    out = []
    for r in rows[h + 1:]:
        if len(r) < 5:
            continue
        raw = str(r[3]).strip()
        x = row("fi_se", r[0], r[1], r[2], _num(raw), _date(r[4]), below=raw.startswith("<") or None,
                current=current, url=url)
        if x:
            out.append(x)
    return out


def parse_ftno(doc: list[dict[str, Any]], url: str = URLS["ftno"]) -> list[dict[str, Any]]:
    out = []
    for inst in doc:
        isin, issuer = inst.get("isin"), inst.get("issuerName")
        events = sorted(inst.get("events") or [], key=lambda e: e.get("date") or "")
        active: set[str] = set()
        last = events[-1]["date"] if events else None
        for ev in events:
            d = _date(ev.get("date"))
            x = row("ftno", None, issuer, isin, _num(ev.get("shortPercent")), d, ptype="aggregate",
                    shares=_num(ev.get("shares")), current=ev.get("date") == last, url=url)
            if x:
                out.append(x)
            now_active = set()
            for ap in ev.get("activePositions") or []:
                h = _clean(ap.get("positionHolder"))
                if not h:
                    continue
                now_active.add(h)
                y = row("ftno", h, issuer, isin, _num(ap.get("shortPercent")), _date(ap.get("date")),
                        shares=_num(ap.get("shares")), current=ev.get("date") == last, url=url)
                if y:
                    out.append(y)
            for h in sorted(active - now_active):   # dropped from the published list -> below 0.5%
                z = row("ftno", h, issuer, isin, None, d, below=True, current=False, url=url)
                if z:
                    out.append(z)
            active = now_active
    return out


# ------------------------------------------------------------------------------------------------ frame


def to_frame(rows: list[dict[str, Any]], download_date: date, now: datetime) -> pl.DataFrame:
    """Rows -> contract frame: knowledge dates (observed / estimated + capped), dedupe on the PK (current-list and
    later rows win)."""
    if not rows:
        return empty_frame("eu_short_positions")
    for r in rows:
        file_date = r.pop("_file_date", None)
        if r["publication_date"] is not None and r["regulator"] == "amf":
            kd, est = r["publication_date"], False
        else:
            # estimate, capped by what is certain: every row of a file was public by the file date and by our download
            kd, est = add_bdays(r["position_date"], LAG_BDAYS[r["regulator"]]), True
            kd = min([kd] + [x for x in (r["publication_date"], file_date, download_date) if x is not None])
            kd = max(kd, r["position_date"])
        r["knowledge_date"], r["knowledge_date_estimated"] = kd, est
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.lit(SOURCE_ID).alias("source_id"), pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
        pl.lit(None, pl.Utf8).alias("issuer_id"), pl.lit(None, pl.Utf8).alias("security_id"))
    df = (df.with_columns(pl.col("in_current_list").fill_null(False).cast(pl.Int8).alias("_cur"))
          .sort(["_cur", "knowledge_date"]).unique(["regulator", "position_type", "holder_name", "isin", "position_date"],
                                                    keep="last", maintain_order=True).drop("_cur"))
    return df


def resolve_ids(df: pl.DataFrame) -> pl.DataFrame:
    """security_id / issuer_id via identity: ISIN (security level), else issuer LEI -> primary security."""
    if df.is_empty():
        return df
    from app.ingestion.sec_index import try_lookup_many  # noqa: PLC0415

    m = try_lookup_many("isin", df["isin"].unique().to_list())
    sec = {k: v.get("security_id") or v.get("primary_security_id") for k, v in m.items()}
    iss = {k: v.get("issuer_id") for k, v in m.items()}
    leis = [x for x in df["issuer_lei"].drop_nulls().unique().to_list()]
    ml = try_lookup_many("lei", leis)
    lsec = {k: v.get("primary_security_id") or v.get("security_id") for k, v in ml.items()}
    liss = {k: v.get("issuer_id") for k, v in ml.items()}
    return df.with_columns(
        pl.coalesce(pl.col("isin").replace_strict(sec, default=None),
                    pl.col("issuer_lei").replace_strict(lsec, default=None)).alias("security_id"),
        pl.coalesce(pl.col("isin").replace_strict(iss, default=None),
                    pl.col("issuer_lei").replace_strict(liss, default=None)).alias("issuer_id"))


# ------------------------------------------------------------------------------------------------ adapter


@register_adapter
class EuShortAdapter(SourceAdapter):
    source_id = SOURCE_ID
    produces = ("eu_short_positions",)
    regions = ("EU", "UK")
    rate_limit = RateLimit(0.5, 1)

    def __init__(self, *a: Any, params: dict[str, Any] | None = None, **kw: Any):
        super().__init__(*a, **kw)
        self.params = params or {}

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def _get(self, key: str, url: str) -> bytes:
        r = self.http.get(url, cache_ttl=0)
        ext = {"fca_individual": "xlsx", "cbi": "xlsx", "cnmv": "xls", "fi_se_current": "ods", "fi_se_hist": "ods",
               "ftno": "json", "amf": "csv"}.get(key, "csv")
        d = self.settings.raw_dir / SOURCE_ID / key
        d.mkdir(parents=True, exist_ok=True)
        (d / f"latest.{ext}").write_bytes(r.content)
        if r.content[:15].lower().startswith((b"<!doctype html", b"<html")) and ext != "csv":
            raise ValueError(f"{key}: HTML instead of a {ext} file (bot challenge / format change)")
        return r.content

    def _amf_url(self) -> str:
        doc = self.http.get(URLS["amf_dataset"], cache_ttl=0).json()
        res = [r for r in doc.get("resources") or [] if (r.get("format") or "").lower() == "csv"]
        if not res:
            raise ValueError("AMF dataset has no CSV resource")
        return max(res, key=lambda r: r.get("last_modified") or "")["url"]

    def _amf_rows(self) -> list[dict[str, Any]]:
        u = self._amf_url()
        return parse_amf(self._get("amf", u), u)

    def jobs(self) -> dict[str, Callable[[], list[dict[str, Any]]]]:
        import orjson  # noqa: PLC0415

        return {
            "fca_individual": lambda: parse_fca_individual(self._get("fca_individual", URLS["fca_individual"])),
            "fca_agg_current": lambda: parse_fca_aggregate(self._get("fca_agg_current", URLS["fca_agg_current"]), True,
                                                           URLS["fca_agg_current"]),
            "fca_agg_hist": lambda: parse_fca_aggregate(self._get("fca_agg_hist", URLS["fca_agg_hist"]), False,
                                                        URLS["fca_agg_hist"]),
            "amf": self._amf_rows,
            "cnmv": lambda: parse_cnmv(self._get("cnmv", URLS["cnmv"])),
            "afm_current": lambda: parse_afm(self._get("afm_current", URLS["afm_current"]), True, URLS["afm_current"]),
            "afm_hist": lambda: parse_afm(self._get("afm_hist", URLS["afm_hist"]), False, URLS["afm_hist"]),
            "cbi": lambda: parse_cbi(self._get("cbi", URLS["cbi"])),
            "fsma": lambda: parse_fsma(self._get("fsma", URLS["fsma"])),
            "fi_se_current": lambda: parse_fi_se(self._get("fi_se_current", URLS["fi_se_current"]), True,
                                                 URLS["fi_se_current"]),
            "fi_se_hist": lambda: parse_fi_se(self._get("fi_se_hist", URLS["fi_se_hist"]), False, URLS["fi_se_hist"]),
            "ftno": lambda: parse_ftno(orjson.loads(self._get("ftno", URLS["ftno"]))),
        }

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(source_id=SOURCE_ID, started_at=datetime.now(UTC))
        only = set(self.params.get("sources") or [])
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for key, fn in self.jobs().items():
            if only and key not in only and key.split("_")[0] not in only:
                continue
            try:
                got = fn()
                if not got:
                    raise ValueError("no rows parsed (format change?)")
                rows += got
                counts[key] = len(got)
                res.entities_ok.append(key)
            except Exception as e:  # noqa: BLE001 - one register down must not stop the others
                res.entities_failed[key] = str(e)[:300]
                self.log.warning("eu_short %s failed: %s", key, e)
        res.meta.update(rows=rows, counts=counts, skipped=SKIPPED)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        now = datetime.now(UTC)
        df = to_frame(fetched.meta.get("rows") or [], now.date(), now)
        df = resolve_ids(df)
        fetched.meta["n_rows"] = df.height
        if df.height:
            fetched.meta["n_isin"] = df["isin"].n_unique()
            fetched.meta["n_isin_mapped"] = df.filter(pl.col("security_id").is_not_null())["isin"].n_unique()
        fetched.meta.pop("rows", None)
        return {"eu_short_positions": df}


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    ad = EuShortAdapter(settings=ctx.settings, params=dict(ctx.params),
                        http=HttpClient(SOURCE_ID, rate_limit=RateLimit(0.5, 1), settings=ctx.settings))
    fetched, rows = ad.run()
    m = fetched.meta
    return JobResult(status=fetched.status, rows_written=rows, entities_ok=len(fetched.entities_ok),
                     entities_failed=len(fetched.entities_failed), http_requests=ad.http.n_requests,
                     message=(f"rows={m.get('n_rows')} isin={m.get('n_isin')} mapped={m.get('n_isin_mapped')} "
                              f"per_source={m.get('counts')} failed={fetched.entities_failed} skipped={sorted(SKIPPED)}"))
