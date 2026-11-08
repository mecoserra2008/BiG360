"""Volatility and regimes: GARCH / EGARCH / GJR (arch), Markov switching (statsmodels), state space
(UnobservedComponents) and Kalman time-varying beta (owner: model-studio)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats

from .. import _algos as A
from .. import diagnostics as dg
from .._core import next_bucket
from .timeseries import _next_weekday
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, series, ts_band


def _ts(data: FitData, cols: list[str]) -> tuple[Any, list[Any]]:
    if data.is_panel:
        raise GuardrailError("bad_shape", "needs a time-series dataset")
    f = data.frame.select(["date"] + cols).to_pandas().dropna()
    return f, f["date"].tolist()


class GarchParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vol: Literal["garch", "egarch", "gjr"] = "garch"
    p: int = Field(default=1, ge=1, le=3)
    q: int = Field(default=1, ge=1, le=3)
    dist: Literal["normal", "t", "skewt"] = "t"
    mean: Literal["constant", "zero", "ar"] = "constant"
    ar_lags: int = Field(default=1, ge=1, le=5)
    scale: float = Field(default=100.0, gt=0, description="multiply returns (fractions -> percent helps the optimizer)")
    horizon: int = Field(default=10, ge=1, le=250)
    var_levels: list[float] = Field(default=[0.01, 0.05], min_length=1, max_length=4)


@register
class Garch(Method):
    id = "garch"
    family = "volatility"
    title = "GARCH volatility (GARCH / EGARCH / GJR)"
    description = ("arch package: conditional mean + GARCH(p,q), EGARCH or GJR-GARCH with normal / Student-t / skew-t "
                   "errors; conditional volatility, persistence and half-life, volatility forecast, 1-period VaR "
                   "with an in-sample Kupiec backtest.")
    shapes = ("timeseries",)
    min_x = 0
    max_x = 0
    Params = GarchParams
    outputs = ("parameters", "conditional volatility (ts_band)", "VaR backtest", "volatility forecast")
    diagnostics = ("Ljung-Box on squared std. residuals", "Kupiec POF", "Jarque-Bera std. residuals")

    def fit(self, data: FitData, p: GarchParams) -> Result:
        res = Result(self.id, self.family, f"{p.vol.upper()}({p.p},{p.q}) {p.dist}")
        f, dates = _ts(data, [data.y])
        r = f[data.y].to_numpy(float) * p.scale
        if len(r) < 250:
            res.warn("short_sample", f"{len(r)} observations: GARCH needs long samples (>= 500 recommended)")
        if len(r) < 100:
            raise GuardrailError("too_few_obs", "GARCH needs at least 100 observations")
        g = A.garch_fit(r, p.vol, p.p, p.q, p.dist, p.mean, p.ar_lags)
        ci = np.asarray(g.conf_int())
        res.coefficients = coef_rows(list(g.params.index), g.params, g.std_err, g.tvalues, g.pvalues, ci)
        prm = g.params
        pers = None
        if p.vol in ("garch", "gjr"):
            pers = float(sum(v for k, v in prm.items() if k.startswith(("alpha", "beta"))) +
                         0.5 * sum(v for k, v in prm.items() if k.startswith("gamma")))
        elif p.vol == "egarch":
            pers = float(sum(v for k, v in prm.items() if k.startswith("beta")))
        hl = float(np.log(0.5) / np.log(pers)) if pers and 0 < pers < 1 else None
        cv = np.asarray(g.conditional_volatility, float) / p.scale
        ret = r / p.scale
        mu = (r - np.asarray(g.resid, float)) / p.scale
        std = np.asarray(g.std_resid, float)
        dist = g.model.distribution
        dparams = prm[[k for k in prm.index if k in ("nu", "lambda")]].to_numpy()
        bands, var_rows = [], []
        for a in p.var_levels:
            qa = float(dist.ppf(a, dparams))
            var_line = -(mu + qa * cv)
            exceed = ret < -var_line
            lr, pv = A.kupiec(exceed[np.isfinite(var_line)], a)
            var_rows.append([a, float(exceed.mean()), int(exceed.sum()), lr, pv])
            res.add_test(f"Kupiec POF VaR {a:.0%}", lr, pv, 1, f"exceedance rate = {a:.0%}", group="diagnostics")
            bands.append((f"VaR {a:.0%}", -var_line, var_line))
        ts_band(res, "cond_vol", "Conditional volatility (per period)", dates, [("|return|", np.abs(ret)), ("sigma", cv)])
        ts_band(res, "var_backtest", "Returns vs in-sample VaR thresholds", dates,
                [("return", ret)] + [(n, lo) for n, lo, _ in bands])
        res.add_table("var_backtest", "VaR backtest (in-sample)", ["level", "exceed_rate", "exceedances", "LR_pof", "p_value"],
                      clean(var_rows))
        from statsmodels.stats.diagnostic import acorr_ljungbox
        lb = acorr_ljungbox(std[np.isfinite(std)] ** 2, lags=[10], return_df=True)
        res.add_test("Ljung-Box std. residuals^2 (lag 10)", lb["lb_stat"].iloc[0], lb["lb_pvalue"].iloc[0], 10,
                     "no remaining ARCH effects")
        jb = stats.jarque_bera(std[np.isfinite(std)])
        res.add_test("Jarque-Bera std. residuals", jb.statistic, jb.pvalue, 2, "normal standardized residuals",
                     group="distribution", passed=None)
        fc = g.forecast(horizon=p.horizon, reindex=False, method="simulation", simulations=1000)
        fvar = np.asarray(fc.variance.iloc[-1], float)
        sims = np.sqrt(np.asarray(fc.simulations.variances, float)[-1]) / p.scale  # (simulations, horizon)
        band = 0.05
        fmean = np.asarray(fc.mean.iloc[-1], float)
        fd, d = [], dates[-1]
        for _ in range(p.horizon):
            d = next_bucket(d, data.freq) if data.freq != "D" else _next_weekday(d)
            fd.append(d)
        fsig = np.sqrt(fvar) / p.scale
        q1 = float(dist.ppf(min(p.var_levels), dparams))
        res.forecast = clean({"dates": fd, "mean": fsig, "lower": np.quantile(sims, band / 2, axis=0),
                              "upper": np.quantile(sims, 1 - band / 2, axis=0), "alpha": band,
                              "interval": "simulated 95% band of the conditional volatility (1000 paths)",
                              "steps": p.horizon, "variable": "sigma",
                              "var_1": float(-(fmean[0] / p.scale + q1 * fsig[0])),
                              "history": {"dates": dates[-250:], "values": cv[-250:]}})
        ann = float(np.sqrt({"D": 252, "W": 52, "M": 12, "Q": 4}[data.freq]))
        uncond = float(np.sqrt(prm["omega"] / (1 - pers)) / p.scale) if (pers and pers < 1 and p.vol != "egarch") else None
        res.fit_stats.update(clean({"persistence": pers, "half_life_periods": hl, "uncond_vol": uncond,
                                    "uncond_vol_annualized": uncond * ann if uncond else None,
                                    "last_vol": cv[-1], "last_vol_annualized": cv[-1] * ann,
                                    "var_next": float(-(fmean[0] / p.scale + q1 * fsig[0])), "aic": g.aic, "bic": g.bic,
                                    "llf": g.loglikelihood}))
        res.summary.update({"dependent": data.y, "n_obs": len(r), "model": res.title, "scale": p.scale})
        res.metrics.update(clean({"persistence": pers, "aic": g.aic, "last_vol_annualized": cv[-1] * ann, "n_obs": len(r)}))
        if pers is not None and pers >= 0.999:
            res.warn("integrated", f"persistence {pers:.4f} ~ 1: IGARCH-like, unconditional variance undefined")
        return res


class MSParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    k_regimes: int = Field(default=2, ge=2, le=4)
    model: Literal["regression", "ar"] = "regression"
    order: int = Field(default=1, ge=1, le=4, description="AR order (model=ar)")
    switching_variance: bool = True
    search_reps: int = Field(default=10, ge=0, le=100, description="random starting values (robustness)")


@register
class MarkovSwitching(Method):
    id = "markov_switching"
    family = "volatility"
    title = "Markov-switching regression / AR"
    description = ("statsmodels MarkovRegression / MarkovAutoregression with switching mean (and variance); x columns "
                   "enter as switching regressors (regression model). Smoothed regime probabilities, transition "
                   "matrix and expected durations.")
    shapes = ("timeseries",)
    min_x = 0
    Params = MSParams
    outputs = ("regime parameters", "smoothed probabilities (regime_prob)", "transition matrix", "expected durations")
    diagnostics = ("expected durations", "regime classification certainty")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.3 + n_rows * 2e-4 * (params.search_reps + 1) * params.k_regimes

    def fit(self, data: FitData, p: MSParams) -> Result:
        res = Result(self.id, self.family, self.title)
        f, dates = _ts(data, [data.y] + list(data.x))
        if len(f) < 40:
            raise GuardrailError("too_few_obs", "Markov switching needs >= 40 observations")
        y = f[data.y].to_numpy(float)
        ex = f[list(data.x)].to_numpy(float) if data.x and p.model == "regression" else None
        r = A.markov_fit(y, p.k_regimes, p.model, p.order, ex, p.switching_variance, p.search_reps, data.seed)
        names = list(r.model.param_names)
        ci = np.asarray(r.conf_int())
        res.coefficients = coef_rows(names, r.params, r.bse, r.tvalues, r.pvalues, ci)
        sp = np.asarray(r.smoothed_marginal_probabilities)
        sp = sp if sp.shape[0] == len(y) - (p.order if p.model == "ar" else 0) else sp.T
        d0 = dates[len(dates) - sp.shape[0]:]
        prm = dict(zip(names, np.asarray(r.params), strict=True))
        sig = [prm.get(f"sigma2[{i}]", prm.get("sigma2", np.nan)) for i in range(p.k_regimes)]
        cst = [prm.get(f"const[{i}]", np.nan) for i in range(p.k_regimes)]
        order = np.argsort(sig) if p.switching_variance else np.argsort(cst)
        labels = {int(i): ("calm" if rank == 0 else "turbulent" if rank == p.k_regimes - 1 else f"regime {rank}")
                  for rank, i in enumerate(order)} if p.switching_variance else {int(i): f"regime {i}" for i in range(p.k_regimes)}
        res.plots["regime_prob"] = clean({"kind": "regime_prob", "title": "Smoothed regime probabilities", "x": d0,
                                          "regimes": [{"regime": i, "label": labels[i], "prob": sp[:, i]} for i in range(p.k_regimes)],
                                          "overlay": {"name": data.y, "y": y[len(y) - len(d0):]}})
        P = np.asarray(r.regime_transition)[:, :, 0] if np.asarray(r.regime_transition).ndim == 3 else np.asarray(r.regime_transition)
        res.add_table("transition", "Transition matrix P[i -> j] (rows: to, columns: from, statsmodels convention)",
                      ["to / from"] + [labels[i] for i in range(p.k_regimes)],
                      clean([[labels[i]] + list(P[i]) for i in range(p.k_regimes)]))
        dur = np.asarray(r.expected_durations, float).ravel()
        regime_rows = [[i, labels[i], cst[i], sig[i], dur[i], float(sp[:, i].mean())] for i in range(p.k_regimes)]
        res.add_table("regimes", "Regimes", ["regime", "label", "const", "sigma2", "expected_duration", "share_of_time"],
                      clean(regime_rows))
        cert = float(np.mean(np.max(sp, axis=1)))
        res.fit_stats.update(clean({"aic": r.aic, "bic": r.bic, "llf": r.llf, "classification_certainty": cert,
                                    **{f"duration_{labels[i]}": dur[i] for i in range(p.k_regimes)}}))
        res.summary.update({"dependent": data.y, "n_obs": len(y), "k_regimes": p.k_regimes, "model": p.model,
                            "current_regime": labels[int(np.argmax(sp[-1]))]})
        res.metrics.update(clean({"aic": r.aic, "bic": r.bic, "n_obs": len(y), "classification_certainty": cert}))
        if cert < 0.7:
            res.warn("weak_regimes", f"average max regime probability {cert:.2f}: regimes are not well separated")
        return res


class UCMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    level: Literal["local level", "local linear trend", "smooth trend", "random walk with drift", "random walk"] = "local level"
    seasonal: int | None = Field(default=None, ge=2, le=52, description="seasonal period (e.g. 12)")
    forecast_steps: int = Field(default=12, ge=0, le=120)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class StateSpace(Method):
    id = "state_space"
    family = "volatility"
    title = "State space: unobserved components"
    description = ("statsmodels UnobservedComponents (local level / trend / seasonal, optional static regressors from "
                   "x): smoothed components with bands, forecast, residual diagnostics.")
    shapes = ("timeseries",)
    min_x = 0
    Params = UCMParams
    outputs = ("smoothed level / trend / seasonal (ts_band)", "forecast", "variance parameters")
    diagnostics = ("Ljung-Box", "Jarque-Bera", "heteroskedasticity")

    def fit(self, data: FitData, p: UCMParams) -> Result:
        res = Result(self.id, self.family, f"UCM ({p.level}{', seasonal ' + str(p.seasonal) if p.seasonal else ''})")
        f, dates = _ts(data, [data.y] + list(data.x))
        y = f[data.y].to_numpy(float)
        ex = f[list(data.x)].to_numpy(float) if data.x else None
        if len(y) < 24:
            raise GuardrailError("too_few_obs", "need >= 24 observations")
        r = A.ucm_fit(y, p.level, p.seasonal, ex)
        names = list(r.model.param_names)
        if data.x:
            names = [data.x[int(n.split(".x")[-1]) - 1] if n.startswith("beta.x") else n for n in names]
        res.coefficients = coef_rows(names, r.params, r.bse, r.tvalues, r.pvalues, np.asarray(r.conf_int(alpha=p.alpha)))
        z = float(stats.norm.ppf(1 - p.alpha / 2))
        for comp in ("level", "trend", "seasonal"):
            c = getattr(r, comp, None)
            if c is None or c.get("smoothed") is None:
                continue
            m = np.asarray(c["smoothed"], float)
            sd = np.sqrt(np.maximum(np.asarray(c["smoothed_cov"], float), 0))
            lines = [(comp, m)] + ([("y", y)] if comp == "level" else [])
            ts_band(res, f"component_{comp}", f"Smoothed {comp}", dates, lines, [(f"{int((1 - p.alpha) * 100)}%", m - z * sd, m + z * sd)])
        burn = 2 + (p.seasonal or 0)
        resid = np.asarray(r.resid, float)[burn:]
        dg.residual_diagnostics(res, y[burn:], np.asarray(r.fittedvalues)[burn:], resid, None, dates[burn:], True)
        if p.forecast_steps:
            fx = np.repeat(ex[-1:], p.forecast_steps, axis=0) if ex is not None else None
            fc = r.get_forecast(p.forecast_steps, exog=fx)
            ci = np.asarray(fc.conf_int(alpha=p.alpha))
            fd, d = [], dates[-1]
            for _ in range(p.forecast_steps):
                d = next_bucket(d, data.freq)
                fd.append(d)
            res.forecast = clean({"dates": fd, "mean": np.asarray(fc.predicted_mean), "lower": ci[:, 0], "upper": ci[:, 1],
                                  "alpha": p.alpha, "steps": p.forecast_steps,
                                  "history": {"dates": dates[-120:], "values": y[-120:]}})
        res.fit_stats.update(clean({"aic": r.aic, "bic": r.bic, "llf": r.llf}))
        res.summary.update({"dependent": data.y, "n_obs": len(y), "level": p.level, "seasonal": p.seasonal})
        res.metrics.update(clean({"aic": r.aic, "bic": r.bic, "n_obs": len(y)}))
        return res


class TVBetaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_constant: bool = True
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class TVBeta(Method):
    id = "tv_beta"
    family = "volatility"
    title = "Time-varying beta (Kalman filter)"
    description = ("Regression with random-walk coefficients y_t = x_t' b_t + e_t estimated by MLE with the Kalman "
                   "smoother; smoothed beta paths with bands vs the static OLS beta.")
    shapes = ("timeseries",)
    min_x = 1
    max_x = 5
    Params = TVBetaParams
    outputs = ("beta paths (ts_band)", "state variances", "static OLS comparison")
    diagnostics = ("Ljung-Box on one-step errors", "share of beta variation")

    def fit(self, data: FitData, p: TVBetaParams) -> Result:
        res = Result(self.id, self.family, self.title)
        f, dates = _ts(data, [data.y] + list(data.x))
        y = f[data.y].to_numpy(float)
        X = f[list(data.x)].to_numpy(float)
        names = list(data.x)
        if p.add_constant:
            X = np.column_stack([np.ones(len(y)), X])
            names = ["const"] + names
        if len(y) < 30:
            raise GuardrailError("too_few_obs", "need >= 30 observations")
        r = A.tv_beta(y, X)
        res.coefficients = coef_rows(list(r.model.param_names), r.params, r.bse, r.tvalues, r.pvalues,
                                     np.asarray(r.conf_int(alpha=p.alpha)))
        st = np.asarray(r.smoothed_state, float)
        cov = np.asarray(r.smoothed_state_cov, float)
        z = float(stats.norm.ppf(1 - p.alpha / 2))
        b_ols = np.linalg.lstsq(X, y, rcond=None)[0]
        burn = min(5, len(y) // 10)
        rows = []
        for i, n in enumerate(names):
            m, sd = st[i], np.sqrt(np.maximum(cov[i, i], 0))
            ts_band(res, f"beta_{n}", f"Time-varying coefficient: {n}", dates[burn:], [(n, m[burn:]), ("OLS", np.full(len(y) - burn, b_ols[i]))],
                    [(f"{int((1 - p.alpha) * 100)}%", (m - z * sd)[burn:], (m + z * sd)[burn:])])
            rows.append([n, b_ols[i], float(m[-1]), float(np.min(m[burn:])), float(np.max(m[burn:])), float(np.std(m[burn:]))])
        res.add_table("betas", "Static OLS vs time-varying coefficients", ["term", "ols", "last", "min", "max", "sd_path"],
                      clean(rows))
        fe = np.asarray(r.forecasts_error, float).ravel()[burn:]
        from statsmodels.stats.diagnostic import acorr_ljungbox
        lb = acorr_ljungbox(fe[np.isfinite(fe)], lags=[min(10, len(fe) // 5)], return_df=True)
        res.add_test("Ljung-Box one-step errors", lb["lb_stat"].iloc[0], lb["lb_pvalue"].iloc[0], None, "no autocorrelation")
        last = {names[i]: float(st[i, -1]) for i in range(len(names))}
        res.fit_stats.update(clean({"aic": r.aic, "bic": r.bic, "llf": r.llf, **{f"last_{k}": v for k, v in last.items()}}))
        res.summary.update({"dependent": data.y, "regressors": names, "n_obs": len(y)})
        res.metrics.update(clean({"aic": r.aic, "n_obs": len(y), **{f"last_{k}": v for k, v in last.items() if k != "const"}}))
        return res
