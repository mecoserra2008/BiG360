"""Startup sync policy (docs/STARTUP_SYNC_PLAN.md workstream A): which jobs fetch which coverage dataset, how jobs
are classed and laned, which API keys unlock what, and per-job pacing. Pure data plus tiny helpers (no I/O), so the
scan, the executor and the tests share one source of truth.

Vocabulary
* fetch job   - talks to an external source (module app.ingestion.* or FETCH_EXTRA). Runs in a network lane: lanes
                run in parallel, at most `lane_max(source)` jobs per source at once.
* derive job  - recomputes from the lake. Runs only when one of its input tables (the `writes` of its upstream jobs
                in contracts/jobs.yaml) changed after its last successful start ("input fingerprint").
                CPU/RAM-heavy ones (process executor or HEAVY_JOBS) run one at a time in the heavy lane, under the
                machine guard and the exclusive heavy slot; the others share a small light lane.
* tier        - `core` jobs run in pass 1 and are published first (prices, FX, macro, filings, the analytics behind
                the coverage datasets); `extended` jobs (slow model analytics) run in pass 2 and are published by a
                second section-incremental publish.
* root fetch  - a fetch job with no upstream inside the sync set. Covered by a coverage.yaml dataset -> runs only
                when that dataset is `due`; otherwise it is a job-level dataset `job:<id>` with a daily cadence
                (`min_interval_h`).
"""

from __future__ import annotations

#: coverage dataset -> fetch jobs that bring it to the latest period (params are computed by the scan)
DATASET_FETCH: dict[str, tuple[str, ...]] = {
    "prices.US": ("prices_us_polygon", "prices_global_yf"),
    "prices.CA": ("prices_global_yf",),
    "prices.EU": ("prices_global_yf",),
    "prices.UK": ("prices_global_yf",),
    "prices.JP": ("prices_global_yf",),
    "prices.KR": ("prices_global_yf", "prices_kr_krx"),
    "prices.HK": ("prices_global_yf",),
    "prices.CN": ("prices_cn_baostock", "prices_global_yf"),
    "prices.LATAM": ("prices_br_b3", "prices_global_yf"),
    "prices.APAC": ("prices_tw", "prices_global_yf"),
    "fx.rates": ("fx_rates",),
    "news.items": ("news_ingest",),
    "filings.sec": ("sec_filings_index",),
    "insider.us": ("sec_filings_index", "sec_form4"),
    "corporate_actions": ("corporate_actions",),
    "cftc.cot": ("cftc_cot",),
    "inst.13f": ("sec_13f",),
    "short_interest": ("finra_short_interest",),
    "earnings.dates": ("earnings_dates_fetch",),
    # W3 rates & debt (rates-w3): official curves, auctions, bond reference, issuer debt
    "rates.curves": ("rates_curves",),
    "rates.auctions": ("rates_auctions",),
    "credit.bonds_eu": ("bonds_reference",),
    "credit.trace": ("credit_trace",),
    # W2 regulator registers (ingest-register): SEC lists daily, FIRDS weekly files checked daily; Companies House bulk
    "register.US": ("regulatory_register",),
    "register.EU": ("regulatory_register",),
    "register.UK": ("regulatory_register",),
    "filings.uk_ch": ("ch_accounts",),
}

#: fetch jobs planned by dataset rules outside DATASET_FETCH (macro groups by source / release, GDELT days)
DYNAMIC_FETCH: frozenset[str] = frozenset({"macro_fred", "macro_eu", "macro_intl", "macro_national", "gdelt_stream",
                                           "news_finalise"})
#: GDELT days assembled per sync (newest first; older holes are the gap filler's gdelt_redo)
GDELT_MAX_DAYS_PER_RUN = 4
GDELT_WINDOW_DAYS = 10

#: datasets whose "latest" is owned by another process: reported, never fetched by the sync
OWNED_ELSEWHERE: dict[str, str] = {
    "prices.history": "deep price history is the gap filler's job (campaign yahoo_price_history)",
    "news.live": "live news is the realtime service plus the hourly DAG (news_hourly / news_finalise)",
    # coverage v2 W2: statements for every register issuer are long backfills (filler campaigns register_statements_*);
    # the nightly jobs regulatory_register / ch_accounts / regulatory_coverage keep the registers and the report current
    "statements.register.US": "register statements backfill is the gap filler's job (campaign register_statements_us)",
    "statements.register.EU": "register statements backfill is the gap filler's job (campaigns register_statements_eu_uk / register_oam / register_yf_fallback)",
    "statements.register.UK": "register statements backfill is the gap filler's job (campaigns register_statements_eu_uk / register_yf_fallback)",
}

#: price region -> exchange calendar used when coverage.yaml has none
REGION_CALENDAR: dict[str, str] = {"US": "XNYS", "CA": "XTSE", "EU": "XETR", "UK": "XLON", "JP": "XTKS", "KR": "XKRX",
                                   "HK": "XHKG", "CN": "XSHG", "LATAM": "BVMF", "APAC": "XTAI"}

#: minutes after an exchange close before the day's bar counts as published (Yahoo / Polygon end of day)
PRICE_PUBLISH_DELAY_MIN: dict[str, int] = {"prices_us_polygon": 60, "default": 30}

#: extra fetch-class jobs outside app.ingestion
FETCH_EXTRA: frozenset[str] = frozenset({"gdelt_stream", "earnings_dates_fetch", "cb_balance_sheets", "news_finalise",
                                         "news_hourly", "rates_curves", "rates_auctions", "bonds_reference",
                                         "credit_trace"})

#: never planned by the sync (refresh / hourly DAG / maintenance / publish handled by the sync itself)
EXCLUDE: frozenset[str] = frozenset({
    "build_serving_db", "dq_checks", "qa_regression", "data_gaps_detect", "alerts_eval", "thesis_eval",
    "events_retention", "macro_audit", "ref_universe", "macro_global", "news_hourly", "news_finalise", "news_intraday",
    "gdelt_events", "gdelt_gkg",   # aliases of gdelt_stream (no-ops when it ran)
})

#: run after a publish (cheap, read the new snapshot)
POST_PUBLISH: tuple[str, ...] = ("alerts_eval", "thesis_eval")
#: the regression guard, once at the end of a sync that published (alert hygiene 2026-10-30; SyncRun.qa_step)
QA_JOB = "qa_regression"
#: data-quality checks after a sync that published, at most every app.core.dq.POST_PUBLISH_MIN_INTERVAL_H hours
#: (A5 2026-11; SyncRun.dq_step); excluded from the plan above because the sync runs it itself
DQ_JOB = "dq_checks"
#: longest wait of that post-publish dq_checks for the heavy slot / RAM floor before it is deferred (monitoring only:
#: it never holds the sync - and its lock - behind a big job of another process; A5 review 2026-11)
DQ_START_WAIT_S = 600.0

#: dags whose jobs are sync candidates (weekly jobs only when a dataset names them)
SYNC_DAGS: frozenset[str] = frozenset({"nightly", "cftc_weekly"})

#: thread jobs that are CPU / RAM heavy (process-executor jobs are heavy anyway)
HEAVY_JOBS: frozenset[str] = frozenset({
    "prices_finalize", "fundamentals_normalize", "ratios_kpis", "earnings_reactions", "earnings_expectations",
    "macro_cells", "identity_resolve", "cb_tone", "event_escalation", "story_cluster", "gdelt_stream",
})

#: fetch jobs that are network-bound although they use the process executor (RAM ~1 GB, no big parse)
NETWORK_PROCESS_OK: frozenset[str] = frozenset({"prices_global_yf", "earnings_dates_fetch"})

#: derive jobs of pass 1 (needed for the headline data); every other derive job is `extended` (pass 2)
CORE_DERIVED: frozenset[str] = frozenset({
    "prices_finalize", "corporate_actions", "valuation_daily", "scoring", "ml_features", "ml_predict",
    "composite_ml_blend", "insider_signals", "sentiment_daily", "macro_cells", "macro_cycle", "macro_calendar",
    "macro_surprise", "macro_expectations", "macro_nowcast", "macro_recession", "macro_regime_nowcast",
    "macro_factors", "regime_performance", "cb_rates", "cb_curves_implied", "cb_projections", "cb_stance",
    "cftc_signals", "cross_asset_signals", "event_connectedness", "sec_form4", "identity_resolve", "events_agg",
})

#: fetch jobs of pass 2 although a dataset names them: slow or not needed for the headline data (news is also live;
#: BaoStock takes ~1 h for the A-share list, so CN is first finalised from Yahoo and again after BaoStock in pass 2)
EXTENDED_FETCH: frozenset[str] = frozenset({"gdelt_stream", "news_finalise", "sec_text", "sec_xbrl_facts", "news_ingest",
                                            "prices_cn_baostock"})

#: true data dependencies inside the sync (everything else is ordering only: a failed upstream never blocks)
HARD_DEPS: dict[str, frozenset[str]] = {
    "ml_predict": frozenset({"ml_features"}),
    "composite_ml_blend": frozenset({"ml_predict"}),
    "confluence_zones": frozenset({"tech_levels"}),
    "macro_cycle": frozenset({"macro_cells"}),
    "cftc_signals": frozenset({"cftc_cot"}),
}

#: minimum hours between two sync runs of a job (noisy inputs such as hourly news, or slow model jobs); root fetch
#: jobs without a coverage dataset default to ROOT_MIN_INTERVAL_H
MIN_INTERVAL_H: dict[str, float] = {
    "nlp_sentiment": 12, "story_cluster": 6, "gdelt_company_daily": 12, "sentiment_daily": 6, "events_agg": 6,
    "event_hotspots": 20, "event_changepoints": 20, "event_escalation": 20, "event_gpr": 20, "event_exposure": 20,
    "news_filings": 12, "lazy_prices": 20, "supply_chain_network": 20, "earnings_events": 12,
    "earnings_reactions": 20, "earnings_expectations": 20, "earnings_signals": 20, "cb_tone": 20,
    "identity_resolve": 6, "fundamentals_normalize": 12, "ratios_kpis": 12, "forecast": 12, "tech_meta_score": 20,
    "technicals_probabilities": 20, "tech_breakouts": 20, "tech_factors": 12, "conformal_envelopes": 12,
    "conformal_peer_groups": 12, "conformal_coverage": 12, "macro_calendar": 3, "macro_expectations": 6,
    "macro_nowcast": 6, "cross_asset_signals": 3, "smart_money_signals": 12, "earnings_dates_fetch": 20,
    "news_ingest": 3, "sec_text": 20, "sec_xbrl_facts": 12,
    # full-universe extension 2026-10-30: incremental (per-security fingerprints), but a changed MVP reference
    # re-scores every security (~75 s, ~3.5 GB) -> at most every 2 h; extended predictions at most every 6 h
    "scoring_full": 2, "ml_predict_full": 6,
    # W3 rates & debt: issuer debt profiles change with filings (yield_curves changes daily)
    "company_debt": 20,
}
ROOT_MIN_INTERVAL_H = 20.0

#: API keys per job (Settings secret names, never values) and per macro source (macro_* jobs skip such sources)
JOB_KEYS: dict[str, tuple[str, ...]] = {
    "prices_kr_krx": ("krx_openapi_key",), "opendart_filings": ("opendart_apikey",),
    "uk_companies_house": ("companies_house_apikey",), "prices_us_polygon": ("polygon_apikey",),
    "corporate_actions": ("polygon_apikey",), "edinet_filings": ("edinet_apikey",), "macro_fred": ("fred_apikey",),
    "credit_trace": ("finra_api_client_id", "finra_api_client_secret"),
}
SOURCE_KEYS: dict[str, tuple[str, ...]] = {
    "banxico": ("banxico_token",), "bok_ecos": ("bok_ecos_apikey",), "inegi": ("inegi_token",),
    "estat_jp": ("estat_appid",), "bcch": ("bcch_user", "bcch_pass"), "kosis": ("kosis_apikey",),
    "eia": ("eia_apikey",),
}
#: macro group job per macro_series.source_id prefix (fred -> macro_fred; the rest by the group SOURCES lists)
MACRO_GROUP_JOBS: tuple[str, ...] = ("macro_fred", "macro_eu", "macro_intl", "macro_national")

#: entities without a free / official source (skipped permanently, documented)
NO_SOURCE: dict[str, dict[str, str]] = {
    "fx.rates": {"TWD": "daily TWD/USD needs the CBC Taiwan rate: only the weekly Fed H.10 (FRED DEXTAUS) is free",
                 "BGN": "BGN retired 2026-01-01 (euro adoption): BG listings convert via EUR"},
    "prices.KR": {"krx_official": "KRX official whole-market prices need KRX_OPENAPI_KEY; Yahoo covers KR meanwhile"},
    "job:eu_short_positions": {"DE/IT/DK/FI": "no free machine-readable short register (BaFin, CONSOB, DK, FI)"},
}

#: serving sections that change with every hourly news fold: republished at most every NOISY_PUBLISH_MIN_H hours
#: when nothing else changed (the News Terminal reads the intraday store, not the snapshot)
NOISY_SECTIONS: frozenset[str] = frozenset({"events_v2", "events_models"})
NOISY_PUBLISH_MIN_H = 6.0

#: failure handling
PARK_AFTER_FAILURES = 3            # consecutive failed attempts of the same period -> parked (alert) ...
PARK_HOURS = 24.0                  # ... for this long
RETRY_NOT_PUBLISHED_MIN = (60, 120, 240, 480, 720)   # back-off when a fetch succeeded but the period is still missing
RETRY_UNAVAILABLE_MIN = (30, 60, 120, 240, 480)       # source down / rate-limited
NOT_PUBLISHED_PARK_ATTEMPTS = 6    # still missing after this many successful fetches -> parked for PARK_HOURS

#: lanes
LANE_MAX_DEFAULT = 2
HEAVY_LANE_MAX = 1
#: ram_fix 2026-10-31: process jobs with a jobs.yaml budget (mem_gb) below this run in the light lane
HEAVY_MEM_GB = 3.0
LIGHT_LANE_MAX = 2
NETWORK_MAX = 4                    # network jobs running at once (all sources)

NETWORK_ERROR_HINTS = ("timeout", "timed out", "connection", "temporarily", "unavailable", "429", "rate limit",
                       "ratelimit", "503", "502", "504", "max retries", "name resolution", "getaddrinfo",
                       "remote end closed", "ssl", "too many requests", "service unavailable", "proxyerror")
KEY_ERROR_HINTS = ("missing key", "_apikey", "api key", "_token missing", "missing ", "in .env")


def lane_max(source: str) -> int:
    """Concurrent jobs per source lane (the runner's SOURCE_MAX_JOBS; one Polygon / Yahoo job at a time)."""
    try:
        from app.scheduler.runner import SOURCE_MAX_JOBS  # noqa: PLC0415

        return int(SOURCE_MAX_JOBS.get(source, LANE_MAX_DEFAULT))
    except Exception:  # pragma: no cover
        return LANE_MAX_DEFAULT


def min_interval_h(job_id: str, root: bool) -> float:
    if job_id in MIN_INTERVAL_H:
        return float(MIN_INTERVAL_H[job_id])
    return ROOT_MIN_INTERVAL_H if root else 0.0


def is_network_error(msg: str | None) -> bool:
    m = (msg or "").lower()
    return any(h in m for h in NETWORK_ERROR_HINTS)


def missing_key_names(msg: str | None) -> list[str]:
    """Key names mentioned by a 'missing key' job message (e.g. 'missing OPENDART_APIKEY in .env')."""
    import re  # noqa: PLC0415

    m = msg or ""
    if not any(h in m.lower() for h in ("missing", "key")):
        return []
    return sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]*_(?:APIKEY|API_KEY|KEY|TOKEN|APPID|USER|PASS))\b", m)))
