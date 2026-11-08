"""CFTC Commitments of Traders (official) -> cftc_positions + cftc_markets. Jobs `cftc_cot` (weekly) / `cftc_cot_history`.

Source: CFTC Public Reporting Environment (Socrata SODA API, https://publicreporting.cftc.gov/resource/<id>.csv).
Datasets (DATASETS): Legacy, Disaggregated, Traders in Financial Futures - each futures-only and futures+options
combined - plus the Supplemental (commodity index traders) report. Paged CSV ($order=:id, 50k rows per request,
explicit $select of the 'All' crop-year columns), landed gzipped under data/raw/cftc/<report_type>/dt=<day>/.

Point in time: reports carry Tuesday positions released Friday 15:30 ET. knowledge_ts = the row's Socrata
`:created_at` (observed first publication) when it is plausible - within 70 days after the scheduled release and not
part of a bulk load (a created_at day shared by > 3 report dates, e.g. the 2022-09-13 platform migration) - else the
scheduled release (release_schedule()) with knowledge_date_estimated = true.

Incremental: since = watermark (last report date) - 28 days (CFTC republishes corrections); full history when no
watermark or params.full = true. Upsert on (report_type, market_code, report_date, category).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.core.http import HttpClient
from app.core.ratelimit import RateLimit
from app.core.storage import read_table, scan_table, write_table
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter
from app.ingestion.cftc_mapping import CURATED, asset_class_for, name_match, primary_report_for, title_name

SOURCE = "cftc"
BASE = "https://publicreporting.cftc.gov/resource"
ET = ZoneInfo("America/New_York")
PAGE = 50_000
REVISION_DAYS = 28

DATASETS: dict[str, str] = {
    "legacy_fut": "6dca-aqww", "legacy_comb": "jun7-fc8e",
    "disagg_fut": "72hh-3qpy", "disagg_comb": "kh3c-gbw2",
    "tff_fut": "gpe5-46if", "tff_comb": "yw9f-hn96",
    "cit_comb": "4zgm-a668",
}
REPORT_TYPES = tuple(DATASETS)

META_COLS = ("cftc_contract_market_code", "report_date_as_yyyy_mm_dd", "market_and_exchange_names", "contract_market_name",
             "cftc_commodity_code", "commodity_name", "commodity_group_name", "commodity_subgroup_name", "contract_units")

# category -> (long, short, spread, chg_long, chg_short, chg_spread, pct_long, pct_short, pct_spread,
#              traders_long, traders_short, traders_spread); None = not published for that category
Spec = tuple[str | None, ...]
_TOT: Spec = ("tot_rept_positions_long_all", "tot_rept_positions_short", None, "change_in_tot_rept_long_all",
              "change_in_tot_rept_short", None, "pct_of_oi_tot_rept_long_all", "pct_of_oi_tot_rept_short", None,
              "traders_tot_rept_long_all", "traders_tot_rept_short_all", None)
_NONREPT: Spec = ("nonrept_positions_long_all", "nonrept_positions_short_all", None, "change_in_nonrept_long_all",
                  "change_in_nonrept_short_all", None, "pct_of_oi_nonrept_long_all", "pct_of_oi_nonrept_short_all", None,
                  None, None, None)
_OTHER: Spec = ("other_rept_positions_long", "other_rept_positions_short", "other_rept_positions_spread",
                "change_in_other_rept_long", "change_in_other_rept_short", "change_in_other_rept_spread",
                "pct_of_oi_other_rept_long", "pct_of_oi_other_rept_short", "pct_of_oi_other_rept_spread",
                "traders_other_rept_long_all", "traders_other_rept_short", "traders_other_rept_spread")
CATEGORIES: dict[str, dict[str, Spec]] = {
    "legacy": {
        "noncomm": ("noncomm_positions_long_all", "noncomm_positions_short_all", "noncomm_postions_spread_all",
                    "change_in_noncomm_long_all", "change_in_noncomm_short_all", "change_in_noncomm_spead_all",
                    "pct_of_oi_noncomm_long_all", "pct_of_oi_noncomm_short_all", "pct_of_oi_noncomm_spread",
                    "traders_noncomm_long_all", "traders_noncomm_short_all", "traders_noncomm_spread_all"),
        "comm": ("comm_positions_long_all", "comm_positions_short_all", None, "change_in_comm_long_all",
                 "change_in_comm_short_all", None, "pct_of_oi_comm_long_all", "pct_of_oi_comm_short_all", None,
                 "traders_comm_long_all", "traders_comm_short_all", None),
        "tot_rept": _TOT, "nonrept": _NONREPT,
    },
    "disagg": {
        "prod_merc": ("prod_merc_positions_long", "prod_merc_positions_short", None, "change_in_prod_merc_long",
                      "change_in_prod_merc_short", None, "pct_of_oi_prod_merc_long", "pct_of_oi_prod_merc_short", None,
                      "traders_prod_merc_long_all", "traders_prod_merc_short_all", None),
        "swap": ("swap_positions_long_all", "swap__positions_short_all", "swap__positions_spread_all",
                 "change_in_swap_long_all", "change_in_swap_short_all", "change_in_swap_spread_all",
                 "pct_of_oi_swap_long_all", "pct_of_oi_swap_short_all", "pct_of_oi_swap_spread_all",
                 "traders_swap_long_all", "traders_swap_short_all", "traders_swap_spread_all"),
        "m_money": ("m_money_positions_long_all", "m_money_positions_short_all", "m_money_positions_spread",
                    "change_in_m_money_long_all", "change_in_m_money_short_all", "change_in_m_money_spread",
                    "pct_of_oi_m_money_long_all", "pct_of_oi_m_money_short_all", "pct_of_oi_m_money_spread",
                    "traders_m_money_long_all", "traders_m_money_short_all", "traders_m_money_spread_all"),
        "other_rept": _OTHER, "tot_rept": _TOT, "nonrept": _NONREPT,
    },
    "tff": {
        "dealer": ("dealer_positions_long_all", "dealer_positions_short_all", "dealer_positions_spread_all",
                   "change_in_dealer_long_all", "change_in_dealer_short_all", "change_in_dealer_spread_all",
                   "pct_of_oi_dealer_long_all", "pct_of_oi_dealer_short_all", "pct_of_oi_dealer_spread_all",
                   "traders_dealer_long_all", "traders_dealer_short_all", "traders_dealer_spread_all"),
        "asset_mgr": ("asset_mgr_positions_long", "asset_mgr_positions_short", "asset_mgr_positions_spread",
                      "change_in_asset_mgr_long", "change_in_asset_mgr_short", "change_in_asset_mgr_spread",
                      "pct_of_oi_asset_mgr_long", "pct_of_oi_asset_mgr_short", "pct_of_oi_asset_mgr_spread",
                      "traders_asset_mgr_long_all", "traders_asset_mgr_short_all", "traders_asset_mgr_spread"),
        "lev_money": ("lev_money_positions_long", "lev_money_positions_short", "lev_money_positions_spread",
                      "change_in_lev_money_long", "change_in_lev_money_short", "change_in_lev_money_spread",
                      "pct_of_oi_lev_money_long", "pct_of_oi_lev_money_short", "pct_of_oi_lev_money_spread",
                      "traders_lev_money_long_all", "traders_lev_money_short_all", "traders_lev_money_spread"),
        "other_rept": _OTHER, "tot_rept": _TOT, "nonrept": _NONREPT,
    },
    "cit": {
        "noncomm": ("ncomm_postions_long_all_nocit", "ncomm_postions_short_all_nocit", "ncomm_postions_spread_all_nocit",
                    "change_noncomm_long_all_nocit", "change_noncomm_short_all_nocit", "change_noncomm_spead_all_nocit",
                    "pct_oi_noncomm_long_all_nocit", "pct_oi_noncomm_short_all_nocit", "pct_oi_noncomm_spread_all_nocit",
                    "traders_noncomm_long_all_nocit", "traders_noncomm_short_all_nocit", "traders_noncomm_spread_all_nocit"),
        "comm": ("comm_positions_long_all_nocit", "comm_positions_short_all_nocit", None, "change_comm_long_all_nocit",
                 "change_comm_short_all_nocit", None, "pct_oi_comm_long_all_nocit", "pct_oi_comm_short_all_nocit", None,
                 "traders_comm_long_all_nocit", "traders_comm_short_all_nocit", None),
        "cit": ("cit_positions_long_all", "cit_positions_short_all", None, "change_cit_long_all", "change_cit_short_all",
                None, "pct_oi_cit_long_all", "pct_oi_cit_short_all", None, "traders_cit_long_all", "traders_cit_short_all", None),
        "tot_rept": ("tot_rept_positions_long_all", "tot_rept_positions_short", None, "change_tot_rept_long_all",
                     "change_tot_rept_short_all", None, "pct_oi_tot_rept_long_all_nocit", "pct_oi_tot_rept_short_all_nocit",
                     None, "traders_tot_rept_long_all_nocit", "traders_tot_rept_short_all_nocit", None),
        "nonrept": ("nonrept_positions_long_all", "nonrept_positions_short_all", None, "change_nonrept_long_all",
                    "change_nonrept_short_all", None, "pct_oi_nonrept_long_all_nocit", "pct_oi_nonrept_short_all_nocit",
                    None, None, None, None),
    },
}
_CONC = {"conc_gross_4_long": "conc_gross_le_4_tdr_long", "conc_gross_4_short": "conc_gross_le_4_tdr_short",
         "conc_gross_8_long": "conc_gross_le_8_tdr_long", "conc_gross_8_short": "conc_gross_le_8_tdr_short",
         "conc_net_4_long": "conc_net_le_4_tdr_long_all", "conc_net_4_short": "conc_net_le_4_tdr_short_all",
         "conc_net_8_long": "conc_net_le_8_tdr_long_all", "conc_net_8_short": "conc_net_le_8_tdr_short_all"}
MARKET: dict[str, dict[str, str]] = {
    fam: {"open_interest": "open_interest_all", "chg_open_interest": "change_in_open_interest_all",
          "traders_total": "traders_tot_all", **_CONC} for fam in ("legacy", "disagg", "tff")
}
MARKET["cit"] = {"open_interest": "open_interest_all", "chg_open_interest": "change_open_interest_all",
                 "traders_total": "traders_tot_all"}
_POS_FIELDS = ("long_pos", "short_pos", "spread_pos", "chg_long", "chg_short", "chg_spread", "pct_oi_long", "pct_oi_short",
               "pct_oi_spread", "traders_long", "traders_short", "traders_spread")
_INT64 = {"long_pos", "short_pos", "spread_pos", "chg_long", "chg_short", "chg_spread", "open_interest", "chg_open_interest"}
_INT32 = {"traders_long", "traders_short", "traders_spread", "traders_total"}


def family(report_type: str) -> str:
    return report_type.split("_")[0]


def select_columns(report_type: str) -> list[str]:
    fam = family(report_type)
    cols = [":created_at", *META_COLS]
    for spec in CATEGORIES[fam].values():
        cols += [c for c in spec if c]
    cols += list(MARKET[fam].values())
    return list(dict.fromkeys(cols))


# ------------------------------------------------------------------------------------------------ release schedule

# Government-shutdown backlogs: report dates in [start, end] were published late; conservative release dates.
SHUTDOWN_BACKLOGS: tuple[tuple[date, date, date], ...] = (
    (date(2013, 10, 1), date(2013, 10, 15), date(2013, 11, 12)),
    (date(2018, 12, 24), date(2019, 1, 29), date(2019, 3, 8)),
)


@lru_cache(maxsize=1)
def _federal_holidays() -> frozenset[date]:
    from pandas.tseries.holiday import USFederalHolidayCalendar  # noqa: PLC0415

    return frozenset(d.date() for d in USFederalHolidayCalendar().holidays(start="1985-01-01", end="2040-12-31"))


def release_schedule(report_date: date) -> datetime:
    """Scheduled release (UTC) for a report as-of date: the Friday of that week, 15:30 ET; a federal holiday between
    the as-of date (exclusive) and that Friday moves it to the next business day after the Friday (CFTC practice,
    e.g. 2026: Thanksgiving -> Monday). Shutdown backlogs use a conservative fixed date."""
    hol = _federal_holidays()
    fri = report_date + timedelta(days=(4 - report_date.weekday()) % 7 or 7)
    rel = fri
    if any(report_date < h <= fri for h in hol):
        rel = fri + timedelta(days=1)
        while rel.weekday() >= 5 or rel in hol:
            rel += timedelta(days=1)
    for a, b, late in SHUTDOWN_BACKLOGS:
        if a <= report_date <= b and late > rel:
            rel = late
    return datetime.combine(rel, time(15, 30), ET).astimezone(UTC)


# ------------------------------------------------------------------------------------------------ parsing


def _num(col: str | None, cast: pl.DataType, scale: float | None = None) -> pl.Expr:
    if col is None:
        return pl.lit(None, dtype=cast)
    e = pl.col(col).str.strip_chars().str.replace_all(",", "").cast(pl.Float64, strict=False)
    if scale is not None:
        e = e * scale
    if cast in (pl.Int64, pl.Int32):
        e = e.round(0)
    return e.cast(cast, strict=False)


def _dtype(field: str) -> pl.DataType:
    if field in _INT64:
        return pl.Int64()
    if field in _INT32:
        return pl.Int32()
    return pl.Float32()


def parse_csv(content: bytes, report_type: str, now: datetime | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """One Socrata CSV page -> (positions long frame, market metadata frame). Missing columns become nulls."""
    now = now or datetime.now(UTC)
    fam = family(report_type)
    raw = pl.read_csv(io.BytesIO(content), infer_schema=False, encoding="utf8-lossy")
    if raw.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    raw = raw.rename({c: c.lower() for c in raw.columns})  # CSV headers keep the dataset's mixed case (CIT)
    for c in select_columns(report_type):
        if c not in raw.columns:
            raw = raw.with_columns(pl.lit(None, pl.Utf8).alias(c))
    raw = raw.with_columns(
        pl.col("cftc_contract_market_code").str.strip_chars().alias("market_code"),
        pl.col("report_date_as_yyyy_mm_dd").str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False).alias("report_date"),
        pl.col(":created_at").str.to_datetime("%Y-%m-%dT%H:%M:%S%.fZ", time_zone="UTC", strict=False).alias("created_at"),
    ).filter(pl.col("market_code").is_not_null() & pl.col("report_date").is_not_null())
    raw = raw.with_columns(_knowledge_exprs(raw))
    mk = MARKET[fam]
    market_exprs = [_num(mk.get(f), _dtype(f), 0.01 if f.startswith("conc_") else None).alias(f)
                    for f in ("open_interest", "chg_open_interest", "traders_total", *_CONC)]
    base = ["market_code", "report_date", "knowledge_ts", "knowledge_date_estimated"]
    parts = []
    for cat, spec in CATEGORIES[fam].items():
        ex = [_num(col, _dtype(f), 0.01 if f.startswith("pct_") else None).alias(f) for f, col in zip(_POS_FIELDS, spec, strict=True)]
        parts.append(raw.select(*[pl.col(b) for b in base], pl.lit(cat).alias("category"), *ex, *market_exprs))
    pos = pl.concat(parts, how="vertical_relaxed").with_columns(
        pl.lit(report_type).alias("report_type"),
        pl.col("knowledge_ts").dt.convert_time_zone("America/New_York").dt.date().alias("knowledge_date"),
        pl.lit(SOURCE).alias("source_id"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("ingested_at"),
    )
    # a category row with neither long nor short published carries no information (e.g. CIT columns pre-2006)
    pos = pos.filter(pl.col("long_pos").is_not_null() | pl.col("short_pos").is_not_null())
    meta = raw.select(
        "market_code", "report_date",
        pl.col("contract_market_name").str.strip_chars().alias("market_name"),
        pl.col("market_and_exchange_names").str.strip_chars().alias("market_and_exchange"),
        pl.col("commodity_name").str.strip_chars(), pl.col("cftc_commodity_code").str.strip_chars().alias("commodity_code"),
        pl.col("commodity_group_name").str.strip_chars().alias("commodity_group"),
        pl.col("commodity_subgroup_name").str.strip_chars().alias("commodity_subgroup"),
        pl.col("contract_units").str.strip_chars(),
    )
    return pos, meta


def _knowledge_exprs(raw: pl.DataFrame) -> list[pl.Expr]:
    """knowledge_ts / knowledge_date_estimated (see module doc)."""
    sched = {d: release_schedule(d) for d in raw.get_column("report_date").unique().to_list()}
    sched_df = pl.DataFrame({"report_date": list(sched), "sched": list(sched.values())},
                            schema={"report_date": pl.Date, "sched": pl.Datetime("us", "UTC")})
    tmp = raw.select("report_date", "created_at").join(sched_df, on="report_date", how="left")
    bulk = (tmp.filter(pl.col("created_at").is_not_null())
            .group_by(pl.col("created_at").dt.date().alias("cd")).agg(pl.col("report_date").n_unique().alias("n"))
            .filter(pl.col("n") > 3).get_column("cd").to_list())
    s = tmp.get_column("sched")
    c = tmp.get_column("created_at")
    ok = (c.is_not_null() & (c >= s - timedelta(days=1)) & (c <= s + timedelta(days=70))
          & ~c.dt.date().is_in(bulk)).fill_null(False)
    kts = pl.select(pl.when(ok).then(c).otherwise(s)).to_series()
    return [pl.lit(kts).alias("knowledge_ts"), pl.lit(~ok).alias("knowledge_date_estimated")]


# ------------------------------------------------------------------------------------------------ adapter


@register_adapter
class CftcAdapter(SourceAdapter):
    source_id = SOURCE
    regions = ("US",)
    produces = ("cftc_positions", "cftc_markets")
    rate_limit = RateLimit(2.0, 2)
    base_url = BASE
    cache_ttl = 0

    def __init__(self, settings: Any = None, http: HttpClient | None = None, params: dict[str, Any] | None = None):
        self.params = params or {}
        super().__init__(settings=settings, http=http or HttpClient(SOURCE, base_url=BASE, rate_limit=self.rate_limit,
                                                                    settings=settings, timeout=300, cache=False))

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([{"source_entity_id": k, "name": v} for k, v in DATASETS.items()], SOURCE)

    def fetch_pages(self, report_type: str, since: date | None, max_pages: int | None = None):
        """Yield raw CSV pages (bytes) for one report type, landing each under data/raw/cftc/."""
        ds = DATASETS[report_type]
        where = f"report_date_as_yyyy_mm_dd >= '{since.isoformat()}T00:00:00.000'" if since else None
        offset, page = 0, 0
        while True:
            params = {"$select": ",".join(select_columns(report_type)), "$order": ":id", "$limit": PAGE, "$offset": offset}
            if where:
                params["$where"] = where
            r = self.http.get(f"{ds}.csv", params, cache_ttl=0)
            body = r.content
            n = max(0, body.count(b"\n") - 1)
            self.save_raw(report_type, f"{(since or date(1900, 1, 1)).isoformat()}_p{page:03d}.csv", body)
            yield body
            page += 1
            if n < PAGE or (max_pages and page >= max_pages):
                break
            offset += PAGE

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        raise NotImplementedError("use run_cot()")

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        raise NotImplementedError("use run_cot()")


def _latest_report_date(report_type: str) -> date | None:
    """Latest stored report date (None = nothing stored yet: scan_table returns a typed empty frame). A read error
    is raised (F24) - it used to look like an empty table and silently forced a full re-download."""
    lf = scan_table("cftc_positions").filter(pl.col("report_type") == report_type)
    return lf.select(pl.col("report_date").max()).collect().item()


def run_cot(ad: CftcAdapter, report_types: Sequence[str], full: bool, since: date | None, log: Any,
            check: Any = None) -> dict[str, Any]:
    """Fetch + parse + upsert each report type; then rebuild cftc_markets. Returns a summary dict."""
    out: dict[str, Any] = {"rows": {}, "failed": {}, "max_date": None}
    metas: list[pl.DataFrame] = []
    for rt in report_types:
        if check:
            check()
        try:
            st = None if full else (since or _latest_report_date(rt))
        except Exception as e:  # noqa: BLE001 - unreadable lake table: fail this report type, never re-download all
            out["failed"][rt] = f"cftc_positions unreadable: {type(e).__name__}: {str(e)[:200]}"
            log.warning("cftc %s: %s", rt, out["failed"][rt])
            continue
        if st is not None and not since:
            st = st - timedelta(days=REVISION_DAYS)
        try:
            frames, mframes = [], []
            for body in ad.fetch_pages(rt, st, ad.params.get("max_pages")):
                pos, meta = parse_csv(body, rt)
                if pos.height:
                    frames.append(pos)
                    mframes.append(meta)
            if not frames:
                out["rows"][rt] = 0
                continue
            df = pl.concat(frames, how="vertical_relaxed")
            n = write_table(df, "cftc_positions", mode="upsert", settings=ad.settings)
            out["rows"][rt] = n
            md = df.get_column("report_date").max()
            out["max_date"] = max(out["max_date"] or md, md)
            metas.append(pl.concat(mframes, how="vertical_relaxed").with_columns(pl.lit(rt).alias("report_type")))
            log.info("cftc %s: %d rows (since %s, last %s)", rt, n, st, md)
        except Exception as e:  # noqa: BLE001 - one report type failing must not block the others
            log.warning("cftc %s failed: %s", rt, e)
            out["failed"][rt] = str(e)[:300]
    out["markets"] = build_markets(metas, ad.settings)
    return out


def _futures_names(settings: Any) -> dict[str, str]:
    try:
        sm = read_table("securities_master", columns=["security_id", "security_type", "name"], settings=settings)
        sm = sm.filter(pl.col("security_type") == "future")
        return dict(zip(sm.get_column("security_id").to_list(), sm.get_column("name").to_list(), strict=True))
    except Exception:  # noqa: BLE001
        return {}


_FAM_ORDER = {"legacy_fut": 0, "legacy_comb": 1, "disagg_fut": 2, "disagg_comb": 3, "tff_fut": 4, "tff_comb": 5, "cit_comb": 6}


def build_markets(metas: list[pl.DataFrame], settings: Any, positions: pl.LazyFrame | None = None,
                  futures: dict[str, str] | None = None, now: datetime | None = None) -> int:
    """Rebuild cftc_markets from the whole cftc_positions table + latest metadata (new pages over the existing table)."""
    now = now or datetime.now(UTC)
    try:
        lf = positions if positions is not None else scan_table("cftc_positions", settings)
        agg = (lf.group_by("market_code", "report_type")
               .agg(pl.col("report_date").min().alias("first"), pl.col("report_date").max().alias("last"),
                    pl.col("report_date").n_unique().alias("n"),
                    pl.col("open_interest").sort_by("report_date").last().alias("oi"))
               .collect())
    except Exception:  # noqa: BLE001 - nothing ingested yet
        return 0
    if agg.is_empty():
        return 0
    agg = agg.with_columns(pl.col("report_type").replace_strict(_FAM_ORDER, return_dtype=pl.Int8).alias("o"))
    span = (agg.sort("o").group_by("market_code", maintain_order=True)
            .agg(pl.col("report_type").alias("report_types"), pl.col("first").min().alias("first_report_date"),
                 pl.col("last").max().alias("last_report_date"), pl.col("n").max().cast(pl.Int32).alias("n_weeks"),
                 pl.col("oi").filter(pl.col("last") == pl.col("last").max()).first().alias("latest_open_interest")))
    try:
        old = read_table("cftc_markets", settings=settings).select(
            "market_code", "market_name", "market_and_exchange", "commodity_name", "commodity_code", "commodity_group",
            "commodity_subgroup", "contract_units").with_columns(pl.lit(date(1900, 1, 1)).alias("report_date"))
    except Exception:  # noqa: BLE001
        old = None
    frames = [m.drop("report_type") if "report_type" in m.columns else m for m in metas]
    if old is not None and old.height:
        frames.append(old.select(frames[0].columns) if frames else old)
    if not frames:
        return 0
    meta = pl.concat(frames, how="vertical_relaxed")
    # latest non-null value per field (older rows have null groups / units)
    fields = [c for c in meta.columns if c not in ("market_code", "report_date")]
    meta = (meta.sort("report_date").group_by("market_code")
            .agg([pl.col(f).drop_nulls().last().alias(f) for f in fields]))
    df = span.join(meta, on="market_code", how="left")
    latest = df.get_column("last_report_date").max()
    fut = futures if futures is not None else _futures_names(settings)
    curated_sids = {c.sid for c in CURATED.values() if c.sid}

    def active_of(r: dict[str, Any]) -> bool:
        return bool(latest and r["last_report_date"] and (latest - r["last_report_date"]).days <= 21)

    rows = []
    for r in df.iter_rows(named=True):
        code = r["market_code"]
        name = r.get("market_name") or code
        cur = CURATED.get(code)
        ac = asset_class_for(code, r.get("commodity_subgroup"), r.get("market_and_exchange") or name, r.get("commodity_group"))
        sid, method = None, "none"
        if cur and cur.sid:
            sid, method = cur.sid, "curated"
        elif not cur and active_of(r):
            # name matching only for live markets whose instrument is not already curated to another market
            # (defunct regional / mini contracts such as "CORN - MIDAMERICA" must not hijack ZC=F)
            hit = name_match(name, fut)
            if hit and hit not in curated_sids:
                sid, method = hit, "name_match"
        elif cur:
            method = "curated"
        price_sid = sid or (cur.proxy if cur else None)
        links = ([sid] if sid else []) + (list(cur.links) if cur else [])
        mex = r.get("market_and_exchange") or ""
        rows.append({
            "market_code": code, "market_name": name, "market_and_exchange": r.get("market_and_exchange"),
            "exchange": mex.rsplit(" - ", 1)[1].strip() if " - " in mex else None,
            "commodity_name": r.get("commodity_name"), "commodity_code": r.get("commodity_code"),
            "commodity_group": r.get("commodity_group"), "commodity_subgroup": r.get("commodity_subgroup"),
            "contract_units": r.get("contract_units"), "asset_class": ac,
            "short_name": cur.short_name if cur else title_name(name), "root": cur.root if cur else None,
            "security_id": sid, "price_security_id": price_sid,
            "price_is_proxy": (price_sid is not None and sid is None) if price_sid else None,
            "linked_security_ids": links or None, "mapping_method": method, "is_major": cur is not None,
            "primary_report": primary_report_for(ac, r["report_types"]), "report_types": r["report_types"],
            "first_report_date": r["first_report_date"], "last_report_date": r["last_report_date"],
            "n_weeks": r["n_weeks"], "latest_open_interest": r["latest_open_interest"],
            "is_active": active_of(r),
            "source_id": SOURCE, "updated_at": now,
        })
    out = pl.DataFrame(rows, infer_schema_length=None)
    return write_table(out, "cftc_markets", mode="overwrite", settings=settings)


def coverage(settings: Any = None) -> dict[str, Any]:
    """Mapping coverage report: futures instruments with a CFTC market, markets per asset class."""
    m = read_table("cftc_markets", settings=settings)
    fut = _futures_names(settings)
    linked = {x for xs in m.get_column("linked_security_ids").drop_nulls().to_list() for x in xs}
    return {
        "markets": m.height, "active": int(m.get_column("is_active").sum()),
        "mapped_markets": int(m.filter(pl.col("security_id").is_not_null()).height),
        "proxy_priced": int(m.filter(pl.col("price_is_proxy").fill_null(False)).height),
        "futures_instruments": len(fut), "futures_with_positioning": sorted(s for s in fut if s in linked),
        "futures_without": sorted(s for s in fut if s not in linked),
        "by_asset_class": dict(m.filter(pl.col("is_active")).group_by("asset_class").len().iter_rows()),
    }


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = dict(ctx.params)
    ad = CftcAdapter(settings=ctx.settings, params=p)
    rts = [r for r in (p.get("report_types") or REPORT_TYPES) if r in DATASETS]
    since = date.fromisoformat(p["since"]) if p.get("since") else None
    res = run_cot(ad, rts, bool(p.get("full")), since, ctx.log, ctx.check_cancelled)
    rows = sum(res["rows"].values())
    status = "failed" if res["failed"] and not res["rows"] else ("partial" if res["failed"] else "success")
    try:
        cov = coverage(ctx.settings)
        _state({"last_run": datetime.now(UTC).isoformat(), "rows": res["rows"], "failed": res["failed"],
                "max_report_date": str(res["max_date"]), "coverage": {k: v for k, v in cov.items() if k != "futures_with_positioning"}},
               ctx.settings)
    except Exception:  # noqa: BLE001
        pass
    return JobResult(status=status, rows_written={"cftc_positions": rows, "cftc_markets": res["markets"]},
                     entities_ok=len(res["rows"]), entities_failed=len(res["failed"]),
                     watermark=str(res["max_date"]) if res["max_date"] else None, http_requests=ad.http.n_requests,
                     message=f"report types {sorted(res['rows'])}; failed {sorted(res['failed'])}")


def _state(upd: dict[str, Any], settings: Any) -> None:
    import json  # noqa: PLC0415

    p = settings.data_dir / "state" / "cftc.json"
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        cur = {}
    cur.setdefault("ingest", {}).update(upd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=1, default=str), encoding="utf-8")
