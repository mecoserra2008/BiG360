"""Point-in-time feature construction for the meta-labelling model (shared by the history build and nightly scoring).

Every feature of an origin (security, date d, direction) uses information known at the close of d only:

  t2        v2 T2 calibrated P(win) / P(loss) / P(neither) (walk-forward OOS in the history, the latest surface live),
            marked-to-market expected R and the round-trip cost in R (base features of the meta model)
  geometry  setup geometry in sigma units: log a, log b (standardised barrier distances), R:R from the close,
            direction, limit-entry gap (sigma_d units), annualised vol, vol regime, earnings day inside H
  setup     the primary model's own descriptors: trend / structure / location / momentum sub-scores, zone count,
            trend and structure state signed by the setup direction
  techctx   app.technicals.context on bars <= d: calibrated 20d range-width percentile (squeeze; percentile of V(20)
            over the last 504 sessions - the calibrated radius cancels), realised-vol and ATR percentiles, ADX(14),
            +DI vs -DI and daily / weekly trend alignment signed by direction, distance to the 50d average, 5 / 20
            session returns in sigma units (signed)
  market    VIX (FRED VIXCLS) level / 1y percentile / 20d change; cross-asset risk-on / off gauge (risk_composite,
            as of knowledge_date) and its 20d change, first-eigenvalue share; W4 macro states (cycle quadrant, HMM
            regime, rate cycle of the region's central bank) LAGGED TWO MONTHS (publication lag + revisions)
  peer      conformal peer-group break-away (|z_cond| >= 2 within the last 5 sessions): signed member z, group score
            / 90% radius, sessions since; NaN when the security is in no peer group
  smart     insider open-market net buying over 90 days by knowledge date (log USD, signed by direction), distinct
            buyers / sellers, cluster flag; FINRA short interest (% shares out, 1y z, days to cover) as of knowledge
            date; smart-money composite percentile (13F flows) as of knowledge date. NaN outside source coverage.
  earnings  sessions to the next earnings day (as known), sessions since the last one, post-earnings window
  sector    region and sector one-hots

Directional context is multiplied by the setup direction (+1 long / -1 short) so a linear model can use it.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.logsetup import get_logger

log = get_logger("app.techmeta.features")

LOOKBACK = 504
REGIONS = ("US", "EU", "UK", "JP")
SECTORS = ("COMM", "CDIS", "CSTA", "ENRG", "FINS", "HLTH", "INDU", "MATR", "REAL", "TECH", "UTIL")
REGION_CB = {"US": "fed", "EU": "ecb", "UK": "boe", "JP": "boj"}
CYCLE = ("expansion", "slowdown", "contraction", "recovery")
RATE = ("hiking", "cutting", "pausing")
MACRO_LAG_MONTHS = 2
PEER_WINDOW_DAYS = 7
PEER_Z = 2.0
INSIDER_DAYS = 90
SI_MAX_AGE = 45
SM_MAX_AGE = 120

GROUPS: dict[str, list[str]] = {
    "t2": ["t2_logit_pw", "t2_logit_pl", "t2_pn", "t2_er_mtm", "t2_cost_r"],
    "geometry": ["g_log_a", "g_log_b", "g_rr", "g_dir", "g_entry_gap", "g_vol_ann", "g_vol_regime", "g_earn_in_h"],
    "setup": ["s_score_trend", "s_score_structure", "s_score_location", "s_score_momentum", "s_trend_signed",
              "s_struct_signed"],
    "techctx": ["c_rw_pct", "c_rv_pct", "c_atr_pct", "c_adx", "c_di_signed", "c_trend_d", "c_trend_w", "c_align",
                "c_sma50", "c_ret5", "c_ret20"],
    "market": ["m_vix", "m_vix_pct", "m_vix_chg20", "m_risk", "m_risk_chg20", "m_eig1",
               *[f"m_cyc_{s}" for s in CYCLE], *[f"m_hmm_{s}" for s in CYCLE], *[f"m_rate_{s}" for s in RATE]],
    "peer": ["p_z_signed", "p_absz", "p_ratio", "p_days"],
    "smart": ["i_net_signed", "i_buyers", "i_sellers", "i_cluster_signed", "si_pct", "si_z_signed", "si_dtc",
              "sm_score_signed", "sm_inst_signed"],
    "earnings": ["e_days_ahead", "e_days_since", "e_post5"],
    "sector": [*[f"r_{r}" for r in REGIONS], *[f"sec_{s}" for s in SECTORS]],
}
CONTEXT_FREE = ("t2", "geometry", "setup")
CONTEXT = ("techctx", "market", "peer", "smart", "earnings", "sector")
EXTRA_GROUP = "extra"          # passing features of the parallel agent (b1 / b5), when announced
#: Technicals v3 #1 passing factors (hi52, MAX, IVOL; docs/TECHNICALS_V3_VALIDATION.md): percentiles 0..100 within
#: region x sector (tech_factors, bars <= the date), raw and signed by the setup direction ((pct - 50) / 50 x dir)
B1_FEATURES = ["x_hi52_signed", "x_maxret_signed", "x_ivol_signed", "x_pct_hi52", "x_pct_maxret", "x_pct_ivol"]
#: Technicals v3 #5 passing output (volume confirmation): the most recent 20 / 55-session breakout within the last
#: PEER_WINDOW_DAYS calendar days (tech_breakouts, known at its close): alignment with the setup (+1 same direction,
#: -1 against, 0 none), confirmed flag and confirmation score signed by the alignment, relative volume, days since
B5_FEATURES = ["x_bo_align", "x_bo_confirmed_signed", "x_bo_score_signed", "x_bo_relvol50", "x_bo_days"]
EXTRA_FEATURES = {"b1": B1_FEATURES, "b5": B5_FEATURES}


def all_features(groups: tuple[str, ...] | list[str] | None = None, extra: list[str] | None = None) -> list[str]:
    """Feature names of the groups (default all); `extra` = full x_* names of the #1 / #5 passing features."""
    gs = list(groups) if groups is not None else list(GROUPS)
    out = [f for g in gs if g in GROUPS for f in GROUPS[g]]
    if extra and (groups is None or EXTRA_GROUP in gs):
        out += [x if x.startswith("x_") else f"x_{x}" for x in extra]
    return out


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


# ------------------------------------------------------------------------------------------------ bar features


def _pct_at(x: np.ndarray, idx: np.ndarray, lookback: int = LOOKBACK, min_n: int = 60) -> np.ndarray:
    """Share of x[i - lookback + 1 .. i] that is <= x[i] (today included), NaN with < min_n finite values."""
    out = np.full(len(idx), np.nan)
    for k, i in enumerate(idx):
        xi = x[i]
        if not math.isfinite(xi):
            continue
        w = x[max(0, i - lookback + 1): i + 1]
        w = w[np.isfinite(w)]
        if w.size >= min_n:
            out[k] = float((w <= xi + 1e-15).mean())
    return out


def _rolling_vol(c: np.ndarray, n: int = 20) -> np.ndarray:
    """app.technicals.context.realised_vol, vectorised (same NaN rule: >= n - 2 finite returns)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(c))
    out = np.full(len(c), np.nan)
    if len(r) < n:
        return out
    from numpy.lib.stride_tricks import sliding_window_view  # noqa: PLC0415

    W = sliding_window_view(r, n)                       # W[t - n] = r[t - n .. t - 1] -> realised_vol at t
    fin = np.isfinite(W)
    cnt = fin.sum(1)
    Wz = np.where(fin, W, 0.0)
    m = Wz.sum(1) / np.maximum(cnt, 1)
    v = (np.where(fin, (W - m[:, None]) ** 2, 0.0).sum(1)) / np.maximum(cnt - 1, 1)
    sd = np.sqrt(v) * math.sqrt(252)
    ok = cnt >= n - 2
    out[n:] = np.where(ok, sd, np.nan)[: len(c) - n]
    return out


def _dir_code(close: float, ma: float, ma_prev: float) -> int:
    if not all(math.isfinite(x) for x in (close, ma, ma_prev)):
        return 0
    rising = ma > ma_prev
    if close > ma and rising:
        return 1
    if close < ma and not rising:
        return -1
    return 0


def bar_features(dates: np.ndarray, o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,  # noqa: E741
                 E: np.ndarray, idx: np.ndarray, direction: np.ndarray, has_earnings: bool = True,
                 ke_last: int | None = None) -> dict[str, np.ndarray]:
    """Bar-derived context at origins idx (each uses bars <= idx only). E = earnings reaction-day flags (history:
    realised dates used as if pre-announced, as in the v2 gate). ke_last (live): sessions to the next expected earnings
    day after the last bar (0 / None = none within 20), so the last origin's V(20) carries the jump like the history."""
    from app.conformal import envelope as V  # noqa: PLC0415
    from app.technicals import context as TC  # noqa: PLC0415

    idx = np.asarray(idx, dtype=np.int64)
    dr = np.asarray(direction, dtype=np.float64)
    n = len(c)
    lc = np.log(c)
    r = np.r_[np.nan, np.diff(lc)]
    r[np.abs(np.nan_to_num(r)) > 1.0] = np.nan
    Eb = E.astype(bool)[:, None]
    s2 = V.ewma_var(r[:, None], Eb)[:, 0]
    J = V.jump_var(r[:, None], Eb, s2[:, None])[:, 0]
    ke = V.first_event_offset(Eb, 20)[:, 0].astype(np.int64)
    if ke_last is not None and n:
        ke[-1] = int(ke_last) if 0 < int(ke_last) <= 20 else 0
    v20 = 20.0 * s2 + np.where((ke > 0) & np.isfinite(J), J, 0.0)
    sd = np.sqrt(np.where(s2 > 0, s2, np.nan))
    rv = _rolling_vol(c)
    _, ap = TC.atr_pct(h, l, c)
    a, pdi, mdi = TC.adx(h, l, c)
    sma50 = TC.sma(c, 50)
    out: dict[str, np.ndarray] = {
        "c_rw_pct": _pct_at(v20, idx), "c_rv_pct": _pct_at(rv, idx), "c_atr_pct": _pct_at(ap, idx),
        "c_adx": a[idx] if n else np.zeros(0)}
    with np.errstate(divide="ignore", invalid="ignore"):
        di = (pdi[idx] - mdi[idx]) / (pdi[idx] + mdi[idx])
    out["c_di_signed"] = di * dr
    # daily trend (close vs SMA50 and its 10-session slope) and weekly (last weekly close vs SMA30w, 4-week slope)
    wk = (dates.astype("datetime64[D]").astype(np.int64) + 3) // 7
    last_of_week = np.r_[wk[1:] != wk[:-1], True]
    w_idx = np.nonzero(last_of_week)[0]                  # bar index of each week's last bar (in the data)
    w_close = c[w_idx]
    w_pos = np.searchsorted(w_idx, idx, side="left")    # position of the origin's own week
    cs = np.r_[0.0, np.cumsum(w_close)]
    td = np.zeros(len(idx))
    tw = np.zeros(len(idx))
    for k, i in enumerate(idx):
        if i >= 60:
            td[k] = _dir_code(c[i], sma50[i], sma50[i - 10])
        p = int(w_pos[k])                               # weeks before the origin's week: p complete weeks
        if p + 1 >= 35:
            m_now = (cs[p] - cs[p - 29] + c[i]) / 30.0
            m_prev = (cs[p - 3] - cs[p - 33]) / 30.0     # SMA30 four weekly points earlier (complete weeks)
            tw[k] = _dir_code(float(c[i]), m_now, m_prev)
    out["c_trend_d"] = td * dr
    out["c_trend_w"] = tw * dr
    out["c_align"] = np.where((td == tw) & (td != 0), td, 0.0) * dr
    sdi = sd[idx]
    with np.errstate(divide="ignore", invalid="ignore"):
        out["c_sma50"] = np.log(c[idx] / sma50[idx]) / (sdi * math.sqrt(50.0)) * dr
        out["c_ret5"] = np.where(idx >= 5, (lc[idx] - lc[np.maximum(idx - 5, 0)]) / (sdi * math.sqrt(5.0)), np.nan) * dr
        out["c_ret20"] = np.where(idx >= 20, (lc[idx] - lc[np.maximum(idx - 20, 0)]) / (sdi * math.sqrt(20.0)), np.nan) * dr
    # sessions since the last earnings reaction day (<= origin)
    ev = np.nonzero(E)[0]
    if has_earnings and ev.size:
        k = np.searchsorted(ev, idx, side="right") - 1
        since = np.where(k >= 0, idx - ev[np.maximum(k, 0)], np.nan).astype(np.float64)   # no earlier event: unknown
        out["e_days_since"] = np.minimum(since, 63.0)
        out["e_post5"] = np.where(np.isfinite(since), (since <= 5).astype(np.float64), np.nan)
    else:
        out["e_days_since"] = np.full(len(idx), np.nan)
        out["e_post5"] = np.full(len(idx), np.nan)
    out["_sigma_d"] = sdi
    return out


# ------------------------------------------------------------------------------------------------ T2 / geometry / setup


def base_columns(df: pl.DataFrame) -> pl.DataFrame:
    """t2 / geometry / setup / sector features from a frame with the T2 + setup columns:
    pw, pl, pn, exp_r_mtm, cost_r, a, b, rr_close, direction, entry, close, sigma_d, vol_regime, earn, H,
    score_*, trend, structure_state, region, sector_id."""
    d = pl.col("direction").cast(pl.Float64)
    sig = pl.col("sigma_d")
    trend = pl.col("trend").replace_strict({"up": 1.0, "down": -1.0}, default=0.0, return_dtype=pl.Float64)
    struct = pl.col("structure_state").replace_strict({"bullish": 1.0, "bearish": -1.0}, default=0.0, return_dtype=pl.Float64)
    ex = [
        pl.Series("t2_logit_pw", _logit(df["pw"].to_numpy())), pl.Series("t2_logit_pl", _logit(df["pl"].to_numpy())),
        pl.col("pn").cast(pl.Float64).alias("t2_pn"), pl.col("exp_r_mtm").cast(pl.Float64).alias("t2_er_mtm"),
        pl.col("cost_r").cast(pl.Float64).alias("t2_cost_r"),
        pl.col("a").clip(1e-3, None).log().alias("g_log_a"), pl.col("b").clip(1e-3, None).log().alias("g_log_b"),
        pl.col("rr_close").cast(pl.Float64).clip(0.0, 20.0).alias("g_rr"), d.alias("g_dir"),
        ((pl.col("entry") / pl.col("close")).log() / sig * d).fill_nan(None).alias("g_entry_gap"),
        (sig * math.sqrt(252.0)).alias("g_vol_ann"), pl.col("vol_regime").cast(pl.Float64).alias("g_vol_regime"),
        pl.col("earn").cast(pl.Float64).alias("g_earn_in_h"),
        *[pl.col(f"score_{k}").cast(pl.Float64).alias(f"s_score_{k}") for k in ("trend", "structure", "location", "momentum")],
        (trend * d).alias("s_trend_signed"),
        (struct * d).alias("s_struct_signed"),
        *[(pl.col("region") == r).cast(pl.Float64).alias(f"r_{r}") for r in REGIONS],
        *[(pl.col("sector_id") == s).cast(pl.Float64).alias(f"sec_{s}") for s in SECTORS],
    ]
    return df.with_columns(ex)


# ------------------------------------------------------------------------------------------------ market context


def market_daily(start: date, end: date, settings: Any = None) -> pl.DataFrame:
    """[date, m_vix, m_vix_pct, m_vix_chg20, m_risk, m_risk_chg20, m_eig1] on every calendar day (as known that day)."""
    from app.conformal import data as D  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415

    days = pl.DataFrame({"date": pl.date_range(start - timedelta(days=500), end, "1d", eager=True)})
    vx = D.vix_daily(start - timedelta(days=800), end, settings)
    if vx.height:
        vx = vx.sort("date").with_columns(
            pl.col("vix").rolling_map(lambda s: float((s.to_numpy() <= s.to_numpy()[-1] + 1e-12).mean()), window_size=252,
                                      min_samples=60).alias("m_vix_pct"),
            (pl.col("vix") / pl.col("vix").shift(20) - 1.0).alias("m_vix_chg20")).rename({"vix": "m_vix"})
        out = days.join_asof(vx.sort("date"), on="date", strategy="backward")
    else:
        out = days.with_columns(pl.lit(None, pl.Float64).alias(c) for c in ("m_vix", "m_vix_pct", "m_vix_chg20"))
    try:
        cr = (scan_table("cross_asset_regime").select("date", "knowledge_date", "risk_composite", "eig1_share_60")
              .filter(pl.col("risk_composite").is_not_null()).collect().sort("date"))
        cr = cr.with_columns((pl.col("risk_composite") - pl.col("risk_composite").shift(20)).alias("m_risk_chg20"))
        cr = cr.with_columns(pl.coalesce("knowledge_date", "date").alias("kd")).sort("kd")
        cr = cr.select(pl.col("kd").alias("date"), pl.col("risk_composite").cast(pl.Float64).alias("m_risk"),
                       pl.col("m_risk_chg20").cast(pl.Float64), pl.col("eig1_share_60").cast(pl.Float64).alias("m_eig1"))
        cr = cr.unique("date", keep="last").sort("date")
        out = out.join_asof(cr, on="date", strategy="backward", tolerance=timedelta(days=10))
    except Exception as e:  # noqa: BLE001
        log.warning("cross_asset_regime unavailable: %s", e)
        out = out.with_columns(pl.lit(None, pl.Float64).alias(c) for c in ("m_risk", "m_risk_chg20", "m_eig1"))
    return out.filter(pl.col("date") >= start)


def macro_monthly(settings: Any = None) -> pl.DataFrame:
    """[region, month (the month the state becomes usable), m_cyc_*, m_hmm_*, m_rate_*] - W4 states lagged."""
    from app.conformal import data as D  # noqa: PLC0415

    st = D.macro_states(settings)
    rows = []
    if st.is_empty():
        return pl.DataFrame(schema={"region": pl.Utf8, "month": pl.Date})
    st = st.with_columns(pl.col("month").dt.offset_by(f"{MACRO_LAG_MONTHS}mo").alias("usable"))
    for rg in REGIONS:
        scope = "US" if rg == "US" else "GLOBAL"
        parts = []
        for rt, sc, pre, vals in (("cycle_quadrant", scope, "m_cyc", CYCLE), ("hmm_regime", scope, "m_hmm", CYCLE),
                                  ("rate_cycle", REGION_CB[rg], "m_rate", RATE)):
            x = st.filter((pl.col("regime_type") == rt) & (pl.col("scope") == sc)).select("usable", "state")
            if x.is_empty():
                continue
            x = x.unique("usable", keep="last").select(
                pl.col("usable").alias("month"), *[(pl.col("state") == v).cast(pl.Float64).alias(f"{pre}_{v}") for v in vals])
            parts.append(x)
        if not parts:
            continue
        m = parts[0]
        for p in parts[1:]:
            m = m.join(p, on="month", how="full", coalesce=True)
        rows.append(m.with_columns(pl.lit(rg).alias("region")))
    return pl.concat(rows, how="diagonal_relaxed").sort("month") if rows else pl.DataFrame(schema={"region": pl.Utf8, "month": pl.Date})


def join_market(df: pl.DataFrame, mk: pl.DataFrame, mm: pl.DataFrame) -> pl.DataFrame:
    df = df.sort("date").join_asof(mk.sort("date"), on="date", strategy="backward")
    if mm.height:
        df = df.with_columns(pl.col("date").dt.truncate("1mo").alias("_month"))
        df = df.sort("_month").join_asof(mm.sort("month"), left_on="_month", right_on="month", by="region",
                                         strategy="backward", tolerance=timedelta(days=100)).drop("_month", "month")
    for c in GROUPS["market"]:
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias(c))
    return df


# ------------------------------------------------------------------------------------------------ peer break-away


def peer_signals_wf(data_dir: Path) -> pl.DataFrame:
    """[security_id, date, z_cond, ratio] from the P4 walk-forward GCN-GRU signal files (|z_cond| >= 2 member-days)."""
    files = sorted((data_dir / "reports" / "conformal" / "wf_peer").glob("signals_*_gcn_gru.parquet"))
    if not files:
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "z_cond": pl.Float64, "ratio": pl.Float64})
    sg = pl.concat([pl.read_parquet(f, columns=["security_id", "date", "z_cond", "score", "q90"]) for f in files])
    return sg.select("security_id", pl.col("date").cast(pl.Date), "z_cond",
                     (pl.col("score") / pl.col("q90")).alias("ratio")).filter(pl.col("z_cond").abs() >= PEER_Z)


def peer_signals_live(as_of: date, days: int = 30) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    lo = as_of - timedelta(days=days)
    m = (scan_table("conformal_peer_members").filter((pl.col("as_of_date") >= lo) & (pl.col("as_of_date") <= as_of))
         .select("as_of_date", "group_id", "security_id", "z_cond").collect())
    s = (scan_table("conformal_peer_scores").filter((pl.col("as_of_date") >= lo) & (pl.col("as_of_date") <= as_of))
         .select("as_of_date", "group_id", "score", "q_90").collect())
    j = m.join(s, on=["as_of_date", "group_id"], how="left")
    return j.select("security_id", pl.col("as_of_date").cast(pl.Date).alias("date"), pl.col("z_cond").cast(pl.Float64),
                    (pl.col("score") / pl.col("q_90")).cast(pl.Float64).alias("ratio")).filter(pl.col("z_cond").abs() >= PEER_Z)


def peer_members(settings: Any = None) -> set[str]:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        g = scan_table("conformal_peer_groups")
        v = g.select(pl.col("version").max()).collect().item()
        return set(g.filter(pl.col("version") == v).select("security_id").collect()["security_id"].to_list())
    except Exception as e:  # noqa: BLE001
        log.warning("conformal_peer_groups unavailable: %s", e)
        return set()


def join_peer(df: pl.DataFrame, sig: pl.DataFrame, members: set[str]) -> pl.DataFrame:
    """Most extreme break-away of the security within the last PEER_WINDOW_DAYS calendar days (<= origin date)."""
    base = df.select("security_id", "date", "direction").unique(["security_id", "date"])
    if sig.height:
        s = sig.filter(pl.col("security_id").is_in(base["security_id"].unique().to_list()))
        j = base.join(s.rename({"date": "sdate"}), on="security_id", how="inner").filter(
            (pl.col("sdate") <= pl.col("date")) & (pl.col("sdate") > pl.col("date") - pl.duration(days=PEER_WINDOW_DAYS)))
        j = (j.sort(pl.col("z_cond").abs(), descending=True).unique(["security_id", "date"], keep="first")
             .select("security_id", "date", pl.col("z_cond").alias("_z"), pl.col("ratio").alias("_ratio"),
                     ((pl.col("date") - pl.col("sdate")).dt.total_days()).cast(pl.Float64).alias("_days")))
    else:
        j = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "_z": pl.Float64, "_ratio": pl.Float64, "_days": pl.Float64})
    df = df.join(j, on=["security_id", "date"], how="left")
    mem = pl.col("security_id").is_in(list(members))
    hit = pl.col("_z").is_not_null()
    df = df.with_columns(
        pl.when(hit).then(pl.col("_z") * pl.col("direction")).when(mem).then(0.0).otherwise(None).alias("p_z_signed"),
        pl.when(hit).then(pl.col("_z").abs()).when(mem).then(0.0).otherwise(None).alias("p_absz"),
        pl.when(hit).then(pl.col("_ratio")).when(mem).then(0.0).otherwise(None).alias("p_ratio"),
        pl.when(hit).then(pl.col("_days")).when(mem).then(float(PEER_WINDOW_DAYS + 1)).otherwise(None).alias("p_days"))
    return df.drop("_z", "_ratio", "_days")


# ------------------------------------------------------------------------------------------------ smart money


def insider_daily(ids: list[str], start: date, end: date) -> pl.DataFrame:
    """Open-market insider buys / sells [security_id, kd, value_usd (signed), insider_id] by knowledge date."""
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        x = (scan_table("insider_transactions")
             .filter(pl.col("security_id").is_in(ids) & pl.col("knowledge_date").is_between(start - timedelta(days=INSIDER_DAYS + 5), end)
                     & pl.col("is_open_market").fill_null(False) & pl.col("txn_type").is_in(["buy", "sell"])
                     & ~pl.col("is_derivative").fill_null(False))
             .select("security_id", pl.col("knowledge_date").alias("kd"), "txn_type", "value_usd", "insider_id").collect())
    except Exception as e:  # noqa: BLE001
        log.warning("insider_transactions unavailable: %s", e)
        return pl.DataFrame(schema={"security_id": pl.Utf8, "kd": pl.Date, "v": pl.Float64, "insider_id": pl.Utf8, "side": pl.Int8})
    return x.select("security_id", "kd", pl.when(pl.col("txn_type") == "buy").then(pl.col("value_usd").abs())
                    .otherwise(-pl.col("value_usd").abs()).fill_null(0.0).alias("v"), "insider_id",
                    pl.when(pl.col("txn_type") == "buy").then(1).otherwise(-1).cast(pl.Int8).alias("side"))


def insider_coverage(settings: Any = None) -> dict[str, date]:
    """First month per region with >= 300 open-market insider transactions (features are NaN before it)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        x = (scan_table("insider_transactions").filter(pl.col("is_open_market").fill_null(False))
             .group_by("region", pl.col("knowledge_date").dt.truncate("1mo").alias("m")).agg(pl.len().alias("n"))
             .filter(pl.col("n") >= 300).group_by("region").agg(pl.col("m").min()).collect())
        return {r: m for r, m in x.iter_rows()}
    except Exception:  # noqa: BLE001
        return {}


def join_insider(df: pl.DataFrame, tx: pl.DataFrame, cov: dict[str, date]) -> pl.DataFrame:
    """Sums over knowledge dates in (d - 90, d]."""
    base = df.select("security_id", "date").unique()
    if tx.height:
        j = base.join(tx, on="security_id", how="inner").filter(
            (pl.col("kd") <= pl.col("date")) & (pl.col("kd") > pl.col("date") - pl.duration(days=INSIDER_DAYS)))
        agg = j.group_by("security_id", "date").agg(
            pl.col("v").sum().alias("_net"), pl.col("insider_id").filter(pl.col("side") > 0).n_unique().alias("_nb"),
            pl.col("insider_id").filter(pl.col("side") < 0).n_unique().alias("_ns"))
    else:
        agg = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "_net": pl.Float64, "_nb": pl.UInt32, "_ns": pl.UInt32})
    df = df.join(agg, on=["security_id", "date"], how="left")
    covd = pl.col("region").replace_strict({k: v for k, v in cov.items()}, default=None, return_dtype=pl.Date)
    inc = covd.is_not_null() & (pl.col("date") >= covd)
    net = pl.col("_net").fill_null(0.0)
    sgnlog = net.sign() * (net.abs() / 1e5).log1p()
    nb = pl.col("_nb").fill_null(0).cast(pl.Float64)
    return df.with_columns(
        pl.when(inc).then(sgnlog * pl.col("direction")).otherwise(None).alias("i_net_signed"),
        pl.when(inc).then(nb).otherwise(None).alias("i_buyers"),
        pl.when(inc).then(pl.col("_ns").fill_null(0).cast(pl.Float64)).otherwise(None).alias("i_sellers"),
        pl.when(inc).then((nb >= 3).cast(pl.Float64) * pl.col("direction")).otherwise(None).alias("i_cluster_signed"),
    ).drop("_net", "_nb", "_ns")


def si_frame(ids: list[str], start: date, end: date) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        return (scan_table("si_signals").filter(pl.col("security_id").is_in(ids) & pl.col("knowledge_date").is_between(start - timedelta(days=60), end))
                .select("security_id", pl.col("knowledge_date").alias("kd"), pl.col("si_pct_out").cast(pl.Float64),
                        pl.col("si_z_1y").cast(pl.Float64), pl.col("days_to_cover").cast(pl.Float64)).collect()
                .drop_nulls("kd").sort("kd"))
    except Exception as e:  # noqa: BLE001
        log.warning("si_signals unavailable: %s", e)
        return pl.DataFrame(schema={"security_id": pl.Utf8, "kd": pl.Date, "si_pct_out": pl.Float64, "si_z_1y": pl.Float64,
                                    "days_to_cover": pl.Float64})


def sm_frame(ids: list[str], start: date, end: date) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        return (scan_table("smart_money_signals").filter(pl.col("security_id").is_in(ids) & pl.col("knowledge_date").is_between(start - timedelta(days=150), end))
                .select("security_id", pl.col("knowledge_date").alias("kd"), pl.col("score_pct").cast(pl.Float64),
                        pl.col("z_inst_flow").cast(pl.Float64)).collect().drop_nulls("kd").sort("kd")
                .unique(["security_id", "kd"], keep="last").sort("kd"))
    except Exception as e:  # noqa: BLE001
        log.warning("smart_money_signals unavailable: %s", e)
        return pl.DataFrame(schema={"security_id": pl.Utf8, "kd": pl.Date, "score_pct": pl.Float64, "z_inst_flow": pl.Float64})


def join_si_sm(df: pl.DataFrame, si: pl.DataFrame, sm: pl.DataFrame) -> pl.DataFrame:
    d = pl.col("direction").cast(pl.Float64)
    df = df.sort("date")
    if si.height:
        df = df.join_asof(si.sort("kd"), left_on="date", right_on="kd", by="security_id", strategy="backward",
                          tolerance=timedelta(days=SI_MAX_AGE)).drop("kd")
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias(c) for c in ("si_pct_out", "si_z_1y", "days_to_cover"))
    if sm.height:
        df = df.join_asof(sm.sort("kd"), left_on="date", right_on="kd", by="security_id", strategy="backward",
                          tolerance=timedelta(days=SM_MAX_AGE)).drop("kd")
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias(c) for c in ("score_pct", "z_inst_flow"))
    sp = pl.col("score_pct")
    sp = pl.when(sp > 1.5).then(sp / 100.0).otherwise(sp)                    # 0..100 or 0..1 -> 0..1
    return df.with_columns(pl.col("si_pct_out").alias("si_pct"), (pl.col("si_z_1y") * d).alias("si_z_signed"),
                           pl.col("days_to_cover").clip(0, 60).alias("si_dtc"), ((sp - 0.5) * 2.0 * d).alias("sm_score_signed"),
                           (pl.col("z_inst_flow").clip(-5, 5) * d).alias("sm_inst_signed")).drop(
        "si_pct_out", "si_z_1y", "days_to_cover", "score_pct", "z_inst_flow")


# ------------------------------------------------------------------------------------------------ v3 #1 / #5 features


FACTOR_COLS = ("pct_hi52", "pct_maxret", "pct_ivol")
BO_COLS = ("direction", "confirmed", "confirm_score", "relvol50")


def _read_parts(root: Path, regions: tuple[str, ...], pattern: str, cols: list[str], ids: list[str]) -> list[pl.DataFrame]:
    """Per-file reads (files without the columns, or still being written, are skipped and logged)."""
    parts = []
    for rg in regions:
        for f in sorted((root / rg).glob(pattern)):
            try:
                lf = pl.scan_parquet(f)
                if not set(cols) <= set(lf.collect_schema().names()):
                    log.warning("%s lacks %s: skipped", f, sorted(set(cols) - set(lf.collect_schema().names())))
                    continue
                parts.append(lf.select(cols).filter(pl.col("security_id").is_in(ids)).collect()
                             .with_columns(pl.col("date").cast(pl.Date)))
            except Exception as e:  # noqa: BLE001
                log.warning("%s unreadable: %s", f, e)
    return [x for x in parts if x.height]


def factors_history(root: Path, regions: tuple[str, ...], ids: list[str]) -> pl.DataFrame:
    """[security_id, date, pct_*] from the #1 research build (scratchpad/tech_v3_b15/wf/<region>/f_<year>.parquet)."""
    parts = _read_parts(root, regions, "f_*.parquet", ["security_id", "date", *FACTOR_COLS], ids)
    if parts:
        return pl.concat(parts)
    return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, **{c: pl.Float32 for c in FACTOR_COLS}})


def factors_live(ids: list[str], as_of: date, days: int = 10) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        return (scan_table("tech_factors").filter(pl.col("security_id").is_in(ids) & (pl.col("as_of_date") <= as_of)
                                                  & (pl.col("as_of_date") > as_of - timedelta(days=days)))
                .select("security_id", pl.col("as_of_date").alias("date"), *FACTOR_COLS).collect())
    except Exception as e:  # noqa: BLE001
        log.warning("tech_factors unavailable: %s", e)
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, **{c: pl.Float32 for c in FACTOR_COLS}})


def breakouts_history(root: Path, regions: tuple[str, ...], ids: list[str]) -> pl.DataFrame:
    """#5 breakout events (scratchpad/tech_v3_b15/wf/<region>/bo_c*.parquet research build)."""
    parts = _read_parts(root, regions, "bo_c*.parquet", ["security_id", "date", *BO_COLS], ids)
    return pl.concat(parts) if parts else pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date})


def breakouts_live(ids: list[str], as_of: date, days: int = 14) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        return (scan_table("tech_breakouts").filter(pl.col("security_id").is_in(ids) & (pl.col("date") <= as_of)
                                                    & (pl.col("date") > as_of - timedelta(days=days)))
                .select("security_id", pl.col("date").cast(pl.Date), *BO_COLS).collect())
    except Exception as e:  # noqa: BLE001
        log.warning("tech_breakouts unavailable: %s", e)
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date})


def join_factors(df: pl.DataFrame, fac: pl.DataFrame) -> pl.DataFrame:
    """Latest #1 percentiles on or before the origin (at most 7 calendar days old)."""
    d = pl.col("direction").cast(pl.Float64)
    if fac.height:
        f = fac.with_columns(pl.col(c).cast(pl.Float64) for c in FACTOR_COLS).sort("date")
        df = df.sort("date").join_asof(f, on="date", by="security_id", strategy="backward", tolerance=timedelta(days=7))
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias(c) for c in FACTOR_COLS)
    return df.with_columns(
        ((pl.col("pct_hi52") - 50) / 50 * d).alias("x_hi52_signed"), ((pl.col("pct_maxret") - 50) / 50 * d).alias("x_maxret_signed"),
        ((pl.col("pct_ivol") - 50) / 50 * d).alias("x_ivol_signed"), pl.col("pct_hi52").alias("x_pct_hi52"),
        pl.col("pct_maxret").alias("x_pct_maxret"), pl.col("pct_ivol").alias("x_pct_ivol")).drop(list(FACTOR_COLS))


def join_breakouts(df: pl.DataFrame, bo: pl.DataFrame) -> pl.DataFrame:
    """Most recent breakout of the security within PEER_WINDOW_DAYS calendar days (on or before the origin date)."""
    base = df.select("security_id", "date").unique()
    if bo.height:
        b = bo.filter(pl.col("security_id").is_in(base["security_id"].unique().to_list())).rename({"date": "bdate", "direction": "bdir"})
        j = base.join(b, on="security_id", how="inner").filter(
            (pl.col("bdate") <= pl.col("date")) & (pl.col("bdate") > pl.col("date") - pl.duration(days=PEER_WINDOW_DAYS)))
        j = (j.sort(["bdate", "confirm_score"], descending=[True, True]).unique(["security_id", "date"], keep="first")
             .select("security_id", "date", pl.col("bdir").cast(pl.Float64).alias("_bdir"),
                     pl.col("confirmed").cast(pl.Float64).alias("_conf"), pl.col("confirm_score").cast(pl.Float64).alias("_score"),
                     pl.col("relvol50").cast(pl.Float64).alias("_rv"),
                     (pl.col("date") - pl.col("bdate")).dt.total_days().cast(pl.Float64).alias("_days")))
    else:
        j = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "_bdir": pl.Float64, "_conf": pl.Float64,
                                 "_score": pl.Float64, "_rv": pl.Float64, "_days": pl.Float64})
    df = df.join(j, on=["security_id", "date"], how="left")
    al = (pl.col("_bdir") * pl.col("direction").cast(pl.Float64)).fill_null(0.0)
    return df.with_columns(al.alias("x_bo_align"), (pl.col("_conf").fill_null(0.0) * al).alias("x_bo_confirmed_signed"),
                           (pl.col("_score").fill_null(0.0) / 100.0 * al).alias("x_bo_score_signed"),
                           pl.col("_rv").fill_null(0.0).clip(0, 20).alias("x_bo_relvol50"),
                           pl.col("_days").fill_null(float(PEER_WINDOW_DAYS + 1)).alias("x_bo_days")).drop(
        "_bdir", "_conf", "_score", "_rv", "_days")


# ------------------------------------------------------------------------------------------------ matrix


def ensure_columns(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    miss = [c for c in cols if c not in df.columns]
    return df.with_columns(pl.lit(None, pl.Float64).alias(c) for c in miss) if miss else df


def matrix(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    df = ensure_columns(df, cols)
    return np.column_stack([df[c].cast(pl.Float64).fill_null(np.nan).to_numpy() for c in cols]) if cols else np.zeros((df.height, 0))
