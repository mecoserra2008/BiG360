"""Out-of-sample validation of the level-type ranking (owner: technicals). Read-only research script.

    cd backend
    ..\\.venv\\Scripts\\python.exe -m app.technicals.research_report [--source auto|prices_daily|staging]
        [--start 2012-01-01] [--split 2021-01-01] [--limit N] [--workers 16] [--k 10] [--perms 200] [--json out.json]

Design (no look-ahead anywhere):
* events come from `level_backtest.ticker_events` — identical level code to production, levels known before each bar;
* IN-SAMPLE (IS)  = events with t >= start and outcome fully resolved before `split`;
  OUT-OF-SAMPLE (OOS) = events with t >= split;
* IS events -> `level_backtest.combine` -> per-ticker shrunk reliability scores (exactly what the weekly job stores);
* OOS: bounce (hit) rates of real levels vs the random-level control (same instances shifted ±0.75-2.5 ATR).

Reports
  A. per level key: OOS edge vs random control (+ IS edge, sign agreement, rank correlation IS vs OOS)
  B. per family and per side (support tests vs resistance tests; control matched by side)
  C. selection test: per ticker take the top-K / bottom-K level keys by IS score -> pooled OOS edge, compared with
     (i) ranking by the universe IS edge only and (ii) a shuffle control that permutes IS scores within each ticker
     (p-value = share of permutations whose top-K OOS edge >= the real one); plus OOS edge by IS-score quintile and a
     ticker-clustered t-stat.
Events are serially and cross-sectionally correlated, so plain z-scores overstate significance; read the clustered
t-stats and the permutation p-values first.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from datetime import date
from functools import partial

import numpy as np
import orjson
import polars as pl

from . import taxonomy as T
from .data import iter_bars, list_securities, load_ath_prior, load_earnings_dates, load_meta, load_price_panel, prices_source
from .level_backtest import DEFAULT_BT, combine, sufficient_stats, ticker_events
from .parallel import pmap


def _ticker_split_stats(b, start: date, split: date):
    ev = ticker_events(b, t_min_date=start)
    if ev.is_empty():
        return None
    ev = ev.with_columns(
        pl.when((pl.col("t_date") >= split)).then(pl.lit("OOS"))
        .when(pl.col("res_date") < split).then(pl.lit("IS")).otherwise(None).alias("period"))
    ev = ev.filter(pl.col("period").is_not_null())
    st = sufficient_stats(ev, ["period", "side"])
    return st.with_columns(pl.lit(b.security_id).alias("security_id"), pl.lit(b.pool_key).alias("pool_key"))


def collect(source: str, start: date, split: date, limit: int | None, workers: int | None) -> tuple[pl.DataFrame, dict]:
    t0 = time.perf_counter()
    sids = list_securities(source, since=start, min_bars=500)
    if limit:
        sids = sids[:limit]
    meta = load_meta(sids) if source == "prices_daily" else None
    earnings = load_earnings_dates(meta) if meta is not None else {}
    load_start = date(start.year - 2, start.month, 1)
    parts = []
    n_bars = 0
    n_ok = 0
    t_events = 0.0
    step = 1500
    for i in range(0, len(sids), step):
        chunk = sids[i:i + step]
        panel = load_price_panel(chunk, start=load_start, source=source)
        ath = load_ath_prior(chunk, load_start, source=source)
        bars = list(iter_bars(panel, meta, earnings, ath, min_bars=500))
        n_bars += sum(b.n for b in bars)
        t1 = time.perf_counter()
        res = pmap(partial(_ticker_split_stats, start=start, split=split), bars, workers=workers)
        t_events += time.perf_counter() - t1
        ok = [r for r in res if r is not None and not r.is_empty()]
        n_ok += len(ok)
        parts += ok
        print(f"  events: {min(i + step, len(sids))}/{len(sids)} securities ({time.perf_counter() - t0:.0f}s)", file=sys.stderr)
    timing = {"securities": n_ok, "bars": n_bars, "event_study_s": t_events, "total_s": time.perf_counter() - t0,
              "s_per_1000": 1000.0 * t_events / max(n_ok, 1), "avg_bars": n_bars / max(n_ok, 1)}
    return pl.concat(parts, how="diagonal_relaxed"), timing


def _rate(h, n):
    return h / n if n else float("nan")


def _z(h1, n1, h2, n2):
    if not n1 or not n2:
        return float("nan")
    p = (h1 + h2) / (n1 + n2)
    se = math.sqrt(max(p * (1 - p) * (1 / n1 + 1 / n2), 1e-12))
    return (h1 / n1 - h2 / n2) / se


def by_key(st: pl.DataFrame, group: list[str]) -> pl.DataFrame:
    """Pooled real vs control rates per `group` (+period)."""
    real = st.filter(~pl.col("is_control")).group_by(["period", *group]).agg(pl.col("n").sum(), pl.col("hits").sum(),
                                                                              pl.col("holds").sum(), pl.col("sum_r").sum(), pl.col("n_r").sum())
    ctrl = st.filter(pl.col("is_control")).group_by(["period", *group]).agg(pl.col("n").sum().alias("c_n"), pl.col("hits").sum().alias("c_hits"),
                                                                             pl.col("holds").sum().alias("c_holds"))
    df = real.join(ctrl, on=["period", *group], how="left").fill_null(0)
    return df.with_columns(
        (pl.col("hits") / pl.col("n")).alias("hit"), (pl.col("c_hits") / pl.col("c_n")).alias("ctrl"),
        (pl.col("sum_r") / pl.col("n_r")).alias("exp_r"),
        (pl.col("holds") / pl.col("n")).alias("hold"), (pl.col("c_holds") / pl.col("c_n")).alias("c_hold"),
    ).with_columns((pl.col("hit") - pl.col("ctrl")).alias("edge"), (pl.col("hold") - pl.col("c_hold")).alias("hold_edge"),
                   pl.struct("hits", "n", "c_hits", "c_n").map_elements(
                       lambda s: _z(s["hits"], s["n"], s["c_hits"], s["c_n"]), return_dtype=pl.Float64).alias("z"))


def selection_test(st: pl.DataFrame, k: int, perms: int, seed: int = 7) -> dict:
    """Top-K / bottom-K level keys per ticker by IS reliability score -> OOS edge vs control (+ shuffle control)."""
    is_st = st.filter(pl.col("period") == "IS").group_by(["security_id", "pool_key", "key", "is_control"]).agg(
        [pl.col(c).sum() for c in ("n", "hits", "holds", "breaks", "n_sup", "hits_sup", "sum_mfe", "sum_mae", "sum_mfe_pct",
                                    "n_ft", "sum_ft", "n_r", "sum_r")]).with_columns(pl.lit("IS").alias("period"))
    scores = combine(is_st, DEFAULT_BT, ("period",))
    tick_sc = scores.filter(~pl.col("security_id").str.starts_with("*")).select("security_id", "key", "score", pl.col("n_events").alias("is_n"))
    uni_sc = scores.filter(pl.col("security_id") == "*").select("key", pl.col("edge").alias("uni_edge"))
    oos = st.filter(pl.col("period") == "OOS").group_by(["security_id", "key", "is_control"]).agg(pl.col("n").sum(), pl.col("hits").sum(),
                                                                                                    pl.col("sum_r").sum(), pl.col("n_r").sum())
    o_r = oos.filter(~pl.col("is_control")).drop("is_control")
    o_c = oos.filter(pl.col("is_control")).select("security_id", "key", pl.col("n").alias("c_n"), pl.col("hits").alias("c_hits"))
    cells = (o_r.join(o_c, on=["security_id", "key"], how="left").fill_null(0)
             .join(tick_sc, on=["security_id", "key"], how="inner").join(uni_sc, on="key", how="left")
             .filter(pl.col("n") > 0)).sort(["security_id", "key"])
    if cells.is_empty():
        return {}
    sid = cells["security_id"].to_numpy()
    n = cells["n"].to_numpy().astype(float)
    h = cells["hits"].to_numpy().astype(float)
    cn = cells["c_n"].to_numpy().astype(float)
    ch = cells["c_hits"].to_numpy().astype(float)
    sr = cells["sum_r"].to_numpy().astype(float)
    nr = cells["n_r"].to_numpy().astype(float)
    score = cells["score"].to_numpy().astype(float)
    uedge = cells["uni_edge"].fill_null(0.0).to_numpy().astype(float)
    _, starts = np.unique(sid, return_index=True)
    bounds = list(zip(starts, list(starts[1:]) + [len(sid)], strict=True))

    def pick(sc: np.ndarray, top: bool) -> np.ndarray:
        sel = np.zeros(len(sc), dtype=bool)
        for a, b in bounds:
            if b - a < 2 * k:
                continue
            o = np.argsort(-sc[a:b] if top else sc[a:b], kind="stable")[:k]
            sel[a + o] = True
        return sel

    def agg(sel: np.ndarray) -> dict:
        N, H, CN, CH = n[sel].sum(), h[sel].sum(), cn[sel].sum(), ch[sel].sum()
        # ticker-clustered t-stat of the per-ticker edge
        e = []
        for a, b in bounds:
            s = sel[a:b]
            if s.any() and n[a:b][s].sum() >= 5 and cn[a:b][s].sum() >= 5:
                e.append(h[a:b][s].sum() / n[a:b][s].sum() - ch[a:b][s].sum() / cn[a:b][s].sum())
        e = np.asarray(e)
        t = float(e.mean() / (e.std(ddof=1) / math.sqrt(len(e)))) if len(e) > 2 and e.std(ddof=1) > 0 else float("nan")
        return {"n": int(N), "hit": _rate(H, N), "ctrl": _rate(CH, CN), "edge": _rate(H, N) - _rate(CH, CN),
                "exp_r": _rate(sr[sel].sum(), nr[sel].sum()), "z_events": _z(H, N, CH, CN), "t_tickers": t, "tickers": len(e)}

    out = {"k": k, "cells": int(len(sid)), "tickers": len(bounds)}
    out["all"] = agg(np.ones(len(sid), bool))
    out["top_ticker"] = agg(pick(score, True))
    out["bottom_ticker"] = agg(pick(score, False))
    out["top_universe"] = agg(pick(uedge, True))
    out["bottom_universe"] = agg(pick(uedge, False))
    # quintiles of IS score (pooled cells)
    q = np.quantile(score, [0.2, 0.4, 0.6, 0.8])
    qi = np.searchsorted(q, score, side="right")
    out["quintiles"] = [dict(q=int(i + 1), **agg(qi == i)) for i in range(5)]
    # shuffle control: permute IS scores among keys within each ticker
    rng = np.random.default_rng(seed)
    real_edge = out["top_ticker"]["edge"]
    null = []
    for _ in range(perms):
        sc = score.copy()
        for a, b in bounds:
            sc[a:b] = rng.permutation(sc[a:b])
        sel = pick(sc, True)
        N, H, CN, CH = n[sel].sum(), h[sel].sum(), cn[sel].sum(), ch[sel].sum()
        null.append(_rate(H, N) - _rate(CH, CN))
    null = np.asarray(null)
    out["shuffle"] = {"perms": perms, "null_mean": float(null.mean()) if perms else None,
                      "null_p95": float(np.quantile(null, 0.95)) if perms else None,
                      "p_value": float((1 + (null >= real_edge).sum()) / (1 + perms)) if perms else None}
    # rank agreement IS score vs OOS raw edge (cells with enough OOS events)
    good = (n >= 20) & (cn >= 20)
    if good.sum() > 20:
        from scipy.stats import spearmanr
        oe = h[good] / n[good] - ch[good] / cn[good]
        out["spearman_score_vs_oos_edge"] = float(spearmanr(score[good], oe).statistic)
        out["spearman_cells"] = int(good.sum())
    return out


def _print_table(df: pl.DataFrame, title: str, n: int = 40) -> None:
    print(f"\n{title}")
    with pl.Config(tbl_rows=n, tbl_cols=20, tbl_width_chars=200, float_precision=3):
        print(df.head(n))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", default="auto")
    ap.add_argument("--start", default="2012-01-01")
    ap.add_argument("--split", default="2021-01-01")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--perms", type=int, default=200)
    ap.add_argument("--min-events", type=int, default=300, help="min pooled OOS events to list a key")
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    source = prices_source() if a.source == "auto" else a.source
    start, split = date.fromisoformat(a.start), date.fromisoformat(a.split)
    print(f"source={source} start={start} split={split} (IS: resolved < split, OOS: t >= split)")
    st, timing = collect(source, start, split, a.limit, a.workers)
    print(f"securities={timing['securities']} avg_bars={timing['avg_bars']:.0f} event_study={timing['event_study_s']:.1f}s "
          f"-> {timing['s_per_1000']:.1f}s per 1000 securities (real + control, {a.workers or 'auto'} workers)")
    lk = pl.DataFrame({"key": np.arange(T.N_KEYS, dtype=np.int32), "level_key": list(T.KEY_BY_ID),
                       "family": [k.family for k in T.LEVEL_KEYS]})
    st = st.with_columns(pl.col("key").cast(pl.Int32)).join(lk, on="key", how="left")

    tot = by_key(st, [])
    _print_table(tot.select("period", "n", "hit", "c_n", "ctrl", "edge", "z", "hold", "c_hold", "hold_edge", "exp_r"), "ALL LEVELS vs RANDOM CONTROL")

    keys = by_key(st, ["level_key"])
    wide = keys.filter(pl.col("period") == "OOS").join(
        keys.filter(pl.col("period") == "IS").select("level_key", pl.col("edge").alias("is_edge"), pl.col("n").alias("is_n")),
        on="level_key", how="left")
    wide = wide.filter(pl.col("n") >= a.min_events).sort("edge", descending=True)
    _print_table(wide.select("level_key", "is_n", "is_edge", "n", "hit", "ctrl", "edge", "z", "hold_edge", "exp_r"),
                 f"A. LEVEL KEYS ranked by OOS bounce-rate edge vs random levels (OOS n >= {a.min_events})", 200)
    both = wide.filter(pl.col("is_edge").is_not_null())
    if both.height > 5:
        from scipy.stats import spearmanr
        rho = spearmanr(both["is_edge"].to_numpy(), both["edge"].to_numpy()).statistic
        agree = float(((both["is_edge"] > 0) == (both["edge"] > 0)).mean())
        print(f"\nkey-level IS vs OOS edge: spearman={rho:.3f}  sign agreement={agree:.2%}  keys={both.height}")

    fam = by_key(st, ["family"]).filter(pl.col("period") == "OOS").sort("edge", descending=True)
    _print_table(fam.select("family", "n", "hit", "ctrl", "edge", "z", "hold", "c_hold", "hold_edge", "exp_r"), "B1. FAMILIES (OOS)")
    side = by_key(st.with_columns(pl.when(pl.col("side") == 1).then(pl.lit("support")).otherwise(pl.lit("resistance")).alias("test")),
                  ["family", "test"]).filter(pl.col("period") == "OOS").sort(["test", "edge"], descending=[False, True])
    _print_table(side.select("test", "family", "n", "hit", "ctrl", "edge", "z", "hold_edge"), "B2. FAMILY x SIDE (OOS, control matched by side)", 60)

    sel = selection_test(st, a.k, a.perms)
    print(f"\nC. SELECTION TEST (per-ticker top/bottom {a.k} keys by IS reliability score -> OOS)")
    for name in ("all", "top_ticker", "bottom_ticker", "top_universe", "bottom_universe"):
        r = sel.get(name)
        if r:
            print(f"  {name:16s} n={r['n']:8d} hit={r['hit']:.4f} ctrl={r['ctrl']:.4f} edge={r['edge']:+.4f} "
                  f"expR={r['exp_r']:+.3f} z_events={r['z_events']:+.2f} t_tickers={r['t_tickers']:+.2f} ({r['tickers']} tickers)")
    for r in sel.get("quintiles", []):
        print(f"  IS-score quintile {r['q']}: n={r['n']:8d} hit={r['hit']:.4f} ctrl={r['ctrl']:.4f} edge={r['edge']:+.4f} "
              f"expR={r['exp_r']:+.3f} t_tickers={r['t_tickers']:+.2f}")
    if sel.get("shuffle"):
        s = sel["shuffle"]
        print(f"  shuffle control ({s['perms']} perms): null mean edge={s['null_mean']:+.4f} p95={s['null_p95']:+.4f} "
              f"-> p-value of top-{a.k} edge = {s['p_value']:.3f}")
    if "spearman_score_vs_oos_edge" in sel:
        print(f"  spearman(IS score, OOS edge) over {sel['spearman_cells']} ticker x key cells = {sel['spearman_score_vs_oos_edge']:+.3f}")
    if a.json:
        doc = {"source": source, "start": str(start), "split": str(split), "timing": timing, "selection": sel,
               "keys": wide.to_dicts(), "families": fam.to_dicts(), "all": tot.to_dicts()}
        with open(a.json, "wb") as f:
            f.write(orjson.dumps(doc, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_INDENT_2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
