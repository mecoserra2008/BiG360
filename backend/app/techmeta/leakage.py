"""Leakage tests of the meta-labelling features (same discipline as app.techprob.leakage / app.conformal.leakage).

causal_bars      structural: recompute the bar-context features after replacing every bar AFTER a cut with a shuffled,
                 rescaled path (app.techprob.leakage.perturb_future); every feature of an origin <= cut must be
                 bit-for-bit unchanged. peek=True swaps in a feature that reads the close 5 sessions ahead - it MUST
                 be reported.
causal_context   structural: the as-of joins (market, macro, peer, insider, short interest, smart money) of an origin
                 must not change when rows known only AFTER the origin (knowledge date / signal date > origin) are
                 added to the sources; a deliberately peeking join (tolerance forward) must be reported.
The model-level tests (label permutation, context permutation, shifted-feature peek) run in the walk-forward
(scripts/technicals_v3/meta/wf_meta.py) and are summarised by the report.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.techmeta import features as F


def _bar_frame(si: Any, idx: np.ndarray, dr: np.ndarray, peek: bool) -> dict[str, np.ndarray]:
    from app.techprob import history as Hh  # noqa: PLC0415

    b = si.bars
    E = Hh.earnings_flags(b.dates, si.earn)
    f = F.bar_features(b.dates, b.o, b.h, b.l, b.c, E, idx, dr, has_earnings=si.earn.size > 0)
    if peek:                                          # PEEKS: return over the next 5 sessions
        j = np.minimum(idx + 5, b.n - 1)
        f["c_ret5"] = np.log(b.c[j] / b.c[idx]) * dr
    return f


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return bool(np.array_equal(np.isnan(a), np.isnan(b)) and np.array_equal(a[~np.isnan(a)], b[~np.isnan(b)]))


def causal_bars(si: Any, cuts: tuple[float, ...] = (0.5, 0.8), peek: bool = False, seed: int = 0,
                start: date = date(2008, 1, 1)) -> dict[str, Any]:
    from app.techprob import history as Hh  # noqa: PLC0415
    from app.techprob import leakage as TL  # noqa: PLC0415

    b = si.bars
    idx = Hh.weekly_origins(b.dates, start, min_i=60)
    rng = np.random.default_rng(seed)
    dr = np.where(rng.random(len(idx)) < 0.5, 1.0, -1.0)
    base = _bar_frame(si, idx, dr, peek)
    res: dict[str, Any] = {"security_id": b.security_id, "passed": True, "cuts": []}
    for c in cuts:
        cut = int(b.n * c)
        pert = TL.perturb_future(si, cut, seed=seed)
        f2 = _bar_frame(pert, idx, dr, peek)
        k = idx <= cut
        bad = [n for n in base if not _same(base[n][k], f2[n][k])]
        changed_after = [n for n in base if not _same(base[n][~k], f2[n][~k])]
        res["cuts"].append({"cut_date": str(b.dates[cut]), "origins_checked": int(k.sum()), "changed_features": bad,
                            "features_changed_after_cut": len(changed_after)})
        if bad:
            res["passed"] = False
    return res


def causal_context(X: pl.DataFrame, ctx: Any, peek: bool = False) -> dict[str, Any]:
    """X: origins [security_id, date, direction, region, earn_days_ahead, e_days_since]. ctx: app.techmeta.dataset.Context.
    Adds future-known rows (dated after every origin) to each source and checks the joined features are unchanged."""
    ids = sorted(X["security_id"].unique().to_list())
    cols = [c for g in ("market", "peer", "smart") for c in F.GROUPS[g]]
    base = _ctx_join(X, ctx, ids, extra=None, peek=peek)
    d_max = X["date"].max()
    fut = d_max + timedelta(days=1)
    extra = {
        "mk": pl.DataFrame({"date": [fut + timedelta(days=i) for i in range(5)]}).with_columns(
            pl.lit(99.0).alias("m_vix"), pl.lit(1.0).alias("m_vix_pct"), pl.lit(9.0).alias("m_vix_chg20"),
            pl.lit(9.0).alias("m_risk"), pl.lit(9.0).alias("m_risk_chg20"), pl.lit(0.99).alias("m_eig1")),
        "peer": pl.DataFrame({"security_id": ids, "date": [fut] * len(ids), "z_cond": [9.0] * len(ids), "ratio": [9.0] * len(ids)}),
        "tx": pl.DataFrame({"security_id": ids, "kd": [fut] * len(ids), "v": [1e9] * len(ids), "insider_id": ["leak"] * len(ids),
                            "side": [1] * len(ids)}).with_columns(pl.col("side").cast(pl.Int8)),
        "si": pl.DataFrame({"security_id": ids, "kd": [fut] * len(ids), "si_pct_out": [0.9] * len(ids), "si_z_1y": [9.0] * len(ids),
                            "days_to_cover": [50.0] * len(ids)}),
        "sm": pl.DataFrame({"security_id": ids, "kd": [fut] * len(ids), "score_pct": [100.0] * len(ids), "z_inst_flow": [5.0] * len(ids)}),
    }
    pert = _ctx_join(X.with_columns(pl.col("date")), ctx, ids, extra=extra, peek=peek)
    bad = []
    for c in cols:
        if c in base.columns and not _same(base[c].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                                           pert[c].cast(pl.Float64).fill_null(np.nan).to_numpy()):
            bad.append(c)
    return {"passed": not bad, "changed_features": bad, "origins": X.height}


def _ctx_join(X: pl.DataFrame, ctx: Any, ids: list[str], extra: dict[str, pl.DataFrame] | None, peek: bool) -> pl.DataFrame:
    mk = ctx.mk if not extra else pl.concat([ctx.mk, extra["mk"]], how="diagonal_relaxed")
    peer = ctx.peer if not extra else pl.concat([ctx.peer, extra["peer"]], how="diagonal_relaxed")
    tx = F.insider_daily(ids, ctx.start, ctx.end)
    si = F.si_frame(ids, ctx.start, ctx.end)
    sm = F.sm_frame(ids, ctx.start, ctx.end)
    if extra:
        tx = pl.concat([tx, extra["tx"]], how="diagonal_relaxed")
        si = pl.concat([si, extra["si"]], how="diagonal_relaxed").sort("kd")
        sm = pl.concat([sm, extra["sm"]], how="diagonal_relaxed").sort("kd")
    Y = X.sort(["security_id", "date"])
    if peek:                                           # PEEKS: market state 3 days AFTER the origin
        Y = Y.with_columns((pl.col("date") + pl.duration(days=3)).alias("_d"))
        Y = Y.sort("_d").join_asof(mk.sort("date"), left_on="_d", right_on="date", strategy="backward").drop("_d", "date_right")
    else:
        Y = F.join_market(Y, mk, ctx.mm)
    Y = F.join_peer(Y, peer, ctx.members)
    Y = F.join_insider(Y, tx, ctx.icov)
    Y = F.join_si_sm(Y, si, sm)
    return Y.sort(["security_id", "date"])
