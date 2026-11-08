"""Job `earnings_events`: historical earnings releases, upcoming calendar and unified corporate events.

Writes earnings_events (one row per issuer x fiscal period), earnings_calendar (next expected periods, labelled by
date source) and corporate_events.

Inputs
* SEC submissions history per CIK (app.earnings.sec_history): 8-K Item 2.02 with acceptance times = official
  earnings releases; 10-Q / 10-K / 20-F / 40-F (reportDate = period end) = periodic reports; other 8-K items and
  DEF 14A -> corporate_events. params: refresh_days (nightly: re-request CIKs with a filing in filings_index over the
  last N days, default 4), fetch_missing (request CIKs never landed, default true), older_pages (older submission pages
  back to min_year from the HTTP cache, default true), min_year (default 2012), max_requests (SEC request cap, 1500).
* filings_index for every other regulator: periodic report rows (annual / semiannual / quarterly with period_end),
  TDnet kessan tanshin (official release), HKEX results announcements (official release), TDnet buybacks / forecast
  revisions and HKEX announcements -> corporate_events.
* earnings_announcements: Yahoo dates (provider) and JPX schedule (company-announced, upcoming).
* corporate_actions (dividends / splits, incl. declared future ex dates), fundamentals_canonical (fiscal labels).

Period assignment: a dated release belongs to the latest known fiscal period end P with 4 <= D - P <= 100 days
(130 outside the US); otherwise P is extrapolated from the issuer's reporting cadence (or the calendar quarter
before D when nothing is known) and period_inferred = true. Per period the release is the earliest official
release; else the provider date; else the periodic report filing itself.

Calendar (model-based projection): next period end from the cadence (month-end arithmetic, 52/53-week issuers via
the same period a year earlier + 364 days); expected date = same-period release a year earlier + 364 days (keeps the
weekday) or period end + median lag of the last 4 releases; window = +-max(3, 1.5 x MAD of lags) days (<= 21).
Overridden by a company-announced JPX date, else a Yahoo date (provider estimate) mapped to the same period.
"""

from __future__ import annotations

import itertools
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.storage import scan_table
from app.earnings import common as C
from app.earnings import sec_history as SH

log = C.log

MAX_LAG_US, MAX_LAG_INTL, MIN_LAG = 100, 130, 4
CORP_8K = {"1.01": ("mna_agreement", "Material definitive agreement (8-K 1.01)"),
           "2.01": ("mna_completed", "Acquisition / disposition completed (8-K 2.01)"),
           "5.02": ("exec_change", "Executive / director change (8-K 5.02)"),
           "2.03": ("financing", "Direct financial obligation (8-K 2.03)"),
           "3.02": ("equity_issue", "Unregistered equity sale (8-K 3.02)"),
           "8.01": ("other_material", "Other material event (8-K 8.01)"),
           "5.07": ("agm_results", "Shareholder vote results (8-K 5.07)")}
SEC_LABEL = {"8-K": "SEC 8-K Item 2.02 (acceptance time)", "10-Q": "SEC 10-Q filing", "10-K": "SEC 10-K filing",
             "20-F": "SEC 20-F filing", "40-F": "SEC 40-F filing"}
SOURCE_LABEL = {"tdnet": "TDnet kessan tanshin (release time)", "hkex": "HKEX results announcement (release time)",
                "edinet": "EDINET statutory report filing", "dart_bulk": "DART periodic report", "opendart": "DART periodic report",
                "cvm": "CVM ITR / DFP filing", "esef_xbrl_org": "ESEF annual report (filings.xbrl.org)",
                "fca_nsm": "FCA NSM annual report", "akshare": "CNINFO / exchange periodic report",
                "baostock": "Exchange periodic report", "yahoo": "Provider date (Yahoo)",
                "cninfo": "CNINFO actual disclosure date (SSE / SZSE schedule)",
                "fca_nsm_results": "FCA NSM results announcement (publication time)",
                "afm_nl": "Half-yearly financial report (AFM register)",
                "fsma_be": "Half-yearly financial report (FSMA STORI)"}
#: earnings_announcements sources whose `release` rows are official results publications (app.earnings.announce)
RELEASE_SOURCES = ("cninfo", "fca_nsm")
#: OAM half-yearly financial reports without a period end in filings_index (published on the results day)
OAM_HFR = (("afm_nl", "HFR"), ("fsma_be", "HFR"))
FP_FROM_TITLE = (("第1四半期", "Q1"), ("第１四半期", "Q1"), ("第2四半期", "Q2"), ("第２四半期", "Q2"), ("中間", "H1"),
                 ("第3四半期", "Q3"), ("第３四半期", "Q3"))
HK_RESULT_CATS = ("[Final Results]", "[Interim Results]", "[Quarterly Results]")


# ------------------------------------------------------------------------------------------------ inputs


def _fi(s: Settings) -> pl.LazyFrame:
    return scan_table("filings_index", s).filter(pl.col("issuer_id").is_not_null())


def periodic_from_filings_index(s: Settings, exclude_sec: bool = False) -> pl.DataFrame:
    """Periodic reports with a period end: [issuer_id, period_end, fiscal_period, fiscal_year, ts, kdate, source_id,
    form_type, filing_id, url]."""
    lf = _fi(s).filter(pl.col("form_class").is_in(["annual", "semiannual", "quarterly"])
                       & pl.col("period_end").is_not_null() & ~pl.col("is_amendment").fill_null(False))
    if exclude_sec:
        lf = lf.filter(pl.col("source_id") != "sec_edgar")
    return lf.select("issuer_id", "period_end", "fiscal_period", "fiscal_year", pl.col("knowledge_ts").alias("ts"),
                     pl.col("knowledge_date").alias("kdate"), "source_id", "form_type", "filing_id", "url").collect()


def releases_from_filings_index(s: Settings) -> pl.DataFrame:
    """Official earnings-release documents outside SEC: TDnet TANSHIN, HKEX results announcements, OAM half-yearly
    financial reports filed without a period end (AFM, FSMA; period assigned from the cadence)."""
    oam = pl.lit(False)
    for src, form in OAM_HFR:
        oam = oam | ((pl.col("source_id") == src) & (pl.col("form_type") == form) & pl.col("period_end").is_null())
    lf = _fi(s).filter(((pl.col("source_id") == "tdnet") & (pl.col("form_type") == "TANSHIN"))
                       | ((pl.col("source_id") == "hkex") & (pl.col("form_type") == "RESULTS")) | oam)
    d = lf.select("issuer_id", "fiscal_period", pl.col("knowledge_ts").alias("ts"), pl.col("knowledge_date").alias("kdate"),
                  "source_id", "form_type", "filing_id", "url", "items").collect()
    if d.is_empty():
        return d
    cat = pl.col("items").list.get(1, null_on_oob=True).fill_null("")
    title = pl.col("items").list.get(0, null_on_oob=True).fill_null("")
    hk_ok = pl.any_horizontal([cat.str.contains(c, literal=True) for c in HK_RESULT_CATS])
    d = d.filter((pl.col("source_id") != "hkex") | hk_ok)
    # TDnet supplementary material ("補足説明資料") duplicates the release
    d = d.filter(~((pl.col("source_id") == "tdnet") & title.str.contains("補足", literal=True)))
    d = d.with_columns(title.alias("_title"))
    pes = [period_from_title(src, tt) for src, tt in d.select("source_id", "_title").iter_rows()]
    return d.with_columns(pl.Series("period_end", pes, dtype=pl.Date)).drop("items", "_title")


_JP_FY = re.compile(r"(\d{4})年\s*(\d{1,2})月期")
_HK_END = re.compile(r"ENDED\s+(\d{1,2})\s+([A-Z]+)\s+(\d{4})", re.IGNORECASE)
_MONTHS = {m: i + 1 for i, m in enumerate(("JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                                           "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"))}


def period_from_title(source: str, title: str | None) -> date | None:
    """Period end from a TDnet tanshin title ('2027年1月期 第2四半期決算短信' -> 2026-07-31) or an HKEX results
    title ('... RESULTS FOR THE SIX MONTHS ENDED 30 JUNE 2026' -> 2026-06-30)."""
    if not title:
        return None
    if source == "tdnet":
        from app.earnings.fetch import jpx_period_end  # noqa: PLC0415

        m = _JP_FY.search(title)
        if not m:
            return None
        y, mo = int(m.group(1)), int(m.group(2))
        if not 1 <= mo <= 12:
            return None
        q = "Q4"
        for k, v in FP_FROM_TITLE:
            if k in title:
                q = "Q2" if v == "H1" else v
                break
        return jpx_period_end(C.month_end(y, mo), q)
    m = _HK_END.search(title)
    if m and m.group(2).upper() in _MONTHS:
        try:
            return date(int(m.group(3)), _MONTHS[m.group(2).upper()], int(m.group(1)))
        except ValueError:
            return None
    return None


def sec_frames(s: Settings, meta: pl.DataFrame, p: dict[str, Any]) -> tuple[pl.DataFrame, dict[str, Any]]:
    ciks = meta.filter(pl.col("cik").is_not_null()).get_column("cik").to_list()
    refresh = SH.recent_filers(int(p.get("refresh_days", 4)), s) if int(p.get("refresh_days", 4)) > 0 else []
    # older submission pages come from the HTTP cache (immutable, cached forever) - without them the nightly overwrite
    # would drop the pre-2020s history of heavy filers; only CIKs never seen before cost requests (capped)
    df, st = SH.load_history(ciks, refresh=refresh, fetch_missing=_flag(p, "fetch_missing", True),
                             older_pages=_flag(p, "older_pages", True), min_year=int(p.get("min_year", 2012)),
                             settings=s, max_requests=int(p.get("max_requests", 1500)))
    cmap = {SH.cik10(c): i for c, i in meta.filter(pl.col("cik").is_not_null()).select("cik", "issuer_id").iter_rows()}
    df = df.with_columns(pl.col("cik").replace_strict(cmap, default=None).alias("issuer_id")).filter(
        pl.col("issuer_id").is_not_null())
    return df, st


def _flag(p: dict[str, Any], k: str, default: bool) -> bool:
    v = p.get(k, default)
    return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")


def canonical_labels(s: Settings) -> dict[str, list[tuple[date, str, int]]]:
    """issuer -> [(period_end, fiscal_period, fiscal_year)] from fundamentals_canonical quarterly / half / FY rows."""
    try:
        d = (scan_table("fundamentals_canonical", s)
             .filter(pl.col("item_id").is_in(["revenue", "net_income"]) & pl.col("period_kind").is_in(["Q", "H", "FY"]))
             .select("issuer_id", "period_end", "fiscal_period", "fiscal_year").unique().collect())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[tuple[date, str, int]]] = defaultdict(list)
    for i, pe, fp, fy in d.iter_rows():
        out[i].append((pe, fp, fy))
    return out


# ------------------------------------------------------------------------------------------------ per issuer


def _lag_ok(d: date, pe: date, us: bool) -> bool:
    lag = (d - pe).days
    return MIN_LAG <= lag <= (MAX_LAG_US if us else MAX_LAG_INTL)


def cadence_months(periods: list[date]) -> int:
    ps = sorted(set(periods))[-9:]
    if len(ps) < 2:
        return 3
    diffs = [(b - a).days for a, b in itertools.pairwise(ps) if (b - a).days > 40]
    if not diffs:
        return 3
    m = statistics.median(diffs) / 30.44
    return 3 if m < 4.5 else (6 if m < 9 else 12)


def merge_close(periods: dict[date, dict[str, Any]], pe: date, tol: int = 7) -> date:
    """Return an existing period end within tol days of pe (different sources disagree on 52/53-week ends)."""
    for k in periods:
        if abs((k - pe).days) <= tol:
            return k
    return pe


def extrapolate(periods: list[date], until: date, months: int) -> list[date]:
    """Known period ends extended by the cadence up to `until`."""
    ps = sorted(set(periods))
    if not ps:
        return []
    out = list(ps)
    guard = 0
    while out[-1] < until and guard < 40:
        prev_year = next((x for x in reversed(out) if 350 <= (out[-1] + timedelta(days=round(30.44 * months)) - x).days <= 380), None)
        out.append(C.next_period_end(out[-1], months, prev_year))
        guard += 1
    return out


def assign_period(d: date, known: list[date], months: int, us: bool) -> tuple[date, bool]:
    cands = [p for p in known if _lag_ok(d, p, us)]
    if cands:
        return max(cands), False
    ext = extrapolate(known, d, months)
    cands = [p for p in ext if _lag_ok(d, p, us)]
    if cands:
        return max(cands), True
    return C.calendar_quarter_end_before(d, 10 if us else 20), True


def fiscal_label(pe: date, labels: list[tuple[date, str, int]], form: str | None, fp_hint: str | None,
                 fye_mmdd: str | None, months: int) -> tuple[str | None, int | None]:
    best = None
    for lp, fp, fy in labels:
        if abs((lp - pe).days) <= 7 and fp in C.FISCAL_PERIODS and (best is None or (fp != "FY" and best[0] == "FY")):
            best = (fp, fy)
    if best:
        fp, fy = best
        if fp == "FY" and months == 3:
            fp = "Q4"
        return fp, fy
    if fp_hint in C.FISCAL_PERIODS:
        return ("Q4" if fp_hint == "FY" and months == 3 else fp_hint), None
    if form and (form.startswith("10-K") or form in ("20-F", "40-F")):
        return ("Q4" if months == 3 else "FY"), pe.year
    if fye_mmdd and len(str(fye_mmdd)) == 4 and months == 3:
        try:
            fm = int(str(fye_mmdd)[:2])
            k = ((pe.month - fm) % 12) // 3
            return ({1: "Q1", 2: "Q2", 3: "Q3", 0: "Q4"}[k]), (pe.year if pe.month <= fm else pe.year + 1)
        except (ValueError, KeyError):
            return None, None
    return None, None


def _midnight(ts: datetime | None) -> bool:
    return ts is not None and ts.hour == 0 and ts.minute == 0 and ts.second == 0


def build_issuer_events(iid: str, m: dict[str, Any], periodic: list[dict[str, Any]], releases: list[dict[str, Any]],
                        provider: list[dict[str, Any]], labels: list[tuple[date, str, int]]) -> list[dict[str, Any]]:
    """periodic: [{period_end, fiscal_period, ts, kdate, source_id, form_type, filing_id, url}],
    releases: official release docs [{ts, kdate, source_id, form_type, filing_id, url, fiscal_period}],
    provider: Yahoo [{event_date, event_ts}] (past only)."""
    us = (m.get("region") == "US")
    tz, region = m.get("timezone"), m.get("region")
    periods: dict[date, dict[str, Any]] = {}
    # date-only sources store midnight UTC as knowledge_ts: that is not a time of day
    periodic = [r | {"ts": None} if _midnight(r.get("ts")) else r for r in periodic]
    releases = [r | {"ts": None} if _midnight(r.get("ts")) else r for r in releases]
    for r in sorted(periodic, key=lambda r: (r["kdate"], r["ts"] or datetime.min.replace(tzinfo=C.UTC))):
        pe = merge_close(periods, r["period_end"])
        cur = periods.get(pe)
        if cur is None or r["kdate"] < cur["kdate"]:
            periods[pe] = r | {"period_end": pe}
    known = sorted(periods)
    months = cadence_months(known) if known else 3

    def ldate(ts: datetime | None, kd: date) -> date:
        lt = C.local_dt(ts, tz, region)
        return lt.date() if lt else kd

    by_p: dict[date, dict[str, Any]] = {}
    inferred: dict[date, bool] = {}
    for r in sorted(releases, key=lambda r: ldate(r["ts"], r["kdate"])):
        d = ldate(r["ts"], r["kdate"])
        if r.get("period_end") is not None and 0 < (d - r["period_end"]).days <= 200:
            pe, inf = r["period_end"], False
        else:
            pe, inf = assign_period(d, known, months, us)
        pe = merge_close(periods, pe)
        if pe not in by_p:
            by_p[pe] = r | {"release_date": d, "date_source": "official_release"}
            inferred[pe] = inf and pe not in periods
    prov_of: dict[date, date] = {}
    for r in sorted(provider, key=lambda r: r["event_date"]):
        d = r["event_date"]
        # a provider date within 3 days of an assigned official release is the same event
        hit = next((pe for pe, x in by_p.items() if abs((x["release_date"] - d).days) <= 3), None)
        if hit is not None:
            prov_of.setdefault(hit, d)
            continue
        pe, inf = assign_period(d, known, months, us)
        pe = merge_close(periods, pe)
        prov_of.setdefault(pe, d)
        if pe in by_p:
            continue
        rep = periods.get(pe)
        # a statutory report filed AFTER the provider date: the provider date is the earlier public release
        if rep is not None and rep["kdate"] <= d:
            continue
        by_p[pe] = {"ts": r.get("event_ts"), "kdate": d, "source_id": "yahoo", "form_type": None, "filing_id": None,
                    "url": None, "release_date": d, "date_source": "provider", "fiscal_period": None}
        inferred[pe] = inf and pe not in periods
    for pe, rep in periods.items():
        if pe not in by_p:
            by_p[pe] = rep | {"release_date": ldate(rep["ts"], rep["kdate"]), "date_source": "official_filing"}
            inferred[pe] = False
    out = []
    for pe, r in by_p.items():
        rep = periods.get(pe)
        fp, fy = fiscal_label(pe, labels, (rep or {}).get("form_type") or r.get("form_type"),
                              r.get("fiscal_period") or (rep or {}).get("fiscal_period"), m.get("fiscal_year_end_mmdd"),
                              months)
        if fy is None and rep is not None and rep.get("fiscal_year"):
            fy = int(rep["fiscal_year"])
        ts = r.get("ts")
        sess = C.classify_session(ts, tz, region)
        src = r["source_id"]
        label = SEC_LABEL.get((r.get("form_type") or "").split("/")[0], "SEC filing") if src == "sec_edgar" \
            else SOURCE_LABEL.get(src, f"{src} filing")
        out.append({
            "issuer_id": iid, "security_id": m.get("security_id"), "region": region or "OTHER",
            "country": m.get("country"), "period_end": pe, "fiscal_year": fy, "fiscal_period": fp,
            "period_inferred": bool(inferred.get(pe, False)), "release_date": r["release_date"], "release_ts": ts,
            "session": sess, "reaction_date": C.reaction_day(r["release_date"], sess),
            "date_source": r["date_source"], "source_id": src, "source_label": label, "form_type": r.get("form_type"),
            "filing_id": r.get("filing_id"), "url": r.get("url"),
            "report_filing_date": (C.local_dt(rep["ts"], tz, region).date() if rep and rep.get("ts") else
                                   (rep or {}).get("kdate")),
            "report_form_type": (rep or {}).get("form_type"), "provider_date": prov_of.get(pe),
            "knowledge_date": r["release_date"],
        })
    return out


# ------------------------------------------------------------------------------------------------ calendar


def _mad(xs: list[int]) -> float:
    if len(xs) < 2:
        return 0.0
    med = statistics.median(xs)
    return statistics.median([abs(x - med) for x in xs])


def project_calendar(iid: str, m: dict[str, Any], evs: list[dict[str, Any]], today: date,
                     announced: list[dict[str, Any]], provider_future: list[dict[str, Any]],
                     n_next: int = 2) -> list[dict[str, Any]]:
    """Next `n_next` unreleased periods with expected dates (see module doc)."""
    if not evs and not announced and not provider_future:
        return []
    us = m.get("region") == "US"
    tz, region = m.get("timezone"), m.get("region")
    evs = sorted(evs, key=lambda e: e["period_end"])
    known = [e["period_end"] for e in evs]
    months = cadence_months(known) if known else 3
    released = {e["period_end"] for e in evs}
    by_pe = {e["period_end"]: e for e in evs}
    sessions = [e["session"] for e in evs[-8:] if e["session"] != "unknown"]
    usual = Counter(sessions).most_common(1)[0][0] if sessions else "unknown"
    lags = [x for x in ((e["release_date"] - e["period_end"]).days for e in evs[-8:]) if 0 < x <= 200]
    out: dict[date, dict[str, Any]] = {}
    # model-based projection for the next unreleased periods
    if known:
        ext = extrapolate(known, today + timedelta(days=400), months)
        nxt = [p for p in ext if p not in released and p > known[-1]]
        for pe in nxt[: n_next + 1]:
            prev = next((e for p, e in by_pe.items() if 350 <= (pe - p).days <= 380), None)
            if prev is not None:
                exp = prev["release_date"] + timedelta(days=364)
                lag = (exp - pe).days
                if lags and abs(lag - statistics.median(lags)) > 30:
                    exp = pe + timedelta(days=int(statistics.median(lags)))
            elif lags:
                exp = pe + timedelta(days=int(statistics.median(lags[-4:])))
            else:
                exp = pe + timedelta(days=35 if us else 50)
            exp = C.on_or_after_weekday(exp)
            if exp < today - timedelta(days=7):
                continue
            w = int(min(21, max(3, round(1.5 * _mad(lags[-8:])))))
            fp, _ = (prev["fiscal_period"], None) if prev else (None, None)
            out[pe] = {"period_end": pe, "fiscal_period": fp, "expected_date": exp,
                       "expected_ts": None, "session": usual, "window_lo": exp - timedelta(days=w),
                       "window_hi": exp + timedelta(days=w), "date_source": "model_projection",
                       "source_id": "cadence_model", "source_label": C.LABEL_PROJECTION}
            if len(out) >= n_next:
                break

    def place(d: date) -> date:
        unrel = [p for p in extrapolate(known, d, months) if p not in released] if known else []
        cands = [p for p in unrel if _lag_ok(d, p, us)]
        if cands:
            return max(cands)
        return C.calendar_quarter_end_before(d, 10 if us else 20)

    for r in sorted(provider_future, key=lambda r: r["event_date"]):
        pe = merge_close(out, place(r["event_date"]))
        if pe in released:
            continue
        ts = r.get("event_ts")
        prev = out.get(pe) or {}
        out[pe] = {"period_end": pe, "fiscal_period": prev.get("fiscal_period"), "expected_date": r["event_date"],
                   "expected_ts": ts, "session": C.classify_session(ts, tz, region) if ts else usual,
                   "window_lo": None, "window_hi": None, "date_source": "provider_estimate", "source_id": "yahoo",
                   "source_label": C.LABEL_PROVIDER}
    for r in announced:
        pe = r.get("period_end") or place(r["event_date"])
        pe = merge_close(out, pe)
        if pe in released:
            continue
        prev = out.get(pe) or {}
        out[pe] = {"period_end": pe, "fiscal_period": r.get("fiscal_period") or prev.get("fiscal_period"),
                   "expected_date": r["event_date"], "expected_ts": None, "session": usual, "window_lo": None,
                   "window_hi": None, "date_source": "company_announced", "source_id": r["source_id"],
                   "source_label": C.LABEL_COMPANY}
    rows = sorted(out.values(), key=lambda x: x["expected_date"])
    rows = [x for x in rows if x["expected_date"] >= today - timedelta(days=3)][:n_next]
    for x in rows:
        x.update({"issuer_id": iid, "security_id": m.get("security_id"), "region": region, "country": m.get("country"),
                  "sector_id": m.get("sector_id"), "usual_session": usual, "n_history": len(evs)})
    return rows


#: statutory reporting deadlines by region: (interim cadence months, days after an interim period end, days after the
#: fiscal year end). CN: CSRC Q1 / Q3 within 1 month, H1 within 2 months, annual within 4 months; HK: HKEX interim
#: within 2 months, annual within 3 months; KR: FSCMA quarterly / half-year within 45 days, annual within 90 days;
#: JP: TSE tanshin within 45 days; TW: quarterly 45 days, annual 3 months; US: 10-Q 40-45 days, 10-K 60-90 days;
#: EU / UK: half-year within 3 months, annual within 4 months (Transparency Directive / DTR 4).
DEADLINES: dict[str, tuple[int, int, int]] = {"CN": (3, 30, 120), "HK": (6, 61, 91), "KR": (3, 45, 90),
                                              "JP": (3, 45, 45), "APAC": (3, 45, 90), "US": (3, 45, 90),
                                              "CA": (3, 45, 90), "LATAM": (3, 45, 90), "EU": (6, 91, 120),
                                              "UK": (6, 91, 120)}
LABEL_DEADLINE = "Model-based projection (statutory reporting deadline; no release history)"


def project_deadline(iid: str, m: dict[str, Any], today: date, n_next: int = 1) -> list[dict[str, Any]]:
    """Next period(s) for an issuer WITHOUT any release history: period ends from the fiscal year end (12-31 when
    unknown) at the market's interim cadence, expected date = the statutory filing deadline (issuers file on or before
    it), window = the 30 days before it. Labelled as a projection (date_source model_projection)."""
    rule = DEADLINES.get(m.get("region") or "")
    if rule is None:
        return []
    months, d_int, d_fy = rule
    fye = str(m.get("fiscal_year_end_mmdd") or "1231").replace("-", "")
    try:
        fm, fd = int(fye[:2]), int(fye[2:4])
        if not (1 <= fm <= 12 and 1 <= fd <= 31):
            raise ValueError
    except ValueError:
        fm, fd = 12, 31
    out = []
    # period ends of the last and the current fiscal year at the cadence, oldest first
    pes = []
    for y in (today.year - 1, today.year, today.year + 1):
        pes += [C.add_months_end(C.month_end(y, fm), -k) for k in range(0, 12, months)]
    for pe in sorted(set(pes)):
        is_fy = pe.month == fm
        due = C.on_or_after_weekday(pe + timedelta(days=d_fy if is_fy else d_int))
        if due < today - timedelta(days=3):
            continue
        fp = "FY" if is_fy and months == 6 else ("Q4" if is_fy else None)
        out.append({"period_end": pe, "fiscal_period": fp, "expected_date": due, "expected_ts": None,
                    "session": "unknown", "window_lo": due - timedelta(days=30), "window_hi": due,
                    "date_source": "model_projection", "source_id": "deadline_model", "source_label": LABEL_DEADLINE,
                    "issuer_id": iid, "security_id": m.get("security_id"), "region": m.get("region"),
                    "country": m.get("country"), "sector_id": m.get("sector_id"), "usual_session": "unknown",
                    "n_history": 0})
        if len(out) >= n_next:
            break
    return out


# ------------------------------------------------------------------------------------------------ corporate events


def corporate_from_actions(s: Settings, meta: pl.DataFrame, min_date: date) -> pl.DataFrame:
    sec_iss = meta.filter(pl.col("security_id").is_not_null()).select("security_id", "issuer_id")
    try:
        sm = scan_table("securities_master", s).select("security_id", "issuer_id").unique("security_id").collect()
    except Exception:  # noqa: BLE001
        sm = sec_iss
    ca = (scan_table("corporate_actions", s).filter(pl.col("ex_date") >= min_date)
          .select("security_id", "ex_date", "action_type", "ratio", "amount", "currency", "pay_date", "declared_date",
                  "source_id").collect())
    if ca.is_empty():
        return pl.DataFrame()
    ca = ca.join(sm, on="security_id", how="left")
    typ = {"cash_dividend": "dividend_ex", "special_dividend": "special_dividend", "stock_dividend": "stock_dividend",
           "split": "split", "spinoff": "spinoff", "rights": "rights"}
    ca = ca.filter(pl.col("action_type").is_in(list(typ)))
    amt = pl.col("amount").round(6).cast(pl.Utf8)
    title = (pl.when(pl.col("action_type") == "split").then(pl.lit("Split ") + pl.col("ratio").round(4).cast(pl.Utf8) + pl.lit(":1"))
             .when(pl.col("action_type").is_in(["cash_dividend", "special_dividend"]))
             .then(pl.concat_str([pl.when(pl.col("action_type") == "special_dividend").then(pl.lit("Special dividend"))
                                  .otherwise(pl.lit("Dividend")), pl.lit(" ex-date "), amt.fill_null("?"),
                                  pl.lit(" "), pl.col("currency").fill_null("")]))
             .otherwise(pl.col("action_type").str.replace("_", " ").str.to_titlecase()))
    lbl = pl.concat_str([pl.lit("Corporate actions ("), pl.col("source_id"), pl.lit(")")])
    ex = ca.select(
        (pl.col("action_type").replace_strict(typ) + ":" + pl.col("security_id") + ":" + pl.col("ex_date").cast(pl.Utf8)).alias("event_id"),
        "issuer_id", "security_id", pl.col("ex_date").alias("event_date"),
        pl.col("action_type").replace_strict(typ).alias("event_type"), title.alias("title"), "amount", "currency",
        "ratio", "source_id", lbl.alias("source_label"),
        pl.coalesce("declared_date", "ex_date").alias("knowledge_date"))
    pay = ca.filter(pl.col("pay_date").is_not_null() & pl.col("action_type").is_in(["cash_dividend", "special_dividend"])).select(
        ("dividend_pay:" + pl.col("security_id") + ":" + pl.col("ex_date").cast(pl.Utf8)).alias("event_id"),
        "issuer_id", "security_id", pl.col("pay_date").alias("event_date"), pl.lit("dividend_pay").alias("event_type"),
        pl.concat_str([pl.lit("Dividend payment "), amt.fill_null("?"), pl.lit(" "), pl.col("currency").fill_null("")]).alias("title"),
        "amount", "currency", "ratio", "source_id", lbl.alias("source_label"),
        pl.coalesce("declared_date", "ex_date").alias("knowledge_date"))
    return pl.concat([ex, pay], how="vertical_relaxed")


def corporate_from_sec(sec: pl.DataFrame, meta: pl.DataFrame) -> pl.DataFrame:
    if sec.is_empty():
        return pl.DataFrame()
    sid = dict(meta.select("issuer_id", "security_id").iter_rows())
    rows = []
    for r in sec.filter(pl.col("form").is_in(["8-K", "DEF 14A"])).iter_rows(named=True):
        base = {"issuer_id": r["issuer_id"], "security_id": sid.get(r["issuer_id"]), "event_ts": r["acceptance_ts"],
                "source_id": "sec_edgar", "filing_id": f"sec_edgar:{r['accession']}", "url": r["url"],
                "items": r["items"], "event_date": r["filing_date"], "knowledge_date": r["filing_date"]}
        if r["form"] == "DEF 14A":
            rows.append(base | {"event_id": f"agm_notice:{r['accession']}", "event_type": "agm_notice",
                                "title": "Proxy statement / AGM notice (DEF 14A)", "source_label": "SEC DEF 14A"})
            continue
        for it in r["items"] or []:
            if it in CORP_8K:
                et, title = CORP_8K[it]
                rows.append(base | {"event_id": f"{et}:{r['accession']}:{it}", "event_type": et, "title": title,
                                    "source_label": f"SEC 8-K Item {it} (acceptance time)"})
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def corporate_from_filings_index(s: Settings, meta: pl.DataFrame, min_date: date) -> pl.DataFrame:
    d = (_fi(s).filter((pl.col("knowledge_date") >= min_date)
                       & (((pl.col("source_id") == "tdnet") & pl.col("form_type").is_in(["BUYBACK", "FORECAST_REVISION"]))
                          | ((pl.col("source_id") == "hkex") & pl.col("form_type").is_in(["ANNOUNCEMENT", "RESULTS"]))))
         .select("issuer_id", "filing_id", "source_id", "form_type", "knowledge_date", "knowledge_ts", "url", "items")
         .collect())
    if d.is_empty():
        return pl.DataFrame()
    sid = dict(meta.select("issuer_id", "security_id").iter_rows())
    rows = []
    for r in d.iter_rows(named=True):
        title = (r["items"] or [""])[0] or ""
        cat = (r["items"] or ["", ""])[1] if len(r["items"] or []) > 1 else ""
        et = None
        if r["source_id"] == "tdnet":
            et = "buyback" if r["form_type"] == "BUYBACK" else "guidance_revision"
        else:
            low = (title + " " + cat).lower()
            if "results of agm" in low or "poll results" in low:
                et = "agm_results"
            elif "share buyback" in low or "buy-back" in low or "repurchase" in low:
                et = "buyback"
            elif "board meeting" in low:
                et = "board_meeting"
            elif "general meeting" in low or "[agm" in low or "notice of agm" in low:
                et = "general_meeting"
        if et is None:
            continue
        rows.append({"event_id": f"{et}:{r['filing_id']}", "issuer_id": r["issuer_id"], "security_id": sid.get(r["issuer_id"]),
                     "event_date": r["knowledge_date"], "event_ts": r["knowledge_ts"], "event_type": et,
                     "title": title[:200] or et.replace("_", " "), "source_id": r["source_id"],
                     "source_label": {"tdnet": "TDnet disclosure", "hkex": "HKEXnews announcement"}[r["source_id"]],
                     "filing_id": r["filing_id"], "url": r["url"], "knowledge_date": r["knowledge_date"]})
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


# ------------------------------------------------------------------------------------------------ main


def _announcements(s: Settings) -> pl.DataFrame:
    try:
        return scan_table("earnings_announcements", s).filter(pl.col("event_date") > date(1990, 1, 1)).collect()
    except Exception:  # noqa: BLE001
        return pl.DataFrame()


_JPX_HINT = re.compile(r"^(Q[1-4]) FYE (\d{4}-\d{2}-\d{2})$")
_PE_HINT = re.compile(r"^PE (\d{4}-\d{2}-\d{2})")
_NSM_FP = {"FR": "FY", "IR": "H1", "QRF": "Q1", "QRS": "Q2", "QRT": "Q3"}


def hint_period(hint: str | None) -> tuple[date | None, str | None]:
    """(period_end, fiscal_period) from an earnings_announcements period_hint: JPX 'Q2 FYE 2027-03-31', the generic
    'PE 2026-09-30 | ...' (HKEX BMN, CNINFO) or an NSM 'IR | headline' type (fiscal period only)."""
    from app.earnings.fetch import jpx_period_end  # noqa: PLC0415

    h = hint or ""
    mm = _JPX_HINT.match(h)
    if mm:
        return jpx_period_end(date.fromisoformat(mm.group(2)), mm.group(1)), mm.group(1)
    mm = _PE_HINT.match(h)
    if mm:
        return date.fromisoformat(mm.group(1)), None
    return None, _NSM_FP.get(h.split(" | ", 1)[0].strip())


def build_all(s: Settings, p: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    today = today or C.now_utc().date()
    meta = C.issuer_meta(s)
    mrow = {r["issuer_id"]: r for r in meta.iter_rows(named=True)}
    rep: dict[str, Any] = {}
    sec, rep["sec"] = sec_frames(s, meta, p)
    labels = canonical_labels(s)
    # periodic reports
    periodic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sec_p = sec.filter(pl.col("form").is_in(list(SH.PERIODIC)) & pl.col("report_date").is_not_null())
    for r in sec_p.iter_rows(named=True):
        periodic[r["issuer_id"]].append({"period_end": r["report_date"], "fiscal_period": None, "fiscal_year": None,
                                         "ts": r["acceptance_ts"], "kdate": r["filing_date"], "source_id": "sec_edgar",
                                         "form_type": r["form"], "filing_id": f"sec_edgar:{r['accession']}",
                                         "url": r["url"]})
    have_sec = set(sec_p.get_column("issuer_id").unique().to_list())
    fi_p = periodic_from_filings_index(s)
    for r in fi_p.iter_rows(named=True):
        if r["source_id"] == "sec_edgar" and r["issuer_id"] in have_sec:
            continue
        periodic[r["issuer_id"]].append(r)
    # official release documents
    releases: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in sec.filter((pl.col("form") == "8-K") & pl.col("items").list.contains("2.02").fill_null(False)).iter_rows(named=True):
        releases[r["issuer_id"]].append({"ts": r["acceptance_ts"], "kdate": r["filing_date"], "source_id": "sec_edgar",
                                         "form_type": "8-K", "filing_id": f"sec_edgar:{r['accession']}", "url": r["url"],
                                         "fiscal_period": None})
    fi8 = (_fi(s).filter((pl.col("source_id") == "sec_edgar") & (pl.col("form_type") == "8-K")
                         & pl.col("items").list.contains("2.02").fill_null(False))
           .select("issuer_id", "knowledge_ts", "knowledge_date", "filing_id", "url").collect())
    seen8 = {x["filing_id"] for v in releases.values() for x in v}
    for r in fi8.iter_rows(named=True):
        if r["filing_id"] not in seen8:
            releases[r["issuer_id"]].append({"ts": r["knowledge_ts"], "kdate": r["knowledge_date"], "source_id": "sec_edgar",
                                             "form_type": "8-K", "filing_id": r["filing_id"], "url": r["url"],
                                             "fiscal_period": None})
    for r in releases_from_filings_index(s).iter_rows(named=True):
        releases[r["issuer_id"]].append(r)
    # provider + announced dates
    ann = _announcements(s)
    prov_past: dict[str, list[dict[str, Any]]] = defaultdict(list)
    prov_future: dict[str, list[dict[str, Any]]] = defaultdict(list)
    announced: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if ann.height:
        for r in ann.filter(pl.col("issuer_id").is_not_null()).iter_rows(named=True):
            kind = r.get("event_kind")
            if r["source_id"] == "yahoo":
                (prov_past if r["event_date"] < today else prov_future)[r["issuer_id"]].append(r)
            elif kind == "release" and r["source_id"] in RELEASE_SOURCES:
                if r["event_date"] > today:
                    continue
                pe, fp = hint_period(r.get("period_hint"))
                src = "fca_nsm_results" if r["source_id"] == "fca_nsm" else r["source_id"]
                releases[r["issuer_id"]].append({
                    "ts": r.get("event_ts"), "kdate": r["event_date"], "source_id": src,
                    "form_type": (r.get("period_hint") or "").split(" | ", 1)[0][:20] or None,
                    "filing_id": f"{r['source_id']}:{r['security_id']}:{r['event_date'].isoformat()}",
                    "url": r.get("url"), "fiscal_period": fp, "period_end": pe})
            elif r["event_date"] >= today - timedelta(days=3):
                pe, fp = hint_period(r.get("period_hint"))
                announced[r["issuer_id"]].append(r | {"period_end": pe, "fiscal_period": fp})
    # build per issuer
    ev_rows: list[dict[str, Any]] = []
    cal_rows: list[dict[str, Any]] = []
    ids = set(periodic) | set(releases) | set(prov_past) | set(prov_future) | set(announced)
    for iid in ids:
        m = mrow.get(iid)
        if m is None:
            continue
        evs = build_issuer_events(iid, m, periodic.get(iid, []), releases.get(iid, []),
                                  [x for x in prov_past.get(iid, [])], labels.get(iid, []))
        evs = [e for e in evs if e["release_date"] <= today]
        ev_rows += evs
        if m.get("is_active") is False:
            continue
        cal_rows += project_calendar(iid, m, evs, today, announced.get(iid, []), prov_future.get(iid, []))
    # issuers with no release history and nothing announced: the statutory reporting deadline (W1 2026-10-31)
    if _flag(p, "deadline_projection", True):
        listed = C.universe_securities(["full"], s)
        n0 = len(cal_rows)
        for iid, m in mrow.items():
            if iid in ids or m.get("is_active") is False or m.get("security_id") not in listed:
                continue
            cal_rows += project_deadline(iid, m, today)
        rep["deadline_projections"] = len(cal_rows) - n0
    rep["events_build_s"] = round(time.perf_counter() - t0, 1)
    now = C.now_utc()
    run_id = p.get("_run_id")
    ev = pl.DataFrame(ev_rows, infer_schema_length=None) if ev_rows else pl.DataFrame()
    if ev.height:
        ev = ev.with_columns(pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id"))
    rep["earnings_events"] = C.write(ev, "earnings_events", s, mode="overwrite") if ev.height else 0
    cal = pl.DataFrame(cal_rows, infer_schema_length=None) if cal_rows else pl.DataFrame()
    if cal.height:
        cal = carry_calendar_knowledge(cal.with_columns(pl.lit(today).alias("first_seen_date")), s)
        cal = cal.with_columns(pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id")).unique(
            ["issuer_id", "period_end"], keep="first")
    rep["earnings_calendar"] = C.write(cal, "earnings_calendar", s, mode="overwrite") if cal.height else 0
    # corporate events
    min_date = date(int(p.get("corp_min_year", 2015)), 1, 1)
    parts = [corporate_from_actions(s, meta, min_date), corporate_from_sec(sec, meta),
             corporate_from_filings_index(s, meta, min_date)]
    parts = [x for x in parts if x.height]
    if parts:
        ce = pl.concat(parts, how="diagonal_relaxed").filter(pl.col("event_date").is_not_null()).unique("event_id", keep="first")
        ce = ce.with_columns(pl.col("event_date").dt.year().cast(pl.Int16).alias("year"), pl.lit(now).alias("computed_at"))
        rep["corporate_events"] = C.write(ce, "corporate_events", s, mode="overwrite")
    else:
        rep["corporate_events"] = 0
    if ev.height:
        rep["by_date_source"] = dict(ev.group_by("date_source").len().iter_rows())
        rep["issuers"] = ev.get_column("issuer_id").n_unique()
    if cal.height:
        rep["calendar_by_source"] = dict(cal.group_by("date_source").len().iter_rows())
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    C.update_state("earnings_events", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def carry_calendar_knowledge(cal: pl.DataFrame, s: Settings) -> pl.DataFrame:
    try:
        old = scan_table("earnings_calendar", s).select("issuer_id", "period_end", "expected_date",
                                                        pl.col("first_seen_date").alias("_k0")).collect()
    except Exception:  # noqa: BLE001
        return cal
    if old.is_empty():
        return cal
    out = cal.join(old, on=["issuer_id", "period_end", "expected_date"], how="left")
    return out.with_columns(pl.coalesce("_k0", "first_seen_date").alias("first_seen_date")).drop("_k0")


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = dict(ctx.params or {})
    p["_run_id"] = ctx.run_id
    rep = build_all(ctx.settings, p, today=None)
    st = rep.get("sec") or {}
    return JobResult(status="success" if not st.get("errors") else "partial",
                     rows_written={k: rep.get(k, 0) for k in ("earnings_events", "earnings_calendar", "corporate_events")},
                     entities_ok=int(rep.get("issuers", 0)), entities_failed=int(st.get("errors", 0)),
                     http_requests=int(st.get("fetched", 0)) + int(st.get("older_pages", 0)), message=str(rep)[:2000])
