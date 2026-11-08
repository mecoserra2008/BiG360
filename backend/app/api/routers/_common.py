"""Helpers shared by the routers (owner: api): SecurityRef projection, metric / screener-field registries built from
the YAML contracts, region parsing, zone shaping, problem helpers."""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from fastapi import HTTPException, Query

from app.api.serving import FACTORS, HORIZONS, contract_yaml, metric_registry, profiles
from app.core.config import get_settings

REGIONS = ("US", "CA", "LATAM", "UK", "EU", "JP", "KR", "CN", "HK", "APAC", "OTHER")
MACRO_SCOPE = {"US": "US", "CA": "US", "EU": "EU", "UK": "EU", "JP": "JP", "CN": "CN", "HK": "CN"}

# ------------------------------------------------------------------------------------------------ errors


def not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


# ------------------------------------------------------------------------------------------------ small utils


def jload(v: Any, default: Any = None) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def iso(v: Any) -> Any:
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return v


def fnum(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def rnd(v: Any, nd: int = 6) -> float | None:
    f = fnum(v)
    return None if f is None else round(f, nd)


def parse_regions(region: str | None, regions: list[str] | None = None) -> list[str]:
    """`region` (single, or comma-separated) and/or `regions` (repeated or comma-separated) -> validated list."""
    out: list[str] = []
    for raw in ([region] if region else []) + list(regions or []):
        for r in str(raw).split(","):
            r = r.strip().upper()
            if not r:
                continue
            if r not in REGIONS:
                raise invalid(f"unknown region {r!r}; expected one of {', '.join(REGIONS)}")
            if r not in out:
                out.append(r)
    return out


def regions_query() -> Any:
    return Query(None, description="Several regions (repeat the parameter or comma-separate); combined with `region`")


def region_clause(regs: list[str], col: str = "s.region") -> tuple[str, list[Any]]:
    if not regs:
        return "TRUE", []
    return f"{col} IN ({', '.join('?' for _ in regs)})", list(regs)


@lru_cache(maxsize=1)
def peer_region_map() -> dict[str, str]:
    return dict(contract_yaml("scoring.yaml")["peer_groups"]["peer_region_map"])


def profile_weights(profile_id: str | None) -> dict[str, float]:
    p = profiles()
    pid = profile_id or "balanced"
    if pid not in p:
        raise invalid(f"unknown profile_id {pid!r}; expected one of {', '.join(p)}")
    return p[pid]


def check_profile(profile_id: str | None) -> str:
    pid = profile_id or "balanced"
    profile_weights(pid)
    return pid


# ------------------------------------------------------------------------------------------------ SecurityRef

SEC_REF_COLS = ("security_id", "issuer_id", "ticker", "name", "name_local", "mic", "region", "country", "sector_id",
                "subsector_id", "currency", "market_cap_usd", "logo_url", "isin", "exchange_name", "lei")


#: securities_master types that are market instruments (price_history FX pairs, futures, indices incl. volatility
#: indices, ETFs): never part of equity cross-sections (screener, breadth, movers); /instruments serves them
INSTRUMENT_TYPES = ("fx", "future", "index", "etf")


def equity_only_sql(alias: str = "s") -> str:
    """SQL predicate on serving `sec` that drops market instruments (null type = equity)."""
    types = ", ".join(f"'{t}'" for t in INSTRUMENT_TYPES)
    return f"coalesce({alias}.security_type, 'common') NOT IN ({types})"


def sec_ref_sql(alias: str = "s", prefix: str = "ref_") -> str:
    return ", ".join(f"{alias}.{c} AS {prefix}{c}" for c in SEC_REF_COLS)


def sec_ref(r: dict[str, Any], prefix: str = "ref_") -> dict[str, Any]:
    o = {c: r.get(prefix + c) for c in SEC_REF_COLS}
    o["ticker"] = o["ticker"] or o["security_id"]
    o["name"] = o["name"] or o["ticker"]
    o["region"] = o["region"] if o["region"] in REGIONS else "OTHER"
    o["country"] = o["country"] or ""
    o["currency"] = o["currency"] or ""
    o["market_cap_usd"] = fnum(o["market_cap_usd"])
    return o


# ------------------------------------------------------------------------------------------------ metric registry

_TOKENS = {"roic": "ROIC", "roe": "ROE", "roa": "ROA", "croic": "CROIC", "eps": "EPS", "fcf": "FCF", "ebitda": "EBITDA",
           "ebit": "EBIT", "yoy": "YoY", "sbc": "SBC", "rnd": "R&D", "dso": "DSO", "dio": "DIO", "dpo": "DPO",
           "ttm": "TTM", "pe": "P/E", "pb": "P/B", "ps": "P/S", "ev": "EV", "ffo": "FFO", "affo": "AFFO", "tbv": "TBV",
           "cagr": "CAGR", "3y": "3Y", "5y": "5Y", "1y": "1Y", "nim": "NIM", "cet1": "CET1", "ppop": "PPOP",
           "rote": "ROTE", "bvps": "BVPS", "dps": "DPS", "gm": "Gross margin", "stdev": "st.dev.", "z2": "Z''",
           "f": "F-score", "m": "M-score", "mom": "Momentum", "lae": "LAE", "rpo": "RPO", "ntm": "NTM", "usd": "USD",
           "atr": "ATR", "ml": "ML", "pct": "%", "52w": "52w", "200d": "200d", "1m": "1M", "3m": "3M", "6m": "6M",
           "12m": "12M", "1d": "1D", "1w": "1W", "ytd": "YTD", "adv": "ADV", "dq": "DQ", "mcap": "mkt cap"}


def label_for(mid: str) -> str:
    parts = [p for p in re.split(r"_", mid) if p]
    words = [_TOKENS.get(p, p) for p in parts]
    s = " ".join(words)
    return s[:1].upper() + s[1:]


_MARKET_LABELS = {"mom_12_1": "Momentum 12-1", "mom_6_1": "Momentum 6-1", "mom_1m": "1M return (reversal)",
                  "high_52w_proximity": "Proximity to 52w high", "trend_200d": "Distance to 200d average",
                  "vol_1y": "Volatility 1Y"}


@lru_cache(maxsize=1)
def _factor_of_metric() -> dict[str, str]:
    sc = contract_yaml("scoring.yaml")
    kp = contract_yaml("sector_kpis.yaml")
    out: dict[str, str] = {}
    for f in sc["factors"]:
        names = list((f.get("members") or {}).keys())
        for ov in (f.get("overrides") or {}).values():
            names += list(ov.keys())
        names += list((f.get("accounting_block") or {}).keys())
        for m in names:
            out.setdefault(m, f["id"])
    for ks in kp["kpi_sets"]:
        for m in (ks.get("quality_weights") or {}):
            out.setdefault(m, "quality")
    return out


@lru_cache(maxsize=1)
def metric_meta() -> list[dict[str, Any]]:
    """MetricMeta rows (contracts/api.yaml) + internal keys: factor_id, column (sec table column)."""
    kp = contract_yaml("sector_kpis.yaml")
    sc = contract_yaml("scoring.yaml")
    fof = _factor_of_metric()
    sets_of: dict[str, list[str]] = {}
    for ks in kp["kpi_sets"]:
        for m in list((ks.get("quality_weights") or {})) + list(ks.get("peer_metrics") or []):
            sets_of.setdefault(m, [])
            if ks["id"] not in sets_of[m]:
                sets_of[m].append(ks["id"])
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(mid: str, label: str, unit: str, hib: Any, source: str, factor: str, column: str, target: Any = None,
            formula: str | None = None, kpi_sets: list[str] | None = None) -> None:
        if mid in seen:
            return
        seen.add(mid)
        out.append({"id": mid, "label": label, "unit": unit or "", "higher_is_better": hib, "target": target,
                    "source": source, "kpi_set_ids": kpi_sets if kpi_sets is not None else sets_of.get(mid, []),
                    "formula": formula, "factor_id": factor, "column": column})

    for m in kp.get("generic_metrics", []):
        add(m["id"], m.get("label") or label_for(m["id"]), m.get("unit", ""), m.get("higher_is_better"), "ratios",
            fof.get(m["id"], "quality"), "m_" + m["id"], m.get("target"), m.get("formula"))
    for m in kp.get("accounting_scores", []):
        add(m["id"], {"piotroski_f": "Piotroski F-score", "altman_z2": "Altman Z''", "altman_z": "Altman Z",
                      "beneish_m": "Beneish M-score", "sloan_accruals": "Sloan accruals"}.get(m["id"], label_for(m["id"])),
            m.get("unit", "score"), m.get("higher_is_better"), "ratios", fof.get(m["id"], "quality"), "m_" + m["id"],
            None, m.get("formula"))
    for ks in kp.get("kpi_sets", []):
        for k in ks.get("kpis", []) or []:
            if k["id"] in seen:
                for o in out:
                    if o["id"] == k["id"] and ks["id"] not in o["kpi_set_ids"]:
                        o["kpi_set_ids"].append(ks["id"])
                continue
            add(k["id"], k.get("label") or label_for(k["id"]), k.get("unit", ""), k.get("higher_is_better"),
                "sector_kpis", fof.get(k["id"], "quality"), "k_" + k["id"], k.get("target"), k.get("formula"),
                [ks["id"]] + [x for x in sets_of.get(k["id"], []) if x != ks["id"]])
    for mid, spec in (sc.get("valuation_metrics") or {}).items():
        add(mid, label_for(mid), "x" if mid.startswith(("pe", "pb", "p_", "ev_")) else "pct",
            spec.get("higher_is_better"), "valuation", fof.get(mid, "value"), mid)
    for mid, spec in (sc.get("market_metrics") or {}).items():
        add(mid, _MARKET_LABELS.get(mid, label_for(mid)), "pct" if mid != "high_52w_proximity" else "ratio",
            spec.get("higher_is_better"), "market", "momentum", "chg_1m" if mid == "mom_1m" else mid,
            formula=spec.get("formula"))
    for mid, spec in (sc.get("macro_metrics") or {}).items():
        add(mid, label_for(mid), "score", spec.get("higher_is_better"), "macro", "macro", "f_macro",
            formula=spec.get("formula"))
    src_col = {"insider_signal": ("insider", "insider", "insider_signal", "score"),
               "sentiment_ewm_30d": ("sentiment", "sentiment", "sentiment_score", "score"),
               "gdelt_tone_ewm_7d": ("sentiment", "sentiment", "attention_z", "score"),
               "lazy_prices_pct": ("sentiment", "sentiment", "sentiment_score", "pct"),
               "moat_score": ("moat", "moat", "moat_score", "score"),
               "rdcf_implied_vs_hist": ("forecast", "value", "fv_implied_cagr", "pct")}
    for mid, spec in (sc.get("other_metrics") or {}).items():
        source, factor, col, unit = src_col.get(mid, (spec.get("source", "ratios"), "quality", mid, ""))
        add(mid, label_for(mid), unit, spec.get("higher_is_better"), source, factor, col)
    return out


@lru_cache(maxsize=1)
def metric_index() -> dict[str, dict[str, Any]]:
    return {m["id"]: m for m in metric_meta()}


def metric_public(m: dict[str, Any]) -> dict[str, Any]:
    return {k: m[k] for k in ("id", "label", "unit", "higher_is_better", "target", "source", "kpi_set_ids", "formula")}


@lru_cache(maxsize=1)
def kpi_sets() -> dict[str, dict[str, Any]]:
    return {ks["id"]: ks for ks in contract_yaml("sector_kpis.yaml")["kpi_sets"]}


@lru_cache(maxsize=1)
def taxonomy() -> dict[str, Any]:
    tax = contract_yaml("sector_taxonomy.yaml")
    sectors = {s["id"]: s["name"] for s in tax["sectors"]}
    subs = {s["id"]: {"name": s["name"], "sector": s["sector"]} for s in tax["subsectors"]}
    return {"sectors": sectors, "subsectors": subs, "order": [s["id"] for s in tax["sectors"]]}


# ------------------------------------------------------------------------------------------------ screener fields


@lru_cache(maxsize=1)
def screener_fields() -> dict[str, dict[str, Any]]:
    """field id -> {id, label, group, type, unit, enum_values, higher_is_better, sql}. `sql` is the sec column/expr."""
    tax = taxonomy()
    F: dict[str, dict[str, Any]] = {}

    def add(fid: str, label: str, group: str, typ: str, sql: str, unit: str | None = None,
            enum: list[str] | None = None, hib: Any = None) -> None:
        F.setdefault(fid, {"id": fid, "label": label, "group": group, "type": typ, "unit": unit, "enum_values": enum,
                           "higher_is_better": hib, "sql": sql})

    add("ticker", "Ticker", "identity", "string", "ticker")
    add("name", "Name", "identity", "string", "name")
    add("isin", "ISIN", "identity", "string", "isin")
    add("country", "Country", "identity", "string", "country")
    add("region", "Region", "identity", "enum", "region", enum=list(REGIONS))
    add("sector_id", "Sector", "identity", "enum", "sector_id", enum=list(tax["sectors"]))
    add("subsector_id", "Subsector", "identity", "enum", "subsector_id", enum=list(tax["subsectors"]))
    add("currency", "Trading currency", "identity", "string", "currency")
    add("exchange_name", "Exchange", "identity", "string", "exchange_name")
    add("mic", "MIC", "identity", "string", "mic")
    add("size_bucket", "Size bucket", "identity", "enum", "size_bucket", enum=["large", "mid", "small"])
    add("security_type", "Security type", "identity", "string", "security_type")
    add("composite", "Composite score", "score", "number", "composite", "score", hib=True)
    add("grade", "Grade", "score", "enum", "grade", enum=["A", "B", "C", "D", "E"])
    add("rank", "Rank", "score", "number", "rank", hib=False)
    add("dq_score", "Data quality", "score", "number", "dq_score", "score", hib=True)
    add("moat_class", "Moat", "score", "enum", "moat_class", enum=["wide", "narrow", "none", "insufficient_data"])
    add("moat_score", "Moat score", "score", "number", "moat_score", "score", hib=True)
    for f in FACTORS:
        add(f, label_for(f).replace("Balance sheet", "Balance-sheet health"), "factor", "number", f"f_{f}", "score", hib=True)
    for c, lab, unit, hib in (("market_cap_usd", "Market cap (USD)", "currency_usd", None),
                              ("enterprise_value_usd", "Enterprise value (USD)", "currency_usd", None),
                              ("pe_ntm", "P/E NTM (base forecast)", "x", False), ("fv_upside", "DCF upside", "pct", True),
                              ("ev_ebit", "EV / EBIT", "x", False), ("ps_ttm", "P/S TTM", "x", False),
                              ("fair_value", "DCF fair value", "currency", None)):
        add(c, lab, "valuation", "number", "fv_weighted" if c == "fair_value" else c, unit, hib=hib)
    for m in metric_meta():
        if m["source"] == "valuation":
            add(m["id"], m["label"], "valuation", "number", m["column"], m["unit"], hib=m["higher_is_better"])
    for c, lab, unit, hib in (("price", "Price", "currency", None), ("chg_1d", "1D change", "pct", True),
                              ("chg_1w", "1W change", "pct", True), ("chg_1m", "1M change", "pct", True),
                              ("chg_3m", "3M change", "pct", True), ("chg_6m", "6M change", "pct", True),
                              ("ret_12m", "12M return", "pct", True), ("chg_ytd", "YTD return", "pct", True),
                              ("mom_12_1", "Momentum 12-1", "pct", True), ("mom_6_1", "Momentum 6-1", "pct", True),
                              ("high_52w_proximity", "Proximity to 52w high", "ratio", True),
                              ("trend_200d", "Distance to 200d average", "pct", True), ("vol_1y", "Volatility 1Y", "pct", False),
                              ("adv_usd_20d", "Avg dollar volume 20d (USD)", "currency_usd", True),
                              ("volume_z", "Volume z-score", "z", None), ("beta_1y", "Beta 1Y", "x", None),
                              ("short_pct_float", "Short interest % float", "pct", False),
                              ("days_to_cover", "Days to cover", "days", False),
                              ("above_200d", "Above 200d average", "", None), ("new_high_52w", "New 52w high", "", None)):
        typ = "boolean" if c in ("above_200d", "new_high_52w") else "number"
        add(c, lab, "market", typ, "close" if c == "price" else c, unit or None, hib=hib)
    for m in metric_meta():
        if m["source"] == "ratios":
            add(m["id"], m["label"], "fundamental", "number", m["column"], m["unit"], hib=m["higher_is_better"])
    add("revenue_growth", "Revenue growth YoY", "fundamental", "number", "m_revenue_growth_yoy", "pct", hib=True)
    add("net_debt_ebitda", "Net debt / EBITDA", "fundamental", "number", "m_net_debt_to_ebitda", "x", hib=False)
    for m in metric_meta():
        if m["source"] == "sector_kpis":
            add("kpi_" + m["id"], m["label"], "sector_kpi", "number", m["column"], m["unit"], hib=m["higher_is_better"])
    add("insider_signal", "Insider signal", "insider", "number", "insider_signal", "score", hib=True)
    add("insider_cluster_buy", "Insider cluster buy", "insider", "boolean", "ins_cluster_buy")
    add("insider_net_buy_90d_usd", "Insider net buying 90d (USD)", "insider", "number", "ins_net_buy_90d", "currency_usd", hib=True)
    add("sentiment_score", "Sentiment score", "sentiment", "number", "sentiment_score", "score", hib=True)
    add("news_count_30d", "News count 30d", "sentiment", "number", "news_count_30d", "count")
    add("attention_z", "Media attention z", "sentiment", "number", "attention_z", "z")
    add("zone_distance_atr", "Distance to top zone (ATR)", "technical", "number", "tz_distance_atr", "atr")
    add("zone_side", "Top zone side", "technical", "enum", "tz_side", enum=["support", "resistance"])
    add("zone_reward_risk", "Top zone reward/risk", "technical", "number", "tz_rr", "x", hib=True)
    add("atr14", "ATR 14", "technical", "number", "atr14", "currency")
    for h in HORIZONS:
        hl = h.lower()
        add(f"ml_rank_pct_{hl}", f"ML rank pct {h}", "model", "number", f"ml_rp_{h}", "rank_pct", hib=True)
        add(f"ml_decile_{hl}", f"ML decile {h}", "model", "number", f"ml_dec_{h}", "decile", hib=True)
        add(f"ml_expected_excess_{hl}", f"ML expected excess return {h}", "model", "number", f"ml_exp_{h}", "pct", hib=True)
    # macro exposures (macro-w4; model-based rolling betas, shrunk; see /macro/v4/exposures)
    add("macro_beta_rates", "Beta to 10y yield (+100bp)", "macro", "number", "mx_beta_rates", "x")
    add("macro_beta_usd", "Beta to broad USD (elasticity)", "macro", "number", "mx_beta_usd", "x")
    add("macro_beta_oil", "Beta to WTI oil (elasticity)", "macro", "number", "mx_beta_oil", "x")
    add("macro_beta_credit", "Beta to credit spread (+100bp)", "macro", "number", "mx_beta_credit", "x")
    add("macro_beta_growth_surprise", "Beta to growth surprise (+100 pts)", "macro", "number", "mx_beta_growth", "x")
    add("macro_impact_rates_down_100bp", "Impact if rates fall 100bp (model)", "macro", "number", "mx_rates_down_100",
        "pct", hib=True)
    return F


def field_sql(fid: str) -> str:
    f = screener_fields().get(fid)
    if f is None:
        raise invalid(f"unknown field {fid!r} (see GET /api/v1/screener/fields)")
    return f["sql"]


def screener_column_ids() -> list[str]:
    """Pinned (non-dynamic) screener field ids — published as ScreenerColumnId in contracts/api.yaml."""
    return [k for k in screener_fields() if not k.startswith("kpi_")]


# ------------------------------------------------------------------------------------------------ zones


def zone_out(z: dict[str, Any], targets: list[float] | None = None) -> dict[str, Any]:
    return {
        "zone_id": int(z["zone_id"]), "side": z["side"], "zone_low": fnum(z["zone_low"]) or 0.0,
        "zone_high": fnum(z["zone_high"]) or 0.0, "center": fnum(z["center"]) or 0.0,
        "density": fnum(z.get("density")) or 0.0, "strength": fnum(z.get("strength")) or 0.0, "rank": int(z["rank"]),
        "distance_atr": fnum(z.get("distance_atr")) or 0.0, "member_level_keys": list(z.get("member_level_keys") or []),
        "entry": fnum(z.get("entry")), "invalidation": fnum(z.get("invalidation")), "target": fnum(z.get("target")),
        "reward_risk": fnum(z.get("reward_risk")),
        "targets": targets if targets is not None else ([fnum(z["target"])] if fnum(z.get("target")) is not None else []),
    }


def zone_targets(z: dict[str, Any], zones: list[dict[str, Any]], last_close: float | None, n: int = 3) -> list[float]:
    """Stacked targets for a trade plan: the zone's own target, then centers of opposite-side zones beyond it."""
    out: list[float] = []
    t1 = fnum(z.get("target"))
    if t1 is not None:
        out.append(round(t1, 6))
    px = last_close if last_close is not None else fnum(z.get("center"))
    if px is None:
        return out
    if z["side"] == "support":
        cands = sorted(fnum(o["center"]) for o in zones if o["side"] == "resistance" and fnum(o["center"]) and o["center"] > px)
        cands = [c for c in cands if t1 is None or c > t1 * 1.0001]
    else:
        cands = sorted((fnum(o["center"]) for o in zones if o["side"] == "support" and fnum(o["center"])
                        and o["center"] < px), reverse=True)
        cands = [c for c in cands if t1 is None or c < t1 * 0.9999]
    for c in cands:
        if len(out) >= n:
            break
        out.append(round(c, 6))
    return out


def settings_contracts_dir() -> str:
    return str(get_settings().contracts_dir)
