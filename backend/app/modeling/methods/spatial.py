"""Spatial econometrics and spillover methods (owner: event-models, plan B5): spatial_lag (SAR), spatial_error (SEM),
spatial_durbin (SDM), moran_lisa (global Moran's I, LISA, Getis-Ord Gi*), connectedness (Diebold-Yilmaz), and
local_projection (Jorda, optionally with a spatially lagged shock).

Spatial weights come from `app.events_models.weights.get_weights(w_kind, codes, as_of)` (linkage spatial_weights:
contiguity / inverse_distance / knn / trade / io / banking; Natural Earth fallback for the geographic kinds) and
need a country-level dataset (entity_level = country, entity ids ISO2). The W actually used (non-zero triplets) is
stored in the result summary so exported scripts are standalone. Estimation core: app/modeling/_spatial.py.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats

from .. import _spatial as SP
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, series

WKind = Literal["contiguity", "inverse_distance", "knn", "trade", "trade_exports", "io", "banking"]


def _w_triplets(codes: list[str], W: np.ndarray) -> dict[str, Any]:
    i, j = np.nonzero(W)
    return {"codes": list(codes), "ij": [[int(a), int(b), float(W[a, b])] for a, b in zip(i, j)]}


def get_w(codes: list[str], kind: str, k: int, as_of: date | None) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    from app.events_models.weights import WeightsUnavailable, get_weights  # noqa: PLC0415

    try:
        return get_weights(kind, codes, as_of, k=k)
    except WeightsUnavailable as e:
        raise GuardrailError("weights_unavailable", str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise GuardrailError("weights_unavailable", f"spatial weights {kind!r} unavailable: {e}") from e


def _is_iso2(ids: list[str]) -> bool:
    return bool(ids) and all(isinstance(x, str) and (len(x) == 2 and x.isalpha() and x.isupper()) for x in ids)


def balanced_block(data: FitData, cols: list[str]) -> tuple[pd.DataFrame, list[str], list[Any]]:
    """Listwise-complete rows, then the largest balanced block: entities present in >= 80% of the periods, then the
    periods complete for all of them. Returns (df sorted time-major, codes, dates)."""
    df, _ = data.complete()
    if "entity_id" not in df.columns:
        raise GuardrailError("bad_shape", "spatial models need a panel or cross-section of countries (entity_id)")
    ids = sorted(df["entity_id"].astype(str).unique())
    if not _is_iso2(ids):
        raise GuardrailError("not_countries", "spatial models need a country-level dataset (entity_level = country; "
                             "entity ids ISO2)")
    T = df["date"].nunique()
    cnt = df.groupby("entity_id")["date"].nunique()
    keep = sorted(cnt[cnt >= max(1, int(0.8 * T))].index.astype(str))
    d = df[df["entity_id"].isin(keep)]
    per = d.groupby("date")["entity_id"].nunique()
    dates = sorted(per[per == len(keep)].index)
    d = d[d["date"].isin(dates)].sort_values(["date", "entity_id"]).reset_index(drop=True)
    return d, keep, dates


class SpatialParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    w_kind: WKind = Field(default="contiguity", description="spatial weights (linkage spatial_weights; geographic "
                          "kinds fall back to Natural Earth). Row-standardized for estimation.")
    k: int = Field(default=5, ge=1, le=20, description="neighbours for w_kind = knn (fallback only)")
    w_as_of: date | None = Field(default=None, description="point-in-time W (trade / io / banking periods)")
    fe: Literal["entity", "twoway", "none"] = Field(default="entity", description="within transform (cross-section: none)")
    effects_draws: int = Field(default=500, ge=0, le=5000, description="Monte-Carlo draws for effect inference")
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


class _SpatialRegression(Method):
    family = "spatial"
    shapes = ("panel", "cross_section")
    min_x = 1
    Params = SpatialParams
    outputs = ("coefficients", "spatial parameter", "direct / indirect / total effects", "LM diagnostics",
               "impact matrix spillovers by country")
    diagnostics = ("LM lag / error (robust)", "Moran's I of residuals")
    model = "sar"

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.5 + n_rows * (n_x + 2) * 3e-6 + params.effects_draws * 2e-4

    def fit(self, data: FitData, p: SpatialParams) -> Result:
        res = Result(self.id, self.family, self.title)
        d, codes, dates = balanced_block(data, [])
        have, Wraw, meta = get_w(codes, p.w_kind, p.k, p.w_as_of)
        if len(have) < 5:
            raise GuardrailError("too_few_units", f"only {len(have)} countries placeable in W {p.w_kind!r}")
        d = d[d["entity_id"].isin(have)].sort_values(["date", "entity_id"])
        codes = [c for c in codes if c in have]
        idx = [have.index(c) for c in codes]
        Wraw = Wraw[np.ix_(idx, idx)]
        N, T = len(codes), len(dates)
        if N * T < len(data.x) + 10:
            raise GuardrailError("too_few_obs", f"{N} units x {T} periods are too few")
        y = d[data.y].to_numpy(float)
        X = d[data.x].to_numpy(float)
        fe = "none" if T == 1 else p.fe
        data.check()
        if self.model == "sem":
            f = SP.sem_panel(y, X, Wraw, N, fe=fe)
        else:
            f = SP.sar_panel(y, X, Wraw, N, fe=fe, durbin=self.model == "sdm")
        k = f["k"]
        c0 = 1 if f.get("has_const") else 0
        names = (["const"] if c0 else []) + list(data.x) + ([f"W*{v}" for v in data.x] if f["durbin"] else [])
        z = stats.norm.ppf(1 - p.alpha / 2)
        est, se = f["beta"], f["se"][:k]
        tt = np.where(se > 0, est / np.where(se > 0, se, 1), np.nan)
        pv = 2 * stats.norm.sf(np.abs(tt))
        res.coefficients = coef_rows(names, est, se, tt, pv, np.column_stack([est - z * se, est + z * se]))
        par = "lambda" if self.model == "sem" else "rho"
        rs = f["se"][k]
        res.coefficients.append(coef_rows([par], [f["rho"]], [rs], [f["rho"] / rs if rs > 0 else np.nan],
                                          [2 * stats.norm.sf(abs(f["rho"] / rs)) if rs > 0 else np.nan],
                                          [[f["rho"] - z * rs, f["rho"] + z * rs]])[0])
        nx = len(data.x)
        eff = SP.effects(f, nx, draws=p.effects_draws, seed=data.seed, alpha=p.alpha)
        rows = []
        for e, v in zip(eff, data.x):
            for kind in ("direct", "indirect", "total"):
                rows.append([v, kind, e[kind], e[f"{kind}_se"], e[f"{kind}_lo"], e[f"{kind}_hi"], e[f"{kind}_p"]])
        res.add_table("effects", "Average direct / indirect / total effects (LeSage-Pace, Monte-Carlo inference)",
                      ["variable", "effect", "estimate", "std_error", "ci_low", "ci_high", "p_value"], clean(rows))
        # per-country spillovers of the first regressor
        S = SP.impact_matrix(f, 0, nx)
        off = S - np.diag(np.diag(S))
        res.add_table("spillovers_by_country", f"Spillovers of a unit change in {data.x[0]} by country "
                      "(impact matrix: transmit = effect on all others, receive = effect from all others)",
                      ["country", "own", "transmit", "receive"],
                      clean([[c, float(S[i, i]), float(off[:, i].sum()), float(off[i, :].sum())] for i, c in enumerate(codes)]))
        try:
            lm = SP.lm_tests(y, X, Wraw, N, fe=fe)
            for key, nm in (("lm_lag", "LM lag"), ("lm_error", "LM error"), ("rlm_lag", "Robust LM lag"),
                            ("rlm_error", "Robust LM error")):
                res.add_test(nm, lm[key][0], lm[key][1], 1, "no spatial dependence of that form (OLS residuals)",
                             group="specification", passed=None)
        except Exception:  # noqa: BLE001
            pass
        try:
            r = f["resid"].reshape(T, N)
            mi = SP.moran_perm(r.mean(axis=0) if T > 1 else r[0], Wraw, 499, data.seed)
            res.add_test("Moran's I of residuals (time-averaged)", mi["I"], mi["p_sim"], None,
                         "no residual spatial autocorrelation")
        except Exception:  # noqa: BLE001
            pass
        res.fit_stats.update(clean({"llf": f["llf"], "aic": f["aic"], "bic": f["bic"], "pseudo_r2": f["pseudo_r2"],
                                    "sigma2": f["sigma2"], "n_obs": N * T, "n_units": N, "n_periods": T}))
        res.summary.update(clean({"w_kind": p.w_kind, "w_source": meta.get("source"), "fe": fe, "n_units": N,
                                  "n_periods": T, par: f["rho"], "units": codes,
                                  "w": _w_triplets(codes, Wraw)}))
        res.metrics.update(clean({par: f["rho"], "llf": f["llf"], "aic": f["aic"]}))
        if meta.get("source") != "linkage":
            res.info("w_fallback", f"W {p.w_kind!r} from the Natural Earth fallback (linkage spatial_weights not available)")
        if fe != "none":
            res.info("within", "fixed effects by within transform; sigma2 not bias-corrected (Lee-Yu) for short T")
        return res


@register
class SpatialLag(_SpatialRegression):
    id = "spatial_lag"
    title = "Spatial lag model (SAR)"
    description = ("ML spatial autoregressive model y = rho W y + X b + e on a country panel (entity / two-way FE) or "
                   "cross-section; LeSage-Pace direct / indirect / total effects; LM diagnostics. W by kind "
                   "(contiguity, distance, kNN, trade, input-output, banking).")
    model = "sar"


@register
class SpatialError(_SpatialRegression):
    id = "spatial_error"
    title = "Spatial error model (SEM)"
    description = ("ML spatial error model y = X b + u, u = lambda W u + e on a country panel or cross-section; "
                   "W by kind; LM diagnostics.")
    model = "sem"


@register
class SpatialDurbin(_SpatialRegression):
    id = "spatial_durbin"
    title = "Spatial Durbin model (SDM)"
    description = ("ML spatial Durbin model y = rho W y + X b + W X theta + e (country panel / cross-section); "
                   "direct / indirect / total effects with simulation inference; per-country spillovers.")
    model = "sdm"


# ------------------------------------------------------------------------------------------------ Moran / LISA


class MoranParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    w_kind: WKind = "contiguity"
    k: int = Field(default=5, ge=1, le=20)
    w_as_of: date | None = None
    period: Literal["latest", "mean"] = Field(default="latest", description="panel: map the latest period or the time mean")
    permutations: int = Field(default=999, ge=99, le=9999)
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class MoranLisa(Method):
    id = "moran_lisa"
    family = "spatial"
    title = "Spatial autocorrelation (Moran's I, LISA, Getis-Ord Gi*)"
    description = ("Global Moran's I with permutation inference, local Moran (LISA) clusters HH / LL / HL / LH and "
                   "Getis-Ord Gi* hot / cold spots of y across countries (latest period or time mean).")
    shapes = ("panel", "cross_section")
    needs_y = True
    min_x = 0
    max_x = 0
    Params = MoranParams
    outputs = ("global Moran's I", "LISA table", "Gi* hot / cold spots")

    def fit(self, data: FitData, p: MoranParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, _ = data.complete()
        if "entity_id" not in df.columns or not _is_iso2(sorted(df["entity_id"].astype(str).unique())):
            raise GuardrailError("not_countries", "moran_lisa needs a country-level dataset (entity ids ISO2)")
        if p.period == "latest":
            last = df["date"].max()
            df = df[df["date"] == last]
        v = df.groupby("entity_id")[data.y].mean()
        have, Wraw, meta = get_w(list(v.index), p.w_kind, p.k, p.w_as_of)
        if len(have) < 8:
            raise GuardrailError("too_few_units", f"only {len(have)} countries placeable")
        x = v.reindex(have).to_numpy(float)
        mg = SP.moran_perm(x, Wraw, p.permutations, data.seed)
        ml = SP.lisa_perm(x, Wraw, p.permutations, data.seed)
        gz, gp = SP.gi_star(x, Wraw)
        res.add_test("Global Moran's I", mg["I"], mg["p_sim"], None, "no spatial autocorrelation (random permutation)",
                     group="spatial", passed=None, note=f"z_sim = {mg['z_sim']:.3f}, E[I] = {-1 / (len(x) - 1):.4f}")
        q = {1: "HH", 2: "LH", 3: "LL", 4: "HL"}
        rows = []
        for i, c in enumerate(have):
            lab = q[int(ml["q"][i])] if ml["p_sim"][i] < p.alpha else "ns"
            gb = "ns" if gp[i] >= 0.10 else (("hot" if gz[i] > 0 else "cold") + ("99" if gp[i] < 0.01 else "95" if gp[i] < 0.05 else "90"))
            rows.append([c, float(x[i]), float(ml["Is"][i]), float(ml["p_sim"][i]), lab, float(gz[i]), float(gp[i]), gb])
        res.add_table("lisa", "Local indicators (LISA quadrant at alpha; Gi* bins at 90/95/99%)",
                      ["country", "value", "local_i", "p_sim", "cluster", "gi_z", "gi_p", "hotspot"], clean(rows))
        res.summary.update(clean({"moran_i": mg["I"], "p_sim": mg["p_sim"], "z_sim": mg["z_sim"], "n_units": len(have),
                                  "w_kind": p.w_kind, "w_source": meta.get("source"), "period": p.period,
                                  "n_hh": sum(r[4] == "HH" for r in rows), "n_ll": sum(r[4] == "LL" for r in rows),
                                  "w": _w_triplets(have, Wraw), "units": have}))
        res.metrics.update(clean({"moran_i": mg["I"], "p_sim": mg["p_sim"]}))
        return res


# ------------------------------------------------------------------------------------------------ connectedness


class ConnParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lags: int = Field(default=1, ge=1, le=10)
    horizon: int = Field(default=10, ge=1, le=60)
    ridge: float = Field(default=0.0, ge=0, le=1, description="ridge shrinkage of the VAR (relative to trace(X'X)/k)")
    window: int | None = Field(default=None, ge=30, le=2000, description="rolling window (periods) for the total "
                               "connectedness index; null = full sample only")
    step: int = Field(default=5, ge=1, le=60)


@register
class Connectedness(Method):
    id = "connectedness"
    family = "multivariate"
    title = "Diebold-Yilmaz connectedness"
    description = ("Generalized FEVD (Pesaran-Shin) of a VAR on y + x series: total connectedness index, directional "
                   "to / from / net per series, pairwise net connectedness; optional rolling total index.")
    shapes = ("timeseries",)
    needs_y = False
    min_x = 2
    Params = ConnParams
    outputs = ("total connectedness", "to / from / net table", "pairwise net matrix", "rolling total (plot)")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        n_win = (n_rows // max(params.step, 1)) if params.window else 1
        return 0.2 + n_win * n_x ** 2 * 1e-4

    def fit(self, data: FitData, p: ConnParams) -> Result:
        res = Result(self.id, self.family, self.title)
        cols = ([data.y] if data.y else []) + [c for c in data.x if c != data.y]
        f = data.frame.select(["date"] + cols).to_pandas().dropna()
        if len(cols) < 3:
            raise GuardrailError("missing_x", "connectedness needs at least 3 series")
        if len(f) < 3 * len(cols) * p.lags + 20:
            raise GuardrailError("too_few_obs", f"{len(f)} observations are too few for a {len(cols)}-variable VAR")
        t = SP.dy_connectedness(f[cols].to_numpy(float), p.lags, p.horizon, p.ridge)
        res.summary.update(clean({"total": t["total"], "variables": cols, "n_obs": len(f), "lags": p.lags,
                                  "horizon": p.horizon}))
        res.metrics["total_connectedness"] = clean(t["total"])
        res.add_table("directional", "Directional connectedness (%)", ["series", "to_others", "from_others", "net"],
                      clean([[c, t["to"][i], t["from"][i], t["net"][i]] for i, c in enumerate(cols)]))
        res.add_table("pairwise_net", "Net pairwise connectedness row -> column (%)", ["from \\ to"] + cols,
                      clean([[a] + [float(t["pairwise_net"][i, j]) for j in range(len(cols))] for i, a in enumerate(cols)]))
        res.add_table("fevd", "Generalized FEVD (%): row = receiver, column = source", ["series"] + cols,
                      clean([[a] + [float(v) for v in t["theta"][i]] for i, a in enumerate(cols)]))
        if p.window and len(f) > p.window + p.step:
            roll = SP.rolling_connectedness(f.set_index("date")[cols], p.window, p.step, p.lags, p.horizon, p.ridge)
            res.line_plot("rolling_total", f"Total connectedness, rolling {p.window}",
                          [series("total", [r[0] for r in roll], [r[2]["total"] for r in roll])], "date", "%")
            res.line_plot("rolling_net", "Net directional connectedness",
                          [series(c, [r[0] for r in roll], [r[2]["net"][i] for r in roll]) for i, c in enumerate(cols)],
                          "date", "%")
        return res


# ------------------------------------------------------------------------------------------------ local projections


class LPParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    horizon: int = Field(default=12, ge=0, le=60)
    y_lags: int = Field(default=2, ge=0, le=12)
    shock_lags: int = Field(default=0, ge=0, le=12)
    cumulative: bool = Field(default=True, description="response = sum of y over t..t+h (use with returns / growth)")
    fe: Literal["entity", "twoway", "none"] = "entity"
    spatial: bool = Field(default=False, description="panel of countries: add the spatially lagged shock W x")
    w_kind: WKind = "contiguity"
    k: int = Field(default=5, ge=1, le=20)
    w_as_of: date | None = None
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class LocalProjection(Method):
    id = "local_projection"
    family = "time_series"
    title = "Local projections (Jorda)"
    description = ("Impulse responses by direct regression of y_{t+h} (or cumulative y over t..t+h) on the shock x[0] "
                   "at t, with response / shock lags and further x as controls; panel FE; Driscoll-Kraay standard "
                   "errors; optional spatially lagged shock (W x) for country panels.")
    shapes = ("timeseries", "panel")
    min_x = 1
    Params = LPParams
    outputs = ("IRF table", "IRF plot with bands", "spatial IRF")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + n_rows * (params.horizon + 1) * 4e-6

    def fit(self, data: FitData, p: LPParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        shock = data.x[0]
        controls = list(data.x[1:])
        shocks = [shock]
        fe = p.fe if data.is_panel else "none"
        if p.spatial:
            if not data.is_panel:
                raise GuardrailError("bad_shape", "spatial = true needs a country panel")
            ids = sorted(df["entity_id"].astype(str).unique())
            if not _is_iso2(ids):
                raise GuardrailError("not_countries", "spatial LP needs entity ids ISO2 (entity_level = country)")
            have, Wraw, meta = get_w(ids, p.w_kind, p.k, p.w_as_of)
            Wr = SP.row_standardize(Wraw)
            wide = df.pivot_table(index="date", columns="entity_id", values=shock, aggfunc="last").reindex(columns=have)
            lagged = pd.DataFrame(wide.fillna(0.0).to_numpy() @ Wr.T, index=wide.index,
                                  columns=pd.Index(have, name="entity_id"))
            lagged[wide.isna()] = np.nan
            lagged.index.name = "date"
            long = lagged.stack().rename("W_" + shock).reset_index()
            df = df.merge(long, on=["date", "entity_id"], how="inner")
            shocks.append("W_" + shock)
            res.summary.update(clean({"w_kind": p.w_kind, "w_source": meta.get("source"), "w": _w_triplets(have, Wraw)}))
        data.check()
        out = SP.local_projection(df, data.y, shocks, controls, p.horizon, p.y_lags, p.shock_lags,
                                  cumulative=p.cumulative, fe=fe, alpha=p.alpha)
        if out.empty:
            raise GuardrailError("too_few_obs", "not enough observations for any horizon")
        rows = []
        for r in out.itertuples(index=False):
            rows.append([r.shock, int(r.h), r.coef, r.se, r.lo, r.hi, r.p, int(r.n)])
        res.add_table("irf", "Local-projection impulse responses", ["shock", "h", "coef", "std_error", "ci_low",
                                                                     "ci_high", "p_value", "n_obs"], clean(rows))
        for s in shocks:
            o = out[out["shock"] == s]
            res.plots[f"irf_{s}"] = clean({"kind": "ts_band", "title": f"Response of {data.y} to {s}",
                                           "x_label": "horizon", "y_label": data.y, "x": o["h"].tolist(),
                                           "series": [{"name": s, "x": o["h"].tolist(), "y": o["coef"].tolist()}],
                                           "bands": [{"name": f"{int((1 - p.alpha) * 100)}% band", "lower": o["lo"].tolist(),
                                                      "upper": o["hi"].tolist()}], "hlines": [{"y": 0}], "vlines": []})
        o0 = out[out["shock"] == shock]
        res.coefficients = coef_rows([f"{shock} h={h}" for h in o0["h"]], o0["coef"], o0["se"], o0["coef"] / o0["se"],
                                     o0["p"].fillna(1.0), np.column_stack([o0["lo"], o0["hi"]]))
        res.summary.update(clean({"shock": shock, "controls": controls, "horizon": p.horizon, "cumulative": p.cumulative,
                                  "fe": fe, "peak_h": int(o0.loc[o0["coef"].abs().idxmax(), "h"]),
                                  "peak": float(o0.loc[o0["coef"].abs().idxmax(), "coef"])}))
        res.metrics.update(clean({"irf_h0": float(o0["coef"].iloc[0]), "peak": res.summary["peak"]}))
        return res
