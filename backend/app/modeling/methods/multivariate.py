"""Multivariate time series: VAR (lag selection, IRF with bands, FEVD, Granger), VECM with Johansen rank test,
cointegration (Engle-Granger, Johansen) with a pairs-spread helper (owner: model-studio)."""

from __future__ import annotations

import warnings as _w
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .. import _algos as A
from .._core import next_bucket
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, series, ts_band


def _endog(data: FitData) -> tuple[Any, list[str], list[Any]]:
    cols = ([data.y] if data.y else []) + [c for c in data.x if c != data.y]
    f = data.frame.select(["date"] + cols).to_pandas().dropna()
    if len(cols) < 2:
        raise GuardrailError("missing_x", "pick at least 2 series")
    return f[cols].astype(float), cols, f["date"].tolist()


def _future_dates(last: Any, n: int, freq: str) -> list[Any]:
    out, d = [], last
    for _ in range(n):
        d = next_bucket(d, freq)
        out.append(d)
    return out


class VarParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lags: int | None = Field(default=None, ge=1, le=24, description="fixed lag order; null = select by `ic`")
    maxlags: int = Field(default=8, ge=1, le=24)
    ic: Literal["aic", "bic", "hqic", "fpe"] = "bic"
    trend: Literal["n", "c", "ct"] = "c"
    irf_periods: int = Field(default=24, ge=1, le=120)
    orthogonalized: bool = Field(default=True, description="Cholesky-orthogonalized IRFs (ordering = column order)")
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    forecast_steps: int = Field(default=12, ge=0, le=120)


@register
class VAR(Method):
    id = "var"
    family = "multivariate"
    title = "Vector autoregression (VAR)"
    description = ("statsmodels VAR on y + x columns (all endogenous): lag order by information criterion, "
                   "impulse responses with asymptotic bands, forecast-error variance decomposition, pairwise Granger "
                   "causality, stability and residual whiteness.")
    shapes = ("timeseries",)
    needs_y = False
    min_x = 1
    Params = VarParams
    outputs = ("coefficients", "lag selection table", "IRF grid (irf plot)", "FEVD (fevd plot)", "Granger matrix", "forecast")
    diagnostics = ("stability (roots)", "Portmanteau whiteness", "normality")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.3 + n_x ** 2 * 0.02

    def fit(self, data: FitData, p: VarParams) -> Result:
        res = Result(self.id, self.family, self.title)
        Y, cols, dates = _endog(data)
        k = len(cols)
        if len(Y) < 3 * k * (p.lags or 2) + 10:
            raise GuardrailError("too_few_obs", f"{len(Y)} observations are too few for a {k}-variable VAR")
        r, sel, lag = A.var_fit(Y, p.maxlags, p.ic, p.lags, p.trend)
        if sel is not None:
            ics = sel.ics
            res.add_table("lag_selection", f"Lag selection (chosen by {p.ic}: {lag})", ["lag"] + list(ics),
                          clean([[i] + [ics[c][i] for c in ics] for i in range(len(ics["aic"]))]))
        names = list(r.params.index)
        rows = []
        for j, eq in enumerate(cols):
            ci_lo = r.params[eq] - 1.959964 * r.stderr[eq]
            ci_hi = r.params[eq] + 1.959964 * r.stderr[eq]
            for rr in coef_rows([f"{eq}: {n}" for n in names], r.params[eq], r.stderr[eq], r.tvalues[eq],
                                r.pvalues[eq], np.column_stack([ci_lo, ci_hi])):
                rows.append(rr | {"equation": eq})
        res.coefficients = rows
        H = p.irf_periods
        irf = r.irf(H)
        resp = irf.orth_irfs if p.orthogonalized else irf.irfs
        se = irf.stderr(orth=p.orthogonalized)
        z = 1.959964 if p.alpha == 0.05 else float(__import__("scipy").stats.norm.ppf(1 - p.alpha / 2))
        cells = [{"impulse": cols[i], "response": cols[j], "mean": resp[:, j, i], "lower": resp[:, j, i] - z * se[:, j, i],
                  "upper": resp[:, j, i] + z * se[:, j, i]} for i in range(k) for j in range(k)]
        res.plots["irf"] = clean({"kind": "irf", "title": ("Orthogonalized" if p.orthogonalized else "Simple") +
                                  " impulse responses", "horizons": list(range(H + 1)), "variables": cols,
                                  "orthogonalized": p.orthogonalized, "cells": cells})
        fe = r.fevd(H).decomp  # (response, horizon, impulse)
        res.plots["fevd"] = clean({"kind": "fevd", "title": "Forecast-error variance decomposition",
                                   "horizons": list(range(1, H + 1)), "variables": cols,
                                   "shares": [{"response": cols[j], "impulse": cols[i], "share": fe[j, :, i]}
                                              for j in range(k) for i in range(k)]})
        gm = []
        for i in cols:
            for j in cols:
                if i == j:
                    continue
                t = r.test_causality(j, [i], kind="f")
                gm.append([i, j, float(t.test_statistic), float(t.pvalue)])
                res.add_test(f"Granger: {i} -> {j}", t.test_statistic, t.pvalue, t.df, f"{i} does not Granger-cause {j}",
                             group="granger", passed=None)
        res.add_table("granger", "Granger causality (F)", ["cause", "effect", "F", "p_value"], clean(gm))
        stable = bool(r.is_stable())
        res.add_test("Stability (all roots outside unit circle)", float(np.min(np.abs(r.roots))), None, None,
                     "stable VAR", conclusion="stable" if stable else "UNSTABLE", passed=stable)
        if not stable:
            res.warn("unstable", "VAR is not stable: IRFs explode; difference the data or reduce lags")
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            try:
                wt = r.test_whiteness(nlags=max(lag + 4, 10))
                res.add_test("Portmanteau whiteness", wt.test_statistic, wt.pvalue, wt.df, "no residual autocorrelation")
            except Exception:
                pass
            try:
                nt = r.test_normality()
                res.add_test("Jarque-Bera (multivariate)", nt.test_statistic, nt.pvalue, nt.df, "normal residuals")
            except Exception:
                pass
        if p.forecast_steps:
            m, lo, hi = r.forecast_interval(Y.to_numpy()[-lag:], p.forecast_steps, alpha=p.alpha)
            fd = _future_dates(dates[-1], p.forecast_steps, data.freq)
            res.forecast = clean({"dates": fd, "mean": m[:, 0], "lower": lo[:, 0], "upper": hi[:, 0], "alpha": p.alpha,
                                  "steps": p.forecast_steps, "variable": cols[0],
                                  "history": {"dates": dates[-120:], "values": Y.iloc[-120:, 0].to_numpy()},
                                  "by_variable": [{"variable": c, "mean": m[:, j], "lower": lo[:, j], "upper": hi[:, j]}
                                                  for j, c in enumerate(cols)]})
        res.fit_stats.update(clean({"lags": lag, "aic": r.aic, "bic": r.bic, "hqic": r.hqic, "llf": r.llf,
                                    "n_obs": int(r.nobs)}))
        res.summary.update({"variables": cols, "lags": lag, "n_obs": int(r.nobs), "trend": p.trend})
        res.metrics.update(clean({"lags": lag, "aic": r.aic, "bic": r.bic, "n_obs": int(r.nobs)}))
        if p.orthogonalized:
            res.info("ordering", "orthogonalized IRFs depend on the variable order (Cholesky): " + " -> ".join(cols))
        return res


class VecmParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    k_ar_diff: int = Field(default=1, ge=0, le=12, description="lags in differences")
    det_order: Literal[-1, 0, 1] = Field(default=0, description="Johansen deterministic term: -1 none, 0 constant, 1 trend")
    rank: int | None = Field(default=None, ge=1, description="cointegration rank; null = Johansen trace test at 5%")
    forecast_steps: int = Field(default=12, ge=0, le=120)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class VECM(Method):
    id = "vecm"
    family = "multivariate"
    title = "Vector error-correction model (VECM)"
    description = "Johansen trace / max-eigenvalue rank test, then statsmodels VECM: cointegrating vectors (beta), loadings (alpha), forecast."
    shapes = ("timeseries",)
    needs_y = False
    min_x = 1
    Params = VecmParams
    outputs = ("Johansen table", "beta / alpha", "forecast")
    diagnostics = ("Johansen trace / max-eig",)

    def fit(self, data: FitData, p: VecmParams) -> Result:
        from statsmodels.tsa.vector_ar.vecm import VECM as _VECM

        res = Result(self.id, self.family, self.title)
        Y, cols, dates = _endog(data)
        j = A.johansen(Y, p.det_order, p.k_ar_diff)
        tb = j["table"]
        res.add_table("johansen", "Johansen rank test (H0: rank <= r)", list(tb.columns), clean(tb.values.tolist()))
        for _, row in tb.iterrows():
            res.add_test(f"Johansen trace r <= {int(row['rank_le'])}", row["trace"], None, None,
                         f"rank <= {int(row['rank_le'])}", group="cointegration", passed=None,
                         conclusion=(f"rank > {int(row['rank_le'])} (reject at 5%)" if row["trace"] > row["cv95"]
                                     else f"rank <= {int(row['rank_le'])} not rejected (5%)"))
        rank = p.rank or j["rank"]
        if rank == 0:
            res.warn("no_cointegration", "Johansen finds no cointegration at 5%: a VAR in differences is appropriate")
            rank = 1
        rank = min(rank, len(cols) - 1)
        det = {-1: "n", 0: "co", 1: "lo"}[p.det_order]
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            r = _VECM(Y.to_numpy(), k_ar_diff=p.k_ar_diff, coint_rank=rank, deterministic=det).fit()
        res.add_table("beta", "Cointegrating vectors (beta, normalized)", ["variable"] + [f"ce{i + 1}" for i in range(rank)],
                      clean([[c] + list(r.beta[i]) for i, c in enumerate(cols)]))
        rows = []
        for i, c in enumerate(cols):
            for q in range(rank):
                rows.append({"term": f"alpha {c} <- ce{q + 1}", "estimate": r.alpha[i, q], "std_error": r.stderr_alpha[i, q],
                             "statistic": r.tvalues_alpha[i, q], "p_value": r.pvalues_alpha[i, q],
                             "ci_low": r.alpha[i, q] - 1.96 * r.stderr_alpha[i, q], "ci_high": r.alpha[i, q] + 1.96 * r.stderr_alpha[i, q]})
        res.coefficients = clean(rows)
        if p.forecast_steps:
            m, lo, hi = r.predict(steps=p.forecast_steps, alpha=p.alpha)
            fd = _future_dates(dates[-1], p.forecast_steps, data.freq)
            res.forecast = clean({"dates": fd, "mean": m[:, 0], "lower": lo[:, 0], "upper": hi[:, 0], "alpha": p.alpha,
                                  "steps": p.forecast_steps, "variable": cols[0],
                                  "history": {"dates": dates[-120:], "values": Y.iloc[-120:, 0].to_numpy()}})
        ect = Y.to_numpy() @ r.beta[:, 0]
        ts_band(res, "ect", "Error-correction term (first cointegrating relation)", dates, [("ect", ect - ect.mean())],
                hlines=[{"y": 0, "label": ""}])
        res.summary.update({"variables": cols, "rank": rank, "rank_from_test": j["rank"], "n_obs": len(Y)})
        res.fit_stats.update(clean({"rank": rank, "llf": r.llf, "n_obs": len(Y)}))
        res.metrics.update({"rank": rank, "n_obs": len(Y)})
        return res


class CointParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    det_order: Literal[-1, 0, 1] = 0
    k_ar_diff: int = Field(default=1, ge=0, le=12)
    z_window: int = Field(default=60, ge=5, le=1000, description="rolling z-score window for the spread")
    entry_z: float = Field(default=2.0, gt=0, le=6)


@register
class Cointegration(Method):
    id = "cointegration"
    family = "multivariate"
    title = "Cointegration (Engle-Granger, Johansen) + pairs spread"
    description = ("Engle-Granger test of y on x (first x is the pair leg), Johansen on all series; pairs helper: "
                   "OLS hedge ratio, spread, full-sample and rolling z-scores, AR(1) half-life.")
    shapes = ("timeseries",)
    min_x = 1
    Params = CointParams
    outputs = ("EG / Johansen tests", "hedge ratio", "spread with z bands (ts_band)", "half-life")
    diagnostics = ("Engle-Granger", "Johansen trace", "ADF on spread")

    def fit(self, data: FitData, p: CointParams) -> Result:
        res = Result(self.id, self.family, self.title)
        Y, cols, dates = _endog(data)
        if len(Y) < 30:
            raise GuardrailError("too_few_obs", "need >= 30 observations")
        y, x = Y[cols[0]].to_numpy(), Y[cols[1]].to_numpy()
        ps = A.pairs_spread(y, x, p.z_window)
        res.add_test(f"Engle-Granger ({cols[0]} ~ {cols[1]})", ps["eg_stat"], ps["eg_p"], None, "no cointegration",
                     group="cointegration", passed=None,
                     conclusion="cointegrated (5%)" if ps["eg_p"] < 0.05 else "not cointegrated (5%)",
                     note=f"critical values 1/5/10%: {', '.join(f'{c:.2f}' for c in ps['eg_crit'])}")
        j = A.johansen(Y, p.det_order, p.k_ar_diff)
        res.add_table("johansen", "Johansen rank test", list(j["table"].columns), clean(j["table"].values.tolist()))
        res.coefficients = [clean({"term": "const", "estimate": ps["alpha"], "std_error": None, "statistic": None,
                                   "p_value": None, "ci_low": None, "ci_high": None}),
                            clean({"term": f"hedge_ratio ({cols[1]})", "estimate": ps["hedge_ratio"], "std_error": None,
                                   "statistic": None, "p_value": None, "ci_low": None, "ci_high": None})]
        s = ps["spread"]
        m, sd = float(np.mean(s)), float(np.std(s, ddof=1))
        ts_band(res, "spread", f"Spread {cols[0]} - {ps['hedge_ratio']:.3f} x {cols[1]}", dates, [("spread", s)],
                [(f"+/-{p.entry_z} sd", m - p.entry_z * sd + 0 * s, m + p.entry_z * sd + 0 * s)],
                hlines=[{"y": m, "label": "mean"}])
        ts_band(res, "zscore", f"Rolling z-score ({p.z_window})", dates, [("z", ps["z_rolling"])],
                hlines=[{"y": p.entry_z, "label": "entry"}, {"y": -p.entry_z, "label": "entry"}, {"y": 0, "label": ""}])
        res.line_plot("legs", "Series", [series(c, dates, Y[c].to_numpy()) for c in cols], "date", "")
        res.fit_stats.update(clean({"hedge_ratio": ps["hedge_ratio"], "half_life": ps["half_life"],
                                    "eg_p": ps["eg_p"], "johansen_rank": j["rank"], "spread_sd": sd,
                                    "last_z": ps["z_rolling"][-1]}))
        res.summary.update({"variables": cols, "n_obs": len(Y)})
        res.metrics.update(clean({"eg_p": ps["eg_p"], "half_life": ps["half_life"], "n_obs": len(Y)}))
        res.info("insample_hedge", "hedge ratio and full-sample z use the whole sample (in-sample); trade on the rolling z")
        if ps["eg_p"] >= 0.05:
            res.warn("not_cointegrated", f"Engle-Granger does not reject no-cointegration (p={ps['eg_p']:.3f})")
        return res
