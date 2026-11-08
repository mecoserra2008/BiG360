"""Job `earnings_reactions`: price reaction to every earnings release (earnings_events) of the primary security.

Method (the Model Studio event study, app.modeling._algos.car_study, applied per event and vectorised per security):
market model r_i = a + b r_m + e estimated by OLS on trading days [-250, -30] relative to day 0 (>= 60 valid
observations), r_m = the issuer's country index (events_models.common.COUNTRY_INDEX, ACWI fallback) measured over the
security's own trading days (no fill across the security's holidays). Day 0 = first trading day of the security on
or after earnings_events.reaction_date. Windows [-1,+1], [0,+5], [+1,+20]: CAR = sum of abnormal simple returns,
BHAR = prod(1 + r) - prod(1 + a + b r_m). Realised-vol baseline = residual sigma x sqrt(3) for [-1,+1].
Returns use adj_close (splits + dividends); open / high / low are put on the same basis with adj_close / close.
Daily |r| > 50% is treated as a data error (null).
params: since (first release date, default 2012-01-01), batch (securities per price query, default 600).
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.storage import scan_table
from app.earnings import common as C

log = C.log

EST = (-250, -30)
MIN_EST = 60
WINDOWS = {"m1_p1": (-1, 1), "0_p5": (0, 5), "p1_p20": (1, 20)}


def _prices(con, ids: list[str], start: date) -> pl.DataFrame:
    ph = ",".join("?" * len(ids))
    return con.execute(
        f"""SELECT security_id, date, open, high, low, close, coalesce(adj_close, close) AS adj
            FROM prices_daily WHERE security_id IN ({ph}) AND date >= ? AND close > 0
            ORDER BY security_id, date""", [*ids, start]).pl()


def bench_series(con, ids: list[str], start: date) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """benchmark id -> (dates as int days, adj close)."""
    if not ids:
        return {}
    df = _prices(con, ids, start)
    out = {}
    for (sid,), g in df.partition_by("security_id", as_dict=True).items():
        d = g.get_column("date").cast(pl.Int32).to_numpy()
        out[sid] = (d, g.get_column("adj").to_numpy().astype(float))
    return out


def _ols(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    xm, ym = x.mean(), y.mean()
    vx = ((x - xm) ** 2).sum()
    b = float(((x - xm) * (y - ym)).sum() / vx) if vx > 0 else 0.0
    a = float(ym - b * xm)
    e = y - a - b * x
    s = float(np.sqrt((e @ e) / max(len(y) - 2, 1)))
    return a, b, s


def returns_arrays(px: pl.DataFrame, bench: tuple[np.ndarray, np.ndarray] | None) -> dict[str, Any]:
    """One security's prices -> dates, int days, adjusted OHLC, daily simple returns r and benchmark returns m over the
    security's own trading days (benchmark carried at most 7 days)."""
    dates = px.get_column("date").to_list()
    di = px.get_column("date").cast(pl.Int32).to_numpy()
    adj = px.get_column("adj").to_numpy().astype(float)
    close = px.get_column("close").to_numpy().astype(float)
    f = np.where(close > 0, adj / close, np.nan)
    opn = px.get_column("open").to_numpy().astype(float) * f
    hi = px.get_column("high").to_numpy().astype(float) * f
    lo = px.get_column("low").to_numpy().astype(float) * f
    n = len(adj)
    r = np.full(n, np.nan)
    r[1:] = adj[1:] / adj[:-1] - 1
    r[np.abs(r) > 0.5] = np.nan
    if bench is not None:
        bd, bp = bench
        pos = np.searchsorted(bd, di, side="right") - 1
        bpx = np.where(pos >= 0, bp[np.clip(pos, 0, len(bp) - 1)], np.nan)
        # stale benchmark (> 7 days without a print) -> missing
        stale = np.where(pos >= 0, di - bd[np.clip(pos, 0, len(bd) - 1)], 999) > 7
        bpx[stale] = np.nan
        m = np.full(n, np.nan)
        m[1:] = bpx[1:] / bpx[:-1] - 1
        m[np.abs(m) > 0.3] = np.nan
    else:
        m = np.zeros(n)
    return {"dates": dates, "di": di, "adj": adj, "opn": opn, "hi": hi, "lo": lo, "r": r, "m": m, "n": n}


def security_reactions(px: pl.DataFrame, bench: tuple[np.ndarray, np.ndarray] | None,
                       events: list[dict[str, Any]], bench_id: str | None) -> list[dict[str, Any]]:
    """px: one security's [date, open, high, low, close, adj] sorted by date; events: [{issuer_id, period_end,
    release_date, reaction_date, region}]."""
    a_ = returns_arrays(px, bench)
    dates, di, adj, opn, hi, lo, r, m, n = (a_[k] for k in ("dates", "di", "adj", "opn", "hi", "lo", "r", "m", "n"))
    out = []
    for ev in events:
        rd = ev["reaction_date"] or ev["release_date"]
        i0 = int(np.searchsorted(di, (rd - date(1970, 1, 1)).days, side="left"))
        if i0 >= n or i0 + EST[0] < 0 or (int(di[i0]) - (rd - date(1970, 1, 1)).days) > 10:
            continue
        ei = np.arange(i0 + EST[0], i0 + EST[1] + 1)
        ok = np.isfinite(r[ei]) & np.isfinite(m[ei])
        if ok.sum() < MIN_EST:
            continue
        a, b, s = _ols(r[ei][ok], m[ei][ok])
        row: dict[str, Any] = {"issuer_id": ev["issuer_id"], "security_id": ev["security_id"], "region": ev["region"],
                               "period_end": ev["period_end"], "release_date": ev["release_date"],
                               "reaction_date": dates[i0], "benchmark_id": bench_id if bench is not None else None,
                               "alpha": a, "beta": b, "sigma_daily": s, "n_est": int(ok.sum())}
        last_ok = None
        complete = True
        for k, (w0, w1) in WINDOWS.items():
            j0, j1 = i0 + w0, i0 + w1
            if j1 >= n:
                complete = False
                row[f"car_{k}"] = row[f"bhar_{k}"] = None
                continue
            rw, mw = r[j0:j1 + 1], m[j0:j1 + 1]
            if not (np.isfinite(rw).all() and np.isfinite(mw).all()):
                row[f"car_{k}"] = row[f"bhar_{k}"] = None
                continue
            ex = a + b * mw
            row[f"car_{k}"] = float((rw - ex).sum())
            row[f"bhar_{k}"] = float(np.prod(1 + rw) - np.prod(1 + ex))
            last_ok = dates[j1] if last_ok is None else max(last_ok, dates[j1])
            if k == "m1_p1":
                row["ret_m1_p1"] = float(np.prod(1 + rw) - 1)
        row["ar_day0"] = float(r[i0] - (a + b * m[i0])) if np.isfinite(r[i0]) and np.isfinite(m[i0]) else None
        row["baseline_move_m1_p1"] = s * np.sqrt(3.0)
        car1 = row.get("car_m1_p1")
        row["move_ratio"] = abs(car1) / row["baseline_move_m1_p1"] if car1 is not None and s > 0 else None
        if i0 >= 1 and np.isfinite(opn[i0]) and adj[i0 - 1] > 0:
            g = float(opn[i0] / adj[i0 - 1] - 1)
            row["gap_pct"] = g
            if abs(g) >= 0.5 * s and i0 + 5 < n:
                pc = adj[i0 - 1]
                row["gap_filled"] = bool(np.nanmin(lo[i0:i0 + 6]) <= pc) if g > 0 else bool(np.nanmax(hi[i0:i0 + 6]) >= pc)
        c20 = row.get("car_p1_p20")
        row["drift_continuation"] = (bool(np.sign(c20) == np.sign(car1)) if c20 is not None and car1 is not None
                                     and car1 != 0 else None)
        row["complete"] = complete
        if last_ok is None:
            continue
        row["knowledge_date"] = last_ok
        out.append(row)
    return out


MIN_KEEP_SHARE = 0.5


def run_reactions(settings: Settings | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.events_models.common import COUNTRY_INDEX, lake_con  # noqa: PLC0415

    s = settings or get_settings()
    p = params or {}
    t0 = time.perf_counter()
    since = date.fromisoformat(str(p.get("since", "2012-01-01")))
    ev = (scan_table("earnings_events", s).filter(pl.col("security_id").is_not_null() & (pl.col("release_date") >= since))
          .select("issuer_id", "security_id", "region", "country", "period_end", "release_date", "reaction_date")
          .collect())
    if ev.is_empty():
        return {"rows": 0, "events": 0}
    con = lake_con(["prices_daily"], s)
    pstart = since - timedelta(days=420)
    countries = sorted({c for c in ev.get_column("country").drop_nulls().unique().to_list()})
    bids = sorted({i for c in countries for i in COUNTRY_INDEX.get(c, [])} | {"ETF:ACWI"})
    benches = bench_series(con, bids, pstart)
    bmap: dict[str, str] = {}
    for c in countries:
        bmap[c] = next((i for i in COUNTRY_INDEX.get(c, []) if i in benches and len(benches[i][0]) > 500), "ETF:ACWI")
    by_sec: dict[str, list[dict[str, Any]]] = {}
    for r in ev.iter_rows(named=True):
        by_sec.setdefault(r["security_id"], []).append(r)
    secs = sorted(by_sec)
    batch = int(p.get("batch", 600))
    rows: list[dict[str, Any]] = []
    for k in range(0, len(secs), batch):
        ids = secs[k:k + batch]
        px = _prices(con, ids, pstart)
        for (sid,), g in px.partition_by("security_id", as_dict=True).items():
            evs = by_sec[sid]
            bid = bmap.get(evs[0].get("country") or "", "ETF:ACWI")
            rows += security_reactions(g, benches.get(bid), evs, bid)
    con.close()
    now = C.now_utc()
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    n = 0
    if df.height:
        df = df.with_columns(pl.lit(now).alias("computed_at"), pl.lit(p.get("_run_id")).alias("run_id"))
        # shrink guard (found by the O2.3 regression guard: a run that saw a partial earnings_events scan - 169 CA
        # events while the table was being rewritten - replaced 17k reaction rows with 82). The table is fully
        # recomputed each run, so a much smaller result means the input was incomplete: keep the previous table.
        try:
            prev = scan_table("earnings_reactions", s).select(pl.len()).collect().item()
        except Exception:  # noqa: BLE001 - first run
            prev = 0
        if prev and df.height < MIN_KEEP_SHARE * prev and not p.get("allow_shrink"):
            raise RuntimeError(f"earnings_reactions: {df.height} rows from {ev.height} events vs {prev} stored - "
                               "input looks incomplete; previous table kept (param allow_shrink=true overrides)")
        n = C.write(df, "earnings_reactions", s, mode="overwrite")
    rep = {"events": ev.height, "rows": n, "securities": len(secs), "seconds": round(time.perf_counter() - t0, 1)}
    if df.height:
        rep["median_abs_car_m1_p1"] = round(float(df.get_column("car_m1_p1").abs().median() or 0), 4)
        rep["median_move_ratio"] = round(float(df.get_column("move_ratio").median() or 0), 2)
    C.update_state("earnings_reactions", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = dict(ctx.params or {})
    p["_run_id"] = ctx.run_id
    rep = run_reactions(ctx.settings, p)
    return JobResult(status="success", rows_written={"earnings_reactions": rep["rows"]}, entities_ok=rep.get("securities", 0),
                     message=str(rep))
