"""Meta-labelling training rows: setup origins x horizon -> point-in-time features + triple-barrier labels.

Used by the walk-forward build (scripts/technicals_v3/meta/build_dataset.py), the weekly refit (new completed origins
from tech_prob_setup_events) and the nightly scoring (today's setups, T2 from tech_prob_setups). The SAME functions
build the features in all three, so training and serving cannot drift apart.

Labels (per horizon H, from the v2 two-barrier outcome): y_win = T1 before the invalidation within H sessions;
y_loss = invalidation first or both on the same bar; y_nei = neither by H (vertical barrier). real_r_mtm = realised
R of the triple-barrier trade (R:R / -1 / the H-session move in R units) after costs.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.logsetup import get_logger
from app.techmeta import features as F
from app.techmeta import model as M

log = get_logger("app.techmeta.dataset")

HORIZONS = (10, 20, 40)
SETUP_COLS = ["security_id", "date", "region", "sector_id", "close", "entry", "sigma_d", "trend", "structure_state",
              "score_trend", "score_structure", "score_location", "score_momentum", "earn_days_ahead", "direction"]


class Context:
    """Date / security context sources, loaded once per run."""

    def __init__(self, start: date, end: date, settings: Any = None, peer_source: str = "wf", as_of: date | None = None,
                 extra: list[str] | None = None):
        self.extra = list(extra or [])
        self.history = peer_source == "wf"        # history build: #1 / #5 research files; live: their lake tables
        self.mk = F.market_daily(start, end, settings)
        self.mm = F.macro_monthly(settings)
        from app.core.config import get_settings  # noqa: PLC0415

        s = settings or get_settings()
        if peer_source == "wf":
            self.peer = F.peer_signals_wf(s.data_dir)
        else:
            try:
                self.peer = F.peer_signals_live(as_of or end)
            except Exception as e:  # noqa: BLE001
                log.warning("live peer signals unavailable: %s", e)
                self.peer = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "z_cond": pl.Float64, "ratio": pl.Float64})
        self.members = F.peer_members(s)
        self.icov = F.insider_coverage(s)
        self.start, self.end = start, end
        self.v3_root = s.project_root / "scratchpad" / "tech_v3_b15" / "wf"

    def join(self, X: pl.DataFrame, ids: list[str]) -> pl.DataFrame:
        X = F.join_market(X, self.mk, self.mm)
        X = F.join_peer(X, self.peer, self.members)
        X = F.join_insider(X, F.insider_daily(ids, self.start, self.end), self.icov)
        X = F.join_si_sm(X, F.si_frame(ids, self.start, self.end), F.sm_frame(ids, self.start, self.end))
        X = self.join_extra(X, ids)
        ea = pl.col("earn_days_ahead").cast(pl.Float64)
        return X.with_columns(pl.when(pl.col("e_days_since").is_null() & (ea == 0)).then(None)
                              .otherwise(ea.replace(0.0, 63.0).clip(0, 63)).alias("e_days_ahead"))


def _extra_join(self: Context, X: pl.DataFrame, ids: list[str]) -> pl.DataFrame:
    """#1 factor percentiles / #5 breakout confirmation when announced ready (features.EXTRA_FEATURES)."""
    if any(x in F.B1_FEATURES for x in self.extra):
        fac = F.factors_history(self.v3_root, F.REGIONS, ids) if self.history else F.factors_live(ids, self.end)
        X = F.join_factors(X, fac)
    if any(x in F.B5_FEATURES for x in self.extra):
        bo = F.breakouts_history(self.v3_root, F.REGIONS, ids) if self.history else F.breakouts_live(ids, self.end)
        X = F.join_breakouts(X, bo)
    return X


Context.join_extra = _extra_join  # type: ignore[attr-defined]


def bar_rows(items: list[Any], origins: pl.DataFrame, live_ke: dict[str, int | None] | None = None) -> pl.DataFrame:
    """Bar-context features for (security_id, date, direction) origins whose date is one of the security's bars."""
    from app.techprob import history as Hh  # noqa: PLC0415

    parts = []
    by = {str(k[0]): g for k, g in origins.group_by(["security_id"])}
    for si in items:
        b = si.bars
        sub = by.get(b.security_id)
        if sub is None or sub.is_empty():
            continue
        od = np.array(sub["date"].to_list(), dtype="datetime64[D]")
        k = np.searchsorted(b.dates, od)
        ok = (k < b.n) & (b.dates[np.minimum(k, b.n - 1)] == od)
        if not ok.any():
            continue
        E = Hh.earnings_flags(b.dates, si.earn)
        ke_last = (live_ke or {}).get(b.security_id) if live_ke is not None else None
        f = F.bar_features(b.dates, b.o, b.h, b.l, b.c, E, k[ok], sub["direction"].to_numpy()[ok],
                           has_earnings=si.earn.size > 0, ke_last=ke_last if live_ke is not None else None)
        f["sigma_d_bar"] = f.pop("_sigma_d")            # EWMA sigma_d at the origin (live: the setup's sigma_d)
        parts.append(pl.DataFrame({"security_id": b.security_id, "date": sub["date"].filter(pl.Series(ok)),
                                   **{c: v.astype(np.float32) for c, v in f.items()}}).fill_nan(None))
    return pl.concat(parts) if parts else pl.DataFrame()


def finalize(D: pl.DataFrame, H: int, with_labels: bool = True) -> pl.DataFrame:
    """T2 + feature rows of one horizon -> model frame (mtm_r, base / geometry / sector columns, uniqueness weights).
    Needs pw / pl / pn / exp_r_mtm / cost_r / a / b / rr_close / x_up / x_dn / mtm_z_grid (or mtm_r) / direction."""
    if "mtm_r" not in D.columns:
        risk = np.where(D["direction"].to_numpy() > 0, D["x_dn"].to_numpy(), D["x_up"].to_numpy())
        sigH = D["x_up"].to_numpy() / np.maximum(D["a"].to_numpy(), 1e-9)
        mtm = np.nan_to_num(D["mtm_z_grid"].to_numpy()) * sigH * D["direction"].to_numpy() / np.maximum(risk, 1e-6)
        D = D.with_columns(pl.Series("mtm_r", mtm))
    D = F.base_columns(D)
    if with_labels:
        tau = np.minimum(D["tau"].to_numpy(), H)
        w = M.avg_uniqueness(D["security_id"].to_numpy(), D["date"].to_numpy().astype("datetime64[D]"), tau)
        D = D.with_columns(pl.Series("w_uniq", w), pl.col("date").dt.year().alias("year"))
    feats = set(F.all_features())
    return D.with_columns(pl.col(c).cast(pl.Float32) for c in D.columns if c in feats and D[c].dtype in (pl.Float64, pl.Float32))


def t2_from_surface(S: pl.DataFrame, surface: Any, H: int, costs: dict[str, Any]) -> pl.DataFrame:
    """T2 probabilities / labels of setup-event rows (tech_prob_setup_events layout) at horizon H from a surface."""
    from app.techprob import barriers as B  # noqa: PLC0415

    S = S.filter((pl.col("direction") != 0) & pl.col(f"out_{H}").is_not_null() & (pl.col(f"out_{H}") >= 0))
    if S.is_empty():
        return S
    sig = S[f"v_{H}"].to_numpy() ** 0.5
    a = S["x_up"].to_numpy() / sig
    b = S["x_dn"].to_numpy() / sig
    rc = np.array([surface.slot(r)[0] for r in S["region"].to_list()])
    P = surface.eval_many(rc, S["vol_regime"].to_numpy(), S[f"earn_{H}"].to_numpy(), H, a, b)
    d = S["direction"].to_numpy()
    pw = np.where(d > 0, P[:, 0], P[:, 1])
    pl_ = np.where(d > 0, P[:, 1], P[:, 0]) + P[:, 2]
    pn = np.clip(1 - pw - pl_, 0, 1)
    risk = np.where(d > 0, S["x_dn"].to_numpy(), S["x_up"].to_numpy())
    cost = B.round_trip_cost_vec(S["adv_usd"].cast(pl.Float64).fill_null(np.nan).to_numpy(), S["sigma_d"].to_numpy(), d < 0,
                                 np.full(len(d), H / 2.0), costs) / np.maximum(risk, 1e-6)
    rr = S["rr_close"].to_numpy()
    mtm_r = P[:, 3] * sig * d / np.maximum(risk, 1e-6)
    out = S[f"out_{H}"].to_numpy()
    yw = np.where(d > 0, out == 1, out == 2)
    yl = np.where(d > 0, (out == 2) | (out == 3), (out == 1) | (out == 3))
    z = S[f"z_{H}"].cast(pl.Float64).fill_null(0.0).to_numpy() * d / np.maximum(risk, 1e-6)
    real = np.where(yw, rr, np.where(yl, -1.0, z)) - cost
    er = pw * rr - pl_ - cost
    return S.with_columns(pl.lit(H).alias("H"), pl.Series("a", a), pl.Series("b", b), pl.Series("pw", pw), pl.Series("pl", pl_),
                          pl.Series("pn", pn), pl.Series("cost_r", cost), pl.Series("mtm_r", mtm_r),
                          pl.Series("exp_r", er), pl.Series("exp_r_mtm", er + pn * mtm_r), pl.Series("real_r_mtm", real),
                          pl.Series("y_win", yw), pl.Series("y_loss", yl), pl.Series("y_nei", out == 0),
                          pl.col(f"earn_{H}").alias("earn"), pl.col(f"out_{H}").alias("out"), pl.col(f"tau_{H}").alias("tau"))


def embargo_days(H: int) -> int:
    return int(math.ceil(1.5 * H * 7 / 5)) + 7


def history_start(as_of: date) -> date:
    return as_of - timedelta(days=760)
