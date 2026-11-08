"""Smart money (roadmap v2 item #7; contract tag smart-money): who is buying, selling, shorting and crowding.

Serving tables (app.api.serving._Builder.smart_money): sm_inst / sm_inst_latest (13F ownership per quarter, PIT on the
13F deadline cutoff), sm_holders (top-100 holders of the latest two quarters), sm_filers, sm_si / sm_si_latest (FINRA
short interest + FTD), sm_events (13D / 13G), sm_eu / sm_eu_current (EU / UK net short disclosures), sm_signals /
sm_signals_latest (MODEL-BASED combined score) and sm_sector. Definitions: docs/SMART_MONEY.md.

13F positions lag: they are as of the quarter end and become public up to 45 days later - every response carries the
quarter end and the cutoff ("positions as of quarter end Q, filed by D"). Hedge-fund-like, smart cohort, crowding,
squeeze-risk, FTD-spike and the combined score are model-based and labelled; the data sources are official.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, get_db
from app.api.routers._common import (
    equity_only_sql,
    iso,
    not_found,
    parse_regions,
    rnd,
    sec_ref,
    sec_ref_sql,
)
from app.core.logsetup import get_logger

router = APIRouter(tags=["smart-money"])
log = get_logger("app.api.smart_money")

SOURCES = [
    {"id": "sec_13f", "label": "SEC Form 13F (institutional holdings)", "official": True,
     "note": "Quarter-end long positions of managers with > $100M; filed up to 45 days after the quarter"},
    {"id": "finra_si", "label": "FINRA equity short interest", "official": True,
     "note": "Twice-monthly settlement dates, published about 7 business days later"},
    {"id": "sec_ftd", "label": "SEC fails-to-deliver", "official": True, "note": "Half-month files"},
    {"id": "sec_13dg", "label": "SEC Schedules 13D / 13G", "official": True,
     "note": "Beneficial owners above 5%: 13D = active / control intent, 13G = passive"},
    {"id": "sec_form4", "label": "Insider transactions (SEC Form 4 + national regulators)", "official": True, "note": ""},
    {"id": "eu_short", "label": "EU / UK net short positions (FCA, AMF, CNMV, AFM, CBI, FSMA, FI, Finanstilsynet)",
     "official": True, "note": "Holders at or above 0.5% of issued capital; UK aggregates only since 10 Jul 2026"},
    {"id": "cftc", "label": "CFTC Commitments of Traders (futures)", "official": True, "note": "Weekly, Tuesday positions"},
]
REGULATORS = {
    "fca": ("UK FCA", "GB", "https://www.fca.org.uk/markets/short-selling/notification-disclosure-net-short-positions"),
    "amf": ("France AMF", "FR", "https://www.data.gouv.fr/datasets/62738e33f1be79935d3e5553"),
    "cnmv": ("Spain CNMV", "ES", "https://www.cnmv.es/DocPortal/Posiciones-Cortas/NetShortPositions.xls"),
    "afm": ("Netherlands AFM", "NL", "https://www.afm.nl/en/sector/registers/meldingenregisters/netto-shortposities-actueel"),
    "cbi": ("Ireland CBI", "IE", "https://www.centralbank.ie/regulation/industry-market-sectors/securities-markets/short-selling-regulation/public-net-short-positions"),
    "fsma": ("Belgium FSMA", "BE", "https://www.fsma.be/en/shortselling"),
    "fi_se": ("Sweden FI", "SE", "https://www.fi.se/en/our-registers/net-short-positions/"),
    "ftno": ("Norway Finanstilsynet", "NO", "https://ssr.finanstilsynet.no/"),
}
SKIPPED = [
    {"id": "consob", "label": "Italy CONSOB", "reason": "Register file behind a bot challenge (captcha): automated access refused"},
    {"id": "bafin", "label": "Germany BaFin (Bundesanzeiger)", "reason": "robots.txt disallows the net short position pages (/nlp)"},
    {"id": "dfsa", "label": "Denmark Finanstilsynet", "reason": "Monthly snapshots only, no current / historic register"},
    {"id": "fin_fsa", "label": "Finland FIN-FSA", "reason": "HTML tables only"},
]
LABELS = {
    "lag": "13F positions are as of the quarter end and are filed up to 45 days later (point-in-time on the filing deadline)",
    "model_based": "Hedge-fund-like, smart cohort, crowding, squeeze risk, FTD spike and the smart-money score are "
                   "model-based (our definitions, docs/SMART_MONEY.md); the underlying data are official filings",
    "hedge_fund_like": "Model-based: 10-1,000 positions, AUM >= $50M, >= 60% single stocks, and quarterly turnover >= 15% or "
                       "options >= 5% of value or (top-10 weight >= 50% and turnover >= 5%)",
    "smart_cohort": "Model-based: hedge-fund-like filers whose reported books beat the average 13F filer over the past "
                    "4 quarters (top decile, positive excess), selected point-in-time",
    "crowding": "Model-based: number of hedge-fund-like filers holding the stock among their top-20 positions (percentile)",
    "squeeze": "Model-based: short interest >= 15% of shares outstanding, days to cover >= 5, 20-day return >= +10%, "
               "shares <= 150M or market cap <= $2B",
    "si_float": "Short interest % uses shares outstanding (float is not published)",
    "ftd_spike": "Model-based: daily fails >= 0.5% of shares outstanding, or z >= 3 vs the previous 12 files with >= 100k shares",
    "score": "Model-based composite of 13F flows, smart-cohort flows, insider signal, short interest and EU short "
             "disclosures (z-scores within the date; higher = more bullish)",
    "eu_short": "Net short positions >= 0.5% of issued capital as published by the regulators; since 10 Jul 2026 the UK "
                "FCA publishes aggregates per issuer only",
}
EVENT_LABEL = {"new_13d": "New 13D", "amend_13d": "13D amendment", "switch_13g_to_13d": "13G -> 13D switch",
               "new_13g": "New 13G", "amend_13g": "13G amendment"}
EQUITY_FUT = ("13874A", "209742", "239742", "124603", "240743", "1170E1")


def _f(v: Any, nd: int = 6) -> float | None:
    return rnd(v, nd)


def _i(v: Any) -> int | None:
    return int(v) if v is not None else None


def _status(db: Db, *tables: str) -> str:
    return db.snap.status(*tables)


def _lag(db: Db) -> dict[str, Any] | None:
    r = db.one("""SELECT period_end, max(knowledge_date) AS cutoff, bool_and(is_complete) AS complete, count(*) AS n
                  FROM sm_inst GROUP BY 1 ORDER BY 1 DESC LIMIT 1""")
    if not r:
        return None
    pe, cut = r["period_end"], r["cutoff"]
    state = "" if r["complete"] else " (filing window still open)"
    return {"period_end": iso(pe), "cutoff": iso(cut), "is_complete": bool(r["complete"]), "n_securities": int(r["n"]),
            "label": f"13F positions as of quarter end {iso(pe)}, filed by {iso(cut)}{state}"}


def _row(r: dict[str, Any]) -> dict[str, Any]:
    """One security row of the overview lists (contract SmartMoneyRow)."""
    return {
        "security": sec_ref(r),
        "period_end": iso(r.get("period_end")), "inst_own_pct": _f(r.get("inst_own_pct"), 5),
        "inst_own_chg": _f(r.get("inst_own_chg"), 5), "flow_pct_out": _f(r.get("flow_pct_out"), 6),
        "breadth": _f(r.get("breadth"), 4), "n_holders": _i(r.get("n_holders")), "n_new": _i(r.get("n_new")),
        "n_exited": _i(r.get("n_exited")), "smart_buyers": _i(r.get("smart_buyers")),
        "smart_sellers": _i(r.get("smart_sellers")), "smart_flow_pct_out": _f(r.get("smart_flow_pct_out"), 6),
        "hf_top20_count": _i(r.get("hf_top20_count")), "crowding_pct": _f(r.get("crowding_pct"), 4),
        "settlement_date": iso(r.get("settlement_date")), "si_pct_out": _f(r.get("si_pct_out"), 5),
        "si_chg_pct": _f(r.get("si_chg_pct"), 4), "days_to_cover": _f(r.get("days_to_cover"), 2),
        "si_z_1y": _f(r.get("si_z_1y"), 2), "ret_20d": _f(r.get("ret_20d"), 4),
        "squeeze_score": _f(r.get("squeeze_score"), 4), "squeeze_flag": bool(r.get("squeeze_flag") or False),
        "ftd_pct_out": _f(r.get("ftd_pct_out"), 6), "ftd_spike": bool(r.get("ftd_spike") or False),
        "score": _f(r.get("score"), 4), "score_pct": _f(r.get("score_pct"), 2),
        "eu_short_pct": _f(r.get("eu_short_pct"), 3),
    }


def _region_sql(regs: list[str]) -> tuple[str, list[Any]]:
    if not regs:
        return "", []
    return f" AND s.region IN ({', '.join('?' for _ in regs)})", list(regs)


def _event(r: dict[str, Any], with_sec: bool = True) -> dict[str, Any]:
    out = {"filing_id": r["filing_id"], "event_date": iso(r["event_date"]), "event_type": r["event_type"],
           "event_label": EVENT_LABEL.get(r["event_type"], r["event_type"]), "form_type": r["form_type"],
           "subject_cik": r["subject_cik"], "subject_name": r["subject_name"], "filer_cik": r["filer_cik"],
           "filer_name": r["filer_name"], "n_filers": int(r["n_filers"] or 0), "prior_13g_date": iso(r["prior_13g_date"]),
           "role_source": r["role_source"], "url": r["url"], "security": None}
    if with_sec and r.get("ref_security_id"):
        out["security"] = sec_ref(r)
    return out


def _eu(r: dict[str, Any]) -> dict[str, Any]:
    reg = REGULATORS.get(r["regulator"], (r["regulator"], "", ""))
    return {"regulator": r["regulator"], "regulator_label": reg[0], "country": r["country"],
            "position_type": r["position_type"], "holder_name": r["holder_name"], "holder_lei": r.get("holder_lei"),
            "issuer_name": r["issuer_name"], "isin": r["isin"], "security_id": r.get("security_id"),
            "ticker": r.get("ref_ticker"), "name": r.get("ref_name"), "position_pct": _f(r["position_pct"], 3),
            "prev_pct": _f(r.get("prev_pct"), 3), "prev_date": iso(r.get("prev_date")),
            "position_date": iso(r["position_date"]), "publication_date": iso(r.get("publication_date")),
            "knowledge_date": iso(r["knowledge_date"]), "knowledge_date_estimated": bool(r["knowledge_date_estimated"]),
            "below_threshold": bool(r["below_threshold"]), "source_url": r["source_url"]}


# ------------------------------------------------------------------------------------------------ overview


#: cross-dataset enrichment of the overview lists (completeness audit 2026-10-27): every SmartMoneyRow carries the
#: latest 13F, short-interest and combined-score fields of its security, not only those of the list's own dataset
_I_COLS = ("period_end", "inst_own_pct", "inst_own_chg", "flow_pct_out", "breadth", "n_holders", "n_new", "n_exited",
           "smart_buyers", "smart_sellers", "smart_flow_pct_out", "hf_top20_count", "crowding_pct")
_X_COLS = ("settlement_date", "si_pct_out", "si_chg_pct", "days_to_cover", "si_z_1y", "ret_20d", "squeeze_score",
           "squeeze_flag", "ftd_pct_out", "ftd_spike")
_G_COLS = ("score", "score_pct", "eu_short_pct")
_LJ_I = " LEFT JOIN sm_inst_latest ei ON ei.security_id = s.security_id"
_LJ_X = " LEFT JOIN sm_si_latest ex ON ex.security_id = s.security_id"
_LJ_G = " LEFT JOIN sm_signals_latest eg ON eg.security_id = s.security_id"


def _enrich(alias: str, cols: tuple[str, ...], skip: tuple[str, ...] = ()) -> str:
    return "".join(f", {alias}.{c}" for c in cols if c not in skip)


@router.get("/smart-money/overview", operation_id="getSmartMoneyOverview")
def overview(region: str | None = None, regions: list[str] | None = Query(None), limit: int = Query(15, ge=5, le=50),
             min_mcap_usd: float = Query(3e8, ge=0), db: Db = Depends(get_db)) -> dict[str, Any]:
    regs = parse_regions(region, regions)
    rsql, rpar = _region_sql(regs)
    lag = _lag(db)
    base = f"""FROM sec s {{join}} WHERE {equity_only_sql('s')} AND coalesce(s.market_cap_usd, 0) >= ? {rsql}"""
    cols = sec_ref_sql("s")
    out: dict[str, list[dict[str, Any]]] = {}
    if lag:
        j = "JOIN sm_inst_latest i ON i.security_id = s.security_id AND i.period_end = ?"
        q = (f"SELECT {cols}, i.* EXCLUDE (security_id, issuer_id){_enrich('ex', _X_COLS)}{_enrich('eg', _G_COLS)} "
             + base.format(join=j + _LJ_X + _LJ_G) + " AND i.n_holders >= 20")
        # flow lists: established names only (>= 20 holders last quarter: no IPO "inflows"), plausible share counts
        qf = q + " AND coalesce(i.n_holders_prev, 0) >= 20 AND coalesce(i.inst_own_pct, 0) <= 1.5"
        pe = date.fromisoformat(lag["period_end"])
        out["accumulation"] = [_row(r) for r in db.all(qf + " AND i.flow_pct_out IS NOT NULL ORDER BY i.flow_pct_out DESC LIMIT ?",
                                                        [pe, min_mcap_usd, *rpar, limit])]
        out["distribution"] = [_row(r) for r in db.all(qf + " AND i.flow_pct_out IS NOT NULL ORDER BY i.flow_pct_out ASC LIMIT ?",
                                                        [pe, min_mcap_usd, *rpar, limit])]
        out["smart_buying"] = [_row(r) for r in db.all(
            qf + " AND coalesce(i.smart_buyers, 0) + coalesce(i.smart_sellers, 0) > 0 "
                "ORDER BY coalesce(i.smart_buyers, 0) - coalesce(i.smart_sellers, 0) DESC, i.smart_flow_pct_out DESC NULLS LAST LIMIT ?",
            [pe, min_mcap_usd, *rpar, limit])]
        out["crowded"] = [_row(r) for r in db.all(q + " ORDER BY i.hf_top20_count DESC NULLS LAST LIMIT ?",
                                                   [pe, min_mcap_usd, *rpar, limit])]
    else:
        out.update(accumulation=[], distribution=[], smart_buying=[], crowded=[])
    sd = db.scalar("SELECT max(settlement_date) FROM sm_si_latest")
    if sd:
        j = "JOIN sm_si_latest x ON x.security_id = s.security_id AND x.settlement_date = ?"
        q = (f"SELECT {cols}, x.* EXCLUDE (security_id){_enrich('ei', _I_COLS)}{_enrich('eg', _G_COLS)} "
             + base.format(join=j + _LJ_I + _LJ_G))
        out["most_shorted"] = [_row(r) for r in db.all(q + " AND x.si_pct_out IS NOT NULL ORDER BY x.si_pct_out DESC LIMIT ?",
                                                        [sd, min_mcap_usd, *rpar, limit])]
        out["squeeze_watch"] = [_row(r) for r in db.all(
            q + " AND x.squeeze_score IS NOT NULL ORDER BY x.squeeze_flag DESC, x.squeeze_score DESC LIMIT ?",
            [sd, min_mcap_usd, *rpar, limit])]
        out["si_risers"] = [_row(r) for r in db.all(
            q + " AND x.si_chg_pct IS NOT NULL AND x.si_shares >= 100000 ORDER BY x.si_chg_pct DESC LIMIT ?",
            [sd, min_mcap_usd, *rpar, limit])]
        out["ftd_spikes"] = [_row(r) for r in db.all(
            f"SELECT {cols}, x.* EXCLUDE (security_id){_enrich('ei', _I_COLS)}{_enrich('eg', _G_COLS)} FROM sec s "
            f"JOIN sm_si_latest x ON x.security_id = s.security_id{_LJ_I}{_LJ_G} "
            f"WHERE x.ftd_spike AND x.settlement_date >= ? {rsql} ORDER BY x.ftd_pct_out DESC NULLS LAST LIMIT ?",
            [sd - timedelta(days=31), *rpar, limit])]
    else:
        out.update(most_shorted=[], squeeze_watch=[], si_risers=[], ftd_spikes=[])
    sg = db.scalar("SELECT max(as_of_date) FROM sm_signals_latest")
    if sg:
        j = "JOIN sm_signals_latest g ON g.security_id = s.security_id AND g.as_of_date = ?"
        q = (f"SELECT {cols}, g.score, g.score_pct, g.eu_short_pct, g.si_pct_out, g.si_chg_pct, g.inst_flow AS flow_pct_out"
             f"{_enrich('ei', _I_COLS, ('flow_pct_out',))}{_enrich('ex', _X_COLS, ('si_pct_out', 'si_chg_pct'))} "
             + base.format(join=j + _LJ_I + _LJ_X) + " AND g.n_components >= 2")
        out["top_score"] = [_row(r) for r in db.all(q + " ORDER BY g.score DESC LIMIT ?", [sg, min_mcap_usd, *rpar, limit])]
        out["bottom_score"] = [_row(r) for r in db.all(q + " ORDER BY g.score ASC LIMIT ?", [sg, min_mcap_usd, *rpar, limit])]
    else:
        out.update(top_score=[], bottom_score=[])
    ev = db.all(f"""SELECT e.*, {cols} FROM sm_events e LEFT JOIN sec s ON s.security_id = e.security_id
                    WHERE e.schedule = '13D' AND e.event_type IN ('new_13d', 'switch_13g_to_13d')
                      AND e.event_date >= ? ORDER BY e.event_date DESC, e.filing_id LIMIT ?""",
                [db.as_of - timedelta(days=60), limit * 2])
    n_act = db.scalar("""SELECT count(*) FROM sm_events WHERE schedule = '13D' AND event_type IN ('new_13d', 'switch_13g_to_13d')
                         AND event_date >= ?""", [db.as_of - timedelta(days=60)])
    eu_top = db.all("""SELECT c.*, s.ticker AS ref_ticker, s.name AS ref_name FROM sm_eu_current c
                        LEFT JOIN sec s ON s.security_id = c.security_id
                        WHERE c.position_type = 'individual' ORDER BY c.position_date DESC, c.position_pct DESC LIMIT ?""",
                    [limit])
    eu_sum = db.one("""SELECT count(*) FILTER (WHERE position_type = 'individual') AS n_positions,
                              count(DISTINCT isin) AS n_issuers, count(DISTINCT holder_name) FILTER (WHERE position_type = 'individual') AS n_holders,
                              max(position_date) AS last_date FROM sm_eu_current""") or {}
    fut = []
    try:
        fut = db.all(f"""SELECT l.market_code, coalesce(m.short_name, m.market_name) AS name, l.report_date, l.net, l.net_pct_oi, l.cot_index_3y,
                                l.z_3y, l.extreme, l.knowledge_date
                         FROM cftc_latest l LEFT JOIN cftc_markets m USING (market_code)
                         WHERE l.report_type = 'tff_fut' AND l.category = 'lev_money'
                           AND l.market_code IN ({', '.join("'" + c + "'" for c in EQUITY_FUT)})
                         ORDER BY l.market_code""")
    except Exception as e:  # noqa: BLE001 - CFTC tables are optional in a snapshot
        log.info("smart-money overview: CFTC positioning unavailable: %s", e)
        fut = []
    return {
        "as_of": iso(db.as_of), "lag": lag, **out,
        "activist_feed": [_event(r) for r in ev],
        "activist_count_60d": int(n_act or 0),
        "eu_register": {"n_positions": int(eu_sum.get("n_positions") or 0), "n_issuers": int(eu_sum.get("n_issuers") or 0),
                        "n_holders": int(eu_sum.get("n_holders") or 0), "last_position_date": iso(eu_sum.get("last_date")),
                        "latest": [_eu(r) for r in eu_top]},
        "futures_positioning": [{"market_code": r["market_code"], "name": r["name"] or r["market_code"],
                                 "report_date": iso(r["report_date"]), "release_date": iso(r["knowledge_date"]),
                                 "category": "Leveraged funds (TFF)", "net": _i(r["net"]), "net_pct_oi": _f(r["net_pct_oi"], 4),
                                 "cot_index_3y": _f(r["cot_index_3y"], 3), "z_3y": _f(r["z_3y"], 2), "extreme": r["extreme"]}
                                for r in fut],
        "si_settlement_date": iso(sd), "score_date": iso(sg), "sources": SOURCES, "labels": LABELS,
        "data_status": _status(db, "inst_ownership", "si_signals", "activist_events", "eu_short_positions", "smart_money_signals"),
    }


# ------------------------------------------------------------------------------------------------ EU short register


@router.get("/smart-money/eu-shorts", operation_id="getSmartMoneyEuShorts")
def eu_shorts(regulator: str | None = Query(None, pattern="^(fca|amf|cnmv|afm|cbi|fsma|fi_se|ftno)$"),
              q: str | None = Query(None, max_length=80), holder: str | None = Query(None, max_length=120),
              security_id: str | None = Query(None, max_length=60), current: bool = True,
              position_type: str | None = Query(None, pattern="^(individual|aggregate)$"),
              limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0), db: Db = Depends(get_db)) -> dict[str, Any]:
    src = "sm_eu_current" if current else "sm_eu"
    where, par = ["true"], []
    if regulator:
        where.append("c.regulator = ?")
        par.append(regulator)
    if position_type:
        where.append("c.position_type = ?")
        par.append(position_type)
    if security_id:
        where.append("c.security_id = ?")
        par.append(security_id)
    if holder:
        # case-insensitive: the same holder is filed as "Citadel Advisors LLC" and "CITADEL ADVISORS LLC" (guide 07)
        where.append("lower(c.holder_name) = lower(?)")
        par.append(holder.strip())
    if q:
        # tickers also match without the exchange suffix / share-class letter (SBB matches SBB-B.ST)
        tk = "lower(coalesce(s.ticker, ''))"
        where.append(f"(lower(c.holder_name) LIKE ? OR lower(c.issuer_name) LIKE ? OR c.isin = ? OR {tk} = ? "
                     f"OR split_part({tk}, '.', 1) = ? OR split_part(split_part({tk}, '.', 1), '-', 1) = ?)")
        ql = q.strip().lower()
        par += [f"%{ql}%", f"%{ql}%", q.strip().upper(), ql, ql, ql]
    w = " AND ".join(where)
    join = "LEFT JOIN sec s ON s.security_id = c.security_id"
    total = int(db.scalar(f"SELECT count(*) FROM {src} c {join} WHERE {w}", par) or 0)
    sel = "c.*" if current else "c.*, NULL AS prev_pct, NULL AS prev_date"
    rows = db.all(f"""SELECT {sel}, s.ticker AS ref_ticker, s.name AS ref_name FROM {src} c {join} WHERE {w}
                      ORDER BY c.position_date DESC, c.position_pct DESC NULLS LAST, c.holder_name LIMIT ? OFFSET ?""",
                  [*par, limit, offset])
    regs = {r["regulator"]: r for r in db.all("""
        SELECT regulator, count(*) FILTER (WHERE position_type = 'individual') AS n_positions,
               count(*) FILTER (WHERE position_type = 'aggregate') AS n_aggregates, count(DISTINCT isin) AS n_issuers,
               max(position_date) AS last_position_date FROM sm_eu_current GROUP BY 1""")}
    hist = {r["regulator"]: r for r in db.all("""SELECT regulator, min(position_date) AS first_date, max(position_date) AS last_date,
                                                        count(*) AS n_rows FROM sm_eu GROUP BY 1""")}
    reg_out = []
    for k, (lbl, cc, url) in REGULATORS.items():
        c, h = regs.get(k, {}), hist.get(k, {})
        reg_out.append({"id": k, "label": lbl, "country": cc, "url": url, "n_positions": int(c.get("n_positions") or 0),
                        "n_aggregates": int(c.get("n_aggregates") or 0), "n_issuers": int(c.get("n_issuers") or 0),
                        "last_position_date": iso(c.get("last_position_date")), "history_from": iso(h.get("first_date")),
                        "history_to": iso(h.get("last_date")), "n_rows": int(h.get("n_rows") or 0)})
    holders = db.all("""SELECT holder_name, count(*) AS n_positions, sum(position_pct) AS total_pct,
                               count(DISTINCT regulator) AS n_regulators, max(position_date) AS last_date
                        FROM sm_eu_current WHERE position_type = 'individual' GROUP BY 1
                        ORDER BY n_positions DESC, total_pct DESC LIMIT 25""")
    return {"as_of": iso(db.as_of), "current": current, "total": total, "items": [_eu(r) for r in rows],
            "regulators": reg_out, "skipped": SKIPPED,
            "top_holders": [{"holder_name": r["holder_name"], "n_positions": int(r["n_positions"]),
                             "total_pct": _f(r["total_pct"], 3), "n_regulators": int(r["n_regulators"]),
                             "last_date": iso(r["last_date"])} for r in holders],
            "labels": LABELS, "data_status": _status(db, "eu_short_positions")}


# ------------------------------------------------------------------------------------------------ activism


@router.get("/smart-money/activism", operation_id="getSmartMoneyActivism")
def activism(days: int = Query(90, ge=1, le=1095), event_types: str | None = Query(None, max_length=120),
             security_id: str | None = Query(None, max_length=60), limit: int = Query(200, ge=1, le=1000),
             db: Db = Depends(get_db)) -> dict[str, Any]:
    types = [t for t in (event_types or "new_13d,switch_13g_to_13d,amend_13d").split(",") if t]
    bad = [t for t in types if t not in EVENT_LABEL]
    if bad:
        from app.api.routers._common import invalid  # noqa: PLC0415

        raise invalid(f"unknown event_types {bad}; allowed {sorted(EVENT_LABEL)}")
    start = db.as_of - timedelta(days=days)
    par: list[Any] = [start, *types]
    extra = ""
    if security_id:
        extra = " AND e.security_id = ?"
        par.append(security_id)
    rows = db.all(f"""SELECT e.*, {sec_ref_sql('s')} FROM sm_events e LEFT JOIN sec s ON s.security_id = e.security_id
                      WHERE e.event_date >= ? AND e.event_type IN ({', '.join('?' for _ in types)}) {extra}
                      ORDER BY e.event_date DESC, e.filing_id LIMIT ?""", [*par, limit])
    counts = db.all("""SELECT event_type, count(*) AS n FROM sm_events WHERE event_date >= ? GROUP BY 1""", [start])
    filers = db.all("""SELECT filer_name, filer_cik, count(*) AS n, max(event_date) AS last_date FROM sm_events
                       WHERE event_date >= ? AND schedule = '13D' AND filer_name IS NOT NULL GROUP BY 1, 2
                       ORDER BY n DESC LIMIT 20""", [start])
    return {"as_of": iso(db.as_of), "from": iso(start), "event_types": types, "items": [_event(r) for r in rows],
            "counts": [{"event_type": r["event_type"], "label": EVENT_LABEL.get(r["event_type"], r["event_type"]),
                        "n": int(r["n"])} for r in counts],
            "top_filers": [{"filer_name": r["filer_name"], "filer_cik": r["filer_cik"], "n": int(r["n"]),
                            "last_date": iso(r["last_date"])} for r in filers],
            "labels": LABELS, "data_status": _status(db, "activist_events")}


# ------------------------------------------------------------------------------------------------ sectors


@router.get("/smart-money/sectors", operation_id="getSmartMoneySectors")
def sectors(region: str | None = Query(None, max_length=10), db: Db = Depends(get_db)) -> dict[str, Any]:
    if region:
        rows = db.all("SELECT * FROM sm_sector WHERE region = ? ORDER BY sector_id", [region])
    else:
        rows = db.all("SELECT * FROM sm_sector WHERE region IS NULL ORDER BY sector_id")
    out = [{"sector_id": r["sector_id"], "region": r["region"], "n_securities": int(r["n_securities"]),
            "n_13f": int(r["n_13f"]), "median_inst_own_pct": _f(r["median_inst_own_pct"], 4),
            "median_inst_own_chg": _f(r["median_inst_own_chg"], 5), "avg_breadth": _f(r["avg_breadth"], 4),
            "smart_buyers": _i(r["smart_buyers"]), "smart_sellers": _i(r["smart_sellers"]),
            "avg_crowding_pct": _f(r["avg_crowding_pct"], 3), "n_si": int(r["n_si"]),
            "median_si_pct_out": _f(r["median_si_pct_out"], 5), "avg_days_to_cover": _f(r["avg_days_to_cover"], 2),
            "n_squeeze": int(r["n_squeeze"]), "n_eu_shorted": int(r["n_eu_shorted"]),
            "avg_eu_short_pct": _f(r["avg_eu_short_pct"], 3), "avg_score": _f(r["avg_score"], 4),
            "n_scored": int(r["n_scored"])} for r in rows]
    regs = [r["region"] for r in db.all("SELECT DISTINCT region FROM sm_sector WHERE region IS NOT NULL ORDER BY 1")]
    return {"as_of": iso(db.as_of), "region": region, "regions": regs, "lag": _lag(db), "rows": out, "labels": LABELS,
            "data_status": _status(db, "inst_ownership", "si_signals")}


# ------------------------------------------------------------------------------------------------ company ownership


@router.get("/companies/{security_id}/ownership", operation_id="getCompanyOwnership")
def company_ownership(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')} FROM sec s WHERE s.security_id = ?", [security_id])
    if not r:
        raise not_found(f"unknown security_id {security_id!r}")
    trend = db.all("SELECT * FROM sm_inst WHERE security_id = ? ORDER BY period_end", [security_id])
    latest = trend[-1] if trend else None
    holders: list[dict[str, Any]] = []
    exited: list[dict[str, Any]] = []
    if latest:
        pes = [x["period_end"] for x in db.all("""SELECT DISTINCT period_end FROM sm_holders WHERE security_id = ?
                                                  ORDER BY 1 DESC LIMIT 2""", [security_id])]
        cur_all = db.all("SELECT * FROM sm_holders WHERE security_id = ? AND period_end = ? ORDER BY shares DESC",
                         [security_id, latest["period_end"]]) if latest["period_end"] in pes else []
        cur_ciks = {x["filer_cik"] for x in cur_all}
        cur = [x for x in cur_all if x["rk"] <= 100]
        prev_rows = db.all("SELECT * FROM sm_holders WHERE security_id = ? AND period_end = ?",
                           [security_id, pes[1]]) if len(pes) > 1 and pes[0] == latest["period_end"] else []
        prev = {x["filer_cik"]: x for x in prev_rows}
        so = latest.get("shares_out")
        for h in cur:
            p = prev.get(h["filer_cik"])
            ps = p["shares"] if p else None
            status = ("unknown" if not prev_rows else "new" if ps is None else
                      "added" if h["shares"] > ps * 1.01 else "trimmed" if h["shares"] < ps * 0.99 else "unchanged")
            holders.append({"filer_cik": h["filer_cik"], "filer_name": h["filer_name"], "shares": _f(h["shares"], 0),
                            "value_usd": _f(h["value_usd"], 0), "shares_prev": _f(ps, 0),
                            "change_shares": _f(h["shares"] - ps, 0) if ps is not None else None,
                            "change_pct": _f(h["shares"] / ps - 1, 4) if ps else None, "status": status,
                            "pct_out": _f(h["shares"] / so, 5) if so else None,
                            "weight_in_portfolio": _f(h["weight_in_portfolio"], 5),
                            "hedge_fund_like": bool(h["hedge_fund_like"] or False),
                            "in_smart_cohort": bool(h["in_smart_cohort"] or False), "filed_date": iso(h["filed_date"])})
        exited = [{"filer_cik": p["filer_cik"], "filer_name": p["filer_name"], "shares_prev": _f(p["shares"], 0),
                   "hedge_fund_like": bool(p["hedge_fund_like"] or False), "in_smart_cohort": bool(p["in_smart_cohort"] or False)}
                  for p in sorted((x for x in prev_rows if x["rk"] <= 100 and x["filer_cik"] not in cur_ciks),
                                  key=lambda x: -x["shares"])[:25]]
    si = db.all("SELECT * FROM sm_si WHERE security_id = ? ORDER BY settlement_date", [security_id])
    ins = db.all("""SELECT transaction_date, knowledge_date, insider_name, role_norm, txn_type, shares, price, value_usd,
                           cluster_id, is_open_market, source_id
                    FROM insider_txn WHERE sec_id = ? AND transaction_date >= ? AND txn_type IN ('buy', 'sell')
                    ORDER BY transaction_date DESC LIMIT 200""", [security_id, db.as_of - timedelta(days=730)])
    eu_cur = db.all("""SELECT c.*, NULL AS ref_ticker, NULL AS ref_name FROM sm_eu_current c WHERE c.security_id = ?
                       ORDER BY c.position_pct DESC NULLS LAST""", [security_id])
    eu_hist = db.all("""SELECT c.*, NULL AS ref_ticker, NULL AS ref_name, NULL AS prev_pct, NULL AS prev_date FROM sm_eu c
                        WHERE c.security_id = ? ORDER BY c.position_date DESC LIMIT 150""", [security_id])
    eu_series = db.all("""SELECT position_date AS d, regulator, max(position_pct) AS pct FROM sm_eu
                          WHERE security_id = ? AND position_type = 'aggregate' GROUP BY 1, 2 ORDER BY 1""", [security_id])
    ev = db.all("""SELECT e.*, NULL AS ref_security_id FROM sm_events e WHERE e.security_id = ?
                   ORDER BY e.event_date DESC LIMIT 50""", [security_id])
    sg = db.all("""SELECT as_of_date, score, score_pct, g_inst, g_smart, g_insider, g_short_interest, g_eu_short, n_components
                   FROM sm_signals WHERE security_id = ? ORDER BY as_of_date""", [security_id])
    lag = None
    if latest:
        state = "" if latest["is_complete"] else " (filing window still open)"
        lag = {"period_end": iso(latest["period_end"]), "cutoff": iso(latest["knowledge_date"]),
               "is_complete": bool(latest["is_complete"]), "n_securities": 1,
               "label": f"13F positions as of quarter end {iso(latest['period_end'])}, filed by {iso(latest['knowledge_date'])}{state}"}
    has = bool(trend or si or ins or eu_hist or ev or sg)
    return {
        "security": sec_ref(r), "as_of": iso(db.as_of), "lag": lag,
        "summary": {
            "inst_own_pct": _f(latest["inst_own_pct"], 5) if latest else None,
            "inst_own_chg": _f(latest["inst_own_chg"], 5) if latest else None,
            "n_holders": _i(latest["n_holders"]) if latest else None,
            "n_hf_holders": _i(latest["n_hf_holders"]) if latest else None,
            "n_smart_holders": _i(latest["n_smart_holders"]) if latest else None,
            "crowding_pct": _f(latest["crowding_pct"], 4) if latest else None,
            "hhi": _f(latest["hhi"], 5) if latest else None, "top10_share": _f(latest["top10_share"], 4) if latest else None,
            "shares_out": _f(latest["shares_out"], 0) if latest else None,
            "shares_out_source": latest["shares_out_source"] if latest else None,
            "si_pct_out": _f(si[-1]["si_pct_out"], 5) if si else None,
            "days_to_cover": _f(si[-1]["days_to_cover"], 2) if si else None,
            "squeeze_flag": bool(si[-1]["squeeze_flag"]) if si else False,
            "eu_short_pct": _f(sum(x["position_pct"] or 0 for x in eu_cur if x["position_type"] == "individual")
                               or max((x["position_pct"] or 0 for x in eu_cur), default=0), 3) if eu_cur else None,
            "score": _f(sg[-1]["score"], 4) if sg else None, "score_pct": _f(sg[-1]["score_pct"], 2) if sg else None,
            "score_date": iso(sg[-1]["as_of_date"]) if sg else None,
        },
        "holders": holders, "exited": exited,
        "ownership_trend": [{"period_end": iso(x["period_end"]), "knowledge_date": iso(x["knowledge_date"]),
                             "is_complete": bool(x["is_complete"]), "inst_own_pct": _f(x["inst_own_pct"], 5),
                             "n_holders": int(x["n_holders"]), "shares_held": _f(x["shares_held"], 0),
                             "n_new": _i(x["n_new"]), "n_added": _i(x["n_added"]), "n_trimmed": _i(x["n_trimmed"]),
                             "n_exited": _i(x["n_exited"]), "net_flow_shares": _f(x["net_flow_shares"], 0),
                             "hf_shares_pct": _f(x["hf_shares_pct"], 5), "crowding_pct": _f(x["crowding_pct"], 4),
                             "n_smart_holders": _i(x["n_smart_holders"]), "smart_net_flow_shares": _f(x["smart_net_flow_shares"], 0),
                             "put_call_ratio": _f(x["put_call_ratio"], 3)} for x in trend],
        "short_interest": [{"settlement_date": iso(x["settlement_date"]), "knowledge_date": iso(x["knowledge_date"]),
                            "si_shares": _f(x["si_shares"], 0), "si_pct_out": _f(x["si_pct_out"], 5),
                            "days_to_cover": _f(x["days_to_cover"], 2), "si_chg_pct": _f(x["si_chg_pct"], 4),
                            "si_z_1y": _f(x["si_z_1y"], 2), "squeeze_flag": bool(x["squeeze_flag"]),
                            "ftd_max_shares": _f(x["ftd_max_shares"], 0), "ftd_spike": bool(x["ftd_spike"])} for x in si],
        "insider_timeline": [{"transaction_date": iso(x["transaction_date"]), "knowledge_date": iso(x["knowledge_date"]),
                              "insider_name": x["insider_name"], "role": x["role_norm"], "txn_type": x["txn_type"],
                              "shares": _f(x["shares"], 0), "price": _f(x["price"], 4), "value_usd": _f(x["value_usd"], 0),
                              "cluster_id": x["cluster_id"], "is_open_market": bool(x["is_open_market"]),
                              "source_id": x["source_id"]} for x in ins],
        "eu_shorts": {"current": [_eu(x) for x in eu_cur], "notifications": [_eu(x) for x in eu_hist],
                      "aggregate_series": [{"date": iso(x["d"]), "regulator": x["regulator"], "pct": _f(x["pct"], 3)} for x in eu_series]},
        "activism": [_event(x, with_sec=False) for x in ev],
        "score_history": [{"as_of_date": iso(x["as_of_date"]), "score": _f(x["score"], 4), "score_pct": _f(x["score_pct"], 2),
                           "g_inst": _f(x["g_inst"], 3), "g_smart": _f(x["g_smart"], 3), "g_insider": _f(x["g_insider"], 3),
                           "g_short_interest": _f(x["g_short_interest"], 3), "g_eu_short": _f(x["g_eu_short"], 3),
                           "n_components": int(x["n_components"])} for x in sg],
        "sources": SOURCES, "labels": LABELS,
        "data_status": "ok" if has or db.snap.input_ok("inst_ownership") or db.snap.input_ok("si_signals") else "pending",
    }
