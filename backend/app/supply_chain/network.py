"""Supply-chain network analytics (job `supply_chain_network`, nightly) -> sc_edges, sc_exposure, sc_metrics, country_risk.

Point-in-time: every input is filtered to knowledge_date / date <= as_of (company_links filing dates, geographic
breakdown filing dates, event / macro component dates). Definitions: docs/SUPPLY_CHAIN.md and the table contracts.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

LOOKBACK_YEARS = 3
STALE_YEARS = 2
UNRESOLVED_CONF = 0.4          # confidence factor for edges whose counterparty name did not resolve
EA_MEMBERS = ["AT", "BE", "HR", "CY", "EE", "FI", "FR", "DE", "GR", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PT",
              "SK", "SI", "ES"]
RISK_WEIGHTS = {"events": 0.30, "escalation": 0.20, "recession": 0.30, "surprise": 0.10, "gpr": 0.10}
RECESSION_PREF = ["nyfed_yield_curve_official", "yield_curve_probit", "hmm_regime"]


def _read(table: str, settings: Settings, columns: list[str] | None = None, filters: pl.Expr | None = None) -> pl.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        return read_table(table, columns=columns, filters=filters, settings=settings)
    except Exception as e:  # noqa: BLE001 - table not built yet
        log.info("supply_chain: %s unavailable (%s)", table, e)
        return pl.DataFrame()


def _now() -> datetime:
    return datetime.now(UTC)


# ================================================================================================ reference
def issuer_ref(settings: Settings, universe: str = "full") -> pl.DataFrame:
    """[issuer_id, name, country, region, sector_id, security_id, in_universe] for all issuers (primary security)."""
    iss = _read("issuers", settings, columns=["issuer_id", "name", "short_name", "country", "region", "sector_id"])
    if iss.is_empty():
        return iss
    sm = _read("securities_master", settings, columns=["security_id", "issuer_id", "is_primary_security", "is_active"])
    um = _read("universe_membership", settings, columns=["universe_id", "security_id", "valid_to"])
    uni = set(um.filter((pl.col("universe_id") == universe) & pl.col("valid_to").is_null())["security_id"].to_list()) \
        if not um.is_empty() else set()
    if not sm.is_empty():
        sm = sm.filter(pl.col("issuer_id").is_not_null()).with_columns(
            pl.col("security_id").is_in(list(uni)).alias("in_u")).sort(
            ["in_u", "is_primary_security", "is_active"], descending=True, nulls_last=True).unique("issuer_id", keep="first")
        iss = iss.join(sm.select("issuer_id", "security_id", "in_u"), on="issuer_id", how="left")
    else:
        iss = iss.with_columns(pl.lit(None, pl.Utf8).alias("security_id"), pl.lit(False).alias("in_u"))
    return iss.with_columns(pl.col("in_u").fill_null(False).alias("in_universe"),
                            pl.coalesce(pl.col("short_name"), pl.col("name")).alias("display")).drop("in_u")


# ================================================================================================ edges
def build_edges(links: pl.DataFrame, ref: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """company_links (customer + supplier rows) -> one directed supplier -> customer edge per pair (PIT as_of)."""
    if links.is_empty():
        return pl.DataFrame()
    lo = date(as_of.year - LOOKBACK_YEARS, as_of.month, min(as_of.day, 28))
    lk = links.filter(pl.col("link_type").is_in(["customer", "supplier"]) & (pl.col("knowledge_date") <= as_of)
                      & (pl.coalesce(pl.col("period_end"), pl.col("knowledge_date")) >= lo)
                      & (pl.col("dst_kind") == "company"))
    if lk.is_empty():
        return pl.DataFrame()
    for c in ("rule", "snippet", "form_type", "rule_conf", "src_name", "dst_name"):
        if c not in lk.columns:
            lk = lk.with_columns(pl.lit(None).alias(c))
    src_key = pl.coalesce(pl.col("src_issuer_id"), pl.lit("cik:") + pl.col("src_entity_id"))
    conf = pl.when(pl.col("dst_issuer_id").is_not_null() & pl.col("confidence").is_not_null() & (pl.col("confidence") > 0)
                   ).then(pl.col("confidence")).otherwise(pl.col("rule_conf").fill_null(0.9) * UNRESOLVED_CONF)
    lk = lk.with_columns(src_key.alias("__src"), conf.alias("__conf"))
    cust = lk.filter(pl.col("link_type") == "customer").select(
        pl.col("__src").alias("supplier_key"), pl.col("dst_key").alias("customer_key"),
        pl.col("src_issuer_id").alias("supplier_issuer_id"), pl.col("dst_issuer_id").alias("customer_issuer_id"),
        pl.col("src_name").alias("supplier_disc_name"), pl.col("dst_name").alias("customer_disc_name"),
        pl.col("share").alias("s_rev"), pl.lit(None, pl.Float64).alias("s_pur"), pl.lit("disclosed_customer").alias("et"),
        "__conf", "source", "rule", "filing_id", "form_type", "snippet", "period_end", "knowledge_date")
    sup = lk.filter(pl.col("link_type") == "supplier").select(
        pl.col("dst_key").alias("supplier_key"), pl.col("__src").alias("customer_key"),
        pl.col("dst_issuer_id").alias("supplier_issuer_id"), pl.col("src_issuer_id").alias("customer_issuer_id"),
        pl.col("dst_name").alias("supplier_disc_name"), pl.col("src_name").alias("customer_disc_name"),
        pl.lit(None, pl.Float64).alias("s_rev"), pl.col("share").alias("s_pur"), pl.lit("disclosed_supplier").alias("et"),
        "__conf", "source", "rule", "filing_id", "form_type", "snippet", "period_end", "knowledge_date")
    e = pl.concat([cust, sup], how="vertical_relaxed").filter(pl.col("supplier_key") != pl.col("customer_key"))
    if e.is_empty():
        return pl.DataFrame()
    e = e.sort(["knowledge_date", "__conf"], descending=[True, True])
    key = ["supplier_key", "customer_key"]
    # latest disclosure's share per channel (a later filing restates the earlier one)
    latest_rev = e.filter(pl.col("s_rev").is_not_null()).group_by(key).agg(pl.col("s_rev").first())
    latest_pur = e.filter(pl.col("s_pur").is_not_null()).group_by(key).agg(pl.col("s_pur").first())
    agg = e.group_by(key).agg(
        pl.col("supplier_issuer_id").drop_nulls().first(), pl.col("customer_issuer_id").drop_nulls().first(),
        pl.col("supplier_disc_name").drop_nulls().first(), pl.col("customer_disc_name").drop_nulls().first(),
        pl.col("et").unique().alias("ets"), pl.col("__conf").max().alias("confidence"), pl.len().alias("n_disclosures"),
        pl.col("source").unique().sort().alias("sources"), pl.col("rule").drop_nulls().unique().sort().alias("rules"),
        pl.col("filing_id").first(), pl.col("form_type").first(), pl.col("snippet").first(), pl.col("period_end").first(),
        pl.col("knowledge_date").first(), pl.col("knowledge_date").min().alias("first_knowledge_date"))
    agg = agg.join(latest_rev, on=key, how="left").join(latest_pur, on=key, how="left")
    names = dict(ref.select("issuer_id", "display").iter_rows()) if not ref.is_empty() else {}
    stale_lo = date(as_of.year - STALE_YEARS, as_of.month, min(as_of.day, 28))
    out = agg.with_columns(
        pl.lit(as_of).alias("as_of"),
        pl.coalesce(pl.col("supplier_issuer_id").replace_strict(names, default=None, return_dtype=pl.Utf8),
                    pl.col("supplier_disc_name")).alias("supplier_name"),
        pl.coalesce(pl.col("customer_issuer_id").replace_strict(names, default=None, return_dtype=pl.Utf8),
                    pl.col("customer_disc_name")).alias("customer_name"),
        pl.when(pl.col("ets").list.len() > 1).then(pl.lit("both")).otherwise(pl.col("ets").list.first()).alias("edge_type"),
        pl.col("s_rev").clip(0.0, 1.0).alias("share_of_supplier_revenue"),
        pl.col("s_pur").clip(0.0, 1.0).alias("share_of_customer_purchases"),
        pl.col("n_disclosures").cast(pl.Int16),
        (pl.coalesce(pl.col("period_end"), pl.col("knowledge_date")) < stale_lo).alias("stale"),
        pl.col("snippet").str.slice(0, 200))
    return out.select("as_of", "supplier_key", "customer_key", "supplier_issuer_id", "customer_issuer_id", "supplier_name",
                      "customer_name", "edge_type", "share_of_supplier_revenue", "share_of_customer_purchases",
                      "confidence", "n_disclosures", "sources", "rules", "filing_id", "form_type", "snippet",
                      "period_end", "knowledge_date", "first_knowledge_date", "stale")


# ================================================================================================ geography
def direct_country_weights(settings: Settings, as_of: date) -> tuple[dict[str, dict[str, float]], dict[str, dict[str, Any]]]:
    """issuer -> {iso2: share} from the latest annual geographic revenue breakdown known at as_of (+ its meta)."""
    from app.linkage.weights import allocate_exposure  # noqa: PLC0415

    g = _read("company_geo_revenue", settings, filters=(pl.col("measure") == "revenue") & (pl.col("qtrs") == 4)
              & pl.col("issuer_id").is_not_null() & (pl.col("knowledge_date") <= as_of))
    geo = _read("geo_countries", settings, columns=["iso2", "gdp_md_usd"])
    if g.is_empty():
        return {}, {}
    gdp = {a: float(b) for a, b in geo.drop_nulls().iter_rows() if b and b > 0} if not geo.is_empty() else {}
    best = (g.group_by("issuer_id", "period_end", "filing_id", "concept", "source_id")
            .agg(pl.col("knowledge_date").max(), (pl.col("member_type") == "country").sum().alias("nc"), pl.len().alias("n"))
            .sort(["period_end", "knowledge_date", "nc", "n"], descending=True).unique("issuer_id", keep="first"))
    g = g.join(best.select("issuer_id", "period_end", "filing_id", "concept", "source_id"),
               on=["issuer_id", "period_end", "filing_id", "concept", "source_id"])
    out: dict[str, dict[str, float]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for (iid,), grp in g.group_by(["issuer_id"]):
        sh = allocate_exposure(grp, gdp, settings)
        if sh:
            out[iid] = sh
            meta[iid] = {"period_end": grp["period_end"].max(), "knowledge_date": grp["knowledge_date"].max(),
                         "source_id": grp["source_id"][0],
                         "method": grp["method"][0] if "method" in grp.columns else None,
                         "confidence": grp["confidence"][0] if "confidence" in grp.columns else None}
    return out, meta


# ================================================================================================ country risk
def _latest_by(df: pl.DataFrame, key: str, date_col: str) -> pl.DataFrame:
    return df.sort(date_col).group_by(key).agg(pl.all().last())


def _pct_rank(vals: dict[str, float], higher_is_risk: bool = True) -> dict[str, float]:
    ks = [k for k, v in vals.items() if v is not None and math.isfinite(v)]
    if not ks:
        return {}
    arr = np.array([vals[k] for k in ks], dtype=float)
    if not higher_is_risk:
        arr = -arr
    order = arr.argsort().argsort()
    n = len(ks)
    return {k: float((order[i] + 0.5) / n) for i, k in enumerate(ks)} if n > 1 else {ks[0]: 0.5}


def build_country_risk(settings: Settings, as_of: date) -> pl.DataFrame:
    comp: dict[str, dict[str, Any]] = {}

    def put(cc: str, k: str, v: Any) -> None:
        if cc and len(cc) == 2 and cc.isalpha():
            comp.setdefault(cc, {})[k] = v

    # events: trailing 7-day pressure (same definition as event_exposure)
    try:
        from app.events_models import common as EC  # noqa: PLC0415
        from app.events_models.exposure import country_pressure  # noqa: PLC0415

        t1 = EC.load_t1("country", settings=settings)
        if not t1.is_empty():
            t1 = t1.filter(pl.col("date") <= as_of)
            if not t1.is_empty():
                press, last = country_pressure(t1, 7)
                for cc, v in press.items():
                    put(cc, "event_pressure", float(v))
                    put(cc, "event_date", last)
            del t1
    except Exception as e:  # noqa: BLE001 - events lake mid-backfill / absent
        log.info("country_risk: no event pressure (%s)", e)
    esc = _read("event_escalation", settings, columns=["as_of", "iso2", "theme_group", "p_escalation_30d"],
                filters=(pl.col("theme_group") == "all") & (pl.col("as_of") <= as_of))
    if not esc.is_empty():
        for r in _latest_by(esc, "iso2", "as_of").iter_rows(named=True):
            put(r["iso2"], "p_escalation_30d", r["p_escalation_30d"])
    hs = _read("event_hotspots", settings, columns=["week", "theme", "measure", "iso2", "hotspot", "emerging_class"],
               filters=(pl.col("theme") == "all") & (pl.col("measure") == "intensity") & (pl.col("week") <= as_of))
    if not hs.is_empty():
        wk = hs["week"].max()
        for r in hs.filter(pl.col("week") == wk).iter_rows(named=True):
            put(r["iso2"], "hotspot", r["hotspot"])
            put(r["iso2"], "emerging_class", r["emerging_class"])
    gpr = _read("gpr_index", settings, columns=["iso2", "date", "gpr_ma7", "gpr"], filters=pl.col("date") <= as_of)
    if not gpr.is_empty():
        for r in _latest_by(gpr.filter(pl.col("date") >= as_of - timedelta(days=45)), "iso2", "date").iter_rows(named=True):
            v = r["gpr_ma7"] if r["gpr_ma7"] is not None else r["gpr"]
            if v is not None:
                put(r["iso2"], "gpr", float(v))
    rec = _read("macro_recession", settings, columns=["method", "country", "date", "probability", "method_label", "official"],
                filters=pl.col("probability").is_not_null() & (pl.col("date") <= as_of)
                & (pl.col("date") >= as_of - timedelta(days=400)))
    rec_by: dict[str, tuple[float, str, bool]] = {}
    if not rec.is_empty():
        lr = rec.sort("date").group_by("method", "country").agg(pl.all().last())
        for m in reversed(RECESSION_PREF):          # preferred method written last wins
            for r in lr.filter(pl.col("method") == m).iter_rows(named=True):
                rec_by[r["country"]] = (float(r["probability"]), r["method_label"], bool(r["official"]))
    for cc, (p, lab, off) in rec_by.items():
        if cc == "EA":
            continue
        put(cc, "recession_prob", p)
        put(cc, "recession_method", lab)
        put(cc, "recession_official", off)
    if "EA" in rec_by:
        for cc in EA_MEMBERS:
            if "recession_prob" not in comp.get(cc, {}):
                p, lab, off = rec_by["EA"]
                put(cc, "recession_prob", p)
                put(cc, "recession_method", f"{lab} (euro area)")
                put(cc, "recession_official", off)
    sup = _read("macro_surprise_index", settings, columns=["country", "theme", "date", "value"],
                filters=(pl.col("theme") == "all") & (pl.col("date") <= as_of) & (pl.col("date") >= as_of - timedelta(days=120)))
    if not sup.is_empty():
        ls = _latest_by(sup, "country", "date")
        sv = {r["country"]: r["value"] for r in ls.iter_rows(named=True) if r["value"] is not None}
        for cc, v in sv.items():
            if cc != "EA":
                put(cc, "surprise_index", float(v))
        if "EA" in sv:
            for cc in EA_MEMBERS:
                if "surprise_index" not in comp.get(cc, {}):
                    put(cc, "surprise_index", float(sv["EA"]))
    cyc = _read("macro_cycle", settings, columns=["country", "date", "quadrant"],
                filters=(pl.col("date") <= as_of) & (pl.col("date") >= as_of - timedelta(days=200)))
    if not cyc.is_empty():
        for r in _latest_by(cyc.filter(pl.col("quadrant").is_not_null()), "country", "date").iter_rows(named=True):
            put(r["country"], "cycle_quadrant", r["quadrant"])
    if not comp:
        return pl.DataFrame()
    ranks = {
        "events": _pct_rank({c: v.get("event_pressure") for c, v in comp.items()}),
        "escalation": _pct_rank({c: v.get("p_escalation_30d") for c, v in comp.items()}),
        "recession": {c: float(v["recession_prob"]) for c, v in comp.items() if v.get("recession_prob") is not None},
        "surprise": _pct_rank({c: v.get("surprise_index") for c, v in comp.items()}, higher_is_risk=False),
        "gpr": _pct_rank({c: v.get("gpr") for c, v in comp.items()}),
    }
    rows = []
    now = _now()
    for cc, v in sorted(comp.items()):
        used = {k: ranks[k][cc] for k in RISK_WEIGHTS if cc in ranks[k]}
        wsum = sum(RISK_WEIGHTS[k] for k in used)
        raw = 100.0 * sum(RISK_WEIGHTS[k] * x for k, x in used.items()) / wsum if wsum > 0 else None
        # few components -> shrink toward the neutral 50 by the covered weight (one noisy event series cannot make a
        # country look riskiest)
        score = 50.0 + (raw - 50.0) * wsum / sum(RISK_WEIGHTS.values()) if raw is not None else None
        rows.append({"as_of": as_of, "iso2": cc, "event_pressure": v.get("event_pressure"), "event_date": v.get("event_date"),
                     "p_escalation_30d": v.get("p_escalation_30d"), "hotspot": v.get("hotspot"),
                     "emerging_class": v.get("emerging_class"), "gpr": v.get("gpr"),
                     "recession_prob": v.get("recession_prob"), "recession_method": v.get("recession_method"),
                     "recession_official": v.get("recession_official"), "surprise_index": v.get("surprise_index"),
                     "cycle_quadrant": v.get("cycle_quadrant"), "risk_score": score, "n_components": len(used),
                     "components": json.dumps({k: round(x, 4) for k, x in used.items()}) if used else None,
                     "computed_at": now})
    return pl.DataFrame(rows, infer_schema_length=None)


# ================================================================================================ exposure + metrics
def exposures(ref: pl.DataFrame, edges: pl.DataFrame, geo: dict[str, dict[str, float]],
              as_of: date) -> tuple[pl.DataFrame, dict[str, dict[str, Any]]]:
    """sc_exposure rows + per-issuer helper dict (direct country weights, source, disclosed share, n_unweighted)."""
    home = {r["issuer_id"]: r["country"] for r in ref.iter_rows(named=True)} if not ref.is_empty() else {}
    sector = {r["issuer_id"]: r["sector_id"] for r in ref.iter_rows(named=True)} if not ref.is_empty() else {}
    names = dict(ref.select("issuer_id", "display").iter_rows()) if not ref.is_empty() else {}

    def direct(iid: str) -> tuple[dict[str, float], str]:
        if iid in geo:
            return geo[iid], "geo_revenue"
        c = home.get(iid)
        return ({c: 1.0}, "home_country") if c else ({}, "none")

    cust: dict[str, list[tuple[str, float]]] = {}
    unweighted: dict[str, int] = {}
    if not edges.is_empty():
        live = edges.filter(~pl.col("stale"))
        for r in live.filter(pl.col("supplier_issuer_id").is_not_null()).iter_rows(named=True):
            s = r["share_of_supplier_revenue"]
            if s is None or s <= 0 or r["customer_issuer_id"] is None:
                unweighted[r["supplier_issuer_id"]] = unweighted.get(r["supplier_issuer_id"], 0) + 1
                continue
            cust.setdefault(r["supplier_issuer_id"], []).append((r["customer_issuer_id"], float(s)))
    issuers = set(ref.filter(pl.col("in_universe"))["issuer_id"].to_list()) if not ref.is_empty() else set()
    issuers |= set(geo) | set(cust)
    now = _now()
    rows: list[dict[str, Any]] = []
    info: dict[str, dict[str, Any]] = {}
    for iid in sorted(issuers):
        d, src = direct(iid)
        cs = cust.get(iid, [])
        tot = sum(s for _, s in cs)
        scale = 1.0 / tot if tot > 1.0 else 1.0
        S = min(tot, 1.0)
        second: dict[str, float] = {}
        via: dict[str, list[dict[str, Any]]] = {}
        sec2: dict[str, float] = {}
        sec_via: dict[str, list[dict[str, Any]]] = {}
        for cid, s in cs:
            s2 = s * scale
            dc, _ = direct(cid)
            for k, w in dc.items():
                second[k] = second.get(k, 0.0) + s2 * w
                via.setdefault(k, []).append({"key": cid, "name": names.get(cid), "share": round(s2, 4), "weight": round(w, 4)})
            sc = sector.get(cid)
            if sc:
                sec2[sc] = sec2.get(sc, 0.0) + s2
                sec_via.setdefault(sc, []).append({"key": cid, "name": names.get(cid), "share": round(s2, 4), "weight": 1.0})
        keys = set(d) | set(second)
        for k in keys:
            dv = d.get(k, 0.0)
            so = second.get(k, 0.0)
            v = sorted(via.get(k, []), key=lambda x: -x["share"] * x["weight"])[:5]
            rows.append({"as_of": as_of, "issuer_id": iid, "dim": "country", "key": k, "direct": dv if src != "none" else None,
                         "second_order": so if cs else None, "look_through": (1 - S) * dv + so,
                         "n_paths": len(via.get(k, [])), "via": json.dumps(v) if v else None,
                         "source": "geo_revenue" if src == "geo_revenue" else "home_country", "computed_at": now})
        own = sector.get(iid)
        for k in set(sec2) | ({own} if own else set()):
            dv = 1.0 if k == own else 0.0
            so = sec2.get(k, 0.0)
            v = sorted(sec_via.get(k, []), key=lambda x: -x["share"])[:5]
            rows.append({"as_of": as_of, "issuer_id": iid, "dim": "sector", "key": k, "direct": dv,
                         "second_order": so if cs else None, "look_through": (1 - S) * dv + so,
                         "n_paths": len(sec_via.get(k, [])), "via": json.dumps(v) if v else None,
                         "source": "sector_taxonomy", "computed_at": now})
        info[iid] = {"direct": d, "src": src, "S": S if cs else None, "n_unweighted": unweighted.get(iid, 0),
                     "look": {k: (1 - S) * d.get(k, 0.0) + second.get(k, 0.0) for k in keys}}
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if not df.is_empty():
        df = df.with_columns(pl.col("n_paths").cast(pl.Int16))
    return df, info


def centrality(edges: pl.DataFrame) -> dict[str, dict[str, float]]:
    """node -> {pagerank (x N), betweenness, degree} on the supplier -> customer graph."""
    if edges.is_empty():
        return {}
    import networkx as nx  # noqa: PLC0415

    G = nx.DiGraph()
    for a, b, c in edges.filter(~pl.col("stale")).select("supplier_key", "customer_key", "confidence").iter_rows():
        G.add_edge(a, b, weight=float(c or 0.1))
    if G.number_of_nodes() == 0:
        return {}
    n = G.number_of_nodes()
    try:
        pr = nx.pagerank(G, alpha=0.85, weight="weight", max_iter=200)
    except Exception:  # noqa: BLE001 - convergence
        pr = {k: 1.0 / n for k in G.nodes}
    deg = nx.degree_centrality(G)
    res = [k for k in G.nodes if not str(k).startswith(("name:", "cik:"))]
    H = G.subgraph(res).to_undirected()
    bt: dict[str, float] = {}
    if H.number_of_nodes() > 2:
        k = min(500, H.number_of_nodes())
        bt = nx.betweenness_centrality(H, k=k if k < H.number_of_nodes() else None, normalized=True, seed=7)
    return {v: {"pagerank": pr.get(v, 0.0) * n, "betweenness": bt.get(v), "degree": deg.get(v, 0.0)} for v in G.nodes}


def metrics(ref: pl.DataFrame, edges: pl.DataFrame, info: dict[str, dict[str, Any]], geo_meta: dict[str, dict[str, Any]],
            risk: pl.DataFrame, neighbours: pl.DataFrame, as_of: date) -> pl.DataFrame:
    cen = centrality(edges)
    rk = {r["iso2"]: r for r in risk.iter_rows(named=True)} if not risk.is_empty() else {}
    nb = dict(neighbours.filter(pl.col("method") == "partial_corr").group_by("issuer_id").len().iter_rows()) \
        if not neighbours.is_empty() else {}
    live = edges.filter(~pl.col("stale")) if not edges.is_empty() else edges
    by_sup: dict[str, list[dict[str, Any]]] = {}
    by_cus: dict[str, list[dict[str, Any]]] = {}
    if not live.is_empty():
        for r in live.iter_rows(named=True):
            if r["supplier_issuer_id"]:
                by_sup.setdefault(r["supplier_issuer_id"], []).append(r)
            if r["customer_issuer_id"]:
                by_cus.setdefault(r["customer_issuer_id"], []).append(r)
    refd = {r["issuer_id"]: r for r in ref.iter_rows(named=True)} if not ref.is_empty() else {}
    ids = set(info) | set(by_sup) | set(by_cus)
    now = _now()
    rows = []

    def wavg(w: dict[str, float], f: str) -> float | None:
        num = den = 0.0
        for c, x in w.items():
            v = (rk.get(c) or {}).get(f)
            if v is not None and x > 0:
                num += x * float(v)
                den += x
        return num / den if den > 0 else None

    for iid in sorted(ids):
        r = refd.get(iid, {})
        cs = by_sup.get(iid, [])
        ss = by_cus.get(iid, [])
        shares = [(e["customer_key"], e["customer_name"], e["share_of_supplier_revenue"]) for e in cs
                  if e["share_of_supplier_revenue"] is not None]
        top = max(shares, key=lambda t: t[2]) if shares else None
        inf = info.get(iid, {"direct": {}, "src": "none", "S": None, "n_unweighted": 0, "look": {}})
        d = inf["direct"]
        hhi = sum(v * v for v in d.values()) if d else None
        topc = max(d.items(), key=lambda kv: kv[1]) if d else None
        gm = geo_meta.get(iid, {})
        c = cen.get(iid, {})
        has_geo = inf["src"] == "geo_revenue"
        has_links = bool(cs or ss)
        rows.append({
            "as_of": as_of, "issuer_id": iid, "security_id": r.get("security_id"), "country": r.get("country"),
            "region": r.get("region"), "sector_id": r.get("sector_id"),
            "n_customers": len(cs), "n_customers_resolved": sum(1 for e in cs if e["customer_issuer_id"]),
            "n_suppliers": len(ss), "n_suppliers_resolved": sum(1 for e in ss if e["supplier_issuer_id"]),
            "n_unweighted": int(inf.get("n_unweighted") or 0),
            "disclosed_customer_share": min(1.0, sum(t[2] for t in shares)) if shares else None,
            "top_customer_share": top[2] if top else None, "top_customer_key": top[0] if top else None,
            "top_customer_name": top[1] if top else None,
            "customer_hhi": sum(t[2] ** 2 for t in shares) if shares else None,
            "pagerank": c.get("pagerank"), "betweenness": c.get("betweenness"), "degree_centrality": c.get("degree"),
            "n_neighbours_stat": int(nb.get(iid, 0)), "geo_source": inf["src"],
            "geo_period_end": gm.get("period_end"), "geo_knowledge_date": gm.get("knowledge_date"),
            "geo_source_id": gm.get("source_id"), "geo_method": gm.get("method") or ("xbrl_dimensional" if gm else None),
            "geo_confidence": gm.get("confidence") if gm.get("confidence") is not None else (1.0 if gm else None),
            "n_countries": len(d), "geo_hhi": hhi, "top_country": topc[0] if topc else None,
            "top_country_share": topc[1] if topc else None,
            "domestic_share": d.get(r.get("country")) if d and r.get("country") else None,
            "country_risk": wavg(d, "risk_score"), "country_risk_look_through": wavg(inf.get("look") or {}, "risk_score"),
            "event_pressure": wavg(d, "event_pressure"), "recession_prob": wavg(d, "recession_prob"),
            "coverage_level": "full" if has_geo and has_links else "partial" if has_geo or has_links else "sparse",
            "computed_at": now})
    df = pl.DataFrame(rows, infer_schema_length=None)
    for c in ("n_customers", "n_customers_resolved", "n_suppliers", "n_suppliers_resolved", "n_unweighted"):
        df = df.with_columns(pl.col(c).cast(pl.Int32))
    return df.with_columns(pl.col("n_neighbours_stat").cast(pl.Int16), pl.col("n_countries").cast(pl.Int16))


# ================================================================================================ job
def run_network(settings: Settings | None = None, as_of: date | None = None, universe: str = "full") -> dict[str, Any]:
    from app.core.schema import conform  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    ref = issuer_ref(s, universe)
    links = _read("company_links", s, filters=pl.col("link_type").is_in(["customer", "supplier"]))
    edges = build_edges(links, ref, as_of)
    del links
    geo, geo_meta = direct_country_weights(s, as_of)
    risk = build_country_risk(s, as_of)
    exp, info = exposures(ref, edges, geo, as_of)
    nb = _read("company_neighbours", s, columns=["issuer_id", "method"])
    met = metrics(ref, edges, info, geo_meta, risk, nb, as_of)
    rep: dict[str, Any] = {"as_of": str(as_of)}
    now = _now()

    def w(df: pl.DataFrame, table: str, mode: str = "overwrite") -> int:
        if df.is_empty():
            return 0
        if "computed_at" not in df.columns:
            df = df.with_columns(pl.lit(now).alias("computed_at"))
        return write_table(conform(df, table), table, mode=mode, settings=s)

    rep["sc_edges"] = w(edges, "sc_edges")
    rep["sc_exposure"] = w(exp, "sc_exposure")
    rep["sc_metrics"] = w(met, "sc_metrics")
    if not risk.is_empty():
        old = _read("country_risk", s)
        keep = old.filter((pl.col("as_of") != as_of) & (pl.col("as_of") >= as_of - timedelta(days=400))) \
            if not old.is_empty() else old
        allr = pl.concat([keep.select(risk.columns), risk], how="vertical_relaxed") if not keep.is_empty() else risk
        rep["country_risk"] = w(allr, "country_risk", "overwrite")
    rep.update({"edges_resolved_both": int(edges.filter(pl.col("supplier_issuer_id").is_not_null()
                                                        & pl.col("customer_issuer_id").is_not_null()).height)
                if not edges.is_empty() else 0,
                "issuers_geo_revenue": len(geo), "seconds": round(time.perf_counter() - t0, 1)})
    if not met.is_empty():
        rep["coverage"] = dict(met.group_by("coverage_level").len().iter_rows())
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_network(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else None,
                      universe=str(p.get("universe", "full")))
    rows = {k: v for k, v in rep.items() if k in ("sc_edges", "sc_exposure", "sc_metrics", "country_risk")}
    return JobResult(status="success" if rep.get("sc_metrics") else "partial", rows_written=rows, message=str(rep))
