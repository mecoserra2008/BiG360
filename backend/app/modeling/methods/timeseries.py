"""Time-series methods: ARIMA / SARIMA / ARIMAX / SARIMAX with bounded order search, and a stationarity
workbench (ADF, KPSS, ACF/PACF) (owner: model-studio)."""

from __future__ import annotations

import itertools
import time
import warnings as _w
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import diagnostics as dg
from .._core import next_bucket
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, downsample_idx, series


class ArimaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[int] = Field(default=[1, 0, 0], min_length=3, max_length=3, description="(p, d, q)")
    seasonal_order: list[int] = Field(default=[0, 0, 0, 0], min_length=4, max_length=4, description="(P, D, Q, s); s=0 none")
    trend: Literal["n", "c", "t", "ct"] | None = Field(default=None, description="default: c if d=0 else n")
    auto: bool = Field(default=False, description="search (p, q, P, Q) on a bounded grid by information criterion")
    auto_d: bool = Field(default=True, description="with auto: choose d by ADF/KPSS")
    criterion: Literal["aic", "bic", "aicc"] = "aic"
    max_p: int = Field(default=3, ge=0, le=8)
    max_q: int = Field(default=3, ge=0, le=8)
    max_P: int = Field(default=1, ge=0, le=3)
    max_Q: int = Field(default=1, ge=0, le=3)
    max_models: int = Field(default=64, ge=1, le=400)
    forecast_steps: int = Field(default=12, ge=0, le=240)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    exog_future: Literal["last", "dataset", "values"] = Field(
        default="last", description="future regressors: hold last value | rows after the last y in the dataset | provided values")
    exog_values: dict[str, list[float]] = Field(default_factory=dict, description="exog_future=values: {column: [h values]}")
    rolling_eval: bool = True
    eval_horizon: int = Field(default=1, ge=1, le=24)
    eval_folds: int = Field(default=24, ge=3, le=120)
    eval_initial: float = Field(default=0.7, gt=0.2, lt=0.95)
    max_iter: int = Field(default=200, ge=20, le=2000)

    @model_validator(mode="after")
    def _chk(self) -> "ArimaParams":
        if any(v < 0 for v in self.order + self.seasonal_order):
            raise ValueError("orders must be >= 0")
        if self.seasonal_order[3] == 1:
            raise ValueError("seasonal period s must be 0 (none) or >= 2")
        return self


def _fit_sarimax(y: np.ndarray, exog: np.ndarray | None, order: tuple[int, int, int],
                 seasonal: tuple[int, int, int, int], trend: str, max_iter: int, start_params: Any = None):
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    m = SARIMAX(y, exog=exog, order=order, seasonal_order=seasonal, trend=trend,
                enforce_stationarity=True, enforce_invertibility=True)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        return m.fit(disp=False, maxiter=max_iter, start_params=start_params)


def _aicc(r: Any) -> float:
    k = len(r.params)
    n = r.nobs
    return float(r.aic + (2 * k * (k + 1)) / max(n - k - 1, 1))


def _crit(r: Any, c: str) -> float:
    return _aicc(r) if c == "aicc" else float(getattr(r, c))


def _trend_for(d: int, D: int, trend: str | None, has_exog: bool) -> str:
    if trend:
        return trend
    return "c" if d + D == 0 else "n"


@register
class Arima(Method):
    id = "arima"
    family = "time_series"
    title = "ARIMA / SARIMA / ARIMAX / SARIMAX"
    description = ("State-space SARIMAX (statsmodels). Optional bounded order search by AIC/BIC/AICc, forecasts with "
                   "intervals (future exogenous inputs: hold-last, dataset rows or provided values), residual "
                   "diagnostics and rolling-origin evaluation vs naive / random-walk / seasonal-naive baselines.")
    shapes = ("timeseries",)
    min_x = 0
    max_x = 20
    Params = ArimaParams
    outputs = ("coefficients", "order search table", "in-sample fit", "forecast fan", "ACF/PACF", "OOS evaluation")
    diagnostics = ("Ljung-Box", "Jarque-Bera", "heteroskedasticity (H)", "ADF/KPSS of y", "residual ACF")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        per = 0.06 + n_rows * 4e-4 * (2 if getattr(params, "seasonal_order", [0, 0, 0, 0])[3] else 1)
        models = 1
        if getattr(params, "auto", False):
            s = params.seasonal_order[3]
            models = min(params.max_models, (params.max_p + 1) * (params.max_q + 1) *
                         ((params.max_P + 1) * (params.max_Q + 1) if s else 1))
        folds = params.eval_folds if getattr(params, "rolling_eval", False) else 0
        return 0.2 + per * (models + folds + 1)

    def fit(self, data: FitData, p: ArimaParams) -> Result:
        res = Result(self.id, self.family, self.title)
        x = list(data.x)
        y_col = data.y
        full = data.frame.select(["date", y_col] + x).to_pandas()
        # trailing rows where y is missing but x present can serve as future exog
        y_ok = full[y_col].notna() & np.isfinite(full[y_col].astype(float))
        if not y_ok.any():
            raise GuardrailError("too_few_obs", "y has no observations")
        first, last = int(np.argmax(y_ok.values)), int(len(y_ok) - 1 - np.argmax(y_ok.values[::-1]))
        body = full.iloc[first:last + 1]
        tail = full.iloc[last + 1:]
        mask = body[[y_col] + x].notna().all(axis=1)
        if (~mask).any():
            interior = int((~mask).sum())
            res.warn("gaps", f"{interior} interior period(s) with missing values were dropped; ARIMA assumes equally "
                     "spaced observations (consider missing.fill=ffill)")
        body = body[mask]
        y = body[y_col].to_numpy(dtype=float)
        ex = body[x].to_numpy(dtype=float) if x else None
        dates = body["date"].tolist()
        n = len(y)
        s = p.seasonal_order[3]
        if n < 24 or n < 3 * max(s, 1):
            raise GuardrailError("too_few_obs", f"only {n} observations for an ARIMA model (need >= 24 and >= 3 seasons)")
        # stationarity of y
        st_rows = []
        for d in range(3):
            r = dg.adf_kpss(np.diff(y, n=d) if d else y)
            st_rows.append([f"y diff {d}", r.get("adf_stat"), r.get("adf_p"), r.get("kpss_stat"), r.get("kpss_p"),
                             r.get("verdict")])
        res.add_table("stationarity", f"Stationarity of {y_col} (ADF H0: unit root; KPSS H0: stationary)",
                      ["series", "adf_stat", "adf_p", "kpss_stat", "kpss_p", "verdict"], clean(st_rows))
        d, D = p.order[1], p.seasonal_order[1]
        if p.auto and p.auto_d:
            d = dg.suggest_d(y)
            res.summary["d_selected_by"] = "ADF/KPSS"
        if d == 0 and st_rows[0][5] == "unit root":
            res.warn("nonstationary", f"{y_col} looks non-stationary (ADF/KPSS) but d=0: consider d=1")
        trend = _trend_for(d, D, p.trend, bool(x))
        order = (p.order[0], d, p.order[2])
        seas = tuple(p.seasonal_order) if s else (0, 0, 0, 0)
        search_rows = []
        if p.auto:
            grid = list(itertools.product(range(p.max_p + 1), range(p.max_q + 1),
                                          range(p.max_P + 1) if s else [0], range(p.max_Q + 1) if s else [0]))
            grid.sort(key=lambda g: (sum(g), g))
            if len(grid) > p.max_models:
                res.info("search_truncated", f"order grid has {len(grid)} models; evaluated the {p.max_models} simplest")
                grid = grid[:p.max_models]
            best = None
            for (pp, qq, PP, QQ) in grid:
                data.check()
                o, so = (pp, d, qq), ((PP, D, QQ, s) if s else (0, 0, 0, 0))
                t0 = time.perf_counter()
                try:
                    r = _fit_sarimax(y, ex, o, so, trend, min(p.max_iter, 100))
                    c = _crit(r, p.criterion)
                    ok = bool(r.mle_retvals.get("converged", True)) if hasattr(r, "mle_retvals") and r.mle_retvals else True
                except Exception as e:  # noqa: BLE001
                    c, ok = float("nan"), False
                    r = None
                search_rows.append([str(o), str(so) if s else "", c, ok, round(time.perf_counter() - t0, 3)])
                if r is not None and np.isfinite(c) and (best is None or c < best[0]):
                    best = (c, o, so)
            if best is None:
                raise GuardrailError("fit_failed", "no ARIMA model in the search grid could be estimated")
            order, seas = best[1], best[2]
            search_rows.sort(key=lambda r_: (r_[2] if r_[2] == r_[2] else 1e18))
            res.add_table("order_search", f"Order search by {p.criterion.upper()} ({len(search_rows)} models, lower is better)",
                          ["order", "seasonal_order", p.criterion, "converged", "seconds"], clean(search_rows[:25]))
            dg.multiple_testing_note(res, len(search_rows), "candidate models")
            res.info("order_search_overfit", f"order chosen among {len(search_rows)} candidates: in-sample criteria "
                     "favour flexible models; judge it on the out-of-sample table")
        data.check()
        r = _fit_sarimax(y, ex, order, seas, trend, p.max_iter)
        if hasattr(r, "mle_retvals") and r.mle_retvals and not r.mle_retvals.get("converged", True):
            res.warn("not_converged", "maximum likelihood did not converge (try a simpler order or more iterations)")
        names = list(r.model.param_names)
        if x:
            names = [x[int(nm[1:]) - 1] if nm.startswith("x") and nm[1:].isdigit() else nm for nm in names]
        ci = np.asarray(r.conf_int(alpha=p.alpha))
        res.coefficients = coef_rows(names, r.params, r.bse, r.zvalues, r.pvalues, ci)
        k = len(r.params)
        dg.check_obs(res, n, k)
        label = ("SARIMAX" if s and x else "SARIMA" if s else "ARIMAX" if x else "ARIMA") + \
            f"{order}" + (f"x{seas}" if s else "")
        res.title = label
        res.summary.update({"dependent": y_col, "exog": x, "n_obs": n, "order": list(order),
                            "seasonal_order": list(seas), "trend": trend, "model": label,
                            "start": clean(dates[0]), "end": clean(dates[-1])})
        res.fit_stats.update(clean({"aic": r.aic, "bic": r.bic, "hqic": r.hqic, "aicc": _aicc(r), "llf": r.llf,
                                    "sigma2": r.params[-1] if names[-1] == "sigma2" else None, "n_params": k}))
        res.metrics.update(clean({"aic": r.aic, "bic": r.bic, "n_obs": n,
                                  "rmse_in_sample": float(np.sqrt(np.nanmean(np.asarray(r.resid)[d + D * s:] ** 2)))}))
        # diagnostics
        burn = d + D * s
        resid = np.asarray(r.resid, dtype=float)[burn:]
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            lags = sorted({min(10, max(2, n // 5)), 2 * s} - {0}) if s else [min(10, max(2, n // 5))]
            from statsmodels.stats.diagnostic import acorr_ljungbox

            lb = acorr_ljungbox(resid, lags=lags, model_df=sum(order[::2]) + sum(seas[0:3:2]), return_df=True)
            for L, row in lb.iterrows():
                res.add_test(f"Ljung-Box (lag {L})", row["lb_stat"], row["lb_pvalue"], L, "no residual autocorrelation")
            try:
                jb = r.test_normality("jarquebera")[0]
                res.add_test("Jarque-Bera", jb[0], jb[1], 2, "normal residuals")
                h = r.test_heteroskedasticity("breakvar")[0]
                res.add_test("Heteroskedasticity (H)", h[0], h[1], None, "constant residual variance (first vs last third)")
            except Exception as e:  # pragma: no cover
                res.info("diag_failed", f"state-space diagnostics failed: {e}")
        fitted = np.asarray(r.fittedvalues, dtype=float)
        fitted[:burn] = np.nan
        idx = downsample_idx(n)
        res.line_plot("fit", "Actual vs in-sample one-step fit", [series("actual", [dates[i] for i in idx], y[idx]),
                                                                 series("fitted", [dates[i] for i in idx], fitted[idx])],
                      "date", y_col)
        dg.residual_diagnostics(res, y[burn:], fitted[burn:], resid, None, dates[burn:], True, tests=False)
        dg.acf_pacf_plot(res, "acf_y", y, min(36, n // 3), f"ACF / PACF of {y_col}")
        if d:
            dg.acf_pacf_plot(res, "acf_y_diff", np.diff(y, n=d), min(36, n // 3), f"ACF / PACF of diff^{d}({y_col})")
        # forecast
        if p.forecast_steps:
            h = p.forecast_steps
            fx = None
            if x:
                fx, h = self._future_exog(res, p, x, ex, tail, h)
            if h:
                fdates: list[date] = []
                cur = dates[-1]
                for _ in range(h):
                    cur = next_bucket(cur, data.freq) if data.freq != "D" else _next_weekday(cur)
                    fdates.append(cur)
                with _w.catch_warnings():
                    _w.simplefilter("ignore")
                    fc = r.get_forecast(steps=h, exog=fx)
                    m = np.asarray(fc.predicted_mean, dtype=float)
                    c95 = np.asarray(fc.conf_int(alpha=p.alpha))
                    c80 = np.asarray(fc.conf_int(alpha=0.2))
                res.forecast = clean({"dates": fdates, "mean": m, "lower": c95[:, 0], "upper": c95[:, 1],
                                      "lower_80": c80[:, 0], "upper_80": c80[:, 1], "alpha": p.alpha, "steps": h,
                                      "exog_future": p.exog_future if x else None,
                                      "history": {"dates": dates[-min(n, 120):], "values": y[-min(n, 120):]}})
        # rolling origin
        if p.rolling_eval:
            start_params = r.params

            def fc_fn(t: int, h: int) -> np.ndarray:
                rr = _fit_sarimax(y[:t], ex[:t] if ex is not None else None, order, seas, trend,
                                  min(p.max_iter, 100), start_params=start_params)
                with _w.catch_warnings():
                    _w.simplefilter("ignore")
                    return np.asarray(rr.forecast(steps=h, exog=ex[t:t + h] if ex is not None else None), dtype=float)

            ev = dg.rolling_origin(y, fc_fn, dates, initial=max(int(n * p.eval_initial), 24), horizon=p.eval_horizon,
                                   max_folds=p.eval_folds, season=s, check=data.check)
            ev["note"] = "model re-estimated at every origin with the selected order" + \
                ("; exogenous regressors use realized values (conditional forecasts)" if x else "")
            dg.add_eval_metrics(res, ev)
        return res

    @staticmethod
    def _future_exog(res: Result, p: ArimaParams, x: list[str], ex: np.ndarray, tail: pd.DataFrame,
                     h: int) -> tuple[np.ndarray | None, int]:
        if p.exog_future == "values":
            missing = [c for c in x if c not in p.exog_values]
            if missing:
                raise GuardrailError("exog_values", f"exog_values missing for {missing}")
            h = min(h, min(len(p.exog_values[c]) for c in x))
            return np.column_stack([np.asarray(p.exog_values[c][:h], dtype=float) for c in x]), h
        if p.exog_future == "dataset":
            t = tail[x].dropna()
            if t.empty:
                res.warn("no_future_exog", "exog_future=dataset but the dataset has no rows after the last y; "
                         "falling back to holding the last value")
            else:
                h2 = min(h, len(t))
                if h2 < h:
                    res.info("forecast_shortened", f"forecast limited to {h2} steps (future regressor rows available)")
                return t.to_numpy(dtype=float)[:h2], h2
        res.info("exog_hold_last", "future exogenous regressors held at their last observed value")
        return np.repeat(ex[-1:], h, axis=0), h


def _next_weekday(d: date) -> date:
    from datetime import timedelta

    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


class StationarityParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regression: Literal["c", "ct"] = Field(default="c", description="c = level-stationary, ct = trend-stationary")
    max_diff: int = Field(default=1, ge=0, le=2, description="also test differences up to this order")
    nlags: int = Field(default=24, ge=4, le=120, description="ACF/PACF lags")


@register
class Stationarity(Method):
    id = "stationarity"
    family = "time_series"
    title = "Stationarity & autocorrelation (ADF, KPSS, ACF/PACF)"
    description = "Unit-root (ADF) and stationarity (KPSS) tests for y and x columns, levels and differences, with ACF/PACF."
    shapes = ("timeseries",)
    needs_y = False
    min_x = 0
    Params = StationarityParams
    outputs = ("test table", "suggested differencing order", "ACF/PACF per series")
    diagnostics = ("ADF", "KPSS")

    def validate_roles(self, study: Any) -> None:
        super().validate_roles(study)
        if not study.y and not study.x:
            raise GuardrailError("missing_columns", "pick at least one column (y or x)")

    def fit(self, data: FitData, p: StationarityParams) -> Result:
        res = Result(self.id, self.family, self.title)
        cols = ([data.y] if data.y else []) + list(data.x)
        rows, sugg = [], {}
        for c in cols:
            data.check()
            v = data.frame[c].cast(float).to_numpy()
            v = v[np.isfinite(v)]
            for d in range(p.max_diff + 1):
                r = dg.adf_kpss(np.diff(v, n=d) if d else v, p.regression)
                rows.append([c, d, r.get("n"), r.get("adf_stat"), r.get("adf_p"), r.get("adf_lags"), r.get("kpss_stat"),
                             r.get("kpss_p"), r.get("verdict")])
                res.add_test(f"ADF {c} (diff {d})", r.get("adf_stat"), r.get("adf_p"), r.get("adf_lags"), "unit root",
                             group="stationarity")
                res.add_test(f"KPSS {c} (diff {d})", r.get("kpss_stat"), r.get("kpss_p"), None, "stationary",
                             group="stationarity")
            sugg[c] = dg.suggest_d(v)
            dg.acf_pacf_plot(res, f"acf_{c}", v, p.nlags, f"ACF / PACF of {c}")
            if sugg[c]:
                dg.acf_pacf_plot(res, f"acf_{c}_diff", np.diff(v, n=sugg[c]), p.nlags, f"ACF / PACF of diff^{sugg[c]}({c})")
            if v.size:
                idx = downsample_idx(len(v))
                dts = data.frame.filter(data.frame[c].is_not_null())["date"].to_list()
                res.line_plot(f"series_{c}", c, [series(c, [dts[i] for i in idx if i < len(dts)], v[idx])], "date", c)
        res.add_table("stationarity", "ADF (H0: unit root) and KPSS (H0: stationary)",
                      ["variable", "diff", "n", "adf_stat", "adf_p", "adf_lags", "kpss_stat", "kpss_p", "verdict"],
                      clean(rows))
        res.summary.update({"columns": cols, "suggested_d": sugg, "regression": p.regression})
        res.metrics.update({f"d_{c}": v for c, v in sugg.items()})
        return res
