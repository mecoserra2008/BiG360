"""Official macro release calendar + release outcomes (W2.1). Job `macro_calendar` (nightly).

Sources (official only):
* FRED / ALFRED release dates (St. Louis Fed): `fred/series/release` maps each tracked US series to its FRED release
  (BLS CPI / Employment Situation / PPI / JOLTS, BEA GDP / Personal income and outlays, Census retail sales / housing
  starts / durable goods, Fed G.17, DOL claims, UMich surveys), `fred/release/dates` gives past and scheduled dates
  (include_release_dates_with_no_data=true). FRED gives dates only: the time is the publisher's documented release time
  (RELEASE_TIMES, time_known=false).
* Every other publisher (BLS, BEA, FOMC, Eurostat, ECB, ONS, BoE, BoJ, StatCan, BoC, ABS, RBA, IBGE, BCB, national EU
  offices) comes from app.macro.calendar_sources (one parser per official calendar; failures are skipped).

Tables: macro_releases (calendar, past 3y + 90d) and macro_release_events (one row per release x linked series with the
published value, prior, prior revision). Release outcomes are point-in-time: the first vintage of each period in
macro_observations (ALFRED realtime_start for US vintage series, the realtime knowledge_date elsewhere). A past period
whose first release no official calendar covers gets an `inferred` release row (schedule_source=inferred, dated at the
period's first knowledge date) so surprise history is complete; inferred rows are never shown as upcoming.

Importance tier rule (importance_tier): 1 = GDP, CPI / core CPI / HICP, payrolls / employment, unemployment, policy-rate
decisions; 2 = industrial production, retail sales, PPI, PCE prices, PMI proxies / business confidence, jobless claims,
JOLTS; 3 = everything else.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

WINDOW_PAST_DAYS = 3 * 365 + 30
WINDOW_AHEAD_DAYS = 90
TARGET_COUNTRIES = ("US", "EA", "DE", "FR", "IT", "ES", "PT", "NL", "GB", "CH", "JP", "KR", "CN", "HK", "TW", "IN",
                    "AU", "CA", "BR", "MX", "CL", "CO", "PE", "AR")
#: sources excluded from tracked series (not an official publisher)
NON_OFFICIAL_SOURCES = {"akshare_cn", "akshare", "yfinance"}
MODEL_LABEL = "Model-based expectation (not consensus)"

# ------------------------------------------------------------------------------------------------ tracked series

THEME_OF = {"gdp_qoq": "growth", "gdp_yoy": "growth", "industrial_production": "growth", "retail_sales": "growth",
            "pmi_proxy": "growth", "housing": "growth", "orders": "growth", "sentiment": "growth",
            "cpi_yoy": "inflation", "core_cpi_yoy": "inflation", "ppi_yoy": "inflation", "pce": "inflation",
            "employment": "labor", "unemployment": "labor", "claims": "labor", "jolts": "labor"}
TIER_OF = {"gdp_qoq": 1, "gdp_yoy": 1, "cpi_yoy": 1, "core_cpi_yoy": 1, "employment": 1, "unemployment": 1,
           "industrial_production": 2, "retail_sales": 2, "ppi_yoy": 2, "pce": 2, "pmi_proxy": 2, "claims": 2,
           "jolts": 2, "housing": 3, "orders": 3, "sentiment": 3}
#: indicator keys tracked for every target country (first available official series by catalog rank)
TRACKED_INDICATORS = ("gdp_qoq", "cpi_yoy", "core_cpi_yoy", "ppi_yoy", "industrial_production", "retail_sales",
                      "pmi_proxy", "unemployment", "employment")
YOY_TYPE = {"cpi_yoy", "core_cpi_yoy", "ppi_yoy", "gdp_yoy", "industrial_production", "retail_sales"}


@dataclass(frozen=True)
class Tracked:
    series_id: str
    country: str
    indicator: str          # THEME_OF / TIER_OF key
    measure: str            # level | mom_pct | qoq_pct | diff | yoy_pct
    freq: str               # M | Q | W
    seasonal: int = 0       # SARIMA seasonal period (0 = none)
    direction: int = 1      # +1 higher = stronger/hotter; -1 for unemployment, claims
    title: str = ""
    vintages: bool = False  # knowledge dates observed (ALFRED)
    publisher: str = ""

    @property
    def theme(self) -> str:
        return THEME_OF.get(self.indicator, "growth")

    @property
    def tier(self) -> int:
        return TIER_OF.get(self.indicator, 3)


#: US headline series as released (measure = what the release headline reports)
US_TRACKED: tuple[Tracked, ...] = (
    Tracked("fred:A191RL1Q225SBEA", "US", "gdp_qoq", "level", "Q", title="Real GDP, q/q % SAAR", vintages=True, publisher="BEA"),
    Tracked("fred:PAYEMS", "US", "employment", "diff", "M", title="Nonfarm payrolls, change (thous.)", vintages=True, publisher="BLS"),
    Tracked("fred:UNRATE", "US", "unemployment", "level", "M", direction=-1, title="Unemployment rate", vintages=True, publisher="BLS"),
    Tracked("fred:CPIAUCSL", "US", "cpi_yoy", "mom_pct", "M", title="CPI, m/m %", vintages=True, publisher="BLS"),
    Tracked("fred:CPILFESL", "US", "core_cpi_yoy", "mom_pct", "M", title="Core CPI, m/m %", vintages=True, publisher="BLS"),
    Tracked("fred:PCEPILFE", "US", "pce", "mom_pct", "M", title="Core PCE price index, m/m %", vintages=True, publisher="BEA"),
    Tracked("fred:INDPRO", "US", "industrial_production", "mom_pct", "M", title="Industrial production, m/m %", vintages=True, publisher="Federal Reserve"),
    Tracked("fred:RSAFS", "US", "retail_sales", "mom_pct", "M", title="Retail sales, m/m %", vintages=True, publisher="Census"),
    Tracked("fred:PPIFIS", "US", "ppi_yoy", "mom_pct", "M", title="PPI final demand, m/m %", publisher="BLS"),
    Tracked("fred:ICSA", "US", "claims", "level", "W", direction=-1, title="Initial jobless claims", publisher="DOL"),
    Tracked("fred:JTSJOL", "US", "jolts", "level", "M", title="JOLTS job openings (thous.)", publisher="BLS"),
    Tracked("fred:HOUST", "US", "housing", "level", "M", title="Housing starts (thous. SAAR)", publisher="Census"),
    Tracked("fred:DGORDER", "US", "orders", "mom_pct", "M", title="Durable goods orders, m/m %", publisher="Census"),
    Tracked("fred:UMCSENT", "US", "sentiment", "level", "M", title="UMich consumer sentiment", publisher="University of Michigan (via FRED)"),
    Tracked("oecd:CLI.USA.LI", "US", "pmi_proxy", "level", "M", title="OECD CLI (US)", publisher="OECD"),
)

PUBLISHER_OF_SOURCE = {"fred": "FRED", "ecb": "ECB", "eurostat": "Eurostat", "ons": "ONS", "boe": "Bank of England",
                       "boj": "Bank of Japan", "statcan": "Statistics Canada", "boc": "Bank of Canada", "abs": "ABS",
                       "rba": "RBA", "ibge": "IBGE", "bcb": "BCB", "oecd": "OECD", "snb": "SNB", "bok_ecos": "Bank of Korea",
                       "censtatd": "C&SD Hong Kong", "cbc_tw": "CBC Taiwan", "bcrp": "BCRP", "ine_cl": "INE Chile",
                       "datos_ar": "INDEC (datos.gob.ar)", "bcra": "BCRA", "insee": "INSEE", "ine_es": "INE",
                       "bundesbank": "Bundesbank", "bpstat": "Banco de Portugal", "banxico": "Banxico", "imf": "IMF",
                       "bis": "BIS", "worldbank": "World Bank", "mof_jp": "MoF Japan", "hkma": "HKMA", "banrep": "BanRep"}
#: publisher tz + documented default release time (local) when a calendar gives only a date
PUBLISHER_TZ = {"US": ("America/New_York", (8, 30)), "EA": ("Europe/Luxembourg", (11, 0)), "DE": ("Europe/Berlin", (8, 0)),
                "FR": ("Europe/Paris", (8, 45)), "IT": ("Europe/Rome", (10, 0)), "ES": ("Europe/Madrid", (9, 0)),
                "PT": ("Europe/Lisbon", (11, 0)), "NL": ("Europe/Amsterdam", (8, 30)), "GB": ("Europe/London", (7, 0)),
                "CH": ("Europe/Zurich", (8, 30)), "JP": ("Asia/Tokyo", (8, 50)), "KR": ("Asia/Seoul", (8, 0)),
                "CN": ("Asia/Shanghai", (9, 30)), "HK": ("Asia/Hong_Kong", (16, 30)), "TW": ("Asia/Taipei", (16, 0)),
                "IN": ("Asia/Kolkata", (17, 30)), "AU": ("Australia/Sydney", (11, 30)), "CA": ("America/Toronto", (8, 30)),
                "BR": ("America/Sao_Paulo", (9, 0)), "MX": ("America/Mexico_City", (8, 0)), "CL": ("America/Santiago", (8, 0)),
                "CO": ("America/Bogota", (10, 0)), "PE": ("America/Lima", (10, 0)), "AR": ("America/Argentina/Buenos_Aires", (16, 0))}
#: FRED release id -> documented local release time (America/New_York)
RELEASE_TIMES = {10: (8, 30), 50: (8, 30), 53: (8, 30), 54: (8, 30), 13: (9, 15), 9: (8, 30), 180: (8, 30), 46: (8, 30),
                 192: (10, 0), 27: (8, 30), 91: (10, 0), 95: (10, 0), 86: (8, 30)}


def _catalog_rows(settings: Settings | None = None) -> list[Any]:
    try:
        from app.ingestion.macro_common import load_catalog  # noqa: PLC0415

        return load_catalog(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("macro catalog unavailable: %s", e)
        return []


def tracked_series(settings: Settings | None = None, series_meta: pl.DataFrame | None = None) -> list[Tracked]:
    """US headline list + for every other target country the best-ranked official series of each tracked indicator
    that has data in the last 400 days (catalog rank order; non-official scrapers excluded)."""
    from app.core.storage import read_table  # noqa: PLC0415

    meta = series_meta if series_meta is not None else read_table(
        "macro_series", columns=["series_id", "last_observation_date", "frequency", "seasonal_adjustment", "scale", "title"])
    info = {r["series_id"]: r for r in meta.iter_rows(named=True)}
    cutoff = date.today() - timedelta(days=400)
    out: list[Tracked] = list(US_TRACKED)
    have = {(t.country, t.indicator) for t in out} | {("US", i) for i in TRACKED_INDICATORS}
    best: dict[tuple[str, str], tuple[int, Any]] = {}
    for g in _catalog_rows(settings):
        if g.country not in TARGET_COUNTRIES or g.source_id in NON_OFFICIAL_SOURCES:
            continue
        m = info.get(g.series_id)
        if not m or not m["last_observation_date"] or m["last_observation_date"] < cutoff:
            continue
        if (m["frequency"] or g.frequency) not in ("M", "Q"):
            continue
        for ind in (g.indicator or "").split(","):
            if ind not in TRACKED_INDICATORS or (g.country, ind) in have:
                continue
            k = (g.country, ind)
            if k not in best or g.rank < best[k][0]:
                best[k] = (g.rank, g)
    for (cc, ind), (_, g) in sorted(best.items()):
        m = info[g.series_id]
        freq = m["frequency"] or g.frequency
        scale = (m.get("scale") or g.scale or "").lower()
        sa = (m.get("seasonal_adjustment") or g.sa or "").upper()
        if ind == "employment" and scale == "level":
            measure = "diff"
        elif ind in YOY_TYPE and scale in ("index", "level"):
            measure = "yoy_pct"
        else:
            measure = "level"
        seasonal = (12 if freq == "M" else 4) if (sa == "NSA" and measure in ("diff", "level")
                                                  and ind not in YOY_TYPE and ind != "pmi_proxy") else 0
        out.append(Tracked(g.series_id, cc, ind, measure, freq, seasonal, -1 if ind == "unemployment" else 1,
                           m.get("title") or g.title, False, PUBLISHER_OF_SOURCE.get(g.source_id, g.source_id)))
    return out


# ------------------------------------------------------------------------------------------------ periods & measures


def period_end(d: date, freq: str) -> date:
    if freq == "M":
        return (date(d.year + (d.month == 12), d.month % 12 + 1, 1)) - timedelta(days=1)
    if freq == "Q":
        m = ((d.month - 1) // 3 + 1) * 3
        return date(d.year + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    if freq == "W":
        return d
    return date(d.year, 12, 31)


def next_period(d: date, freq: str) -> date:
    if freq == "M":
        return date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    if freq == "Q":
        m = d.month + 3
        return date(d.year + (m > 12), (m - 1) % 12 + 1, 1)
    if freq == "W":
        return d + timedelta(days=7)
    return date(d.year + 1, 1, 1)


def season_len(freq: str) -> int:
    return {"M": 12, "Q": 4, "W": 52}.get(freq, 1)


def apply_measure(s: pd.Series, measure: str, freq: str) -> pd.Series:
    """Raw level series (indexed by period, sorted, regular) -> release measure."""
    if measure == "level":
        return s
    if measure == "diff":
        return s.diff()
    if measure in ("mom_pct", "qoq_pct"):
        return 100.0 * (s / s.shift(1) - 1.0)
    if measure == "yoy_pct":
        return 100.0 * (s / s.shift(season_len(freq)) - 1.0)
    raise ValueError(measure)


def regularize(s: pd.Series, freq: str) -> pd.Series:
    """Reindex to a complete period grid (missing periods = NaN) so lags are period lags."""
    if s.empty:
        return s
    idx = [s.index.min()]
    while idx[-1] < s.index.max():
        idx.append(next_period(idx[-1], freq))
    return s.reindex(idx)


# ------------------------------------------------------------------------------------------------ point-in-time obs


def load_obs(series_ids: Iterable[str], settings: Settings | None = None) -> dict[str, pd.DataFrame]:
    """series_id -> DataFrame[date, value, knowledge_date, est] (all vintages), sorted."""
    from app.core.storage import scan_table  # noqa: PLC0415

    ids = sorted(set(series_ids))
    if not ids:
        return {}
    df = (scan_table("macro_observations", settings)
          .filter(pl.col("series_id").is_in(ids) & pl.col("value").is_not_null())
          .select("series_id", "date", "value", "knowledge_date", "knowledge_date_estimated")
          .collect())
    out: dict[str, pd.DataFrame] = {}
    for (sid,), g in df.group_by(["series_id"]):
        p = g.drop("series_id").rename({"knowledge_date_estimated": "est"}).sort("date", "knowledge_date").to_pandas()
        p["date"] = pd.to_datetime(p["date"]).dt.date
        p["knowledge_date"] = pd.to_datetime(p["knowledge_date"]).dt.date
        out[sid] = p.reset_index(drop=True)
    return out


def asof_series(obs: pd.DataFrame, cutoff: date, freq: str, strict: bool = True) -> pd.Series:
    """Raw value per period as known at `cutoff` (knowledge_date < cutoff if strict else <= cutoff)."""
    if obs is None or obs.empty:
        return pd.Series(dtype=float)
    m = obs["knowledge_date"] < cutoff if strict else obs["knowledge_date"] <= cutoff
    o = obs[m]
    if o.empty:
        return pd.Series(dtype=float)
    o = o.sort_values(["date", "knowledge_date"]).drop_duplicates("date", keep="last")
    s = pd.Series(o["value"].to_numpy(dtype=float), index=list(o["date"]))
    return regularize(s, freq)


def first_releases(obs: pd.DataFrame) -> pd.DataFrame:
    """Per period: first knowledge_date, its value and whether it was estimated."""
    o = obs.sort_values(["date", "knowledge_date"]).drop_duplicates("date", keep="first")
    return o[["date", "knowledge_date", "value", "est"]].reset_index(drop=True)


# ------------------------------------------------------------------------------------------------ helpers


def importance_tier(name: str, kind: str = "data") -> int:
    """Documented tier rule for calendar entries not linked to a tracked series."""
    if kind == "policy_meeting":
        return 1
    n = name.lower()
    if re.search(r"gross domestic|\bgdp\b|consumer price|\bcpi\b|\bhicp\b|inflation|employment situation|payroll|"
                 r"unemployment|labour market|labor force|labour force|ipca\b|monetary policy|interest rate", n):
        return 1
    if re.search(r"industrial production|retail|producer price|\bppi\b|\bpmi\b|business (climate|confidence|survey)|"
                 r"sentiment|jobless|claims|job openings|jolts|personal income|tankan|industrial output", n):
        return 2
    return 3


def release_event_id(source_id: str, release_key: str, d: date) -> str:
    return "RE" + hashlib.sha1(f"{source_id}|{release_key}|{d.isoformat()}".encode()).hexdigest()[:16]


def _local(d: date, hm: tuple[int, int], tz: str) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=ZoneInfo(tz))


@dataclass
class Entry:
    """A calendar entry normalized for the macro_releases table."""
    source_id: str
    release_key: str
    release_name: str
    publisher: str
    country: str
    kind: str
    local: datetime
    tz: str
    time_known: bool
    schedule_source: str = "official_calendar"
    reference_period: str | None = None
    url: str | None = None
    series_ids: list[str] = field(default_factory=list)
    importance: int = 3

    @property
    def release_date(self) -> date:
        return self.local.date()

    @property
    def rid(self) -> str:
        return release_event_id(self.source_id, self.release_key, self.release_date)


# ------------------------------------------------------------------------------------------------ FRED calendar


def fred_release_map(tracked: list[Tracked], fc: Any) -> dict[int, tuple[str, list[str]]]:
    """FRED release_id -> (release name, [series_ids]) for the tracked fred: series."""
    out: dict[int, tuple[str, list[str]]] = {}
    for t in tracked:
        if not t.series_id.startswith("fred:"):
            continue
        try:
            rel = (fc._get("series/release", series_id=t.series_id.split(":", 1)[1]).get("releases") or [{}])[0]
        except Exception as e:  # noqa: BLE001
            log.warning("fred series/release %s: %s", t.series_id, e)
            continue
        if rel.get("id") is None:
            continue
        name, ids = out.get(int(rel["id"]), (rel.get("name") or "", []))
        out[int(rel["id"])] = (name, ids + [t.series_id])
    return out


def fetch_fred_calendar(tracked: list[Tracked], start: date, end: date, settings: Settings | None = None
                        ) -> tuple[list[Entry], dict[str, Any]]:
    from app.ingestion.fred import FredClient  # noqa: PLC0415

    t0 = time.perf_counter()
    fc = FredClient(settings)
    rel = fred_release_map(tracked, fc)
    entries: list[Entry] = []
    for rid, (name, sids) in rel.items():
        try:
            r = fc._get("release/dates", release_id=rid, realtime_start=start.isoformat(), realtime_end="9999-12-31",
                        include_release_dates_with_no_data="true", limit=10000, sort_order="asc")
        except Exception as e:  # noqa: BLE001
            log.warning("fred release/dates %s: %s", rid, e)
            continue
        hm = RELEASE_TIMES.get(rid, (8, 30))
        for x in r.get("release_dates", []):
            d = date.fromisoformat(x["date"])
            if not (start <= d <= end):
                continue
            entries.append(Entry("fred_releases", f"fred:{rid}", name, "FRED (St. Louis Fed)", "US", "data",
                                 _local(d, hm, "America/New_York"), "America/New_York", False,
                                 url=f"https://fred.stlouisfed.org/releases/{rid}", series_ids=list(sids)))
    return entries, {"status": "ok", "n": len(entries), "releases": {str(k): v[0] for k, v in rel.items()},
                     "seconds": round(time.perf_counter() - t0, 1), "requests": fc.http.n_requests}


def official_entries(start: date, end: date, settings: Settings | None = None) -> tuple[list[Entry], dict[str, Any]]:
    """Non-FRED official calendars from app.macro.calendar_sources (skipped gracefully if unavailable)."""
    try:
        from app.macro import calendar_sources as cs  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        return [], {"calendar_sources": {"status": "failed", "error": f"import: {e}"}}
    raw, report = cs.fetch_all(start, end, settings)
    out = []
    for c in raw:
        out.append(Entry(c.source_id, c.native_key, c.name, c.publisher, c.country, c.kind, c.scheduled_local, c.tz,
                         bool(c.time_known), reference_period=c.reference_period, url=c.url))
    return out, report


# ------------------------------------------------------------------------------------------------ linking

#: indicator -> regex on calendar entry names (lower case). Used for non-FRED calendars.
LINK_RULES: dict[str, str] = {
    "gdp_qoq": r"gross domestic product|\bgdp\b|national accounts|quarterly national|pib\b|producto interno|"
               r"contas nacionais|preliminary gdp|gdp flash|economic growth",
    "cpi_yoy": r"consumer price|\bcpi\b|\bhicp\b|inflation|ipca\b|índice de precios al consumo|prix à la consommation|"
               r"verbraucherpreis|prezzi al consumo",
    "core_cpi_yoy": r"consumer price|\bcpi\b|\bhicp\b|inflation|ipca\b",
    "ppi_yoy": r"producer price|\bppi\b|industrial producer|corporate goods price|prices of industrial|"
               r"índices de preços ao produtor|erzeugerpreis",
    "industrial_production": r"industrial production|industrial output|production industrielle|produção industrial|"
                             r"produzione industriale|produktion im produzierenden|index of production|monthly gdp",
    "retail_sales": r"retail|commerce de détail|comércio|commercio al dettaglio|einzelhandel|retail trade",
    "unemployment": r"unemployment|labour market|labor force|labour force|employment situation|desemprego|paro|"
                    r"chômage|disoccup|arbeitsmarkt|pnad",
    "employment": r"employment|labour market|labor force|labour force|payroll|pnad|empleo|emploi",
    "pmi_proxy": r"economic sentiment|business (climate|confidence|survey|tendency)|composite leading|tankan|"
                 r"business conditions",
}
#: per-publisher period-assignment tolerance: an official entry is attached to a period first seen at kd0 when its date
#: is within [kd0 - before, kd0 + after] (estimated knowledge dates are imprecise -> wide window).
#: The entry may never be dated AFTER the first knowledge date (data cannot be known before it is published); 'before'
#: absorbs ALFRED stamping the next day (observed) and imputed release lags (estimated).
TOL_OBSERVED = (1, 0)
TOL_ESTIMATED = (40, 0)
#: invariant tolerance: first_seen_at may precede scheduled_at by at most this much (time-zone / stamping slack)
FIRST_SEEN_TOLERANCE = timedelta(hours=24)


def link_entries(entries: list[Entry], tracked: list[Tracked]) -> None:
    """Attach tracked series ids to non-FRED official entries (in place) by country + name rules. Eurostat euro-area
    releases also carry the member-state Eurostat series of the same indicator (published in the same release)."""
    by_cc: dict[str, list[Tracked]] = {}
    for t in tracked:
        by_cc.setdefault(t.country, []).append(t)
    for e in entries:
        if e.source_id == "fred_releases" or e.kind != "data":
            continue
        name = e.release_name.lower()
        cands = list(by_cc.get(e.country, []))
        if e.publisher.lower().startswith("eurostat"):
            cands += [t for t in tracked if t.series_id.startswith("eurostat:") and t.country != e.country]
        for t in cands:
            if t.series_id.startswith("fred:"):
                continue  # US fred series are linked through FRED release ids
            rx = LINK_RULES.get(t.indicator)
            if rx and re.search(rx, name) and t.series_id not in e.series_ids:
                if t.indicator == "core_cpi_yoy" and re.search(r"flash", name) and e.country != "EA":
                    continue
                e.series_ids.append(t.series_id)


#: FRED release id -> regex identifying the same release in the BLS / BEA calendars (which carry exact times)
FRED_TWINS = {10: r"consumer price index", 50: r"employment situation", 53: r"gross domestic product|\bgdp\b",
              54: r"personal income", 46: r"producer price index", 192: r"job openings", 9: r"retail",
              27: r"residential construction", 95: r"manufacturers|durable goods", 51: r"international trade"}


def merge_fred_twins(entries: list[Entry]) -> list[Entry]:
    """A FRED release date that BLS / BEA also list on the same day is the same release: its linked series move to the
    publisher's entry (exact time) and the FRED row is dropped. FRED rows remain where the publisher calendar has no
    history (BLS ICS covers 2025+, BEA 2025+)."""
    by_day: dict[date, list[Entry]] = {}
    for e in entries:
        if e.country == "US" and e.source_id in ("bls_cal", "bea_cal"):
            by_day.setdefault(e.release_date, []).append(e)
    out = []
    for e in entries:
        if e.source_id == "fred_releases":
            rid = int(e.release_key.split(":")[1])
            rx = FRED_TWINS.get(rid)
            twin = next((o for o in by_day.get(e.release_date, []) if rx and re.search(rx, o.release_name.lower())), None)
            if twin is not None:
                twin.series_ids += [s for s in e.series_ids if s not in twin.series_ids]
                continue
        out.append(e)
    return out


def keep_entry(e: Entry) -> bool:
    """Calendar noise filter: keep linked releases, policy meetings and tier 1-2 releases (statistical offices also list
    hundreds of minor publications a year)."""
    return bool(e.series_ids) or e.kind == "policy_meeting" or e.importance <= 2 or e.source_id == "fred_releases"


def assign_importance(entries: list[Entry], tracked_by_id: dict[str, Tracked]) -> None:
    for e in entries:
        tiers = [tracked_by_id[s].tier for s in e.series_ids if s in tracked_by_id]
        e.importance = min(tiers) if tiers else importance_tier(e.release_name, e.kind)


# ------------------------------------------------------------------------------------------------ release events


def _measure_at(obs: pd.DataFrame, cutoff: date, t: Tracked, strict: bool) -> pd.Series:
    return apply_measure(asof_series(obs, cutoff, t.freq, strict), t.measure, t.freq)


def build_events(t: Tracked, obs: pd.DataFrame | None, entries: list[Entry], start: date, today: date,
                 now: datetime) -> tuple[list[dict[str, Any]], list[Entry]]:
    """Release events of one tracked series. Returns (event rows, inferred Entry rows to add to the calendar)."""
    rows: list[dict[str, Any]] = []
    inferred: list[Entry] = []
    mine = sorted([e for e in entries if t.series_id in e.series_ids], key=lambda e: e.local)
    used: set[str] = set()
    tz, hm = PUBLISHER_TZ.get(t.country, ("UTC", (9, 0)))
    last_period: date | None = None
    if obs is not None and not obs.empty:
        fr = first_releases(obs)
        fr = fr[fr["knowledge_date"] >= start]
        for r in fr.itertuples(index=False):
            kd0, period, est = r.knowledge_date, r.date, bool(r.est)
            if kd0 > today:
                continue
            before, after = TOL_ESTIMATED if est else TOL_OBSERVED
            best = None
            for e in mine:
                if e.rid in used or e.local.astimezone(UTC) > now:   # a future release cannot have published it
                    continue
                dd = (e.release_date - kd0).days
                if -before <= dd <= after and e.release_date > period and (
                        best is None or abs(dd) < abs((best.release_date - kd0).days)):
                    best = e
            if best is None:
                best = Entry("inferred", t.series_id, f"{t.title or t.series_id} (inferred release)", t.publisher or "",
                             t.country, "data", _local(kd0, hm, tz), tz, False, schedule_source="inferred",
                             series_ids=[t.series_id], importance=t.tier)
                inferred.append(best)
            used.add(best.rid)
            rel_day = best.release_date
            # estimated knowledge dates can sit after the official date: read 'before' at the earlier and 'after' at
            # the later of the two so the release's own period is always visible in `cur`
            prev = _measure_at(obs, min(rel_day, kd0), t, strict=True)
            cur = _measure_at(obs, max(rel_day, kd0), t, strict=False)
            pp = period
            prior_p = None
            # previous period on the grid
            if not cur.empty and pp in cur.index:
                i = list(cur.index).index(pp)
                prior_p = cur.index[i - 1] if i > 0 else None
            actual = float(cur.get(pp)) if pp in cur.index and pd.notna(cur.get(pp)) else None
            prior = float(prev.get(prior_p)) if prior_p is not None and prior_p in prev.index and pd.notna(prev.get(prior_p)) else None
            prior_rev = float(cur.get(prior_p)) if prior_p is not None and pd.notna(cur.get(prior_p)) else None
            rows.append({
                "release_event_id": best.rid, "series_id": t.series_id, "country": t.country, "theme": t.theme,
                "measure": t.measure, "release_date": rel_day, "scheduled_at": best.local.astimezone(UTC),
                "period": period, "status": "released" if actual is not None else "not_published",
                "actual": actual, "actual_raw": float(r.value), "prior": prior, "prior_revised": prior_rev,
                "prior_revision": (prior_rev - prior) if prior is not None and prior_rev is not None else None,
                "knowledge_date": kd0, "knowledge_date_estimated": est,
                "first_seen_at": best.local.astimezone(UTC), "computed_at": now})
            last_period = period if last_period is None or period > last_period else last_period
    # pending: future official entries -> next periods (at most one entry per expected period)
    nxt = next_period(last_period, t.freq) if last_period else None
    last_assigned: date | None = None
    min_gap = {"M": 20, "Q": 60, "W": 5}.get(t.freq, 20)
    for e in mine:
        if e.rid in used or e.local.astimezone(UTC) <= now:
            continue
        if nxt is None or period_end(nxt, t.freq) >= e.release_date:
            continue
        if last_assigned and (e.release_date - last_assigned).days < min_gap:
            continue
        cur = _measure_at(obs, e.release_date, t, strict=True) if obs is not None else pd.Series(dtype=float)
        prior = float(cur.iloc[-1]) if not cur.empty and pd.notna(cur.iloc[-1]) else None
        rows.append({
            "release_event_id": e.rid, "series_id": t.series_id, "country": t.country, "theme": t.theme,
            "measure": t.measure, "release_date": e.release_date, "scheduled_at": e.local.astimezone(UTC),
            "period": nxt, "status": "pending", "actual": None, "actual_raw": None, "prior": prior,
            "prior_revised": None, "prior_revision": None, "knowledge_date": None, "knowledge_date_estimated": None,
            "first_seen_at": None, "computed_at": now})
        used.add(e.rid)
        last_assigned = e.release_date
        nxt = next_period(nxt, t.freq)
    return rows, inferred


# ------------------------------------------------------------------------------------------------ frames


def entries_frame(entries: list[Entry], now: datetime) -> pl.DataFrame:
    rows = []
    for e in entries:
        rows.append({"release_event_id": e.rid, "source_id": e.source_id, "release_key": e.release_key,
                     "release_name": e.release_name, "publisher": e.publisher or "", "country": e.country,
                     "kind": e.kind, "scheduled_at": e.local.astimezone(UTC), "scheduled_tz": e.tz,
                     "scheduled_local": e.local.isoformat(), "release_date": e.release_date, "time_known": e.time_known,
                     "schedule_source": e.schedule_source, "importance": int(e.importance),
                     "series_ids": sorted(set(e.series_ids)), "reference_period": e.reference_period, "url": e.url,
                     "first_seen_at": now, "updated_at": now})
    schema = {"release_event_id": pl.Utf8, "source_id": pl.Utf8, "release_key": pl.Utf8, "release_name": pl.Utf8,
              "publisher": pl.Utf8, "country": pl.Utf8, "kind": pl.Utf8, "scheduled_at": pl.Datetime("us", "UTC"),
              "scheduled_tz": pl.Utf8, "scheduled_local": pl.Utf8, "release_date": pl.Date, "time_known": pl.Boolean,
              "schedule_source": pl.Utf8, "importance": pl.Int8, "series_ids": pl.List(pl.Utf8),
              "reference_period": pl.Utf8, "url": pl.Utf8, "first_seen_at": pl.Datetime("us", "UTC"),
              "updated_at": pl.Datetime("us", "UTC")}
    return pl.DataFrame(rows, schema=schema).unique("release_event_id", keep="first")


def merge_history(new: pl.DataFrame, old: pl.DataFrame, today: date) -> pl.DataFrame:
    """Keep first_seen_at of known rows; keep past rows that sources no longer list (future-only calendars); drop old
    future rows that disappeared (rescheduled / cancelled)."""
    if old.is_empty():
        return new
    fs = old.select("release_event_id", pl.col("first_seen_at").alias("_fs"))
    new = new.join(fs, on="release_event_id", how="left").with_columns(
        pl.coalesce("_fs", "first_seen_at").alias("first_seen_at")).drop("_fs")
    keep_old = old.filter((pl.col("release_date") < today) & ~pl.col("release_event_id").is_in(new["release_event_id"])
                          & (pl.col("schedule_source") == "official_calendar"))
    return pl.concat([new, keep_old.select(new.columns)], how="vertical_relaxed")


def invariant_violations(ev: pl.DataFrame, now: datetime) -> pl.DataFrame:
    """Rows breaking the release-outcome invariants: an actual on a release scheduled in the future, an upcoming
    (pending) row with an actual, or first_seen_at earlier than scheduled_at - FIRST_SEEN_TOLERANCE."""
    if ev.is_empty():
        return ev
    return ev.filter((pl.col("actual").is_not_null() & (pl.col("scheduled_at") > now))
                     | ((pl.col("status") == "pending") & pl.col("actual").is_not_null())
                     | (pl.col("first_seen_at") < pl.col("scheduled_at") - FIRST_SEEN_TOLERANCE).fill_null(False))


def events_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    schema = {"release_event_id": pl.Utf8, "series_id": pl.Utf8, "country": pl.Utf8, "theme": pl.Utf8,
              "measure": pl.Utf8, "release_date": pl.Date, "scheduled_at": pl.Datetime("us", "UTC"), "period": pl.Date,
              "status": pl.Utf8, "actual": pl.Float64, "actual_raw": pl.Float64, "prior": pl.Float64,
              "prior_revised": pl.Float64, "prior_revision": pl.Float64, "knowledge_date": pl.Date,
              "knowledge_date_estimated": pl.Boolean, "first_seen_at": pl.Datetime("us", "UTC"),
              "computed_at": pl.Datetime("us", "UTC")}
    return pl.DataFrame(rows, schema=schema).unique(["release_event_id", "series_id"], keep="first")


# ------------------------------------------------------------------------------------------------ job


def build_calendar(settings: Settings | None = None, today: date | None = None, write: bool = True,
                   official: bool = True) -> dict[str, Any]:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    s = settings or get_settings()
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    start, end = today - timedelta(days=WINDOW_PAST_DAYS), today + timedelta(days=WINDOW_AHEAD_DAYS)
    tracked = tracked_series(s)
    by_id = {t.series_id: t for t in tracked}
    report: dict[str, Any] = {"window": [start.isoformat(), end.isoformat()], "tracked": len(tracked), "sources": {}}
    entries: list[Entry] = []
    try:
        fe, rep = fetch_fred_calendar(tracked, start, end, s)
        entries += fe
        report["sources"]["fred_releases"] = rep
    except Exception as e:  # noqa: BLE001
        report["sources"]["fred_releases"] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    if official:
        oe, rep = official_entries(start, end, s)
        entries += [e for e in oe if start <= e.release_date <= end]
        report["sources"].update(rep)
    link_entries(entries, tracked)
    entries = merge_fred_twins(entries)
    assign_importance(entries, by_id)
    n0 = len(entries)
    entries = [e for e in entries if keep_entry(e)]
    report["filtered_minor"] = n0 - len(entries)
    obs = load_obs(by_id, s)
    ev_rows: list[dict[str, Any]] = []
    inferred: list[Entry] = []
    for t in tracked:
        r, inf = build_events(t, obs.get(t.series_id), entries, start, today, now)
        ev_rows += r
        inferred += inf
    allent = entries + inferred
    rel = entries_frame(allent, now)
    ev = events_frame(ev_rows)
    bad = invariant_violations(ev, now)
    report["invariant_violations"] = bad.height
    if bad.height:  # never publish them (logged); the matcher should make this impossible
        log.error("macro_calendar: %d release events violate invariants, dropped: %s", bad.height,
                  bad.select("series_id", "release_date").head(5).to_dicts())
        ev = ev.join(bad.select("release_event_id", "series_id"), on=["release_event_id", "series_id"], how="anti")
    report["releases"] = rel.height
    report["events"] = ev.height
    report["inferred_releases"] = len(inferred)
    report["upcoming"] = rel.filter((pl.col("release_date") >= today) & (pl.col("schedule_source") == "official_calendar")
                                    ).group_by("source_id").len().sort("source_id").to_dicts()
    if write:
        try:
            old = read_table("macro_releases")
        except Exception:  # noqa: BLE001
            old = rel.clear()
        rel = merge_history(rel, old, today)
        report["rows_releases"] = write_table(rel, "macro_releases", mode="overwrite", settings=s)
        report["rows_events"] = write_table(_keep_first_seen(ev, s, now), "macro_release_events", mode="overwrite",
                                            settings=s)
    return report


def _keep_first_seen(ev: pl.DataFrame, s: Settings, now: datetime) -> pl.DataFrame:
    """Live first-seen: a release that was 'pending' in the previous run and is 'released' now is stamped with now;
    a previously stored first_seen_at is preserved."""
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        old = read_table("macro_release_events", columns=["release_event_id", "series_id", "status", "first_seen_at"],
                         settings=s)
    except Exception:  # noqa: BLE001
        return ev
    if old.is_empty():
        return ev
    j = ev.join(old.rename({"status": "_ost", "first_seen_at": "_ofs"}), on=["release_event_id", "series_id"], how="left")
    return j.with_columns(
        pl.when(pl.col("status") != "released").then(pl.lit(None, dtype=pl.Datetime("us", "UTC")))
        .when(pl.col("_ofs").is_not_null() & (pl.col("_ost") == "released")).then(pl.col("_ofs"))
        .when((pl.col("_ost") == "pending") & (pl.col("status") == "released")).then(pl.lit(now))
        .otherwise(pl.col("first_seen_at")).alias("first_seen_at")).drop("_ost", "_ofs")


#: calendar sources whose failure only logs a warning (job stays success); params.warn_sources overrides
WARN_ONLY_SOURCES = ("ine_cal",)


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_calendar. params: official (bool, default true)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    if not ctx.settings.secret("fred_apikey"):
        ctx.log.warning("FRED_APIKEY missing: US release dates skipped")
    rep = build_calendar(ctx.settings, official=bool(p.get("official", True)))
    from app.ingestion.macro_common import _save_report  # noqa: PLC0415

    _save_report({"job": "macro_calendar", **rep}, ctx.settings)
    failed = [k for k, v in rep["sources"].items() if isinstance(v, dict) and v.get("status") == "failed"]
    # optional publisher calendars (large files on slow/flaky hosts): a failure is a warning, not a partial run
    warn = set(p.get("warn_sources", WARN_ONLY_SOURCES))
    hard = [k for k in failed if k not in warn]
    if failed and not hard:
        ctx.log.warning("macro_calendar: optional calendar source(s) failed (warning only): %s", failed)
    return JobResult(status="partial" if hard else "success",
                     rows_written={"macro_releases": rep.get("rows_releases", 0),
                                   "macro_release_events": rep.get("rows_events", 0)},
                     entities_ok=len(rep["sources"]) - len(failed), entities_failed=len(failed),
                     message=f"releases={rep['releases']} events={rep['events']} failed_sources={failed}")
