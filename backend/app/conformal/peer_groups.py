"""Application B: peer-group joint ellipsoids (docs/CONFORMAL_TECHNICALS_PLAN.md §2-3; the paper's sweet spot,
moderate N with a meaningful graph). MODEL-BASED.

Groups (build_groups, versioned in conformal_peer_groups): single listing-region (one trading calendar), N = 20-50,
one security per issuer, members need >= 3 years of prints and a recent print; ranked by 60-session median USD
dollar volume.
  industry      subsector x region: the 50 most liquid names (subsectors with >= 20 eligible names)
  supply_chain  hub (mvp names with disclosed links / statistical neighbours) + disclosed customers / suppliers
                (sc_edges) + statistical neighbours (company_neighbours partial_corr) + their neighbours, 20-30 names;
                groups overlapping an existing one by Jaccard > 0.5 are dropped
Group graph for the GCN-GRU: disclosed supply-chain edges known at the fit date (w = 1), same-subsector ties
(w = 0.3, supply-chain groups) and the top-5 partial correlations within the group computed on the PRETRAIN block
only (w = rho; point-in-time - company_neighbours is current-only and is used for membership, not for the graph).

Y_t = members' one-day log returns. Filters (row t = law of day t+1 from returns <= t): ewma (dense; vol lambda
0.94, correlation lambda 0.985, correlation shrinkage 0.1), ewma_lr (same, projected to diag + rank 4), factor (PCA
rank 4, static), garch_dcc, gcn_gru (shared model, app.conformal.learned). Scores: squared Mahalanobis of the day's
returns vs the law emitted at the previous close; per-group rolling split-conformal radius (window 500 sessions,
re-sorted every 5) with optional ACI (gamma 0.005); score_pct = calibrated percentile among the window's scores.
Attribution: R = sum_i r_i (Sigma^-1 r)_i (exact) plus marginal and conditional standardised residuals.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.conformal import data as D
from app.conformal.filters import EWMAFilter, FactorFilter, GarchFilter, make_filter
from app.conformal.protocol import Calibrator
from app.conformal.scores import Cov, decompose, mahalanobis, to_lowrank
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.conformal.peer_groups")

N_MIN, N_MAX, N_SC = 20, 50, 30
WINDOW = 500
RECAL = 5
GAMMA = 0.005
ALPHAS = (0.2, 0.1, 0.05)
MIN_HISTORY_BARS = 750
TOPK_GRAPH = 5
# production filter (P4, docs/CONFORMAL_VALIDATION.md): the shared GCN-GRU (sharpest at-target ellipsoid, -10% w_proj
# vs EWMA) with the ACI radius; falls back to ewma_lr when no trained model exists yet
PROD_FILTER = "gcn_gru"
FALLBACK_FILTER = "ewma_lr"
RADIUS_MODE = "aci"
VERSION_MAX_AGE_DAYS = 90


def model_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "models" / "conformal"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------------------------------------ group building


def eligible_securities(as_of: date, settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, issuer_id, region, sector_id, subsector_id, adv, bars, is_mvp] for the full universe."""
    s = settings or get_settings()
    con = D.lake(["prices_daily", "universe_membership", "securities_master", "issuers"], s)
    try:
        df = con.execute("""
            WITH u AS (SELECT security_id, max(CASE WHEN universe_id IN ('mvp_us', 'mvp_global') THEN 1 ELSE 0 END) AS is_mvp
                       FROM universe_membership WHERE valid_to IS NULL GROUP BY 1),
            px AS (
              SELECT security_id, any_value(region) AS region,
                     count(*) FILTER (WHERE date > ? - INTERVAL 1260 DAY) AS bars,
                     median(close * usd_per_unit * volume) FILTER (WHERE date > ? - INTERVAL 90 DAY) AS adv,
                     max(date) AS last
              FROM prices_daily WHERE date > ? - INTERVAL 1260 DAY AND date <= ? AND region <> 'FX'
              GROUP BY 1)
            SELECT px.security_id, s.issuer_id, px.region, coalesce(i.sector_id, 'NA') AS sector_id,
                   coalesce(i.subsector_id, 'NA') AS subsector_id, px.adv, px.bars, u.is_mvp,
                   coalesce(s.is_primary_security, false) AS is_primary, coalesce(s.display_ticker, s.name, s.security_id) AS ticker
            FROM px JOIN u USING (security_id) JOIN securities_master s ON s.security_id = px.security_id
            LEFT JOIN issuers i ON i.issuer_id = s.issuer_id
            WHERE px.last >= ? - INTERVAL 10 DAY AND s.issuer_id IS NOT NULL
              AND coalesce(s.security_type, 'equity') NOT IN ('fx', 'future', 'index', 'etf')""",
                          [as_of, as_of, as_of, as_of, as_of]).pl()
    finally:
        con.close()
    df = df.filter((pl.col("bars") >= MIN_HISTORY_BARS) & pl.col("adv").is_not_null() & (pl.col("adv") > 0))
    # one security per issuer: primary first, then the most liquid
    return df.sort(["is_primary", "adv"], descending=True).unique("issuer_id", keep="first")


def _links(as_of: date, settings: Settings) -> tuple[pl.DataFrame, pl.DataFrame]:
    con = D.lake(["sc_edges", "company_neighbours"], settings)
    try:
        sc = con.execute("""SELECT DISTINCT supplier_issuer_id AS a, customer_issuer_id AS b, coalesce(first_knowledge_date, knowledge_date) AS kd
                            FROM sc_edges WHERE supplier_issuer_id IS NOT NULL AND customer_issuer_id IS NOT NULL
                              AND NOT stale AND knowledge_date <= ?""", [as_of]).pl()
        nb = con.execute("""SELECT issuer_id AS a, neighbour_issuer_id AS b, rho FROM company_neighbours
                            WHERE method = 'partial_corr' AND same_region""").pl()
    finally:
        con.close()
    return sc, nb


def build_groups(as_of: date, settings: Settings | None = None, max_sc: int = 100) -> pl.DataFrame:
    s = settings or get_settings()
    el = eligible_securities(as_of, s)
    sc, nb = _links(as_of, s)
    version = as_of.strftime("%Y%m%d")
    rows: list[dict[str, Any]] = []
    groups: list[tuple[str, str, str, str, list[str]]] = []      # (gid, kind, region, label, issuer ids)
    # industry
    for (rg, sub), g in el.filter(pl.col("subsector_id") != "NA").group_by(["region", "subsector_id"]):
        if g.height < N_MIN:
            continue
        top = g.sort("adv", descending=True).head(N_MAX)
        groups.append((f"ind:{rg}:{sub}", "industry", rg, f"{sub.replace('_', ' ')} ({rg})", top["issuer_id"].to_list()))
    # supply chain / statistical ego groups around mvp hubs
    iss = el.select("issuer_id", "region", "adv", "is_mvp", "subsector_id")
    reg_of = dict(zip(iss["issuer_id"].to_list(), iss["region"].to_list(), strict=True))
    adj: dict[str, dict[str, float]] = defaultdict(dict)
    for a, b, _ in sc.iter_rows():
        if a in reg_of and b in reg_of and reg_of[a] == reg_of[b]:
            adj[a][b] = adj[b][a] = 1.0
    stat: dict[str, dict[str, float]] = defaultdict(dict)
    for a, b, rho in nb.iter_rows():
        if a in reg_of and b in reg_of and reg_of[a] == reg_of[b]:
            stat[a][b] = max(stat[a].get(b, 0.0), float(rho))
            stat[b][a] = max(stat[b].get(a, 0.0), float(rho))
    hubs = iss.filter(pl.col("is_mvp") == 1).with_columns(
        pl.col("issuer_id").map_elements(lambda x: 3 * len(adj.get(x, {})) + len(stat.get(x, {})), return_dtype=pl.Int64).alias("deg"))
    hubs = hubs.filter(pl.col("deg") >= 8).sort(["deg", "adv"], descending=True)
    adv = dict(zip(iss["issuer_id"].to_list(), iss["adv"].to_list(), strict=True))
    sc_sets: list[set[str]] = [set(x[4]) for x in groups]
    n_sc = 0
    for hub, rg in hubs.select("issuer_id", "region").iter_rows():
        if n_sc >= max_sc:
            break
        mem = [hub]
        seen = {hub}
        frontier = sorted(adj.get(hub, {}), key=lambda x: -adv.get(x, 0)) + \
            sorted(stat.get(hub, {}), key=lambda x: -stat[hub][x])
        q = list(frontier)
        while q and len(mem) < N_SC:
            x = q.pop(0)
            if x in seen or reg_of.get(x) != rg:
                continue
            seen.add(x)
            mem.append(x)
            if len(q) < N_SC:
                q += sorted(adj.get(x, {}), key=lambda y: -adv.get(y, 0)) + sorted(stat.get(x, {}), key=lambda y: -stat[x][y])
        if len(mem) < N_MIN:
            continue
        sm = set(mem)
        if any(len(sm & o) / len(sm | o) > 0.5 for o in sc_sets):
            continue
        sc_sets.append(sm)
        groups.append((f"sc:{rg}:{hub}", "supply_chain", rg, f"links around {hub} ({rg})", mem))
        n_sc += 1
    el_i = {r["issuer_id"]: r for r in el.iter_rows(named=True)}
    now = datetime.now(UTC)
    for gid, kind, rg, label, mem in groups:
        if kind == "supply_chain":
            label = f"links around {el_i[mem[0]]['ticker']} ({rg})"
        mem = sorted(mem, key=lambda x: -adv.get(x, 0))
        sids = [el_i[x]["security_id"] for x in mem]
        for rank, (x, sid) in enumerate(zip(mem, sids, strict=True), 1):
            nbr = []
            for y, sy in zip(mem, sids, strict=True):
                if y == x:
                    continue
                w = max(adj.get(x, {}).get(y, 0.0), stat.get(x, {}).get(y, 0.0) if stat.get(x, {}).get(y, 0.0) >= 0.3 else 0.0,
                        0.3 if kind == "supply_chain" and el_i[x]["subsector_id"] == el_i[y]["subsector_id"] != "NA" else 0.0)
                if w > 0:
                    nbr.append((sy, round(w, 4)))
            r = el_i[x]
            rows.append({"version": version, "group_id": gid, "security_id": sid, "issuer_id": x, "region_bloc": rg,
                         "group_kind": kind, "label": label, "sector_id": r["sector_id"], "subsector_id": r["subsector_id"],
                         "member_rank": rank, "n_members": len(mem), "built_as_of": as_of,
                         "nbr_ids": [a for a, _ in nbr], "nbr_w": [b for _, b in nbr], "computed_at": now})
    return pl.DataFrame(rows, infer_schema_length=None)


def load_groups(settings: Settings | None = None, version: str | None = None) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    df = scan_table("conformal_peer_groups", settings).collect()
    if df.is_empty():
        return df
    v = version or df["version"].max()
    return df.filter(pl.col("version") == v)


# ------------------------------------------------------------------------------------------------ graph (PIT)


def sc_adjacency(sids: list[str], issuers: list[str], sc: pl.DataFrame | None, known_by: date | None) -> np.ndarray:
    N = len(sids)
    A = np.zeros((N, N))
    if sc is None or sc.is_empty():
        return A
    ix = {x: i for i, x in enumerate(issuers)}
    f = sc if known_by is None else sc.filter(pl.col("kd") <= known_by)
    for a, b, _ in f.iter_rows():
        if a in ix and b in ix:
            A[ix[a], ix[b]] = A[ix[b], ix[a]] = 1.0
    return A


def stat_adjacency(X: np.ndarray, k: int = TOPK_GRAPH, min_rho: float = 0.1) -> np.ndarray:
    """Top-k partial correlations (residuals on the group's equal-weight mean) on the given (pretrain) block."""
    ok = np.isfinite(X).all(axis=1)
    Z = X[ok]
    N = X.shape[1]
    A = np.zeros((N, N))
    if Z.shape[0] < 60:
        return A
    m = Z.mean(1, keepdims=True)
    beta = (Z * m).sum(0) / max((m * m).sum(), 1e-300)
    E = Z - m * beta
    with np.errstate(all="ignore"):
        C = np.nan_to_num(np.corrcoef(E.T), nan=0.0)
    np.fill_diagonal(C, -np.inf)
    for i in range(N):
        for j in np.argsort(-C[i])[:k]:
            if C[i, j] >= min_rho:
                A[i, j] = A[j, i] = max(A[i, j], float(C[i, j]))
    return A


def group_graph(X_pre: np.ndarray, sids: list[str], issuers: list[str], subsectors: list[str], kind: str,
                sc: pl.DataFrame | None, known_by: date | None) -> np.ndarray:
    A = np.maximum(stat_adjacency(X_pre), sc_adjacency(sids, issuers, sc, known_by))
    if kind == "supply_chain":
        same = np.equal.outer(np.array(subsectors), np.array(subsectors)) & (np.array(subsectors)[:, None] != "NA")
        A = np.maximum(A, 0.3 * same)
    np.fill_diagonal(A, 0.0)
    return A


# ------------------------------------------------------------------------------------------------ filters


def make_group_filter(kind: str, A: np.ndarray | None = None, gcn_model: Any = None, scale: np.ndarray | None = None):  # noqa: ANN201
    if kind == "ewma":
        return EWMAFilter(lam=0.94, lam_corr=0.985, shrink=0.1)
    if kind == "ewma_lr":
        return EWMAFilter(lam=0.94, lam_corr=0.985, shrink=0.1, rank=4, name="ewma_lr")
    if kind == "factor":
        return FactorFilter(r=4)
    if kind == "factor_vs":
        return FactorFilter(r=4, vol_scale=True, name="factor_vs")
    if kind == "garch_dcc":
        return GarchFilter(corr="dcc", lam_corr=0.985, shrink=0.1)
    if kind == "static_lw":
        return make_filter("static_lw")
    if kind == "gcn_gru":
        from app.conformal.learned import GCNGRUFilter  # noqa: PLC0415

        f = GCNGRUFilter(A=A)
        f.model, f.scale = gcn_model, scale
        return f
    raise ValueError(kind)


@dataclass
class GroupRun:
    """Filter emission + scores for one group over a date range."""
    dates: np.ndarray
    sids: list[str]
    X: np.ndarray                  # (T, N) log returns, NaN rows dropped already for the scored range
    mu: np.ndarray
    cov: Cov
    R: np.ndarray                  # (T,) R[t] = score of X[t+1] vs law at t
    meta: dict[str, Any] = field(default_factory=dict)


def complete_rows(X: np.ndarray, max_missing_share: float = 0.02, max_zero_share: float = 0.2) -> tuple[np.ndarray, np.ndarray]:
    """Keep members with <= max_missing_share missing rows and <= max_zero_share exactly-zero returns (stale /
    illiquid prints make Sigma singular); then keep rows where all kept members printed."""
    miss = ~np.isfinite(X)
    zero = (X == 0.0)
    keep_c = (miss.mean(0) <= max_missing_share) & (zero.mean(0) <= max_zero_share)
    Xc = X[:, keep_c]
    keep_r = np.isfinite(Xc).all(1)
    return keep_c, keep_r


def run_group(X: np.ndarray, kind: str, fit_rows: slice, A: np.ndarray | None = None, gcn_model: Any = None,
              scale: np.ndarray | None = None) -> tuple[np.ndarray, Cov, np.ndarray]:
    """Fit on X[fit_rows] (frozen) and emit over all rows. Returns mu, cov, R (R[t] pairs row t with X[t+1])."""
    f = make_group_filter(kind, A, gcn_model, scale)
    if kind != "gcn_gru":
        f.fit(X[fit_rows])
    mu, cov = f.emit(X)
    T = X.shape[0]
    R = np.full(T, np.nan)
    R[:-1] = mahalanobis(X[1:] - mu[:-1], cov.take(slice(0, T - 1)))
    return mu, cov, R


# ------------------------------------------------------------------------------------------------ nightly scoring


def _calibrate(R: np.ndarray, start: int, t_from: int, alphas: tuple[float, ...] = ALPHAS, gamma: float = GAMMA,
               ) -> dict[str, np.ndarray]:
    """Radii for decision times t in [t_from, T): plain and ACI (per alpha), n_cal, and the percentile helper."""
    T = len(R)
    cal = Calibrator(R, start=start, window=WINDOW, lag=0, recal_every=RECAL, min_n=100)
    q = np.full((T, len(alphas)), np.nan)
    qa = np.full((T, len(alphas)), np.nan)
    at = np.full((T, len(alphas)), np.nan)
    lev = np.array(alphas, dtype=np.float64)
    for t in range(t_from, T):
        s = cal.sorted_at(t)
        if len(s) >= cal.min_n:
            from app.conformal.scores import quantile_from_sorted  # noqa: PLC0415

            q[t] = quantile_from_sorted(s, np.array(alphas, dtype=np.float64))
            qa[t] = [float(quantile_from_sorted(s, a)) if a > 0 else np.inf for a in lev]
        at[t] = lev
        # feedback: R[t] (law at t vs X[t+1]) is realised at t + 1 -> next level
        if np.isfinite(R[t]) and np.isfinite(qa[t]).all():
            lev = lev + gamma * (np.array(alphas) - (R[t] > qa[t]).astype(float))
    return {"q": q, "q_aci": qa, "alpha_t": at, "cal": cal}


def score_groups(as_of: date, settings: Settings | None = None, filter_kind: str = PROD_FILTER, emit_days: int = 5,
                 radius_mode: str = RADIUS_MODE, history_days: int = 1700) -> dict[str, Any]:
    """Score every group of the current definition version for the last `emit_days` sessions."""
    s = settings or get_settings()
    t0 = time.perf_counter()
    grp = load_groups(s)
    if grp.is_empty():
        return {"rows": 0, "note": "no group definitions (run conformal_peer_groups_fit)"}
    version = grp["version"][0]
    gcn = _load_gcn(s) if filter_kind == "gcn_gru" else None
    if filter_kind == "gcn_gru" and gcn is None:
        log.warning("no trained GCN-GRU (run conformal_peer_groups_fit): falling back to %s", FALLBACK_FILTER)
        filter_kind = FALLBACK_FILTER
    sc, _ = _links(as_of, s)
    out_g, out_m = [], []
    now = datetime.now(UTC)
    for rg, gg in grp.group_by("region_bloc"):
        rg = rg[0]
        ids = gg["security_id"].unique().to_list()
        pan = D.load_panel(rg, as_of - timedelta(days=history_days), as_of, ids, s)
        if pan is None:
            continue
        Rall = D.log_returns(pan.C)
        col = pan.col()
        for gid, g in gg.group_by("group_id"):
            gid = gid[0]
            g = g.sort("member_rank")
            sids = [x for x in g["security_id"].to_list() if x in col]
            if len(sids) < N_MIN:
                continue
            X = Rall[:, [col[x] for x in sids]]
            keep_c, keep_r = complete_rows(X[-WINDOW - 300:])
            sids = [x for x, k in zip(sids, keep_c, strict=True) if k]
            if len(sids) < N_MIN:
                continue
            X = X[:, keep_c]
            rows_ok = np.isfinite(X).all(1)
            Xr, dts = X[rows_ok], pan.dates[rows_ok]
            T = len(Xr)
            if T < WINDOW + 150:
                continue
            fit = slice(0, T - WINDOW - 20)
            iss = g.filter(pl.col("security_id").is_in(sids)).sort("member_rank")
            A = group_graph(Xr[fit], sids, iss["issuer_id"].to_list(), iss["subsector_id"].to_list(),
                            g["group_kind"][0], sc, dts[fit.stop - 1].astype(object))
            scale = gcn["scales"](sids, Xr[fit]) if gcn else None
            mu, cov, R = run_group(Xr, filter_kind, fit, A, gcn["model"] if gcn else None, scale)
            cal = _calibrate(R, start=fit.stop + 50, t_from=max(fit.stop + 50, T - emit_days - 260))
            qsrc = cal["q_aci"] if radius_mode == "aci" else cal["q"]
            lr = cov if cov.lowrank else to_lowrank(cov.to_dense(), 4)
            for t in range(max(1, T - emit_days), T):
                tp = t - 1                                    # law emitted at the previous close
                r = Xr[t]
                sc_t = float(R[tp])
                cs = cal["cal"].sorted_at(tp)
                pct = float(np.searchsorted(cs, sc_t, side="right") / len(cs)) if len(cs) else None
                dec = decompose(r[None], cov.take([tp]))
                contrib = dec["contrib"][0]
                zc = dec["z_cond"][0]
                imax = int(np.argmax(contrib))
                ib = int(np.argmax(np.abs(zc)))
                d = dts[t].astype(object)
                q = cal["q"][tp]
                qa = cal["q_aci"][tp]
                qs = qsrc[tp]
                out_g.append({"as_of_date": d, "group_id": gid, "year": d.year, "version": version, "filter": filter_kind,
                              "radius_mode": radius_mode, "model_based": True, "region_bloc": rg, "n_members": len(sids),
                              "score": sc_t, "score_pct": pct, "q_80": q[0], "q_90": q[1], "q_95": q[2],
                              "q_aci_90": qa[1] if np.isfinite(qa[1]) else None, "aci_alpha_90": cal["alpha_t"][tp, 1],
                              "anomaly_90": bool(sc_t > qs[1]) if np.isfinite(qs[1]) else None,
                              "anomaly_95": bool(sc_t > qs[2]) if np.isfinite(qs[2]) else None,
                              "group_move": float(r.mean()), "top_member": sids[imax],
                              "top_share": float(contrib[imax] / sc_t) if sc_t > 0 else None,
                              "breakaway_member": sids[ib], "breakaway_z": float(zc[ib]),
                              "q_next_90": float(qsrc[t, 1]) if t < T and np.isfinite(qsrc[t, 1]) else None,
                              "q_next_95": float(qsrc[t, 2]) if t < T and np.isfinite(qsrc[t, 2]) else None,
                              "computed_at": now})
                for i, sid in enumerate(sids):
                    out_m.append({"as_of_date": d, "group_id": gid, "security_id": sid, "year": d.year,
                                  "r": float(r[i]), "z_marg": float(dec["z_marg"][0, i]), "z_cond": float(zc[i]),
                                  "contrib": float(contrib[i]), "contrib_share": float(contrib[i] / sc_t) if sc_t > 0 else None,
                                  "mu_next": float(mu[t, i]), "d_next": float(lr.d[t, i]),
                                  **{f"l{k + 1}_next": float(lr.L[t, i, k]) if lr.L.shape[-1] > k else 0.0 for k in range(4)},
                                  "computed_at": now})
        del pan, Rall
    rep = {"groups": len({r["group_id"] for r in out_g}), "rows": len(out_g), "member_rows": len(out_m),
           "version": version, "filter": filter_kind, "seconds": round(time.perf_counter() - t0, 1)}
    rep["_frames"] = (pl.DataFrame(out_g, infer_schema_length=None) if out_g else pl.DataFrame(),
                      pl.DataFrame(out_m, infer_schema_length=None) if out_m else pl.DataFrame())
    return rep


def _load_gcn(settings: Settings) -> dict[str, Any] | None:
    from app.conformal.learned import GCNGRUModel  # noqa: PLC0415

    p = model_dir(settings) / "gcn_gru_peer.npz"
    if not p.exists():
        return None
    m = GCNGRUModel.load(str(p))
    meta_p = p.with_suffix(".json")
    meta = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
    sc_map = meta.get("scales", {})
    pre_end = meta.get("pretrain_end")

    def scales(sids: list[str], X_pre: np.ndarray) -> np.ndarray:
        own = np.nanstd(X_pre, axis=0)
        return np.array([sc_map.get(x, own[i] if np.isfinite(own[i]) and own[i] > 0 else 0.02) for i, x in enumerate(sids)])

    return {"model": m, "scales": scales, "meta": meta, "pretrain_end": pre_end}


# ------------------------------------------------------------------------------------------------ jobs


def run_scoring(settings: Settings | None = None, as_of: date | None = None, emit_days: int = 5, write: bool = True,
                filter_kind: str = PROD_FILTER) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    rep = score_groups(as_of, s, filter_kind=filter_kind, emit_days=emit_days)
    g, m = rep.pop("_frames", (pl.DataFrame(), pl.DataFrame()))
    if write and g.height:
        rep["written"] = write_table(g, "conformal_peer_scores", mode="upsert", settings=s)
        write_table(m, "conformal_peer_members", mode="upsert", settings=s)
    rep["as_of"] = str(as_of)
    _state(s, last_peer_scoring=rep)
    return rep


def run_fit(settings: Settings | None = None, as_of: date | None = None, force_groups: bool = False,
            write: bool = True, force_gcn: bool = False) -> dict[str, Any]:
    """Weekly: (re)build group definitions when missing / forced / older than 90 days."""
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    t0 = time.perf_counter()
    cur = load_groups(s)
    rep: dict[str, Any] = {"as_of": str(as_of)}
    need = force_groups or cur.is_empty() or \
        (as_of - datetime.strptime(cur["version"][0], "%Y%m%d").date()).days > VERSION_MAX_AGE_DAYS
    if need:
        df = build_groups(as_of, s)
        rep["groups"] = df["group_id"].n_unique() if df.height else 0
        rep["members"] = df.height
        rep["by_kind"] = dict(df.unique("group_id").group_by("group_kind").len().iter_rows()) if df.height else {}
        if write and df.height:
            write_table(df, "conformal_peer_groups", mode="upsert", settings=s)
        rep["version"] = as_of.strftime("%Y%m%d")
    else:
        rep["version"] = cur["version"][0]
        rep["groups"] = cur["group_id"].n_unique()
        rep["note"] = "group definitions current"
    if PROD_FILTER == "gcn_gru" or force_gcn:
        rep["gcn"] = train_production_gcn(s, as_of, force=force_gcn, write=write)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    _state(s, last_peer_fit=rep)
    return rep


def train_production_gcn(settings: Settings, as_of: date, force: bool = False, write: bool = True, epochs: int = 20,
                         pretrain_years: int = 5, max_age_days: int = 90) -> dict[str, Any]:
    """Train the shared GCN-GRU on every group's PRETRAIN block, which ends `WINDOW + 20` sessions before the as-of
    date so that the whole rolling calibration window is scored by the frozen model (Algorithm 1). Skipped while
    the current model is younger than max_age_days."""
    from app.conformal.learned import GraphSeq, train_gcn_gru  # noqa: PLC0415

    f = model_dir(settings) / "gcn_gru_peer.npz"
    meta_f = f.with_suffix(".json")
    if f.exists() and meta_f.exists() and not force:
        m = json.loads(meta_f.read_text(encoding="utf-8"))
        if (as_of - date.fromisoformat(m["trained_as_of"])).days <= max_age_days:
            return {"note": "model current", "trained_as_of": m["trained_as_of"]}
    grp = load_groups(settings)
    sc, _ = _links(as_of, settings)
    seqs, scales = [], {}
    pre_end_dates = []
    for rg, gg in grp.group_by("region_bloc"):
        rg = rg[0]
        pan = D.load_panel(rg, as_of - timedelta(days=int(365.25 * (pretrain_years + 3))), as_of,
                           gg["security_id"].unique().to_list(), settings)
        if pan is None:
            continue
        Rall = D.log_returns(pan.C)
        col = pan.col()
        for _gid, g in gg.group_by("group_id"):
            g = g.sort("member_rank")
            sids = [x for x in g["security_id"].to_list() if x in col]
            X = Rall[:, [col[x] for x in sids]]
            end = len(X) - WINDOW - 20
            start = max(0, end - 252 * pretrain_years)
            keep_c, _ = complete_rows(X[start:end])
            sids = [x for x, k in zip(sids, keep_c, strict=True) if k]
            if len(sids) < N_MIN:
                continue
            Xp = X[start:end][:, keep_c]
            Xp = Xp[np.isfinite(Xp).all(1)]
            if len(Xp) < 500:
                continue
            sub = g.filter(pl.col("security_id").is_in(sids)).sort("member_rank")
            A = group_graph(Xp, sids, sub["issuer_id"].to_list(), sub["subsector_id"].to_list(), g["group_kind"][0],
                            sc, pan.dates[end - 1].astype(object))
            seqs.append(GraphSeq(X=Xp, A=A))
            for sid, v in zip(sids, np.nanstd(Xp, axis=0), strict=True):
                scales[sid] = float(v)
            pre_end_dates.append(str(pan.dates[end - 1]))
    if not seqs:
        return {"note": "no sequences"}
    m = train_gcn_gru(seqs, epochs=epochs, log=log.info, time_budget_s=40 * 60)
    if write:
        m.save(str(f))
        meta_f.write_text(json.dumps({"trained_as_of": str(as_of), "pretrain_end": max(pre_end_dates), "scales": scales,
                                      "meta": m.meta, "groups": len(seqs)}), encoding="utf-8")
    return {"groups": len(seqs), "pretrain_end": max(pre_end_dates), **{k: v for k, v in m.meta.items() if k != "loss"}}


def _state(settings: Settings, **kv: Any) -> None:
    from app.conformal.envelope_job import _state as st  # noqa: PLC0415

    st(settings, **kv)


def run(ctx: Any) -> Any:
    """Job conformal_peer_groups (nightly scoring)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_scoring(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.as_of,
                      emit_days=int(p.get("emit_days", 5)), filter_kind=str(p.get("filter", PROD_FILTER)))
    return JobResult(status="success" if rep.get("rows") else "partial",
                     rows_written={"conformal_peer_scores": rep.get("rows", 0), "conformal_peer_members": rep.get("member_rows", 0)},
                     watermark=rep.get("as_of"), message=json.dumps(rep, default=str)[:4000])


def run_fit_job(ctx: Any) -> Any:
    """Job conformal_peer_groups_fit (weekly)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_fit(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.as_of,
                  force_groups=bool(p.get("force_groups", False)))
    return JobResult(status="success", rows_written={"conformal_peer_groups": rep.get("members", 0)},
                     message=json.dumps(rep, default=str)[:4000])


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fit", "score"])
    ap.add_argument("--as-of")
    ap.add_argument("--emit-days", type=int, default=5)
    ap.add_argument("--force-groups", action="store_true")
    ap.add_argument("--filter", default=PROD_FILTER)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    d = date.fromisoformat(a.as_of) if a.as_of else None
    if a.cmd == "fit":
        print(json.dumps(run_fit(as_of=d, force_groups=a.force_groups, write=not a.dry_run), indent=1, default=str))
    else:
        print(json.dumps(run_scoring(as_of=d, emit_days=a.emit_days, write=not a.dry_run, filter_kind=a.filter), indent=1, default=str))
