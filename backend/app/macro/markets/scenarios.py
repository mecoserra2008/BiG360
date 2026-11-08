"""W4.4 scenario & stress engine + historical episode replay (MODEL-BASED estimates).

Shock -> factor vector (joint basis z = [d2y, dcurve, dbe10, dcredit, usd, oil, equity_resid]):
* User shocks (any subset): rates_2y_bp, rates_10y_bp, breakeven_10y_bp, real_10y_bp, credit_bp (Baa-Treasury),
  usd_pct (Fed broad dollar), oil_pct (WTI), equity_pct (S&P 500 total move), growth_surprise / inflation_surprise /
  labor_surprise (surprise-index points, home economy of each entity).
* mode isolated (default): unspecified factors are 0. A single 2y or 10y shock is a parallel shift (curve unchanged);
  real_10y without breakeven moves nominal yields 1:1; rates_10y + breakeven imply the real yield. equity_pct sets the
  equity residual u = equity - gamma'f (gamma = S&P 500 loading on the macro block, latest window), so an equity shock
  on top of macro shocks counts only the part not already explained by them; without equity_pct, u = 0.
* mode conditional: unspecified factors are set to their Gaussian conditional mean given the specified ones
  (E[z | A z = s] = S A' (A S A')^-1 s, S = weekly covariance of z over the last 10 years) - "what usually moves with it".
* Percent shocks enter in log space (usd: log(1 + pct)); impact on an entity = exp(b'z) - 1, 90% band
  exp(b'z +- 1.645 sqrt(z' C z)) - 1 with C the (shrunk) HAC covariance of its betas. Surprise shocks use the surprise
  model and are added to the macro impact (the two channels can overlap). Uncertainty bands cover estimation error
  only (not idiosyncratic noise).
* var_propagation: second-round effects from a monthly VAR (Model Studio app.modeling._algos.var_fit, BIC lag <= 3)
  on [d2y, dcurve, dbe10, dcredit, usd, oil, equity] monthly changes (2006+): cumulative MA response
  sum_{h=0..H} Psi_h s0 replaces the first-round factor vector (H = var_horizon_months, default 3).
Episodes (official-data windows, EPISODES): factor moves = official level changes over the window (pp; log changes
for USD / oil / equity); replay = current betas x moves, compared with realized returns where prices cover the window.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

JOINT_Z = ["d2y", "dcurve", "dbe10", "dcredit", "usd", "oil", "equity_resid"]
SURPRISE_Z = ["surp_growth", "surp_inflation", "surp_labor"]
SHOCK_FIELDS = {
    "rates_2y_bp": "2y Treasury yield change (bp)", "rates_10y_bp": "10y Treasury yield change (bp)",
    "breakeven_10y_bp": "10y breakeven inflation change (bp)", "real_10y_bp": "10y real yield change (bp)",
    "credit_bp": "Baa-Treasury credit spread change (bp)", "usd_pct": "Broad USD change (%)",
    "oil_pct": "WTI oil change (%)", "equity_pct": "S&P 500 change (%)",
    "growth_surprise": "Growth surprise index change (points)", "inflation_surprise": "Inflation surprise change (points)",
    "labor_surprise": "Labor surprise change (points)",
}
Z90 = 1.6448536269514722
MODEL_LABEL = "Model-based estimate (rolling factor betas; not a forecast)"

PRESETS: list[dict[str, Any]] = [
    {"scenario_id": "preset_rates_up_100", "name": "Rates +100bp (parallel)", "shocks": {"rates_2y_bp": 100, "rates_10y_bp": 100}},
    {"scenario_id": "preset_rates_down_100", "name": "Rates -100bp (parallel)", "shocks": {"rates_2y_bp": -100, "rates_10y_bp": -100}},
    {"scenario_id": "preset_bear_steepener", "name": "Bear steepener (10y +75bp, 2y +25bp)", "shocks": {"rates_2y_bp": 25, "rates_10y_bp": 75}},
    {"scenario_id": "preset_bull_flattener", "name": "Bull flattener (10y -75bp, 2y -25bp)", "shocks": {"rates_2y_bp": -25, "rates_10y_bp": -75}},
    {"scenario_id": "preset_usd_up_10", "name": "USD +10%", "shocks": {"usd_pct": 10}},
    {"scenario_id": "preset_oil_up_50", "name": "Oil +50%", "shocks": {"oil_pct": 50}},
    {"scenario_id": "preset_oil_down_40", "name": "Oil -40%", "shocks": {"oil_pct": -40}},
    {"scenario_id": "preset_credit_widening", "name": "Credit spreads +150bp", "shocks": {"credit_bp": 150}},
    {"scenario_id": "preset_stagflation", "name": "Stagflation (breakevens +75bp, oil +40%, growth surprise -100)",
     "shocks": {"breakeven_10y_bp": 75, "oil_pct": 40, "rates_10y_bp": 50, "growth_surprise": -100}},
    {"scenario_id": "preset_risk_off", "name": "Risk-off (equity -15%, credit +100bp, rates -50bp, USD +5%)",
     "shocks": {"equity_pct": -15, "credit_bp": 100, "rates_2y_bp": -50, "rates_10y_bp": -50, "usd_pct": 5}},
]
for _p in PRESETS:
    _p.update(description=None, options={"mode": "isolated", "var_propagation": False}, preset=True)


@dataclass(frozen=True)
class Episode:
    episode_id: str
    name: str
    start: date
    end: date
    rationale: str


EPISODES: list[Episode] = [
    Episode("bond_massacre_1994", "1994 bond massacre", date(1993, 10, 15), date(1994, 11, 7),
            "DGS10 trough (~5.2%, mid-Oct 1993) to peak (~8.0%, early Nov 1994); Fed funds 3.0% -> 4.75% (official H.15)"),
    Episode("gfc_2008", "2008 global financial crisis", date(2008, 9, 12), date(2009, 3, 9),
            "Friday before the Lehman bankruptcy to the equity trough; Baa-Treasury spread peaked Dec 2008 (Moody's/FRED)"),
    Episode("ea_crisis_2011", "2011 euro-area debt crisis", date(2011, 7, 1), date(2011, 10, 3),
            "Euro-area sovereign stress and the US downgrade: DGS10 3.2% -> 1.8%, Baa spread +~1pp (H.15 / FRED)"),
    Episode("taper_2013", "2013 taper tantrum", date(2013, 5, 21), date(2013, 9, 5),
            "Day before Bernanke's 22-May testimony to the DGS10 peak (~3.0%, 5-Sep-2013) (H.15)"),
    Episode("covid_2020", "2020 COVID crash", date(2020, 2, 19), date(2020, 3, 23),
            "S&P 500 peak to the Fed's 23-Mar-2020 open-ended QE announcement (Federal Reserve press release)"),
    Episode("hiking_2022", "2022 hiking cycle", date(2021, 12, 31), date(2022, 10, 21),
            "Year-end 2021 to the DGS10 peak (~4.25%, Oct 2022); Fed funds target 0-0.25% -> 3.0-3.25% (H.15)"),
]
EPISODE_BY_ID = {e.episode_id: e for e in EPISODES}
EPISODE_SERIES = {"d2y": "fred:DGS2", "d10y": "fred:DGS10", "dbe10": "fred:T10YIE", "dreal10": "fred:DFII10",
                  "dcredit": "fred:BAA10Y", "usd": "fred:DTWEXBGS", "oil": "fred:DCOILWTICO"}
PCT_FACTORS = {"usd", "oil", "equity"}


# ------------------------------------------------------------------------------------------------ shocks -> z


def _num(shocks: dict[str, Any], k: str) -> float | None:
    v = shocks.get(k)
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError) as e:
        raise ValueError(f"shock {k!r} must be a number") from e
    if not math.isfinite(x):
        raise ValueError(f"shock {k!r} must be finite")
    return x


def validate_shocks(shocks: dict[str, Any]) -> dict[str, float]:
    unknown = set(shocks) - set(SHOCK_FIELDS)
    if unknown:
        raise ValueError(f"unknown shock field(s): {sorted(unknown)}")
    out = {k: _num(shocks, k) for k in SHOCK_FIELDS}
    clean = {k: v for k, v in out.items() if v is not None}
    if not clean:
        raise ValueError("at least one shock is required")
    for k, v in clean.items():
        if k.endswith("_pct") and v <= -100:
            raise ValueError(f"{k} must be > -100")
    return clean


def isolated_z(shocks: dict[str, float], gamma: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """Explicit mapping of user shocks to the joint basis; returns (z, notes)."""
    notes: list[str] = []
    r2, r10 = shocks.get("rates_2y_bp"), shocks.get("rates_10y_bp")
    be, real = shocks.get("breakeven_10y_bp"), shocks.get("real_10y_bp")
    if r10 is not None and be is not None and real is not None:
        notes.append("real_10y_bp ignored: implied by rates_10y_bp - breakeven_10y_bp")
        real = None
    if r10 is None and real is not None:
        r10 = real + (be or 0.0)
        notes.append("10y nominal = real + breakeven")
    if be is None and r10 is not None and real is not None:
        be = r10 - real
    d10 = r10 if r10 is not None else r2
    d2 = r2 if r2 is not None else r10
    if (r2 is None) != (r10 is None):
        notes.append("single-tenor rate shock applied as a parallel shift (curve unchanged)")
    z = np.zeros(len(JOINT_Z))
    z[0] = (d2 or 0.0) / 100.0
    z[1] = ((d10 or 0.0) - (d2 or 0.0)) / 100.0
    z[2] = (be or 0.0) / 100.0
    z[3] = (shocks.get("credit_bp") or 0.0) / 100.0
    z[4] = math.log1p((shocks.get("usd_pct") or 0.0) / 100.0)
    z[5] = math.log1p((shocks.get("oil_pct") or 0.0) / 100.0)
    if shocks.get("equity_pct") is not None:
        z[6] = math.log1p(shocks["equity_pct"] / 100.0) - float(gamma @ z[:6])
    return z, notes


def constraints(shocks: dict[str, float], gamma: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Linear constraints A z = s implied by the specified shocks (joint basis)."""
    K = len(JOINT_Z)
    rows, vals = [], []

    def add(coef: dict[int, float], v: float) -> None:
        a = np.zeros(K)
        for i, c in coef.items():
            a[i] = c
        rows.append(a)
        vals.append(v)

    if "rates_2y_bp" in shocks:
        add({0: 1}, shocks["rates_2y_bp"] / 100)
    if "rates_10y_bp" in shocks:
        add({0: 1, 1: 1}, shocks["rates_10y_bp"] / 100)
    if "breakeven_10y_bp" in shocks:
        add({2: 1}, shocks["breakeven_10y_bp"] / 100)
    if "real_10y_bp" in shocks and not {"rates_10y_bp", "breakeven_10y_bp"} <= set(shocks):
        add({0: 1, 1: 1, 2: -1}, shocks["real_10y_bp"] / 100)
    if "credit_bp" in shocks:
        add({3: 1}, shocks["credit_bp"] / 100)
    if "usd_pct" in shocks:
        add({4: 1}, math.log1p(shocks["usd_pct"] / 100))
    if "oil_pct" in shocks:
        add({5: 1}, math.log1p(shocks["oil_pct"] / 100))
    if "equity_pct" in shocks:
        a = np.zeros(K)
        a[:6] = gamma
        a[6] = 1.0
        rows.append(a)
        vals.append(math.log1p(shocks["equity_pct"] / 100))
    return (np.array(rows) if rows else np.zeros((0, K))), np.array(vals)


def conditional_z(shocks: dict[str, float], gamma: np.ndarray, sigma: np.ndarray) -> np.ndarray:
    A, s = constraints(shocks, gamma)
    if not len(s):
        return np.zeros(len(JOINT_Z))
    M = A @ sigma @ A.T
    return sigma @ A.T @ np.linalg.pinv(M) @ s


def surprise_z(shocks: dict[str, float]) -> np.ndarray:
    return np.array([(shocks.get(f"{t}_surprise") or 0.0) / 100.0 for t in ("growth", "inflation", "labor")])


# ------------------------------------------------------------------------------------------------ impacts


def impacts(B: np.ndarray, C: np.ndarray, z: np.ndarray) -> dict[str, np.ndarray]:
    """B (N,K) betas, C (N,K,K) covariance, z (K,) -> log impact, sd and simple-return impact + 90% band."""
    mu = B @ z
    var = np.einsum("j,njk,k->n", z, C, z)
    sd = np.sqrt(np.maximum(var, 0.0))
    return {"log": mu, "sd": sd, "impact": np.expm1(mu), "lo": np.expm1(mu - Z90 * sd), "hi": np.expm1(mu + Z90 * sd)}


def fit_arrays(fits: pl.DataFrame, model: str, factors: list[str]) -> tuple[list[tuple[str, str, str]], np.ndarray,
                                                                           np.ndarray, list[str | None]]:
    """macro_exposure_fits rows of `model` -> (keys, B (N,K), C (N,K,K), scopes) in the order of `factors`
    (surprise fits may have fewer factors: missing ones get beta 0, var 0). Vectorised per factor layout."""
    f = fits.filter(pl.col("model") == model)
    K = len(factors)
    if f.is_empty():
        return [], np.zeros((0, K)), np.zeros((0, K, K)), []
    f = f.with_columns(pl.col("factors").list.join(",").alias("_lay"))
    keys = list(zip(f["entity_type"].to_list(), f["entity_id"].to_list(), f["weighting"].to_list(), strict=True))
    scopes = f["scope"].to_list() if "scope" in f.columns else [None] * f.height
    B = np.zeros((f.height, K))
    C = np.zeros((f.height, K, K))
    lay = f["_lay"].to_numpy()
    for layout in np.unique(lay):
        rows = np.where(lay == layout)[0]
        fs = layout.split(",") if layout else []
        k = len(fs)
        sub = f[rows]
        bb = np.array(sub["betas"].to_list(), dtype=float).reshape(len(rows), k)
        cc = np.array(sub["cov"].to_list(), dtype=float).reshape(len(rows), k, k)
        src = [i for i, x in enumerate(fs) if x in factors]
        dst = [factors.index(fs[i]) for i in src]
        if not src:
            continue
        B[np.ix_(rows, dst)] = bb[:, src]
        C[np.ix_(rows, dst, dst)] = cc[np.ix_(np.arange(len(rows)), src, src)]
    return keys, B, C, scopes


def gamma_of(fits: pl.DataFrame) -> np.ndarray:
    g = fits.filter(pl.col("model") == "gamma")
    if g.is_empty():
        return np.zeros(6)
    r = g.row(0, named=True)
    fs = list(r["factors"])
    out = np.zeros(6)
    for f, v in zip(fs, r["betas"], strict=True):
        if f in JOINT_Z[:6]:
            out[JOINT_Z.index(f)] = float(v)
    return out


# ------------------------------------------------------------------------------------------------ covariance / VAR


def factor_history(fac: pl.DataFrame, freq: str, gamma: np.ndarray, years: int = 10,
                   end: date | None = None) -> np.ndarray:
    """Complete rows of [d2y, dcurve, dbe10, dcredit, usd(log), oil(log), equity_resid] (freq W or M) over `years`."""
    from app.macro.markets.factors import wide  # noqa: PLC0415

    w = wide(fac, freq, JOINT_Z[:6] + ["equity"]).drop_nulls()
    if end is not None:
        w = w.filter(pl.col("date") <= end)
    if years:
        w = w.filter(pl.col("date") >= w["date"].max() - timedelta(days=int(365.25 * years)))
    X = w.select(JOINT_Z[:6] + ["equity"]).to_numpy().astype(float)
    for j, f in enumerate(JOINT_Z[:6] + ["equity"]):
        if f in PCT_FACTORS:
            X[:, j] = np.log1p(X[:, j])
    u = X[:, 6] - X[:, :6] @ gamma
    return np.column_stack([X[:, :6], u])


def var_cumulative(fac: pl.DataFrame, s0: np.ndarray, horizon: int, gamma: np.ndarray) -> dict[str, Any]:
    """Monthly VAR on [f (6), equity]; returns cumulative factor moves after `horizon` months starting from the
    impact vector s0 = [f0, equity0] (reduced-form shock), and the path."""
    import pandas as pd  # noqa: PLC0415

    from app.modeling._algos import var_fit  # noqa: PLC0415

    H = fac_hist_monthly(fac, gamma)
    Y = pd.DataFrame(H, columns=JOINT_Z[:6] + ["equity"])
    res, _, p = var_fit(Y, maxlags=3, ic="bic")
    psi = res.ma_rep(horizon)                       # (H+1, K, K), psi[0] = I
    path = np.cumsum(np.einsum("hij,j->hi", psi, s0), axis=0)
    return {"lags": int(p), "n_obs": int(res.nobs), "cumulative": path[-1], "path": path}


def fac_hist_monthly(fac: pl.DataFrame, gamma: np.ndarray) -> np.ndarray:
    from app.macro.markets.factors import wide  # noqa: PLC0415

    w = wide(fac, "M", JOINT_Z[:6] + ["equity"]).drop_nulls().filter(pl.col("date") >= date(2006, 2, 1))
    X = w.select(JOINT_Z[:6] + ["equity"]).to_numpy().astype(float)
    for j, f in enumerate(JOINT_Z[:6] + ["equity"]):
        if f in PCT_FACTORS:
            X[:, j] = np.log1p(X[:, j])
    return X


# ------------------------------------------------------------------------------------------------ run


def resolve(shocks: dict[str, Any], options: dict[str, Any] | None, gamma: np.ndarray,
            fac: pl.DataFrame | None) -> dict[str, Any]:
    """Shocks + options -> factor vectors (first round, total) and notes."""
    sh = validate_shocks(shocks)
    opt = {"mode": "isolated", "var_propagation": False, "var_horizon_months": 3} | (options or {})
    if opt["mode"] not in ("isolated", "conditional"):
        raise ValueError("options.mode must be isolated or conditional")
    hz = int(opt.get("var_horizon_months") or 3)
    if not 1 <= hz <= 24:
        raise ValueError("options.var_horizon_months must be in 1..24")
    notes: list[str] = []
    if opt["mode"] == "conditional":
        if fac is None:
            raise ValueError("conditional mode needs the factor history")
        Xh = factor_history(fac, "W", gamma)
        sigma = np.cov(Xh, rowvar=False)
        z = conditional_z(sh, gamma, sigma)
        notes.append(f"unspecified factors at their conditional mean (weekly covariance, {len(Xh)} weeks)")
    else:
        z, notes = isolated_z(sh, gamma)
    out: dict[str, Any] = {"shocks": sh, "options": opt, "z": z, "z_total": z.copy(), "zs": surprise_z(sh),
                           "notes": notes, "var": None}
    if opt.get("var_propagation"):
        if fac is None:
            raise ValueError("VAR propagation needs the factor history")
        eq0 = float(gamma @ z[:6] + z[6])
        s0 = np.concatenate([z[:6], [eq0]])
        v = var_cumulative(fac, s0, hz, gamma)
        cum = v["cumulative"]
        zt = np.concatenate([cum[:6], [cum[6] - float(gamma @ cum[:6])]])
        out["z_total"] = zt
        out["var"] = {"lags": v["lags"], "n_obs": v["n_obs"], "horizon_months": hz,
                      "path": [{"month": h, **{f: float(x) for f, x in zip(JOINT_Z[:6] + ["equity"], row, strict=True)}}
                               for h, row in enumerate(v["path"])]}
        notes.append(f"second-round effects: monthly VAR({v['lags']}) cumulative response over {hz} months "
                     "(Model Studio var_fit; model-based)")
    return out


def factor_vector_out(z: np.ndarray, gamma: np.ndarray) -> list[dict[str, Any]]:
    eq = float(gamma @ z[:6] + z[6])
    unit = {"d2y": "pp", "dcurve": "pp", "dbe10": "pp", "dcredit": "pp", "usd": "log", "oil": "log",
            "equity_resid": "log"}
    out = [{"factor_id": f, "value": float(v), "unit": unit[f]} for f, v in zip(JOINT_Z, z, strict=True)]
    out.append({"factor_id": "d10y", "value": float(z[0] + z[1]), "unit": "pp"})
    out.append({"factor_id": "dreal10", "value": float(z[0] + z[1] - z[2]), "unit": "pp"})
    out.append({"factor_id": "equity", "value": eq, "unit": "log"})
    return out


def prepare(fits: pl.DataFrame) -> dict[str, Any]:
    """Arrays of the fits (joint + surprise aligned to the joint keys) - cacheable per snapshot."""
    keys, B, C, _ = fit_arrays(fits, "joint", JOINT_Z)
    skeys, SB, SC, _ = fit_arrays(fits, "surprise", SURPRISE_Z)
    pos = {k: i for i, k in enumerate(keys)}
    K = len(SURPRISE_Z)
    SBa, SCa = np.zeros((len(keys), K)), np.zeros((len(keys), K, K))
    for j, k in enumerate(skeys):
        i = pos.get(k)
        if i is not None:
            SBa[i], SCa[i] = SB[j], SC[j]
    return {"keys": keys, "B": B, "C": C, "SB": SBa, "SC": SCa, "gamma": gamma_of(fits),
            "cols": {"entity_type": [k[0] for k in keys], "entity_id": [k[1] for k in keys],
                     "weighting": [k[2] for k in keys]}}


def run_scenario(fits: pl.DataFrame | None, shocks: dict[str, Any], options: dict[str, Any] | None = None,
                 fac: pl.DataFrame | None = None, prepared: dict[str, Any] | None = None) -> dict[str, Any]:
    """Impacts for every entity with a joint fit. Returns {resolved, gamma, frame (keys + impact columns)}."""
    pr = prepared if prepared is not None else prepare(fits)  # type: ignore[arg-type]
    gamma = pr["gamma"]
    rs = resolve(shocks, options, gamma, fac)
    B, C = pr["B"], pr["C"]
    first = impacts(B, C, rs["z"])
    tot = impacts(B, C, rs["z_total"]) if rs["var"] else first
    n = len(pr["keys"])
    sur_log, sur_var = np.zeros(n), np.zeros(n)
    if np.any(rs["zs"] != 0):
        si = impacts(pr["SB"], pr["SC"], rs["zs"])
        sur_log, sur_var = si["log"], si["sd"] ** 2
    mu = tot["log"] + sur_log
    sd = np.sqrt(tot["sd"] ** 2 + sur_var)
    frame = pl.DataFrame({
        **pr["cols"], "impact": np.expm1(mu), "impact_lo": np.expm1(mu - Z90 * sd), "impact_hi": np.expm1(mu + Z90 * sd),
        "impact_sd": sd, "first_round": first["impact"], "surprise_part": np.expm1(sur_log)},
        schema_overrides={"entity_type": pl.Utf8, "entity_id": pl.Utf8, "weighting": pl.Utf8})
    return {"resolved": rs, "gamma": gamma, "frame": frame}


# ------------------------------------------------------------------------------------------------ episodes


def _level_at(obs: pl.DataFrame, sid: str, d: date) -> float | None:
    o = obs.filter((pl.col("series_id") == sid) & (pl.col("date") <= d) & (pl.col("date") >= d - timedelta(days=10)))
    if o.is_empty():
        return None
    return float(o.sort("date")["value"][-1])


def episode_moves(ep: Episode, obs: pl.DataFrame, eq_levels: pl.DataFrame) -> dict[str, float | None]:
    """Official factor moves over the window: pp changes; log changes for usd / oil / equity (None = not covered)."""
    lv = {k: (_level_at(obs, s, ep.start), _level_at(obs, s, ep.end)) for k, s in EPISODE_SERIES.items()}
    out: dict[str, float | None] = {}
    for k, (a, b) in lv.items():
        if a is None or b is None:
            out[k] = None
        elif k in PCT_FACTORS:
            out[k] = math.log(b / a) if a > 0 and b > 0 else None
        else:
            out[k] = b - a
    out["dcurve"] = (out["d10y"] - out["d2y"]) if out["d10y"] is not None and out["d2y"] is not None else None
    e = eq_levels.rename({"value": "v"}) if "value" in eq_levels.columns else eq_levels
    ea = e.filter((pl.col("date") <= ep.start) & (pl.col("date") >= ep.start - timedelta(days=10)))
    eb = e.filter((pl.col("date") <= ep.end) & (pl.col("date") >= ep.end - timedelta(days=10)))
    out["equity"] = (math.log(float(eb.sort("date")["v"][-1]) / float(ea.sort("date")["v"][-1]))
                     if ea.height and eb.height else None)
    return out


def episode_z(moves: dict[str, float | None], gamma: np.ndarray) -> tuple[np.ndarray, list[str]]:
    z = np.zeros(len(JOINT_Z))
    missing = []
    for i, f in enumerate(JOINT_Z[:6]):
        v = moves.get(f)
        if v is None:
            missing.append(f)
        else:
            z[i] = v
    if moves.get("equity") is not None:
        z[6] = float(moves["equity"]) - float(gamma @ z[:6])
    else:
        missing.append("equity")
    return z, missing


def realized_returns(settings: Settings, ids: list[str], start: date, end: date) -> dict[str, float]:
    """Total return (adj_close) from the last close <= start to the last close <= end (both within 10 days)."""
    df = (scan_table("prices_daily", settings)
          .filter(pl.col("security_id").is_in(ids) & (pl.col("date") >= start - timedelta(days=10))
                  & (pl.col("date") <= end)).select("security_id", "date", "adj_close").collect())
    if df.is_empty():
        return {}
    a = df.filter(pl.col("date") <= start).sort("date").group_by("security_id").agg(pl.col("adj_close").last().alias("a"))
    b = (df.filter(pl.col("date") >= end - timedelta(days=10)).sort("date").group_by("security_id")
         .agg(pl.col("adj_close").last().alias("b")))
    j = a.join(b, on="security_id").filter((pl.col("a") > 0) & (pl.col("b") > 0))
    return dict(zip(j["security_id"].to_list(), (j["b"] / j["a"] - 1.0).to_list(), strict=True))


def realized_portfolios(settings: Settings, start: date, end: date) -> dict[tuple[str, str, str], float]:
    """Compounded weekly portfolio / factor returns over the weeks inside [start, end]."""
    from app.macro.markets.returns import build_panels, period_end  # noqa: PLC0415

    try:
        pan = build_panels(settings, "W", start - timedelta(days=7), end, securities=False, min_periods=1)["aggregate"]
    except Exception as e:  # noqa: BLE001 - no price coverage for the window
        log.info("episode %s..%s: no portfolio returns (%s)", start, end, e)
        return {}
    if not pan.keys or not pan.dates:
        return {}
    rows = [i for i, d in enumerate(pan.dates) if d >= start - timedelta(days=start.weekday()) and period_end(d, "W") <= end + timedelta(days=3)]
    if not rows:
        return {}
    sub = pan.R[rows]
    ok = np.isfinite(sub).mean(axis=0) >= 0.8
    tot = np.expm1(np.nansum(np.log1p(sub), axis=0))
    return {k: float(t) for k, t, o in zip(pan.keys, tot, ok, strict=True) if o and k[0] in ("sector", "subsector", "region", "factor")}


def run_episode_replay(settings: Settings | None = None, fits: pl.DataFrame | None = None,
                       fac_w: pl.DataFrame | None = None, write: bool = True) -> dict[str, Any]:
    from app.macro.markets.factors import equity_levels, latest_vintage  # noqa: PLC0415

    s = settings or get_settings()
    if fits is None:
        fits = scan_table("macro_exposure_fits", s).collect()
    gamma = gamma_of(fits)
    keys, B, C, _ = fit_arrays(fits, "joint", JOINT_Z)
    obs = latest_vintage(list(EPISODE_SERIES.values()), s)
    eq = equity_levels(s).rename({"value": "v"})
    now = datetime.now(UTC)
    frames, rep = [], {}
    single_ids = [k[1] for k in keys if k[0] in ("security", "etf", "index", "future")]
    for ep in EPISODES:
        moves = episode_moves(ep, obs, eq)
        z, missing = episode_z(moves, gamma)
        zm = z.copy()
        zm[6] = 0.0
        full, macro = impacts(B, C, z), impacts(B, C, zm)
        real = realized_returns(s, single_ids, ep.start, ep.end)
        realp = realized_portfolios(s, ep.start, ep.end)
        rl = [real.get(k[1]) if k[0] in ("security", "etf", "index", "future") else realp.get(k) for k in keys]
        frames.append(pl.DataFrame({
            "episode_id": ep.episode_id, "entity_type": [k[0] for k in keys], "entity_id": [k[1] for k in keys],
            "weighting": [k[2] for k in keys], "model_implied": full["impact"], "model_implied_macro": macro["impact"],
            "model_lo": full["lo"], "model_hi": full["hi"], "realized": pl.Series(rl, dtype=pl.Float64)}))
        frames.append(pl.DataFrame({
            "episode_id": ep.episode_id, "entity_type": "macro_factor", "entity_id": list(moves),
            "weighting": "none", "model_implied": None, "model_implied_macro": None, "model_lo": None, "model_hi": None,
            "realized": pl.Series(list(moves.values()), dtype=pl.Float64)},
            schema_overrides={"model_implied": pl.Float64, "model_implied_macro": pl.Float64, "model_lo": pl.Float64,
                              "model_hi": pl.Float64}))
        n_real = sum(1 for x in rl if x is not None)
        rep[ep.episode_id] = {"missing_factors": missing, "realized_covered": n_real}
    out = pl.concat(frames, how="vertical_relaxed").with_columns(pl.lit(now).alias("computed_at"))
    out = out.with_columns(pl.col(pl.Float64).fill_nan(None))
    if write:
        rep["rows"] = write_table(out, "macro_episode_replay", mode="overwrite", settings=s)
    return rep


def episodes_out() -> list[dict[str, Any]]:
    return [{"episode_id": e.episode_id, "name": e.name, "start": e.start.isoformat(), "end": e.end.isoformat(),
             "rationale": e.rationale} for e in EPISODES]


def dumps(o: Any) -> str:
    return json.dumps(o, default=str, separators=(",", ":"))
