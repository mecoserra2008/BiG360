"""Backtesting lab: standalone simulation + statistics core (owner: backtest, roadmap v2 item #4).

Pure functions over numpy / polars (no `app` imports) so that `app.backtest.export` can embed this file verbatim in
an exported script. Inputs are a point-in-time signal panel (one row per rebalance date x entity) built by
`app.backtest._panel` and a daily price panel (`PricePanel`).

Timing convention (no look-ahead):
  * the signal row dated t uses only information known at the close of t (the panel builder guarantees it);
  * trades execute at the close of the `execution_lag_days`-th trading day strictly after t (default 1, as the ML
    labels: close(D+1)); the new weights earn returns from the following day on;
  * day d's return applies to the weights held at the close of d-1; positions drift with prices between trades.
Returns are simple daily returns in the base currency (USD by default: adj_close x official usd_per_unit).
Delisted names: after their last price the position is closed at the last price plus `delisting_return`.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations
from statistics import NormalDist
from typing import Any

import numpy as np
import polars as pl

ENGINE_VERSION = "1.1"  # 1.1: per-trade cost cap, unknown sub-dollar ADV, NAV ruin guard
TRADING_DAYS = 252
EULER_GAMMA = 0.5772156649015329
_N = NormalDist()

DEFAULT_SPREAD_BUCKETS = [  # (min ADV USD, half-spread bps): liquidity buckets on 20-day average dollar volume
    (1.0e9, 1.0), (1.0e8, 3.0), (2.0e7, 8.0), (5.0e6, 20.0), (0.0, 50.0)]


# ------------------------------------------------------------------------------------------------ price panel


@dataclass
class PricePanel:
    """Daily base-currency returns for the entity universe.

    dates: sorted trading dates (union calendar); ids: entity ids (columns); R: T x N simple returns (0 on days an
    alive entity has no print; 0 after its last price); first / last: index of the first / last priced day per
    entity (-1 when never priced); currency: trading currency per entity; bench / rf: T daily returns."""
    dates: list[date]
    ids: list[str]
    R: np.ndarray
    first: np.ndarray
    last: np.ndarray
    currency: list[str]
    bench: np.ndarray | None = None
    rf: np.ndarray | None = None
    bench_id: str | None = None
    _L: np.ndarray | None = field(default=None, repr=False)
    _D: np.ndarray | None = field(default=None, repr=False)
    _col: dict[str, int] | None = field(default=None, repr=False)

    @property
    def T(self) -> int:  # noqa: N802
        return len(self.dates)

    @property
    def col(self) -> dict[str, int]:
        if self._col is None:
            self._col = {e: i for i, e in enumerate(self.ids)}
        return self._col

    @property
    def D(self) -> np.ndarray:  # noqa: N802
        if self._D is None:
            self._D = np.array(self.dates, dtype="datetime64[D]")
        return self._D

    @property
    def L(self) -> np.ndarray:  # noqa: N802
        """Cumulative log growth per entity (T x N), for forward / holding-period returns."""
        if self._L is None:
            self._L = np.cumsum(np.log1p(np.clip(self.R, -0.999999, None)), axis=0)
        return self._L

    def day_index(self, d: date, side: str = "right") -> int:
        """Index of the first trading day > d (side=right) or >= d (side=left)."""
        return int(np.searchsorted(self.D, np.datetime64(d, "D"), side=side))

    def last_index_on_or_before(self, d: date) -> int:
        return self.day_index(d, "right") - 1


def price_panel_from_long(df: pl.DataFrame, ids: list[str] | None = None, currency: dict[str, str] | None = None,
                          start: date | None = None, end: date | None = None) -> PricePanel:
    """Long [entity_id, date, px] (base-currency adjusted price) -> PricePanel. Returns are px_t / px_prev - 1 on
    each entity's own print days; non-print days of the union calendar get 0 (price carried)."""
    df = df.filter(pl.col("px").is_not_null() & (pl.col("px") > 0))
    if start:
        df = df.filter(pl.col("date") >= start)
    if end:
        df = df.filter(pl.col("date") <= end)
    dates = sorted(df["date"].unique().to_list())
    ids = ids or sorted(df["entity_id"].unique().to_list())
    T, N = len(dates), len(ids)
    di = {d: i for i, d in enumerate(dates)}
    ci = {e: i for i, e in enumerate(ids)}
    df = df.filter(pl.col("entity_id").is_in(ids)).sort(["entity_id", "date"])
    df = df.with_columns((pl.col("px") / pl.col("px").shift(1).over("entity_id") - 1.0).alias("r"))
    R = np.zeros((T, N), dtype=np.float64)
    first = np.full(N, -1, dtype=np.int64)
    last = np.full(N, -1, dtype=np.int64)
    if df.height:
        rows = np.array([di[d] for d in df["date"].to_list()], dtype=np.int64)
        cols = np.array([ci[e] for e in df["entity_id"].to_list()], dtype=np.int64)
        r = df["r"].fill_null(0.0).fill_nan(0.0).to_numpy()
        R[rows, cols] = r
        g = df.group_by("entity_id").agg(pl.col("date").min().alias("d0"), pl.col("date").max().alias("d1"))
        for e, d0, d1 in g.iter_rows():
            first[ci[e]] = di[d0]
            last[ci[e]] = di[d1]
    cur = [(currency or {}).get(e, "USD") for e in ids]
    return PricePanel(dates=dates, ids=ids, R=R, first=first, last=last, currency=cur)


def align_series(dates: list[date], ser: pl.DataFrame | None, col: str = "r") -> np.ndarray | None:
    """[date, col] -> array aligned to `dates` (0 where missing)."""
    if ser is None or ser.is_empty():
        return None
    m = dict(zip(ser["date"].to_list(), ser[col].to_list(), strict=True))
    return np.array([float(m.get(d) or 0.0) for d in dates], dtype=np.float64)


# ------------------------------------------------------------------------------------------------ helpers


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _rank_pct(x: np.ndarray) -> np.ndarray:
    """Average-rank percentile in (0, 1): (rank - 0.5) / n (ties share their average rank)."""
    n = len(x)
    if n == 0:
        return np.zeros(0)
    _u, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    start = np.concatenate([[0], np.cumsum(cnt)[:-1]])
    avg = start + (cnt + 1) / 2.0
    return (avg[inv] - 0.5) / n


def _zscore(x: np.ndarray) -> np.ndarray:
    sd = x.std()
    return (x - x.mean()) / sd if sd > 0 else np.zeros_like(x)


def _cap_weights(w: np.ndarray, cap: float | None, budget: float) -> tuple[np.ndarray, bool]:
    """Scale w (>= 0) to sum `budget`, then cap each at `cap` redistributing the excess pro rata to uncapped names.
    Returns (weights, infeasible) - infeasible when cap * n < budget (the leg is then under-invested)."""
    s = w.sum()
    if s <= 0 or len(w) == 0:
        return np.zeros_like(w), False
    w = w / s * budget
    if not cap or cap <= 0:
        return w, False
    if cap * len(w) < budget - 1e-12:
        return np.full_like(w, cap), True
    for _ in range(100):
        over = w > cap + 1e-12
        if not over.any():
            break
        excess = (w[over] - cap).sum()
        w[over] = cap
        free = ~over & (w < cap - 1e-12)
        if not free.any():
            break
        w[free] += excess * w[free] / w[free].sum()
    return w, False


def _erc_weights(cov: np.ndarray, iters: int = 60) -> np.ndarray:
    """Equal-risk-contribution long-only weights (cyclical coordinate descent, Griveau-Billion et al. 2013)."""
    n = cov.shape[0]
    w = 1.0 / np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    w = w / w.sum()
    b = np.full(n, 1.0 / n)
    for _ in range(iters):
        for i in range(n):
            s_ii = max(cov[i, i], 1e-12)
            c = cov[i] @ w - s_ii * w[i]
            w[i] = (-c + math.sqrt(c * c + 4.0 * s_ii * b[i])) / (2.0 * s_ii)
        w = np.clip(w, 1e-12, None)
    return w / w.sum()


# ------------------------------------------------------------------------------------------------ costs


@dataclass
class CostModel:
    commission_bps: float = 1.0
    spread_model: str = "adv_buckets"
    fixed_spread_bps: float = 5.0
    spread_buckets: list[tuple[float, float]] = field(default_factory=lambda: list(DEFAULT_SPREAD_BUCKETS))
    impact_coef: float = 0.1
    capital_usd: float = 1.0e7
    borrow_bps: float = 50.0
    htb_borrow_bps: float = 300.0
    htb_adv_usd: float = 5.0e6
    fx_bps: float = 2.0
    base_currency: str = "USD"
    enabled: bool = True
    # hard ceiling on the cost of one trade per unit traded (commission + spread + FX + impact): the square-root
    # impact term is unbounded when the traded notional is many times the ADV (illiquid or zero-volume names)
    max_cost_bps: float = 1000.0
    # ADV below this (USD) is treated as unknown: a 20-day window of zero volume leaves a ~1e-12 floating-point
    # residue in rolling means, which used to make sqrt(notional / ADV) astronomically large
    min_known_adv_usd: float = 1.0
    n_capped: int = 0

    @classmethod
    def from_spec(cls, c: dict[str, Any] | None, base_currency: str = "USD") -> CostModel:
        c = c or {}
        b = c.get("spread_buckets")
        buckets = sorted([(float(x["min_adv_usd"]), float(x["half_spread_bps"])) for x in b], reverse=True) if b \
            else list(DEFAULT_SPREAD_BUCKETS)
        return cls(commission_bps=float(c.get("commission_bps", 1.0)), spread_model=c.get("spread_model", "adv_buckets"),
                   fixed_spread_bps=float(c.get("fixed_spread_bps", 5.0)), spread_buckets=buckets,
                   impact_coef=float(c.get("impact_coef", 0.1)), capital_usd=float(c.get("capital_usd", 1.0e7)),
                   borrow_bps=float(c.get("borrow_bps", 50.0)), htb_borrow_bps=float(c.get("htb_borrow_bps", 300.0)),
                   htb_adv_usd=float(c.get("htb_adv_usd", 5.0e6)), fx_bps=float(c.get("fx_bps", 2.0)),
                   base_currency=base_currency, enabled=bool(c.get("enabled", True)),
                   max_cost_bps=float(c.get("max_cost_bps") or 1000.0))

    def half_spread_bps(self, adv: np.ndarray) -> np.ndarray:
        if self.spread_model == "fixed":
            return np.full(len(adv), self.fixed_spread_bps)
        out = np.full(len(adv), self.spread_buckets[-1][1], dtype=np.float64)
        a = np.nan_to_num(adv, nan=-1.0)
        for lo, bps in reversed(self.spread_buckets):  # ascending thresholds: later (higher) buckets overwrite
            out = np.where(a >= lo, bps, out) if lo > 0 else out
        return out

    def trade_cost(self, dw: np.ndarray, adv: np.ndarray, vol: np.ndarray, fx: np.ndarray, nav: float) -> np.ndarray:
        """Cost of trading |dw| (fraction of NAV) per name, as a fraction of NAV.
        commission + half spread (ADV bucket) + FX conversion (non-base currency) per unit traded, plus square-root
        impact: impact_coef x daily vol x sqrt(traded notional / ADV). ADV below min_known_adv_usd counts as unknown
        (participation 1); the total per unit traded is capped at max_cost_bps (n_capped counts the capped trades)."""
        if not self.enabled:
            return np.zeros_like(dw)
        a = np.abs(dw)
        adv = np.asarray(adv, dtype=np.float64)
        adv_ok = np.where(np.isfinite(adv) & (adv >= self.min_known_adv_usd), adv, np.nan)
        bps = self.commission_bps + self.half_spread_bps(adv_ok) + np.where(fx, self.fx_bps, 0.0)
        unit = bps / 1e4
        if self.impact_coef > 0:
            notional = a * self.capital_usd * min(max(nav, 1e-6), 1e6)
            sig = np.nan_to_num(np.asarray(vol, dtype=np.float64), nan=0.02, posinf=0.02, neginf=0.02)
            part = np.where(np.isfinite(adv_ok), notional / np.where(np.isfinite(adv_ok), adv_ok, 1.0), 1.0)
            unit = unit + self.impact_coef * np.abs(sig) * np.sqrt(np.clip(part, 0.0, None))
        cap = self.max_cost_bps / 1e4 if self.max_cost_bps and self.max_cost_bps > 0 else None
        if cap is not None:
            over = (unit > cap) & (a > 0)
            if over.any():
                self.n_capped += int(over.sum())
                unit = np.minimum(unit, cap)
        return a * unit

    def borrow_rate_daily(self, adv: np.ndarray) -> np.ndarray:
        if not self.enabled:
            return np.zeros(len(adv))
        htb = ~np.isfinite(adv) | (adv < self.htb_adv_usd)
        return np.where(htb, self.htb_borrow_bps, self.borrow_bps) / 1e4 / TRADING_DAYS


# ------------------------------------------------------------------------------------------------ construction


@dataclass
class Book:
    """Target weights at one rebalance: column indices into PricePanel and weights (fraction of NAV)."""
    date: date
    exec_idx: int
    idx: np.ndarray
    w: np.ndarray
    signal: np.ndarray
    pct: np.ndarray
    group: list[str]
    n_eligible: int
    note: str | None = None


def construct(cs: dict[str, Any], px: PricePanel, cons: dict[str, Any], day_idx: int,
              warns: set[str] | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """One cross-section (rows: col, signal, group, mcap, vol) -> (col idx, weights, signal, pct, group).

    selection: quantile (top bucket long, bottom bucket short) | top_n | all (signal-weighted);
    neutralize: none | sector | country (selection and leg budgets per group, proportional to group size);
    weighting: equal | cap | signal | inverse_vol | risk_parity; max_weight caps per name (redistributed)."""
    warns = warns if warns is not None else set()
    side = cons.get("side", "long_only")
    sel = cons.get("selection", "quantile")
    q = int(cons.get("quantiles") or 5)
    top_n = cons.get("top_n")
    wmode = cons.get("weighting", "equal")
    neutral = cons.get("neutralize", "none")
    gross = cons.get("gross")
    gross = float(gross) if gross else (2.0 if side == "long_short" else 1.0)
    budget_long = gross / 2.0 if side == "long_short" else gross
    budget_short = gross / 2.0 if side == "long_short" else 0.0
    cap = cons.get("max_weight")
    cols = cs["col"]
    s = cs["signal"]
    g_src = cs.get("group_sector" if neutral == "sector" else "group_country") if neutral != "none" else None
    grp = list(g_src) if g_src is not None else ["_all"] * len(s)
    mcap = cs["mcap"]
    vol = cs["vol"]
    n = len(s)
    z_all = np.clip(_zscore(s), -3, 3)
    garr = np.array(grp, dtype=object)
    pct_all = np.zeros(n)
    long_m = np.zeros(n, dtype=bool)
    short_m = np.zeros(n, dtype=bool)
    gw_long: dict[str, float] = {}
    groups = sorted(set(grp))
    for g in groups:
        m = garr == g
        ng = int(m.sum())
        if ng == 0:
            continue
        p = _rank_pct(s[m])
        pct_all[m] = p
        if neutral != "none" and ng < 2:
            continue
        if sel == "top_n":
            k = int(top_n or 20)
            if neutral != "none":
                k = max(1, int(round(k * ng / n)))
            k = min(k, ng // (2 if side == "long_short" else 1)) or (1 if ng >= 1 else 0)
            order = np.argsort(-s[m], kind="mergesort")
            li = np.zeros(ng, dtype=bool)
            li[order[:k]] = True
            si = np.zeros(ng, dtype=bool)
            if side == "long_short":
                si[order[-k:]] = True
                si &= ~li
        elif sel == "all":
            li = np.ones(ng, dtype=bool) if side == "long_only" else p > 0.5
            si = np.zeros(ng, dtype=bool) if side == "long_only" else p < 0.5
        else:
            b = np.minimum((p * q).astype(int), q - 1)
            li = b == q - 1
            si = (b == 0) if side == "long_short" else np.zeros(ng, dtype=bool)
        idx = np.where(m)[0]
        long_m[idx[li]] = True
        short_m[idx[si]] = True
        gw_long[g] = ng / n
    sel_long = np.where(long_m)[0]
    sel_short = np.where(short_m)[0]

    def leg(ix: np.ndarray, budget: float, sign: float) -> np.ndarray:
        if len(ix) == 0 or budget <= 0:
            return np.zeros(0)
        out = np.zeros(len(ix))
        gl = garr[ix]
        for g in sorted(set(gl)):
            mm = gl == g
            sub = ix[mm]
            if wmode == "cap":
                mc = mcap[sub]
                if not np.isfinite(mc).any():
                    warns.add("cap weighting: market cap missing for some names (equal weight used for them)")
                    raw = np.ones(len(sub))
                else:
                    med = np.nanmedian(mc)
                    raw = np.where(np.isfinite(mc) & (mc > 0), mc, med)
            elif wmode == "signal":
                if sel == "all":
                    raw = np.abs(pct_all[sub] - 0.5) if side == "long_short" else pct_all[sub]
                else:
                    raw = np.clip(sign * z_all[sub], 0.05, None)
            elif wmode in ("inverse_vol", "risk_parity"):
                v = vol[sub]
                med = np.nanmedian(v) if np.isfinite(v).any() else 0.02
                v = np.where(np.isfinite(v) & (v > 0), v, med)
                raw = 1.0 / v
                if wmode == "risk_parity" and len(sub) >= 2:
                    raw = _rp(px, cols[sub], day_idx, int(cons.get("vol_lookback_days") or 63), raw, warns)
            else:
                raw = np.ones(len(sub))
            share = gw_long.get(g, 0.0) if neutral != "none" else 1.0
            out[mm] = raw / raw.sum() * share
        if neutral != "none":
            tot = out.sum()
            out = out / tot if tot > 0 else out
        w, bad = _cap_weights(out, cap, budget)
        if bad:
            warns.add(f"max_weight {cap} x names < leg budget {budget}: leg under-invested")
        return w * sign

    wl = leg(sel_long, budget_long, 1.0)
    ws = leg(sel_short, budget_short, -1.0)
    ix = np.concatenate([sel_long, sel_short]).astype(np.int64)
    w = np.concatenate([wl, ws])
    keep = np.abs(w) > 1e-12
    ix = ix[keep]
    return cols[ix], w[keep], s[ix], pct_all[ix], [grp[i] for i in ix]


def _rp(px: PricePanel, cols: np.ndarray, day_idx: int, lookback: int, fallback: np.ndarray, warns: set[str]) -> np.ndarray:
    lo = max(0, day_idx - lookback + 1)
    X = px.R[lo:day_idx + 1, cols]
    if X.shape[0] < 20:
        warns.add("risk parity: short return history at some rebalances (inverse-vol used)")
        return fallback
    S = np.cov(X, rowvar=False)
    S = 0.5 * S + 0.5 * np.diag(np.diag(S))  # shrink toward the diagonal
    if not np.isfinite(S).all():
        return fallback
    return _erc_weights(S)


def schedule(panel: pl.DataFrame, px: PricePanel, lag: int) -> list[tuple[date, int]]:
    """Rebalance dates t -> execution day index (lag-th trading day strictly after t; lag 0 = first day >= t)."""
    out = []
    for t in sorted(panel["date"].unique().to_list()):
        e = px.day_index(t, "left") if lag <= 0 else px.day_index(t, "right") + lag - 1
        if 0 <= e < px.T:
            out.append((t, e))
    return out


def prepare_cs(panel: pl.DataFrame, px: PricePanel, lag: int) -> list[tuple[date, int, int, dict[str, Any] | None]]:
    """Per rebalance (t, exec index, day index of t, cross-section arrays | None): eligible rows with a signal that
    are alive at t (priced on or before t and not past their last print). Arrays: col, signal, group_sector,
    group_country, mcap, vol."""
    pan = panel.filter(pl.col("signal").is_not_null() & pl.col("signal").is_not_nan())
    if "eligible" in pan.columns:
        pan = pan.filter(pl.col("eligible").fill_null(False))
    pan = pan.with_columns(pl.col("entity_id").replace_strict(px.col, default=None, return_dtype=pl.Int64).alias("col"))
    pan = pan.filter(pl.col("col").is_not_null())
    for c in ("group_sector", "group_country"):
        if c not in pan.columns:
            pan = pan.with_columns(pl.lit(None, pl.Utf8).alias(c))
    for c in ("mcap", "vol"):
        if c not in pan.columns:
            pan = pan.with_columns(pl.lit(None, pl.Float64).alias(c))
    pan = pan.sort(["date", "col"])
    dts = pan["date"].to_numpy()
    arrs = {"col": pan["col"].to_numpy(), "signal": pan["signal"].cast(pl.Float64).to_numpy(),
            "group_sector": np.array(pan["group_sector"].fill_null("_none").to_list(), dtype=object),
            "group_country": np.array(pan["group_country"].fill_null("_none").to_list(), dtype=object),
            "mcap": pan["mcap"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
            "vol": pan["vol"].cast(pl.Float64).fill_null(np.nan).to_numpy()}
    idx: dict[date, tuple[int, int]] = {}
    if len(dts):
        u, st = np.unique(dts, return_index=True)
        en = list(st[1:]) + [len(dts)]
        for d, a, b in zip(u, st, en, strict=True):
            idx[d.astype("datetime64[D]").astype(object) if hasattr(d, "astype") else d] = (int(a), int(b))
    out = []
    for t, e in schedule(panel, px, lag):
        day = px.last_index_on_or_before(t)
        ab = idx.get(t)
        if ab is None:
            out.append((t, e, day, None))
            continue
        sl = slice(*ab)
        c = arrs["col"][sl]
        ok = (px.first[c] >= 0) & (px.first[c] <= day) & (px.last[c] >= max(day - 5, 0))
        out.append((t, e, day, {k: v[sl][ok] for k, v in arrs.items()}))
    return out


def build_books(panel: pl.DataFrame | None, px: PricePanel, cons: dict[str, Any], warns: set[str] | None = None,
                regime_ok: dict[date, float] | None = None,
                prepared: list[tuple[date, int, int, dict[str, Any] | None]] | None = None) -> list[Book]:
    """Target books at every rebalance (before overlap / turnover handling)."""
    warns = warns if warns is not None else set()
    min_names = int(cons.get("min_names") or 5)
    if prepared is None:
        prepared = prepare_cs(panel, px, int(cons.get("execution_lag_days", 1)))
    books = []
    for t, e, day, cs in prepared:
        n_el = len(cs["col"]) if cs is not None else 0
        scale = 1.0 if regime_ok is None else regime_ok.get(t, 1.0)
        if cs is None or n_el < min_names or scale <= 0:
            note = "regime filter: cash" if (regime_ok is not None and scale <= 0) else (
                f"only {n_el} eligible names (< min_names {min_names}): cash")
            books.append(Book(t, e, np.zeros(0, np.int64), np.zeros(0), np.zeros(0), np.zeros(0), [], n_el, note))
            continue
        ix, w, sg, pc, gr = construct(cs, px, cons, max(day, 0), warns)
        books.append(Book(t, e, ix, w * scale, sg, pc, gr, n_el, "regime filter: scaled" if scale < 1 else None))
    return books


# ------------------------------------------------------------------------------------------------ simulation


@dataclass
class SimResult:
    dates: list[date]
    ret: np.ndarray           # daily NAV return (net of costs)
    gross_ret: np.ndarray     # daily return before trading costs and borrow
    cost: np.ndarray          # daily trading cost (fraction of NAV)
    borrow: np.ndarray        # daily borrow cost
    gross_exp: np.ndarray
    net_exp: np.ndarray
    n_long: np.ndarray
    n_short: np.ndarray
    start_idx: int
    rebalances: list[dict[str, Any]] = field(default_factory=list)   # per rebalance: date, exec_date, turnover, cost, n
    holdings: list[dict[str, Any]] = field(default_factory=list)     # target weights per rebalance
    trades: list[dict[str, Any]] = field(default_factory=list)       # per rebalance trade (rank) / round trip (event)
    warnings: list[str] = field(default_factory=list)


def _sim_core(px: PricePanel, start: int, end: int, plan: dict[int, tuple[np.ndarray, np.ndarray, dict[str, Any]]],
              costs: CostModel, adv_at: dict[int, np.ndarray], vol_at: dict[int, np.ndarray], max_turnover: float | None,
              delisting_return: float, record_trades: bool, on_trade: Any = None,
              info: dict[str, Any] | None = None) -> tuple[dict[str, np.ndarray], list[dict[str, Any]], list[dict[str, Any]]]:
    """Daily loop over [start, end]. plan: exec day -> (col idx, target weights, meta). Weights are fractions of NAV.
    Ruin: when NAV reaches zero the book is closed and every later return is 0 (info["ruined"] = that date), so a
    NAV can never turn negative and compound."""
    info = info if info is not None else {}
    T, N = end - start + 1, px.R.shape[1]
    w = np.zeros(N)
    nav = 1.0
    out = {k: np.zeros(T) for k in ("ret", "gross_ret", "cost", "borrow", "gross_exp", "net_exp", "n_long", "n_short")}
    rf = px.rf if px.rf is not None else np.zeros(px.T)
    fx = np.array([c != costs.base_currency for c in px.currency], dtype=bool)
    reb, trades = [], []
    last = px.last
    borrow_rate = np.zeros(N)
    for t in range(T):
        d = start + t
        if nav <= 0.0:
            continue                                  # ruined: flat book, zero returns from here on
        r = px.R[d]
        held = np.nonzero(w)[0]
        dl = held[(last[held] + 1 == d) & (last[held] >= 0)] if len(held) else held
        if len(dl):
            r = r.copy()
            r[dl] = delisting_return
        pos_ret = float(w[held] @ r[held]) if len(held) else 0.0
        cash_w = 1.0 - float(w.sum())
        bor = float(np.sum(np.where(w[held] < 0, -w[held] * borrow_rate[held], 0.0))) if len(held) else 0.0
        rp = max(pos_ret + cash_w * rf[d] - bor, -1.0)
        if len(held) and rp <= -1.0:
            w[:] = 0.0
            held = held[:0]
        if len(held):
            w[held] = w[held] * (1.0 + r[held]) / (1.0 + rp)
        if len(dl):
            w[dl] = 0.0
        cost = 0.0
        if d in plan:
            ix, tw, meta = plan[d]
            target = np.zeros(N)
            target[ix] = tw
            dead = (last >= 0) & (last < d)
            target[dead] = 0.0
            dw = target - w
            to = float(np.abs(dw).sum()) / 2.0
            if max_turnover and to > max_turnover:
                lam = max_turnover / to
                target = w + lam * dw
                dw = target - w
                to = max_turnover
            adv = adv_at.get(d, np.full(N, np.nan))
            vol = vol_at.get(d, np.full(N, np.nan))
            tix = np.nonzero(np.abs(dw) > 1e-10)[0]
            c_i = costs.trade_cost(dw[tix], adv[tix], vol[tix], fx[tix], nav) if len(tix) else np.zeros(0)
            cost = float(c_i.sum())
            w = target
            borrow_rate = costs.borrow_rate_daily(adv)
            reb.append({**meta, "exec_date": px.dates[d], "turnover": to, "cost": cost,
                        "n_long": int((w > 0).sum()), "n_short": int((w < 0).sum())})
            if record_trades:
                for j, ci in zip(tix.tolist(), c_i.tolist(), strict=True):
                    trades.append({"date": px.dates[d], "entity_id": px.ids[j], "delta_weight": float(dw[j]),
                                   "weight_after": float(w[j]), "cost_bps": (ci / abs(dw[j]) * 1e4) if dw[j] else 0.0})
            if on_trade is not None:
                on_trade(d, w)
        net = max((1.0 + rp) * (1.0 - min(cost, 1.0)) - 1.0, -1.0)
        nav *= 1.0 + net
        if nav <= 1e-12:
            nav = 0.0
            net = -1.0
            w[:] = 0.0
            info.setdefault("ruined", px.dates[d])
        out["ret"][t] = net
        out["gross_ret"][t] = pos_ret + cash_w * rf[d]
        out["cost"][t] = cost
        out["borrow"][t] = bor
        out["gross_exp"][t] = float(np.abs(w).sum())
        out["net_exp"][t] = float(w.sum())
        out["n_long"][t] = int((w > 0).sum())
        out["n_short"][t] = int((w < 0).sum())
    return out, reb, trades


def simulate_rank(panel: pl.DataFrame, px: PricePanel, spec: dict[str, Any], *, costs_on: bool = True,
                  record: bool = True, regime_ok: dict[date, float] | None = None,
                  books: list[Book] | None = None, prepared: list[Any] | None = None) -> SimResult:
    """Rank / quantile strategy: books at every rebalance -> overlapping tranches -> turnover limit -> daily sim."""
    cons = spec.get("construction") or {}
    warns: set[str] = set()
    if books is None:
        books = build_books(panel, px, cons, warns, regime_ok, prepared)
    if not books:
        raise ValueError("no rebalance date falls inside the price history")
    K = max(1, int(cons.get("holding_periods") or 1))
    costs = CostModel.from_spec(spec.get("costs"), (spec.get("costs") or {}).get("currency", "USD") if
                                (spec.get("costs") or {}).get("currency", "USD") != "local" else "USD")
    if not costs_on:
        costs.enabled = False
    # ADV / vol only drive costs and the borrow rate: skipped for gross (cost-free) simulations
    adv_map = _attr_by_exec(panel, px, books, "adv_usd") if costs_on and panel is not None else {}
    vol_map = _attr_by_exec(panel, px, books, "vol") if costs_on and panel is not None else {}
    tranches: deque[dict[int, float]] = deque(maxlen=K)
    plan: dict[int, tuple[np.ndarray, np.ndarray, dict[str, Any]]] = {}
    holdings = []
    for b in books:
        tranches.append({int(i): float(x) for i, x in zip(b.idx.tolist(), b.w.tolist(), strict=True)})
        if K > 1:
            agg: dict[int, float] = {}
            for tr in tranches:
                for i, x in tr.items():
                    agg[i] = agg.get(i, 0.0) + x / len(tranches)
            ix = np.array(list(agg.keys()), dtype=np.int64)
            tw = np.array(list(agg.values()))
        else:
            ix, tw = b.idx, b.w
        plan[b.exec_idx] = (ix, tw, {"date": b.date, "n_eligible": b.n_eligible, "note": b.note})
        if record:
            sig = dict(zip(b.idx.tolist(), b.signal.tolist(), strict=True))
            pct = dict(zip(b.idx.tolist(), b.pct.tolist(), strict=True))
            grp = dict(zip(b.idx.tolist(), b.group, strict=True))
            for i, x in zip(ix.tolist(), tw.tolist(), strict=True):
                holdings.append({"date": b.date, "entity_id": px.ids[i], "weight": x, "signal": sig.get(i),
                                 "rank_pct": pct.get(i), "group": grp.get(i)})
    # the simulation starts at the first book that holds something (signals with a late start - e.g. futures carry
    # once curve history exists - would otherwise add years of cash to every statistic)
    first = next((b for b in books if len(b.idx) or (b.note or "").startswith("regime")), books[0])
    start = first.exec_idx
    end = px.T - 1
    info: dict[str, Any] = {}
    out, reb, trades = _sim_core(px, start, end, plan, costs, adv_map, vol_map, cons.get("max_turnover"),
                                 float(spec.get("delisting_return", 0.0) or 0.0), record, info=info)
    _cost_warnings(costs, info, warns)
    res = SimResult(dates=px.dates[start:end + 1], start_idx=start, rebalances=reb, holdings=holdings, trades=trades,
                    warnings=sorted(warns), **out)
    return res


def _cost_warnings(costs: CostModel, info: dict[str, Any], warns: set[str]) -> None:
    if costs.n_capped:
        warns.add(f"{costs.n_capped} trades hit the {costs.max_cost_bps:g} bp per-trade cost cap (traded notional many "
                  "times the ADV: illiquid names - consider a min ADV screen)")
    if info.get("ruined"):
        warns.add(f"NAV fell to zero on {info['ruined']}: the strategy was wiped out and held cash afterwards "
                  "(results after that date are not meaningful)")


def _attr_by_exec(panel: pl.DataFrame, px: PricePanel, books: list[Book], col: str) -> dict[int, np.ndarray]:
    """Per execution day: attribute (ADV, vol) of every entity known at the rebalance date (NaN if unknown)."""
    if col not in panel.columns:
        return {}
    cm = px.col
    sub = panel.select("date", "entity_id", col).filter(pl.col(col).is_not_null()).with_columns(
        pl.col("entity_id").replace_strict(cm, default=None, return_dtype=pl.Int64).alias("_c")).filter(pl.col("_c").is_not_null())
    parts = {d: g for (d,), g in sub.partition_by("date", as_dict=True).items()}
    out: dict[int, np.ndarray] = {}
    for b in books:
        g = parts.get(b.date)
        arr = np.full(len(px.ids), np.nan)
        if g is not None:
            arr[g["_c"].to_numpy()] = g[col].to_numpy().astype(np.float64)
        out[b.exec_idx] = arr
    return out


def simulate_events(events: pl.DataFrame, px: PricePanel, spec: dict[str, Any], attrs: pl.DataFrame | None = None,
                    *, costs_on: bool = True, record: bool = True) -> SimResult:
    """Event-driven mode. events: [entity_id, date (known at close), direction (+1/-1), strength] and optionally
    per-event `stop_loss` / `take_profit` columns (fractions; a non-null value overrides the spec's for that trade)
    and a per-event `weight` (position weight; null = position_weight, <= 0 = not traded).
    Entry at the close of the entry_lag-th trading day after the event; exit at the close on which the holding time
    reaches hold_days, the trade return hits -stop_loss / +take_profit, or the name stops trading."""
    ev = spec.get("event") or {}
    hold = int(ev.get("hold_days") or 20)
    stop = ev.get("stop_loss")
    tp = ev.get("take_profit")
    maxpos = int(ev.get("max_positions") or 20)
    pw = float(ev.get("position_weight") or (1.0 / maxpos))
    lag = int(ev.get("entry_lag_days", 1))
    allow_short = bool(ev.get("allow_short", False))
    gross_cap = float(ev.get("max_gross") or (2.0 if allow_short else 1.0))
    costs = CostModel.from_spec(spec.get("costs"))
    if not costs_on:
        costs.enabled = False
    cm = px.col
    e = events.filter(pl.col("entity_id").is_in(list(cm)))
    if not allow_short:
        e = e.filter(pl.col("direction") > 0)
    rows = []
    sl_col = e["stop_loss"].to_list() if "stop_loss" in e.columns else [None] * e.height
    tp_col = e["take_profit"].to_list() if "take_profit" in e.columns else [None] * e.height
    wt_col = e["weight"].to_list() if "weight" in e.columns else [None] * e.height     # optional per-event weight
    for (ent, d, dirn, stg), sl_i, tp_i, w_i in zip(e.select("entity_id", "date", "direction", "strength").iter_rows(),
                                                    sl_col, tp_col, wt_col, strict=True):
        k = px.day_index(d, "right") + lag - 1 if lag > 0 else px.day_index(d, "left")
        if 0 <= k < px.T:
            rows.append((k, -(stg if stg is not None else 0.0), cm[ent], int(np.sign(dirn) or 1), d, sl_i, tp_i, w_i))
    rows.sort(key=lambda x: x[:3])
    by_day: dict[int, list[tuple[int, int, date, float, Any, Any, Any]]] = {}
    for k, ns, j, dr, d, sl_i, tp_i, w_i in rows:
        by_day.setdefault(k, []).append((j, dr, d, -ns, sl_i, tp_i, w_i))
    adv = np.full(len(px.ids), np.nan)
    vol = np.full(len(px.ids), np.nan)
    if attrs is not None and not attrs.is_empty():
        last_attr = attrs.sort("date").group_by("entity_id").agg(pl.col("adv_usd").last(), pl.col("vol").last()) \
            if {"adv_usd", "vol"} <= set(attrs.columns) else None
        if last_attr is not None:
            for ent, a, v in last_attr.iter_rows():
                if ent in cm:
                    adv[cm[ent]] = a if a is not None else np.nan
                    vol[cm[ent]] = v if v is not None else np.nan
    start = min(by_day) if by_day else 0
    T, N = px.T - start, len(px.ids)
    w = np.zeros(N)
    out = {k: np.zeros(max(T, 0)) for k in ("ret", "gross_ret", "cost", "borrow", "gross_exp", "net_exp", "n_long", "n_short")}
    rf = px.rf if px.rf is not None else np.zeros(px.T)
    fx = np.array([c != costs.base_currency for c in px.currency], dtype=bool)
    br = costs.borrow_rate_daily(adv)
    L = px.L
    open_: dict[int, dict[str, Any]] = {}
    trades: list[dict[str, Any]] = []
    skipped = 0
    for t in range(T):
        d = start + t
        r = px.R[d]
        held = np.nonzero(w)[0]
        dl = held[(px.last[held] + 1 == d) & (px.last[held] >= 0)] if len(held) else held
        if len(dl):
            r = r.copy()
            r[dl] = float(spec.get("delisting_return", 0.0) or 0.0)
        pos_ret = float(w[held] @ r[held]) if len(held) else 0.0
        cash_w = 1.0 - float(w.sum())
        bor = float(np.sum(np.where(w[held] < 0, -w[held] * br[held], 0.0))) if len(held) else 0.0
        rp = pos_ret + cash_w * rf[d] - bor
        if len(held):
            w[held] = w[held] * (1.0 + r[held]) / (1.0 + rp)
        cost = 0.0
        # exits (evaluated on the close of d)
        for j in list(open_):
            tr = open_[j]
            tret = math.exp(L[d, j] - L[tr["k"], j]) - 1.0
            pnl = tr["dir"] * tret
            reason = None
            if px.last[j] >= 0 and px.last[j] < d:
                reason = "delisted"
            elif (tr.get("sl") if tr.get("sl") is not None else stop) is not None and                     pnl <= -float(tr.get("sl") if tr.get("sl") is not None else stop):
                reason = "stop"
            elif (tr.get("tp") if tr.get("tp") is not None else tp) is not None and                     pnl >= float(tr.get("tp") if tr.get("tp") is not None else tp):
                reason = "target"
            elif d - tr["k"] >= hold:
                reason = "time"
            elif d == px.T - 1:
                reason = "open"
            if reason:
                c = float(costs.trade_cost(np.array([w[j]]), adv[j:j + 1], vol[j:j + 1], fx[j:j + 1], 1.0)[0]) if reason not in ("delisted", "open") else 0.0
                cost += c
                if reason != "open":
                    w[j] = 0.0
                trades.append({"entity_id": px.ids[j], "event_date": tr["event_date"], "entry_date": px.dates[tr["k"]],
                               "exit_date": px.dates[d], "direction": tr["dir"], "return": pnl, "holding_days": d - tr["k"],
                               "exit_reason": reason, "strength": tr["strength"], "weight": tr["w"]})
                if reason != "open":
                    del open_[j]
        # entries (close of d)
        for j, dr, evd, stg, sl_i, tp_i, w_i in by_day.get(d, []):
            if j in open_ or (px.last[j] >= 0 and px.last[j] < d) or px.first[j] < 0 or px.first[j] > d:
                continue
            pw_i = pw if w_i is None or not np.isfinite(w_i) else float(w_i)
            if pw_i <= 0:
                continue                                   # sized to zero: not traded
            if len(open_) >= maxpos or float(np.abs(w).sum()) + pw_i > gross_cap + 1e-9:
                skipped += 1
                continue
            w[j] = dr * pw_i
            cost += float(costs.trade_cost(np.array([pw_i]), adv[j:j + 1], vol[j:j + 1], fx[j:j + 1], 1.0)[0])
            open_[j] = {"k": d, "dir": dr, "event_date": evd, "strength": stg, "w": pw_i, "sl": sl_i, "tp": tp_i}
        net = max((1.0 + rp) * (1.0 - min(cost, 1.0)) - 1.0, -1.0)
        out["ret"][t] = net
        out["gross_ret"][t] = pos_ret + cash_w * rf[d]
        out["cost"][t] = cost
        out["borrow"][t] = bor
        out["gross_exp"][t] = float(np.abs(w).sum())
        out["net_exp"][t] = float(w.sum())
        out["n_long"][t] = int((w > 0).sum())
        out["n_short"][t] = int((w < 0).sum())
    warns = [f"{skipped} events skipped: max_positions / gross cap reached"] if skipped else []
    cw: set[str] = set()
    _cost_warnings(costs, {}, cw)
    warns += sorted(cw)
    return SimResult(dates=px.dates[start:], start_idx=start, trades=trades if record else [], warnings=warns, **out)


def event_paths(events: pl.DataFrame, px: PricePanel, horizon: int = 20, lag: int = 1) -> dict[str, Any]:
    """Average cumulative (direction-adjusted) return path after event entries, raw and minus the benchmark."""
    cm = px.col
    L = px.L
    B = np.cumsum(np.log1p(px.bench)) if px.bench is not None else None
    paths, ex = [], []
    for ent, d, dirn in events.select("entity_id", "date", "direction").iter_rows():
        j = cm.get(ent)
        if j is None:
            continue
        k = px.day_index(d, "right") + lag - 1 if lag > 0 else px.day_index(d, "left")
        if k < 0 or k + horizon >= px.T or px.first[j] < 0 or px.first[j] > k or px.last[j] < k + horizon:
            continue
        p = np.exp(L[k:k + horizon + 1, j] - L[k, j]) - 1.0
        s = float(np.sign(dirn) or 1)
        paths.append(s * p)
        if B is not None:
            ex.append(s * (p - (np.exp(B[k:k + horizon + 1] - B[k]) - 1.0)))
    if not paths:
        return {"days": list(range(horizon + 1)), "n": 0, "mean": [], "lo": [], "hi": [], "excess_mean": []}
    P = np.vstack(paths)
    m, sd = P.mean(0), P.std(0, ddof=1) if len(P) > 1 else np.zeros(P.shape[1])
    se = sd / math.sqrt(len(P))
    out = {"days": list(range(horizon + 1)), "n": len(P), "mean": m.tolist(), "lo": (m - 1.96 * se).tolist(),
           "hi": (m + 1.96 * se).tolist(), "excess_mean": np.vstack(ex).mean(0).tolist() if ex else []}
    return out


# ------------------------------------------------------------------------------------------------ metrics


def _years(dates: list[date]) -> float:
    if len(dates) < 2:
        return 0.0
    return max((dates[-1] - dates[0]).days / 365.25, 1.0 / 365.25)


def drawdown(nav: np.ndarray) -> np.ndarray:
    peak = np.maximum.accumulate(np.concatenate([[1.0], nav]))[1:]
    return nav / peak - 1.0


def perf_metrics(ret: np.ndarray, dates: list[date], bench: np.ndarray | None = None, rf: np.ndarray | None = None,
                 extra: dict[str, Any] | None = None, invested: np.ndarray | None = None) -> dict[str, Any]:
    """Headline statistics of a daily return series (annualised with 252 days; Sharpe / Sortino on excess of rf)."""
    n = len(ret)
    out: dict[str, Any] = {"n_days": n, "start": dates[0].isoformat() if n else None,
                           "end": dates[-1].isoformat() if n else None}
    if n < 2:
        return out
    rf = rf if rf is not None else np.zeros(n)
    nav = np.cumprod(1.0 + ret)
    yrs = _years(dates)
    ex = ret - rf
    sd = ex.std(ddof=1)
    dd = drawdown(nav)
    i_trough = int(np.argmin(dd))
    peak_i = int(np.argmax(nav[:i_trough + 1])) if i_trough > 0 else 0
    # longest drawdown spell in days
    longest, cur = 0, 0
    for x in dd:
        cur = cur + 1 if x < -1e-12 else 0
        longest = max(longest, cur)
    down = np.minimum(ex, 0.0)
    dsd = math.sqrt(float(np.mean(down * down)))
    # hit rate over invested days only (cash days earning the bill rate would otherwise count as "hits")
    active = ret[(np.abs(ret) > 1e-14) & (invested if invested is not None and len(invested) == n else True)]
    mret = monthly_returns(ret, dates)
    m3 = float(((ex - ex.mean()) ** 3).mean() / sd ** 3) if sd > 0 else None
    m4 = float(((ex - ex.mean()) ** 4).mean() / sd ** 4) if sd > 0 else None
    out.update({
        "total_return": float(nav[-1] - 1.0),
        "cagr": float(nav[-1] ** (1.0 / yrs) - 1.0) if nav[-1] > 0 else -1.0,
        "vol": float(ret.std(ddof=1) * math.sqrt(TRADING_DAYS)),
        "sharpe": float(ex.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else None,
        "sortino": float(ex.mean() * TRADING_DAYS / (dsd * math.sqrt(TRADING_DAYS))) if dsd > 0 else None,
        "max_drawdown": float(dd.min()),
        "max_drawdown_peak": dates[peak_i].isoformat(), "max_drawdown_trough": dates[i_trough].isoformat(),
        "max_drawdown_days": int(longest),
        "hit_rate": float((active > 0).mean()) if len(active) else None,
        "pct_months_positive": float((mret["ret"] > 0).mean()) if mret.height else None,
        "best_month": _f(mret["ret"].max()) if mret.height else None,
        "worst_month": _f(mret["ret"].min()) if mret.height else None,
        "skew": m3, "kurtosis": m4, "years": yrs,
    })
    out["calmar"] = out["cagr"] / abs(out["max_drawdown"]) if out["max_drawdown"] < 0 else None
    if bench is not None and len(bench) == n:
        bex = bench - rf
        vb = bex.var(ddof=1)
        beta = float(np.cov(ex, bex, ddof=1)[0, 1] / vb) if vb > 1e-16 else None
        act = ret - bench
        te = act.std(ddof=1) * math.sqrt(TRADING_DAYS)
        bnav = np.cumprod(1.0 + bench)
        out.update({
            "beta": beta, "alpha": float((ex.mean() - (beta or 0.0) * bex.mean()) * TRADING_DAYS) if beta is not None else None,
            "tracking_error": float(te), "information_ratio": float(act.mean() * TRADING_DAYS / te) if te > 0 else None,
            "correlation": float(np.corrcoef(ret, bench)[0, 1]) if ret.std() > 1e-12 and bench.std() > 1e-12 else None,
            "bench_cagr": float(bnav[-1] ** (1.0 / yrs) - 1.0) if bnav[-1] > 0 else -1.0,
            "bench_vol": float(bench.std(ddof=1) * math.sqrt(TRADING_DAYS)),
            "bench_sharpe": float(bex.mean() / bex.std(ddof=1) * math.sqrt(TRADING_DAYS)) if bex.std() > 1e-12 else None,
            "bench_max_drawdown": float(drawdown(bnav).min()),
            "excess_cagr": None,
        })
        out["excess_cagr"] = out["cagr"] - out["bench_cagr"]
    if extra:
        out.update(extra)
    return out


def monthly_returns(ret: np.ndarray, dates: list[date]) -> pl.DataFrame:
    if not len(ret):
        return pl.DataFrame(schema={"month": pl.Date, "ret": pl.Float64})
    df = pl.DataFrame({"date": dates, "r": ret}, schema={"date": pl.Date, "r": pl.Float64})
    return df.group_by(pl.col("date").dt.truncate("1mo").alias("month")).agg(
        ((pl.col("r") + 1.0).product() - 1.0).alias("ret")).sort("month")


def rolling_metrics(ret: np.ndarray, dates: list[date], bench: np.ndarray | None, window: int = 252,
                    step: int = 5) -> dict[str, Any]:
    """Trailing-window return / vol / Sharpe / beta, sampled every `step` days."""
    n = len(ret)
    xs, rr, vv, ss, bb = [], [], [], [], []
    lg = np.log1p(ret)
    cs = np.concatenate([[0.0], np.cumsum(lg)])
    for i in range(window - 1, n, step):
        seg = ret[i - window + 1:i + 1]
        sd = seg.std(ddof=1)
        xs.append(dates[i].isoformat())
        rr.append(float(math.exp(cs[i + 1] - cs[i + 1 - window]) - 1.0))
        vv.append(float(sd * math.sqrt(TRADING_DAYS)))
        ss.append(float(seg.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 else None)
        if bench is not None:
            b = bench[i - window + 1:i + 1]
            vb = b.var(ddof=1)
            bb.append(float(np.cov(seg, b, ddof=1)[0, 1] / vb) if vb > 0 else None)
    return {"window_days": window, "dates": xs, "return": rr, "vol": vv, "sharpe": ss, "beta": bb if bench is not None else []}


def turnover_stats(reb: list[dict[str, Any]], years: float) -> dict[str, Any]:
    if not reb:
        return {"avg_turnover": None, "annual_turnover": None, "n_rebalances": 0, "total_cost": 0.0, "annual_cost_bps": None}
    to = np.array([r["turnover"] for r in reb[1:]] or [reb[0]["turnover"]])
    c = float(sum(r["cost"] for r in reb))
    per_year = len(reb) / years if years > 0 else None
    return {"avg_turnover": float(to.mean()), "annual_turnover": float(to.mean() * per_year) if per_year else None,
            "n_rebalances": len(reb), "total_cost": c,
            "annual_cost_bps": float(c / years * 1e4) if years > 0 else None}


# ------------------------------------------------------------------------------------------------ cross-sectional analytics


def forward_returns(px: PricePanel, idx0: np.ndarray, idx1: np.ndarray, cols: np.ndarray) -> np.ndarray:
    """Simple return of entity cols between day indices idx0 -> idx1 (close to close)."""
    L = px.L
    return np.exp(L[idx1, cols] - L[idx0, cols]) - 1.0


def _date_slices(pan: pl.DataFrame, cols: list[str]) -> tuple[dict[str, np.ndarray], dict[date, tuple[int, int]]]:
    """Frame sorted by date -> numpy columns (floats with NaN for nulls) + date -> row slice."""
    pan = pan.sort("date")
    arrs = {c: (pan[c].to_numpy() if c == "col" else pan[c].cast(pl.Float64).fill_null(np.nan).to_numpy()) for c in cols}
    dts = pan["date"].to_numpy()
    idx: dict[date, tuple[int, int]] = {}
    if len(dts):
        u, st = np.unique(dts, return_index=True)
        en = list(st[1:]) + [len(dts)]
        for d, a, b in zip(u, st, en, strict=True):
            idx[d.astype("datetime64[D]").astype(object)] = (int(a), int(b))
    return arrs, idx


def decile_analysis(panel: pl.DataFrame, px: PricePanel, lag: int, n_q: int = 10, min_names: int = 20) -> dict[str, Any]:
    """Per rebalance: quantile portfolios (equal weight) of the eligible universe sorted on the signal, held to the
    next execution; mean period return per quantile, cumulative quantile curves, top-minus-bottom spread, rank IC."""
    pan = panel.filter(pl.col("signal").is_not_null() & pl.col("signal").is_not_nan())
    if "eligible" in pan.columns:
        pan = pan.filter(pl.col("eligible").fill_null(False))
    cm = px.col
    pan = pan.with_columns(pl.col("entity_id").replace_strict(cm, default=None, return_dtype=pl.Int64).alias("col")) \
        .filter(pl.col("col").is_not_null())
    sch = schedule(panel, px, lag)
    arrs, idx = _date_slices(pan, ["col", "signal"])
    qr, ics, dts, ns = [], [], [], []
    for (t, e0), (_, e1) in zip(sch[:-1], sch[1:], strict=True):
        ab = idx.get(t)
        if ab is None or ab[1] - ab[0] < max(min_names, 2 * n_q):
            continue
        c = arrs["col"][ab[0]:ab[1]]
        day = px.last_index_on_or_before(t)
        ok = (px.first[c] >= 0) & (px.first[c] <= day) & (px.last[c] >= e0)
        c = c[ok]
        s = arrs["signal"][ab[0]:ab[1]][ok]
        if len(c) < max(min_names, 2 * n_q):
            continue
        fr = forward_returns(px, np.full(len(c), e0), np.full(len(c), e1), c)
        p = _rank_pct(s)
        b = np.minimum((p * n_q).astype(int), n_q - 1)
        qr.append([float(fr[b == k].mean()) if (b == k).any() else np.nan for k in range(n_q)])
        rs, rr = _rank_pct(s), _rank_pct(fr)
        ics.append(float(np.corrcoef(rs, rr)[0, 1]) if rs.std() > 0 and rr.std() > 0 else np.nan)
        dts.append(t)
        ns.append(len(c))
    if not qr:
        return {"n_quantiles": n_q, "n_periods": 0}
    Q = np.array(qr)
    spread = Q[:, -1] - Q[:, 0]
    ic = np.array(ics)
    icv = ic[np.isfinite(ic)]
    ppy = len(dts) / max(((dts[-1] - dts[0]).days / 365.25), 1e-9) if len(dts) > 1 else 12.0
    cum = np.cumprod(1.0 + np.nan_to_num(Q, nan=0.0), axis=0) - 1.0
    mono = None
    mq = np.nanmean(Q, axis=0)
    if np.isfinite(mq).all():
        mono = float(np.corrcoef(np.arange(n_q), mq)[0, 1]) if mq.std() > 0 else 0.0
    return {
        "n_quantiles": n_q, "n_periods": len(dts), "dates": [d.isoformat() for d in dts], "n_names": ns,
        "mean_return": [float(x) for x in mq], "ann_return": [float((1 + x) ** ppy - 1) for x in mq],
        "hit_rate": [float(np.nanmean(Q[:, k] > 0)) for k in range(n_q)],
        "cumulative": [[float(x) for x in cum[:, k]] for k in range(n_q)],
        "spread_mean": float(np.nanmean(spread)), "spread_ann": float((1 + np.nanmean(spread)) ** ppy - 1),
        "spread_t": float(np.nanmean(spread) / (np.nanstd(spread, ddof=1) / math.sqrt(np.isfinite(spread).sum())))
        if np.isfinite(spread).sum() > 2 and np.nanstd(spread) > 0 else None,
        "spread_cumulative": (np.cumprod(1.0 + np.nan_to_num(spread)) - 1.0).tolist(),
        "ic": [None if not math.isfinite(x) else x for x in ics], "ic_mean": float(icv.mean()) if len(icv) else None,
        "ic_ir": float(icv.mean() / icv.std(ddof=1)) if len(icv) > 2 and icv.std() > 0 else None,
        "ic_t": float(icv.mean() / (icv.std(ddof=1) / math.sqrt(len(icv)))) if len(icv) > 2 and icv.std() > 0 else None,
        "ic_hit_rate": float((icv > 0).mean()) if len(icv) else None, "monotonicity": mono,
        "periods_per_year": ppy,
    }


def period_returns(ret: np.ndarray, sim_dates: list[date], boundaries: list[date]) -> np.ndarray:
    """Compound daily returns into periods ending at each boundary date (boundary k covers (b_{k-1}, b_k])."""
    d = np.array(sim_dates, dtype="datetime64[D]")
    lg = np.concatenate([[0.0], np.cumsum(np.log1p(ret))])
    out = []
    prev = 0
    for b in boundaries:
        i = int(np.searchsorted(d, np.datetime64(b, "D"), side="right"))
        out.append(math.exp(lg[i] - lg[prev]) - 1.0 if i > prev else 0.0)
        prev = i
    return np.array(out)


def ols_hac(y: np.ndarray, X: np.ndarray, lags: int | None = None) -> dict[str, Any]:
    """OLS with Newey-West (Bartlett) standard errors. X without constant (added)."""
    n = len(y)
    Z = np.column_stack([np.ones(n), X])
    k = Z.shape[1]
    ZtZ_inv = np.linalg.pinv(Z.T @ Z)
    b = ZtZ_inv @ Z.T @ y
    u = y - Z @ b
    L = lags if lags is not None else int(math.floor(4 * (n / 100.0) ** (2.0 / 9.0)))
    S = (Z * u[:, None]).T @ (Z * u[:, None])
    for l in range(1, L + 1):
        wl = 1.0 - l / (L + 1.0)
        G = (Z[l:] * u[l:, None]).T @ (Z[:-l] * u[:-l, None])
        S += wl * (G + G.T)
    V = ZtZ_inv @ S @ ZtZ_inv * (n / max(n - k, 1))
    se = np.sqrt(np.clip(np.diag(V), 0, None))
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((u ** 2).sum()) / ss_tot if ss_tot > 0 else None
    return {"coef": b, "se": se, "t": np.where(se > 0, b / np.where(se > 0, se, 1), np.nan), "r2": r2, "n": n,
            "resid_sd": float(u.std(ddof=k)) if n > k else None, "lags": L}


def factor_series(panel: pl.DataFrame, px: PricePanel, lag: int, factor_cols: list[str], frac: float = 1.0 / 3.0,
                  invert: tuple[str, ...] = ("mcap",)) -> tuple[list[date], dict[str, np.ndarray]]:
    """Long-short factor returns per rebalance period from panel columns (top minus bottom `frac`, equal weight,
    sorted at t over the panel universe; `invert` columns are sorted low-minus-high, e.g. size = small minus big)."""
    cm = px.col
    pan = panel.with_columns(pl.col("entity_id").replace_strict(cm, default=None, return_dtype=pl.Int64).alias("col")) \
        .filter(pl.col("col").is_not_null())
    if "member" in pan.columns:
        pan = pan.filter(pl.col("member").fill_null(True))
    sch = schedule(panel, px, lag)
    have = [f for f in factor_cols if f in pan.columns]
    arrs, idx = _date_slices(pan, ["col"] + have)
    out: dict[str, list[float]] = {f: [] for f in factor_cols}
    ends = []
    for (t, e0), (_, e1) in zip(sch[:-1], sch[1:], strict=True):
        ab = idx.get(t)
        ends.append(px.dates[e1])
        if ab is None:
            for f in factor_cols:
                out[f].append(np.nan)
            continue
        sl = slice(*ab)
        c = arrs["col"][sl]
        day = px.last_index_on_or_before(t)
        ok = (px.first[c] >= 0) & (px.first[c] <= day) & (px.last[c] >= e0)
        fr_all = forward_returns(px, np.full(len(c), e0), np.full(len(c), e1), c)
        for f in factor_cols:
            if f not in arrs:
                out[f].append(np.nan)
                continue
            v = arrs[f][sl]
            m = ok & np.isfinite(v)
            if m.sum() < 15:
                out[f].append(np.nan)
                continue
            fr, p = fr_all[m], _rank_pct(v[m])
            top, bot = fr[p > 1 - frac], fr[p <= frac]
            if not len(top) or not len(bot):
                out[f].append(np.nan)
                continue
            hi, lo = top.mean(), bot.mean()
            out[f].append(float(lo - hi) if f in invert else float(hi - lo))
    return ends, {f: np.array(v) for f, v in out.items()}


def attribution(strat_period: np.ndarray, factors: dict[str, np.ndarray], rf_period: np.ndarray | None,
                ppy: float, labels: dict[str, str] | None = None) -> dict[str, Any]:
    """Time-series regression of strategy period excess returns on factor returns (Newey-West SEs).
    contribution = beta x mean factor return, annualised; alpha annualised."""
    names = [f for f, v in factors.items() if np.isfinite(v).sum() >= 12 and np.nanstd(v) > 0]
    y = strat_period - (rf_period if rf_period is not None else 0.0)
    if not names:
        return {"factors": [], "n": 0}
    X = np.column_stack([factors[f] for f in names])
    ok = np.isfinite(y) & np.isfinite(X).all(axis=1)
    if ok.sum() < len(names) + 8:
        return {"factors": [], "n": int(ok.sum())}
    fit = ols_hac(y[ok], X[ok])
    rows = []
    for i, f in enumerate(names):
        mf = float(np.mean(X[ok, i]))
        rows.append({"factor": f, "label": (labels or {}).get(f, f), "beta": float(fit["coef"][i + 1]),
                     "t_stat": _f(fit["t"][i + 1]), "factor_mean_ann": mf * ppy,
                     "contribution_ann": float(fit["coef"][i + 1] * mf * ppy)})
    return {"n": int(ok.sum()), "alpha_ann": float(fit["coef"][0] * ppy), "alpha_t": _f(fit["t"][0]), "r2": fit["r2"],
            "factors": rows, "total_ann": float(np.mean(y[ok]) * ppy), "hac_lags": fit["lags"]}


def exposures(holdings: list[dict[str, Any]], panel: pl.DataFrame, cols: list[str]) -> dict[str, Any]:
    """Average weighted cross-sectional z-score of each attribute for the book (long minus short), over rebalances."""
    if not holdings:
        return {}
    h = pl.DataFrame(holdings).select("date", "entity_id", "weight")
    out: dict[str, Any] = {}
    have = [c for c in cols if c in panel.columns]
    if not have:
        return {}
    z = panel.select(["date", "entity_id"] + have).with_columns(
        [((pl.col(c) - pl.col(c).mean().over("date")) / pl.col(c).std().over("date")).alias(c) for c in have])
    j = h.join(z, on=["date", "entity_id"], how="left")
    for c in have:
        g = j.filter(pl.col(c).is_not_null()).group_by("date").agg((pl.col("weight") * pl.col(c)).sum().alias("e"))
        out[c] = _f(g["e"].mean()) if g.height else None
    return out


def regime_breakdown(ret: np.ndarray, dates: list[date], bench: np.ndarray | None, states: pl.DataFrame) -> list[dict[str, Any]]:
    """Strategy (and benchmark) monthly returns grouped by the macro regime state of the same month (descriptive,
    contemporaneous). states: [regime_type, scope, month (month start), state, model_based]."""
    if states is None or states.is_empty() or len(ret) < 40:
        return []
    m = monthly_returns(ret, dates)
    if bench is not None:
        mb = monthly_returns(bench, dates).rename({"ret": "bench"})
        m = m.join(mb, on="month", how="left")
    else:
        m = m.with_columns(pl.lit(None, pl.Float64).alias("bench"))
    j = m.join(states.rename({"month": "month"}), on="month", how="inner")
    out = []
    for (rt, sc, st), g in j.group_by(["regime_type", "scope", "state"], maintain_order=True):
        r = g["ret"].to_numpy()
        b = g["bench"].to_numpy().astype(float)
        sd = r.std(ddof=1) if len(r) > 1 else 0.0
        out.append({"regime_type": rt, "scope": sc, "state": st, "n_months": len(r),
                    "mean_monthly": float(r.mean()), "ann_return": float((1 + r.mean()) ** 12 - 1),
                    "ann_vol": float(sd * math.sqrt(12)) if sd else None,
                    "sharpe": float(r.mean() / sd * math.sqrt(12)) if sd > 0 else None,
                    "hit_rate": float((r > 0).mean()),
                    "bench_ann_return": float((1 + np.nanmean(b)) ** 12 - 1) if np.isfinite(b).any() else None,
                    "model_based": bool(g["model_based"][0]) if "model_based" in g.columns else None})
    return sorted(out, key=lambda x: (x["regime_type"], x["scope"], x["state"]))


# ------------------------------------------------------------------------------------------------ overfitting statistics


def sharpe_stats(ret: np.ndarray, rf: np.ndarray | None = None) -> dict[str, float | None]:
    """Per-period Sharpe and the higher moments used by PSR / DSR (kurtosis is NOT excess)."""
    ex = ret - (rf if rf is not None else 0.0)
    n = len(ex)
    sd = ex.std(ddof=1) if n > 1 else 0.0
    if n < 3 or sd <= 0:
        return {"sr": None, "n": n, "skew": None, "kurtosis": None}
    mu = ex.mean()
    s0 = ex.std()
    return {"sr": float(mu / sd), "n": n, "skew": float(((ex - mu) ** 3).mean() / s0 ** 3),
            "kurtosis": float(((ex - mu) ** 4).mean() / s0 ** 4)}


def psr(sr: float, n: int, skew: float, kurtosis: float, sr_star: float = 0.0) -> float:
    """Probabilistic Sharpe ratio P(SR > sr_star) (Bailey & Lopez de Prado 2012); per-period SRs."""
    den = 1.0 - skew * sr + (kurtosis - 1.0) / 4.0 * sr * sr
    if den <= 0 or n < 2:
        return float("nan")
    return _N.cdf((sr - sr_star) * math.sqrt(n - 1) / math.sqrt(den))


def expected_max_sr(n_trials: int, sr_var: float) -> float:
    """E[max SR] of n_trials unskilled trials with variance sr_var of the trial SRs (False Strategy theorem)."""
    if n_trials <= 1 or sr_var <= 0:
        return 0.0
    return math.sqrt(sr_var) * ((1.0 - EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n_trials)
                                + EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(sr: float, n: int, skew: float, kurtosis: float, n_trials: int, sr_var: float) -> dict[str, Any]:
    """Deflated Sharpe ratio (Bailey & Lopez de Prado 2014): PSR against the expected maximum SR of n_trials."""
    sr0 = expected_max_sr(n_trials, sr_var)
    return {"sr0": sr0, "dsr": psr(sr, n, skew, kurtosis, sr0), "psr": psr(sr, n, skew, kurtosis, 0.0),
            "n_trials": int(n_trials), "sr_var": sr_var}


def pbo_cscv(M: np.ndarray, n_blocks: int = 10) -> dict[str, Any]:
    """Probability of backtest overfitting by combinatorially symmetric cross-validation (Bailey, Borwein,
    Lopez de Prado & Zhu 2017). M: T x N per-period returns of N strategy configurations."""
    T, N = M.shape
    S = int(n_blocks) - (int(n_blocks) % 2)
    if N < 2 or S < 2 or T < S * 2:
        return {"pbo": None, "n_combinations": 0, "n_strategies": N, "n_blocks": S}
    cut = T - (T // S) * S
    Mx = M[cut:]
    blocks = np.array_split(np.arange(Mx.shape[0]), S)

    def sr(X: np.ndarray) -> np.ndarray:
        sd = X.std(axis=0, ddof=1)
        return np.where(sd > 0, X.mean(axis=0) / np.where(sd > 0, sd, 1.0), -np.inf)

    lam, is_best, oos_best, losses = [], [], [], 0
    for combo in combinations(range(S), S // 2):
        is_idx = np.concatenate([blocks[i] for i in combo])
        oos_idx = np.concatenate([blocks[i] for i in range(S) if i not in combo])
        s_is, s_oos = sr(Mx[is_idx]), sr(Mx[oos_idx])
        n_star = int(np.argmax(s_is))
        rank = float((s_oos < s_oos[n_star]).sum() + 1 + 0.5 * ((s_oos == s_oos[n_star]).sum() - 1))
        w = rank / (N + 1.0)
        lam.append(math.log(w / (1.0 - w)))
        is_best.append(float(s_is[n_star]))
        oos_best.append(float(s_oos[n_star]))
        losses += s_oos[n_star] < 0
    lam_a = np.array(lam)
    slope = None
    if len(is_best) > 2 and np.std(is_best) > 0:
        slope = float(np.polyfit(np.array(is_best), np.array(oos_best), 1)[0])
    return {"pbo": float((lam_a <= 0).mean()), "n_combinations": len(lam), "n_strategies": N, "n_blocks": S,
            "logits": [float(x) for x in lam_a[:: max(1, len(lam_a) // 200)]], "prob_oos_loss": losses / len(lam),
            "degradation_slope": slope, "median_logit": float(np.median(lam_a))}


def walk_forward(M: np.ndarray, n_folds: int, metric: str = "sharpe") -> dict[str, Any]:
    """Anchored walk-forward selection over columns of M (T x N daily returns, in-sample period only): the fold
    boundaries split T into n_folds + 1 equal segments; for each test segment f >= 1 the configuration with the best
    in-sample metric on [0, start_f) is chosen and its returns on the test segment are kept."""
    T, _n = M.shape
    edges = np.linspace(0, T, n_folds + 2).astype(int)
    chosen, stitched, folds = [], [], []
    for f in range(1, n_folds + 1):
        tr, te0, te1 = slice(0, edges[f]), edges[f], edges[f + 1]
        X = M[tr]
        sd = X.std(axis=0, ddof=1)
        sc = np.where(sd > 0, X.mean(axis=0) / np.where(sd > 0, sd, 1), -np.inf) if metric == "sharpe" else X.mean(axis=0)
        j = int(np.argmax(sc))
        chosen.append(j)
        seg = M[te0:te1, j]
        stitched.append(seg)
        tsd = seg.std(ddof=1) if len(seg) > 1 else 0.0
        folds.append({"fold": f, "train_end": int(edges[f]), "test_start": int(te0), "test_end": int(te1), "chosen": j,
                      "train_sharpe": float(sc[j] * math.sqrt(TRADING_DAYS)) if math.isfinite(sc[j]) else None,
                      "test_sharpe": float(seg.mean() / tsd * math.sqrt(TRADING_DAYS)) if tsd > 0 else None})
    ret = np.concatenate(stitched) if stitched else np.zeros(0)
    return {"chosen": chosen, "folds": folds, "returns": ret, "start": int(edges[1])}


def permutation_sharpes(panel: pl.DataFrame, px: PricePanel, spec: dict[str, Any], n: int, seed: int,
                        regime_ok: dict[date, float] | None = None) -> list[float]:
    """Sharpe ratios (gross of costs) after shuffling the signal across entities within each rebalance date
    (label permutation: destroys any cross-sectional information, keeps the universe, calendar and construction)."""
    rng = np.random.default_rng(seed)
    cons = spec.get("construction") or {}
    prep = prepare_cs(panel, px, int(cons.get("execution_lag_days", 1)))
    out = []
    for _ in range(n):
        p2 = [(t, e, d, None if cs is None else {**cs, "signal": rng.permutation(cs["signal"])}) for t, e, d, cs in prep]
        res = simulate_rank(panel, px, spec, costs_on=False, record=False, regime_ok=regime_ok, prepared=p2)
        st = sharpe_stats(res.ret, px.rf[res.start_idx:] if px.rf is not None else None)
        out.append(st["sr"] * math.sqrt(TRADING_DAYS) if st["sr"] is not None else 0.0)
    return out


def shift_signal(panel: pl.DataFrame, periods: int = 1) -> pl.DataFrame:
    """Signal lagged by `periods` rebalance dates per entity (stale signal diagnostic)."""
    return panel.sort(["entity_id", "date"]).with_columns(pl.col("signal").shift(periods).over("entity_id"))


# ------------------------------------------------------------------------------------------------ IS / OOS split


def split_metrics(sim: SimResult, px: PricePanel, oos_start: date | None, rf_on: bool = True) -> dict[str, Any]:
    """Metrics for full / in-sample / out-of-sample windows of a simulation."""
    s = sim.start_idx
    bench = px.bench[s:] if px.bench is not None else None
    rf = px.rf[s:] if (px.rf is not None and rf_on) else None
    dates = sim.dates
    inv = np.concatenate([[False], sim.gross_exp[:-1] > 1e-9]) if len(sim.gross_exp) else None
    out = {"full": perf_metrics(sim.ret, dates, bench, rf, invested=inv)}
    if oos_start is not None:
        d = np.array(dates, dtype="datetime64[D]")
        k = int(np.searchsorted(d, np.datetime64(oos_start, "D"), side="left"))
        if 1 < k < len(dates) - 1:
            out["in_sample"] = perf_metrics(sim.ret[:k], dates[:k], bench[:k] if bench is not None else None,
                                            rf[:k] if rf is not None else None, invested=inv[:k] if inv is not None else None)
            out["out_of_sample"] = perf_metrics(sim.ret[k:], dates[k:], bench[k:] if bench is not None else None,
                                                rf[k:] if rf is not None else None, invested=inv[k:] if inv is not None else None)
    return out


def default_oos_start(start: date, end: date, frac: float = 0.3) -> date:
    days = (end - start).days
    return start + timedelta(days=int(days * (1.0 - frac)))
