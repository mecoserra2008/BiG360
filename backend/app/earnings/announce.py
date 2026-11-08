"""Official earnings-date sources beyond Yahoo / JPX (COVERAGE_V2 W1, 2026-10-31) -> earnings_announcements.

Every source is public, keyless and called politely (one client, >= 1 s between requests, raw answers landed under
data/raw/earnings/<source>/dt=YYYY-MM-DD/). Rows carry `event_kind`: `schedule` = a date announced IN ADVANCE
(company-announced upcoming release), `release` = the actual publication of results (official release time / date).

* hkex_bmn - HKEX "Board Meeting Notifications" (https://www.hkexnews.hk/reports/bmn/ebmn.htm -> www3), the
  exchange's consolidated list of board-meeting dates announced by listed issuers (date, stock code, purpose,
  period). Results purposes only ("RES" in the purpose). One request per run. -> schedule rows.
* cninfo - CNINFO (Shenzhen / Shanghai exchanges' designated disclosure site) periodic-report booking schedule
  ("预约披露", the JSON the cninfo.com.cn page itself calls: POST /new/information/getPrbookInfo, market=szsh = both
  exchanges): per A-share the first booked date, up to three changed dates and the actual disclosure date of the
  report for a period (Q1 / H1 / Q3 / FY). The latest period whose end has passed and the next one are fetched
  (~60 pages of 100). The site serves older periods only intermittently (504), so history comes from Yahoo.
  -> schedule rows (booked / changed date, future) + release rows (actual disclosure date, date only).
* fca_nsm - FCA National Storage Mechanism search (POST https://api.data.fca.org.uk/search?index=nsm-search, the
  JSON the NSM page uses; already used by app.ingestion.fca_nsm): RNS / other PIP announcements typed FR (final
  results), IR (half-year report), QRF / QRS / QRT (quarterly results) -> release rows at the publication minute;
  NOR (notice of results) -> the announced date is parsed from the notice text -> schedule rows. Matched to issuers
  by LEI (identifier_map). Incremental from a watermark (state data/state/earnings/announce.json); `nsm_since`
  for a back-fill (one search page = 500 hits).

Rejected (see docs/COVERAGE_V2_PLAN.md W1): api.nasdaq.com earnings calendar - robots.txt `Disallow: /` for the
whole API host; NYSE - no public earnings calendar; TDnet (www.release.tdnet.info) - robots.txt `Disallow: /`, so no
new TDnet requests are made here (the JPX schedule workbooks on jpx.co.jp are allowed and already used).
"""

from __future__ import annotations

import gzip
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings
from app.core.storage import scan_table
from app.earnings import common as C

log = C.log
UA = "Mozilla/5.0 (BiG Equity 360 research; local)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
HKEX_BMN = "https://www.hkexnews.hk/reports/bmn/ebmn.htm"
CNINFO_PRBOOK = "https://www.cninfo.com.cn/new/information/getPrbookInfo"
NSM_SEARCH = "https://api.data.fca.org.uk/search?index=nsm-search"
NSM_ARTEFACTS = "https://data.fca.org.uk/artefacts/"
NSM_RESULT_TYPES = ("FR", "IR", "QRF", "QRS", "QRT")
NSM_NOTICE_TYPES = ("NOR",)
STATE_FILE = "earnings/announce.json"
COLS = ("source_id", "security_id", "issuer_id", "event_date", "event_ts", "time_known", "period_hint",
        "provider_symbol", "knowledge_date", "fetched_at", "event_kind", "url")


def _state(s: Settings) -> dict[str, Any]:
    from app.ops.paths import read_json  # noqa: PLC0415

    return read_json(s.state_dir / STATE_FILE, {}) or {}


def _save_state(s: Settings, st: dict[str, Any]) -> None:
    from app.ops.paths import write_json_atomic  # noqa: PLC0415

    write_json_atomic(s.state_dir / STATE_FILE, st)


def _land(s: Settings, source: str, name: str, content: bytes) -> None:
    p = s.raw_dir / "earnings" / source / f"dt={C.now_utc().date().isoformat()}"
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_bytes(content)


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame([{k: r.get(k) for k in COLS} for r in rows], infer_schema_length=None)
    # one row per (source, security, date): the actual release wins over an announced date of the same day
    return (df.with_columns((pl.col("event_kind") == "release").fill_null(False).alias("_rel")).sort("_rel")
            .unique(["source_id", "security_id", "event_date"], keep="last", maintain_order=True).drop("_rel"))


# ------------------------------------------------------------------------------------------------ HKEX BMN

_DMY = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
_YEAR = re.compile(r"(FIRST|SECOND|THIRD|1ST|2ND|3RD)\s+QUARTER\s+(\d{4})")


def _dmy(m: re.Match[str]) -> date | None:
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    y = y + 2000 if y < 100 else y
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def bmn_period_end(period: str, bm: date) -> date | None:
    """'QTR ENDED30/09/26' / '6-MTH-ENDED31/08/26' / 'Y.E.31/07/26' -> the period end; 'THIRD QUARTER 2026' -> the
    calendar quarter end; else None (period assigned from the cadence)."""
    m = _DMY.search(period or "")
    if m:
        pe = _dmy(m)
        return pe if pe is not None and pe < bm else None
    m = _YEAR.search((period or "").upper())
    if m:
        q = {"FIRST": 1, "1ST": 1, "SECOND": 2, "2ND": 2, "THIRD": 3, "3RD": 3}[m.group(1)]
        return C.month_end(int(m.group(2)), 3 * q)
    return None


def parse_bmn(html: str) -> list[dict[str, Any]]:
    """Rows [{code, bm_date, purpose, period, period_end}] of the BMN table (results purposes only)."""
    from bs4 import BeautifulSoup  # noqa: PLC0415

    soup = BeautifulSoup(html, "html.parser")
    out = []
    for tr in soup.find_all("tr"):
        tds = [td.get_text(" ", strip=True).replace("\xa0", " ").strip() for td in tr.find_all("td")]
        if len(tds) < 5:
            continue
        m = _DMY.fullmatch(tds[0])
        if not m:
            continue
        bm = _dmy(m)
        code = re.sub(r"\D", "", tds[3])
        purpose, period = tds[4].upper(), (tds[5] if len(tds) > 5 else "")
        if bm is None or not code or "RES" not in purpose:
            continue
        out.append({"code": code.zfill(4), "bm_date": bm, "purpose": purpose, "period": period,
                    "period_end": bmn_period_end(period, bm)})
    return out


def fetch_hkex_bmn(s: Settings, meta: pl.DataFrame, client: Any) -> pl.DataFrame:
    r = client.get(HKEX_BMN, headers={"User-Agent": UA})
    r.raise_for_status()
    _land(s, "hkex_bmn", f"ebmn_{time.time_ns()}.htm", r.content)
    rows = parse_bmn(r.text)
    hk = (meta.filter(pl.col("mic") == "XHKG").drop_nulls("local_symbol")
          .with_columns(pl.col("local_symbol").str.zfill(4).alias("_code")).unique("_code"))
    sid = {c: (a, b) for c, a, b in hk.select("_code", "security_id", "issuer_id").iter_rows()}
    now = C.now_utc()
    out = []
    for x in rows:
        if x["code"] not in sid:
            continue
        sec, iss = sid[x["code"]]
        hint = (f"PE {x['period_end'].isoformat()} | " if x["period_end"] else "") + f"{x['purpose']} {x['period']}".strip()
        out.append({"source_id": "hkex_bmn", "security_id": sec, "issuer_id": iss, "event_date": x["bm_date"],
                    "event_ts": None, "time_known": False, "period_hint": hint[:200], "provider_symbol": x["code"],
                    "knowledge_date": now.date(), "fetched_at": now, "event_kind": "schedule",
                    "url": "https://www.hkexnews.hk/reports/bmn/ebmn.htm"})
    log.info("HKEX BMN: %d results rows, %d matched", len(rows), len(out))
    return _frame(out)


# ------------------------------------------------------------------------------------------------ CNINFO

def cninfo_sections(today: date) -> list[date]:
    """The latest report period whose end has passed and the next one (quarter ends)."""
    cur = C.calendar_quarter_end_before(today, 0)
    nxt = C.add_months_end(cur, 3)
    return [cur, nxt]


def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse_cninfo(rows: list[dict[str, Any]], section: date) -> list[dict[str, Any]]:
    """-> [{code, booked, actual, period_end}] (booked = the latest changed date, else the first booking)."""
    out = []
    for r in rows:
        code = str(r.get("seccode") or "").strip()
        if not code:
            continue
        changes = [_d(r.get(k)) for k in ("f005d_0102", "f004d_0102", "f003d_0102", "f002d_0102")]
        booked = next((x for x in changes if x), None)
        out.append({"code": code, "booked": booked, "actual": _d(r.get("f006d_0102")),
                    "period_end": _d(r.get("f001d_0102")) or section})
    return out


def fetch_cninfo(s: Settings, meta: pl.DataFrame, client: Any, sections: list[date] | None = None,
                 page_size: int = 100, max_pages: int = 120, pause: float = 1.0) -> pl.DataFrame:
    today = C.now_utc().date()
    cn = (meta.filter(pl.col("mic").is_in(["XSHG", "XSHE", "BJSE", "XBSE"])).drop_nulls("local_symbol")
          .unique("local_symbol"))
    sid = {c: (a, b) for c, a, b in cn.select("local_symbol", "security_id", "issuer_id").iter_rows()}
    now = C.now_utc()
    out: list[dict[str, Any]] = []
    for sec in sections or cninfo_sections(today):
        page, raw = 1, []
        while page <= max_pages:
            form = {"sectionTime": sec.isoformat(), "firstTime": "", "lastTime": "", "market": "szsh",
                    "stockCode": "", "orderClos": "", "isDesc": "", "pagesize": str(page_size), "pagenum": str(page)}
            r = None
            for attempt in range(3):
                r = client.post(CNINFO_PRBOOK, data=form, headers={"User-Agent": UA})
                if r.status_code == 200:
                    break
                time.sleep(5 * (attempt + 1))
            if r is None or r.status_code != 200:
                log.warning("CNINFO %s page %d: HTTP %s; section stopped", sec, page, getattr(r, "status_code", None))
                break
            j = r.json()
            items = j.get("prbookinfos") or []
            raw += items
            if not j.get("hasNextPage") or not items:
                break
            page += 1
            time.sleep(pause)
        if raw:
            _land(s, "cninfo", f"prbook_{sec.isoformat()}_{time.time_ns()}.json", orjson.dumps(raw))
        for x in parse_cninfo(raw, sec):
            if x["code"] not in sid:
                continue
            secid, iss = sid[x["code"]]
            base = {"source_id": "cninfo", "security_id": secid, "issuer_id": iss, "event_ts": None,
                    "time_known": False, "period_hint": f"PE {x['period_end'].isoformat()}", "provider_symbol": x["code"],
                    "knowledge_date": now.date(), "fetched_at": now,
                    "url": "https://www.cninfo.com.cn/new/commonUrl?url=data/yypl"}
            if x["actual"] is not None and x["actual"] <= today:
                out.append(base | {"event_date": x["actual"], "event_kind": "release"})
            elif x["booked"] is not None and x["booked"] >= today - timedelta(days=3):
                out.append(base | {"event_date": x["booked"], "event_kind": "schedule"})
        log.info("CNINFO section %s: %d rows", sec, len(raw))
    return _frame(out)


# ------------------------------------------------------------------------------------------------ FCA NSM

_MONTHS = {m: i + 1 for i, m in enumerate(("january", "february", "march", "april", "may", "june", "july", "august",
                                           "september", "october", "november", "december"))}
_MON = "(" + "|".join(_MONTHS) + ")"
_DATE_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MON + r",?\s+(\d{4})\b", re.IGNORECASE)
_PERIOD_CTX = re.compile(r"(ended|ending|end of|period to|year to|months to|weeks to|as at|as of)\s*(\w+\s*){0,2}$")
_DATE_MDY = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.IGNORECASE)


def notice_date(text: str, published: date, horizon_days: int = 150) -> date | None:
    """The announced results date in a 'Notice of Results' text: the first date after the publication day within
    `horizon_days` (a notice names the release date first; later dates are calls / AGMs)."""
    found: list[tuple[int, date]] = []
    for rx, order in ((_DATE_DMY, "dmy"), (_DATE_MDY, "mdy")):
        for m in rx.finditer(text or ""):
            try:
                if order == "dmy":
                    d = date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
                else:
                    d = date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
            except ValueError:
                continue
            before = (text or "")[max(0, m.start() - 40): m.start()].lower()
            # a period end ("six months ended 31 October 2026", "year to ...") is not the release date; results are
            # not released on weekends
            if _PERIOD_CTX.search(before) or d.weekday() >= 5:
                continue
            if published < d <= published + timedelta(days=horizon_days):
                found.append((m.start(), d))
    return min(found)[1] if found else None


def nsm_search(client: Any, types: tuple[str, ...], since: date, until: date, size: int = 500,
               max_hits: int = 9500, pause: float = 1.0) -> list[dict[str, Any]]:
    crit = [{"name": "latest_flag", "value": "Y"}, {"name": "type_code", "value": list(types)}]
    dc = [{"name": "submitted_date", "value": {"from": f"{since.isoformat()}T00:00:00Z",
                                               "to": f"{until.isoformat()}T23:59:59Z"}}]
    hits: list[dict[str, Any]] = []
    frm = 0
    while frm < max_hits:
        body = {"from": frm, "size": size, "sort": "submitted_date", "sortorder": "desc",
                "criteriaObj": {"criteria": crit, "dateCriteria": dc}}
        r = client.post(NSM_SEARCH, json=body, headers={"User-Agent": BROWSER_UA})
        r.raise_for_status()
        j = r.json()
        hh = ((j.get("hits") or {}).get("hits")) if isinstance(j, dict) else None
        if hh is None:
            raise ValueError(f"NSM schema change: keys={list(j)[:5] if isinstance(j, dict) else type(j)}")
        hits += [h.get("_source") or {} for h in hh]
        if len(hh) < size:
            break
        frm += size
        time.sleep(pause)
    return hits


def _lei_map(s: Settings) -> dict[str, str]:
    try:
        im = (scan_table("identifier_map", s).filter((pl.col("id_type") == "lei") & pl.col("valid_to").is_null())
              .select("id_value", "issuer_id").unique("id_value").collect())
    except Exception:  # noqa: BLE001
        return {}
    return {str(a).upper(): b for a, b in im.iter_rows()}


def fetch_nsm(s: Settings, meta: pl.DataFrame, client: Any, since: date | None = None,
              max_notices: int = 200, pause: float = 1.0) -> tuple[pl.DataFrame, dict[str, Any]]:
    today = C.now_utc().date()
    st = _state(s)
    nst = st.get("fca_nsm") or {}
    wm = _d(nst.get("watermark"))
    back = _d(nst.get("backfilled_from"))
    if since is not None and back is not None and back <= since:
        since = None  # that back-fill is done: incremental from the watermark (the weekly campaign re-runs cheaply)
    backfill_from = since
    since = since or ((wm - timedelta(days=2)) if wm else today - timedelta(days=60))
    leis = _lei_map(s)
    sec_of = dict(meta.select("issuer_id", "security_id").drop_nulls().iter_rows())
    now = C.now_utc()
    rep: dict[str, Any] = {"since": since.isoformat()}
    # windows of <= 120 days keep every search under the index's 10k result window (deep paging is refused)
    res: list[dict[str, Any]] = []
    a = since
    while a <= today:
        b = min(today, a + timedelta(days=120))
        res += nsm_search(client, NSM_RESULT_TYPES, a, b, pause=pause, max_hits=9500)
        a = b + timedelta(days=1)
    nor = nsm_search(client, NSM_NOTICE_TYPES, max(since, today - timedelta(days=150)), today, pause=pause)
    _land(s, "fca_nsm", f"search_{time.time_ns()}.json.gz", gzip.compress(orjson.dumps(res + nor)))
    rep["results_hits"], rep["notice_hits"] = len(res), len(nor)
    out: list[dict[str, Any]] = []
    for h in res:
        iss = leis.get(str(h.get("lei") or "").upper())
        if not iss or not sec_of.get(iss) or not h.get("publication_date"):
            continue
        ts = datetime.fromisoformat(str(h["publication_date"]).replace("Z", "+00:00")).astimezone(UTC)
        out.append({"source_id": "fca_nsm", "security_id": sec_of[iss], "issuer_id": iss,
                    "event_date": ts.date(), "event_ts": ts, "time_known": True,
                    "period_hint": f"{h.get('type_code')} | {(h.get('headline') or '').strip()}"[:200],
                    "provider_symbol": h.get("lei"), "knowledge_date": ts.date(), "fetched_at": now,
                    "event_kind": "release",
                    "url": NSM_ARTEFACTS + h["download_link"] if h.get("download_link") else None})
    rep["results_matched"] = len(out)
    n_doc = 0
    for h in sorted(nor, key=lambda x: str(x.get("publication_date")), reverse=True):
        iss = leis.get(str(h.get("lei") or "").upper())
        if not iss or not sec_of.get(iss) or not h.get("download_link") or n_doc >= max_notices:
            continue
        pub = datetime.fromisoformat(str(h["publication_date"]).replace("Z", "+00:00")).astimezone(UTC)
        try:
            time.sleep(pause)
            r = client.get(NSM_ARTEFACTS + h["download_link"], headers={"User-Agent": BROWSER_UA})
            n_doc += 1
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001 - one notice must not stop the run
            log.warning("NSM notice %s: %s", h.get("download_link"), e)
            continue
        from bs4 import BeautifulSoup  # noqa: PLC0415

        text = (h.get("headline") or "") + " " + BeautifulSoup(r.text, "html.parser").get_text(" ")
        d = notice_date(text, pub.date())
        if d is None:
            continue
        out.append({"source_id": "fca_nsm", "security_id": sec_of[iss], "issuer_id": iss, "event_date": d,
                    "event_ts": None, "time_known": False,
                    "period_hint": f"NOR | {(h.get('headline') or '').strip()}"[:200], "provider_symbol": h.get("lei"),
                    "knowledge_date": pub.date(), "fetched_at": now, "event_kind": "schedule",
                    "url": NSM_ARTEFACTS + h["download_link"]})
    rep["notices_fetched"] = n_doc
    rep["rows"] = len(out)
    pubs = [str(h.get("publication_date"))[:10] for h in res if h.get("publication_date")]
    if pubs:
        st["fca_nsm"] = {"watermark": max(pubs), "at": now.isoformat(timespec="seconds"),
                         "backfilled_from": min(x for x in (back, backfill_from) if x is not None).isoformat()
                         if (back or backfill_from) else None}
        _save_state(s, st)
    return _frame(out), rep


# ------------------------------------------------------------------------------------------------ entry


def fetch_all(s: Settings, meta: pl.DataFrame, p: dict[str, Any]) -> tuple[list[pl.DataFrame], dict[str, Any]]:
    """Run the enabled sources (params hkex / cninfo / nsm, default true; nsm_since = back-fill start)."""
    import httpx  # noqa: PLC0415

    def on(k: str) -> bool:
        return str(p.get(k, "true")).lower() not in ("0", "false", "no")

    frames: list[pl.DataFrame] = []
    rep: dict[str, Any] = {}
    from app.core.robots_policy import httpx_request_hook  # noqa: PLC0415 - A9 robots deny list

    with httpx.Client(timeout=90, follow_redirects=True, event_hooks={"request": [httpx_request_hook]}) as cl:
        for key, fn in (("hkex", lambda: (fetch_hkex_bmn(s, meta, cl), {})),
                        ("cninfo", lambda: (fetch_cninfo(s, meta, cl), {})),
                        ("nsm", lambda: fetch_nsm(s, meta, cl, _d(p.get("nsm_since"))))):
            if not on(key):
                continue
            try:
                df, r = fn()
                rep[key] = r | {"rows": df.height}
                if df.height:
                    frames.append(df)
            except Exception as e:  # noqa: BLE001 - optional source: the run continues
                log.warning("earnings announcements %s failed: %s", key, e)
                rep[key] = {"error": f"{type(e).__name__}: {e}"[:200]}
    return frames, rep
