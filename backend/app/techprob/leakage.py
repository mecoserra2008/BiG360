"""Leakage tests for the Technicals v2 models (T6; same discipline as app.conformal.leakage / app.ml.train).

causal_history   structural test: rebuild the point-in-time history after replacing every bar AFTER a cut index with
                 a shuffled, rescaled path; every feature of an origin <= cut (setup, score, zone features) must be
                 bit-for-bit unchanged (labels may change - they are outcomes). A feature that reads a future bar
                 changes and is reported (the deliberately peeking variant `peek=True` must be caught).
permutation_skill  empirical test: fit the classifier on labels permuted within month blocks; out-of-sample skill vs
                 climatology (Brier skill score) and AUC must be ~0 / ~0.5.
ic_permutation   T4: rank IC of the score against permuted forward returns (within date) must be ~0.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import polars as pl

from app.techprob import history as Hh

LABEL_COLS = {"touched", "tau", "outcome", "complete", "hold", *[f"out_{h}" for h in (5, 10, 20, 40, 60)],
              *[f"tau_{h}" for h in (5, 10, 20, 40, 60)], *[f"z_{h}" for h in (5, 10, 20, 40, 60)],
              *[f"fwd_{h}" for h in (5, 20, 60)]}


def perturb_future(si: Hh.SecInput, cut: int, seed: int = 0, scale: float = 2.5) -> Hh.SecInput:
    """Copy of the input with bars after `cut` replaced: shuffled, rescaled log returns from the cut close, consistent
    OHLC (the series length is unchanged)."""
    b = si.bars
    rng = np.random.default_rng(seed)
    lc = np.log(b.c)
    r = np.diff(lc)[cut:]
    r = r[rng.permutation(len(r))] * scale + rng.normal(0, np.nanstd(r), len(r))
    new_lc = lc.copy()
    new_lc[cut + 1:] = lc[cut] + np.cumsum(r)
    c = np.exp(new_lc)
    o = c.copy()
    o[cut + 1:] = np.exp(np.r_[new_lc[cut], new_lc[cut + 1:-1]])
    wig = np.exp(np.abs(rng.normal(0, 0.01, len(c))))
    h = np.maximum(o, c) * wig
    l_ = np.minimum(o, c) / wig
    h[: cut + 1], l_[: cut + 1], o[: cut + 1], c[: cut + 1] = b.h[: cut + 1], b.l[: cut + 1], b.o[: cut + 1], b.c[: cut + 1]
    v = b.v.copy()
    v[cut + 1:] = v[cut + 1:][rng.permutation(len(v) - cut - 1)]
    nb = replace(b, o=o, h=h, l=l_, c=c, v=v)
    dv = si.dv_usd.copy()
    return Hh.SecInput(nb, dv, si.earn)


def _rows(si: Hh.SecInput, region: str, vix_d: np.ndarray, vix_v: np.ndarray, peek: bool) -> tuple[pl.DataFrame, pl.DataFrame]:
    if peek:
        orig = Hh.band_features

        def leaky(lo, hi, side, in_zone, i, b, st, ctx, setup, vp_px, kh, mb):  # noqa: ANN001, PLR0913
            out = orig(lo, hi, side, in_zone, i, b, st, ctx, setup, vp_px, kh, mb)
            j = min(i + 5, b.n - 1)                       # PEEKS at the close 5 sessions ahead
            out["approach5"] = float(np.log(b.c[j] / b.c[i]))
            return out

        Hh.band_features = leaky
    try:
        s, z = Hh.process_security(si, region, vix_d, vix_v, None)
    finally:
        if peek:
            Hh.band_features = orig
    return pl.DataFrame(s, infer_schema_length=None), pl.DataFrame(z, infer_schema_length=None)


def causal_history(si: Hh.SecInput, region: str, cuts: tuple[float, ...] = (0.5, 0.8), vix_d: np.ndarray | None = None,
                   vix_v: np.ndarray | None = None, peek: bool = False, seed: int = 0) -> dict[str, Any]:
    vix_d = vix_d if vix_d is not None else np.array([], dtype="datetime64[D]")
    vix_v = vix_v if vix_v is not None else np.array([])
    base_s, base_z = _rows(si, region, vix_d, vix_v, peek)
    res: dict[str, Any] = {"passed": True, "cuts": []}
    for c in cuts:
        cut = int(si.bars.n * c)
        cut_date = si.bars.dates[cut]
        ps, pz = _rows(perturb_future(si, cut, seed=seed), region, vix_d, vix_v, peek)
        bad = []
        for name, a, b, key in (("setups", base_s, ps, ["date"]), ("zones", base_z, pz, ["date", "zone_id", "is_control"])):
            if a.is_empty() or b.is_empty():
                continue
            a2 = a.filter(pl.col("date") <= cut_date)
            b2 = b.filter(pl.col("date") <= cut_date)
            feats = [x for x in a2.columns if x not in LABEL_COLS and x not in key and x in b2.columns
                     and a2[x].dtype.is_numeric()]
            j = a2.select(key + feats).join(b2.select(key + feats), on=key, how="inner", suffix="_p")
            for x in feats:
                d = (j[x].cast(pl.Float64) - j[f"{x}_p"].cast(pl.Float64)).abs().fill_nan(0.0).fill_null(0.0)
                if j.height and float(d.max()) > 1e-9:
                    bad.append({"table": name, "feature": x, "max_abs_diff": float(d.max()), "rows": int((d > 1e-9).sum())})
            # rows of origins <= cut whose existence does not depend on the future must match: setups always exist
            if name == "setups" and a2.height != b2.height:
                bad.append({"table": name, "feature": "<row set>", "max_abs_diff": float(abs(a2.height - b2.height)), "rows": 0})
        res["cuts"].append({"cut_date": str(cut_date), "violations": bad})
        if bad:
            res["passed"] = False
    return res


def permutation_skill(X: np.ndarray, y: np.ndarray, dates: np.ndarray, kind: str = "logit", seed: int = 0,
                      test_frac: float = 0.3) -> dict[str, Any]:
    """Fit on month-block-permuted labels (time split, purge 60 days) and score out-of-sample."""
    from sklearn.metrics import roc_auc_score  # noqa: PLC0415

    from app.techprob import calib as K  # noqa: PLC0415
    from app.techprob import zone_model as M  # noqa: PLC0415

    order = np.argsort(dates, kind="stable")
    X, y, dates = X[order], y[order], dates[order]
    n = len(y)
    cut = dates[int(n * (1 - test_frac))]
    tr = dates < cut - np.timedelta64(60, "D")
    te = dates >= cut
    rng = np.random.default_rng(seed)
    yp = y.copy()
    blocks = K.month_blocks(dates)
    for bl in np.unique(blocks[tr]):
        ix = np.nonzero(tr & (blocks == bl))[0]
        yp[ix] = y[ix][rng.permutation(len(ix))]
    # permute ACROSS blocks too (a block-constant label would survive a within-block shuffle)
    itr = np.nonzero(tr)[0]
    yp[itr] = yp[itr][rng.permutation(len(itr))]
    m = M.fit(kind, X[tr], yp[tr], None, None, [f"x{i}" for i in range(X.shape[1])], seed=seed)
    p = m.predict(X[te])
    clim = float(yp[tr].mean())
    bs = float(np.mean((p - y[te]) ** 2))
    bs0 = float(np.mean((clim - y[te]) ** 2))
    auc = float(roc_auc_score(y[te], p)) if len(np.unique(y[te])) > 1 else None
    return {"kind": kind, "n_train": int(tr.sum()), "n_test": int(te.sum()), "brier_skill": 1.0 - bs / bs0 if bs0 > 0 else None,
            "auc": auc}


def ic_permutation(df: pl.DataFrame, score_col: str, h: int, seed: int = 0) -> dict[str, Any]:
    from app.techprob import score_cal as SC  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    d = df.filter(pl.col(f"exc_{h}").is_not_null())
    d = d.with_columns(pl.Series(f"exc_{h}", d[f"exc_{h}"].to_numpy()[rng.permutation(d.height)]))
    d = d.with_columns(pl.col(f"exc_{h}").shuffle(seed=seed).over(["region", "date"]))
    return SC.ic_summary(SC.rank_ic(d, score_col, h), h)
