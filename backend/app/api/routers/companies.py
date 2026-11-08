"""Company 360 (tag: company). All reads hit the serving snapshot; `overview` is the above-the-fold aggregate."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api import pricebars
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import (
    FACTORS,
    MACRO_SCOPE,
    check_profile,
    fnum,
    iso,
    jload,
    kpi_sets,
    label_for,
    metric_index,
    metric_public,
    not_found,
    peer_region_map,
    profile_weights,
    rnd,
    sec_ref,
    sec_ref_sql,
    zone_out,
    zone_targets,
)
from app.api.serving import contract_yaml

router = APIRouter(tags=["company"], route_class=FastRoute)

PEER_LEVELS = ("subsector_region_size", "subsector_region", "subsector_global", "sector_region", "sector_global")
SHAP_UNIT = ("Contribution to the model's raw score (predicted cross-sectional rank-gauss label, ~standard-normal "
             "units); contributions + model base value = score. Positive = pushes the stock up the ranking.")


# ------------------------------------------------------------------------------------------------ helpers


def load_sec(db: Db, security_id: str) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')}, s.* FROM sec s WHERE s.security_id = ?", [security_id])
    if r is None:
        raise not_found(f"unknown security_id {security_id!r}")
    return r


_SEC_ORDER = ("coalesce(s.is_active, TRUE) DESC, coalesce(s.is_primary_security, FALSE) DESC, "
              "s.market_cap_usd DESC NULLS LAST, s.ticker")


def resolve_sec(db: Db, ident: str) -> dict[str, Any]:
    """load_sec() that also accepts a ticker, an ISIN or a ticker alias (coverage v3 A7: the Debt tab returned 404
    "unknown security_id 'AAPL'" for /company/AAPL). Precedence: the internal id, then the exact ticker
    (case-insensitive) or the ISIN (ties -> active, primary listing, larger market cap), then the ticker without its
    exchange suffix / with '-' read as '.' (7203 -> 7203.T, BRK.B -> BRK-B). An alias resolves only when all its
    matches belong to ONE issuer (its active / primary listing); a root shared by several issuers (2317 = 2317.TW Hon
    Hai, 2317.T Systena, 2317.HK Vedan) is a 404 naming the candidate tickers - never another company's data."""
    from fastapi import HTTPException  # noqa: PLC0415

    try:
        return load_sec(db, ident)
    except HTTPException:  # not an internal id: try the ticker forms below
        pass
    t = (ident or "").strip().upper()
    if not t or len(t) > 64:
        raise not_found(f"unknown security_id or ticker {ident!r}")
    r = db.one(f"""SELECT {sec_ref_sql('s')}, s.* FROM sec s
                  WHERE upper(s.ticker) = ? OR upper(coalesce(s.isin, '')) = ?
                  ORDER BY CASE WHEN upper(s.ticker) = ? THEN 0 ELSE 1 END, {_SEC_ORDER}
                  LIMIT 1""", [t] * 3)
    if r is not None:
        return r
    alias = db.all(f"""SELECT {sec_ref_sql('s')}, s.* FROM sec s
                      WHERE upper(split_part(s.ticker, '.', 1)) = ? OR replace(upper(s.ticker), '-', '.') = ?
                      ORDER BY {_SEC_ORDER} LIMIT 200""", [t, t])
    issuers = {a.get("issuer_id") or a.get("security_id") for a in alias}
    if len(issuers) == 1:
        return alias[0]
    if len(issuers) > 1:
        cands = sorted({str(a.get("ticker")) for a in alias})
        more = f" (+{len(cands) - 10} more)" if len(cands) > 10 else ""
        raise not_found(f"ambiguous ticker {ident!r}: {len(issuers)} issuers ({', '.join(cands[:10])}{more}); "
                        f"use the full ticker with its exchange suffix")
    raise not_found(f"unknown security_id or ticker {ident!r}")


def _score_scope(r: dict[str, Any]) -> str | None:
    """mvp = scored in the MVP model universe; extended = anchored to the MVP reference (full-universe extension)."""
    if r.get("score_scope"):
        return str(r["score_scope"])
    return "mvp" if (r.get("u_mvp_us") or r.get("u_mvp_global")) else None


def _score_reason(r: dict[str, Any], pid: str, factor: str | None = None) -> dict[str, Any] | None:
    from app.core import availability as AV  # noqa: PLC0415

    miss = r.get("score_missing")
    if factor is None and r.get(f"cr_{pid}"):
        return AV.make("insufficient_data", AV.describe_missing(miss)).public()
    if factor is not None and miss and factor in AV.parse_missing(miss)["factors"]:
        return AV.make("insufficient_data", AV.describe_missing(miss, factor)).public()
    return None


def _composite(r: dict[str, Any], pid: str) -> dict[str, Any]:
    from app.analytics.composite_gate import composite_label  # noqa: PLC0415

    g = r.get(f"g_{pid}")
    # coverage v3 A8: 'descriptive' unless the profile passed its out-of-sample gate AND this composite lies inside the
    # population the gate tested (its score scope and security: a US-only pass never validates a JP home listing or
    # an extended-scope score)
    label = composite_label(pid, scope=_score_scope(r), security_id=r.get("security_id"))
    return {"profile_id": pid, "score": rnd(r.get(f"c_{pid}"), 4), "grade": g if g in ("A", "B", "C", "D", "E") else None,
            "label": label, "validated": label == "validated",
            "rank_global": r.get(f"rk_{pid}"), "rank_sector": r.get(f"rks_{pid}"), "coverage": fnum(r.get(f"cc_{pid}")),
            "dq_score": fnum(r.get("dq_score")),
            # v1.44 full-universe extension: scope, ranks among all scored securities (region x sector), reason
            "scope": _score_scope(r), "rank_global_full": r.get(f"rgf_{pid}"), "n_global_full": r.get(f"ngf_{pid}"),
            "rank_sector_full": r.get(f"rsf_{pid}"), "n_sector_full": r.get(f"nsf_{pid}"),
            "reason": _score_reason(r, pid) if r.get(f"c_{pid}") is None else None}


def _factor_medians(db: Db, r: dict[str, Any]) -> dict[str, float | None]:
    """Median factor score of sector peers (same sector x region, falling back to the global sector when < 8)."""
    sel = ", ".join(f"median(f_{f}) FILTER (region = ?) AS r_{f}, median(f_{f}) AS g_{f}" for f in FACTORS)
    params: list[Any] = [r.get("region")] * len(FACTORS)
    row = db.one(f"SELECT count(*) FILTER (region = ?) AS n_r, {sel} FROM sec WHERE sector_id = ?",
                 [r.get("region")] + params + [r.get("sector_id")]) if r.get("sector_id") else None
    if not row:
        return {f: None for f in FACTORS}
    use_r = (row["n_r"] or 0) >= 8
    return {f: rnd(row[f"r_{f}"] if use_r else row[f"g_{f}"], 4) for f in FACTORS}


def _factors(db: Db, r: dict[str, Any]) -> list[dict[str, Any]]:
    med = _factor_medians(db, r)
    out = []
    for f in FACTORS:
        out.append({"factor_id": f, "score": rnd(r.get(f"f_{f}"), 4), "coverage": fnum(r.get(f"fc_{f}")) or 0.0,
                    "rank_in_sector": r.get(f"fr_{f}"), "sector_count": r.get(f"fn_{f}"), "delta_1m": fnum(r.get(f"fd_{f}")),
                    "peer_median": med[f], "rank_in_sector_full": r.get(f"frf_{f}"),
                    "sector_count_full": r.get(f"fnf_{f}"),
                    "reason": _score_reason(r, "balanced", f) if r.get(f"f_{f}") is None else None})
    return out


def _metric_scores(db: Db, r: dict[str, Any], metric_ids: list[str] | None) -> list[dict[str, Any]]:
    """MetricScore rows: peer_scores (scoring job) when present, else raw value + live subsector median."""
    mi = metric_index()
    rows = {x["metric_id"]: x for x in db.all("SELECT * FROM peer_latest WHERE security_id = ?", [r["security_id"]])}
    ids = metric_ids if metric_ids is not None else list(rows)
    if metric_ids is None and not rows:
        ids = []
    cols = {m: mi[m]["column"] for m in ids if m in mi and m not in rows}
    med: dict[str, Any] = {}
    if cols and r.get("subsector_id"):
        sel = ", ".join(f'median("{c}") AS "md_{m}", count("{c}") AS "n_{m}"' for m, c in cols.items())
        med = db.one(f"SELECT {sel} FROM sec WHERE subsector_id = ?", [r["subsector_id"]]) or {}
    out = []
    for m in ids:
        meta = mi.get(m, {})
        p = rows.get(m)
        if p:
            out.append({"metric_id": m, "label": meta.get("label") or label_for(m),
                        "factor_id": p.get("factor_id") if p.get("factor_id") in FACTORS else meta.get("factor_id", "quality"),
                        "unit": meta.get("unit"), "higher_is_better": meta.get("higher_is_better"),
                        "raw_value": fnum(p.get("raw_value")), "peer_percentile": fnum(p.get("peer_percentile")),
                        "peer_median": fnum(p.get("peer_median")), "peer_group_key": p.get("peer_group_key"),
                        "peer_group_level": p.get("peer_group_level"), "peer_count": p.get("peer_count"),
                        "self_z": fnum(p.get("self_z")), "self_score": fnum(p.get("self_score")),
                        "mcap_adj_score": fnum(p.get("mcap_adj_score")), "metric_score": fnum(p.get("metric_score"))})
        elif m in mi:
            c = mi[m]["column"]
            out.append({"metric_id": m, "label": meta["label"], "factor_id": meta["factor_id"], "unit": meta["unit"],
                        "higher_is_better": meta["higher_is_better"], "raw_value": fnum(r.get(c)),
                        "peer_percentile": None, "peer_median": fnum(med.get(f"md_{m}")),
                        "peer_group_key": f"subsector_global:{r.get('subsector_id')}" if r.get("subsector_id") else None,
                        "peer_group_level": "subsector_global" if r.get("subsector_id") else None,
                        "peer_count": med.get(f"n_{m}"), "self_z": None, "self_score": None, "mcap_adj_score": None,
                        "metric_score": None})
    return out


def _beneish(db: Db, r: dict[str, Any]) -> dict[str, Any]:
    from app.api.data_integrity import beneish_detail  # noqa: PLC0415

    sig = {k: fnum(r.get(f"m_{k}")) for k in ("piotroski_f", "cfo_to_net_income", "net_margin", "altman_z2")}
    return beneish_detail(db, r.get("issuer_id"), r.get("sector_id"), fnum(r.get("m_beneish_m")), sig)


def _zones(db: Db, security_id: str) -> list[dict[str, Any]]:
    return db.all("SELECT * FROM zones WHERE security_id = ? ORDER BY rank, abs(distance_atr)", [security_id])


def _badges(r: dict[str, Any], beneish_level: str | None = None) -> list[str]:
    b = []
    if r.get("ins_cluster_buy"):
        b.append("cluster_buy")
    if r.get("ins_c_suite_buy"):
        b.append("c_suite_buy")
    z2 = fnum(r.get("m_altman_z2"))
    if z2 is not None and z2 < 1.1:
        b.append("distress_zone")
    bm = fnum(r.get("m_beneish_m"))
    # O2.2a / F15: beneish_flag only for the recalibrated (model-based) screen; textbook-only -> beneish_watch
    if beneish_level == "flag":
        b.append("beneish_flag")
    elif bm is not None and bm > -1.78 and beneish_level != "not_applicable":
        b.append("beneish_watch")
    if r.get("moat_class") == "wide":
        b.append("wide_moat")
    fpe = r.get("fund_period_end")
    if isinstance(fpe, date) and r.get("px_date") and (r["px_date"] - fpe).days > 400:
        b.append("stale_fundamentals")
    if r.get("new_high_52w"):
        b.append("new_52w_high")
    if r.get("new_low_52w"):
        b.append("new_52w_low")
    if (r.get("ml_dec_3M") or 0) >= 10:
        b.append("ml_top_decile")
    dq = fnum(r.get("dq_score"))
    if dq is not None and dq < 0.6:
        b.append("low_data_quality")
    pf = fnum(r.get("m_piotroski_f"))
    if pf is not None and pf >= 8:
        b.append("piotroski_high")
    return b


def _ml_view(rows: list[dict[str, Any]], runs: dict[str, dict[str, Any]], prod_id: str | None) -> list[dict[str, Any]]:
    out = []
    for p in rows:
        run = runs.get(p["model_run_id"], {})
        t = fnum(run.get("rank_ic_tstat"))
        rp = fnum(p.get("rank_pct")) or 0.0
        conf = None if t is None else round(max(0.0, min(1.0, t / 3.0)) * abs(2 * rp - 1), 4)
        out.append({
            "model_type": run.get("model_type") or "ensemble", "model_run_id": p["model_run_id"], "horizon": p["horizon"],
            "is_production": p["model_run_id"] == prod_id, "date": iso(p["date"]), "score": fnum(p.get("score")),
            "rank_pct": rp, "decile": int(p.get("decile") or 0), "expected_excess_return": fnum(p.get("exp_excess")),
            "interval": None if p.get("p10") is None else {"low": fnum(p["p10"]), "high": fnum(p["p90"]), "level": 0.8},
            "confidence": conf, "trained_at": iso(run.get("finished_at") or run.get("train_end")),
            "feature_set_version": run.get("feature_set_version"), "scope": p.get("scope") or "mvp",
        })
    order = {"ensemble": 0, "xgboost_rank": 1, "ft_transformer": 2, "cs_attention": 3}
    return sorted(out, key=lambda m: (not m["is_production"], order.get(m["model_type"], 9)))


# ------------------------------------------------------------------------------------------------ overview


@router.get("/companies/{security_id}/overview", operation_id="getCompanyOverview")
def get_company_overview(security_id: str, profile_id: str = "balanced", db: Db = Depends(get_db)) -> dict[str, Any]:
    pid = check_profile(profile_id)
    r = load_sec(db, security_id)
    iss = db.one("SELECT * FROM issuers WHERE issuer_id = ?", [r["issuer_id"]]) or {}
    idents = {}
    for x in db.all("SELECT id_type, id_value, security_id FROM identifiers WHERE issuer_id = ? "
                    "AND (security_id IS NULL OR security_id = ?) ORDER BY confidence", [r["issuer_id"], security_id]):
        idents[x["id_type"]] = x["id_value"]
    for k in ("cik", "lei", "isin", "cusip", "figi"):
        if r.get(k) and k not in idents:
            idents[k] = str(r[k])
    issuer = {
        "issuer_id": r["issuer_id"], "name": iss.get("name") or r["name"] or r["ticker"], "name_local": iss.get("name_local"),
        "description": iss.get("description"), "website": iss.get("website"), "employees": iss.get("employees"),
        "country": iss.get("country") or r.get("country") or "", "fiscal_year_end_mmdd": iss.get("fiscal_year_end_mmdd"),
        "reporting_gaap": iss.get("reporting_gaap") or r.get("fund_gaap"), "reporting_currency": iss.get("reporting_currency"),
        "identifiers": idents,
    }
    quote = None
    if r.get("close") is not None:
        quote = {"date": iso(r["px_date"]), "close": fnum(r["close"]), "currency": r.get("px_currency") or r.get("currency") or "",
                 "chg_1d": fnum(r.get("chg_1d")), "chg_1m": fnum(r.get("chg_1m")), "chg_ytd": fnum(r.get("chg_ytd")),
                 "high_52w": fnum(r.get("high_52w")), "low_52w": fnum(r.get("low_52w")), "volume": fnum(r.get("volume")),
                 "avg_dollar_volume_20d_usd": fnum(r.get("adv_usd_20d")), "market_cap_usd": fnum(r.get("market_cap_usd")),
                 "enterprise_value_usd": fnum(r.get("enterprise_value_usd"))}
    valuation = {k: fnum(r.get(k)) for k in ("pe_ttm", "pe_ntm", "earnings_yield", "pb", "ps_ttm", "ev_sales", "ev_ebitda",
                                             "ev_ebit", "fcf_yield", "dividend_yield", "shareholder_yield", "p_ffo", "p_tbv")}
    moat = None
    if r.get("moat_class"):
        moat = {"moat_class": r["moat_class"], "confidence": fnum(r.get("moat_confidence")) or 0.0,
                "moat_score": fnum(r.get("moat_score")), "as_of": iso(r.get("moat_as_of"))}
    ml = None
    if r.get("ml_rp_3M") is not None:
        ml = {"horizon": "3M", "rank_pct": fnum(r["ml_rp_3M"]), "decile": int(r.get("ml_dec_3M") or 0),
              "model_run_id": r.get("ml_run_3M") or "", "date": iso(r.get("ml_date_3M")),
              "scope": "extrapolated" if r.get("ml_extrap_3M") else "mvp",
              "scope_note": r.get("ml_detail_3M") if r.get("ml_extrap_3M") else None}
    fair = None
    if r.get("fv_weighted") is not None or r.get("fv_implied_cagr") is not None:
        base = db.one("SELECT * FROM fv_latest WHERE issuer_id = ? AND scenario = 'base'", [r["issuer_id"]]) or {}
        ex = _valuation_extra(base)
        fair = {"weighted_fair_value": fnum(r.get("fv_weighted")), "upside_pct": fnum(r.get("fv_upside")),
                "implied_revenue_cagr_5y": fnum(r.get("fv_implied_cagr")),
                "currency": r.get("px_currency") or r.get("currency") or r.get("fv_currency") or "",
                **{k: ex[k] for k in ("model", "fair_value_dcf", "fair_value_multiples", "fair_value_low", "fair_value_high",
                                      "valuation_confidence", "target_price_12m")}}
    ks = kpi_sets().get(r.get("kpi_set_id") or "", {})
    key_ids = list(ks.get("peer_metrics") or ["revenue_growth_yoy", "gross_margin", "operating_margin", "roic",
                                                "fcf_margin", "net_debt_to_ebitda", "pe_ttm", "ev_ebitda", "fcf_yield"])
    zones = _zones(db, security_id)
    top_zone = zone_out(zones[0], zone_targets(zones[0], zones, fnum(r.get("close")))) if zones else None
    status = db.snap.status("prices_daily", "fundamentals_canonical", "factor_scores")
    if not db.snap.input_ok("prices_daily") and not db.snap.input_ok("fundamentals_canonical"):
        status = "pending"
    return {
        "security": sec_ref(r), "issuer": issuer, "as_of": iso(r.get("px_date") or db.as_of), "quote": quote,
        "valuation": valuation, "composite": _composite(r, pid), "factors": _factors(db, r), "moat": moat, "ml": ml,
        "fair_value": fair, "key_metrics": _metric_scores(db, r, key_ids), "top_zone": top_zone,
        "badges": _badges(r, _beneish(db, r)["level"]),
        "beta_1y": rnd(r.get("beta_1y"), 4), "shares_outstanding": fnum(r.get("shares_out")),
        "last_filing_date": iso(r.get("last_filing_date")), "fx_to_usd": fnum(r.get("fx_to_usd")),
        "cost_of_capital": coc_out(db, r["issuer_id"]), "data_status": status,
    }


# ------------------------------------------------------------------------------------------------ prices


@router.get("/companies/{security_id}/prices", operation_id="getCompanyPrices")
def get_company_prices(security_id: str, from_: date | None = Query(None, alias="from"), to: date | None = None,
                       adjusted: bool = True, interval: Literal["D", "W", "M"] = "D",
                       db: Db = Depends(get_db)) -> dict[str, Any]:
    r = db.one("SELECT security_id, currency, px_currency FROM sec WHERE security_id = ?", [security_id])
    if r is None:
        if not db.scalar("SELECT 1 FROM prices WHERE security_id = ? LIMIT 1", [security_id]):
            raise not_found(f"unknown security_id {security_id!r}")
        r = {"security_id": security_id, "currency": None, "px_currency": None}
    end = to or db.as_of
    start = from_ or (end - timedelta(days=365 * 5))
    if interval == "D" and (end - start).days > pricebars.MAX_DAILY_DAYS:
        interval = "W"  # payload cap: > 10y of daily bars is auto-resampled weekly (ARCHITECTURE §12)
    bars = pricebars.bars(db, security_id, start, end, interval, adjusted)
    out_bars = [{"t": iso(b["date"]), "o": rnd(b["o"]), "h": rnd(b["h"]), "l": rnd(b["l"]), "c": rnd(b["c"]) or 0.0,
                 "v": fnum(b["v"])} for b in bars if b["c"] is not None]
    events: list[dict[str, Any]] = []
    for e in db.all("SELECT ex_date, action_type, ratio, amount, currency FROM corp_actions WHERE security_id = ? "
                    "AND ex_date BETWEEN ? AND ? ORDER BY ex_date", [security_id, start, end]):
        if e["action_type"] == "split":
            events.append({"date": iso(e["ex_date"]), "type": "split", "label": f"split {fnum(e['ratio']) or ''}".strip()})
        elif e["action_type"] in ("cash_dividend", "special_dividend"):
            amt = fnum(e["amount"])
            events.append({"date": iso(e["ex_date"]), "type": "dividend",
                           "label": f"dividend {amt:g} {e.get('currency') or ''}".strip() if amt is not None else "dividend"})
    iss = db.scalar("SELECT issuer_id FROM sec WHERE security_id = ?", [security_id])
    if iss:
        for e in db.all("SELECT knowledge_date, form_type, form_class, items FROM filings WHERE issuer_id = ? "
                        "AND knowledge_date BETWEEN ? AND ? AND form_class IN ('annual','semiannual','quarterly','current_report')",
                        [iss, start, end]):
            items = e.get("items") or []
            if e["form_class"] == "current_report" and not any(str(i).startswith("2.02") for i in items):
                continue
            typ = "earnings" if e["form_class"] == "current_report" else "filing"
            events.append({"date": iso(e["knowledge_date"]), "type": typ, "label": e["form_type"]})
    for e in db.all("""SELECT transaction_date, txn_type, sum(value_usd) AS v, count(*) AS n FROM insider_txn
                       WHERE sec_id = ? AND is_open_market AND txn_type IN ('buy', 'sell') AND transaction_date BETWEEN ? AND ?
                       GROUP BY 1, 2 ORDER BY 1""", [security_id, start, end]):
        v = fnum(e["v"])
        events.append({"date": iso(e["transaction_date"]), "type": "insider_buy" if e["txn_type"] == "buy" else "insider_sell",
                       "label": f"{e['n']} insider {e['txn_type']}(s)" + (f" ${v:,.0f}" if v else "")})
    events.sort(key=lambda e: e["date"])
    cur = (bars[-1]["currency"] if bars else None) or r.get("px_currency") or r.get("currency") or ""
    fd, dfrom = pricebars.first_date(db, security_id), pricebars.daily_from(db, security_id)
    return {"security_id": security_id, "currency": cur, "adjusted": adjusted, "interval": interval, "bars": out_bars,
            "first_date": iso(fd), "daily_from": iso(dfrom), "events": events, "data_status": "ok" if out_bars else ("pending" if not db.snap.input_ok("prices_daily") else "ok")}


# ------------------------------------------------------------------------------------------------ financials


def _line_items() -> list[dict[str, Any]]:
    return contract_yaml("canonical_line_items.yaml")["items"]


def _periods_and_values(db: Db, issuer_id: str, period: str, as_of: date | None, limit: int,
                        value_col: str) -> tuple[list[date], dict[date, dict[str, Any]], dict[tuple[str, date], tuple[Any, str]]]:
    kd = "AND knowledge_date <= ?" if as_of else ""
    params: list[Any] = [issuer_id] + ([as_of] if as_of else [])
    rows = db.all(f"""SELECT item_id, fiscal_year, fiscal_period, period_kind, period_end, {value_col} AS v, currency,
                             derivation, knowledge_date, knowledge_date_estimated, is_restatement, gaap
                      FROM fund WHERE issuer_id = ? {kd} AND period_kind IN (?, 'INSTANT')
                      QUALIFY row_number() OVER (PARTITION BY item_id, period_kind, period_end ORDER BY knowledge_date DESC) = 1""",
                  params + [period])
    dur = [x for x in rows if x["period_kind"] == period]
    ends = sorted({x["period_end"] for x in dur})
    if not ends and period == "FY":
        ends = sorted({x["period_end"] for x in rows if x["period_kind"] == "INSTANT" and x["fiscal_period"] == "FY"})
    ends = ends[-limit:]
    hdr: dict[date, dict[str, Any]] = {}
    vals: dict[tuple[str, date], tuple[Any, str]] = {}
    endset = set(ends)
    for x in rows:
        if x["period_end"] not in endset:
            continue
        if x["period_kind"] == period:
            h = hdr.setdefault(x["period_end"], {"period_end": iso(x["period_end"]), "fiscal_year": x["fiscal_year"],
                                                 "fiscal_period": x["fiscal_period"], "period_kind": period,
                                                 "knowledge_date": x["knowledge_date"], "knowledge_date_estimated": False,
                                                 "is_restated": False, "_cur": x["currency"], "_gaap": x["gaap"]})
            if x["knowledge_date"] and (h["knowledge_date"] is None or x["knowledge_date"] > h["knowledge_date"]):
                h["knowledge_date"] = x["knowledge_date"]
            h["knowledge_date_estimated"] = h["knowledge_date_estimated"] or bool(x["knowledge_date_estimated"])
            h["is_restated"] = h["is_restated"] or bool(x["is_restatement"])
        key = (x["item_id"], x["period_end"])
        if key not in vals or x["period_kind"] == period:
            vals[key] = (x["v"], x["derivation"])
    for e in ends:
        if e not in hdr:  # instant-only period (balance sheet without matching duration rows)
            fy = next((x for x in rows if x["period_end"] == e), {})
            hdr[e] = {"period_end": iso(e), "fiscal_year": fy.get("fiscal_year") or e.year,
                      "fiscal_period": fy.get("fiscal_period") or period, "period_kind": period,
                      "knowledge_date": fy.get("knowledge_date"), "knowledge_date_estimated": False, "is_restated": False,
                      "_cur": fy.get("currency"), "_gaap": fy.get("gaap")}
    return ends, hdr, vals


@router.get("/companies/{security_id}/financials", operation_id="getCompanyFinancials")
def get_company_financials(security_id: str, statement: Literal["IS", "BS", "CF", "ALL"] = "ALL",
                           period: Literal["FY", "Q", "H", "TTM"] = "FY", currency: Literal["LOCAL", "USD"] = "LOCAL",
                           limit: int = Query(10, ge=1, le=40), as_of: date | None = None, common_size: bool = False,
                           db: Db = Depends(get_db)) -> dict[str, Any]:
    r = db.one("SELECT issuer_id, reporting_currency, reporting_gaap FROM sec WHERE security_id = ?", [security_id])
    if r is None:
        raise not_found(f"unknown security_id {security_id!r}")
    vcol = "value_usd" if currency == "USD" else "value"
    ends, hdr, vals = _periods_and_values(db, r["issuer_id"], period, as_of, limit, vcol)
    stmts = {"IS", "BS", "CF", "NOTE"} if statement == "ALL" else {statement}
    unit_of = {"per_share": "per_share", "shares": "shares", "pure": "pure", "count": "count"}
    lines = []
    for it in _line_items():
        if it["statement"] not in stmts:
            continue
        vs = [vals.get((it["id"], e)) for e in ends]
        if not any(v is not None and v[0] is not None for v in vs):
            continue
        values = [fnum(v[0]) if v else None for v in vs]
        unit = unit_of.get(it.get("unit") or "", "currency")
        if common_size and unit == "currency":
            base_item = "total_assets" if it["statement"] == "BS" else "revenue"
            bases = [fnum(vals.get((base_item, e), (None, ""))[0]) for e in ends]
            values = [None if v is None or not b else round(v / b, 6) for v, b in zip(values, bases, strict=True)]
            unit = "pure"
        lines.append({"item_id": it["id"], "label": it.get("label") or label_for(it["id"]), "statement": it["statement"],
                      "unit": unit, "is_derived": any(v is not None and v[1] not in (None, "reported") for v in vs),
                      "indent": 0, "values": values})
    periods = []
    cur = None
    gaap = None
    for e in ends:
        h = dict(hdr[e])
        cur = cur or h.pop("_cur", None)
        gaap = gaap or h.pop("_gaap", None)
        h.pop("_cur", None)
        h.pop("_gaap", None)
        h["knowledge_date"] = iso(h["knowledge_date"])
        periods.append(h)
    return {"security_id": security_id, "currency": "USD" if currency == "USD" else (cur or r.get("reporting_currency") or ""),
            "currency_basis": currency, "gaap": gaap or r.get("reporting_gaap"), "as_of": iso(as_of), "periods": periods,
            "lines": lines, "common_size": common_size,
            "data_status": "ok" if periods else ("pending" if not db.snap.input_ok("fundamentals_canonical") else "ok")}


@router.get("/companies/{security_id}/ratios", operation_id="getCompanyRatios")
def get_company_ratios(security_id: str, period: Literal["FY", "TTM"] = "FY", limit: int = Query(10, ge=1, le=40),
                       db: Db = Depends(get_db)) -> dict[str, Any]:
    iss = db.scalar("SELECT issuer_id FROM sec WHERE security_id = ?", [security_id])
    if iss is None:
        raise not_found(f"unknown security_id {security_id!r}")
    rows = db.all("SELECT * FROM metrics WHERE issuer_id = ? AND period_kind = ? ORDER BY period_end", [iss, period])
    ends = sorted({x["period_end"] for x in rows})[-limit:]
    idx = {e: i for i, e in enumerate(ends)}
    hdr = {}
    by_metric: dict[str, dict[str, Any]] = {}
    for x in rows:
        if x["period_end"] not in idx:
            continue
        h = hdr.setdefault(x["period_end"], {"period_end": iso(x["period_end"]), "fiscal_year": x["fiscal_year"],
                                             "fiscal_period": x["fiscal_period"], "period_kind": period,
                                             "knowledge_date": x["knowledge_date"], "knowledge_date_estimated": False,
                                             "is_restated": False})
        if x["knowledge_date"] and x["knowledge_date"] > h["knowledge_date"]:
            h["knowledge_date"] = x["knowledge_date"]
        m = by_metric.setdefault(x["metric_id"], {"vals": [None] * len(ends), "kpi_set_id": x["kpi_set_id"], "unit": x["unit"]})
        m["vals"][idx[x["period_end"]]] = fnum(x["value"])
    mi = metric_index()
    order = {m: i for i, m in enumerate(mi)}
    metrics = []
    for mid in sorted(by_metric, key=lambda k: order.get(k, 10_000)):
        meta = mi.get(mid, {})
        metrics.append({"metric_id": mid, "label": meta.get("label") or label_for(mid), "unit": by_metric[mid]["unit"] or "",
                        "higher_is_better": meta.get("higher_is_better"), "kpi_set_id": by_metric[mid]["kpi_set_id"],
                        "values": by_metric[mid]["vals"]})
    periods = []
    for e in ends:
        h = hdr[e]
        h["knowledge_date"] = iso(h["knowledge_date"])
        periods.append(h)
    return {"periods": periods, "metrics": metrics, "data_status": db.snap.status("ratios", "sector_kpis") if metrics
            else ("pending" if not db.snap.input_ok("ratios") else "ok")}


# ------------------------------------------------------------------------------------------------ scores


@router.get("/companies/{security_id}/scores", operation_id="getCompanyScores")
def get_company_scores(security_id: str, profile_id: str = "balanced", history_days: int = Query(365, ge=1, le=3650),
                       db: Db = Depends(get_db)) -> dict[str, Any]:
    pid = check_profile(profile_id)
    r = load_sec(db, security_id)
    start = db.as_of - timedelta(days=history_days)
    fh = db.all("SELECT as_of_date, factor_id, score FROM factor_hist WHERE security_id = ? AND as_of_date >= ? "
                "ORDER BY as_of_date", [security_id, start])
    ch = {x["as_of_date"]: fnum(x["score"]) for x in db.all(
        "SELECT as_of_date, score FROM composite_hist WHERE security_id = ? AND profile_id = ? AND as_of_date >= ?",
        [security_id, pid, start])}
    by_date: dict[date, dict[str, float | None]] = defaultdict(dict)
    for x in fh:
        by_date[x["as_of_date"]][x["factor_id"]] = fnum(x["score"])
    w = profile_weights(pid)
    history = []
    for d in sorted(set(by_date) | set(ch)):
        fs = by_date.get(d, {})
        comp = ch.get(d)
        if comp is None and fs:
            num = sum(w[f] * fs[f] for f in w if fs.get(f) is not None)
            den = sum(w[f] for f in w if fs.get(f) is not None)
            comp = round(num / den, 4) if den >= 0.5 * sum(w.values()) and den > 0 else None
        history.append({"date": iso(d), "composite": comp, "factors": {f: fs.get(f) for f in FACTORS}})
    z2 = fnum(r.get("m_altman_z2"))
    bm = fnum(r.get("m_beneish_m"))
    bd = _beneish(db, r)
    flags = {"piotroski_f": fnum(r.get("m_piotroski_f")), "altman_z2": z2,
             "beneish_flag_adjusted": bd["flag"], "beneish_level": bd["level"], "beneish": bd,
             "altman_zone": None if z2 is None else ("distress" if z2 < 1.1 else "safe" if z2 > 2.6 else "grey"),
             "beneish_m": bm, "beneish_flag": None if bm is None else bm > -1.78,
             "sloan_accruals": fnum(r.get("m_sloan_accruals"))}
    return {"as_of": iso(r.get("score_date") or db.as_of), "composite": _composite(r, pid), "factors": _factors(db, r),
            "metrics": _metric_scores(db, r, None), "history": history, "accounting_flags": flags,
            "data_status": db.snap.status("factor_scores", "peer_scores", "composite_scores")}


# ------------------------------------------------------------------------------------------------ peers


def _peer_keys(r: dict[str, Any], level: str) -> tuple[str, list[tuple[str, Any]]]:
    pr = peer_region_map().get(r.get("region") or "OTHER", "GLOBAL")
    spec = {"subsector_region_size": [("subsector_id", r.get("subsector_id")), ("peer_region", pr), ("size_bucket", r.get("size_bucket"))],
            "subsector_region": [("subsector_id", r.get("subsector_id")), ("peer_region", pr)],
            "subsector_global": [("subsector_id", r.get("subsector_id"))],
            "sector_region": [("sector_id", r.get("sector_id")), ("peer_region", pr)],
            "sector_global": [("sector_id", r.get("sector_id"))]}[level]
    key = f"{level}:" + "|".join(str(v) for _, v in spec)
    return key, spec


def _peer_where(spec: list[tuple[str, Any]], params: list[Any]) -> str:
    pmap = peer_region_map()
    conds = []
    for col, v in spec:
        if col == "peer_region":
            regs = [k for k, x in pmap.items() if x == v]
            conds.append(f"region IN ({', '.join('?' for _ in regs) or 'NULL'})")
            params.extend(regs)
        else:
            conds.append(f"{col} = ?" if v is not None else f"{col} IS NULL")
            if v is not None:
                params.append(v)
    return " AND ".join(conds) or "TRUE"


@router.get("/companies/{security_id}/peers", operation_id="getCompanyPeers")
def get_company_peers(security_id: str,
                      level: Literal["subsector_region_size", "subsector_region", "subsector_global", "sector_region",
                                     "sector_global"] | None = None,
                      limit: int = Query(15, ge=1, le=50), peer_set_id: str | None = None,
                      db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    ps_ids: list[str] | None = None
    if peer_set_id:
        from app.modeling.store import peer_set_members_for

        got = peer_set_members_for(peer_set_id, security_id)
        if got is None:
            raise not_found(f"peer set {peer_set_id} not found")
        ps_ids, ps_cluster = got
        level = "peer_set"
    if level is None:
        lv = db.scalar("SELECT mode(peer_group_level) FROM peer_latest WHERE security_id = ?", [security_id])
        if lv in PEER_LEVELS:
            level = lv
        else:
            level = "sector_global"
            for cand in PEER_LEVELS:
                _, spec = _peer_keys(r, cand)
                if any(v is None for _, v in spec):
                    continue
                params: list[Any] = []
                n = db.scalar(f"SELECT count(*) FROM sec WHERE {_peer_where(spec, params)}", params)
                if (n or 0) >= 8:
                    level = cand
                    break
    if ps_ids is not None:
        key, spec = f"peer_set:{peer_set_id}:{ps_cluster if ps_cluster is not None else 'all'}", []
    else:
        key, spec = _peer_keys(r, level)
    ks = kpi_sets().get(r.get("kpi_set_id") or "", {})
    mi = metric_index()
    mids = [m for m in (ks.get("peer_metrics") or ["revenue_growth_yoy", "gross_margin", "operating_margin", "roic",
                                                   "fcf_margin", "pe_ttm", "ev_ebitda", "fcf_yield"]) if m in mi]
    cols = {m: mi[m]["column"] for m in mids}
    vsel = ", ".join(f'"{c}" AS "x_{m}"' for m, c in cols.items())
    params = []
    if ps_ids is not None:
        where = f"s.security_id IN ({', '.join('?' for _ in ps_ids) or 'NULL'})"
        params.extend(ps_ids)
    else:
        where = _peer_where(spec, params)
    sel = f"{sec_ref_sql('s')}, c_balanced, moat_class{', ' + vsel if vsel else ''}"
    rows = db.all(f"""SELECT {sel} FROM sec s WHERE ({where}) OR s.security_id = ?
                      ORDER BY s.security_id = ? DESC, market_cap_usd DESC NULLS LAST LIMIT ?""",
                  params + [security_id, security_id, limit + 1])
    params2: list[Any] = []
    w2 = (f"security_id IN ({', '.join('?' for _ in ps_ids) or 'NULL'})" if ps_ids is not None
          else _peer_where(spec, params2))
    if ps_ids is not None:
        params2.extend(ps_ids)
    med = db.one("SELECT count(*) AS n"
                 + "".join(f', median("{c}") AS "md_{m}"' for m, c in cols.items())
                 + f" FROM sec WHERE {w2}", params2) or {}
    out_rows = []
    for x in rows[: limit + 1]:
        out_rows.append({"security": sec_ref(x), "is_self": x["ref_security_id"] == security_id,
                         "composite": fnum(x.get("c_balanced")), "moat_class": x.get("moat_class"),
                         "values": {m: fnum(x.get(f"x_{m}")) for m in mids}})
    return {"peer_group_key": key, "level": level, "metrics": [metric_public(mi[m]) for m in mids], "rows": out_rows,
            "medians": {m: rnd(med.get(f"md_{m}")) for m in mids}, "peer_count": int(med.get("n") or 0),
            "data_status": "ok" if out_rows else "pending"}


# ------------------------------------------------------------------------------------------------ forecast


def _sensitivity(base: dict[str, Any] | None, fcff: list[float]) -> dict[str, Any] | None:
    if not base or not fcff:
        return None
    w0, g0 = fnum(base.get("wacc")), fnum(base.get("terminal_growth"))
    ev0, eq0, fv0 = fnum(base.get("enterprise_value")), fnum(base.get("equity_value")), fnum(base.get("fair_value_per_share"))
    if None in (w0, g0, ev0, eq0, fv0) or not fv0 or fv0 <= 0 or eq0 is None or eq0 == 0:
        return None

    def ev(w: float, g: float) -> float | None:
        if w <= g + 0.005:
            return None
        n = len(fcff)
        pv = sum(cf / (1 + w) ** (t + 1) for t, cf in enumerate(fcff))
        tv = fcff[-1] * (1 + g) / (w - g) / (1 + w) ** n
        return pv + tv

    calc0 = ev(w0, g0)
    if not calc0:
        return None
    k = ev0 / calc0
    net_debt = ev0 - eq0
    shares_eff = eq0 / fv0
    waccs = [round(w0 + d, 4) for d in (-0.02, -0.01, 0.0, 0.01, 0.02) if w0 + d > 0.02]
    tgs = [round(g0 + d, 4) for d in (-0.01, -0.005, 0.0, 0.005, 0.01)]
    grid = []
    for w in waccs:
        row = []
        for g in tgs:
            e = ev(w, g)
            row.append(None if e is None else round((k * e - net_debt) / shares_eff, 4))
        grid.append(row)
    return {"wacc": waccs, "terminal_growth": tgs, "fair_value": grid, "base_wacc": w0, "base_terminal_growth": g0}


def _grid(base: dict[str, Any] | None) -> dict[str, Any] | None:
    g = jload((base or {}).get("sensitivity_grid"), None)
    if not isinstance(g, dict) or not g.get("wacc") or not g.get("fair_value"):
        return None
    return {"wacc": [fnum(x) for x in g["wacc"]], "terminal_growth": [fnum(x) for x in g.get("terminal_growth") or []],
            "fair_value": [[fnum(x) for x in row] for row in g["fair_value"]],
            "base_wacc": fnum(g.get("base_wacc", base.get("wacc"))),
            "base_terminal_growth": fnum(g.get("base_terminal_growth", base.get("terminal_growth"))),
            "metric": g.get("metric")}


_FORECAST_ORDER = ["revenue", "gross_profit", "ebitda", "ebit", "operating_income", "net_income", "net_income_parent",
                   "eps_diluted", "cfo", "capex", "fcf", "fcff", "total_debt", "cash_and_equivalents", "net_debt",
                   "total_equity", "shares_diluted_weighted"]


def coc_out(db: Db, issuer_id: str) -> dict[str, Any] | None:
    c = db.one("SELECT * FROM coc_latest WHERE issuer_id = ?", [issuer_id])
    if not c:
        return None
    return {"as_of": iso(c["as_of_date"]), "currency": c.get("currency"), "wacc": fnum(c.get("wacc")),
            "cost_of_equity": fnum(c.get("cost_of_equity")), "beta": fnum(c.get("beta")), "beta_raw": fnum(c.get("beta_raw")),
            "beta_source": c.get("beta_source"), "rf": fnum(c.get("rf")), "rf_source": c.get("rf_source"),
            "erp": fnum(c.get("erp")), "crp": fnum(c.get("crp")), "kd_post": fnum(c.get("kd_post")),
            "tax_rate": fnum(c.get("tax_rate")), "weight_equity": fnum(c.get("weight_equity")),
            "terminal_growth": fnum(c.get("terminal_growth")), "is_financial": c.get("is_financial"),
            "flags": list(c.get("flags") or [])}


def _valuation_extra(v: dict[str, Any]) -> dict[str, Any]:
    return {"model": {"fcff_dcf": "dcf"}.get(v.get("model"), v.get("model")), "fair_value_dcf": fnum(v.get("fair_value_dcf")),
            "fair_value_multiples": fnum(v.get("fair_value_multiples")), "fair_value_low": fnum(v.get("fair_value_low")),
            "fair_value_high": fnum(v.get("fair_value_high")), "tv_share": fnum(v.get("tv_share")),
            "valuation_confidence": fnum(v.get("valuation_confidence")),
            "confidence_reasons": list(v.get("confidence_reasons") or []), "flags": list(v.get("flags") or []),
            "target_price_12m": fnum(v.get("target_price_12m")), "cost_of_equity": fnum(v.get("cost_of_equity"))}


@router.get("/companies/{security_id}/forecast", operation_id="getCompanyForecast")
def get_company_forecast(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    iss = r["issuer_id"]
    fc = db.all("SELECT * FROM forecasts_latest WHERE issuer_id = ? ORDER BY scenario, item_id, fiscal_year", [iss])
    fv = {x["scenario"]: x for x in db.all("SELECT * FROM fv_latest WHERE issuer_id = ?", [iss])}
    years = sorted({x["fiscal_year"] for x in fc})
    actual = sorted({x["fiscal_year"] for x in fc if x["is_actual"]})
    yi = {y: i for i, y in enumerate(years)}
    scen = []
    default_p = {"bear": 0.25, "base": 0.5, "bull": 0.25}
    by: dict[str, dict[str, list[float | None]]] = defaultdict(dict)
    for x in fc:
        line = by[x["scenario"]].setdefault(x["item_id"], [None] * len(years))
        line[yi[x["fiscal_year"]]] = fnum(x["value"])
    order = {k: i for i, k in enumerate(_FORECAST_ORDER)}
    for s in ("bear", "base", "bull"):
        if s not in by and s not in fv:
            continue
        v = fv.get(s, {})
        lines = [{"item_id": k, "label": label_for(k.removeprefix("drv_")) + (" (driver)" if k.startswith("drv_") else ""),
                  "is_driver": k.startswith("drv_"), "values": vals}
                 for k, vals in sorted(by.get(s, {}).items(), key=lambda kv: (kv[0].startswith("drv_"), order.get(kv[0], 99), kv[0]))]
        scen.append({"scenario": s, "probability": fnum(v.get("probability")) or default_p[s], "lines": lines,
                     "valuation": {"wacc": fnum(v.get("wacc")) or 0.0, "terminal_growth": fnum(v.get("terminal_growth")) or 0.0,
                                   "enterprise_value": fnum(v.get("enterprise_value")), "equity_value": fnum(v.get("equity_value")),
                                   "fair_value_per_share": fnum(v.get("fair_value_per_share")), "upside_pct": fnum(v.get("upside_pct")),
                                   "cost_of_equity": fnum(v.get("cost_of_equity")),
                                   "fair_value_dcf": fnum(v.get("fair_value_dcf")),
                                   "fair_value_multiples": fnum(v.get("fair_value_multiples"))}})
    mi = fv.get("market_implied", {})
    base_lines = by.get("base", {})
    fc_years = [y for y in years if y not in actual]
    fcff_line = base_lines.get("fcff") or base_lines.get("fcf")
    fcff = [x for y, x in zip(years, fcff_line or [], strict=False) if y in fc_years and x is not None] if fcff_line else []
    price = fnum(next((x.get("price") for x in fv.values() if x.get("price") is not None), None)) or fnum(r.get("close"))
    status = "ok" if scen else ("pending" if not db.snap.input_ok("forecasts") else "ok")
    return {
        "as_of": iso(next((x["as_of_date"] for x in fv.values()), None) or (fc[0]["as_of_date"] if fc else db.as_of)),
        "currency": next((x["currency"] for x in fv.values() if x.get("currency")), None) or r.get("reporting_currency")
        or r.get("currency") or "",
        "years": years, "actual_years": actual, "scenarios": scen, "price": price,
        "weighted_fair_value": fnum(r.get("fv_weighted")),
        "reverse_dcf": {"implied_revenue_cagr_5y": fnum(mi.get("implied_revenue_cagr_5y")),
                        "implied_fcf_margin": fnum(mi.get("implied_fcf_margin")),
                        "implied_vs_hist_growth_pct": fnum(mi.get("implied_vs_hist_growth_pct")),
                        "hist_revenue_cagr_5y": fnum(r.get("m_revenue_cagr_5y"))},
        "sensitivity_grid": _grid(fv.get("base")) or _sensitivity(fv.get("base"), fcff),
        "consensus_growth": None,
        **_valuation_extra(fv.get("base") or {}),
        "cost_of_capital": coc_out(db, iss),
        "data_status": status,
    }


# ------------------------------------------------------------------------------------------------ moat


_MOAT_RAW = {"roic_wacc_spread": "roic_wacc_spread_avg", "roic_wacc_persistence": "roic_wacc_persistence",
             "gm_level": "gm_level_pct", "gm_stability": "gm_stability_pct", "margin_premium": "margin_premium_pct",
             "market_share": "subsector_revenue_share", "intangibles_intensity": "intangibles_intensity_pct"}
_MOAT_THRESHOLD = {"roic_wacc_persistence": 60.0}
_SOURCE_COMPONENT = {"scale": "market_share", "intangibles": "intangibles_intensity", "switching_costs": "gm_stability",
                     "network": "margin_premium", "cost_advantage": "gm_level", "efficient_scale": "market_share"}


@router.get("/companies/{security_id}/moat", operation_id="getCompanyMoat")
def get_company_moat(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    iss = r["issuer_id"]
    hist = db.all("SELECT * FROM moat WHERE issuer_id = ? ORDER BY as_of_date", [iss])
    comps_cfg = contract_yaml("scoring.yaml")["moat"]["components"]
    last = hist[-1] if hist else None
    cs_hist = [(h["as_of_date"], jload(h.get("component_scores"), {}) or {}) for h in hist]
    components = []
    cur_scores = jload(last.get("component_scores"), {}) if last else {}
    detail = (jload(last.get("component_detail"), {}) or {}) if last else {}
    if not isinstance(detail, dict):
        detail = {}
    ids = list(comps_cfg) + [k for k in detail if not k.startswith("_") and k not in comps_cfg]
    for cid in ids:
        d = detail.get(cid) if isinstance(detail.get(cid), dict) else {}
        score = fnum(d.get("score")) if d.get("score") is not None else fnum((cur_scores or {}).get(cid))
        th = fnum(d.get("threshold")) if d.get("threshold") is not None else _MOAT_THRESHOLD.get(cid, 50.0)
        passed = d.get("passed") if d.get("passed") is not None else (None if score is None or th is None else score >= th)
        if d.get("history"):
            hist_c = [{"date": f"{int(h['fiscal_year'])}-12-31", "score": fnum(h.get("score", h.get("value"))),
                       "fiscal_year": int(h["fiscal_year"]), "value": fnum(h.get("value"))}
                      for h in d["history"] if isinstance(h, dict) and h.get("fiscal_year") is not None]
        else:
            hist_c = [{"date": iso(dt), "score": fnum(cs.get(cid))} for dt, cs in cs_hist if cid in cs]
        weight = fnum(d.get("weight")) if d.get("weight") is not None else float((comps_cfg.get(cid) or {}).get("weight", 0.0))
        components.append({"id": cid, "label": label_for(cid).replace("Roic wacc", "ROIC-WACC").replace("ROIC wacc", "ROIC-WACC"),
                           "weight": weight or 0.0, "score": score,
                           "raw": fnum(d["value"]) if d.get("value") is not None else
                           (fnum(last.get(_MOAT_RAW.get(cid, ""))) if last else None),
                           "threshold": th, "passed": passed, "history": hist_c,
                           "supplementary": cid not in comps_cfg})
    wacc = fnum(last.get("wacc")) if last else None
    rvw = detail.get("_roic_vs_wacc") if isinstance(detail.get("_roic_vs_wacc"), list) else None
    if rvw:
        roic_vs_wacc = [{"fiscal_year": int(x["fiscal_year"]), "roic": fnum(x.get("roic")),
                         "wacc": fnum(x.get("hurdle", x.get("wacc"))), "hurdle": fnum(x.get("hurdle", x.get("wacc")))}
                        for x in rvw if isinstance(x, dict) and x.get("fiscal_year") is not None]
    else:
        roic = db.all("SELECT fiscal_year, value FROM metrics WHERE issuer_id = ? AND metric_id = 'roic' AND period_kind = 'FY' "
                      "ORDER BY period_end DESC LIMIT 10", [iss])
        roic_vs_wacc = [{"fiscal_year": int(x["fiscal_year"]), "roic": fnum(x["value"]), "wacc": wacc, "hurdle": wacc}
                        for x in sorted(roic, key=lambda x: x["fiscal_year"])]
    share = db.all("SELECT fiscal_year, share FROM rev_share WHERE issuer_id = ? ORDER BY fiscal_year", [iss])
    sources = list(last.get("sources_of_moat") or []) if last else []
    comp_score = {c["id"]: c["score"] for c in components}
    typed = jload(last.get("moat_sources"), None) if last else None
    if not isinstance(typed, list) or (not typed and sources):
        typed = [{"type": s, "strength": comp_score.get(_SOURCE_COMPONENT.get(s, ""))} for s in sources]
    typed = [{"type": str(x.get("type")), "strength": fnum(x.get("strength"))} for x in typed if isinstance(x, dict) and x.get("type")]
    if not sources:
        sources = [x["type"] for x in typed]
    if not last:
        return {"as_of": iso(db.as_of), "moat_class": "insufficient_data", "confidence": 0.0, "moat_score": None,
                "components": components, "sources_of_moat": [], "moat_sources": [], "roic_vs_wacc": roic_vs_wacc,
                "history": [], "market_share_series": [{"fiscal_year": int(x["fiscal_year"]), "share": fnum(x["share"])} for x in share],
                "years_evaluated": None, "data_status": "pending" if not db.snap.input_ok("moat") else "ok"}
    return {
        "as_of": iso(last["as_of_date"]), "moat_class": last["moat_class"], "confidence": fnum(last["confidence"]) or 0.0,
        "moat_score": fnum(last.get("moat_score")), "components": components, "sources_of_moat": sources,
        "moat_sources": typed, "roic_vs_wacc": roic_vs_wacc,
        "history": [{"date": iso(h["as_of_date"]), "moat_class": h["moat_class"], "moat_score": fnum(h.get("moat_score"))}
                    for h in hist],
        "market_share_series": [{"fiscal_year": int(x["fiscal_year"]), "share": fnum(x["share"])} for x in share],
        "years_evaluated": last.get("years_evaluated"), "model_variant": last.get("model_variant"), "data_status": "ok",
    }


# ------------------------------------------------------------------------------------------------ insiders


def insider_txn_out(t: dict[str, Any], anomaly: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "txn_id": t["txn_id"], "insider_name": t.get("insider_name") or "", "role_norm": t.get("role_norm"),
        "officer_title": t.get("officer_title"), "is_director": bool(t.get("is_director")),
        "is_officer": bool(t.get("is_officer")), "transaction_date": iso(t["transaction_date"]),
        "knowledge_date": iso(t["knowledge_date"]), "txn_code": t.get("txn_code"), "txn_type": t.get("txn_type") or "other",
        "is_open_market": bool(t.get("is_open_market")), "is_10b5_1": t.get("is_10b5_1"), "shares": fnum(t.get("shares")) or 0.0,
        "price": fnum(t.get("price")), "currency": t.get("currency"), "value_usd": fnum(t.get("value_usd")),
        "shares_owned_after": fnum(t.get("shares_owned_after")), "holding_change_pct": fnum(t.get("holding_change_pct")),
        "filing_url": _filing_url(t),
        "anomaly_score": fnum(t.get("txn_anomaly_score")) if t.get("txn_anomaly_score") is not None
        else (anomaly or {}).get(t["txn_id"]),
        "cluster_id": t.get("cluster_id"),
        "features": None if t.get("role_weight") is None and t.get("size_vs_hist") is None else {
            "role_weight": fnum(t.get("role_weight")), "n_insiders_same_dir_30d": t.get("n_insiders_same_dir_30d"),
            "cluster_n_insiders": t.get("cluster_n_insiders"), "first_buy_2y": t.get("first_buy_2y"), "routine": t.get("routine"),
            "size_vs_hist": fnum(t.get("size_vs_hist")), "pre_drawdown_252d": fnum(t.get("pre_drawdown_252d")),
            "pre_ret_126d": fnum(t.get("pre_ret_126d"))},
    }


def _filing_url(t: dict[str, Any]) -> str | None:
    fid = str(t.get("filing_id") or "")
    if t.get("source_id") == "sec_edgar" and fid:
        acc = fid.split(":")[-1]
        if len(acc.replace("-", "")) == 18:
            cik = str(t.get("source_entity_id") or "").lstrip("0") or acc.split("-")[0].lstrip("0")
            return f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{acc}-index.htm"
    return None


def txn_anomaly_scores(db: Db, security_ids: list[str]) -> dict[str, float]:
    """txn_id -> anomaly score from insider_signals.top_transactions (list of txn ids, or of {txn_id, anomaly_score})."""
    if not security_ids:
        return {}
    ph = ", ".join("?" for _ in security_ids)
    out: dict[str, float] = {}
    for s in db.all(f"SELECT anomaly_score, top_transactions FROM insider_signals WHERE security_id IN ({ph}) "
                    "AND top_transactions IS NOT NULL ORDER BY as_of_date", security_ids):
        tt = jload(s["top_transactions"], [])
        if isinstance(tt, dict):
            tt = tt.get("txn_ids") or tt.get("transactions") or []
        for x in tt or []:
            if isinstance(x, str) and s.get("anomaly_score") is not None:
                out[x] = fnum(s["anomaly_score"])
            elif isinstance(x, dict) and x.get("txn_id"):
                v = fnum(x.get("anomaly_score", s.get("anomaly_score")))
                if v is not None:
                    out[str(x["txn_id"])] = v
    return out


def insider_signal_out(s: dict[str, Any]) -> dict[str, Any]:
    return {"as_of_date": iso(s["as_of_date"]), "signal_score": fnum(s.get("signal_score")),
            "net_buy_value_usd_90d": fnum(s.get("net_buy_value_usd_90d")), "net_buy_mcap_bps_90d": fnum(s.get("net_buy_mcap_bps_90d")),
            "n_buyers_90d": s.get("n_buyers_90d"), "n_sellers_90d": s.get("n_sellers_90d"),
            "cluster_buy": bool(s.get("cluster_buy")), "c_suite_buy": bool(s.get("c_suite_buy")),
            "anomaly_score": fnum(s.get("anomaly_score")), "xgb_prob_up": fnum(s.get("xgb_prob_up"))}


@router.get("/companies/{security_id}/insiders", operation_id="getCompanyInsiders")
def get_company_insiders(security_id: str, days: int = Query(730, ge=1, le=3650), open_market_only: bool = False,
                         db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    start = db.as_of - timedelta(days=days)
    om = "AND is_open_market" if open_market_only else ""
    txns = db.all(f"SELECT * FROM insider_txn WHERE sec_id = ? AND transaction_date >= ? {om} "
                  "ORDER BY transaction_date DESC, txn_id LIMIT 500", [security_id, start])
    sig = db.one("SELECT * FROM insider_latest WHERE security_id = ?", [security_id])
    an = txn_anomaly_scores(db, [security_id])
    monthly = db.all("""SELECT strftime(transaction_date, '%Y-%m') AS month,
                               coalesce(sum(value_usd) FILTER (txn_type = 'buy'), 0) AS buy_value_usd,
                               coalesce(sum(value_usd) FILTER (txn_type = 'sell'), 0) AS sell_value_usd,
                               count(DISTINCT insider_id) FILTER (txn_type = 'buy')::INT AS n_buyers,
                               count(DISTINCT insider_id) FILTER (txn_type = 'sell')::INT AS n_sellers
                        FROM insider_txn WHERE sec_id = ? AND transaction_date >= ? AND is_open_market
                        GROUP BY 1 ORDER BY 1""", [security_id, start])
    region = r.get("region")
    coverage = "full" if region == "US" else ("partial" if region in ("KR", "EU", "UK") else "none")
    status = "ok" if txns or sig else ("pending" if not db.snap.input_ok("insider_transactions") else "ok")
    return {"signal": insider_signal_out(sig) if sig else None, "coverage": coverage,
            "transactions": [insider_txn_out(t, an) for t in txns],
            "monthly": [{**m, "buy_value_usd": fnum(m["buy_value_usd"]) or 0.0, "sell_value_usd": fnum(m["sell_value_usd"]) or 0.0}
                        for m in monthly],
            "data_status": status}


# ------------------------------------------------------------------------------------------------ sentiment & events


@router.get("/companies/{security_id}/sentiment", operation_id="getCompanySentiment")
def get_company_sentiment(security_id: str, days: int = Query(180, ge=1, le=730), db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    start = db.as_of - timedelta(days=days)
    daily = db.all("SELECT * FROM sentiment_daily WHERE security_id = ? AND date >= ? ORDER BY date", [security_id, start])
    items = db.all("SELECT * FROM news WHERE issuer_id = ? AND published_at >= ? ORDER BY published_at DESC LIMIT 100",
                   [r["issuer_id"], start])
    lazy = db.all("""SELECT * FROM (SELECT t.*, percent_rank() OVER (PARTITION BY section ORDER BY cosine_tfidf) AS pr
                                     FROM text_changes t) WHERE issuer_id = ? ORDER BY knowledge_date DESC LIMIT 40""",
                  [r["issuer_id"]])
    status = db.snap.status("sentiment_daily", "news_items")
    return {
        "sentiment_score": fnum(r.get("sentiment_score")),
        "daily": [{"date": iso(d["date"]), "news_count": int(d.get("news_count") or 0), "attention_z": fnum(d.get("news_count_z")),
                   "sentiment_mean": fnum(d.get("sentiment_mean")), "sentiment_ewm_7d": fnum(d.get("sentiment_ewm_7d")),
                   "sentiment_ewm_30d": fnum(d.get("sentiment_ewm_30d")), "gdelt_tone_ewm_7d": fnum(d.get("gdelt_tone_ewm_7d"))}
                  for d in daily],
        "items": [{"item_id": n["item_id"], "published_at": iso(n["published_at"]), "title": n["title"] or "", "url": n["url"] or "",
                   "publisher": n.get("publisher"), "source_id": n.get("source_id") or "", "language": n.get("language"),
                   "sentiment_label": n.get("sentiment_label"), "sentiment_score": fnum(n.get("sentiment_score")),
                   "gdelt_tone": fnum(n.get("gdelt_tone")), "relevance": fnum(n.get("relevance"))} for n in items],
        "lazy_prices": [{"filing_id": x["filing_id"], "form_type": x.get("form_type"), "knowledge_date": iso(x["knowledge_date"]),
                         "section": x["section"], "cosine_tfidf": fnum(x.get("cosine_tfidf")),
                         "words_added_pct": fnum(x.get("words_added_pct")), "neg_word_delta": fnum(x.get("neg_word_delta")),
                         "universe_percentile": rnd(x.get("pr"), 4)} for x in lazy],
        "data_status": status,
    }


@router.get("/companies/{security_id}/events", operation_id="getCompanyEvents")
def get_company_events(security_id: str, days: int = Query(90, ge=1, le=365), db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.api.routers.events import _URL_KEY_SQL, event_out, securities_for_issuers

    r = load_sec(db, security_id)
    iss = r["issuer_id"]
    start = db.as_of - timedelta(days=days)
    daily = db.all("SELECT date, mentions, avg_tone, attention_z, top_themes FROM gdelt_company_daily "
                   "WHERE issuer_id = ? AND date >= ? ORDER BY date", [iss, start])
    # one row per source article (GDELT codes one article as several events)
    evs = db.all(f"""SELECT e.* FROM gdelt_events e JOIN gdelt_issuer gi USING (global_event_id)
                     WHERE gi.issuer_id = ? AND e.event_date >= ?
                     QUALIFY row_number() OVER (PARTITION BY coalesce({_URL_KEY_SQL.replace('source_url', 'e.source_url')},
                                                                       CAST(e.global_event_id AS VARCHAR))
                                                ORDER BY e.num_articles DESC, e.event_date DESC, e.global_event_id) = 1
                     ORDER BY e.num_articles DESC, e.event_date DESC LIMIT 100""", [iss, start])
    secs = securities_for_issuers(db, {i for e in evs for i in (e.get("issuer_ids") or [])})
    expo = db.all("""SELECT e.action_country AS country, count(*)::INT AS events, avg(e.goldstein) AS avg_goldstein
                     FROM gdelt_events e JOIN gdelt_issuer gi USING (global_event_id)
                     WHERE gi.issuer_id = ? AND e.event_date >= ? AND e.action_country IS NOT NULL
                     GROUP BY 1 ORDER BY 2 DESC LIMIT 30""", [iss, start])
    theme_count: dict[str, int] = defaultdict(int)
    for d in daily:
        for t in d.get("top_themes") or []:
            theme_count[t] += 1
    return {
        "daily": [{"date": iso(d["date"]), "mentions": int(d["mentions"] or 0), "avg_tone": fnum(d.get("avg_tone")),
                   "attention_z": fnum(d.get("attention_z"))} for d in daily],
        "events": [event_out(e, secs) for e in evs],
        "country_exposure": [{"country": x["country"], "events": x["events"], "avg_goldstein": rnd(x["avg_goldstein"], 4)} for x in expo],
        "top_themes": [t for t, _ in sorted(theme_count.items(), key=lambda kv: -kv[1])[:10]],
        "data_status": db.snap.status("gdelt_company_daily", "gdelt_events"),
    }


# ------------------------------------------------------------------------------------------------ technicals


@router.get("/companies/{security_id}/technicals", operation_id="getCompanyTechnicals")
def get_company_technicals(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.api.routers import _technicals as T

    r = load_sec(db, security_id)
    zones = _zones(db, security_id)
    setup = db.one("SELECT * FROM setups WHERE security_id = ?", [security_id])
    levels = db.all("SELECT * FROM tech_levels WHERE security_id = ? AND coalesce(still_valid, true) "
                    "ORDER BY abs(distance_atr) LIMIT 80", [security_id])
    piv = db.all("""SELECT * FROM tech_levels WHERE security_id = ? AND (family IN ('market_structure', 'swing_pivot')
                    OR starts_with(level_type, 'ms_') OR starts_with(level_type, 'swing_'))
                    ORDER BY anchor_date DESC LIMIT 60""", [security_id])
    stats = db.all("SELECT * FROM level_stats WHERE security_id = ? ORDER BY rank NULLS LAST, n_events DESC LIMIT 80",
                   [security_id])
    ft = T.frontend_types()
    last_close = fnum(setup["last_close"]) if setup else (fnum(zones[0]["last_close"]) if zones else fnum(r.get("close")))
    atr = fnum(setup["atr14"]) if setup else (fnum(zones[0]["atr14"]) if zones else fnum(r.get("atr14")))
    as_of = ((setup or {}).get("as_of_date") or (zones[0]["as_of_date"] if zones else None)
             or (levels[0]["as_of_date"] if levels else None) or r.get("px_date") or db.as_of)
    zout = [zone_out(z, zone_targets(z, zones, last_close)) for z in zones]
    plan = None
    cands = [z for z in zout if (z["reward_risk"] or 0) >= 2]
    if cands:
        plan = sorted(cands, key=lambda z: (z["rank"], abs(z["distance_atr"])))[0]
    stat_out, reliability = T.stats_and_reliability(stats, ft)
    status = "ok" if zones or levels or setup else ("pending" if not db.snap.input_ok("confluence_zones") else "ok")
    if last_close is None:
        status = "pending"
    return {
        "as_of": iso(as_of), "last_close": last_close or 0.0, "atr14": atr or 0.0,
        "levels": [T.level_out(x, ft) for x in levels], "zones": zout, "level_stats": stat_out, "trade_plan": plan,
        "setup": T.setup_out(setup), "indicators": T.indicators_out(setup), "pivots": [T.pivot_out(x) for x in piv],
        "type_reliability": reliability, "data_status": status,
    }


# ------------------------------------------------------------------------------------------------ model


def shap_out(raw: Any) -> list[dict[str, Any]]:
    out = []
    for x in jload(raw, []) or []:
        if not isinstance(x, dict) or x.get("feature") is None:
            continue
        feat = str(x["feature"])
        fam = x.get("family") or (feat.removeprefix("f_").split("_")[0] if "_" in feat else None)
        out.append({"feature": feat, "label": x.get("label") or label_for(feat.removeprefix("f_")), "family": fam,
                    "value": fnum(x.get("value")), "contribution": fnum(x.get("contribution")) or 0.0})
    return sorted(out, key=lambda s: -abs(s["contribution"]))


@router.get("/companies/{security_id}/model", operation_id="getCompanyModel")
def get_company_model(security_id: str, horizon: Literal["1M", "3M", "6M"] = "3M", db: Db = Depends(get_db)) -> dict[str, Any]:
    load_sec(db, security_id)
    prod = db.one("SELECT * FROM ml_prod WHERE horizon = ?", [horizon])
    preds = db.all("SELECT * FROM ml_latest WHERE security_id = ? AND horizon = ?", [security_id, horizon])
    runs = {x["model_run_id"]: x for x in db.all(
        f"SELECT * FROM model_runs WHERE model_run_id IN ({', '.join('?' for _ in preds) or 'NULL'})",
        [p["model_run_id"] for p in preds])}
    prod_id = prod["model_run_id"] if prod else None
    models = _ml_view(preds, runs, prod_id)
    main = next((p for p in preds if p["model_run_id"] == prod_id), preds[0] if preds else None)
    if main is None:
        return {"model_run_id": "", "model_type": "ensemble", "horizon": horizon, "date": iso(db.as_of), "score": None,
                "rank_pct": 0.0, "decile": 0, "shap_top": [], "history": [], "model_metrics": {}, "models": [],
                "expected_excess_return": None, "interval": None, "confidence": None, "trained_at": None,
                "feature_set_version": None, "shap_unit": SHAP_UNIT,
                "data_status": "pending" if not db.snap.input_ok("ml_predictions") else "ok",
                "scope": None, "scope_note": None, "reason": _ml_reason(db, security_id, horizon)}
    run = runs.get(main["model_run_id"], {})
    mv = next(m for m in models if m["model_run_id"] == main["model_run_id"])
    hist = db.all("SELECT date, rank_pct, decile FROM ml_pred WHERE security_id = ? AND horizon = ? AND model_run_id = ? "
                  "ORDER BY date", [security_id, horizon, main["model_run_id"]])
    return {
        "model_run_id": main["model_run_id"], "model_type": run.get("model_type") or "ensemble", "horizon": horizon,
        "date": iso(main["date"]), "score": fnum(main.get("score")), "rank_pct": fnum(main.get("rank_pct")) or 0.0,
        "decile": int(main.get("decile") or 0), "shap_top": shap_out(main.get("shap_top")),
        "history": [{"date": iso(h["date"]), "rank_pct": fnum(h["rank_pct"]) or 0.0, "decile": int(h["decile"] or 0)} for h in hist],
        "model_metrics": {k: fnum(run.get(k)) for k in ("ic_mean", "rank_ic_mean", "rank_ic_ir", "rank_ic_tstat",
                                                         "decile_spread_ann", "long_short_sharpe", "turnover_monthly", "hit_rate")},
        "models": models, "expected_excess_return": mv["expected_excess_return"], "interval": mv["interval"],
        "confidence": mv["confidence"], "trained_at": mv["trained_at"], "feature_set_version": mv["feature_set_version"],
        "shap_unit": SHAP_UNIT, "data_status": "ok", "scope": main.get("scope") or "mvp",
        "scope_note": _ml_scope_note(db, security_id, horizon) if main.get("scope") == "extrapolated" else None,
        "reason": None,
    }


def _ml_scope_note(db: Db, security_id: str, horizon: str) -> str | None:
    try:
        return db.scalar("SELECT detail FROM ml_scope WHERE security_id = ? AND horizon = ?", [security_id, horizon])
    except Exception:  # noqa: BLE001 - table absent in an older snapshot
        return None


def _ml_reason(db: Db, security_id: str, horizon: str) -> dict[str, Any] | None:
    from app.core import availability as AV  # noqa: PLC0415

    try:
        row = db.one("SELECT status, reason, detail FROM ml_scope WHERE security_id = ? AND horizon = ?",
                     [security_id, horizon])
    except Exception:  # noqa: BLE001
        return None
    if not row or row.get("status") != "no_prediction" or not row.get("reason"):
        return None
    return AV.make(row["reason"], row.get("detail")).public()


# ------------------------------------------------------------------------------------------------ macro & filings


def regime_snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    labels = list(row.get("state_labels") or [])
    probs = list(row.get("probabilities") or [])
    pm: dict[str, float] = {}
    for lab, p in zip(labels, probs, strict=False):
        if lab is not None and p is not None:
            pm[lab] = round(pm.get(lab, 0.0) + float(p), 6)
    return {"date": iso(row["date"]), "label": row["regime_label"], "probabilities": pm}


def latest_regime(db: Db, scope: str) -> dict[str, Any] | None:
    return db.one("SELECT * FROM macro_regimes WHERE region_scope = ? ORDER BY date DESC LIMIT 1", [scope]) or \
        db.one("SELECT * FROM macro_regimes ORDER BY region_scope = 'GLOBAL' DESC, date DESC LIMIT 1")


@router.get("/companies/{security_id}/macro", operation_id="getCompanyMacro")
def get_company_macro(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = load_sec(db, security_id)
    rows = db.all("SELECT * FROM macro_betas WHERE security_id = ? ORDER BY factor_id", [security_id])
    betas = [{"factor_id": x["factor_id"], "beta": fnum(x["beta"]), "t_stat": fnum(x.get("t_stat")),
              "peer_percentile": fnum(x.get("peer_percentile"))} for x in rows if not x["factor_id"].startswith("regime_")]
    regime_ret = {x["factor_id"].removeprefix("regime_"): fnum(x["beta"]) for x in rows if x["factor_id"].startswith("regime_")}
    labels = ("expansion", "slowdown", "contraction", "recovery", "stagflation")
    from app.api.data_integrity import regime_scope_info  # noqa: PLC0415

    rinfo = regime_scope_info(db, MACRO_SCOPE.get(r.get("region") or "", r.get("region") or "GLOBAL"))
    reg = latest_regime(db, rinfo["scope"]) if rinfo["scope"] else None
    snap = regime_snapshot(reg)
    if snap is not None:  # F07: say which model answered (own scope or labelled GLOBAL fallback)
        snap.update({k: rinfo[k] for k in ("scope", "requested_scope", "modelled", "fallback", "model_id")})
    fit = None
    if snap and regime_ret:
        num = sum(p * (regime_ret.get(lab) or 0.0) for lab, p in snap["probabilities"].items() if regime_ret.get(lab) is not None)
        fit = round(num, 6)
    return {"as_of": iso(rows[0]["as_of_date"]) if rows else None, "betas": betas,
            "regime_returns": [{"regime_label": lab, "mean_excess_return": regime_ret.get(lab)} for lab in labels
                               if lab in regime_ret],
            "current_regime": snap, "regime_fit_score": fit, "macro_v4": _macro_v4(db, security_id),
            "data_status": db.snap.status("macro_betas", "macro_regimes")}


def _macro_v4(db: Db, security_id: str) -> dict[str, Any] | None:
    """macro-w4 (v1.12.0, additive): rolling macro exposures, sector regime table, scenario impacts, episode replay."""
    from app.api.routers.macro_v4 import company_macro_v4  # noqa: PLC0415

    try:
        return company_macro_v4(db, security_id)
    except Exception:  # noqa: BLE001 - never break the legacy macro tab
        from app.core.logsetup import get_logger  # noqa: PLC0415

        get_logger(__name__).exception("company macro_v4 failed for %s", security_id)
        return None


@router.get("/companies/{security_id}/filings", operation_id="getCompanyFilings")
def get_company_filings(security_id: str,
                        form_class: Literal["annual", "semiannual", "quarterly", "current_report", "insider", "holdings",
                                            "proxy", "prospectus", "other"] | None = None,
                        limit: int = Query(50, ge=1, le=500), db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    iss = db.scalar("SELECT issuer_id FROM sec WHERE security_id = ?", [security_id])
    if iss is None:
        raise not_found(f"unknown security_id {security_id!r}")
    fc = "AND form_class = ?" if form_class else ""
    rows = db.all(f"SELECT * FROM filings WHERE issuer_id = ? {fc} ORDER BY knowledge_date DESC, filing_id LIMIT ?",
                  [iss] + ([form_class] if form_class else []) + [limit])
    return [{"filing_id": x["filing_id"], "source_id": x["source_id"], "form_type": x["form_type"],
             "form_class": x["form_class"], "period_end": iso(x.get("period_end")), "knowledge_date": iso(x["knowledge_date"]),
             "url": x.get("url"), "items": list(x.get("items") or [])} for x in rows]
