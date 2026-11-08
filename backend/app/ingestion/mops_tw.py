"""Taiwan MOPS IFRS XBRL (2013Q1 ->) -> fundamentals_raw + filings_index, PIT-stamped with TWSE document-site uploads.

Source research: docs/COMPANY_COVERAGE_OPTIONS.md §4.3 (live-verified 2026-10-11).
  * Bulk seasons: https://mopsov.twse.com.tw/server-java/FileDownLoad?step=9&functionName=show_file2
        &fileName=tifrs-2026Q2.zip&filePath=/ifrs/2026/      (40-111 MB, one instance per company)
    The server answers `Range` requests with a full 200 body (no Content-Range) -> no ranged-zip reading.
  * The SAME endpoint serves single zip members directly (`fileName=tifrs-fr1-m1-ci-cr-2330-2026Q2.html`), so we fetch
    one ~0.4-0.8 MB instance per company-season instead of ~100 MB per season. Member names are
    `tifrs-{fr0|fr1|fr2}-{m1|m2}-{ci|basi|bd|fh|ins|mim}-{cr|ir|er}-{co_id}-{YYYYQn}.{xml|html}` (cr = consolidated,
    ir = individual (no subsidiaries), er = parent-only); seasons before ~2019 are plain XBRL .xml, later inline XBRL.
    Resolution order per (company, season): remembered member pattern -> per-company iXBRL viewer
    `server-java/t164sb01?step=1&CO_ID=..&SYEAR=..&SSEASON=..&REPORT_ID=C` (2019Q1 ->) -> candidate member names ->
    bulk season zip (streamed to a temp file, universe members extracted, zip deleted) only when the TWSE upload list
    proves a report exists. `mops.twse.com.tw` (new site) blocks bots; `mopsov` works.
  * PIT: https://doc.twse.com.tw/server-java/t57sb01?step=1&co_id=2330&year=114&mtype=A (ROC year, big5): every
    financial-report upload with timestamp ('114/05/15 13:40:15', Taipei UTC+8) and file '202501_2330_AI1.pdf'
    (yyyy + season, AI1 = consolidated Chinese). knowledge_date = earliest consolidated upload of the season; when
    absent, impute_knowledge_date(period_end, annual, 'APAC') with knowledge_date_estimated=True.

Rows: source_entity_id = Yahoo ticker ('2330.TW' TWSE, 'xxxx.TWO' TPEx); identity resolves via id_type yf_ticker.
ifrs-full (and the 2013-era `ifrs` 2010 namespace) -> taxonomy 'ifrs-full', bare local name; Taiwan tifrs-* concepts ->
taxonomy 'other' with the full QName. Values are scaled to full TWD (iXBRL `scale`, `sign`); plain XBRL values are
already in units. Zero-length duration contexts (statement-of-equity balances) are treated as instants.
Raw: data/raw/mops/ixbrl/<season>/<member>.gz, data/raw/mops/t57sb01/<co>_<rocY>.html.gz (reused; refresh_pit=true
re-reads). Throttling: doc.twse answers bursts with '查詢過量' and mopsov answers both busy and missing members with
'伺服器忙碌中' (HTTP 200) -> paced + retried; a member is only declared missing once the upload list was read.
State (data/state/mops.json): per company-season resolution (member / missing), remembered member patterns.
"""

from __future__ import annotations

import gzip
import re
import tempfile
import time
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (fact_row, filing_row, frame, impute_knowledge_date, load_state,
                                          run_adapter_job, safe_call, save_state, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

BASE = "https://mopsov.twse.com.tw/server-java"
DOC_LIST = "https://doc.twse.com.tw/server-java/t57sb01"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
FIRST_SEASON = "2013Q1"
IXBRL_FROM = "2019Q1"      # per-company viewer serves inline XBRL from here on; earlier seasons are .xml instances
TW_OFFSET = timedelta(hours=8)
T57_THROTTLE = "查詢過量".encode("big5")   # doc.twse burst throttle page
T57_OK = "電子資料查詢作業".encode("big5")    # title of a genuine t57sb01 answer (with or without rows)
IFRS_NS = ("xbrl.ifrs.org/taxonomy", "xbrl.iasb.org/taxonomy")
UNIT_MAP = {"TWD": "TWD", "shares": "shares", "pure": "pure", "USD": "USD", "CNY": "CNY"}
_SEASON_RE = re.compile(r"^(\d{4})Q([1-4])$")
_MEMBER_RE = re.compile(r"^tifrs-(fr\d)-(m\d)-([a-z]+)-(cr|ir|er)-([0-9A-Z]+)-(\d{4}Q[1-4])\.(xml|html)$")
_PREFIX_CANDIDATES = [(fr, m) for fr in ("fr1", "fr0", "fr2") for m in ("m1", "m2")]


# --------------------------------------------------------------------------------------------- seasons


def parse_season(s: str) -> tuple[int, int]:
    m = _SEASON_RE.match(s.strip().upper())
    if not m:
        raise ValueError(f"bad season {s!r} (want YYYYQn)")
    return int(m.group(1)), int(m.group(2))


def season_end(s: str) -> date:
    y, q = parse_season(s)
    return {1: date(y, 3, 31), 2: date(y, 6, 30), 3: date(y, 9, 30), 4: date(y, 12, 31)}[q]


def season_range(a: str, b: str) -> list[str]:
    ya, qa = parse_season(a)
    yb, qb = parse_season(b)
    out = []
    y, q = ya, qa
    while (y, q) <= (yb, qb):
        out.append(f"{y}Q{q}")
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


def latest_season(today: date) -> str:
    """Most recent season whose period has ended (its reports may still be pending)."""
    y, q = today.year, (today.month - 1) // 3  # quarter fully ended before today
    if q == 0:
        y, q = y - 1, 4
    return f"{y}Q{q}"


def parse_seasons_param(v: str | Sequence[str] | None, today: date) -> list[str] | None:
    if v is None or v == "":
        return None
    items = [x.strip() for x in (v.split(",") if isinstance(v, str) else v) if str(x).strip()]
    out: list[str] = []
    for it in items:
        if "-" in it:
            a, b = it.split("-", 1)
            out += season_range(a or FIRST_SEASON, b or latest_season(today))
        else:
            parse_season(it)
            out.append(it.upper())
    return sorted(set(out))


# --------------------------------------------------------------------------------------------- PIT upload list


_TS_RE = re.compile(r"(\d{2,3})/(\d{2})/(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
_FILE_RE = re.compile(r"(\d{4})(0[1-4])_([0-9A-Z]+)_A([0-9A-Z]+)\.pdf", re.I)


def parse_upload_list(content: bytes) -> dict[str, dict[str, Any]]:
    """t57sb01 page -> {season: {"ts_local": datetime (Taipei), "file": name, "consolidated": bool}} keeping the
    earliest upload of the consolidated report (AI1) per season, else the earliest financial-report upload."""
    text = content.decode("big5", errors="replace")
    best: dict[str, tuple[int, datetime, str]] = {}
    for row in re.findall(r"<tr.*?</tr>", text, re.S | re.I):
        fm, tm = _FILE_RE.search(row), _TS_RE.search(row)
        if not fm or not tm:
            continue
        season = f"{fm.group(1)}Q{int(fm.group(2))}"
        ts = datetime(int(tm.group(1)) + 1911, int(tm.group(2)), int(tm.group(3)), int(tm.group(4)), int(tm.group(5)),
                      int(tm.group(6)))
        rank = 0 if fm.group(4).upper() == "I1" else 1
        cur = best.get(season)
        if cur is None or (rank, ts) < (cur[0], cur[1]):
            best[season] = (rank, ts, fm.group(0))
    return {s: {"ts_local": ts, "file": f, "consolidated": rank == 0} for s, (rank, ts, f) in best.items()}


# --------------------------------------------------------------------------------------------- XBRL / iXBRL parsing


_XMLNS_RE = re.compile(r'xmlns:([\w\-.]+)="([^"]+)"')
_CTX_RE = re.compile(r"<(?:xbrli:)?context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</(?:xbrli:)?context>", re.S)
_UNIT_RE = re.compile(r"<(?:xbrli:)?unit\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</(?:xbrli:)?unit>", re.S)
_MEASURE_RE = re.compile(r"<(?:xbrli:)?measure>\s*([^<\s]+)\s*</(?:xbrli:)?measure>")
_EXPLICIT_RE = re.compile(r"<xbrldi:explicitMember\b[^>]*dimension=\"([^\"]+)\"[^>]*>\s*([^<\s]+)\s*</xbrldi:explicitMember>")
_TYPED_RE = re.compile(r"<xbrldi:typedMember\b[^>]*dimension=\"([^\"]+)\"[^>]*>(.*?)</xbrldi:typedMember>", re.S)
_IX_RE = re.compile(r"<ix:nonFraction\b([^>]*?)(/>|>(.*?)</ix:nonFraction>)", re.S)
_ATTR_RE = re.compile(r'([\w:\-]+)="([^"]*)"')
_XML_FACT_RE = re.compile(r"<([\w\-]+):([\w\-.]+)\b([^>]*\bunitRef=\"[^\"]+\"[^>]*?)(?:/>|>([^<]*)</\1:\2>)")


def _tag_date(body: str, tag: str) -> date | None:
    m = re.search(rf"<(?:xbrli:)?{tag}>\s*([0-9\-]+)\s*</(?:xbrli:)?{tag}>", body)
    return to_date(m.group(1)) if m else None


def _ifrs_prefixes(text: str) -> set[str]:
    return {p for p, uri in _XMLNS_RE.findall(text[:20000]) if any(k in uri for k in IFRS_NS)} | {"ifrs-full"}


def _norm_qname(q: str, ifrs: set[str]) -> str:
    p, _, local = q.strip().partition(":")
    return f"ifrs-full:{local}" if local and p in ifrs else q.strip()


def parse_contexts(text: str, ifrs: set[str]) -> dict[str, tuple[date | None, date, dict[str, str]]]:
    out: dict[str, tuple[date | None, date, dict[str, str]]] = {}
    for cid, body in _CTX_RE.findall(text):
        inst = _tag_date(body, "instant")
        start, end = _tag_date(body, "startDate"), _tag_date(body, "endDate")
        if inst is not None:
            start, end = None, inst
        elif end is None:
            continue
        elif start == end:  # zero-length duration (equity-statement balances) == instant
            start = None
        dims = {_norm_qname(d, ifrs): _norm_qname(m, ifrs) for d, m in _EXPLICIT_RE.findall(body)}
        dims.update({_norm_qname(d, ifrs): re.sub(r"<[^>]+>|\s+", " ", v).strip() for d, v in _TYPED_RE.findall(body)})
        out[cid] = (start, end, dims)
    return out


def parse_units(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for uid, body in _UNIT_RE.findall(text):
        ms = [m.split(":", 1)[-1] for m in _MEASURE_RE.findall(body)]
        if not ms:
            continue
        ms = [UNIT_MAP.get(m, m) for m in ms]
        out[uid] = f"{ms[0]}/{ms[1]}" if "divide" in body and len(ms) >= 2 else ms[0]
    return out


def _decimals(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "", "INF") else None
    except ValueError:
        return None


def _fiscal_period(start: date | None, end: date) -> str | None:
    if start is None:
        return None
    months = round(((end - start).days + 1) / 30.44)
    if months == 3:
        return f"Q{(end.month - 1) // 3 + 1}"
    if months == 6:
        return "H1" if end.month <= 6 else "H2"
    if months == 9:
        return "9M"
    if months == 12:
        return "FY"
    return "YTD"


def iter_facts(content: bytes) -> list[dict[str, Any]]:
    """Numeric facts of a MOPS instance (inline XBRL .html or plain XBRL .xml) as
    {qname, start, end, dims, unit, value (scaled), decimals}. Text-level parsing: the files are big5/utf-8 mixed and
    the viewer prepends a non-XML <META> header, so no XML parser is used."""
    text = content.decode("utf-8", errors="replace")
    ifrs = _ifrs_prefixes(text)
    ctxs = parse_contexts(text, ifrs)
    units = parse_units(text)
    out: list[dict[str, Any]] = []
    inline = "ix:nonFraction" in text

    def add(qname: str, ctx: str, unit_ref: str, raw: str, scale: int, neg: bool, dec: str | None) -> None:
        c, u = ctxs.get(ctx), units.get(unit_ref)
        if c is None or u is None:
            return
        s = re.sub(r"<[^>]+>", "", raw).replace(",", "").replace(" ", "").strip()
        if not s or s in ("-", "－"):
            return
        try:
            v = float(s)
        except ValueError:
            return
        v = v * (10 ** scale) if scale else v
        if neg:
            v = -v
        if "PerShare" in qname and "/" not in u and u not in ("pure", "shares"):
            u = f"{u}/shares"  # 2013-era instances tag EPS with the plain currency unit
        out.append({"qname": _norm_qname(qname, ifrs), "start": c[0], "end": c[1], "dims": c[2], "unit": u,
                    "value": v, "decimals": _decimals(dec)})

    if inline:
        for attrs, closer, inner in _IX_RE.findall(text):
            a = dict(_ATTR_RE.findall(attrs))
            if closer == "/>" or a.get("xsi:nil") == "true":
                continue
            add(a.get("name", ""), a.get("contextRef", ""), a.get("unitRef", ""), inner,
                int(a.get("scale") or 0), a.get("sign") == "-", a.get("decimals"))
    else:
        for prefix, local, attrs, val in _XML_FACT_RE.findall(text):
            a = dict(_ATTR_RE.findall(attrs))
            if a.get("xsi:nil") == "true" or prefix in ("xbrli", "link", "xbrldi"):
                continue
            add(f"{prefix}:{local}", a.get("contextRef", ""), a.get("unitRef", ""), val or "", 0, False,
                a.get("decimals"))
    return out


def is_instance(content: bytes) -> bool:
    head = content[:400000]
    return b"contextRef" in head and (b"ix:header" in head or b"<xbrl" in head or b"xbrli:xbrl" in head)


def facts_to_rows(facts: list[dict[str, Any]], *, source_id: str, source_entity_id: str, filing_id: str,
                  form_type: str, knowledge_date: date, estimated: bool, consolidated: bool) -> list[dict[str, Any]]:
    rows = []
    for f in facts:
        if f["end"] > knowledge_date:
            continue
        q = f["qname"]
        taxonomy, concept = ("ifrs-full", q.split(":", 1)[1]) if q.startswith("ifrs-full:") else ("other", q)
        rows.append(fact_row(
            source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id, form_type=form_type,
            taxonomy=taxonomy, concept=concept, label=None, period_start=f["start"], period_end=f["end"],
            value=f["value"], unit=f["unit"], knowledge_date=knowledge_date, fiscal_year=f["end"].year,
            fiscal_period=_fiscal_period(f["start"], f["end"]), decimals=f["decimals"], dims=f["dims"] or None,
            consolidated=consolidated, knowledge_date_estimated=estimated))
    return rows


# --------------------------------------------------------------------------------------------- adapter


def co_id_of(entity: str) -> str:
    return entity.strip().upper().split(".", 1)[0]


@register_adapter
class MopsAdapter(SourceAdapter):
    source_id = "mops"
    regions = ("APAC",)
    produces = ("filings_index", "fundamentals_raw")
    base_url = BASE
    rate_limit = RateLimit(0.9, 1)
    cache_ttl = 0
    t57_pause = 3.0                    # extra spacing for doc.twse (throttles ~10 rapid queries)
    retry_pauses = (10.0, 30.0, 90.0)  # busy / throttled pages (HTTP 200) -> back off

    def __init__(self, *args: Any, seasons: str | Sequence[str] | None = None, bulk_fallback: bool = True,
                 refresh_pit: bool = False, **kw: Any):
        super().__init__(*args, **kw)
        try:  # mopsov / doc.twse expect a browser-like agent
            self.http.headers["User-Agent"] = UA
            self.http._client.headers["User-Agent"] = UA
        except AttributeError:
            pass
        self.seasons_param = seasons
        self.bulk_fallback = str(bulk_fallback).lower() not in ("0", "false", "no")
        self.refresh_pit = str(refresh_pit).lower() in ("1", "true", "yes")

    # ------------------------------------------------------------------ discover
    def discover(self) -> pl.DataFrame:
        seed = seed_universe(self.settings, countries=["TW"])
        rows = [{"source_entity_id": r["yf_ticker"], "name": r.get("name"), "country": "TW", "ticker": r.get("ticker"),
                 "mic": r.get("mic"), "yf_ticker": r["yf_ticker"], "isin": r.get("isin") or None, "is_active": True}
                for r in seed.iter_rows(named=True) if r.get("yf_ticker")]
        return self.discover_frame(rows, self.source_id)

    def _entities(self, entities: Sequence[str] | None) -> list[str]:
        if entities:
            return [e if "." in e else f"{e.strip()}.TW" for e in entities]
        seed = seed_universe(self.settings, countries=["TW"])
        return [t for t in seed.get_column("yf_ticker").to_list() if t] if seed.height else []

    def _seasons(self, since: Any, today: date) -> list[str]:
        p = parse_seasons_param(self.seasons_param, today)
        if p is not None:
            return p
        last = latest_season(today)
        d = to_date(since)
        if d is None:
            return season_range(FIRST_SEASON, last)
        start = d - timedelta(days=200)
        return [s for s in season_range(FIRST_SEASON, last) if season_end(s) >= start]

    # ------------------------------------------------------------------ paths
    def member_dir(self, season: str) -> Path:
        p = self.settings.raw_dir / self.source_id / "ixbrl" / season
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _land(self, season: str, name: str, content: bytes) -> Path:
        path = self.member_dir(season) / f"{name}.gz"
        tmp = path.with_suffix(".gz.tmp")
        tmp.write_bytes(gzip.compress(content, compresslevel=6))
        tmp.replace(path)
        return path

    # ------------------------------------------------------------------ HTTP pieces
    def t57_path(self, co: str, roc: int) -> Path:
        p = self.settings.raw_dir / self.source_id / "t57sb01" / f"{co}_{roc}.html.gz"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def upload_list(self, co: str, year: int, today: date) -> dict[str, dict[str, Any]]:
        """Landed page reused (past years forever, recent years for 12 h); doc.twse throttles bursts with a
        '查詢過量' page (HTTP 200), which is retried with backoff and never landed."""
        roc = year - 1911
        p = self.t57_path(co, roc)
        recent = year >= today.year - 1
        if p.exists() and not self.refresh_pit and (
                not recent or datetime.now(UTC).timestamp() - p.stat().st_mtime < 12 * 3600):
            return parse_upload_list(gzip.decompress(p.read_bytes()))
        for pause in (*self.retry_pauses, None):
            time.sleep(self.t57_pause)
            r = self.http.get(DOC_LIST, params={"step": "1", "co_id": co, "year": str(roc), "mtype": "A"}, cache_ttl=0)
            if r.status_code == 200 and T57_THROTTLE not in r.content and T57_OK in r.content:
                tmp = p.with_suffix(".gz.tmp")
                tmp.write_bytes(gzip.compress(r.content))
                tmp.replace(p)
                return parse_upload_list(r.content)
            if pause is None:
                break
            self.log.info("mops: t57sb01 %s/%s throttled; retry in %.0fs", co, roc, pause)
            time.sleep(pause)
        raise ValueError(f"t57sb01 {co}/{roc}: throttled or unexpected page (HTTP {r.status_code})")

    def _get_member(self, season: str, name: str, retries: int = 0) -> bytes | None:
        """MOPS answers a missing member AND a busy server with the same '伺服器忙碌中' page, so retries are only
        spent on names expected to exist (remembered pattern / season known from the upload list)."""
        y, _ = parse_season(season)
        for attempt in range(retries + 1):
            r = self.http.get("FileDownLoad", params={"step": "9", "functionName": "show_file2", "fileName": name,
                                                      "filePath": f"/ifrs/{y}/"}, cache_ttl=0, raise_for_status=False)
            if r.status_code == 200 and is_instance(r.content):
                return r.content
            if attempt < retries:
                time.sleep(self.retry_pauses[min(attempt, len(self.retry_pauses) - 1)])
        return None

    def _get_viewer(self, co: str, season: str, retries: int = 0) -> bytes | None:
        y, q = parse_season(season)
        for attempt in range(retries + 1):
            r = self.http.get("t164sb01", params={"step": "1", "CO_ID": co, "SYEAR": str(y), "SSEASON": str(q),
                                                  "REPORT_ID": "C"}, cache_ttl=0, raise_for_status=False)
            if r.status_code == 200 and b"ix:nonFraction" in r.content:
                i = r.content.find(b"<?xml")
                return r.content[i:] if i > 0 else r.content
            if attempt < retries:
                time.sleep(self.retry_pauses[min(attempt, len(self.retry_pauses) - 1)])
        return None

    def _candidates(self, co: str, season: str, remembered: str | None) -> list[str]:
        exts = ("html", "xml") if season >= IXBRL_FROM else ("xml", "html")
        names = [f"tifrs-{fr}-{m}-ci-cr-{co}-{season}.{exts[0]}" for fr, m in _PREFIX_CANDIDATES]
        names += [f"tifrs-{fr}-{m}-ci-cr-{co}-{season}.{exts[1]}" for fr, m in _PREFIX_CANDIDATES[:2]]
        if remembered:
            names = [f"{remembered}-{co}-{season}.{e}" for e in exts] + names
        return list(dict.fromkeys(names))

    def resolve_member(self, co: str, season: str, state: dict[str, Any], expected: bool = False
                       ) -> tuple[str, bytes] | None:
        """expected=True when the upload list shows a report for the season (retries busy pages harder)."""
        pats = state.setdefault("patterns", {})
        remembered = pats.get(co)
        cands = self._candidates(co, season, remembered)
        # 1) remembered pattern, preferred extension first (retried: busy pages are common)
        if remembered:
            for i, name in enumerate(cands[:2]):
                c = self._get_member(season, name, retries=(2 if expected else 1) if i == 0 else 0)
                if c:
                    return name, c
        # 2) per-company inline viewer (2019Q1 ->), consolidated report
        if season >= IXBRL_FROM:
            c = self._get_viewer(co, season, retries=2 if expected else 1)
            if c:
                return f"t164sb01-C-{co}-{season}.html", c
        # 3) candidate member names (commercial & industrial consolidated)
        for name in cands[2:] if remembered else cands:
            c = self._get_member(season, name)
            if c:
                m = _MEMBER_RE.match(name)
                if m:
                    pats[co] = f"tifrs-{m.group(1)}-{m.group(2)}-{m.group(3)}-{m.group(4)}"
                return name, c
        return None

    def bulk_extract(self, season: str, cos: set[str]) -> dict[str, tuple[str, bytes]]:
        """Stream the season zip to a temp file, return {co: (member, bytes)} preferring cr > ir > er, delete zip."""
        y, _ = parse_season(season)
        url = f"FileDownLoad?step=9&functionName=show_file2&fileName=tifrs-{season}.zip&filePath=/ifrs/{y}/"
        tmpdir = Path(tempfile.mkdtemp(prefix="mops_", dir=self.settings.cache_dir))
        dest = tmpdir / f"tifrs-{season}.zip"
        try:
            self.log.info("mops: bulk zip fallback %s for %s", season, sorted(cos))
            self.http.download(url, dest)
            zf = zipfile.ZipFile(dest)
            found: dict[str, tuple[int, str]] = {}
            for n in zf.namelist():
                m = _MEMBER_RE.match(Path(n).name)
                if not m or m.group(5) not in cos:
                    continue
                rank = {"cr": 0, "ir": 1, "er": 2}[m.group(4)]
                if m.group(5) not in found or rank < found[m.group(5)][0]:
                    found[m.group(5)] = (rank, n)
            out = {co: (Path(n).name, zf.read(n)) for co, (_, n) in found.items()}
            zf.close()
            return out
        finally:
            for p in tmpdir.glob("*"):
                p.unlink(missing_ok=True)
            tmpdir.rmdir()

    # ------------------------------------------------------------------ fetch
    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        done: dict[str, Any] = state.setdefault("done", {})
        today = datetime.now(UTC).date()
        ents = self._entities(entities)
        seasons = sorted(self._seasons(since, today), reverse=True)  # newest first
        res.meta.update({"seasons": seasons, "entities": ents})
        # PIT upload lists, one call per company-year
        pit: dict[str, dict[str, dict[str, Any]]] = {}
        pit_ok: set[tuple[str, int]] = set()
        for ent in ents:
            co = co_id_of(ent)
            pit[co] = {}
            for y in sorted({parse_season(s)[0] for s in seasons}, reverse=True):
                lst = safe_call(lambda co=co, y=y: self.upload_list(co, y, today), res, f"pit:{co}:{y}", self.log)
                if lst is not None:
                    pit_ok.add((co, y))
                pit[co].update(lst or {})
        res.meta["pit"] = {co: {s: {**v, "ts_local": v["ts_local"].isoformat()} for s, v in d.items()}
                           for co, d in pit.items()}
        need_bulk: dict[str, set[str]] = {}
        for season in seasons:
            for ent in ents:
                co = co_id_of(ent)
                key = f"{co}:{season}"
                rec = done.get(key) or {}
                if rec.get("path") and (self.settings.data_dir / rec["path"]).exists():
                    continue
                if rec.get("missing") and rec.get("pit_checked") and season_end(season) < today - timedelta(days=400):
                    continue

                def one(co=co, season=season, key=key) -> None:
                    expected = season in pit.get(co, {})
                    got = self.resolve_member(co, season, state, expected=expected)
                    if got is None:
                        if expected and self.bulk_fallback:
                            need_bulk.setdefault(season, set()).add(co)
                        else:  # final only when the upload list was read and shows no report
                            done[key] = {"missing": True, "checked": today.isoformat(),
                                         "pit_checked": (co, parse_season(season)[0]) in pit_ok}
                        return
                    name, content = got
                    p = self._land(season, name, content)
                    done[key] = {"path": str(p.relative_to(self.settings.data_dir)).replace("\\", "/"), "member": name,
                                 "fetched": today.isoformat()}
                    res.raw_paths.append(p)
                safe_call(one, res, key, self.log)
                save_state(self.source_id, state, self.settings)
        for season, cos in sorted(need_bulk.items(), reverse=True):
            def bulk(season=season, cos=cos) -> None:
                got = self.bulk_extract(season, cos)
                for co in cos:
                    key = f"{co}:{season}"
                    if co in got:
                        name, content = got[co]
                        p = self._land(season, name, content)
                        done[key] = {"path": str(p.relative_to(self.settings.data_dir)).replace("\\", "/"),
                                     "member": name, "fetched": today.isoformat(), "via": "bulk"}
                        res.raw_paths.append(p)
                    else:
                        done[key] = {"missing": True, "checked": today.isoformat(), "via": "bulk", "pit_checked": True}
            safe_call(bulk, res, f"bulk:{season}", self.log)
            save_state(self.source_id, state, self.settings)
        if not entities and self.seasons_param is None:
            res.watermark = today.isoformat()
            state["watermark"] = res.watermark
        save_state(self.source_id, state, self.settings)
        res.meta["done"] = {k: v for k, v in done.items()
                            if k.split(":")[1] in seasons and k.split(":")[0] in {co_id_of(e) for e in ents}}
        return res

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        today = datetime.now(UTC).date()
        ents = {co_id_of(e): e for e in fetched.meta.get("entities", [])}
        pit = fetched.meta.get("pit", {})
        fil: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        stats: dict[str, int] = {}
        n_real = n_est = 0
        for key, rec in sorted(fetched.meta.get("done", {}).items()):
            if not rec.get("path"):
                continue
            co, season = key.split(":")
            ent = ents.get(co, f"{co}.TW")
            path = self.settings.data_dir / rec["path"]
            if not path.exists():
                continue
            pe = season_end(season)
            q = parse_season(season)[1]
            annual = q == 4
            up = pit.get(co, {}).get(season)
            if up:
                ts_local = datetime.fromisoformat(up["ts_local"])
                kd, kts, est = ts_local.date(), (ts_local - TW_OFFSET).replace(tzinfo=UTC), False
                n_real += 1
            else:
                kd, kts, est = impute_knowledge_date(pe, annual, "APAC", today), None, True
                n_est += 1
            member = rec.get("member", "")
            mm = _MEMBER_RE.match(member)
            scope = mm.group(4) if mm else "cr"
            consolidated = scope == "cr"
            fid_native = f"{co}:{season}"
            fid = f"{self.source_id}:{fid_native}"
            form = "TIFRS-FY" if annual else f"TIFRS-Q{q}"
            try:
                content = gzip.decompress(path.read_bytes())
                rows = facts_to_rows(iter_facts(content), source_id=self.source_id, source_entity_id=ent,
                                     filing_id=fid, form_type=form, knowledge_date=kd, estimated=est,
                                     consolidated=consolidated)
            except Exception as e:  # noqa: BLE001
                fetched.entities_failed[f"parse:{key}"] = f"{type(e).__name__}: {e}"[:300]
                continue
            facts += rows
            stats[season] = stats.get(season, 0) + len(rows)
            y = parse_season(season)[0]
            fil.append(filing_row(
                source_id=self.source_id, native_key=fid_native, source_entity_id=ent, form_type=form,
                form_class="annual" if annual else "quarterly", knowledge_date=kd, knowledge_ts=kts, period_end=pe,
                fiscal_year=pe.year, fiscal_period="FY" if annual else f"Q{q}", language="zh",
                url=f"{BASE}/t164sb01?step=1&CO_ID={co}&SYEAR={y}&SSEASON={q}&REPORT_ID=C",
                doc_path=rec["path"], has_xbrl=True,
                items=[f"member={member}", f"scope={scope}",
                       f"upload_file={up['file']}" if up else "upload_file=",
                       "knowledge=t57sb01" if up else "knowledge=imputed"]))
        fetched.meta["rows_per_season"] = stats
        fetched.meta["pit_coverage"] = {"real": n_real, "estimated": n_est}
        return {"filings_index": frame(fil, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def run(ctx: Any) -> Any:
    return run_adapter_job(MopsAdapter, ctx)
