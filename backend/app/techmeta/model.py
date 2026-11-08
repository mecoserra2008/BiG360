"""Meta model: P(primary setup wins | context) and P(loses | context), calibrated (AFML ch. 3 meta-labelling).

Both learners start from the v2 T2 probability as an OFFSET (logit of the calibrated T2 P(win) - or P(loss) for the
loss model), so a model with no usable context reproduces T2 and the context only has to explain the deviation:

  lr    L2-regularised logistic regression with offset (own L-BFGS fit, weighted): standardised features (training
        mean / sd, clipped at +-6), NaN -> 0 plus a missing indicator per column with missing values in training
  xgb   XGBoost (CPU hist, nthread <= 4, depth 2-3) with base_margin = the offset, early stopping on an inner
        purged validation block

Sample weights: average uniqueness of each label (AFML ch. 4) over the label's own sessions, per security
(overlapping weekly labels of the same name share weight). Calibration: none (the offset model is itself a logistic
model), Platt (2 parameters on the logit) or isotonic, chosen on a time split of the calibration block (first half fit,
second half scored), refitted on the whole block.
Hyperparameters are chosen only inside the training block (inner validation = its last year, embargoed).

Outputs per setup: calibrated meta P(win) / P(loss) (P(neither) = the rest), meta expected R = P(win) R:R - P(loss)
- costs + P(neither) E[R | neither] (T2's mark-to-market of the time exit), take / skip at a threshold chosen
in-sample, a capped fractional Kelly risk fraction and per-feature contributions (LR: beta x z; XGB: TreeSHAP).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

KELLY_FRACTION = 0.25
KELLY_CAP = 0.02            # max fraction of equity at risk (1R) per trade
THRESHOLDS = (-math.inf, -0.05, 0.0, 0.025, 0.05, 0.10)
LR_GRID = (1e-4, 1e-3, 1e-2, 1e-1, 1.0)
XGB_GRID = ({"max_depth": 2, "min_child_weight": 300.0}, {"max_depth": 3, "min_child_weight": 300.0})
XGB_BASE = {"objective": "binary:logistic", "eta": 0.05, "subsample": 0.7, "colsample_bytree": 0.8, "lambda": 10.0,
            "tree_method": "hist", "max_bin": 128, "nthread": 4, "eval_metric": "logloss", "verbosity": 0}
EPS = 1e-6


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-4, 1 - 1e-4)
    return np.log(p / (1 - p))


def sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def wlogloss(p: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> float:
    p = np.clip(p, EPS, 1 - EPS)
    ll = -(y * np.log(p) + (1 - y) * np.log(1 - p))
    return float(np.average(ll, weights=w) if w is not None else ll.mean())


# ------------------------------------------------------------------------------------------------ sample weights


def avg_uniqueness(sec: np.ndarray, start: np.ndarray, tau: np.ndarray) -> np.ndarray:
    """AFML ch. 4 average uniqueness per label: label k of security s spans business days (start_k, start_k + tau_k];
    c_t = labels of s alive on day t; u_k = mean over the span of 1 / c_t."""
    sec = np.asarray(sec)
    bd = np.busday_count(np.datetime64("1990-01-01"), np.asarray(start, dtype="datetime64[D]"))
    tau = np.maximum(np.asarray(tau, dtype=np.int64), 1)
    out = np.ones(len(sec), dtype=np.float64)
    order = np.argsort(sec, kind="stable")
    ss = sec[order]
    cuts = np.r_[0, np.nonzero(ss[1:] != ss[:-1])[0] + 1, len(ss)]
    for a, b in zip(cuts[:-1], cuts[1:], strict=True):
        ix = order[a:b]
        s0 = bd[ix] + 1
        e0 = bd[ix] + tau[ix]
        lo, hi = int(s0.min()), int(e0.max())
        cnt = np.zeros(hi - lo + 2)
        np.add.at(cnt, s0 - lo, 1.0)
        np.add.at(cnt, e0 - lo + 1, -1.0)
        c = np.cumsum(cnt)[:-1]
        inv = np.where(c > 0, 1.0 / np.maximum(c, 1.0), 0.0)
        cs = np.r_[0.0, np.cumsum(inv)]
        out[ix] = (cs[e0 - lo + 1] - cs[s0 - lo]) / (e0 - s0 + 1)
    return out


# ------------------------------------------------------------------------------------------------ learners


@dataclass
class Prep:
    """Standardisation + missing indicators (LR)."""
    cols: list[str]
    mean: np.ndarray
    sd: np.ndarray
    miss: np.ndarray                        # indices of columns with a missing indicator

    @classmethod
    def fit(cls, X: np.ndarray, cols: list[str], w: np.ndarray | None = None) -> Prep:
        fin = np.isfinite(X)
        Xz = np.where(fin, X, 0.0)
        ww = (w if w is not None else np.ones(len(X)))[:, None] * fin
        n = np.maximum(ww.sum(0), 1e-9)
        m = (Xz * ww).sum(0) / n
        v = (((Xz - m) ** 2) * ww).sum(0) / n
        sd = np.sqrt(v)
        sd = np.where(sd > 1e-9, sd, 1.0)
        miss = np.nonzero((~fin).any(0) & fin.any(0))[0]
        return cls(cols, m, sd, miss)

    def transform(self, X: np.ndarray) -> np.ndarray:
        fin = np.isfinite(X)
        Z = np.clip(np.where(fin, (X - self.mean) / self.sd, 0.0), -6.0, 6.0)
        if self.miss.size:
            Z = np.hstack([Z, (~fin[:, self.miss]).astype(np.float64)])
        return Z

    @property
    def names(self) -> list[str]:
        return [*self.cols, *[f"{self.cols[j]}__missing" for j in self.miss]]


def _lr_fit(Z: np.ndarray, y: np.ndarray, w: np.ndarray, off: np.ndarray, lam: float,
            beta0: np.ndarray | None = None, maxiter: int = 300) -> np.ndarray:
    """argmin_beta  sum_i w_i logloss(y_i, sigmoid(off_i + b0 + Z_i beta)) / sum w + lam / 2 |beta|^2 (no penalty on b0)."""
    from scipy.optimize import minimize  # noqa: PLC0415

    n, k = Z.shape
    W = w / w.sum()
    A = np.hstack([np.ones((n, 1)), Z])

    def f(b: np.ndarray) -> tuple[float, np.ndarray]:
        z = off + A @ b
        p = sigmoid(z)
        ll = np.logaddexp(0.0, z) - y * z
        loss = float(W @ ll) + 0.5 * lam * float(b[1:] @ b[1:])
        g = A.T @ (W * (p - y))
        g[1:] += lam * b[1:]
        return loss, g

    x0 = beta0 if beta0 is not None and beta0.shape == (k + 1,) else np.zeros(k + 1)
    res = minimize(f, x0, jac=True, method="L-BFGS-B", options={"maxiter": maxiter, "gtol": 1e-7})
    return res.x


@dataclass
class Learner:
    kind: str                               # lr | xgb
    cols: list[str]
    base_col: str                           # offset column (T2 logit)
    params: dict[str, Any] = field(default_factory=dict)
    prep: Prep | None = None
    beta: np.ndarray | None = None
    booster: Any = None
    n_trees: int | None = None

    def _off(self, X: np.ndarray) -> np.ndarray:
        j = self.cols.index(self.base_col)
        o = X[:, j]
        return np.where(np.isfinite(o), o, 0.0)

    def margin(self, X: np.ndarray) -> np.ndarray:
        off = self._off(X)
        if self.kind == "lr":
            Z = self.prep.transform(X)
            return off + self.beta[0] + Z @ self.beta[1:]
        import xgboost as xgb  # noqa: PLC0415

        dm = xgb.DMatrix(X, base_margin=off, missing=np.nan)
        return self.booster.predict(dm, output_margin=True, iteration_range=(0, self.n_trees or 0))

    def contributions(self, X: np.ndarray) -> tuple[np.ndarray, list[str]]:
        """(n, k) additive contributions to the margin beyond the offset (LR beta x z incl. missing indicators;
        XGB TreeSHAP without the bias column) and their names."""
        if self.kind == "lr":
            Z = self.prep.transform(X)
            return Z * self.beta[1:][None, :], self.prep.names
        import xgboost as xgb  # noqa: PLC0415

        dm = xgb.DMatrix(X, base_margin=self._off(X), missing=np.nan)
        C = self.booster.predict(dm, pred_contribs=True, iteration_range=(0, self.n_trees or 0))
        return C[:, :-1], list(self.cols)


def fit_learner(kind: str, X: np.ndarray, y: np.ndarray, w: np.ndarray, cols: list[str], base_col: str,
                inner_val: np.ndarray | None = None, params: dict[str, Any] | None = None,
                inner_train: np.ndarray | None = None) -> Learner:
    """Fit on all rows; with inner_val (bool mask of a later block of X) choose the hyperparameters on it first
    (train = inner_train, default ~inner_val; pass an embargoed mask), then refit on everything with the chosen ones.
    params skips the search."""
    L = Learner(kind, cols, base_col)
    off = L._off(X)
    if inner_val is not None and inner_train is None:
        inner_train = ~inner_val
    if kind == "lr":
        grid = [params] if params else [{"lam": v} for v in LR_GRID]
        best = grid[0]
        if inner_val is not None and len(grid) > 1 and inner_val.any() and inner_train.any():
            tr = inner_train
            pr = Prep.fit(X[tr], cols, w[tr])
            Ztr, Zva = pr.transform(X[tr]), pr.transform(X[inner_val])
            scores = []
            for g in grid:
                b = _lr_fit(Ztr, y[tr], w[tr], off[tr], g["lam"])
                p = sigmoid(off[inner_val] + b[0] + Zva @ b[1:])
                scores.append(wlogloss(p, y[inner_val], w[inner_val]))
            best = grid[int(np.argmin(scores))]
            L.params["inner_logloss"] = dict(zip([g["lam"] for g in grid], scores, strict=True))
        L.prep = Prep.fit(X, cols, w)
        L.beta = _lr_fit(L.prep.transform(X), y, w, off, best["lam"])
        L.params.update(best)
        return L
    import xgboost as xgb  # noqa: PLC0415

    grid = [params] if params else list(XGB_GRID)
    best, n_best = grid[0], int((params or {}).get("n_trees", 300))
    if inner_val is not None and inner_val.any() and inner_train.any() and not (params and "n_trees" in params):
        tr = inner_train
        dtr = xgb.DMatrix(X[tr], label=y[tr], weight=w[tr], base_margin=off[tr], missing=np.nan)
        dva = xgb.DMatrix(X[inner_val], label=y[inner_val], weight=w[inner_val], base_margin=off[inner_val], missing=np.nan)
        scores = []
        for g in grid:
            bst = xgb.train({**XGB_BASE, **{k: v for k, v in g.items() if k != "n_trees"}}, dtr, num_boost_round=800,
                            evals=[(dva, "val")], early_stopping_rounds=50, verbose_eval=False)
            scores.append((float(bst.best_score), int(bst.best_iteration) + 1, g))
        sc, n_best, best = min(scores, key=lambda x: x[0])
        L.params["inner_logloss"] = [{"params": g, "logloss": s, "n_trees": n} for s, n, g in scores]
        n_best = max(20, int(round(n_best * (1.0 + 0.5 * inner_val.mean()))))      # more data in the refit
    d = xgb.DMatrix(X, label=y, weight=w, base_margin=off, missing=np.nan)
    L.booster = xgb.train({**XGB_BASE, **{k: v for k, v in best.items() if k != "n_trees"}}, d, num_boost_round=n_best)
    L.n_trees = n_best
    L.params.update({k: v for k, v in best.items() if k != "n_trees"}, n_trees=n_best)
    return L


# ------------------------------------------------------------------------------------------------ calibration


@dataclass
class Calibrator:
    kind: str                               # platt | isotonic | identity
    a: float = 1.0
    b: float = 0.0
    iso: Any = None

    def __call__(self, m: np.ndarray) -> np.ndarray:
        if self.kind == "platt":
            return sigmoid(self.a * m + self.b)
        if self.kind == "isotonic":
            return np.clip(self.iso.predict(sigmoid(m)), 1e-4, 1 - 1e-4)
        return sigmoid(m)


def _platt(m: np.ndarray, y: np.ndarray, w: np.ndarray) -> Calibrator:
    b = _lr_fit(m[:, None], y, w, np.zeros_like(m), 0.0)
    return Calibrator("platt", float(b[1]), float(b[0]))


def _iso(m: np.ndarray, y: np.ndarray, w: np.ndarray) -> Calibrator:
    from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

    return Calibrator("isotonic", iso=IsotonicRegression(out_of_bounds="clip", y_min=1e-4, y_max=1 - 1e-4)
                      .fit(sigmoid(m), y, sample_weight=w))


def fit_calibrator(m: np.ndarray, y: np.ndarray, w: np.ndarray, order: np.ndarray) -> tuple[Calibrator, dict[str, Any]]:
    """Choose Platt vs isotonic on a time split of the calibration block (order = dates), refit on all of it."""
    o = np.argsort(order, kind="stable")
    h = len(o) // 2
    a, b = o[:h], o[h:]
    sc = {"identity": wlogloss(sigmoid(m[b]), y[b], w[b])}
    for nm, fn in (("platt", _platt), ("isotonic", _iso)):
        try:
            c = fn(m[a], y[a], w[a])
            sc[nm] = wlogloss(c(m[b]), y[b], w[b])
        except Exception:  # noqa: BLE001
            sc[nm] = math.inf
    pick = min(sc, key=lambda k: sc[k])
    cal = Calibrator("identity") if pick == "identity" else (_platt if pick == "platt" else _iso)(m, y, w)
    return cal, {"chosen": pick, "half_logloss": sc}


# ------------------------------------------------------------------------------------------------ meta model


@dataclass
class MetaModel:
    """Win + loss learners and their calibrators for one horizon; plus the in-sample decision threshold."""
    H: int
    kind: str
    cols: list[str]
    win: Learner
    loss: Learner
    cal_win: Calibrator
    cal_loss: Calibrator
    threshold: float = 0.0
    size_ref: float | None = None
    info: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: np.ndarray) -> dict[str, np.ndarray]:
        pw = self.cal_win(self.win.margin(X))
        pl_ = np.minimum(self.cal_loss(self.loss.margin(X)), 1.0 - pw)      # P(win) is the gated output: kept as is
        return {"p_win": pw, "p_loss": pl_, "p_neither": np.clip(1.0 - pw - pl_, 0.0, 1.0)}


def expected_r(pw: np.ndarray, pl_: np.ndarray, rr: np.ndarray, cost_r: np.ndarray, mtm_r: np.ndarray) -> np.ndarray:
    pn = np.clip(1.0 - pw - pl_, 0.0, 1.0)
    return pw * rr - pl_ - cost_r + pn * np.nan_to_num(mtm_r)


def kelly_fraction(pw: np.ndarray, pl_: np.ndarray, rr: np.ndarray, mtm_r: np.ndarray, er: np.ndarray,
                   frac: float = KELLY_FRACTION, cap: float = KELLY_CAP) -> np.ndarray:
    """Capped fractional Kelly risk fraction: f* ~ E[R] / E[R^2] (small-edge growth-optimal fraction of equity per 1R),
    E[R^2] = P(win) R:R^2 + P(loss) + P(neither) (mtm^2 + (R:R + 1)^2 / 12) (time exits spread over [-1, R:R])."""
    pn = np.clip(1.0 - pw - pl_, 0.0, 1.0)
    m2 = pw * rr ** 2 + pl_ + pn * (np.nan_to_num(mtm_r) ** 2 + (rr + 1.0) ** 2 / 12.0)
    f = np.where(m2 > 0, er / np.maximum(m2, 1e-9), 0.0)
    return np.clip(frac * f, 0.0, cap)


def weekly_t(r: np.ndarray, week: np.ndarray) -> tuple[float, int]:
    """t-stat of the mean realised R per week (weeks with at least one trade), and the number of weeks."""
    if r.size == 0:
        return -math.inf, 0
    u, inv = np.unique(week, return_inverse=True)
    m = np.bincount(inv, weights=r) / np.bincount(inv)
    if m.size < 10 or m.std(ddof=1) <= 0:
        return -math.inf, int(m.size)
    return float(m.mean() / (m.std(ddof=1) / math.sqrt(m.size))), int(m.size)


def choose_threshold(er: np.ndarray, real_r: np.ndarray, week: np.ndarray) -> tuple[float, dict[str, Any]]:
    """In-sample (calibration block) threshold on meta expected R maximising the weekly t-stat of realised R of the
    taken setups; -inf = take every setup (no filter)."""
    res = {}
    for t in THRESHOLDS:
        k = er > t
        res[str(t)] = weekly_t(real_r[k], week[k])[0]
    best = max(THRESHOLDS, key=lambda t: res[str(t)])
    return float(best), {"t_by_threshold": res}


def week_index(dates: np.ndarray) -> np.ndarray:
    return (np.asarray(dates, dtype="datetime64[D]").astype(np.int64) + 3) // 7
