"""Job `earnings_signals`: earnings-season tracker (earnings_season) and the post-earnings-announcement-drift signal
(earnings_pead) on MODEL-BASED surprises (not consensus).

Season (calendar quarter of the fiscal period end, e.g. 2026Q3 = periods ending Jul-Sep 2026), per region x sector
with ALL roll-ups, for the current and previous 7 seasons as of the run date:
* expected = issuers that reported this season, reported the same quarter a year earlier, or have a calendar row for
  the season; reported = earnings_events of the season with release_date <= as_of;
* beats / median surprises from the primary released expectations (earnings_expectations);
* reaction breadth = share of reporters with CAR[-1,+1] > 0; median |CAR| / realised-vol baseline.

PEAD: one row per release with a primary EPS or revenue surprise. signal_date = the later of (reaction day + 1 trading
day) and (first trading day after the actual's knowledge date) - for US issuers the XBRL actual usually arrives with
the 10-Q, so the signal waits for it (point in time). SUE = EPS surprise / std of the issuer's previous 8 EPS
surprises known at the time (>= 4), else the model surprise z; pead_score = mean of the available inputs capped at
+-3 (SUE, revenue surprise z, CAR[-1,+1] / baseline); decile within region x signal month (>= 20 names).
outcome_car_p2_p20 / _p60: beta-adjusted CAR (beta from earnings_reactions, no alpha: a pre-event alpha would
mechanically penalise past winners over long horizons) over the 20 / 60 trading
days after signal_date - LABELS for the backtesting lab, never inputs.
"""

from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.storage import scan_table
from app.earnings import common as C

log = C.log


def _season_key(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _prev_season(y: int, q: int, k: int = 1) -> tuple[int, int]:
    n = y * 4 + (q - 1) - k
    return n // 4, n % 4 + 1


def _sname(y: int, q: int) -> str:
    return f"{y}Q{q}"


def primary_surprises(s: Settings) -> pl.DataFrame:
    try:
        x = (scan_table("earnings_expectations", s).filter(pl.col("is_primary") & (pl.col("status") == "released"))
             .select("issuer_id", "item_id", "period_end", "surprise", "surprise_pct", "surprise_z", "sigma",
                     "actual_knowledge_date").collect())
    except Exception:  # noqa: BLE001
        return pl.DataFrame()
    if x.is_empty():
        return x
    e = x.filter(pl.col("item_id") == "eps_diluted").drop("item_id").rename(
        {"surprise": "eps_surprise", "surprise_pct": "eps_surprise_pct", "surprise_z": "eps_surprise_z",
         "sigma": "eps_sigma", "actual_knowledge_date": "eps_kd"})
    r = x.filter(pl.col("item_id") == "revenue").drop("item_id", "sigma").rename(
        {"surprise": "rev_surprise", "surprise_pct": "rev_surprise_pct", "surprise_z": "rev_surprise_z",
         "actual_knowledge_date": "rev_kd"})
    return e.join(r, on=["issuer_id", "period_end"], how="full", coalesce=True)


def build_season(ev: pl.DataFrame, cal: pl.DataFrame, sur: pl.DataFrame, rx: pl.DataFrame, sectors: dict[str, str],
                 as_of: date, n_seasons: int = 8) -> pl.DataFrame:
    ev = ev.filter(pl.col("release_date") <= as_of).with_columns(
        (pl.col("period_end").dt.year().cast(pl.Utf8) + "Q" + ((pl.col("period_end").dt.month() - 1) // 3 + 1).cast(pl.Utf8))
        .alias("season"))
    if sur.height:
        ev = ev.join(sur.select("issuer_id", "period_end", "eps_surprise", "eps_surprise_z", "rev_surprise",
                                "rev_surprise_pct"), on=["issuer_id", "period_end"], how="left")
    else:
        ev = ev.with_columns([pl.lit(None, dtype=pl.Float64).alias(c) for c in
                              ("eps_surprise", "eps_surprise_z", "rev_surprise", "rev_surprise_pct")])
    if rx.height:
        ev = ev.join(rx.select("issuer_id", "period_end", "car_m1_p1", "move_ratio"), on=["issuer_id", "period_end"],
                     how="left")
    else:
        ev = ev.with_columns(pl.lit(None, dtype=pl.Float64).alias("car_m1_p1"), pl.lit(None, dtype=pl.Float64).alias("move_ratio"))
    ev = ev.with_columns(pl.col("issuer_id").replace_strict(sectors, default="UNKNOWN").alias("sector_id"))
    cur = _season_key(as_of)
    # the season being reported now is the quarter that ended most recently (period ends ~0-3 months before as_of)
    cur = _prev_season(*cur, 1)
    rows = []
    reg_of = dict(ev.select("issuer_id", "region").unique("issuer_id", keep="last").iter_rows())
    if cal.height:
        reg_of |= {i: r for i, r in cal.select("issuer_id", "region").iter_rows() if i not in reg_of and r}
    for k in range(-1, n_seasons - 1):
        y, q = _prev_season(*cur, k)  # k = -1: the quarter in progress (early reporters / upcoming)
        sname, prev = _sname(y, q), _sname(*_prev_season(y, q, 4))
        rep = ev.filter(pl.col("season") == sname)
        exp_ids = set(rep.get_column("issuer_id").to_list()) | set(ev.filter(pl.col("season") == prev).get_column("issuer_id").to_list())
        up7: set[str] = set()
        if cal.height:
            cs = cal.filter((pl.col("period_end").dt.year() == y) & (((pl.col("period_end").dt.month() - 1) // 3 + 1) == q))
            exp_ids |= set(cs.get_column("issuer_id").to_list())
            up7 = set(cs.filter((pl.col("expected_date") >= as_of) & (pl.col("expected_date") <= as_of + timedelta(days=7)))
                      .get_column("issuer_id").to_list())
        if not exp_ids:
            continue
        exp_df = pl.DataFrame({"issuer_id": sorted(exp_ids)}).with_columns(
            pl.col("issuer_id").replace_strict(reg_of, default="OTHER").alias("region"),
            pl.col("issuer_id").replace_strict(sectors, default="UNKNOWN").alias("sector_id"),
            pl.col("issuer_id").is_in(list(up7)).alias("up7"))
        for gcols in (["region", "sector_id"], ["region"], ["sector_id"], []):
            e = exp_df.group_by(gcols).agg(pl.len().alias("n_expected"), pl.col("up7").sum().alias("n_upcoming_7d")) \
                if gcols else exp_df.select(pl.len().alias("n_expected"), pl.col("up7").sum().alias("n_upcoming_7d"))
            agg = [pl.len().alias("n_reported"),
                   pl.col("eps_surprise").is_not_null().sum().alias("n_eps_surprise"),
                   (pl.col("eps_surprise") > 0).sum().alias("_eb"),
                   pl.col("eps_surprise_z").median().alias("median_eps_surprise_z"),
                   pl.col("rev_surprise").is_not_null().sum().alias("n_rev_surprise"),
                   (pl.col("rev_surprise") > 0).sum().alias("_rb"),
                   pl.col("rev_surprise_pct").median().alias("median_rev_surprise_pct"),
                   pl.col("car_m1_p1").is_not_null().sum().alias("n_reactions"),
                   pl.col("car_m1_p1").median().alias("median_car_m1_p1"),
                   (pl.col("car_m1_p1") > 0).sum().alias("_pos"),
                   pl.col("move_ratio").median().alias("median_abs_move_ratio")]
            r = rep.group_by(gcols).agg(agg) if gcols else rep.select(agg)
            j = e.join(r, on=gcols, how="left") if gcols else e.join(r, how="cross")
            for c in ("region", "sector_id"):
                if c not in gcols:
                    j = j.with_columns(pl.lit("ALL").alias(c))
            rows.append(j.with_columns(pl.lit(sname).alias("season")))
    if not rows:
        return pl.DataFrame()
    out = pl.concat(rows, how="diagonal_relaxed").with_columns(
        [pl.col(c).fill_null(0) for c in ("n_reported", "n_eps_surprise", "_eb", "n_rev_surprise", "_rb", "n_reactions", "_pos",
                                          "n_upcoming_7d")])
    return out.with_columns(
        (pl.col("n_reported") / pl.col("n_expected")).alias("share_reported"),
        pl.when(pl.col("n_eps_surprise") > 0).then(pl.col("_eb") / pl.col("n_eps_surprise")).alias("pct_beat_eps"),
        pl.when(pl.col("n_rev_surprise") > 0).then(pl.col("_rb") / pl.col("n_rev_surprise")).alias("pct_beat_rev"),
        pl.when(pl.col("n_reactions") > 0).then(pl.col("_pos") / pl.col("n_reactions")).alias("reaction_breadth"),
        pl.lit(as_of).alias("as_of_date"),
    ).drop("_eb", "_rb", "_pos")


def _next_trading(di: np.ndarray, d: date, strictly_after: bool) -> int:
    x = (d - date(1970, 1, 1)).days
    return int(np.searchsorted(di, x, side="right" if strictly_after else "left"))


def build_pead(s: Settings, ev: pl.DataFrame, sur: pl.DataFrame, rx: pl.DataFrame, sectors: dict[str, str]) -> pl.DataFrame:
    from app.earnings.reactions import _prices, bench_series, returns_arrays  # noqa: PLC0415
    from app.events_models.common import lake_con  # noqa: PLC0415

    if sur.is_empty() or rx.is_empty():
        return pl.DataFrame()
    d = (ev.select("issuer_id", "security_id", "region", "country", "period_end", "release_date")
         .join(sur, on=["issuer_id", "period_end"], how="inner")
         .join(rx.select("issuer_id", "period_end", "reaction_date", "alpha", "beta", "car_m1_p1", "baseline_move_m1_p1",
                         "benchmark_id"), on=["issuer_id", "period_end"], how="inner")
         .filter(pl.col("security_id").is_not_null()).sort("issuer_id", "period_end"))
    if d.is_empty():
        return d
    # SUE: EPS surprise / std of the issuer's previous 8 EPS surprises known before this release
    rows = d.to_dicts()
    hist: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for r in rows:
        prev = [v for kd, v in hist[r["issuer_id"]] if kd is not None and kd < r["release_date"]][-8:]
        if r.get("eps_surprise") is not None and len(prev) >= 4 and np.std(prev, ddof=1) > 0:
            r["sue"] = float(r["eps_surprise"] / np.std(prev, ddof=1))
        else:
            r["sue"] = r.get("eps_surprise_z")
        if r.get("eps_surprise") is not None:
            hist[r["issuer_id"]].append((r.get("eps_kd"), float(r["eps_surprise"])))
        b = r.get("baseline_move_m1_p1")
        r["ear"] = r.get("car_m1_p1")
        r["ear_z"] = (r["ear"] / b) if r["ear"] is not None and b else None
        xs = [float(np.clip(v, -3, 3)) for v in (r["sue"], r.get("rev_surprise_z"), r["ear_z"]) if v is not None and np.isfinite(v)]
        r["pead_score"] = float(np.mean(xs)) if xs else None
    # signal dates + outcomes from prices
    con = lake_con(["prices_daily"], s)
    by_sec: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_sec[r["security_id"]].append(r)
    start = min(r["release_date"] for r in rows) - timedelta(days=10)
    bids = sorted({r["benchmark_id"] for r in rows if r.get("benchmark_id")} | {"ETF:ACWI"})
    benches = bench_series(con, bids, start)
    secs = sorted(by_sec)
    out = []
    for k in range(0, len(secs), 600):
        px = _prices(con, secs[k:k + 600], start)
        for (sid,), g in px.partition_by("security_id", as_dict=True).items():
            evs = by_sec[sid]
            bid = evs[0].get("benchmark_id")
            a = returns_arrays(g, benches.get(bid) if bid else None)
            di, dates, rr, mm, n = a["di"], a["dates"], a["r"], a["m"], a["n"]
            for r in evs:
                kd = max([x for x in (r.get("eps_kd"), r.get("rev_kd")) if x is not None], default=r["release_date"])
                i1 = _next_trading(di, r["reaction_date"], True)
                i2 = _next_trading(di, kd, True)
                i = max(i1, i2)
                if i >= n:
                    # not yet tradable: signal on the next weekday after both dates (prices pending)
                    r["signal_date"] = C.next_weekday(max(r["reaction_date"], kd))
                else:
                    r["signal_date"] = dates[i]
                for h, col in ((20, "outcome_car_p2_p20"), (60, "outcome_car_p2_p60")):
                    j0, j1 = i + 1, i + h
                    if j1 < n and r.get("alpha") is not None:
                        ar = rr[j0:j1 + 1] - r["beta"] * mm[j0:j1 + 1]  # beta-adjusted, no alpha (long horizon)
                        r[col] = float(np.nansum(ar)) if np.isfinite(ar).sum() >= 0.8 * h else None
                    else:
                        r[col] = None
                out.append(r)
    con.close()
    if not out:
        return pl.DataFrame()
    df = pl.DataFrame(out, infer_schema_length=None).with_columns(
        pl.col("issuer_id").replace_strict(sectors, default=None).alias("sector_id"),
        pl.lit(C.LABEL_SURPRISE).alias("label"))
    df = df.with_columns(pl.col("signal_date").dt.strftime("%Y-%m").alias("_m"))
    cnt = pl.col("pead_score").count().over("region", "_m")
    rank = pl.col("pead_score").rank("average").over("region", "_m")
    df = df.with_columns(pl.when(pl.col("pead_score").is_not_null() & (cnt >= 20))
                         .then(((rank - 1) / cnt * 10).floor().clip(0, 9) + 1).cast(pl.Int8).alias("decile")).drop("_m")
    return df


def run_signals(settings: Settings | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    p = params or {}
    t0 = time.perf_counter()
    as_of = date.fromisoformat(p["as_of"]) if p.get("as_of") else C.now_utc().date()
    ev = scan_table("earnings_events", s).select("issuer_id", "security_id", "region", "country", "period_end",
                                                 "release_date").collect()
    try:
        cal = scan_table("earnings_calendar", s).select("issuer_id", "region", "period_end", "expected_date").collect()
    except Exception:  # noqa: BLE001
        cal = pl.DataFrame()
    try:
        rx = scan_table("earnings_reactions", s).collect()
    except Exception:  # noqa: BLE001
        rx = pl.DataFrame()
    sur = primary_surprises(s)
    sectors = dict(scan_table("issuers", s).select("issuer_id", "sector_id").unique("issuer_id", keep="last")
                   .drop_nulls().collect().iter_rows())
    now = C.now_utc()
    season = build_season(ev, cal, sur, rx, sectors, as_of)
    n1 = C.write(season.with_columns(pl.lit(now).alias("computed_at")), "earnings_season", s, mode="overwrite") \
        if season.height else 0
    pead = build_pead(s, ev, sur, rx, sectors)
    n2 = 0
    if pead.height:
        pead = pead.with_columns(pl.lit(now).alias("computed_at"), pl.lit(p.get("_run_id")).alias("run_id"))
        n2 = C.write(pead.unique(["security_id", "period_end"], keep="last"), "earnings_pead", s, mode="overwrite")
    rep = {"earnings_season": n1, "earnings_pead": n2, "seconds": round(time.perf_counter() - t0, 1)}
    if pead.height:
        x = pead.filter(pl.col("decile").is_not_null() & pl.col("outcome_car_p2_p60").is_not_null())
        if x.height:
            g = x.group_by("decile").agg(pl.col("outcome_car_p2_p60").mean()).sort("decile")
            rep["outcome_p60_by_decile"] = [round(float(v), 4) for v in g.get_column("outcome_car_p2_p60").to_list()]
    C.update_state("earnings_signals", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = dict(ctx.params or {})
    p["_run_id"] = ctx.run_id
    rep = run_signals(ctx.settings, p)
    return JobResult(status="success", rows_written={"earnings_season": rep["earnings_season"],
                                                     "earnings_pead": rep["earnings_pead"]}, message=str(rep))
