"""Model Studio: filtered conformal ellipsoid on any multivariate selection (owner: conformal; P3 of
docs/CONFORMAL_TECHNICALS_PLAN.md). Core: app/modeling/_conformal.py (embedded in exported scripts)."""

from __future__ import annotations

import math
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .. import _conformal as CF
from .._core import next_bucket
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, ts_band

MAX_COORDS = 60


class ConformalParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filter: Literal["ewma", "static_shrink", "ewma_vol_static_corr"] = Field(
        default="ewma", description="shape filter (frozen, emits mean / covariance before the outcome)")
    transform: Literal["none", "diff", "log_diff", "pct_change"] = Field(
        default="none", description="applied to every series before the ellipsoid (levels -> changes)")
    alpha: float = Field(default=0.1, gt=0.001, lt=0.5, description="miscoverage: 0.1 = 90% set")
    pretrain_frac: float = Field(default=0.3, ge=0.1, le=0.7, description="first share of observations: filter fit / burn-in")
    window: int = Field(default=250, ge=30, le=5000, description="rolling calibration window (realised scores)")
    min_cal: int = Field(default=50, ge=20, le=2000)
    aci_gamma: float = Field(default=0.005, ge=0, le=0.1, description="ACI step (0 = plain split conformal)")
    lam: float = Field(default=0.94, gt=0.5, lt=1)
    lam_corr: float = Field(default=0.985, gt=0.5, lt=1)
    shrink: float = Field(default=0.1, ge=0, le=1)
    panel_variable: str | None = Field(default=None, description="panel datasets: the variable whose entities become the "
                                       "coordinates (default y, else the first x)")
    max_entities: int = Field(default=40, ge=2, le=MAX_COORDS)


def _matrix(data: FitData, p: ConformalParams) -> tuple[pd.DataFrame, str]:
    if data.is_panel:
        var = p.panel_variable or data.y or (data.x[0] if data.x else None)
        if not var or var not in data.frame.columns:
            raise GuardrailError("missing_x", "pick the panel variable (y or one x) whose entities form the vector")
        f = data.frame.select("date", "entity_id", var).to_pandas()
        W = f.pivot_table(index="date", columns="entity_id", values=var, aggfunc="last").sort_index()
        keep = W.notna().mean().sort_values(ascending=False).index[: p.max_entities]
        W = W[keep]
        kind = f"panel ({var} across {W.shape[1]} entities)"
    else:
        cols = ([data.y] if data.y else []) + [c for c in data.x if c != data.y]
        W = data.frame.select(["date"] + cols).to_pandas().set_index("date").sort_index()
        kind = "time series"
    W = W.astype(float)
    if p.transform == "diff":
        W = W.diff()
    elif p.transform == "log_diff":
        W = np.log(W.where(W > 0)).diff()
    elif p.transform == "pct_change":
        W = W.pct_change(fill_method=None)
    W = W.replace([np.inf, -np.inf], np.nan).dropna(how="any")
    if W.shape[1] < 2:
        raise GuardrailError("missing_x", "a conformal ellipsoid needs at least 2 series (or 2 panel entities)")
    if W.shape[1] > MAX_COORDS:
        raise GuardrailError("too_many_x", f"at most {MAX_COORDS} coordinates")
    return W, kind


@register
class ConformalEllipsoid(Method):
    id = "conformal_ellipsoid"
    family = "multivariate"
    title = "Filtered conformal ellipsoid (multivariate)"
    description = ("Calibrated joint prediction set for the next observation of a vector of series (Limmer 2026): a "
                   "frozen filter (EWMA / static shrinkage / EWMA vol + static correlation) gives the shape, split "
                   "conformal calibration of the squared Mahalanobis score over a rolling window gives the radius "
                   "(optionally ACI). Outputs out-of-sample coverage vs target, set size, the anomaly score history, "
                   "per-variable projection bands for the next step and the latest attribution. Model-based: coverage "
                   "is marginal over time, never a statement about one date. Panel datasets: one variable across entities.")
    shapes = ("timeseries", "panel")
    needs_y = False
    min_x = 1
    Params = ConformalParams
    outputs = ("coverage vs target", "score history with radius (ts_band)", "next-step projection bands (forecast)",
               "anomaly dates", "attribution of the latest observation")
    diagnostics = ("binomial coverage test", "per-coordinate projection coverage")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + n_rows * max(n_x, 2) ** 2 * 2e-6

    def fit(self, data: FitData, p: ConformalParams) -> Result:
        res = Result(self.id, self.family, self.title)
        W, kind = _matrix(data, p)
        X = W.to_numpy(float)
        T, N = X.shape
        if T < max(120, p.min_cal + 40):
            raise GuardrailError("too_few_obs", f"{T} complete observations are too few (need >= {max(120, p.min_cal + 40)})")
        out = CF.run(X, p.filter, p.alpha, p.pretrain_frac, p.window, p.min_cal, p.aci_gamma, p.lam, p.lam_corr, p.shrink)
        dates = list(W.index)
        cols = [str(c) for c in W.columns]
        tgt = 1 - p.alpha
        cov, n = out["coverage"], out["n_test"]
        if n:
            from scipy.stats import binomtest  # noqa: PLC0415

            k = int(round(cov * n))
            bt = binomtest(k, n, tgt)
            res.add_test("Coverage = target (binomial, test period)", cov, bt.pvalue, n,
                         f"realised coverage equals {tgt:.0%}", group="calibration",
                         passed=abs(cov - tgt) <= 0.015, note="marginal over time (exchangeability is approximate)")
        if out["member_coverage"] is not None:
            res.add_table("projection_coverage", "Per-coordinate coverage of the joint set's projections (test period)",
                          ["variable", "coverage"], clean([[c, float(v)] for c, v in zip(cols, out["member_coverage"], strict=True)]),
                          note="projections of a joint set are simultaneous bands, so they over-cover as single-variable bands")
        R, q = out["R"], out["q"]
        # score history: R[t] belongs to observation t + 1 vs the radius in force at t
        sx = dates[1:]
        ts_band(res, "score", "Squared Mahalanobis score vs conformal radius", sx,
                [("score", R[:-1]), ("radius", q[:-1])], y_label="score",
                vlines=[{"x": dates[out["t_fit"]], "label": "calibration starts"}] if out["t_fit"] < T else None)
        anom = [(dates[t + 1], float(R[t]), float(q[t])) for t in range(T - 1)
                if out["test"][t] and np.isfinite(q[t]) and R[t] > q[t]]
        anom = sorted(anom, key=lambda a: -a[1] / a[2])[:25]
        rows = []
        for d, r_, q_ in anom:
            t = dates.index(d) - 1
            c, _ = CF.contributions(X[t + 1] - out["mu"][t], out["S"][t])
            rows.append([d, r_, q_, cols[int(np.argmax(c))]])
        res.add_table("anomalies", "Largest exceedances of the radius (test period)", ["date", "score", "radius", "top contributor"],
                      clean(rows))
        # latest attribution
        tl = T - 2
        c, zc = CF.contributions(X[T - 1] - out["mu"][tl], out["S"][tl])
        res.add_table("attribution_last", f"Attribution of the last observation ({dates[-1]})",
                      ["variable", "value", "contribution", "share", "z_cond"],
                      clean([[cols[i], X[T - 1, i], c[i], c[i] / max(c.sum(), 1e-300), zc[i]] for i in range(N)]))
        # next step: projection bands per variable from the last emitted law
        qn = out["q_next"]
        mu, S = out["mu"][T - 1], out["S"][T - 1]
        nxt = next_bucket(dates[-1], data.freq) if data.freq else dates[-1]
        by_var = []
        for i, cname in enumerate(cols):
            a = np.zeros(N)
            a[i] = 1.0
            lo, hi = CF.projection(mu, S, qn, a) if np.isfinite(qn) else (math.nan, math.nan)
            by_var.append({"variable": cname, "mean": [mu[i]], "lower": [lo], "upper": [hi]})
        res.forecast = clean({"dates": [nxt], "mean": by_var[0]["mean"], "lower": by_var[0]["lower"],
                              "upper": by_var[0]["upper"], "alpha": p.alpha, "steps": 1, "variable": cols[0],
                              "history": {"dates": dates[-120:], "values": X[-120:, 0]}, "by_variable": by_var,
                              "note": "projections of the joint (simultaneous) set"})
        res.metrics.update(clean({"coverage": cov, "target": tgt, "n_test": n, "w_proj": out["w_proj"],
                                  "log_volume": out["log_volume"], "q_next": qn, "n_coordinates": N}))
        res.fit_stats.update(clean({"coverage": cov, "target": tgt, "n_test": n, "n_obs": T, "w_proj": out["w_proj"],
                                    "log_volume": out["log_volume"]}))
        res.summary.update(clean({"variables": cols, "layout": kind, "filter": p.filter, "transform": p.transform,
                                  "radius": "aci" if p.aci_gamma > 0 else "plain", "t_fit": out["t_fit"],
                                  "first_date": dates[0], "last_date": dates[-1], "model_based": True}))
        res.info("model_based", "Model-based: coverage is marginal over time ('calibrated to X% over the test period'), "
                                "not the probability for any single date. See docs/CONFORMAL_VALIDATION.md.")
        if N > 50:
            res.warn("large_n", "joint sets grow conservative as N rises (the paper's sweet spot is N ~ 20-50)")
        if n and abs(cov - tgt) > 0.03:
            res.warn("miscalibrated", f"test coverage {cov:.1%} vs target {tgt:.0%}: regime drift - try ACI or a shorter window")
        return res
