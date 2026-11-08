"""Non-EU national macro sources -> macro_series + macro_observations. Job `macro_national`.

Series + request parameters live in contracts/macro_catalog.yaml (src = fetcher name below). Keyed sources are
skipped with a "missing key" status when the optional key is absent (BOK_ECOS_APIKEY, BANXICO_TOKEN, ESTAT_APPID,
KOSIS_APIKEY, INEGI_TOKEN, BCCH_USER/BCCH_PASS).
"""

from __future__ import annotations

import csv
import io
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.http import HttpError
from app.ingestion.macro_common import (
    GSeries,
    MacroRunner,
    dedupe_pairs,
    fetcher,
    parse_bcb,
    parse_boj_multi,
    parse_period,
    run_job,
    to_float,
)

SOURCES = ["ust", "statcan", "boc", "bcb", "ibge", "banxico", "inegi", "ine_cl", "bcch", "banrep", "bcrp", "bcra",
           "datos_ar", "boe", "ons", "boj", "mof_jp", "estat_jp", "bok_ecos", "akshare_cn", "hkma", "censtatd", "rba",
           "abs", "cbc_tw", "nyfed", "nyfed_rates"]


# ------------------------------------------------------------------------------------------------ UK


# A9 2026-11-07 (docs/COVERAGE_V3_PLAN.md, docs/it/ROBOTS_COMPLIANCE.md): www.bankofengland.co.uk robots.txt disallows
# the IADB (/boeapps/database/_iadb-FromShowColumns.asp, /boeapps/iadb; app.core.robots_policy blocks it). Every
# boe series now comes from an allowed official file, keeping its IADB series id and history; overlap verified
# 2026-11-07 against the stored IADB values: IUDMNZC 9,276 / 9,287 days identical at 4 dp (the 11 others are 2019
# days where the IADB repeated a stale 1.0364), IUDBEDR 7,435 / 7,435 days since 1997-06-06, IUDSOIA 7,541 / 7,541.
BOE_MEDIA = "https://www.bankofengland.co.uk/-/media/boe/files/"
BOE_YC_LATEST = BOE_MEDIA + "statistics/yield-curves/latest-yield-curve-data.zip"
BOE_YC_ARCHIVE = BOE_MEDIA + "statistics/yield-curves/glcnominalddata.zip"
BOE_MPC_VOTES = BOE_MEDIA + "monetary-policy-summary-and-minutes/mpcvoting.xlsx"
BOE_BIS_GB = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_CBPOL/1.0/D.GB"
FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"
#: IADB code -> (route, argument). yc_spot: BoE fitted nominal spot (zero-coupon) curve at <months> (the IADB
#: IUD*NZC series are this curve); bank_rate: MPC decision record (mpcvoting.xlsx, 1997-06-06 ->) as a step series on
#: London business days, BIS WS_CBPOL D.GB before the first MPC decision; fred: FRED's republication of the BoE series.
BOE_ROUTES: dict[str, tuple[str, Any]] = {
    "IUDMNZC": ("yc_spot", 120), "IUDSNZC": ("yc_spot", 60), "IUDLNZC": ("yc_spot", 240),
    "IUDBEDR": ("bank_rate", None),
    "IUDSOIA": ("fred", "IUDSOIA"),
}
#: IADB-only series (par yields IUDSNPY / IUDMNPY / IUDLNPY, Bankstats LPMVQJW / LPMBC57 / LPMVTVX / IUMBV34): par yields
#: re-derived from the published spot curve differ from the IADB by up to ~20 bp and the Bankstats codes are in no
#: allowed file -> not fetched, history kept. contracts/macro_catalog.yaml: par yields under `retired` (the zero-coupon
#: series of the same tenor replace them over the full history); Bankstats under `frozen` (no replacement: still
#: served with their stored history, app.ingestion.macro_common.freeze_series labels the title)
BOE_FROZEN_NOTE = ("source_disallowed: IADB-only series (BoE robots.txt disallows the IADB, checked 2026-11-07), no "
                   "allowed source with matching values -> stored history kept, not updated")


def xlon_sessions(start: date, end: date) -> list[date]:
    """London Stock Exchange sessions in [start, end] (the BoE business-day calendar of the IADB daily series);
    weekdays when exchange_calendars is unavailable."""
    try:
        import exchange_calendars as xc  # noqa: PLC0415

        cal = xc.get_calendar("XLON", start=min(start, date(1990, 1, 1)).isoformat())
        # clamp to the calendar's bounds: sessions_in_range raises for a start before the first session (1990-01-01
        # is a holiday), which used to drop a full-history run to the weekday fallback (bank holidays included)
        lo = max(start, cal.first_session.date())
        hi = min(end, cal.last_session.date())
        return [d.date() for d in cal.sessions_in_range(lo.isoformat(), hi.isoformat())] if lo <= hi else []
    except Exception:  # noqa: BLE001 - calendar unavailable: weekdays
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [d for d in days if d.weekday() < 5]


def bank_rate_daily(decisions: list[tuple[date, float]], before: list[tuple[date, float]], start: date,
                    end: date) -> list[tuple[date, float]]:
    """Bank Rate in force on each London business day (XLON sessions = the IADB IUDBEDR calendar, verified
    1997-2026): last MPC decision <= day; days before the first decision use the `before` step series (BIS)."""
    import bisect  # noqa: PLC0415

    days = xlon_sessions(start, end)
    dec = sorted(decisions)
    dd = [d for d, _ in dec]
    bf = sorted(before)
    bd = [d for d, _ in bf]
    out: list[tuple[date, float]] = []
    for d in days:
        if d > end:
            break
        if dd and d >= dd[0]:
            out.append((d, dec[bisect.bisect_right(dd, d) - 1][1]))
        else:
            i = bisect.bisect_right(bd, d)
            if i:
                out.append((d, bf[i - 1][1]))
    return out


def _boe_curve_from_zip(content: Any, months: tuple[int, ...], min_year: int | None = None) -> dict[date, dict[int, float]]:
    """BoE yield-curve zip (latest-yield-curve-data.zip or glcnominalddata.zip; bytes or a file object) -> the
    nominal spot curve {date: {months: percent}} of the GLC nominal workbooks."""
    import zipfile  # noqa: PLC0415

    from app.rates.sources import parse_boe_workbook  # noqa: PLC0415

    z = zipfile.ZipFile(io.BytesIO(content) if isinstance(content, bytes) else content)
    out: dict[date, dict[int, float]] = {}
    for n in z.namelist():
        low = n.lower()
        if not low.startswith("glc nominal"):
            continue
        if min_year is not None:
            yrs = [int(y) for y in re.findall(r"(19\d\d|20\d\d)", n)] + ([datetime.now(UTC).year] if "present" in low else [])
            if yrs and max(yrs) < min_year:
                continue
        for d, row in (parse_boe_workbook(z.read(n), months).get("spot") or {}).items():
            out.setdefault(d, {}).update(row)
    return out


def _boe_code(sp: GSeries) -> str:
    return str(sp.req.get("code") or sp.native)


#: holes are looked for over this many days before the first day of the current-month workbook (the month boundary:
#: the latest zip switches to the new month about a day after month end, the archive is updated early each month)
BOE_YC_GAP_WINDOW_DAYS = 62


def _boe_stored_dates(r: MacroRunner, ids: list[str], lo: date) -> dict[str, set[date]] | None:
    """Stored observation dates >= lo per series (macro_observations, boe partition); None if unreadable."""
    import polars as pl  # noqa: PLC0415

    if not ids:
        return {}
    try:
        from app.core.storage import table_dir  # noqa: PLC0415

        d = table_dir("macro_observations", r.s) / "source_id=boe"
        files = [str(f) for f in d.glob("*.parquet")] if d.exists() else []
        out: dict[str, set[date]] = {i: set() for i in ids}
        if not files:
            return out
        df = (pl.scan_parquet(files).filter(pl.col("series_id").is_in(ids) & (pl.col("date") >= lo)
                                            & pl.col("value").is_not_null())
              .select("series_id", "date").unique().collect())
        for sid, dt in df.iter_rows():
            out[sid].add(dt)
        return out
    except Exception as e:  # noqa: BLE001
        from app.core.logsetup import get_logger  # noqa: PLC0415

        get_logger(__name__).warning("boe: stored dates unreadable (%s); falling back to the last-observation check", e)
        return None


def _boe_yield_curve(r: MacroRunner, sub: list[GSeries]) -> None:
    starts = {sp.series_id: r.since_for([sp]) for sp in sub}
    months = tuple(sorted({int(BOE_ROUTES[_boe_code(sp)][1]) for sp in sub}))
    latest = _boe_curve_from_zip(r.http("boe").get(BOE_YC_LATEST, cache_ttl=3 * 3600).content, months)
    first_latest = min(latest) if latest else r.today
    last = r.last_obs()
    # The archive is read for new series and for any London session missing from the stored rows in the window before
    # the current-month workbook (nightly runs otherwise need the current workbook only). Missing sessions, not a
    # calendar tolerance on the max(date) watermark: a month-end day missed while it sat in the latest zip (~24 h), or
    # absent from an archive copy that predates the BoE's monthly update, is retried every night until it is filled.
    lo = first_latest - timedelta(days=BOE_YC_GAP_WINDOW_DAYS)
    sessions = xlon_sessions(lo, first_latest - timedelta(days=1))
    known = [sp.series_id for sp in sub if not r.full and last.get(sp.series_id) is not None]
    stored = _boe_stored_dates(r, known, lo)
    need: list[GSeries] = []
    gap_end: date | None = None
    for sp in sub:
        sid = sp.series_id
        if sid not in known:
            need.append(sp)
            continue
        have = None if stored is None else stored.get(sid)
        miss = ([d for d in sessions if d > last[sid]] if have is None
                else [d for d in sessions if d >= starts[sid] and d not in have])
        if miss:
            need.append(sp)
            gap_end = max(gap_end or miss[-1], miss[-1])
            r.report.setdefault("boe_yc_gaps", {})[sid] = f"{len(miss)} sessions {miss[0]}..{miss[-1]}"
    curve: dict[date, dict[int, float]] = {}
    if need:
        dest = r.s.raw_dir / "rates" / "boe" / "glcnominalddata.zip"   # shared with app.rates.sources (W3)
        newest = max((last.get(sp.series_id) or r.start) for sp in need)
        mtime = datetime.fromtimestamp(dest.stat().st_mtime, UTC).date() if dest.exists() else None

        def download() -> date:
            dest.parent.mkdir(parents=True, exist_ok=True)
            r.http("boe_yc_archive", timeout=600).download(BOE_YC_ARCHIVE, dest)
            return r.today

        # refresh (<= weekly) only when the copy on disk may end before the gap starts
        if mtime is None or ((r.today - mtime).days > 8 and newest > mtime - timedelta(days=40)):
            mtime = download()
        min_year = min(starts[sp.series_id] for sp in need).year
        with open(dest, "rb") as fh:
            curve = _boe_curve_from_zip(fh, months, min_year=min_year)
        # the copy ends before the newest missing session (it predates the BoE's monthly archive update): fetch it
        # again whatever its age, at most once a day
        if gap_end and mtime < r.today and (max(curve) if curve else date.min) < gap_end:
            mtime = download()
            with open(dest, "rb") as fh:
                curve = _boe_curve_from_zip(fh, months, min_year=min_year)
    for d, row in latest.items():   # the current workbook wins (latest estimates)
        curve.setdefault(d, {}).update(row)
    for sp in sub:
        m = int(BOE_ROUTES[_boe_code(sp)][1])
        pairs = [(d, round(row[m], 4)) for d, row in sorted(curve.items()) if m in row and d >= starts[sp.series_id]]
        if pairs:
            r.add(sp, pairs)
        else:
            r.fail(sp, f"BoE yield-curve files: no {m}M nominal spot rate since {starts[sp.series_id]}")


def _boe_bank_rate(r: MacroRunner, sub: list[GSeries]) -> None:
    from app.macro.cb.rates import parse_bis_csv, parse_boe_votes  # noqa: PLC0415

    start = min(r.since_for([sp]) for sp in sub)
    votes = parse_boe_votes(r.http("boe").get(BOE_MPC_VOTES, cache_ttl=6 * 3600).content)
    dec = sorted({v["date"]: float(v["rate"]) for v in votes}.items())
    if not dec:
        raise ValueError("mpcvoting.xlsx: no Bank Rate decisions parsed")
    before: list[tuple[date, float]] = []
    if start < dec[0][0]:
        txt = r.http("bis_cbpol").get(BOE_BIS_GB, {"startPeriod": start.isoformat(), "format": "csv"},
                                      cache_ttl=12 * 3600).text
        before = [(d, v) for d, v in parse_bis_csv(txt).get("GB", []) if d < dec[0][0]]
    pairs = bank_rate_daily(dec, before, start, r.today)
    for sp in sub:
        if pairs:
            r.add(sp, pairs)
        else:
            r.fail(sp, "no Bank Rate observations (MPC voting record / BIS)")


def _boe_fred(r: MacroRunner, sub: list[GSeries]) -> None:
    key = r.s.secret("fred_apikey")
    for sp in sub:
        if not key:
            r.fail(sp, "missing key FRED_APIKEY (BoE series via FRED; the IADB is robots-disallowed)")
            continue
        fid = str(BOE_ROUTES[_boe_code(sp)][1])
        j = r.http("fred").get(FRED_OBS, {"series_id": fid, "api_key": key, "file_type": "json", "limit": 100000,
                                          "observation_start": r.since_for([sp]).isoformat()}, cache_ttl=6 * 3600).json()
        pairs = [(date.fromisoformat(o["date"]), to_float(o.get("value"))) for o in j.get("observations") or []]
        pairs = [(d, v) for d, v in pairs if v is not None]
        if pairs:
            r.add(sp, pairs)
        else:
            r.fail(sp, f"FRED {fid}: no observations")


@fetcher("boe")
def fetch_boe(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: code (the IADB series code, kept as the series id). Routed by BOE_ROUTES to allowed official files (no
    IADB request); IADB-only codes are frozen (reported under skipped_series with reason source_disallowed)."""
    routed: dict[str, list[GSeries]] = {}
    for sp in specs:
        routed.setdefault(BOE_ROUTES.get(_boe_code(sp), ("frozen", None))[0], []).append(sp)
    for sp in routed.pop("frozen", []):
        r.report.setdefault("skipped_series", {})[sp.series_id] = BOE_FROZEN_NOTE
    for route, fn in (("yc_spot", _boe_yield_curve), ("bank_rate", _boe_bank_rate), ("fred", _boe_fred)):
        sub = routed.get(route) or []
        if sub:
            r.attempt(f"boe:{route}", lambda fn=fn, sub=sub: fn(r, sub), sub)


# ------------------------------------------------------------------------------------------------ Japan


@fetcher("boj")
def fetch_boj(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: db, code. One request per db (<= 250 codes, NEXTPOSITION paging)."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["db"]), []).append(sp)
    for db, grp in groups.items():
        def one(db: str = db, grp: list[GSeries] = grp) -> None:
            freqs = {sp.frequency for sp in grp}
            data: dict[str, list[tuple[date, float | None]]] = {}
            for fq in freqs:  # BoJ requires one frequency per request
                sub = [sp for sp in grp if sp.frequency == fq]
                params: dict[str, Any] = {"format": "json", "lang": "en", "db": db,
                                          "code": ",".join(sorted({str(sp.req["code"]) for sp in sub})),
                                          "startDate": boj_start(r.start, fq)}
                for _ in range(40):
                    j = r.http("boj").get("https://www.stat-search.boj.or.jp/api/v1/getDataCode", params,
                                          cache_ttl=6 * 3600).json()
                    for k, v in parse_boj_multi(j).items():
                        data.setdefault(k, []).extend(v)
                    nxt = j.get("NEXTPOSITION")
                    if not nxt:
                        break
                    params["startPosition"] = nxt
            for sp in grp:
                pairs = data.get(str(sp.req["code"]))
                if pairs and sp.frequency == "Q":   # quarterly SURVEY_DATES are YYYYQQ (202603 = Q3)
                    pairs = [(date(d.year, (d.month - 1) * 3 + 1, 1), v) for d, v in pairs if 1 <= d.month <= 4]
                if pairs:
                    r.add(sp, dedupe_pairs(pairs))
                else:
                    r.fail(sp, f"code {sp.req['code']} not in BoJ {db} response")
        r.attempt(f"boj:{db}", one, grp)


def boj_start(start: date, freq: str) -> str:
    if freq == "Q":
        return f"{start.year}{(start.month - 1) // 3 + 1:02d}"
    if freq == "A":
        return str(start.year)
    return start.strftime("%Y%m")


# ------------------------------------------------------------------------------------------------ Korea / Mexico (keyed)


@fetcher("bok_ecos")
def fetch_ecos(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: stat, item, cycle (D/M/Q/A). Requires BOK_ECOS_APIKEY."""
    key = r.s.secret("bok_ecos_apikey")
    today = datetime.now(UTC).date()
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            cyc = str(sp.req.get("cycle", sp.frequency))
            fmt = {"D": "%Y%m%d", "M": "%Y%m", "A": "%Y"}.get(cyc)
            frm = r.start.strftime(fmt) if fmt else f"{r.start.year}Q1"
            to = today.strftime(fmt) if fmt else f"{today.year}Q4"
            url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/en/1/100000/{sp.req['stat']}/{cyc}/"
                   f"{frm}/{to}/{sp.req['item']}")
            j = r.http("bok_ecos").get(url, cache_ttl=6 * 3600).json()
            rows = (j.get("StatisticSearch") or {}).get("row", [])
            r.add(sp, [(parse_period(str(x["TIME"]).replace("Q", "-Q") if "Q" in str(x["TIME"]) else x["TIME"]),
                        to_float(x.get("DATA_VALUE"))) for x in rows])
        r.attempt(sp.series_id, one, [sp])


@fetcher("banxico")
def fetch_banxico(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: code (SIE series id). Requires BANXICO_TOKEN; up to 20 ids per request."""
    tok = r.s.secret("banxico_token")
    today = datetime.now(UTC).date()
    for i in range(0, len(specs), 20):
        chunk = specs[i:i + 20]

        def one(chunk: list[GSeries] = chunk) -> None:
            ids = ",".join(str(sp.req["code"]) for sp in chunk)
            j = r.http("banxico").get(
                f"https://www.banxico.org.mx/SieAPIRest/service/v1/series/{ids}/datos/{r.start.isoformat()}/"
                f"{today.isoformat()}", headers={"Bmx-Token": tok}, cache_ttl=6 * 3600).json()
            data = {s["idSerie"]: s.get("datos", []) for s in j["bmx"]["series"]}
            for sp in chunk:
                rows = data.get(str(sp.req["code"]), [])
                r.add(sp, [(datetime.strptime(d["fecha"], "%d/%m/%Y").date(), to_float(d["dato"])) for d in rows])
        r.attempt("banxico", one, chunk)


# ------------------------------------------------------------------------------------------------ Brazil


@fetcher("bcb")
def fetch_bcb(r: MacroRunner, specs: list[GSeries]) -> None:
    """req: code (SGS id). Daily series need <= 10y windows; 5y windows with retries (BCB answers HTML errors)."""
    today = datetime.now(UTC).date()
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            daily = sp.frequency == "D"
            pairs: list[tuple[date, float | None]] = []
            y0 = max(r.start.year, 2000 if daily else 1980)
            spans = [(date(y, 1, 1), min(date(y + 4, 12, 31), today)) for y in range(y0, today.year + 1, 5)]
            bad = 0
            for d0, d1 in spans:
                for attempt in range(3):
                    resp = r.http("bcb").get(f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{sp.req['code']}/dados",
                                             {"formato": "json", "dataInicial": d0.strftime("%d/%m/%Y"),
                                              "dataFinal": d1.strftime("%d/%m/%Y")}, cache_ttl=6 * 3600,
                                             raise_for_status=False)
                    if resp.status_code == 404:  # window before the series starts
                        break
                    if resp.content[:1] == b"[":
                        pairs += parse_bcb(resp.json())
                        break
                    time.sleep(2 * (attempt + 1))
                else:
                    bad += 1
            if bad:
                r.report.setdefault("partial", {})[sp.series_id] = f"{bad}/{len(spans)} windows failed"
            r.add(sp, [p for p in pairs if p[0] <= today])  # 432 carries future-dated rows
        r.attempt(sp.series_id, one, [sp])


# ------------------------------------------------------------------------------------------------ United States


UST_COLS = {"1M": "1 Mo", "2M": "2 Mo", "3M": "3 Mo", "4M": "4 Mo", "6M": "6 Mo", "1Y": "1 Yr", "2Y": "2 Yr", "3Y": "3 Yr",
            "5Y": "5 Yr", "7Y": "7 Yr", "10Y": "10 Yr", "20Y": "20 Yr", "30Y": "30 Yr"}


@fetcher("ust")
def fetch_ust(r: MacroRunner, specs: list[GSeries]) -> None:
    """US Treasury daily par yield curve CSV, one file per year (req: tenor in UST_COLS)."""
    def one() -> None:
        y0 = max(r.since_for(specs).year, 1990)
        data: dict[str, list[tuple[date, float | None]]] = {}
        for y in range(y0, r.today.year + 1):
            resp = r.http("ust").get(
                f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{y}/all",
                {"type": "daily_treasury_yield_curve", "field_tdr_date_value": y, "page": "", "_format": "csv"},
                cache_ttl=6 * 3600 if y == r.today.year else 30 * 86400)
            for k, v in parse_ust_csv(resp.text).items():
                data.setdefault(k, []).extend(v)
        for sp in specs:
            pairs = data.get(UST_COLS.get(str(sp.req["tenor"]), str(sp.req["tenor"])))
            if pairs:
                r.add(sp, sorted(pairs))
            else:
                r.fail(sp, f"tenor {sp.req['tenor']} not in Treasury CSV")
    r.attempt("ust", one, specs)


def parse_ust_csv(text: str) -> dict[str, list[tuple[date, float | None]]]:
    out: dict[str, list[tuple[date, float | None]]] = {}
    for row in csv.DictReader(io.StringIO(text.lstrip("\ufeff"))):
        try:
            d = datetime.strptime(row.get("Date", ""), "%m/%d/%Y").date()
        except ValueError:
            continue
        for k, v in row.items():
            if k and k != "Date":
                out.setdefault(k.strip(), []).append((d, to_float(v)))
    return out


@fetcher("nyfed")
def fetch_nyfed(r: MacroRunner, specs: list[GSeries]) -> None:
    """NY Fed ACM Treasury term premia (official ~10 MB OLE .xls, sheets 'ACM Daily'/'ACM Monthly'; columns DATE,
    ACMYnn fitted yield, ACMTPnn term premium, ACMRNYnn risk-neutral yield; percent). req: url, sheet, column.
    One download per sheet (12 h cache); the file always carries the full history since 1961."""
    def one() -> None:
        cache: dict[tuple[str, str], dict[str, list[tuple[date, float | None]]]] = {}
        for sp in specs:
            k = (str(sp.req["url"]), str(sp.req.get("sheet") or "ACM Daily"))
            if k not in cache:
                content = r.http("nyfed", timeout=180).get(k[0], cache_ttl=12 * 3600).content
                cache[k] = parse_acm_xls(content, k[1])
            pairs = cache[k].get(str(sp.req["column"]).upper())
            if pairs:
                r.add(sp, pairs)
            else:
                r.fail(sp, f"column {sp.req['column']} not in ACM sheet {k[1]}")
    r.attempt("nyfed", one, specs)


def parse_acm_xls(content: bytes, sheet: str = "ACM Daily") -> dict[str, list[tuple[date, float | None]]]:
    import xlrd  # noqa: PLC0415 - optional dependency, only for this source

    book = xlrd.open_workbook(file_contents=content, on_demand=True)
    sh = book.sheet_by_name(sheet)
    rows = [sh.row_values(i) for i in range(sh.nrows)]
    return parse_acm_rows(rows, book.datemode)


def parse_acm_rows(rows: list[list[Any]], datemode: int = 0) -> dict[str, list[tuple[date, float | None]]]:
    """Header row (DATE, ACM...) then one row per date; DATE is '14-Jun-1961' text or an Excel serial."""
    if not rows:
        return {}
    head = [str(h).strip().upper() for h in rows[0]]
    out: dict[str, list[tuple[date, float | None]]] = {h: [] for h in head[1:] if h}
    for row in rows[1:]:
        d0 = row[0]
        try:
            if isinstance(d0, (int, float)):
                import xlrd  # noqa: PLC0415

                d = xlrd.xldate_as_datetime(d0, datemode).date()
            else:
                d = datetime.strptime(str(d0).strip(), "%d-%b-%Y").date()
        except (ValueError, TypeError):
            continue
        for h, v in zip(head[1:], row[1:], strict=False):
            if h:
                out[h].append((d, to_float(v)))
    return {k: dedupe_pairs(sorted(v)) for k, v in out.items() if v}


NYFED_RATES = "https://markets.newyorkfed.org/api/rates/{path}/search.json"


@fetcher("nyfed_rates")
def fetch_nyfed_rates(r: MacroRunner, specs: list[GSeries]) -> None:
    """NY Fed Markets Data API reference rates (W3 rates-w3, verified 2026-10-31; robots: none, public API): req path
    (secured/sofr, unsecured/effr, unsecured/obfr, secured/tgcr, secured/bgcr, secured/sofrai) and field (percentRate,
    average30day, average90day, average180day, index). One request per path; search.json serves the full history."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["path"]), []).append(sp)
    for path, grp in groups.items():
        def one(path: str = path, grp: list[GSeries] = grp) -> None:
            since = r.since_for(grp, lookback_days=30)
            j = r.http("nyfed_rates").get(NYFED_RATES.format(path=path),
                                          {"startDate": since.isoformat(), "endDate": r.today.isoformat()},
                                          cache_ttl=3 * 3600).json()
            data = parse_nyfed_rates(j)
            for sp in grp:
                pairs = data.get(str(sp.req.get("field") or "percentRate"))
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"field {sp.req.get('field')} not in NY Fed {path}")
        r.attempt(f"nyfed_rates:{path}", one, grp)


def parse_nyfed_rates(j: dict[str, Any]) -> dict[str, list[tuple[date, float | None]]]:
    """{'refRates': [{effectiveDate, percentRate | average30day ..., revisionIndicator}]} -> {field: pairs}."""
    out: dict[str, list[tuple[date, float | None]]] = {}
    for rec in (j or {}).get("refRates") or []:
        try:
            d = date.fromisoformat(str(rec.get("effectiveDate"))[:10])
        except ValueError:
            continue
        for k in ("percentRate", "average30day", "average90day", "average180day", "index", "volumeInBillions"):
            if k in rec:
                out.setdefault(k, []).append((d, to_float(rec.get(k))))
    return {k: dedupe_pairs(sorted(v)) for k, v in out.items()}


# ------------------------------------------------------------------------------------------------ Canada


@fetcher("boc")
def fetch_boc(r: MacroRunner, specs: list[GSeries]) -> None:
    """Bank of Canada Valet (req: code). One request for all codes; a missing id 404s the batch -> per-id retry."""
    def get(codes: list[str]) -> dict[str, list[tuple[date, float | None]]]:
        j = r.http("boc").get(f"https://www.bankofcanada.ca/valet/observations/{','.join(codes)}/json",
                              {"start_date": r.start.isoformat()}, cache_ttl=6 * 3600).json()
        return parse_valet(j)

    def one() -> None:
        codes = sorted({str(sp.req["code"]) for sp in specs})
        try:
            data = get(codes)
        except HttpError:
            data = {}
            for c in codes:
                try:
                    data.update(get([c]))
                except HttpError as e:
                    r.report["failed"][f"boc:{c}"] = str(e)[:160]
        for sp in specs:
            pairs = data.get(str(sp.req["code"]))
            if pairs:
                r.add(sp, pairs)
            else:
                r.fail(sp, f"{sp.req['code']} not in Valet response")
    r.attempt("boc", one, specs)


def parse_valet(j: dict[str, Any]) -> dict[str, list[tuple[date, float | None]]]:
    out: dict[str, list[tuple[date, float | None]]] = {}
    for o in j.get("observations", []) or []:
        try:
            d = parse_period(o["d"])
        except (KeyError, ValueError):
            continue
        for k, v in o.items():
            if k != "d" and isinstance(v, dict):
                out.setdefault(k, []).append((d, to_float(v.get("v"))))
    return {k: sorted(v) for k, v in out.items()}


@fetcher("statcan")
def fetch_statcan(r: MacroRunner, specs: list[GSeries]) -> None:
    """Statistics Canada WDS getDataFromVectorsAndLatestNPeriods (req: vector). One POST for all vectors."""
    def one() -> None:
        n = (r.today.year - r.start.year + 1) * 12
        body = [{"vectorId": int(str(sp.req["vector"]).lstrip("v")), "latestN": n} for sp in specs]
        j = r.http("statcan").post("https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods",
                                   json_body=body).json()
        data = parse_statcan(j)
        for sp in specs:
            pairs = data.get(str(sp.req["vector"]).lstrip("v"))
            if pairs:
                r.add(sp, pairs)
            else:
                r.fail(sp, f"vector {sp.req['vector']} not in WDS response")
    r.attempt("statcan", one, specs)


def parse_statcan(j: Any) -> dict[str, list[tuple[date, float | None]]]:
    """Results are NOT in request order: map by vectorId. refPer = first day of the period (quarter start)."""
    out: dict[str, list[tuple[date, float | None]]] = {}
    for item in j or []:
        if item.get("status") != "SUCCESS":
            continue
        o = item.get("object") or {}
        rows = []
        for p in o.get("vectorDataPoint", []) or []:
            try:
                rows.append((date.fromisoformat(p["refPer"][:10]), to_float(p.get("value"))))
            except (KeyError, ValueError):
                continue
        out[str(o.get("vectorId"))] = sorted(rows)
    return out


# ------------------------------------------------------------------------------------------------ Brazil (IBGE)


@fetcher("ibge")
def fetch_ibge(r: MacroRunner, specs: list[GSeries]) -> None:
    """IBGE SIDRA API (req: path, e.g. '/t/5932/n1/all/v/6564/p/all/c11255/90707')."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            j = r.http("ibge").get(f"https://apisidra.ibge.gov.br/values{sp.req['path']}", cache_ttl=6 * 3600).json()
            r.add(sp, parse_sidra(j))
        r.attempt(sp.series_id, one, [sp])


def parse_sidra(j: list[dict[str, Any]]) -> list[tuple[date, float | None]]:
    """Row 0 = header names; the period column is the D?C whose header mentions Trimestre / Mes / Ano."""
    if not j:
        return []
    head = j[0]
    pcol, kind = None, "M"
    for k, v in head.items():
        lv = str(v).lower()
        if k.endswith("C") and ("trimestre" in lv or "mês" in lv or "mes " in lv or lv.startswith("mês") or
                                lv.startswith("ano")):
            pcol = k
            # 'Trimestre Movel' (PNAD rolling quarter) codes are YYYYMM of the END month -> monthly
            kind = "M" if ("móvel" in lv or "movel" in lv) else ("Q" if "trimestre" in lv else
                                                                  ("A" if lv.startswith("ano") else "M"))
            break
    out = []
    for row in j[1:]:
        code = str(row.get(pcol, ""))
        if len(code) < 4:
            continue
        y = int(code[:4])
        if kind == "Q":
            d = date(y, (int(code[4:6]) - 1) * 3 + 1, 1)
        elif kind == "A":
            d = date(y, 1, 1)
        else:
            d = date(y, int(code[4:6]), 1)
        out.append((d, to_float(row.get("V"))))
    return sorted(out)


# ------------------------------------------------------------------------------------------------ Peru / Argentina


@fetcher("bcrp")
def fetch_bcrp(r: MacroRunner, specs: list[GSeries]) -> None:
    """BCRP statistics API (req: code, monthly codes end in M). Invalid codes return an HTML bot page."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            resp = r.http("bcrp").get(
                f"https://estadisticas.bcrp.gob.pe/estadisticas/series/api/{sp.req['code']}/json/"
                f"{r.start.year}-{r.start.month}/{r.today.year}-12", cache_ttl=6 * 3600)
            r.add(sp, parse_bcrp(resp.json()))
        r.attempt(sp.series_id, one, [sp])


def parse_bcrp(j: dict[str, Any], col: int = 0) -> list[tuple[date, float | None]]:
    out = []
    for p in j.get("periods", []) or []:
        try:
            d = parse_period(str(p["name"]))
        except (KeyError, ValueError):
            continue
        vals = p.get("values") or []
        out.append((d, to_float(vals[col]) if len(vals) > col else None))
    return sorted(out)


@fetcher("bcra")
def fetch_bcra(r: MacroRunner, specs: list[GSeries]) -> None:
    """BCRA estadisticas v4.0 monetarias/{id} (v3 is 410 Gone); newest-first, offset/limit paging."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            since = r.since_for([sp])
            rows: list[tuple[date, float | None]] = []
            offset = 0
            for _ in range(50):
                j = r.http("bcra").get(f"https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/{sp.native}",
                                       {"desde": since.isoformat(), "hasta": r.today.isoformat(), "limit": 3000,
                                        "offset": offset}, cache_ttl=6 * 3600).json()
                res = (j.get("results") or [{}])[0]
                det = res.get("detalle") or []
                rows += [(date.fromisoformat(x["fecha"][:10]), to_float(x.get("valor"))) for x in det]
                rs = ((j.get("metadata") or {}).get("resultset") or {})
                offset += len(det)
                if not det or offset >= int(rs.get("count") or 0):
                    break
            r.add(sp, dedupe_pairs(rows))
        r.attempt(sp.series_id, one, [sp])


@fetcher("datos_ar")
def fetch_datos_ar(r: MacroRunner, specs: list[GSeries]) -> None:
    """datos.gob.ar series API (native id). ONE id per request: a multi-id request is resampled to the lowest common
    frequency (monthly series silently become quarterly). 1000 rows per page (start= paging)."""
    for i in range(0, len(specs), 1):
        chunk = specs[i:i + 1]

        def one(chunk: list[GSeries] = chunk) -> None:
            ids = [sp.native for sp in chunk]
            data: dict[str, list[tuple[date, float | None]]] = {k: [] for k in ids}
            start = 0
            for _ in range(20):
                j = r.http("datos_ar").get("https://apis.datos.gob.ar/series/api/series/",
                                           {"ids": ",".join(ids), "limit": 1000, "start": start, "format": "json",
                                            "start_date": r.start.isoformat()}, cache_ttl=6 * 3600).json()
                rows = j.get("data") or []
                for row in rows:
                    d = date.fromisoformat(str(row[0])[:10])
                    for k, v in zip(ids, row[1:], strict=False):
                        data[k].append((d, to_float(v)))
                start += len(rows)
                if not rows or start >= int(j.get("count") or 0):
                    break
            for sp in chunk:
                if data.get(sp.native):
                    r.add(sp, data[sp.native])
                else:
                    r.fail(sp, f"{sp.native} not in datos.gob.ar response")
        r.attempt("datos_ar", one, chunk)


# ------------------------------------------------------------------------------------------------ Colombia / Chile


@fetcher("banrep")
def fetch_banrep(r: MacroRunner, specs: list[GSeries]) -> None:
    """Banco de la Republica SDMX (GenericData XML only; req: flow, key). One 'all' request per dataflow."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["flow"]), []).append(sp)
    for flow, grp in groups.items():
        def one(flow: str = flow, grp: list[GSeries] = grp) -> None:
            since = r.since_for(grp)
            resp = r.http("banrep").get(f"https://totoro.banrep.gov.co/nsi-jax-ws/rest/data/ESTAT,{flow},1.0/all",
                                        {"detail": "dataonly", "startPeriod": since.isoformat()[:7]},
                                        headers={"Accept": "*/*"}, cache_ttl=6 * 3600)
            data = parse_sdmx_generic_xml(resp.content)
            for sp in grp:
                pairs = data.get(str(sp.req["key"]))
                if pairs:
                    r.add(sp, pairs)
                else:
                    r.fail(sp, f"key {sp.req['key']} not in BanRep {flow}")
        r.attempt(f"banrep:{flow}", one, grp)


def parse_sdmx_generic_xml(content: bytes) -> dict[str, list[tuple[date, float | None]]]:
    from lxml import etree  # noqa: PLC0415

    root = etree.fromstring(content)
    out: dict[str, list[tuple[date, float | None]]] = {}

    def local(el: Any) -> str:
        return el.tag.rsplit("}", 1)[-1] if isinstance(el.tag, str) else ""

    for s in root.iter():
        if local(s) != "Series":
            continue
        key, rows = "", []
        for c in s:
            if local(c) == "SeriesKey":
                key = ".".join(v.get("value", "") for v in c if local(v) == "Value")
            elif local(c) == "Obs":
                t = v = None
                for x in c:
                    if local(x) == "ObsDimension":
                        t = x.get("value")
                    elif local(x) == "ObsValue":
                        v = x.get("value")
                if t:
                    try:
                        rows.append((parse_period(t), to_float(v)))
                    except ValueError:
                        continue
        out[key] = sorted(rows)
    return out


@fetcher("ine_cl")
def fetch_ine_cl(r: MacroRunner, specs: list[GSeries]) -> None:
    """INE Chile SDMX (labour market only; req: flow 'CL01,DF_..,1.0', key). CSV only (JSON -> 406)."""
    from app.ingestion.macro_common import sdmx_fetch  # noqa: PLC0415

    sdmx_fetch(r, specs, "ine_cl", lambda flow, key: f"https://sdmx.ine.gob.cl/rest/data/{flow}/{key}",
               {"startPeriod": r.since()}, headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0"},
               cache_ttl=6 * 3600, max_batch=1)


@fetcher("bcch")
def fetch_bcch(r: MacroRunner, specs: list[GSeries]) -> None:
    """BCCh SieteRestWS (requires BCCH_USER + BCCH_PASS). Errors come as JSON Codigo != 0 with HTTP 200."""
    user, pw = r.s.secret("bcch_user"), r.s.secret("bcch_pass")
    if not (user and pw):
        r.skip("bcch", "missing key BCCH_USER/BCCH_PASS")
        for sp in specs:
            r.report.setdefault("skipped_series", {})[sp.series_id] = "missing key BCCH_USER/BCCH_PASS"
        return
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            j = r.http("bcch").get("https://si3.bcentral.cl/SieteRestWS/SieteRestWS.ashx",
                                   {"user": user, "pass": pw, "function": "GetSeries", "timeseries": sp.req["code"],
                                    "firstdate": r.start.isoformat(), "lastdate": r.today.isoformat()},
                                   cache_ttl=6 * 3600).json()
            if int(j.get("Codigo", -1)) != 0:
                raise ValueError(f"BCCh error {j.get('Codigo')}: {j.get('Descripcion')}")
            obs = (j.get("Series") or {}).get("Obs") or []
            r.add(sp, [(datetime.strptime(o["indexDateString"], "%d-%m-%Y").date(), to_float(o.get("value")))
                       for o in obs if o.get("statusCode", "OK") == "OK"])
        r.attempt(sp.series_id, one, [sp])


@fetcher("inegi")
def fetch_inegi(r: MacroRunner, specs: list[GSeries]) -> None:
    """INEGI BIE API (requires INEGI_TOKEN; req: indicator)."""
    tok = r.s.secret("inegi_token")
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            j = r.http("inegi").get(
                f"https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml/INDICATOR/{sp.req['indicator']}"
                f"/es/0700/false/BIE/2.0/{tok}", {"type": "json"}, cache_ttl=6 * 3600).json()
            obs = ((j.get("Series") or [{}])[0]).get("OBSERVATIONS") or []
            rows = []
            for o in obs:
                y, _, n = str(o.get("TIME_PERIOD", "")).partition("/")
                try:
                    k = int(n or 1)
                    d = date(int(y), (k - 1) * 3 + 1 if sp.frequency == "Q" else k, 1)
                except ValueError:
                    continue
                rows.append((d, to_float(o.get("OBS_VALUE"))))
            r.add(sp, rows)
        r.attempt(sp.series_id, one, [sp])


# ------------------------------------------------------------------------------------------------ UK (ONS)


@fetcher("ons")
def fetch_ons(r: MacroRunner, specs: list[GSeries]) -> None:
    """ONS time series JSON (req: uri, e.g. /economy/.../timeseries/d7g7/mm23). api.ons.gov.uk was decommissioned on
    2024-11-25; the www '<uri>/data' JSON is the machine-readable endpoint the ONS site itself serves."""
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            j = r.http("ons").get(f"https://www.ons.gov.uk{sp.req['uri']}/data", cache_ttl=6 * 3600).json()
            r.add(sp, parse_ons(j, sp.frequency))
        r.attempt(sp.series_id, one, [sp])


def parse_ons(j: dict[str, Any], freq: str) -> list[tuple[date, float | None]]:
    arr = {"M": "months", "Q": "quarters", "A": "years"}.get(freq, "months")
    out = []
    for o in j.get(arr, []) or []:
        s = str(o.get("date", "")).replace(" Q", "-Q")
        try:
            out.append((parse_period(s), to_float(o.get("value"))))
        except ValueError:
            continue
    return sorted(out)


# ------------------------------------------------------------------------------------------------ Japan (MoF, e-Stat)


@fetcher("mof_jp")
def fetch_mof_jp(r: MacroRunner, specs: list[GSeries]) -> None:
    """MoF JGB benchmark yields: historical CSV (1974-) + current-month CSV (req: tenor column, e.g. 10Y)."""
    def one() -> None:
        data: dict[str, dict[date, float | None]] = {}
        base = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate"
        for url, ttl in ((f"{base}/historical/jgbcme_all.csv", 3 * 86400), (f"{base}/jgbcme.csv", 6 * 3600)):
            resp = r.http("mof_jp").get(url, cache_ttl=ttl)
            for k, rows in parse_mof_csv(resp.content).items():
                data.setdefault(k, {}).update(dict(rows))
        for sp in specs:
            rows = data.get(str(sp.req["tenor"]))
            if rows:
                r.add(sp, sorted(rows.items()))
            else:
                r.fail(sp, f"tenor {sp.req['tenor']} not in MoF CSV")
    r.attempt("mof_jp", one, specs)


def parse_mof_csv(content: bytes) -> dict[str, list[tuple[date, float | None]]]:
    text = content.decode("utf-8", errors="replace").lstrip("\ufeff")
    out: dict[str, list[tuple[date, float | None]]] = {}
    header: list[str] | None = None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        if row[0].strip() == "Date":
            header = [c.strip() for c in row]
            continue
        if header is None:
            continue
        try:
            d = datetime.strptime(row[0].strip(), "%Y/%m/%d").date()
        except ValueError:
            continue
        for k, v in zip(header[1:], row[1:], strict=False):
            out.setdefault(k, []).append((d, to_float(v)))
    return out


@fetcher("estat_jp")
def fetch_estat(r: MacroRunner, specs: list[GSeries]) -> None:
    """e-Stat getStatsData (requires ESTAT_APPID; req: stats_id, cat01 code). Time codes 'YYYY00MMMM'."""
    app = r.s.secret("estat_appid")
    for sp in specs:
        def one(sp: GSeries = sp) -> None:
            j = r.http("estat_jp").get("https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData",
                                       {"appId": app, "statsDataId": sp.req["stats_id"], "cdCat01": sp.req["cat01"],
                                        **(sp.req.get("params") or {})}, cache_ttl=6 * 3600).json()
            vals = (((j.get("GET_STATS_DATA") or {}).get("STATISTICAL_DATA") or {}).get("DATA_INF") or {}).get("VALUE") or []
            rows = []
            for v in vals:
                t = str(v.get("@time", ""))
                if len(t) == 10 and t[4:6] == "00":
                    try:
                        rows.append((date(int(t[:4]), int(t[8:10]), 1), to_float(v.get("$"))))
                    except ValueError:
                        continue
            r.add(sp, sorted(rows))
        r.attempt(sp.series_id, one, [sp])


# ------------------------------------------------------------------------------------------------ China (AKShare)


def ak_records(fn: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Runs in the hard-timeout child process: call akshare.<fn>(**kwargs) -> picklable columns/rows."""
    import akshare as ak  # noqa: PLC0415

    df = getattr(ak, fn)(**(kwargs or {}))
    cols = [str(c) for c in df.columns]
    rows = [[None if (isinstance(x, float) and x != x) else (x if isinstance(x, (int, float, str)) else str(x))
             for x in row] for row in df.itertuples(index=False, name=None)]
    return {"columns": cols, "rows": rows}


_CN_Q = re.compile(r"(\d{4})年第(?:\d-)?(\d)季度")
_CN_M = re.compile(r"(\d{4})年(\d{1,2})月")


def parse_cn_period(s: Any) -> date:
    s = str(s).strip()
    m = _CN_Q.search(s)
    if m:
        return date(int(m[1]), (int(m[2]) - 1) * 3 + 1, 1)
    m = _CN_M.search(s)
    if m:
        return date(int(m[1]), int(m[2]), 1)
    return parse_period(s[:10])


#: A9 2026-11-07 (docs/it/ROBOTS_COMPLIANCE.md): AKShare functions whose upstream host disallows automated collection.
#: macro_china_shibor_all reads Jin10 (cdn.jin10.com robots.txt `Disallow: /`) -> the SHIBOR administrator's own
#: publication, CFETS / chinamoney ShiborHis (no robots.txt; 1-year windows). Overlap 2025-11-07..2026-11-06 vs the
#: stored Jin10 rows: 245 / 245 days identical (3M); CFETS also has the make-up working days Jin10 missed.
AK_DISALLOWED = {"macro_china_shibor_all": "chinamoney_shibor"}
CHINAMONEY_SHIBOR = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis"
SHIBOR_START = date(2006, 10, 8)
_SHIBOR_TENOR = {"O/N-定价": "ON", "1W-定价": "1W", "2W-定价": "2W", "1M-定价": "1M", "3M-定价": "3M", "6M-定价": "6M",
                 "9M-定价": "9M", "1Y-定价": "1Y"}


def parse_shibor_his(j: dict[str, Any] | None) -> dict[str, list[tuple[date, float]]]:
    """chinamoney ShiborHis JSON -> {tenor (ON, 1W, ..., 1Y): [(date, percent)]}."""
    out: dict[str, list[tuple[date, float]]] = {}
    for rec in (j or {}).get("records") or []:
        try:
            d = date.fromisoformat(str(rec.get("showDateCN") or "")[:10])
        except ValueError:
            continue
        for k, v in rec.items():
            if k.startswith("showDate"):
                continue
            fv = to_float(v)
            if fv is not None:
                out.setdefault(k, []).append((d, fv))
    return {k: sorted(set(v)) for k, v in out.items()}


def fetch_shibor_official(r: MacroRunner, specs: list[GSeries]) -> None:
    start = max(SHIBOR_START, min(r.since_for([sp]) for sp in specs))
    data: dict[str, list[tuple[date, float]]] = {}
    lo = start
    while lo <= r.today:
        hi = min(lo + timedelta(days=364), r.today)
        j = r.http("chinamoney").get(CHINAMONEY_SHIBOR, {"lang": "en", "startDate": lo.isoformat(),
                                                         "endDate": hi.isoformat()}, cache_ttl=6 * 3600).json()
        for k, v in parse_shibor_his(j).items():
            data.setdefault(k, []).extend(v)
        lo = hi + timedelta(days=1)
    for sp in specs:
        tenor = _SHIBOR_TENOR.get(str(sp.req.get("col")), str(sp.req.get("col")))
        pairs = sorted(set(data.get(tenor) or []))
        if pairs:
            r.add(sp, dedupe_pairs(pairs))
        else:
            r.fail(sp, f"chinamoney ShiborHis: no {tenor} fixings since {start}")


@fetcher("akshare_cn")
def fetch_akshare_cn(r: MacroRunner, specs: list[GSeries]) -> None:
    """AKShare macro_china_* / bond_* (Eastmoney-backed; the Jin10 '*_yearly' functions froze in 2025-09) inside the
    child-process hard-timeout runner. req: fn, col, date_col, [item_col, item], [kwargs], [since_kw]."""
    from app.ingestion._global_common import HardTimeoutRunner  # noqa: PLC0415

    official = [sp for sp in specs if AK_DISALLOWED.get(str(sp.req.get("fn"))) == "chinamoney_shibor"]
    if official:   # A9: never call the robots-disallowed upstream
        r.attempt("chinamoney:shibor", lambda: fetch_shibor_official(r, official), official)
    specs = [sp for sp in specs if str(sp.req.get("fn")) not in AK_DISALLOWED]
    groups: dict[tuple[str, str], list[GSeries]] = {}
    for sp in specs:
        groups.setdefault((str(sp.req["fn"]), repr(sorted((sp.req.get("kwargs") or {}).items()))), []).append(sp)
    if not groups:
        return
    with HardTimeoutRunner(timeout_s=120) as runner:
        for (fn, _), grp in groups.items():
            def one(fn: str = fn, grp: list[GSeries] = grp) -> None:
                kw = dict(grp[0].req.get("kwargs") or {})
                if grp[0].req.get("since_kw"):
                    kw[grp[0].req["since_kw"]] = r.since_for(grp).strftime("%Y%m%d")
                res = None
                for attempt in range(3):  # Eastmoney intermittently drops the connection (ChunkedEncodingError)
                    try:
                        r.http("akshare").limiter.acquire()
                        res = runner.call("app.ingestion.macro_national:ak_records", fn=fn, kwargs=kw)
                        break
                    except Exception:  # noqa: BLE001
                        if attempt == 2:
                            raise
                        time.sleep(3 * (attempt + 1))
                cols = res["columns"]
                for sp in grp:
                    dc, vc = cols.index(sp.req["date_col"]), cols.index(sp.req["col"])
                    ic = cols.index(sp.req["item_col"]) if sp.req.get("item_col") else None
                    pairs = []
                    for row in res["rows"]:
                        if ic is not None and str(row[ic]).strip() != sp.req["item"]:
                            continue
                        try:
                            d = parse_cn_period(row[dc])
                        except ValueError:
                            continue
                        if sp.frequency == "M":
                            d = d.replace(day=1)
                        if d >= r.today:   # bond_zh_us_rate forward-fills its last row to today
                            continue
                        pairs.append((d, to_float(row[vc])))
                    if pairs:
                        r.add(sp, dedupe_pairs(pairs))
                    else:
                        r.fail(sp, f"no rows for {fn}.{sp.req['col']}")
            r.attempt(f"akshare:{fn}", one, grp)


# ------------------------------------------------------------------------------------------------ Hong Kong


@fetcher("hkma")
def fetch_hkma(r: MacroRunner, specs: list[GSeries]) -> None:
    """HKMA open API (req: path, field, date_field). Paged by 1000 newest-first until the incremental start.
    NB: the HKMA WAF (Aliyun) blocks bursts with HTTP 405 -> keep requests minimal."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["path"]), []).append(sp)
    for path, grp in groups.items():
        def one(path: str = path, grp: list[GSeries] = grp) -> None:
            since = r.since_for(grp)
            dfield = grp[0].req.get("date_field", "end_of_day")
            recs: list[dict[str, Any]] = []
            for page in range(60):
                j = r.http("hkma").get(f"https://api.hkma.gov.hk/public/{path}",
                                       {"pagesize": 1000, "offset": page * 1000, "sortby": dfield, "sortorder": "desc"},
                                       cache_ttl=6 * 3600).json()
                batch = ((j.get("result") or {}).get("records")) or []
                recs += batch
                if len(batch) < 1000 or (batch and str(batch[-1].get(dfield, ""))[:10] < since.isoformat()):
                    break
            for sp in grp:
                pairs = []
                for x in recs:
                    try:
                        pairs.append((date.fromisoformat(str(x[dfield])[:10]), to_float(x.get(sp.req["field"]))))
                    except (KeyError, ValueError):
                        continue
                r.add(sp, dedupe_pairs(pairs))
        r.attempt(f"hkma:{path.rsplit('/', 1)[-1]}", one, grp)


@fetcher("censtatd")
def fetch_censtatd(r: MacroRunner, specs: list[GSeries]) -> None:
    """HK C&SD web table API (req: table, filters {sv, svDesc, freq, ...}). full_series=1 is required."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["table"]), []).append(sp)
    for table, grp in groups.items():
        def one(table: str = table, grp: list[GSeries] = grp) -> None:
            j = r.http("censtatd").get("https://www.censtatd.gov.hk/api/get.php",
                                       {"id": table, "lang": "en", "full_series": 1}, cache_ttl=6 * 3600).json()
            ds = j.get("dataSet") or []
            for sp in grp:
                filt = {str(k): str(v) for k, v in (sp.req.get("filters") or {}).items()}
                pairs = []
                for x in ds:
                    if any(str(x.get(k, "")) != v for k, v in filt.items()):
                        continue
                    p = str(x.get("period", ""))
                    if len(p) < 6:
                        continue
                    y, m = int(p[:4]), int(p[4:6])
                    if sp.frequency == "Q":   # quarters are labelled by their end month
                        m = m - 2
                    pairs.append((date(y, m, 1), to_float(x.get("figure"))))
                if pairs:
                    r.add(sp, dedupe_pairs(pairs))
                else:
                    r.fail(sp, f"no rows for {table} {filt}")
        r.attempt(f"censtatd:{table}", one, grp)


# ------------------------------------------------------------------------------------------------ Australia


@fetcher("rba")
def fetch_rba(r: MacroRunner, specs: list[GSeries]) -> None:
    """RBA statistical tables CSV (req: table e.g. f2, code = Series ID)."""
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["table"]), []).append(sp)
    for table, grp in groups.items():
        def one(table: str = table, grp: list[GSeries] = grp) -> None:
            resp = r.http("rba").get(f"https://www.rba.gov.au/statistics/tables/csv/{table}-data.csv", cache_ttl=6 * 3600)
            data = parse_rba_csv(resp.content.decode("utf-8", errors="replace"))
            for sp in grp:
                pairs = data.get(str(sp.req["code"]))
                if not pairs:
                    r.fail(sp, f"{sp.req['code']} not in RBA {table}")
                    continue
                if sp.frequency in ("M", "Q"):   # RBA dates are period ends
                    pairs = [(date(d.year, ((d.month - 1) // 3) * 3 + 1 if sp.frequency == "Q" else d.month, 1), v)
                             for d, v in pairs]
                r.add(sp, dedupe_pairs(pairs))
        r.attempt(f"rba:{table}", one, grp)


def parse_rba_csv(text: str) -> dict[str, list[tuple[date, float | None]]]:
    codes: list[str] | None = None
    out: dict[str, list[tuple[date, float | None]]] = {}
    for row in csv.reader(io.StringIO(text.lstrip("\ufeff"))):
        if not row or not row[0].strip():
            continue
        if row[0].strip() == "Series ID":
            codes = [c.strip() for c in row[1:]]
            continue
        if codes is None:
            continue
        s = row[0].strip()
        d = None
        for fmt in ("%d-%b-%Y", "%d/%m/%Y"):
            try:
                d = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
        if d is None:
            continue
        for c, v in zip(codes, row[1:], strict=False):
            fv = to_float(v)
            if c and fv is not None:
                out.setdefault(c, []).append((d, fv))
    return out


@fetcher("abs")
def fetch_abs(r: MacroRunner, specs: list[GSeries]) -> None:
    """ABS Data API SDMX (req: flow 'ABS,LF,1.0.0', key)."""
    from app.ingestion.macro_common import sdmx_fetch  # noqa: PLC0415

    sdmx_fetch(r, specs, "abs", lambda flow, key: f"https://data.api.abs.gov.au/rest/data/{flow}/{key}",
               {"startPeriod": r.since()}, headers={"Accept": "application/vnd.sdmx.data+csv"}, cache_ttl=6 * 3600,
               max_batch=20, single_dim=True)


# ------------------------------------------------------------------------------------------------ Taiwan


@fetcher("cbc_tw")
def fetch_cbc(r: MacroRunner, specs: list[GSeries]) -> None:
    """Central Bank of the ROC (Taiwan) DataAPI (req: file e.g. EG2AM01, col = 0-based value column).
    The server certificate lacks a Subject Key Identifier -> relax only VERIFY_X509_STRICT (chain still verified)."""
    import ssl  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    c = r.http("cbc_tw")
    if not getattr(c, "_ssl_relaxed", False):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        c._client.close()
        c._client = httpx.Client(headers=c.headers, timeout=c.timeout, follow_redirects=True, verify=ctx)
        c._ssl_relaxed = True
    groups: dict[str, list[GSeries]] = {}
    for sp in specs:
        groups.setdefault(str(sp.req["file"]), []).append(sp)
    for fname, grp in groups.items():
        def one(fname: str = fname, grp: list[GSeries] = grp) -> None:
            resp = c.get("https://cpx.cbc.gov.tw/API/DataAPI/Get", {"FileName": fname}, cache_ttl=6 * 3600)
            import json  # noqa: PLC0415

            rows = (json.loads(resp.content.decode("utf-8-sig")).get("data") or {}).get("dataSets") or []
            for sp in grp:
                col = int(sp.req["col"])
                pairs = []
                for row in rows:
                    try:
                        d = parse_period(str(row[0]))
                    except ValueError:
                        continue
                    pairs.append((d, to_float(row[1 + col]) if len(row) > 1 + col else None))
                r.add(sp, pairs)
        r.attempt(f"cbc_tw:{fname}", one, grp)


def run(ctx):  # noqa: ANN001 - JobContext
    return run_job(ctx, "macro_national", SOURCES)


_ = timedelta  # re-exported for fetchers added below
