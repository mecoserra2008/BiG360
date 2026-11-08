"""Studio v2 S2 simple neural networks (torch, CPU, threads capped): MLP, small GRU / LSTM for time series and panels,
autoencoder factors (owner: model-studio). Core: app/modeling/_nn.py. Each result carries the mandatory linear
baseline (OLS / logit, ridge on the same window, PCA with the same number of factors) and a Diebold-Mariano test, and
says plainly when the network does not beat it."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import _advml as A
from .. import _ml as M
from .. import _nn as N
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, series
from .advml import _lead, dm_vs, linear_oos, metrics_of, tested_mask
from .ml import CVParams, _oos_block, _task, make_folds


class TrainParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lr: float = Field(default=1e-3, gt=0, le=1, description="Adam learning rate")
    weight_decay: float = Field(default=1e-4, ge=0, le=1)
    batch_size: int = Field(default=256, ge=8, le=8192)
    max_epochs: int = Field(default=200, ge=1, le=2000)
    patience: int = Field(default=15, ge=1, le=200, description="early stopping: epochs without validation improvement")
    val_frac: float = Field(default=0.2, gt=0, lt=0.5, description="last share of each training fold's dates used for early stopping")
    n_seeds: int = Field(default=3, ge=1, le=10, description="networks averaged (seeds seed, seed+1, ...)")


def _curve_plot(res: Result, curve: dict[str, list[float]], best: int, ylab: str) -> None:
    ep = list(range(1, len(curve["train"]) + 1))
    s = [series("train", ep, curve["train"])]
    if curve["valid"]:
        s.append(series("validation", list(range(1, len(curve["valid"]) + 1)), curve["valid"]))
    res.line_plot("training_curve", f"Training and validation loss, last fold (mean over seeds; best epoch ≈ {best})",
                  s, "epoch", ylab, vlines=[{"x": best, "label": "early stop"}])


def _epoch_cost(n: int, params: int, batch: int, epochs: int) -> float:
    return min(epochs, 60) * ((n / batch) * 4e-4 + n * params * 3e-9)


# ------------------------------------------------------------------------------------------------ MLP


class MLPParams(TrainParams):
    task: Literal["auto", "regression", "classification"] = "auto"
    hidden: list[int] = Field(default_factory=lambda: [32, 16], min_length=1, max_length=4, description="units per hidden layer")
    dropout: float = Field(default=0.1, ge=0, lt=0.9)
    activation: Literal["relu", "tanh", "gelu"] = "relu"
    cv: CVParams = Field(default_factory=CVParams)

    @field_validator("hidden")
    @classmethod
    def _h(cls, v: list[int]) -> list[int]:
        if any(h < 2 or h > 512 for h in v):
            raise ValueError("hidden units must be between 2 and 512")
        return v


@register
class MLP(Method):
    id = "mlp"
    family = "neural"
    title = "Neural network: multilayer perceptron (MLP)"
    description = ("Feed-forward network for regression or classification on standardized features, purged / "
                   "walk-forward CV, early stopping on a time-ordered validation block, seed averaging; always compared "
                   "with OLS / logit on the same folds (Diebold-Mariano).")
    min_x = 1
    Params = MLPParams
    outputs = ("OOS metrics vs linear baseline", "training / validation curves", "permutation importance (last fold)")
    diagnostics = ("DM test vs linear (mandatory)", "best epoch per fold")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        h = list(params.hidden)
        npar = n_x * h[0] + sum(a * b for a, b in zip(h, h[1:], strict=False)) + h[-1]
        return 2.0 + params.cv.folds * params.n_seeds * _epoch_cost(n_rows, npar, params.batch_size, params.max_epochs)

    def fit(self, data: FitData, p: MLPParams) -> Result:
        df, dropped = data.complete()
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        task = _task(y, p.task)
        res = Result(self.id, self.family, f"MLP {p.hidden} ({task})")
        res.summary["rows_dropped_missing"] = dropped
        if len(y) < 100:
            raise GuardrailError("too_few_obs", "a neural network needs at least 100 complete rows")
        folds, scheme, purge = make_folds(data, df, p.cv)
        prm = p.model_dump()
        make = lambda: N.mlp_net(X.shape[1], list(p.hidden), p.dropout, p.activation)  # noqa: E731
        out = N.nn_cv(make, X, y, df["date"].to_numpy(), folds, task, prm, data.seed, purge, data.check)
        mo = _oos_block(res, data, df, y, out["oos"], task, folds, scheme, purge, "mlp")
        mask = tested_mask(len(y), folds)
        lin = linear_oos(X, y, folds, task, data.check)
        res.evaluation["metrics"]["linear"] = clean(metrics_of(y[mask], lin[mask], task))
        dm = dm_vs(res, df, y, out["oos"], lin, task, mask, "MLP", "linear baseline", _lead(data))
        best = int(np.median(out["best_epochs"]))
        _curve_plot(res, out["curve"], best, "BCE" if task == "classification" else "MSE (standardized y)")
        # permutation importance on the last test fold (loss increase when a feature is shuffled; averaged nets of that fold)
        tr, te = folds[-1]
        data.check()
        rng = np.random.default_rng(data.seed)
        imp = []
        if len(te) >= 20:
            import torch

            fi, _ = A.inner_val_split(tr, df["date"].to_numpy(), p.val_frac, purge, data.seed)
            Xf, Xt = N.standardize(X[fi], X[te])
            mu, sd = (0.0, 1.0) if task == "classification" else (float(y[fi].mean()), float(y[fi].std() or 1.0))
            with N.TorchCtx():
                torch.manual_seed(data.seed)
                yf = y[fi] if task == "classification" else (y[fi] - mu) / sd
                net, _, _ = N.train(make(), Xf, yf, None, None, "bce" if task == "classification" else "mse", p.lr,
                                    p.weight_decay, p.batch_size, max(best, 1), p.patience, data.seed, data.check)

                def pr(Z: np.ndarray) -> np.ndarray:
                    o = N._out(net, Z).reshape(-1)
                    return 1 / (1 + np.exp(-o)) if task == "classification" else o * sd + mu
                l0 = float(np.mean(A.loss_vec(y[te], pr(Xt), task)))
                for j, c in enumerate(data.x):
                    Z = Xt.copy()
                    Z[:, j] = Z[rng.permutation(len(Z)), j]
                    imp.append((c, float(np.mean(A.loss_vec(y[te], pr(Z), task))) - l0))
            imp.sort(key=lambda r: -r[1])
            res.add_table("importance", "Permutation importance on the last test fold (loss increase)", ["feature", "loss_increase"], clean(imp))
            res.line_plot("importance", "Permutation importance", [series("loss increase", [r[0] for r in imp], [r[1] for r in imp])],
                          "feature", "loss increase", kind="bar")
        res.add_table("best_epochs", "Early-stopping epoch per fold and seed", ["fold", "seed", "best_epoch"],
                      [[i // p.n_seeds + 1, i % p.n_seeds, int(b)] for i, b in enumerate(out["best_epochs"])])
        key = "auc" if task == "classification" else "rmse"
        res.fit_stats["best_epoch_median"] = best
        res.summary.update({"dependent": data.y, "n_obs": len(y), "task": task, "hidden": list(p.hidden), "cv_scheme": scheme,
                            "n_seeds": p.n_seeds, "beats_linear": dm["beats"], "verdict": dm["verdict"]})
        res.metrics.update(clean({"n_obs": len(y), f"oos_{key}": mo.get(key), "dm_p_vs_linear": dm["dm_p"],
                                  f"linear_oos_{key}": res.evaluation["metrics"]["linear"].get(key)}))
        return res


# ------------------------------------------------------------------------------------------------ GRU / LSTM


class RNNParams(TrainParams):
    cell: Literal["gru", "lstm"] = "gru"
    task: Literal["auto", "regression", "classification"] = "auto"
    window: int = Field(default=12, ge=2, le=120, description="look-back length in rows (periods)")
    hidden_size: int = Field(default=16, ge=2, le=256)
    layers: int = Field(default=1, ge=1, le=3)
    dropout: float = Field(default=0.0, ge=0, lt=0.9, description="between stacked layers")
    include_y_lags: bool = Field(default=True, description="add y lagged by max(1, horizon) rows as an input (no overlap with the target)")
    lr: float = Field(default=3e-3, gt=0, le=1)
    batch_size: int = Field(default=128, ge=8, le=8192)
    max_epochs: int = Field(default=150, ge=1, le=2000)
    cv: CVParams = Field(default_factory=CVParams)


@register
class RNN(Method):
    id = "rnn"
    family = "neural"
    title = "Neural network: small GRU / LSTM"
    description = ("Recurrent network on look-back windows of the features (and of y lagged by the target horizon, so "
                   "no label overlaps the target), per entity on panels; walk-forward / purged CV on the window end "
                   "dates, early stopping and seed averaging; compared with ridge on the same flattened window (DM).")
    shapes = ("timeseries", "panel")
    min_x = 0
    Params = RNNParams
    outputs = ("OOS metrics vs linear baseline (same window)", "training / validation curves", "OOS fit")
    diagnostics = ("DM test vs linear (mandatory)", "best epoch per fold")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        h, f = params.hidden_size, n_x + int(params.include_y_lags)
        npar = params.layers * 3 * h * (f + h) * (4 / 3 if params.cell == "lstm" else 1)
        per = min(params.max_epochs, 60) * ((n_rows / params.batch_size) * 1.5e-3 * params.window / 12 + n_rows * params.window * npar * 4e-9)
        return 2.0 + params.cv.folds * params.n_seeds * per

    def fit(self, data: FitData, p: RNNParams) -> Result:
        df, dropped = data.complete()
        feats = list(data.x)
        lag = max(1, _lead(data))
        if p.include_y_lags:
            key = df["entity_id"].astype(str) if "entity_id" in df.columns else pd.Series(0, index=df.index)
            df = df.assign(_k=key.to_numpy()).sort_values(["_k", "date"])
            df[f"{data.y}_lag{lag}"] = df.groupby("_k")[data.y].shift(lag)
            df = df.drop(columns="_k").reset_index(drop=True)
            feats.append(f"{data.y}_lag{lag}")
        if not feats:
            raise GuardrailError("missing_x", "the RNN needs features or include_y_lags")
        ends, W = N.make_windows(df, feats, p.window)
        if len(ends) < 100:
            raise GuardrailError("too_few_obs", f"only {len(ends)} complete windows of length {p.window} (need 100)")
        de = df.iloc[ends].reset_index(drop=True)
        y = de[data.y].to_numpy(float)
        task = _task(y, p.task)
        res = Result(self.id, self.family, f"{p.cell.upper()} (window {p.window}, {task})")
        res.summary["rows_dropped_missing"] = dropped
        folds, scheme, purge = make_folds(data, de, p.cv)
        prm = p.model_dump()
        make = lambda: N.rnn_net(W.shape[2], p.cell, p.hidden_size, p.layers, p.dropout)  # noqa: E731
        out = N.nn_cv(make, W, y, de["date"].to_numpy(), folds, task, prm, data.seed, purge, data.check)
        mo = _oos_block(res, data, de, y, out["oos"], task, folds, scheme, purge, p.cell)
        mask = tested_mask(len(y), folds)
        lin = N.ridge_window_cv(W, y, folds, task)
        res.evaluation["metrics"]["linear"] = clean(metrics_of(y[mask], lin[mask], task))
        dm = dm_vs(res, de, y, out["oos"], lin, task, mask, p.cell.upper(), "linear baseline", _lead(data))
        best = int(np.median(out["best_epochs"]))
        _curve_plot(res, out["curve"], best, "BCE" if task == "classification" else "MSE (standardized y)")
        if not data.is_panel:
            t = np.where(mask)[0]
            res.line_plot("oos_fit", "Out-of-fold predictions vs actual", [
                series("actual", [d for d in pd.to_datetime(de["date"].iloc[t]).dt.date], y[t]),
                series(p.cell.upper(), [d for d in pd.to_datetime(de["date"].iloc[t]).dt.date], out["oos"][t]),
                series("linear", [d for d in pd.to_datetime(de["date"].iloc[t]).dt.date], lin[t])], "date", data.y)
        key = "auc" if task == "classification" else "rmse"
        res.summary.update({"dependent": data.y, "n_obs": len(y), "task": task, "cell": p.cell, "window": p.window,
                            "inputs": feats, "y_lag_offset": lag if p.include_y_lags else None, "cv_scheme": scheme,
                            "n_seeds": p.n_seeds, "beats_linear": dm["beats"], "verdict": dm["verdict"]})
        res.fit_stats["best_epoch_median"] = best
        res.metrics.update(clean({"n_obs": len(y), f"oos_{key}": mo.get(key), "dm_p_vs_linear": dm["dm_p"],
                                  f"linear_oos_{key}": res.evaluation["metrics"]["linear"].get(key)}))
        return res


# ------------------------------------------------------------------------------------------------ autoencoder


class AEParams(TrainParams):
    n_factors: int = Field(default=3, ge=1, le=20)
    hidden: int = Field(default=32, ge=2, le=512)
    activation: Literal["relu", "tanh", "gelu"] = "tanh"
    holdout_frac: float = Field(default=0.2, gt=0.05, lt=0.5, description="last share of dates kept out to compare with PCA")


@register
class Autoencoder(Method):
    id = "autoencoder"
    family = "neural"
    title = "Autoencoder factors"
    description = ("Non-linear latent factors: a one-hidden-layer autoencoder compresses the x columns into k factors; "
                   "compared with PCA using the same k (the linear autoencoder) on held-out dates by reconstruction R² "
                   "and a Diebold-Mariano test on per-row reconstruction error.")
    needs_y = False
    min_x = 2
    Params = AEParams
    outputs = ("hold-out reconstruction R² vs PCA", "factor paths", "factor-feature correlations", "training curve")
    diagnostics = ("DM test vs PCA (mandatory)", "per-feature R²")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        npar = 2 * (n_x * params.hidden + params.hidden * params.n_factors)
        return 2.0 + params.n_seeds * _epoch_cost(n_rows, npar, params.batch_size, params.max_epochs)

    def fit(self, data: FitData, p: AEParams) -> Result:
        if p.n_factors >= len(data.x):
            raise GuardrailError("too_many_factors", "n_factors must be smaller than the number of x columns")
        df, dropped = data.complete()
        X = df[data.x].to_numpy(float)
        if len(X) < 100:
            raise GuardrailError("too_few_obs", "the autoencoder needs at least 100 complete rows")
        res = Result(self.id, self.family, f"Autoencoder ({p.n_factors} factors)")
        res.summary["rows_dropped_missing"] = dropped
        dates = df["date"].to_numpy()
        train_idx, hold = A.holdout_split(dates, p.holdout_frac, 0, data.seed)
        fi, vi = A.inner_val_split(train_idx, dates, p.val_frac, 0, data.seed)
        out = N.ae_fit(X, fi, vi, p.n_factors, p.model_dump(), data.seed, data.check)
        Xs = out["Xs"]
        from sklearn.decomposition import PCA

        pca = PCA(n_components=p.n_factors, random_state=data.seed).fit(Xs[train_idx])
        rec_pca = pca.inverse_transform(pca.transform(Xs))

        def r2(idx: np.ndarray, R: np.ndarray) -> float:
            return float(1 - np.sum((Xs[idx] - R[idx]) ** 2) / np.sum(Xs[idx] ** 2))

        ra, rp = r2(hold, out["recon"]), r2(hold, rec_pca)
        res.add_table("reconstruction", "Reconstruction R² (standardized x; scaling and fits from the training dates only)",
                      ["model", "train_r2", "holdout_r2"], clean([["autoencoder", r2(train_idx, out["recon"]), ra],
                                                                 ["PCA (same k)", r2(train_idx, rec_pca), rp]]))
        pf = [[c, 1 - np.sum((Xs[hold, j] - out["recon"][hold, j]) ** 2) / np.sum(Xs[hold, j] ** 2),
               1 - np.sum((Xs[hold, j] - rec_pca[hold, j]) ** 2) / np.sum(Xs[hold, j] ** 2)] for j, c in enumerate(data.x)]
        res.add_table("feature_r2", "Hold-out R² per feature", ["feature", "autoencoder", "pca"], clean(pf))
        order = np.argsort(pd.to_datetime(dates[hold]), kind="stable")
        la = np.mean((Xs[hold] - out["recon"][hold]) ** 2, 1)[order]
        lp = np.mean((Xs[hold] - rec_pca[hold]) ** 2, 1)[order]
        stat, pv = M.dm_test(lp, la)
        better, sig = ra > rp, pv is not None and np.isfinite(pv) and pv < 0.05
        verdict = (f"the autoencoder reconstructs held-out data better than PCA (DM p = {pv:.3g})" if better and sig else
                   f"the autoencoder does not significantly beat PCA with the same k (DM p = {pv:.3g}): "
                   "the non-linear factors add nothing measurable")
        res.add_test("Diebold-Mariano: autoencoder vs PCA (per-row reconstruction MSE)", stat, pv, None,
                     "equal reconstruction accuracy", group="inference", passed=None, conclusion=verdict)
        if not (better and sig):
            res.warn("no_gain_vs_pca", verdict, "info" if better else "warning")
        F = out["factors"]
        names = [f"AE{j + 1}" for j in range(p.n_factors)]
        corr = [[float(np.corrcoef(Xs[:, i], F[:, j])[0, 1]) if np.std(F[:, j]) > 0 else None for j in range(p.n_factors)]
                for i in range(len(data.x))]
        res.plots["loadings_heatmap"] = clean({"kind": "heatmap", "title": "Correlation of each feature with each factor",
                                               "x": names, "y": list(data.x), "z": corr, "x_label": "factor", "y_label": "feature"})
        fd = pd.DataFrame(F, columns=names).assign(date=pd.to_datetime(dates)).groupby("date").mean().sort_index()
        res.line_plot("factors", "Factor scores by date" + (" (cross-sectional mean)" if data.is_panel else ""),
                      [series(n, [d.date() for d in fd.index], fd[n]) for n in names], "date", "factor")
        cv_ = out["curve"]
        _curve_plot(res, cv_, int(np.nanargmin(cv_["valid"])) + 1 if cv_["valid"] else len(cv_["train"]), "MSE")
        res.summary.update({"features": list(data.x), "n_factors": p.n_factors, "n_obs": len(X), "n_train": len(train_idx),
                            "n_holdout": len(hold), "beats_pca": bool(better and sig), "verdict": verdict})
        res.metrics.update(clean({"holdout_r2": ra, "pca_holdout_r2": rp, "dm_p_vs_pca": pv, "n_obs": len(X)}))
        return res
