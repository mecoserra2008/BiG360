"""Data completeness audit (docs/STARTUP_SYNC_PLAN.md section 3, workstream C).

Measures the null rate of every BQL field (contracts/bql_fields.yaml) per universe (full / mvp_us / mvp_global) and
region on the LIVE API (POST /api/v1/bql, compute-only, sequential requests), assigns every gap a reason code
(app.core.availability) from the rule table below, and writes

    contracts/field_availability.yaml   machine-readable: coverage + reason per field x region (UI tooltips, sync)
    docs/DATA_COMPLETENESS.md           one table per area: field, coverage %, reason, fix / owner

    python -m app.qa.completeness --write                       # sweep the live API, then write both files
    python -m app.qa.completeness --write --baseline <json>     # + a "before" coverage column
    python -m app.qa.completeness --from-json <json> --write    # re-classify a saved sweep (no API calls)

The sweep result is saved under data/reports/completeness/coverage_<UTC stamp>.json.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.availability import REASONS, YAML_NAME
from app.core.config import Settings, get_settings

API = "http://127.0.0.1:8010/api/v1"
UNIVERSES = ("full", "mvp_us", "mvp_global")
REGIONS = ("US", "CA", "LATAM", "UK", "EU", "JP", "KR", "CN", "HK", "APAC")
FRESH_PCT = 95.0          # a region at or above this coverage needs no reason
MIN_REGION_N = 20         # regions with fewer securities are not classified on their own
BATCH = 25

#: area of the report per BQL field group (docs/DATA_COMPLETENESS.md sections)
AREAS = {
    "identity": "Identity & classification", "price": "Prices & returns", "return": "Prices & returns",
    "technical": "Technicals", "conformal": "Technicals", "fundamental": "Fundamentals",
    "valuation": "Valuation", "score": "Scores & factors", "factor": "Scores & factors", "model": "Scores & factors",
    "sector_kpi": "Sector KPIs", "earnings": "Earnings", "smart_money": "Smart money", "insider": "Insiders",
    "sentiment": "News & sentiment", "macro": "Macro exposures", "other": "Other",
}

NON_US = ("CA", "LATAM", "UK", "EU", "JP", "KR", "CN", "HK", "APAC")
FUND_CORE = ("US", "CA", "LATAM", "UK", "EU", "JP")
_SCOPE_NOTE = ("Ratios and valuation run for every issuer with loaded statements (~4.9k issuers); the rest of the "
               "universe has no statements loaded yet")
_SCOPE_FIX = ("2026-10-27: ratios_kpis + valuation_daily default universe=full (every issuer with loaded statements); "
              "statements for the remaining US small caps need sec_xbrl_bulk all_ciks (ref-identity / ingest-us)")
_SHARES_NOTE = "Needs shares outstanding, which is loaded only for issuers with financial statements"

#: ordered rules: the first match wins. fields: fnmatch globs; groups: BQL groups; regions: None = every region.
RULES: list[dict[str, Any]] = [
    # ---------------------------------------------------------------- gates / bounds / not applicable by design
    {"fields": ["tech.composite", "tech.composite_pct"], "reason": "suppressed",
     "note": "The technical-factor composite failed its pre-registered validation gate and is withheld "
             "(docs/TECHNICALS_V3_VALIDATION.md)", "fix": "by design"},
    {"fields": ["bo.*"], "reason": "not_applicable", "note": "No breakout in the last 20 sessions", "fix": "by design"},
    {"fields": ["setup.target2"], "reason": "not_applicable", "note": "The setup has no second target level",
     "fix": "by design"},
    {"fields": ["cf.earn_days_ahead"], "reason": "not_yet_published", "note": "No upcoming earnings date announced",
     "fix": "earnings calendar (JPX / provider dates)"},
    {"fields": ["pead.outcome_car_p2_p60", "pead.outcome_*"], "reason": "not_yet_published",
     "note": "The post-earnings outcome window has not ended yet", "fix": "by design"},
    {"fields": ["pead.decile"], "reason": "not_applicable",
     "note": "PEAD deciles need enough same-region releases in the quarter", "fix": "by design"},
    {"fields": ["kpi.*"], "reason": "not_applicable",
     "note": "Sector KPI: computed only for companies of the KPI set's sectors and when the line items are reported",
     "fix": "by design"},
    {"fields": ["sent.lazy_prices_*"], "regions": list(NON_US), "reason": "not_applicable",
     "note": "10-K / 10-Q text similarity: SEC filers only", "fix": "by design"},
    {"fields": ["sent.*", "sentiment", "news_count_30d", "attention_z"], "reason": "not_applicable",
     "note": "No news items linked to this company in the window", "fix": "news linker (events-v2 / news-live)"},
    {"fields": ["ins.*", "insider_signal", "sm.insider_signal", "sm.z_insider", "sm.g_insider"], "regions": ["US", "UK"],
     "reason": "not_applicable", "note": "No insider (Form 4 / PDMR) transactions in the signal window",
     "fix": "by design"},
    {"fields": ["ins.*", "insider_signal", "sm.insider_signal", "sm.z_insider", "sm.g_insider"], "regions": ["EU"],
     "reason": "out_of_scope", "note": "EU PDMR notices (AFM, BaFin) are stored but not scored by the insider model",
     "fix": "analytics-insider: score EU PDMR"},
    {"fields": ["ins.*", "insider_signal", "sm.insider_signal", "sm.z_insider", "sm.g_insider"], "reason": "no_source",
     "note": "No free structured insider-transaction feed for this market", "fix": "-"},
    {"fields": ["sm.eu_*", "sm.z_eu_*", "sm.g_eu_short"], "regions": ["EU"], "reason": "no_source",
     "note": "DE (BaFin) and IT (CONSOB) registers are not machine-readable (robots / captcha); positions below 0.5 % "
             "are not published", "fix": "user decision 2026-10-19: skipped"},
    {"fields": ["sm.eu_*", "sm.z_eu_*", "sm.g_eu_short"], "regions": ["UK"], "reason": "not_applicable",
     "note": "No disclosed net short position >= 0.5 %", "fix": "by design"},
    {"fields": ["sm.eu_*", "sm.z_eu_*", "sm.g_eu_short"], "reason": "not_applicable",
     "note": "EU / UK short-selling disclosure regime only", "fix": "by design"},
    # ---------------------------------------------------------------- US regulatory datasets
    {"fields": ["si.pct_float", "shrt.short_pct_float", "si.si_pct_out", "si.shares_out*", "si.market_cap_usd", "si.si_pct_rank", "si.squeeze_score",
                "si.ftd_pct_out", "own.shares_out*", "own.inst_own_*", "own.flow_pct_out", "own.hf_shares_pct",
                "own.smart_flow_pct_out", "sm.si_pct_out", "sm.z_si_level", "sm.smart_flow", "sm.z_smart_flow",
                "sm.g_smart"], "regions": ["US"], "reason": "out_of_scope", "note": _SHARES_NOTE, "fix": _SCOPE_FIX},
    {"fields": ["si.*", "shrt.*", "own.*", "sm.*"], "regions": ["US"], "reason": "mapping_gap",
     "note": "FINRA / 13F / FTD row not matched to this listing (symbol or CUSIP)",
     "fix": "2026-10-27: compact class-share, ETF, OpenFIGI and FTD-history symbol resolution; ADR rows kept on the "
            "US listing (app.normalize.us_symbols)"},
    {"fields": ["si.*", "shrt.*", "own.*", "sm.*"], "reason": "not_applicable",
     "note": "US-listed securities only (FINRA short interest, SEC 13F, SEC fails-to-deliver); an ADR carries it on "
             "its US line", "fix": "by design"},
    # ---------------------------------------------------------------- earnings
    # A9 2026-11-07: explicit id (inserting a rule must not renumber the positional ids r01.. of the rules after it)
    {"id": "r_a9_jp_tdnet", "fields": ["eps_surprise*", "rev_surprise*", "earn.eps_*", "earn.rev_*", "pead.*"],
     "regions": ["JP"], "reason": "source_disallowed",
     "note": "TDnet (robots.txt Disallow: /) is no longer fetched since 2026-11-07: JP kessan-tanshin releases and "
             "Q1-Q4 actuals stop (rows 2026-09..2026-11-06 kept, source tdnet); dates come from the JPX schedule + "
             "Yahoo, statements from EDINET (+ Yahoo quarterly fallback)",
     "fix": "A9 2026-11-07 (docs/it/ROBOTS_COMPLIANCE.md): JP Q1/Q3 actuals need an allowed source"},
    {"fields": ["eps_surprise*", "rev_surprise*", "earn.eps_*", "earn.rev_*", "pead.*"], "regions": ["KR"],
     "reason": "needs_key", "note": "Needs OPENDART_APIKEY (Korea DART quarterly reports)", "fix": "user: add OPENDART_APIKEY"},
    {"fields": ["eps_surprise*", "rev_surprise*", "earn.eps_*", "earn.rev_*", "pead.*"], "regions": ["EU", "UK"],
     "reason": "no_source", "note": "Quarterly EPS is not filed machine-readably in the EU / UK (ESEF covers annual "
                                    "reports; most issuers report semi-annually)", "fix": "-"},
    {"fields": ["eps_surprise*", "rev_surprise*", "earn.eps_*", "earn.rev_*", "pead.*"], "regions": ["CN", "HK", "APAC"],
     "reason": "no_source", "note": "No free structured quarterly results feed for this market", "fix": "-"},
    # COVERAGE_V2 W1 (2026-10-31): earnings run for the whole `full` universe; a gap is the company's own data
    {"fields": ["eps_surprise*", "rev_surprise*", "earn.eps_*", "earn.rev_*", "pead.*"], "reason": "insufficient_data",
     "note": "Model expectations need the company's quarterly / half-yearly revenue or EPS including the same period a "
             "year earlier (no statements loaded, or annual-only reporting)",
     "fix": "statements coverage (W2: regulator-complete statements)"},
    {"fields": ["earn.car_*", "earn.bhar_*", "earn.ret_*", "earn.ar_*", "earn.beta", "earn.sigma_daily",
                "earn.baseline_*", "earn.move_ratio", "earn.gap_*", "earn.benchmark_id", "earn.trade_reaction_date"],
     "reason": "insufficient_data",
     "note": "Price reactions need ~250 sessions of prices before the release (market-model estimation window)",
     "fix": "by design"},
    {"fields": ["earn.*"], "reason": "insufficient_data",
     "note": "No earnings release recorded for this company yet (no regulatory filing, exchange disclosure or provider "
             "date matched this listing)", "fix": "earnings_dates_fetch (Yahoo / exchange schedules, full universe)"},
    # ---------------------------------------------------------------- identity
    {"fields": ["isin"], "regions": ["JP", "CN"], "reason": "no_source",
     "note": "No free bulk ISIN list wired for TSE / SSE-SZSE codes", "fix": "ref-identity: JPX / CSDC ISIN lists"},
    {"fields": ["isin"], "reason": "mapping_gap", "note": "ISIN not resolved for this listing (no CUSIP / registry "
                                                          "match)", "fix": "ref-identity: OpenFIGI / exchange lists"},
    {"fields": ["cik", "cusip"], "regions": list(NON_US), "reason": "not_applicable",
     "note": "SEC / US identifiers apply to SEC registrants only", "fix": "by design"},
    {"fields": ["lei"], "regions": ["US", "JP", "KR", "CN", "HK", "APAC", "LATAM"], "reason": "no_source",
     "note": "LEI is not carried by this market's filings; GLEIF lookup not wired", "fix": "ref-identity: GLEIF"},
    {"fields": ["figi"], "reason": "out_of_scope", "note": "OpenFIGI mapping was run for the US and EU expansion only",
     "fix": "ref-identity: openfigi refresh (OPENFIGI_APIKEY speeds it up)"},
    {"fields": ["sector", "subsector"], "regions": ["US"], "reason": "mapping_gap",
     "note": "SEC SIC code not yet mapped to the taxonomy",
     "fix": "2026-10-27: identity_resolve SIC backfill for every CIK issuer (3,000 per night)"},
    {"fields": ["sector", "subsector"], "regions": ["KR", "LATAM"], "reason": "mapping_gap",
     "note": "Native industry name not mapped to the taxonomy",
     "fix": "2026-10-27: KSIC / CVM keyword rules extended (next ref_universe run)"},
    {"fields": ["sector", "subsector"], "regions": ["HK"], "reason": "no_source",
     "note": "HKEX industry classification (HSICS) is not a free feed; Yahoo profile not fetched for HK",
     "fix": "ref-identity: Yahoo profile sector for HK"},
    {"fields": ["employees"], "reason": "out_of_scope", "note": "Employee counts come from the company profile of "
                                                              "the analysis universe", "fix": "ref-identity"},
    {"fields": ["size_bucket", "mkt_cap_usd", "ev_usd", "shares_out", "fa.shares_outstanding"],
     "regions": ["JP", "KR", "CN", "HK", "APAC", "LATAM", "UK", "EU", "US"], "reason": "out_of_scope",
     "note": _SHARES_NOTE, "fix": _SCOPE_FIX},
    # ---------------------------------------------------------------- technicals / conformal (job state)
    {"fields": ["cf.*", "macro.*"], "regions": ["US", "LATAM"], "reason": "failed",
     "note": "conformal_envelopes timed out (45 min) on 10-24/25 before the 10-26 fix (90 min, per-region checkpoint); "
             "the next run refreshes US / LATAM", "fix": "sync engine retry (agent A)"},
    {"fields": ["tp.vol_regime"], "regions": ["EU", "UK", "APAC"], "reason": "stale",
     "note": "The 2026-10-23 session bars are missing for EU / UK / APAC; the vol regime needs bars ending on the "
             "snapshot date", "fix": "sync engine: prices per region since watermark (agent A)"},
    {"fields": ["tech.*"], "reason": "not_applicable",
     "note": "Factor percentiles are computed among liquid names with enough history only", "fix": "by design"},
    {"groups": ["technical", "conformal"], "reason": "stale",
     "note": "Not computed for the latest session (stale bars or a pending run)", "fix": "sync engine"},
    {"groups": ["price", "return"], "reason": "stale",
     "note": "No bar since <= 2026-10-13 (delisted / acquired names still in the universe, or a halted listing)",
     "fix": "ref-identity: retire delisted securities from universe full"},
    # ---------------------------------------------------------------- fundamentals / valuation / scores
    # full-universe scores & ML (2026-10-30): ML outside the MVP universe only where the extrapolation check passed
    {"groups": ["model"], "reason": "suppressed",
     "note": "ML forecasts outside the MVP model universe are served (flagged extrapolated) only in the region x "
             "liquidity segments where the out-of-sample check of the MVP-trained model passed; elsewhere withheld "
             "(per-security reason in the tooltip: no validated skill, too few features or no recent price)",
     "fix": "full-scope 2026-10-30: evidence data/state/full_scope.json (ml); re-check after model retrains"},
    {"groups": ["fundamental", "valuation", "score", "factor", "model"], "regions": ["KR"], "reason": "needs_key",
     "note": "Needs OPENDART_APIKEY (Korea DART financial statements)", "fix": "user: add OPENDART_APIKEY"},
    {"groups": ["fundamental", "valuation", "score", "factor", "model"], "regions": ["CN"],
     "reason": "source_unavailable",
     "note": "CN statements: BaoStock is IP-banned and AKShare covers the curated names only",
     "fix": "ingest-global: resume BaoStock with one worker"},
    {"groups": ["fundamental", "valuation", "score", "factor", "model"], "regions": ["HK"], "reason": "no_source",
     "note": "HKEX filings are PDF only (no XBRL); no free structured source", "fix": "-"},
    {"groups": ["fundamental", "valuation", "score", "factor", "model"], "regions": ["APAC"], "reason": "no_source",
     "note": "No free structured statements for IN / AU / SG / TH / ID (TW MOPS covers the TW universe only)",
     "fix": "-"},
    {"fields": ["fa.*"], "sparse": True, "reason": "not_applicable",
     "note": "Line item not reported separately by most issuers (industry-specific or optional disclosure)",
     "fix": "by design"},
    {"groups": ["score", "factor"], "reason": "insufficient_data",
     "note": "Scores cover every security with enough inputs (outside the MVP universe anchored to the MVP reference "
             "cross-section); a composite needs >= 5 factor scores incl. 2 of quality / profitability / value / "
             "growth, so companies without loaded statements, market cap or 126 sessions of prices have none (the "
             "tooltip lists the missing inputs per security)",
     "fix": "statements for the remaining US small caps: sec_xbrl_bulk all_ciks (ingest-us); a shares-outstanding "
            "feed for JP / KR / CN / HK"},
    {"groups": ["fundamental", "valuation", "score", "factor", "model"], "reason": "out_of_scope",
     "note": _SCOPE_NOTE, "fix": _SCOPE_FIX},
    {"groups": ["macro"], "reason": "stale", "note": "Macro exposures not refreshed for this security yet",
     "fix": "macro_exposures weekly"},
    # W3 rates & debt (rates-w3): appended so the rule ids above keep their numbers
    {"fields": ["debt.mat_*", "debt.wam_years", "debt.refi_share_24m", "debt.refi_cost_24m"], "reason": "not_applicable",
     "note": "Contractual maturity schedule not tagged in the filings (US GAAP LongTermDebtMaturities* / IFRS "
             "MaturityAxis): most IFRS and non-US filers do not tag it", "fix": "by design (filings)"},
    {"fields": ["debt.fixed_debt", "debt.floating_debt", "debt.floating_share", "debt.weighted_avg_rate"],
     "reason": "not_applicable", "note": "Fixed / floating split or weighted-average rate not tagged by the issuer",
     "fix": "by design (filings)"},
    {"groups": ["credit"], "reason": "out_of_scope", "note": "No debt items in the issuer's statements loaded "
                                                               "(fundamentals_canonical)", "fix": _SCOPE_FIX},
    {"groups": ["rates"], "reason": "no_source", "note": "No official daily / monthly government curve for the "
                                                          "domicile country in yield_curves", "fix": "app.rates.registry"},
]


# ------------------------------------------------------------------------------------------------ sweep


def _post(base: str, q: str, max_rows: int = 60000, timeout: int = 600) -> dict[str, Any]:
    body = json.dumps({"query": q, "max_rows": max_rows}).encode()
    req = urllib.request.Request(f"{base}/bql", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - local API
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode(errors="replace")[:2000]}


def _values(res: dict[str, Any], fields: list[str]) -> dict[str, dict[str, bool]]:
    """field -> {security_id: has value} from a cross-section result (value columns in request order)."""
    cols = res.get("columns") or []
    sid_i = next(i for i, c in enumerate(cols) if c["name"] == "security_id")
    vi = [i for i, c in enumerate(cols) if c["role"] == "value"]
    out: dict[str, dict[str, bool]] = {f: {} for f in fields}
    for row in res.get("rows") or []:
        sid = row[sid_i]
        for f, i in zip(fields, vi, strict=False):
            out[f][sid] = row[i] is not None
    return out


def sweep(base: str = API, fields: list[str] | None = None, log=print) -> dict[str, Any]:  # noqa: ANN001
    """Sequential BQL sweep: base frame (region / type / universes), then BATCH fields per request."""
    t0 = time.perf_counter()
    base_res = _post(base, "get(region, security_type) for(members('full'))")
    if base_res.get("error"):
        raise RuntimeError(f"BQL base query failed: {base_res}")
    cols = [c["name"] for c in base_res["columns"]]
    ri, ti = cols.index("region"), cols.index("security_type")
    secs = {r[0]: {"region": r[ri], "type": r[ti], "u": ["full"]} for r in base_res["rows"]}
    for u in ("mvp_us", "mvp_global"):
        r = _post(base, f"get(region) for(members('{u}'))")
        for row in r.get("rows") or []:
            if row[0] in secs:
                secs[row[0]]["u"].append(u)
    doc = yaml.safe_load((get_settings().contracts_dir / "bql_fields.yaml").read_text(encoding="utf-8"))
    meta = {f["mnemonic"]: f for f in doc["fields"]}
    names = fields or [m for m in meta if m != "id"]
    has: dict[str, dict[str, bool]] = {}
    errors: dict[str, str] = {}
    for k in range(0, len(names), BATCH):
        batch = names[k:k + BATCH]
        res = _post(base, "get(" + ", ".join(batch) + ") for(members('full'))")
        if res.get("error"):
            for f in batch:  # isolate the failing field
                r1 = _post(base, f"get({f}) for(members('full'))")
                if r1.get("error"):
                    errors[f] = str(r1.get("detail"))[:300]
                else:
                    has.update(_values(r1, [f]))
        else:
            has.update(_values(res, batch))
        log(f"  {min(k + BATCH, len(names))}/{len(names)} fields, {time.perf_counter() - t0:.0f}s")
    cov: dict[str, Any] = {}
    for f, vals in has.items():
        c: dict[str, Any] = {}
        for u in UNIVERSES:
            ids = [s for s, m in secs.items() if u in m["u"]]
            c[u] = round(100.0 * sum(vals.get(s, False) for s in ids) / len(ids), 1) if ids else None
        by_r: dict[str, list[bool]] = defaultdict(list)
        for s, m in secs.items():
            by_r[m["region"] or "OTHER"].append(vals.get(s, False))
        c["regions"] = {r: round(100.0 * sum(v) / len(v), 1) for r, v in by_r.items() if v}
        cov[f] = c
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "as_of": base_res.get("as_of"),
            "serving_run_id": base_res.get("serving_run_id"), "n": {u: sum(u in m["u"] for m in secs.values())
                                                                     for u in UNIVERSES},
            "region_n": dict(Counter(m["region"] or "OTHER" for m in secs.values())), "coverage": cov,
            "errors": errors, "seconds": round(time.perf_counter() - t0, 1),
            "groups": {f: meta[f]["group"] for f in cov if f in meta}}


# ------------------------------------------------------------------------------------------------ classification


def _match(rule: dict[str, Any], field: str, group: str, region: str, cov: dict[str, Any]) -> bool:
    if "fields" in rule and not any(fnmatch.fnmatchcase(field, g) for g in rule["fields"]):
        return False
    if "groups" in rule and group not in rule["groups"]:
        return False
    if rule.get("regions") is not None and region not in rule["regions"]:
        return False
    if rule.get("sparse") and not ((cov.get("mvp_us") or 0) < 70 and (cov.get("mvp_global") or 0) < 70):
        return False
    return True


DEFAULT_RULE = {"id": "default", "reason": "not_applicable", "note": "Not reported for every company", "fix": "-"}


def rule_table() -> dict[str, dict[str, Any]]:
    """id -> {reason, note, fix} of every rule (ids r01.. in RULES order, then `default`)."""
    out = {r.get("id") or f"r{i + 1:02d}": {"reason": r["reason"], "note": r.get("note"), "fix": r.get("fix")}
           for i, r in enumerate(RULES)}
    out["default"] = {k: DEFAULT_RULE[k] for k in ("reason", "note", "fix")}
    return out


def _rule_id(rule: dict[str, Any]) -> str:
    if rule is DEFAULT_RULE:
        return "default"
    return rule.get("id") or f"r{RULES.index(rule) + 1:02d}"


def classify(field: str, group: str, cov: dict[str, Any], region_n: dict[str, int]) -> dict[str, Any]:
    """Field entry of field_availability.yaml: coverage per universe + {region: {pct, rule}} below FRESH_PCT; `rule`
    = the main gap rule (the US one when the US has a gap, else the most common)."""
    regions: dict[str, Any] = {}
    for rg in REGIONS:
        pct = (cov.get("regions") or {}).get(rg)
        if pct is None or region_n.get(rg, 0) < MIN_REGION_N or pct >= FRESH_PCT:
            continue
        rule = next((r for r in RULES if _match(r, field, group, rg, cov)), DEFAULT_RULE)
        regions[rg] = {"pct": pct, "rule": _rule_id(rule)}
    counts = Counter(v["rule"] for v in regions.values())
    default = regions.get("US", {}).get("rule") or (counts.most_common(1)[0][0] if counts else None)
    out: dict[str, Any] = {"group": group, "area": AREAS.get(group, "Other"),
                           "coverage": {u: cov.get(u) for u in UNIVERSES}}
    if default:
        out["rule"] = default
    if regions:
        out["regions"] = regions
    return out


def build(sweep_doc: dict[str, Any]) -> dict[str, Any]:
    rules = rule_table()
    fields = {f: classify(f, sweep_doc["groups"].get(f, "other"), c, sweep_doc.get("region_n") or {})
              for f, c in sweep_doc["coverage"].items()}
    dist = Counter(rules[r["rule"]]["reason"] for f in fields.values() for r in (f.get("regions") or {}).values())
    used = {r["rule"] for f in fields.values() for r in (f.get("regions") or {}).values()}
    used |= {f["rule"] for f in fields.values() if f.get("rule")}
    return {
        "version": 1,
        "generated_by": "app.qa.completeness (python -m app.qa.completeness --write); do not edit by hand",
        "description": ("Measured availability of every BQL field per universe x region on the live snapshot, with the "
                        "reason of each gap (codes: app.core.availability; sync_code = the startup-sync code of "
                        "docs/STARTUP_SYNC_PLAN.md section 1). fields.<f>.coverage = percent of the universe's "
                        "securities with a value; regions.<R> = a gap region (universe full, below "
                        f"{FRESH_PCT:.0f} %) with its coverage and the rule (rules.<id>: reason code, note, fix / "
                        "owner); fields.<f>.rule = the field's main gap rule."),
        "generated_at": sweep_doc["generated_at"], "serving_run_id": sweep_doc.get("serving_run_id"),
        "as_of": sweep_doc.get("as_of"), "universe_size": sweep_doc.get("n"), "region_size": sweep_doc.get("region_n"),
        "reason_codes": {c: {"sync_code": s, "label": lab, "message": m} for c, (s, lab, m) in REASONS.items()},
        "reason_distribution": dict(dist.most_common()),
        "rules": {k: v for k, v in rules.items() if k in used},
        "areas": PAGE_AREAS,
        "aliases": _aliases(set(fields)),
        "fields": fields,
        **_regulator_block(),
    }


def _regulator_block() -> dict[str, Any]:
    """Coverage v2 W2: the regulator coverage summary (job regulatory_coverage) is kept in field_availability.yaml
    across `--write` runs (key regulator_coverage; data/state/regulatory_coverage.json is the source)."""
    try:
        from app.qa.regulatory_coverage import STATE_FILE, field_block

        st = json.loads((get_settings().state_dir / STATE_FILE).read_text(encoding="utf-8"))
        return {"regulator_coverage": field_block(st["summary"], st["as_of"])}
    except (OSError, ValueError, KeyError):
        return {}


def _aliases(known: set[str]) -> dict[str, str]:
    """BQL alias / screener key -> mnemonic (pe_ntm -> pe_fwd, dividend_yield -> div_yld ...)."""
    try:
        doc = yaml.safe_load((get_settings().contracts_dir / "bql_fields.yaml").read_text(encoding="utf-8"))
    except OSError:
        return {}
    out: dict[str, str] = {}
    for f in doc.get("fields") or []:
        for a in f.get("aliases") or []:
            if f["mnemonic"] in known and a not in known:
                out[a] = f["mnemonic"]
    extra = {"revenue_growth": "revenue_growth_yoy", "net_debt_ebitda": "net_debt_to_ebitda", "dividend_yield": "div_yld",
             "market_cap_usd": "mkt_cap_usd", "ml_expected_excess_3m": "ml.exp_excess_3m",
             "macro_beta_rates": "macro.beta_rates", "macro_beta_usd": "macro.beta_usd", "macro_beta_oil": "macro.beta_oil",
             "macro_beta_credit": "macro.beta_credit", "macro_beta_growth_surprise": "macro.beta_growth",
             "macro_impact_rates_down_100bp": "macro.rates_down_100bp", "enterprise_value_usd": "ev_usd",
             "short_pct_float": "si.pct_float", "days_to_cover": "si.days"}
    out.update({k: v for k, v in extra.items() if v in known and k not in known})
    return dict(sorted(out.items()))


#: non-BQL page elements (per-entity reasons come from the API payloads themselves)
PAGE_AREAS: dict[str, dict[str, Any]] = {
    "earnings.upcoming.actuals": {"reason": "not_yet_published", "note": "Not published yet: the company reports on "
                                  "the listed date", "source": "earnings calendar item date"},
    "macro.tile.latest": {"reason": "not_yet_published", "note": "Next official release on the calendar date",
                          "source": "macro release calendar (next_release)"},
    "cross_asset.positioning": {"reason": "no_source", "note": "No CFTC futures market for this asset",
                                "source": "cftc_markets mapping"},
    "security.logo_url": {"reason": "no_source", "note": "Logos are not stored (no licensed logo source)",
                          "source": "-"},
    "security.name_local": {"reason": "not_applicable", "note": "Local-script name only for non-Latin markets",
                            "source": "-"},
    "smart_money.overview.cross_fields": {"reason": "pipeline_bug", "note": "Fixed 2026-10-27: overview lists now "
                                          "carry the 13F, short-interest and score fields of every row",
                                          "source": "app.api.routers.smart_money"},
    "company.factors.delta_1m": {"reason": "stale", "note": "Fixed 2026-10-27: filled by the next scoring run",
                                 "source": "factor_scores.delta_1m"},
}


# ------------------------------------------------------------------------------------------------ report


def _fmt(v: Any) -> str:
    return "–" if v is None else f"{v:.1f}"


def _span(vals: list[Any]) -> str:
    v = [x for x in vals if x is not None]
    if not v:
        return "–"
    return f"{min(v):.1f}" if min(v) == max(v) else f"{min(v):.1f}–{max(v):.1f}"


def markdown(doc: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    rules = doc["rules"]
    base_cov = (baseline or {}).get("coverage") or {}
    L: list[str] = ["# Data completeness (field availability)", "",
                    f"Generated by `python -m app.qa.completeness --write` on {doc['generated_at']} from the live API "
                    f"(snapshot `{doc.get('serving_run_id')}`, data as of {doc.get('as_of')}). "
                    "Machine-readable twin: `contracts/field_availability.yaml` (used by the UI tooltips, "
                    "`GET /api/v1/meta/field-availability`, `GET /api/v1/companies/{id}/availability` and the startup "
                    "sync engine).", "",
                    "**Method.** Every BQL field (`contracts/bql_fields.yaml`) is requested for `members('full')` on "
                    "`POST /api/v1/bql` (compute-only, sequential, 25 fields per request). Coverage = share of "
                    "securities with a non-null value, per universe (full / mvp_us / mvp_global) and per region "
                    f"(universe full). A region below {FRESH_PCT:.0f} % gets a reason from the rule table in "
                    "`app/qa/completeness.py`. Fields with the same gaps share one row.", "",
                    "## Reason codes", "", "| Code | Sync code | Meaning |", "|---|---|---|"]
    for c, v in doc["reason_codes"].items():
        L.append(f"| `{c}` | `{v['sync_code']}` | {v['message']} |")
    L += ["", "## Reason distribution (field x region gaps)", "", "| Reason | Gaps |", "|---|---|"]
    for c, n in doc["reason_distribution"].items():
        L.append(f"| `{c}` | {n} |")
    L += ["", f"Universe sizes: {doc.get('universe_size')}; securities per region (full): {doc.get('region_size')}.", ""]
    by_area: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for f, e in doc["fields"].items():
        by_area[e["area"]].append((f, e))
    for area in list(dict.fromkeys(AREAS.values())):
        items = by_area.get(area) or []
        if not items:
            continue
        cov_full = sorted(e["coverage"]["full"] or 0 for _, e in items)
        line = f"{len(items)} fields; median coverage (universe full) {cov_full[len(cov_full) // 2]:.1f} %"
        if base_cov:
            bf = sorted((base_cov.get(f) or {}).get("full") or 0 for f, _ in items)
            line += f" (before the fixes: {bf[len(bf) // 2]:.1f} %)"
        L += [f"## {area}", "", line + ".", ""]
        groups: dict[tuple, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for f, e in items:
            sig = tuple((rg, v["rule"]) for rg, v in (e.get("regions") or {}).items())
            groups[sig].append((f, e))
        hdr = ("| Fields | Full % | " + ("Before % | " if base_cov else "")
               + "mvp_us % | mvp_global % | Gaps: region % → reason (rule) |")
        L += [hdr, "|" + "---|" * (hdr.count("|") - 1)]
        for sig, fl in sorted(groups.items(), key=lambda kv: min(x[1]["coverage"]["full"] or 0 for x in kv[1])):
            names = [f for f, _ in fl]
            shown = ", ".join(f"`{n}`" for n in names[:10]) + (f" +{len(names) - 10} more" if len(names) > 10 else "")
            before = f" {_span([(base_cov.get(f) or {}).get('full') for f in names])} |" if base_cov else ""
            gl = []
            for rg, rid in sig:
                pc = [x[1]["regions"][rg]["pct"] for x in fl]
                rr = f"{min(pc):.0f}" if min(pc) == max(pc) else f"{min(pc):.0f}–{max(pc):.0f}"
                gl.append(f"{rg} {rr} → `{rules[rid]['reason']}` ({rid})")
            L.append(f"| {shown} | {_span([x[1]['coverage']['full'] for x in fl])} |{before} "
                     f"{_span([x[1]['coverage']['mvp_us'] for x in fl])} | "
                     f"{_span([x[1]['coverage']['mvp_global'] for x in fl])} | {'; '.join(gl) or 'none'} |")
        L.append("")
    L += ["## Rules (reason, note, fix / owner)", "", "| Rule | Reason | Note | Fix / owner |", "|---|---|---|---|"]
    for rid, r in rules.items():
        L.append(f"| {rid} | `{r['reason']}` | {r.get('note') or ''} | {r.get('fix') or ''} |")
    L += ["", "## Page elements outside BQL", "", "| Element | Reason | Note | Source |", "|---|---|---|---|"]
    for k, v in doc["areas"].items():
        L.append(f"| `{k}` | `{v['reason']}` | {v['note']} | {v['source']} |")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------ CLI


def write(doc: dict[str, Any], md: str, settings: Settings | None = None, docs_path: Path | None = None) -> None:
    s = settings or get_settings()
    (s.contracts_dir / YAML_NAME).write_text(
        "# Generated by app.qa.completeness - do not edit by hand (python -m app.qa.completeness --write)\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120, default_flow_style=None),
        encoding="utf-8")
    p = docs_path or (s.contracts_dir.parent / "docs" / "DATA_COMPLETENESS.md")
    head = ""
    if p.exists():  # keep the hand-written preamble above the generated part
        old = p.read_text(encoding="utf-8")
        mark = "<!-- generated: app.qa.completeness -->"
        if mark in old:
            head = old.split(mark, 1)[0] + mark + "\n\n"
    p.write_text(head + md, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.qa.completeness")
    ap.add_argument("--base", default=API)
    ap.add_argument("--from-json", dest="from_json")
    ap.add_argument("--baseline")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    s = get_settings()
    if a.from_json:
        sw = json.loads(Path(a.from_json).read_text(encoding="utf-8"))
    else:
        sw = sweep(a.base)
        out = s.data_dir / "reports" / "completeness"
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"coverage_{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
        p.write_text(json.dumps(sw), encoding="utf-8")
        print(f"sweep saved to {p} ({sw['seconds']} s, {len(sw['coverage'])} fields, {len(sw['errors'])} errors)")
    doc = build(sw)
    base = json.loads(Path(a.baseline).read_text(encoding="utf-8")) if a.baseline else None
    md = markdown(doc, base)
    if a.write:
        write(doc, md, s)
        print(f"wrote contracts/{YAML_NAME} and docs/DATA_COMPLETENESS.md; reasons {doc['reason_distribution']}")
    else:
        print(json.dumps(doc["reason_distribution"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
