"""Backtesting lab: run computation (owner: backtest).

    res = compute(spec, settings, check, prior={"n_trials": 3, "sharpes": [...]})

Builds (or loads from data/cache/backtest/) the point-in-time panel for the spec's plan, simulates the strategy net
and gross of costs, and assembles every result block: metrics (full / in-sample / out-of-sample holdout), curves,
rolling statistics, turnover and costs, decile spreads and IC, factor attribution, regime breakdown, robustness
(label permutation, stale-signal check, walk-forward sweep, PBO, deflated Sharpe) and holdings / trades.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import threading
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from . import _engine as E
from . import _panel as P
from .spec import BacktestSpec, make_plan, plan_key, set_path

log = get_logger("app.backtest.runner")

MAX_CACHE_BYTES = 1_500_000_000
MAX_CACHE_FILES = 400
SURVIVORSHIP_CAVEAT = (
    "Survivorship: universe_membership history starts 2026-10-10, so earlier dates use the initial member list "
    "(names that left the universe before then are missing). The 753 US delisted names (Polygon, about 2 years of "
    "history) are included while they traded where the universe allows it; deeper delisted history needs CRSP or "
    "another paid source. Results before 2024-10 are therefore biased upward for broad universes.")
REGIME_SCOPES = {"US": [("cycle_quadrant", "US"), ("hmm_regime", "US"), ("rate_cycle", "fed"), ("inflation_regime", "US")],
                 "GLOBAL": [("cycle_quadrant", "GLOBAL"), ("hmm_regime", "GLOBAL"), ("rate_cycle", "fed"),
                            ("inflation_regime", "US")]}
STYLE_LABELS = {"mkt": "Market (benchmark excess)", "smb": "Size (small minus big, PIT market cap)",
                "hml": "Value (value factor score)", "mom": "Momentum (12-1 price momentum)"}
_cache_lock = threading.Lock()


# ------------------------------------------------------------------------------------------------ directories / cache


def backtest_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.data_dir / "backtest"
    (d / "runs").mkdir(parents=True, exist_ok=True)
    return d


def cache_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.data_dir / "cache" / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot_id(settings: Settings | None = None) -> str:
    from app.modeling.datasets import snapshot_id as sid  # noqa: PLC0415

    return sid(settings)


def prune_cache(settings: Settings | None = None, max_bytes: int = MAX_CACHE_BYTES, max_files: int = MAX_CACHE_FILES) -> int:
    """Delete the least recently used panel cache entries beyond the size / count budget. Returns entries removed."""
    d = cache_dir(settings)
    groups: dict[str, list[Path]] = {}
    for p in d.iterdir():
        if p.is_file() and "." in p.name:
            groups.setdefault(p.name.split(".")[0], []).append(p)
    entries = []
    for k, files in groups.items():
        try:
            entries.append((max(f.stat().st_mtime for f in files), sum(f.stat().st_size for f in files), k, files))
        except OSError:
            continue
    entries.sort(reverse=True)
    total, removed = 0, 0
    for i, (_, size, _, files) in enumerate(entries):
        total += size
        if total > max_bytes or i >= max_files:
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass
            removed += 1
    return removed


def _save_px(path: Path, px: E.PricePanel) -> None:
    np.savez_compressed(path, R=px.R, first=px.first, last=px.last,
                        dates=np.array(px.dates, dtype="datetime64[D]"), ids=np.array(px.ids, dtype=object),
                        currency=np.array(px.currency, dtype=object),
                        bench=px.bench if px.bench is not None else np.zeros(0), rf=px.rf if px.rf is not None else np.zeros(0),
                        bench_id=np.array([px.bench_id or ""], dtype=object))


def _load_px(path: Path) -> E.PricePanel:
    z = np.load(path, allow_pickle=True)
    dates = [d.astype(object) for d in z["dates"]]
    bench = z["bench"] if len(z["bench"]) else None
    rf = z["rf"] if len(z["rf"]) else None
    return E.PricePanel(dates=dates, ids=[str(x) for x in z["ids"]], R=z["R"].astype(np.float64), first=z["first"],
                        last=z["last"], currency=[str(x) for x in z["currency"]], bench=bench, rf=rf,
                        bench_id=str(z["bench_id"][0]) or None)


def lake_connection(plan: dict[str, Any], settings: Settings | None = None) -> Any:
    from app.modeling.datasets import lake_connection as lc  # noqa: PLC0415

    return lc(P.plan_tables(plan), settings)


def load_panel(plan: dict[str, Any], settings: Settings | None = None, check: Callable[[], None] | None = None,
               use_cache: bool = True) -> tuple[pl.DataFrame, E.PricePanel, pl.DataFrame | None, dict[str, Any]]:
    """Cached panel build: data/cache/backtest/<plan key>.{panel.parquet, px.npz, events.parquet, states.parquet, meta.json}."""
    s = settings or get_settings()
    snap = snapshot_id(s)
    key = plan_key(plan, snap)
    d = cache_dir(s)
    f_panel, f_px, f_ev, f_st, f_meta = (d / f"{key}.panel.parquet", d / f"{key}.px.npz", d / f"{key}.events.parquet",
                                         d / f"{key}.states.parquet", d / f"{key}.meta.json")
    if use_cache and f_panel.exists() and f_px.exists() and f_meta.exists():
        try:
            meta = json.loads(f_meta.read_text(encoding="utf-8"))
            panel = pl.read_parquet(f_panel)
            px = _load_px(f_px)
            ev = pl.read_parquet(f_ev) if f_ev.exists() else None
            meta["_states"] = pl.read_parquet(f_st) if f_st.exists() else None
            now = time.time()
            for f in (f_panel, f_px, f_meta):
                os.utime(f, (now, now))
            meta.update(cached=True, plan_key=key, snapshot_id=snap)
            return panel, px, ev, meta
        except Exception:  # noqa: BLE001 - corrupt cache entry: rebuild
            log.warning("backtest cache entry %s unreadable: rebuilding", key)
    con = lake_connection(plan, s)
    try:
        panel, px, ev, meta = P.build_panel(plan, con, check, str(s.data_dir))
    finally:
        con.close()
    states = meta.pop("_states", None)
    with _cache_lock:
        tmp = f".{os.getpid()}.{threading.get_ident()}.tmp"
        panel.write_parquet(d / f"{key}.panel.parquet{tmp}")
        os.replace(d / f"{key}.panel.parquet{tmp}", f_panel)
        _save_px(d / f"{key}.px{tmp}.npz", px)
        os.replace(d / f"{key}.px{tmp}.npz", f_px)
        if ev is not None:
            ev.write_parquet(f_ev)
        if states is not None:
            states.write_parquet(f_st)
        f_meta.write_text(json.dumps(meta, default=str), encoding="utf-8")
        prune_cache(s)
    meta.update(cached=False, plan_key=key, snapshot_id=snap, _states=states)
    return panel, px, ev, meta


# ------------------------------------------------------------------------------------------------ helpers


def _iso(d: Any) -> Any:
    return d.isoformat() if isinstance(d, date) else d


def _clean(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, date):
        return o.isoformat()
    return o


def _nav(ret: np.ndarray) -> np.ndarray:
    return np.cumprod(1.0 + ret)


def _oos_start(spec: BacktestSpec, sim: E.SimResult) -> date:
    """Holdout start: the spec's date, else the last oos_fraction of the *traded* window (from the first book)."""
    if spec.period.oos_start:
        return spec.period.oos_start
    return E.default_oos_start(sim.dates[0], sim.dates[-1], spec.period.oos_fraction)


def _regime_ok(spec: BacktestSpec, panel: pl.DataFrame, meta: dict[str, Any], warns: list[str]) -> dict[date, float] | None:
    rf = spec.construction.regime_filter
    st = meta.get("_states")
    if rf is None:
        return None
    if st is None or st.is_empty():
        warns.append("regime filter ignored: macro regime states unavailable")
        return None
    out, w = P.regime_overlay(st, sorted(panel["date"].unique().to_list()), rf.model_dump(mode="json"))
    warns.extend(w)
    return out or None


def _labels(panel: pl.DataFrame) -> dict[str, dict[str, Any]]:
    cols = [c for c in ("entity_label", "entity_name", "sector_id", "country") if c in panel.columns]
    last = panel.sort("date").group_by("entity_id").agg([pl.col(c).last() for c in cols])
    return {r["entity_id"]: r for r in last.iter_rows(named=True)}


def _trial_sr(ret: np.ndarray, rf: np.ndarray | None) -> float | None:
    st = E.sharpe_stats(ret, rf)
    return st["sr"]


def run_dict(spec: BacktestSpec) -> dict[str, Any]:
    """Normalized spec as consumed by the engine (assets: futures / FX shorts pay no borrow fee)."""
    d = spec.normalized()
    if spec.universe.kind == "assets":
        d["costs"]["borrow_bps"] = 0.0
        d["costs"]["htb_borrow_bps"] = 0.0
    return d


# ------------------------------------------------------------------------------------------------ simulate one spec


def simulate(spec_d: dict[str, Any], panel: pl.DataFrame, px: E.PricePanel, events: pl.DataFrame | None,
             regime_ok: dict[date, float] | None, costs_on: bool = True, record: bool = True) -> E.SimResult:
    if spec_d.get("mode") == "event":
        if events is None:
            raise ValueError("event mode without events")
        attrs = panel.select("entity_id", "date", "adv_usd", "vol") if {"adv_usd", "vol"} <= set(panel.columns) else None
        return E.simulate_events(events, px, spec_d, attrs, costs_on=costs_on, record=record)
    p = panel
    lagk = int((spec_d.get("signal") or {}).get("_extra_lag") or 0)
    if lagk:
        p = E.shift_signal(p, lagk)
    return E.simulate_rank(p, px, spec_d, costs_on=costs_on, record=record, regime_ok=regime_ok)


# ------------------------------------------------------------------------------------------------ compute


def compute(spec: BacktestSpec, settings: Settings | None = None, check: Callable[[], None] | None = None,
            prior: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full backtest run -> result dict (JSON-safe) + private keys `_holdings` / `_trades` (polars frames)."""
    s = settings or get_settings()
    check = check or (lambda: None)
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    plan, warns = make_plan(spec)
    panel, px, events, meta = load_panel(plan, s, check)
    timings["panel_s"] = round(time.perf_counter() - t0, 3)
    warns = list(dict.fromkeys(warns + list(meta.get("warnings") or [])))
    notes = list(meta.get("notes") or [])
    warns.extend(n for n in notes if "LOOK-AHEAD" in n and n not in warns)
    sd = run_dict(spec)
    regime_ok = _regime_ok(spec, panel, meta, warns)
    check()
    # ---- base simulations
    t1 = time.perf_counter()
    sim = simulate(sd, panel, px, events, regime_ok, costs_on=True, record=True)
    gsim = simulate(sd, panel, px, events, regime_ok, costs_on=False, record=False)
    oos = _oos_start(spec, sim)
    span_y = (sim.dates[-1] - sim.dates[0]).days / 365.25
    if span_y < 2:
        warns.append(f"only {span_y:.1f} years of tradable signal history (first book {sim.dates[0]}): statistics, "
                     "the holdout and the overfitting tests are unreliable")
    timings["simulate_s"] = round(time.perf_counter() - t1, 3)
    warns.extend(w for w in sim.warnings if w not in warns)
    if len(sim.ret) < 30:
        raise ValueError(f"only {len(sim.ret)} trading days simulated: widen the period")
    s0 = sim.start_idx
    bench = px.bench[s0:] if px.bench is not None else None
    metrics = E.split_metrics(sim, px, oos)
    gmetrics = E.split_metrics(gsim, px, oos)
    years = E._years(sim.dates)
    to = E.turnover_stats(sim.rebalances, years)
    for blk in metrics.values():
        blk["avg_gross_exposure"] = float(np.mean(sim.gross_exp))
    metrics["full"].update({
        "avg_turnover": to["avg_turnover"], "annual_turnover": to["annual_turnover"], "n_rebalances": to["n_rebalances"],
        "cost_drag_bps": float((np.sum(sim.cost) + np.sum(sim.borrow)) / max(years, 1e-9) * 1e4),
        "trading_cost_bps": float(np.sum(sim.cost) / max(years, 1e-9) * 1e4),
        "borrow_cost_bps": float(np.sum(sim.borrow) / max(years, 1e-9) * 1e4),
        "avg_gross_exposure": float(np.mean(sim.gross_exp)), "avg_net_exposure": float(np.mean(sim.net_exp)),
        "avg_n_long": float(np.mean(sim.n_long)), "avg_n_short": float(np.mean(sim.n_short)),
        "pct_invested": float(np.mean(sim.gross_exp > 1e-6)),
    })
    nav = _nav(sim.ret)
    series = {"dates": [d.isoformat() for d in sim.dates], "nav": nav, "gross_nav": _nav(gsim.ret[-len(sim.ret):])
              if len(gsim.ret) >= len(sim.ret) else _nav(gsim.ret),
              "bench_nav": _nav(bench) if bench is not None else None, "drawdown": E.drawdown(nav),
              "bench_drawdown": E.drawdown(_nav(bench)) if bench is not None else None,
              "gross_exposure": sim.gross_exp, "net_exposure": sim.net_exp, "n_long": sim.n_long, "n_short": sim.n_short}
    mret = E.monthly_returns(sim.ret, sim.dates)
    if bench is not None:
        mret = mret.join(E.monthly_returns(bench, sim.dates).rename({"ret": "bench"}), on="month", how="left")
    monthly = [{"month": r["month"].isoformat(), "ret": r["ret"], "bench": r.get("bench")} for r in mret.iter_rows(named=True)]
    rolling = E.rolling_metrics(sim.ret, sim.dates, bench, 252 if len(sim.ret) > 400 else max(40, len(sim.ret) // 3))
    check()
    # ---- cross-sectional analytics (rank mode)
    t2 = time.perf_counter()
    lag = int(spec.construction.execution_lag_days)
    deciles = E.decile_analysis(panel, px, lag, spec.robustness.deciles) if spec.mode == "rank" else None
    attribution: dict[str, Any] = {}
    if spec.robustness.attribution and spec.mode == "rank":
        attribution = _attribution(spec, panel, px, sim, lag)
    regimes = _regimes(spec, sim, bench, meta)
    timings["analytics_s"] = round(time.perf_counter() - t2, 3)
    check()
    # ---- robustness
    t3 = time.perf_counter()
    robust = _robustness(spec, sd, panel, px, events, regime_ok, sim, gsim, oos, prior or {}, check, s, warns)
    timings["robustness_s"] = round(time.perf_counter() - t3, 3)
    # ---- events / trades / holdings
    labels = _labels(panel)
    ev_block = None
    if spec.mode == "event":
        ev_block = _event_block(spec, events, sim, px)
    hold = pl.DataFrame(sim.holdings) if sim.holdings else None
    trades = pl.DataFrame(sim.trades, infer_schema_length=None) if sim.trades else None
    holdings_dates = sorted({h["date"] for h in sim.holdings}) if sim.holdings else []
    last_hold = []
    if hold is not None and holdings_dates:
        lh = hold.filter(pl.col("date") == holdings_dates[-1]).sort("weight", descending=True)
        for r in lh.iter_rows(named=True):
            lab = labels.get(r["entity_id"], {})
            last_hold.append({**r, "label": lab.get("entity_label"), "name": lab.get("entity_name"),
                              "sector_id": lab.get("sector_id"), "country": lab.get("country")})
    caveats = {"survivorship": SURVIVORSHIP_CAVEAT if spec.universe.kind == "securities" else
               "Assets universe: fixed registry of 73 cross-asset instruments (no survivorship filter needed).",
               "model_based": sorted({n for n in notes if "MODEL" in n.upper() or "model-based" in n})}
    if meta.get("n_delisted"):
        notes.append(f"{meta['n_delisted']} delisted names included while they traded")
    res = {
        "summary": {"name": spec.name, "mode": spec.mode, "universe": _universe_label(spec), "signal": _signal_label(spec),
                    "start": sim.dates[0].isoformat(), "end": sim.dates[-1].isoformat(), "oos_start": oos.isoformat(),
                    "benchmark": px.bench_id, "currency": spec.costs.currency, "rebalance": spec.construction.rebalance,
                    "side": spec.construction.side, "n_rebalances": len(sim.rebalances), "n_days": len(sim.ret),
                    "execution": f"close of trading day t+{lag} after the signal date" if lag else "close of the signal date",
                    "delisting_return": spec.delisting_return},
        "metrics": metrics, "gross_metrics": gmetrics, "series": series, "monthly": monthly, "rolling": rolling,
        "turnover": to, "rebalances": [{k: _iso(v) for k, v in r.items()} for r in sim.rebalances[-400:]],
        "deciles": deciles, "attribution": attribution, "regimes": regimes, "robustness": robust, "events": ev_block,
        "holdings_dates": [d.isoformat() for d in holdings_dates], "holdings_last": last_hold,
        "warnings": warns, "notes": notes, "caveats": caveats,
        "panel": {k: v for k, v in meta.items() if not k.startswith("_")},
        "timings": {**timings, "total_s": round(time.perf_counter() - t0, 3)},
        "flat_metrics": _flat(metrics, robust, deciles),
    }
    out = _clean(res)
    out["_holdings"] = _label_frame(hold, labels)
    out["_trades"] = _label_frame(trades, labels)
    return out


def _label_frame(df: pl.DataFrame | None, labels: dict[str, dict[str, Any]]) -> pl.DataFrame | None:
    if df is None or df.is_empty():
        return df
    lab = pl.DataFrame([{"entity_id": k, "label": v.get("entity_label"), "name": v.get("entity_name"),
                         "sector_id": v.get("sector_id")} for k, v in labels.items()],
                       schema={"entity_id": pl.Utf8, "label": pl.Utf8, "name": pl.Utf8, "sector_id": pl.Utf8})
    return df.join(lab, on="entity_id", how="left")


def _universe_label(spec: BacktestSpec) -> str:
    u = spec.universe
    if u.kind == "assets":
        return "Assets: " + (", ".join(u.asset_ids) if u.asset_ids else (", ".join(u.asset_classes) or "all 73 cross-asset instruments"))
    parts = [u.universe_id or "custom"]
    for lab, v in (("regions", u.regions), ("countries", u.countries), ("sectors", u.sectors)):
        if v:
            parts.append(f"{lab} {', '.join(v)}")
    if u.security_ids:
        parts.append(f"{len(u.security_ids)} securities")
    if u.screen:
        parts.append(f"{len(u.screen)} screen rule(s)")
    return " | ".join(parts)


def _signal_label(spec: BacktestSpec) -> str:
    sig = spec.signal
    names = [f"{v.source}.{v.field}" + ("" if v.higher_is_better else " (low)") for v in sig.variables]
    if spec.mode == "event" and spec.event:
        return f"event: {spec.event.trigger}"
    if sig.combine == "formula":
        return f"formula: {sig.formula}"
    return (sig.combine + ": " if len(names) > 1 else "") + ", ".join(names)


def _flat(metrics: dict[str, Any], robust: dict[str, Any], deciles: dict[str, Any] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for blk, pre in (("full", ""), ("in_sample", "is_"), ("out_of_sample", "oos_")):
        for k in ("cagr", "vol", "sharpe", "sortino", "max_drawdown", "hit_rate", "alpha", "beta", "information_ratio",
                  "total_return", "calmar"):
            v = (metrics.get(blk) or {}).get(k)
            if isinstance(v, (int, float)) and v is not None and math.isfinite(v):
                out[pre + k] = float(v)
    for k in ("annual_turnover", "cost_drag_bps"):
        v = metrics["full"].get(k)
        if isinstance(v, (int, float)) and v is not None and math.isfinite(v):
            out[k] = float(v)
    d = robust.get("dsr") or {}
    for k in ("dsr", "psr"):
        if isinstance(d.get(k), (int, float)) and math.isfinite(d[k]):
            out[k] = float(d[k])
    p = (robust.get("permutation") or {}).get("p_value")
    if isinstance(p, (int, float)):
        out["perm_p_value"] = float(p)
    pb = ((robust.get("sweep") or {}).get("pbo") or {}).get("pbo")
    if isinstance(pb, (int, float)):
        out["pbo"] = float(pb)
    if deciles and isinstance(deciles.get("ic_mean"), (int, float)):
        out["ic_mean"] = float(deciles["ic_mean"])
        if isinstance(deciles.get("spread_ann"), (int, float)):
            out["decile_spread_ann"] = float(deciles["spread_ann"])
    return out


# ------------------------------------------------------------------------------------------------ analytics blocks


def _attribution(spec: BacktestSpec, panel: pl.DataFrame, px: E.PricePanel, sim: E.SimResult, lag: int) -> dict[str, Any]:
    fcols = [c for c in panel.columns if c.startswith("f_")]
    style_cols = [c for c in ("mcap", "f_value", "mom_12_1") if c in panel.columns]
    ends, fac = E.factor_series(panel, px, lag, sorted(set(fcols + style_cols)))
    if not ends:
        return {}
    strat = E.period_returns(sim.ret, sim.dates, ends)
    d = np.array(px.dates, dtype="datetime64[D]")

    def per(arr: np.ndarray | None) -> np.ndarray | None:
        if arr is None:
            return None
        lg = np.concatenate([[0.0], np.cumsum(np.log1p(arr))])
        out, prev = [], int(np.searchsorted(d, np.datetime64(sim.dates[0], "D"), side="left"))
        for b in ends:
            i = int(np.searchsorted(d, np.datetime64(b, "D"), side="right"))
            out.append(math.exp(lg[i] - lg[prev]) - 1.0 if i > prev else 0.0)
            prev = i
        return np.array(out)

    rf_p = per(px.rf)
    b_p = per(px.bench)
    ppy = len(ends) / max(E._years([sim.dates[0], ends[-1]]), 1e-9)
    style: dict[str, np.ndarray] = {}
    if b_p is not None:
        style["mkt"] = b_p - (rf_p if rf_p is not None else 0.0)
    if "mcap" in fac:
        style["smb"] = fac["mcap"]
    if "f_value" in fac:
        style["hml"] = fac["f_value"]
    if "mom_12_1" in fac:
        style["mom"] = fac["mom_12_1"]
    ours = {k: v for k, v in fac.items() if k.startswith("f_")}
    if b_p is not None:
        ours = {"mkt": style["mkt"], **ours}
    labels = {**STYLE_LABELS, **{f"f_{f}": f"{f.replace('_', ' ')} factor (top - bottom tercile of scores)"
                                 for f in [c[2:] for c in fcols]}}
    out = {"periods": len(ends), "periods_per_year": ppy,
           "style": E.attribution(strat, style, rf_p, ppy, labels),
           "factors": E.attribution(strat, ours, rf_p, ppy, labels),
           "exposures": E.exposures(sim.holdings, panel, sorted(set(fcols + style_cols))),
           "note": "time-series regression of strategy excess returns per rebalance period on long-short factor returns "
                   "built from the same point-in-time panel (equal-weight terciles); Newey-West t-stats"}
    return out


def _regimes(spec: BacktestSpec, sim: E.SimResult, bench: np.ndarray | None, meta: dict[str, Any]) -> dict[str, Any]:
    st = meta.get("_states")
    if st is None or st.is_empty():
        return {"rows": [], "note": "macro regime states unavailable"}
    us = spec.universe.kind == "securities" and (spec.universe.universe_id == "mvp_us" or spec.universe.regions == ["US"])
    pairs = REGIME_SCOPES["US" if us else "GLOBAL"]
    keep = pl.DataFrame({"regime_type": [p[0] for p in pairs], "scope": [p[1] for p in pairs]})
    st2 = st.join(keep, on=["regime_type", "scope"], how="inner")
    rows = E.regime_breakdown(sim.ret, sim.dates, bench, st2)
    return {"rows": rows, "note": "monthly strategy returns grouped by the macro regime of the same month (descriptive, "
                                  "W4 macro_factors states; HMM regimes and the provisional rate-cycle rule are model-based)"}


def _event_block(spec: BacktestSpec, events: pl.DataFrame | None, sim: E.SimResult, px: E.PricePanel) -> dict[str, Any]:
    tr = [t for t in sim.trades if t.get("exit_reason") != "open"]
    rets = np.array([t["return"] for t in tr]) if tr else np.zeros(0)
    wins, losses = rets[rets > 0], rets[rets <= 0]
    reasons: dict[str, int] = {}
    for t in sim.trades:
        reasons[t["exit_reason"]] = reasons.get(t["exit_reason"], 0) + 1
    ev = spec.event
    path = E.event_paths(events, px, min(ev.hold_days, 120), ev.entry_lag_days) if events is not None and ev else None
    return {"n_events": events.height if events is not None else 0, "n_trades": len(tr),
            "hit_rate": float((rets > 0).mean()) if len(rets) else None,
            "avg_return": float(rets.mean()) if len(rets) else None,
            "median_return": float(np.median(rets)) if len(rets) else None,
            "avg_win": float(wins.mean()) if len(wins) else None, "avg_loss": float(losses.mean()) if len(losses) else None,
            "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else None,
            "avg_holding_days": float(np.mean([t["holding_days"] for t in tr])) if tr else None,
            "exit_reasons": reasons, "path": path}


# ------------------------------------------------------------------------------------------------ robustness


def _grid(sweep: dict[str, list[Any]]) -> list[dict[str, Any]]:
    keys = list(sweep)
    return [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*[sweep[k] for k in keys])] if keys else []


def _robustness(spec: BacktestSpec, sd: dict[str, Any], panel: pl.DataFrame, px: E.PricePanel, events: pl.DataFrame | None,
                regime_ok: dict[date, float] | None, sim: E.SimResult, gsim: E.SimResult, oos: date,
                prior: dict[str, Any], check: Callable[[], None], settings: Settings, warns: list[str]) -> dict[str, Any]:
    rb = spec.robustness
    rng_seed = int(spec.seed)
    s0 = sim.start_idx
    rf_full = px.rf[s0:] if px.rf is not None else None
    out: dict[str, Any] = {}
    # ---- label permutation (gross): the signal shuffled across names within each date -> Sharpe ~ 0
    gst = E.sharpe_stats(gsim.ret, px.rf[gsim.start_idx:] if px.rf is not None else None)
    g_sr = gst["sr"] * math.sqrt(E.TRADING_DAYS) if gst["sr"] is not None else None
    if rb.permutations and spec.mode == "rank":
        perms = E.permutation_sharpes(panel, px, sd, rb.permutations, rng_seed, regime_ok)
        pa = np.array(perms)
        out["permutation"] = {"n": len(perms), "sharpes": perms, "mean": float(pa.mean()), "std": float(pa.std(ddof=1)) if len(pa) > 1 else None,
                              "actual_gross_sharpe": g_sr,
                              "p_value": float((1 + (pa >= (g_sr if g_sr is not None else 0)).sum()) / (1 + len(pa))),
                              "note": "signal shuffled across names within each rebalance date (gross of costs); the "
                                      "mean should be ~0 and the actual Sharpe should sit in the right tail"}
    elif rb.permutations and spec.mode == "event" and events is not None and events.height:
        rng = np.random.default_rng(rng_seed)
        ents = sorted(panel["entity_id"].unique().to_list())
        perms = []
        for _ in range(rb.permutations):
            e2 = events.with_columns(pl.Series("entity_id", rng.choice(ents, events.height).tolist()))
            r2 = E.simulate_events(e2, px, sd, None, costs_on=False, record=False)
            st = E.sharpe_stats(r2.ret, px.rf[r2.start_idx:] if px.rf is not None else None)
            perms.append(st["sr"] * math.sqrt(E.TRADING_DAYS) if st["sr"] is not None else 0.0)
            check()
        pa = np.array(perms)
        out["permutation"] = {"n": len(perms), "sharpes": perms, "mean": float(pa.mean()), "std": float(pa.std(ddof=1)) if len(pa) > 1 else None,
                              "actual_gross_sharpe": g_sr,
                              "p_value": float((1 + (pa >= (g_sr if g_sr is not None else 0)).sum()) / (1 + len(pa))),
                              "note": "placebo events: same dates, random names from the universe (gross of costs)"}
    check()
    # ---- stale signal: lag the signal one more rebalance (event mode: 5 more days); should not improve results
    if rb.stale_check:
        if spec.mode == "rank":
            st_sim = E.simulate_rank(E.shift_signal(panel, 1), px, sd, costs_on=False, record=False, regime_ok=regime_ok)
        else:
            sd2 = set_path(sd, "event.entry_lag_days", int(spec.event.entry_lag_days) + 5) if spec.event else sd
            st_sim = E.simulate_events(events, px, sd2, None, costs_on=False, record=False) if events is not None else None
        if st_sim is not None:
            ss = E.sharpe_stats(st_sim.ret, px.rf[st_sim.start_idx:] if px.rf is not None else None)
            s_sr = ss["sr"] * math.sqrt(E.TRADING_DAYS) if ss["sr"] is not None else None
            se = math.sqrt(E.TRADING_DAYS / max(len(gsim.ret), 1))  # ~ s.e. of an annualised Sharpe
            out["stale"] = {"sharpe": s_sr, "actual_gross_sharpe": g_sr,
                            "delta": (s_sr - g_sr) if (s_sr is not None and g_sr is not None) else None,
                            "suspicious": bool(s_sr is not None and g_sr is not None and s_sr > g_sr + 2 * se),
                            "lag": "1 rebalance" if spec.mode == "rank" else "+5 trading days",
                            "note": "a staler signal should not do better; a large improvement hints at a timing problem"}
    check()
    # ---- parameter sweep: walk-forward selection on the in-sample window, PBO (CSCV), holdout of the IS choice
    grid = _grid(rb.sweep)
    sr_list: list[float] = []
    if grid:
        out["sweep"] = _sweep(spec, sd, grid, panel, px, events, regime_ok, oos, rb, check, settings, warns)
        sr_list = [g["is_sr"] for g in out["sweep"]["grid"] if g.get("is_sr") is not None]
    # ---- deflated Sharpe with the strategy-level multiple-testing counter
    n_prior = int(prior.get("n_trials") or 0)
    n_trials = max(1, len(grid)) + n_prior
    prior_sr = [x for x in (prior.get("sharpes") or []) if isinstance(x, (int, float))]
    all_sr = sr_list + prior_sr
    d = np.array(sim.dates, dtype="datetime64[D]")
    k = int(np.searchsorted(d, np.datetime64(oos, "D"), side="left"))
    is_ret = sim.ret[:k] if k > 30 else sim.ret
    is_rf = (rf_full[:k] if k > 30 else rf_full) if rf_full is not None else None
    stt = E.sharpe_stats(is_ret, is_rf)
    if stt["sr"] is not None:
        if len(all_sr) >= 2:
            var = float(np.var(np.array(all_sr), ddof=1))
            var_src = "sweep / previous trials of this strategy"
        else:
            var = 1.0 / max(len(is_ret), 1)
            var_src = "null approximation 1/T (fewer than 2 recorded trials)"
        dsr = E.deflated_sharpe(stt["sr"], stt["n"], stt["skew"], stt["kurtosis"], n_trials, var)
        dsr.update({"sharpe_ann": stt["sr"] * math.sqrt(E.TRADING_DAYS), "sr0_ann": dsr["sr0"] * math.sqrt(E.TRADING_DAYS),
                    "window": "in-sample", "n_obs": stt["n"], "skew": stt["skew"], "kurtosis": stt["kurtosis"],
                    "sr_var_source": var_src, "n_trials_this_run": max(1, len(grid)), "n_trials_prior": n_prior})
        out["dsr"] = dsr
    out["n_trials"] = n_trials
    out["trial_sharpe"] = stt["sr"]
    return out


def _sweep(spec: BacktestSpec, sd: dict[str, Any], grid: list[dict[str, Any]], panel: pl.DataFrame, px: E.PricePanel,
           events: pl.DataFrame | None, regime_ok: dict[date, float] | None, oos: date, rb: Any,
           check: Callable[[], None], settings: Settings, warns: list[str]) -> dict[str, Any]:
    rets: list[np.ndarray] = []
    dates_l: list[list[date]] = []
    rows: list[dict[str, Any]] = []
    panels: dict[str, tuple[pl.DataFrame, E.PricePanel, pl.DataFrame | None, dict[date, float] | None]] = {}
    base_plan, _ = make_plan(spec)
    panels[json.dumps(base_plan, sort_keys=True, default=str)] = (panel, px, events, regime_ok)
    for combo in grid:
        check()
        d = sd
        for pth, v in combo.items():
            if pth == "signal.lag_periods":
                d = set_path(d, "signal._extra_lag", int(v) - int(spec.signal.lag_periods))
                continue
            d = set_path(d, pth, v)
        try:
            dd = {k: v for k, v in d.items()}
            sig_extra = (dd.get("signal") or {}).pop("_extra_lag", None)
            sp = BacktestSpec.model_validate(dd)
            plan, _ = make_plan(sp)
            pk = json.dumps(plan, sort_keys=True, default=str)
            if pk not in panels:
                pn, ppx, pev, pm = load_panel(plan, settings, check)
                panels[pk] = (pn, ppx, pev, _regime_ok(sp, pn, pm, []))
            pn, ppx, pev, rok = panels[pk]
            run_d = run_dict(sp)
            if sig_extra:
                run_d["signal"]["_extra_lag"] = sig_extra
            r = simulate(run_d, pn, ppx, pev, rok, costs_on=True, record=False)
        except Exception as e:  # noqa: BLE001 - one bad combination must not stop the sweep
            rows.append({"params": combo, "error": f"{type(e).__name__}: {e}"})
            continue
        rets.append(r.ret)
        dates_l.append(r.dates)
        rows.append({"params": combo, "_i": len(rets) - 1})
    if len(rets) < 2:
        return {"grid": rows, "n": len(rows), "note": "fewer than 2 valid configurations"}
    common = sorted(set.intersection(*[set(x) for x in dates_l]))
    M = np.column_stack([np.array([dict(zip(dl, rr, strict=True))[c] for c in common]) for dl, rr in zip(dates_l, rets, strict=True)])
    dc = np.array(common, dtype="datetime64[D]")
    k = int(np.searchsorted(dc, np.datetime64(oos, "D"), side="left"))
    Mi, Mo = M[:k], M[k:]
    rf_c = None
    if px.rf is not None:
        rmap = dict(zip(px.dates, px.rf.tolist(), strict=True))
        rf_c = np.array([rmap.get(c, 0.0) for c in common])

    def sr(x: np.ndarray, r: np.ndarray | None) -> float | None:
        st = E.sharpe_stats(x, r)
        return st["sr"]

    for row in rows:
        if "_i" not in row:
            continue
        j = row.pop("_i")
        a = sr(Mi[:, j], rf_c[:k] if rf_c is not None else None) if k > 30 else None
        b = sr(Mo[:, j], rf_c[k:] if rf_c is not None else None) if len(Mo) > 30 else None
        row.update({"col": j, "is_sr": a, "is_sharpe": a * math.sqrt(E.TRADING_DAYS) if a is not None else None,
                    "oos_sharpe": b * math.sqrt(E.TRADING_DAYS) if b is not None else None,
                    "is_cagr": float(np.prod(1 + Mi[:, j]) ** (E.TRADING_DAYS / max(len(Mi), 1)) - 1) if k > 30 else None})
    valid = [r for r in rows if r.get("is_sr") is not None]
    best = max(valid, key=lambda r: r["is_sr"]) if valid else None
    wf = E.walk_forward(Mi, rb.wf_folds) if k > rb.wf_folds * 40 else None
    wf_block = None
    if wf is not None and len(wf["returns"]):
        wd = common[wf["start"]:k]
        wm = E.perf_metrics(wf["returns"], wd, None, rf_c[wf["start"]:k] if rf_c is not None else None)
        wf_block = {"folds": [{**f, "params": next((r["params"] for r in rows if r.get("col") == f["chosen"]), None),
                               "test_start_date": common[f["test_start"]].isoformat(),
                               "test_end_date": common[min(f["test_end"], k) - 1].isoformat()} for f in wf["folds"]],
                    "metrics": wm, "dates": [x.isoformat() for x in wd[::5]],
                    "nav": np.cumprod(1 + wf["returns"])[::5].tolist()}
    pbo = E.pbo_cscv(Mi, rb.cscv_blocks) if k > rb.cscv_blocks * 20 else {"pbo": None, "note": "in-sample window too short"}
    hold = None
    if best is not None and len(Mo) > 30:
        hold = {"params": best["params"], "is_sharpe": best["is_sharpe"],
                "metrics": E.perf_metrics(Mo[:, best["col"]], common[k:], None, rf_c[k:] if rf_c is not None else None)}
    return {"n": len(rows), "grid": rows, "best_in_sample": best["params"] if best else None, "walk_forward": wf_block,
            "pbo": pbo, "holdout": hold,
            "note": "every configuration is simulated net of costs; selection uses the in-sample window only, the "
                    "holdout is the out-of-sample performance of the in-sample choice"}
