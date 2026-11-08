"""W4.2 macro exposures (job `macro_exposures`, weekly) -> macro_exposures, macro_exposure_fits (+ episode replay).

Method (see contracts/schemas/macro_exposures.yaml):
* Weekly local-currency returns (app.macro.markets.returns) on weekly official factor changes (macro_factors, W).
* Every month end A (plus the run date): the trailing WINDOW weeks whose Friday <= A; >= MIN_OBS weekly returns.
* joint:      r = a + b'f + b_u u + e, f = JOINT, u = equity - [1 f] gamma (S&P 500 orthogonalised on f in the same
              window), so b are total effects and b_u is the loading on the equity shock not explained by macro.
* univariate: r = a + b f + e for each f in UNIV (equity = CAPM beta).
* surprise:   r = a + b's + b_u u_s + e, s = changes of the W2 surprise indices (growth, inflation, labor) of the
              entity's home economy (model-based expectations), u_s = equity orthogonalised on s.
* Newey-West HAC(HAC_LAGS) covariance with small-sample factor n / (n - k); missing weeks are masked per entity.
* Empirical-Bayes shrinkage of securities toward the sector prior (median of raw betas in the sector at that date,
  as_of x model x factor): tau2 = max(robust var(b_raw) - mean(se^2), 0.05 var), w = tau2 / (tau2 + se^2);
  beta = m + w (b_raw - m), se = sqrt(w) se_raw. Sectors with < 5 securities use the all-security prior.
Resumable: per-chunk raw results are checkpointed under data/tmp/macro_w4_exposures/<run key>/ and reused.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table
from app.macro.markets import factors as fx
from app.macro.markets.returns import Panel, build_panels, period_end, security_meta

log = get_logger(__name__)

CODE_VERSION = "mx_v1"
JOINT = ["d2y", "dcurve", "dbe10", "dcredit", "usd", "oil"]
UNIV = ["d2y", "d10y", "dcurve", "dreal10", "dbe10", "dcredit", "usd", "oil", "equity"]
SURP_THEMES = ["growth", "inflation", "labor"]
WINDOW = 156
MIN_OBS = 52
HAC_LAGS = 4
CHUNK = 2000
HISTORY_MONTHS_SECURITIES = 24
AGG_START = date(2004, 1, 1)
SHRINK_MIN_GROUP = 5

EA_MEMBERS = {"DE", "FR", "IT", "ES", "NL", "PT", "BE", "AT", "FI", "IE", "GR", "LU", "SK", "SI", "LT", "LV", "EE",
              "CY", "MT", "HR"}
REGION_SCOPE = {"US": "US", "CA": "CA", "UK": "GB", "EU": "EA", "JP": "JP", "KR": "KR", "CN": "CN", "HK": "HK",
                "LATAM": "BR", "APAC": "AU", "OTHER": "GLOBAL"}
INSTRUMENT_SCOPE = {
    "JP": ["ETF:EWJ", "IDX:^N225", "IDX:6J=F", "ETF:FXY"], "DE": ["ETF:EWG", "IDX:^GDAXI"],
    "GB": ["ETF:EWU", "IDX:^FTSE", "IDX:6B=F", "ETF:FXB"], "FR": ["ETF:EWQ", "IDX:^FCHI"],
    "IT": ["ETF:EWI", "IDX:FTSEMIB.MI"], "ES": ["ETF:EWP", "IDX:^IBEX"], "NL": ["ETF:EWN", "IDX:^AEX"],
    "CA": ["ETF:EWC", "IDX:^GSPTSE"], "AU": ["ETF:EWA", "IDX:^AXJO"], "BR": ["ETF:EWZ", "IDX:^BVSP"],
    "MX": ["ETF:EWW", "IDX:^MXX"], "KR": ["ETF:EWY", "IDX:^KS11", "IDX:^KQ11"],
    "CN": ["ETF:FXI", "ETF:MCHI", "ETF:ASHR", "ETF:KWEB", "IDX:000300.SS", "IDX:000001.SS", "IDX:399001.SZ"],
    "HK": ["ETF:EWH", "IDX:^HSI", "IDX:^HSCE"], "TW": ["ETF:EWT", "IDX:^TWII"],
    "IN": ["ETF:INDA", "ETF:EPI", "IDX:^NSEI", "IDX:^BSESN"], "CL": ["ETF:ECH", "IDX:^IPSA"], "PE": ["ETF:EPU"],
    "AR": ["ETF:ARGT", "IDX:^MERV"], "CH": ["ETF:EWL", "IDX:^SSMI"], "PT": ["IDX:PSI20.LS", "ETF:PGAL"],
    "EA": ["ETF:EZU", "ETF:FEZ", "IDX:^STOXX50E", "ETF:VGK", "IDX:^STOXX", "IDX:6E=F", "ETF:FXE", "ETF:EWK",
           "ETF:EWO", "ETF:EFNL", "ETF:EIRL", "ETF:GREK", "IDX:^BFX", "IDX:^ATX"],
    "GLOBAL": ["ETF:ACWI", "ETF:VT", "ETF:EFA", "ETF:VEA", "ETF:EEM", "ETF:IEMG", "ETF:VWO", "ETF:VXUS", "ETF:AAXJ",
               "ETF:BNDX", "ETF:BWX", "ETF:EMB"],
}
INSTRUMENT_SCOPE_OF = {i: cc for cc, ids in INSTRUMENT_SCOPE.items() for i in ids}


# ------------------------------------------------------------------------------------------------ regression engine


def fit_batch(Y: np.ndarray, X: np.ndarray, min_obs: int, lags: int = HAC_LAGS) -> dict[str, np.ndarray]:
    """Masked OLS of every column of Y (T x N, NaN = missing) on X (T x K, incl. constant, complete) with
    Newey-West HAC(lags) covariance. Returns b (N,K), cov (N,K,K), r2, n, rvol (NaN where n < min_obs)."""
    T, N = Y.shape
    K = X.shape[1]
    M = np.isfinite(Y)
    Y0 = np.where(M, Y, 0.0)
    Mf = M.astype(float)
    n = M.sum(axis=0)
    XtX = np.einsum("tn,tj,tk->njk", Mf, X, X, optimize=True)
    Xty = np.einsum("tn,tj->nj", Y0, X, optimize=True)
    ok = n >= max(min_obs, K + 2)
    b = np.full((N, K), np.nan)
    cov = np.full((N, K, K), np.nan)
    r2 = np.full(N, np.nan)
    rvol = np.full(N, np.nan)
    if not ok.any():
        return {"b": b, "cov": cov, "r2": r2, "n": n, "rvol": rvol}
    idx = np.where(ok)[0]
    A = XtX[idx]
    det_ok = np.linalg.cond(A) < 1e12
    idx, A = idx[det_ok], A[det_ok]
    if not len(idx):
        return {"b": b, "cov": cov, "r2": r2, "n": n, "rvol": rvol}
    Ainv = np.linalg.inv(A)
    bb = np.einsum("njk,nk->nj", Ainv, Xty[idx])
    Ms = Mf[:, idx]
    e = (Y0[:, idx] - X @ bb.T) * Ms                                    # T x n
    ni = n[idx].astype(float)
    ybar = Y0[:, idx].sum(axis=0) / ni
    sst = (((Y0[:, idx] - ybar) * Ms) ** 2).sum(axis=0)
    ssr = (e ** 2).sum(axis=0)
    G = e[:, :, None] * X[:, None, :]                                    # T x n x K
    S = np.einsum("tnj,tnk->njk", G, G, optimize=True)
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        Gm = np.einsum("tnj,tnk->njk", G[lag:], G[:-lag], optimize=True)
        S += w * (Gm + np.transpose(Gm, (0, 2, 1)))
    C = Ainv @ S @ Ainv * (ni / np.maximum(ni - K, 1.0))[:, None, None]
    b[idx] = bb
    cov[idx] = C
    with np.errstate(invalid="ignore", divide="ignore"):
        r2[idx] = np.where(sst > 0, 1.0 - ssr / sst, np.nan)
        rvol[idx] = np.sqrt(ssr / np.maximum(ni - K, 1.0))
    return {"b": b, "cov": cov, "r2": r2, "n": n, "rvol": rvol}


def ols1(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Plain OLS for a complete column y on X; returns (coef, resid, HAC cov)."""
    out = fit_batch(y[:, None], X, min_obs=X.shape[1] + 2)
    coef = out["b"][0]
    return coef, y - X @ coef, out["cov"][0]


# ------------------------------------------------------------------------------------------------ scopes


def surprise_scopes(fac_w: pl.DataFrame) -> dict[str, list[str]]:
    """economy -> available surprise factor columns (growth & inflation required)."""
    out: dict[str, list[str]] = {}
    for c in fac_w.columns:
        if c.startswith("surp_"):
            _, th, cc = c.split("_", 2)
            out.setdefault(cc, []).append(th)
    return {cc: [f"surp_{t}_{cc}" for t in SURP_THEMES if t in ths] for cc, ths in out.items()
            if "growth" in ths and "inflation" in ths}


def entity_scope(key: tuple[str, str, str], sec_country: dict[str, str], scopes: set[str]) -> str:
    et, eid, _ = key

    def pick(cc: str | None) -> str:
        if cc in scopes:
            return cc  # type: ignore[return-value]
        if cc in EA_MEMBERS and "EA" in scopes:
            return "EA"
        return "GLOBAL" if "GLOBAL" in scopes else next(iter(scopes), "GLOBAL")

    if et == "security":
        return pick(sec_country.get(eid))
    if et == "sector" and ":" in eid:
        return pick(REGION_SCOPE.get(eid.split(":")[0]))
    if et == "region":
        return pick(REGION_SCOPE.get(eid))
    if et in ("etf", "index", "future"):
        cc = INSTRUMENT_SCOPE_OF.get(eid)
        if cc is None:
            cc = "US" if et in ("etf", "future") or eid in ("IDX:^GSPC", "IDX:^DJI", "IDX:^IXIC", "IDX:^NDX",
                                                            "IDX:^RUT", "IDX:^MID", "IDX:DX-Y.NYB") else "GLOBAL"
        return pick(cc)
    return pick("GLOBAL")


# ------------------------------------------------------------------------------------------------ estimation


def as_of_schedule(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        nm = date(y + (m == 12), m % 12 + 1, 1)
        me = nm - timedelta(days=1)
        if me > end:
            break
        if me >= start:
            out.append(me)
        y, m = nm.year, nm.month
    if not out or out[-1] != end:
        out.append(end)
    return out


def _window(dates: list[date], as_of: date, ok_rows: np.ndarray) -> np.ndarray:
    """Row indices of the trailing WINDOW complete weeks (Friday <= as_of, factor row complete)."""
    rows = [i for i, d in enumerate(dates) if period_end(d, "W") <= as_of and ok_rows[i]]
    return np.array(rows[-WINDOW:], dtype=int)


def _rows_out(keys: list[tuple[str, str, str]], as_of: date, model: str, factors: list[str], res: dict[str, np.ndarray],
              scope: str | None, cols_k: list[int]) -> list[pl.DataFrame]:
    """Long rows for the coefficients at positions cols_k (aligned to factors)."""
    N = len(keys)
    out = []
    for f, k in zip(factors, cols_k, strict=True):
        b = res["b"][:, k]
        se = np.sqrt(res["cov"][:, k, k])
        good = np.isfinite(b) & np.isfinite(se)
        if not good.any():
            continue
        gi = np.where(good)[0]
        out.append(pl.DataFrame({
            "as_of_date": [as_of] * len(gi), "entity_type": [keys[i][0] for i in gi],
            "entity_id": [keys[i][1] for i in gi], "weighting": [keys[i][2] for i in gi], "model": model,
            "factor_id": f, "beta_raw": b[gi], "se_raw": se[gi], "r2": res["r2"][gi],
            "n_obs": res["n"][gi].astype(np.int32), "scope": scope}, schema_overrides={"as_of_date": pl.Date}))
    del N
    return out


def estimate(panel: Panel, fac_w: pl.DataFrame, as_ofs: list[date], sec_country: dict[str, str],
             keep_fits_for: date | None) -> tuple[pl.DataFrame, list[dict[str, Any]], dict[str, Any]]:
    """Run joint / univariate / surprise for every entity of `panel` at every as_of.
    Returns (long raw rows, fit records for keep_fits_for, gamma info)."""
    fw = fac_w.with_columns(pl.col("date").cast(pl.Date))
    pos = {d: i for i, d in enumerate(fw["date"].to_list())}
    rows_idx = np.array([pos.get(d, -1) for d in panel.dates])

    def col(name: str) -> np.ndarray:
        if name not in fw.columns:
            return np.full(len(panel.dates), np.nan)
        v = fw[name].to_numpy().astype(float)
        return np.where(rows_idx >= 0, v[np.maximum(rows_idx, 0)], np.nan)

    Fj = np.column_stack([col(f) for f in JOINT])
    eq = col("equity")
    ok_joint = np.isfinite(Fj).all(axis=1) & np.isfinite(eq)
    scopes = surprise_scopes(fac_w)
    scope_of = [entity_scope(k, sec_country, set(scopes)) for k in panel.keys]
    out: list[pl.DataFrame] = []
    fits: list[dict[str, Any]] = []
    gamma: dict[str, Any] = {}
    for a in as_ofs:
        R = panel.R
        # ---- joint
        w = _window(panel.dates, a, ok_joint)
        if len(w) >= MIN_OBS:
            F = Fj[w]
            Xf = np.column_stack([np.ones(len(w)), F])
            g, u, gcov = ols1(eq[w], Xf)
            X = np.column_stack([Xf, u])
            res = fit_batch(R[w], X, MIN_OBS)
            names = JOINT + ["equity_resid"]
            out += _rows_out(panel.keys, a, "joint", names, res, None, list(range(1, len(names) + 1)))
            if a == keep_fits_for:
                gamma = {"factors": JOINT, "betas": g[1:].tolist(), "cov": gcov[1:, 1:].ravel().tolist(),
                         "alpha": float(g[0]), "resid_vol": float(np.std(u, ddof=len(JOINT) + 1)),
                         "r2": float(1 - np.var(u) / np.var(eq[w])), "n_obs": int(len(w))}
                for i, k in enumerate(panel.keys):
                    if np.isfinite(res["b"][i]).all():
                        fits.append({"key": k, "model": "joint", "factors": names, "b": res["b"][i, 1:],
                                     "cov": res["cov"][i, 1:, 1:], "alpha": res["b"][i, 0], "rvol": res["rvol"][i],
                                     "r2": res["r2"][i], "n": int(res["n"][i]), "scope": None})
        # ---- univariate
        for f in UNIV:
            x = eq if f == "equity" else col(f)
            okf = np.isfinite(x)
            w = _window(panel.dates, a, okf)
            if len(w) < MIN_OBS:
                continue
            X = np.column_stack([np.ones(len(w)), x[w]])
            res = fit_batch(R[w], X, MIN_OBS)
            out += _rows_out(panel.keys, a, "univariate", [f], res, None, [1])
        # ---- surprise (per economy)
        for cc, cols in scopes.items():
            members = [i for i, s in enumerate(scope_of) if s == cc]
            if not members:
                continue
            S = np.column_stack([col(c) for c in cols])
            oks = np.isfinite(S).all(axis=1) & np.isfinite(eq)
            w = _window(panel.dates, a, oks)
            if len(w) < MIN_OBS:
                continue
            Xs = np.column_stack([np.ones(len(w)), S[w]])
            _, u, _ = ols1(eq[w], Xs)
            X = np.column_stack([Xs, u])
            sub = panel.subset(members)
            res = fit_batch(sub.R[w], X, MIN_OBS)
            names = [f"surp_{c.split('_')[1]}" for c in cols] + ["equity_resid"]
            out += _rows_out(sub.keys, a, "surprise", names, res, cc, list(range(1, len(names) + 1)))
            if a == keep_fits_for:
                for i, k in enumerate(sub.keys):
                    if np.isfinite(res["b"][i]).all():
                        fits.append({"key": k, "model": "surprise", "factors": names, "b": res["b"][i, 1:],
                                     "cov": res["cov"][i, 1:, 1:], "alpha": res["b"][i, 0], "rvol": res["rvol"][i],
                                     "r2": res["r2"][i], "n": int(res["n"][i]), "scope": cc})
    raw = pl.concat(out, how="vertical_relaxed") if out else pl.DataFrame()
    return raw, fits, gamma


# ------------------------------------------------------------------------------------------------ shrinkage


def shrink(raw: pl.DataFrame, sector_of: pl.DataFrame) -> pl.DataFrame:
    """Empirical-Bayes shrinkage of security betas toward the sector prior; aggregates pass through (w = 1)."""
    sec = raw.filter(pl.col("entity_type") == "security")
    agg = raw.filter(pl.col("entity_type") != "security").with_columns(
        pl.col("beta_raw").alias("beta"), pl.col("se_raw").alias("se"), pl.lit(1.0).alias("shrink_weight"),
        pl.lit(None, dtype=pl.Float64).alias("peer_percentile"))
    if sec.is_empty():
        return agg
    keys = ["as_of_date", "model", "factor_id"]
    sec = sec.join(sector_of, left_on="entity_id", right_on="security_id", how="left").with_columns(
        pl.col("sector_id").fill_null("UNKNOWN"))

    def stats(by: list[str], pfx: str) -> pl.DataFrame:
        med = pl.col("beta_raw").median()
        return sec.group_by(by).agg(
            med.alias(f"{pfx}m"),
            ((1.4826 * (pl.col("beta_raw") - med).abs().median()) ** 2).alias(f"{pfx}v"),
            (pl.col("se_raw") ** 2).mean().alias(f"{pfx}s"), pl.len().alias(f"{pfx}n"))

    s = sec.join(stats(keys + ["sector_id"], "g_"), on=keys + ["sector_id"], how="left").join(
        stats(keys, "a_"), on=keys, how="left")
    use_g = pl.col("g_n") >= SHRINK_MIN_GROUP
    m = pl.when(use_g).then(pl.col("g_m")).otherwise(pl.col("a_m"))
    v = pl.when(use_g).then(pl.col("g_v")).otherwise(pl.col("a_v"))
    sm = pl.when(use_g).then(pl.col("g_s")).otherwise(pl.col("a_s"))
    tau2 = pl.when(v - sm > 0.05 * v).then(v - sm).otherwise(0.05 * v).clip(lower_bound=1e-12)
    s = s.with_columns(m.alias("_m"), tau2.alias("_t"))
    s = s.with_columns((pl.col("_t") / (pl.col("_t") + pl.col("se_raw") ** 2)).alias("shrink_weight"))
    s = s.with_columns((pl.col("_m") + pl.col("shrink_weight") * (pl.col("beta_raw") - pl.col("_m"))).alias("beta"),
                       (pl.col("shrink_weight").sqrt() * pl.col("se_raw")).alias("se"))
    s = s.with_columns((pl.col("beta").rank("average").over(keys + ["sector_id"])
                        / pl.len().over(keys + ["sector_id"]) * 100.0).alias("peer_percentile"))
    s = s.drop([c for c in s.columns if c.startswith(("g_", "a_", "_")) or c == "sector_id"])
    return pl.concat([s, agg], how="diagonal_relaxed")


# ------------------------------------------------------------------------------------------------ job


def _ckpt_dir(settings: Settings, key: str) -> Path:
    d = settings.data_dir / "tmp" / "macro_w4_exposures" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_fits(fits: list[dict[str, Any]], path: Path) -> None:
    rows = [{"entity_type": f["key"][0], "entity_id": f["key"][1], "weighting": f["key"][2], "model": f["model"],
             "factors": f["factors"], "b_raw": [float(x) for x in f["b"]], "cov_raw": [float(x) for x in np.ravel(f["cov"])],
             "alpha": float(f["alpha"]), "resid_vol": float(f["rvol"]), "r2": float(f["r2"]), "n_obs": f["n"],
             "scope": f["scope"]} for f in fits]
    schema = {"entity_type": pl.Utf8, "entity_id": pl.Utf8, "weighting": pl.Utf8, "model": pl.Utf8,
              "factors": pl.List(pl.Utf8), "b_raw": pl.List(pl.Float64), "cov_raw": pl.List(pl.Float64),
              "alpha": pl.Float64, "resid_vol": pl.Float64, "r2": pl.Float64, "n_obs": pl.Int32, "scope": pl.Utf8}
    pl.DataFrame(rows, schema=schema).write_parquet(path)


FITS_SCHEMA: dict[str, Any] = {
    "as_of_date": pl.Date, "entity_type": pl.Utf8, "entity_id": pl.Utf8, "weighting": pl.Utf8, "model": pl.Utf8,
    "factors": pl.List(pl.Utf8), "betas": pl.List(pl.Float64), "cov": pl.List(pl.Float64), "alpha": pl.Float64,
    "resid_vol": pl.Float64, "r2": pl.Float64, "n_obs": pl.Int32, "scope": pl.Utf8,
    "computed_at": pl.Datetime("us", "UTC")}


def final_fits(fits_raw: pl.DataFrame, shrunk: pl.DataFrame, as_of: date, now: datetime) -> pl.DataFrame:
    """Shrunk betas + covariance scaled by sqrt(w_i w_j) for the latest as_of."""
    latest = shrunk.filter(pl.col("as_of_date") == as_of).select(
        "entity_type", "entity_id", "weighting", "model", "factor_id", "beta", "shrink_weight")
    lk = {(r[0], r[1], r[2], r[3], r[4]): (r[5], r[6]) for r in latest.iter_rows()}
    rows = []
    for r in fits_raw.iter_rows(named=True):
        fs = r["factors"]
        k = len(fs)
        bw = [lk.get((r["entity_type"], r["entity_id"], r["weighting"], r["model"], f), (None, None)) for f in fs]
        b = np.array([x[0] if x[0] is not None else br for x, br in zip(bw, r["b_raw"], strict=True)])
        w = np.array([x[1] if x[1] is not None else 1.0 for x in bw])
        cov = np.array(r["cov_raw"]).reshape(k, k) * np.sqrt(np.outer(w, w))
        rows.append({"as_of_date": as_of, "entity_type": r["entity_type"], "entity_id": r["entity_id"],
                     "weighting": r["weighting"], "model": r["model"], "factors": fs, "betas": b.tolist(),
                     "cov": cov.ravel().tolist(), "alpha": r["alpha"], "resid_vol": r["resid_vol"], "r2": r["r2"],
                     "n_obs": r["n_obs"], "scope": r["scope"], "computed_at": now})
    return pl.DataFrame(rows, schema=FITS_SCHEMA)


def load_weekly_factors(settings: Settings, end: date) -> pl.DataFrame:
    try:
        fac = fx.load_factors(settings, "W")
    except Exception:  # noqa: BLE001 - table missing -> compute on the fly
        fac = pl.DataFrame()
    if fac.is_empty():
        fac, _ = fx.build_factors(settings, end)
    ids = sorted(set(JOINT + UNIV + ["cash"]) | {f for f in fac["factor_id"].unique().to_list() if f.startswith("surp_")})
    return fx.wide(fac, "W", ids).filter(pl.col("date") <= end)


def run_exposures(settings: Settings | None = None, as_of: date | None = None, run_id: str | None = None,
                  history_months: int = HISTORY_MONTHS_SECURITIES, securities: bool = True, write: bool = True,
                  sec_limit: int | None = None, check=None) -> dict[str, Any]:  # noqa: ANN001
    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    t0 = time.perf_counter()
    rep: dict[str, Any] = {"as_of": as_of.isoformat()}
    fac_w = load_weekly_factors(s, as_of)
    key = f"{as_of.isoformat()}_{CODE_VERSION}_{history_months}_{int(securities)}_{sec_limit or 0}"
    ck = _ckpt_dir(s, key)
    meta = security_meta(s)
    sec_country = dict(zip(meta["security_id"].to_list(), meta["country"].to_list(), strict=True))
    first_win = AGG_START + timedelta(days=7 * MIN_OBS)
    as_of_agg = as_of_schedule(first_win, as_of)
    sec_start = date(as_of.year - (history_months // 12) - 4, 1, 1)
    as_of_sec = [d for d in as_of_agg if d >= as_of - timedelta(days=31 * history_months)]
    # ---- aggregates
    agg_raw_p, agg_fit_p = ck / "agg_raw.parquet", ck / "agg_fits.parquet"
    gamma_p = ck / "gamma.json"
    if not agg_raw_p.exists():
        t1 = time.perf_counter()
        pan = build_panels(s, "W", AGG_START, as_of, securities=False)["aggregate"]
        raw, fits, gamma = estimate(pan, fac_w, as_of_agg, sec_country, as_of)
        raw.write_parquet(agg_raw_p)
        _save_fits(fits, agg_fit_p)
        gamma_p.write_text(json.dumps(gamma), encoding="utf-8")
        rep["agg_seconds"] = round(time.perf_counter() - t1, 1)
        rep["agg_entities"] = len(pan.keys)
        del pan
    if check:
        check()
    # ---- securities (chunked, checkpointed)
    if securities:
        t1 = time.perf_counter()
        pan = build_panels(s, "W", sec_start, as_of, aggregates=False)["security"]
        n_all = len(pan.keys) if sec_limit is None else min(sec_limit, len(pan.keys))
        rep["securities"] = n_all
        for c0 in range(0, n_all, CHUNK):
            pr, pf = ck / f"sec_{c0:06d}_raw.parquet", ck / f"sec_{c0:06d}_fits.parquet"
            if pr.exists() and pf.exists():
                continue
            sub = pan.subset(list(range(c0, min(c0 + CHUNK, n_all))))
            raw, fits, _ = estimate(sub, fac_w, as_of_sec, sec_country, as_of)
            raw.write_parquet(pr)
            _save_fits(fits, pf)
            log.info("macro_exposures: securities %d-%d done (%.0fs)", c0, c0 + len(sub.keys), time.perf_counter() - t1)
            if check:
                check()
        rep["sec_seconds"] = round(time.perf_counter() - t1, 1)
        del pan
    # ---- assemble + shrink
    raws = [pl.read_parquet(p) for p in sorted(ck.glob("*_raw.parquet"))]
    raw = pl.concat([r for r in raws if r.height], how="vertical_relaxed")
    sector_of = meta.select("security_id", "sector_id")
    shr = shrink(raw, sector_of)
    now = datetime.now(UTC)
    shr = shr.with_columns((pl.col("beta_raw") / pl.col("se_raw")).alias("t_stat"),
                           pl.lit(WINDOW).cast(pl.Int16).alias("window_weeks"), pl.lit(now).alias("computed_at"),
                           pl.lit(run_id).alias("run_id"))
    cols = ["as_of_date", "entity_type", "entity_id", "weighting", "model", "factor_id", "beta", "beta_raw", "se",
            "t_stat", "shrink_weight", "peer_percentile", "r2", "n_obs", "window_weeks", "scope", "computed_at", "run_id"]
    shr = shr.select(cols).with_columns(pl.col(pl.Float64).fill_nan(None))
    fits_raw = pl.concat([pl.read_parquet(p) for p in sorted(ck.glob("*_fits.parquet"))], how="vertical_relaxed")
    fits = final_fits(fits_raw, shr, as_of, now)
    gamma = json.loads(gamma_p.read_text(encoding="utf-8")) if gamma_p.exists() else {}
    if gamma:
        fits = pl.concat([fits, pl.DataFrame([{
            "as_of_date": as_of, "entity_type": "index", "entity_id": fx.MARKET_ID, "weighting": "none", "model": "gamma",
            "factors": gamma["factors"], "betas": gamma["betas"], "cov": gamma["cov"], "alpha": gamma["alpha"],
            "resid_vol": gamma["resid_vol"], "r2": gamma["r2"], "n_obs": gamma["n_obs"], "scope": None,
            "computed_at": now}], schema=FITS_SCHEMA)], how="vertical_relaxed")
    rep.update(rows=shr.height, fits=fits.height,
               entities=shr.filter(pl.col("as_of_date") == as_of).select("entity_type", "entity_id", "weighting")
               .unique().group_by("entity_type").len().sort("entity_type").rows())
    if write:
        rep["rows_written"] = write_table(shr, "macro_exposures", mode="overwrite", settings=s)
        rep["fits_written"] = write_table(fits.with_columns(pl.col("n_obs").cast(pl.Int32)), "macro_exposure_fits",
                                          mode="overwrite", settings=s)
        from app.macro.markets import scenarios  # noqa: PLC0415

        rep["episodes"] = scenarios.run_episode_replay(s, fits=fits, fac_w=fac_w, write=True)
        shutil.rmtree(ck.parent, ignore_errors=True)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    fx._update_state(s, last_exposures_run={k: v for k, v in rep.items() if k != "episodes"})
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_exposures (weekly; resumable). params: history_months (24), securities (true), sec_limit."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    try:
        rep = run_exposures(ctx.settings, ctx.as_of, ctx.run_id, int(p.get("history_months", HISTORY_MONTHS_SECURITIES)),
                            bool(p.get("securities", True)), sec_limit=p.get("sec_limit"), check=ctx.check_cancelled)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "JobCancelled":
            raise
        log.exception("macro_exposures failed")
        return JobResult(status="failed", message=str(e)[:2000], retryable=True)
    return JobResult(status="success", rows_written={"macro_exposures": rep.get("rows_written", 0),
                                                     "macro_exposure_fits": rep.get("fits_written", 0)},
                     entities_ok=rep.get("securities", 0), watermark=ctx.as_of.isoformat(),
                     message=json.dumps(rep, default=str)[:4000])
