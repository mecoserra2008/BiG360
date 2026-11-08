"""CFTC positioning analytics -> cftc_signals (+ cftc_event_stats). Job `cftc_signals` (weekly, after cftc_cot).

Per report type x market x category (rows ordered by report date; windows count REPORTS, ~weeks):
  net = long - short (spreading excluded), net_pct_oi, long_share, chg_1w / 4w / 13w, flow_z (chg_1w / 52-report
  std of chg_1w), COT index (net - min) / (max - min) over 52 / 156 reports (min 26 / 78 obs), z-scores, percent rank
  over 156. Pseudo category spec_hedger = speculator net - hedger net (SPEC_HEDGER).
MODEL-BASED (labelled downstream): extreme (COT index 3y >= 0.9 / <= 0.1), crowded (extreme and |z_3y| >= 1.5),
  divergence vs price (z of the 13w / 26w net change minus z of the 13w / 26w log return, 156-report windows; flag when
  opposite signs with |z| >= 1 each) and the event study of forward returns after extremes (event_stats()).
Prices: adj_close of cftc_markets.price_security_id (futures instrument, else a labelled proxy) as of the report
Tuesday for returns; the event study enters at the first close strictly after the release date (PIT).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numba import njit

from app.core.storage import read_table, scan_table, write_table

EXTREME_HI, EXTREME_LO, CROWD_Z, DIV_Z = 0.9, 0.1, 1.5, 1.0
W1, W3 = 52, 156
SPEC_HEDGER: dict[str, tuple[str, tuple[str, ...]]] = {
    "legacy": ("noncomm", ("comm",)),
    "disagg": ("m_money", ("prod_merc", "swap")),
    "tff": ("lev_money", ("dealer",)),
    "cit": ("cit", ("comm",)),
}
# report type -> categories studied in the event study
EVENT_CATS: dict[str, tuple[str, ...]] = {
    "legacy_fut": ("noncomm", "comm"), "disagg_fut": ("m_money", "prod_merc"), "tff_fut": ("lev_money", "asset_mgr", "dealer"),
}
HORIZONS = (4, 13, 26)
COOLDOWN = 13
KEYS = ["report_type", "market_code", "category"]


@njit(cache=True)
def _rolling_rank(x: np.ndarray, gid: np.ndarray, w: int, minp: int) -> np.ndarray:
    """Percent rank of x[i] within the trailing window of w rows of the same group (ties count half)."""
    n = x.shape[0]
    out = np.full(n, np.nan)
    start = 0
    for i in range(n):
        if i == 0 or gid[i] != gid[i - 1]:
            start = i
        lo = max(start, i - w + 1)
        if np.isnan(x[i]):
            continue
        below = 0.0
        cnt = 0
        for j in range(lo, i + 1):
            v = x[j]
            if np.isnan(v):
                continue
            cnt += 1
            if v < x[i]:
                below += 1.0
            elif v == x[i] and j != i:
                below += 0.5
        if cnt >= minp and cnt > 1:
            out[i] = below / (cnt - 1)
    return out


def net_frame(pos: pl.LazyFrame) -> pl.DataFrame:
    """cftc_positions -> per-row net + spec_hedger pseudo rows."""
    base = pos.select("report_type", "market_code", "report_date", "category", "long_pos", "short_pos", "open_interest",
                      "knowledge_date").with_columns(
        (pl.col("long_pos") - pl.col("short_pos")).alias("net"),
        (pl.col("long_pos") / (pl.col("long_pos") + pl.col("short_pos"))).cast(pl.Float32).alias("long_share"),
    ).collect()
    parts = []
    for fam, (spec, hedgers) in SPEC_HEDGER.items():
        f = base.filter(pl.col("report_type").str.starts_with(fam + "_") & pl.col("category").is_in([spec, *hedgers]))
        if f.is_empty():
            continue
        g = (f.group_by("report_type", "market_code", "report_date")
             .agg((pl.col("net").filter(pl.col("category") == spec).sum()
                   - pl.col("net").filter(pl.col("category").is_in(list(hedgers))).sum()).alias("net"),
                  (pl.col("category") == spec).any().alias("has_spec"),
                  pl.col("category").is_in(list(hedgers)).any().alias("has_hedge"),
                  pl.col("open_interest").first(), pl.col("knowledge_date").max())
             .filter(pl.col("has_spec") & pl.col("has_hedge"))
             .drop("has_spec", "has_hedge")
             .with_columns(pl.lit("spec_hedger").alias("category"), pl.lit(None, pl.Float32).alias("long_share"),
                           pl.lit(None, pl.Int64).alias("long_pos"), pl.lit(None, pl.Int64).alias("short_pos")))
        parts.append(g.select(base.columns))
    df = pl.concat([base, *parts], how="vertical_relaxed") if parts else base
    return df.drop("long_pos", "short_pos").sort([*KEYS, "report_date"])


def _z(col: str, w: int, minp: int) -> pl.Expr:
    m = pl.col(col).rolling_mean(w, min_samples=minp).over(KEYS)
    s = pl.col(col).rolling_std(w, min_samples=minp).over(KEYS)
    return pl.when(s > 0).then((pl.col(col) - m) / s).otherwise(None)


def _cot(w: int, minp: int) -> pl.Expr:
    lo = pl.col("net").rolling_min(w, min_samples=minp).over(KEYS)
    hi = pl.col("net").rolling_max(w, min_samples=minp).over(KEYS)
    return pl.when(hi > lo).then((pl.col("net") - lo) / (hi - lo)).when(hi == lo).then(0.5).otherwise(None)


def compute_signals(net: pl.DataFrame, prices: pl.DataFrame | None = None, markets: pl.DataFrame | None = None,
                    run_id: str | None = None, now: datetime | None = None) -> pl.DataFrame:
    """Core signal math (pure; unit-tested). net: output of net_frame (sorted). prices: (security_id, date, adj_close);
    markets: (market_code, price_security_id)."""
    now = now or datetime.now(UTC)
    df = net.sort([*KEYS, "report_date"]).with_columns(pl.col("net").cast(pl.Float64).alias("_n"))
    df = df.with_columns(
        (pl.col("net") / pl.col("open_interest")).cast(pl.Float32).alias("net_pct_oi"),
        (pl.col("net") - pl.col("net").shift(1).over(KEYS)).alias("chg_1w"),
        (pl.col("net") - pl.col("net").shift(4).over(KEYS)).alias("chg_4w"),
        (pl.col("net") - pl.col("net").shift(13).over(KEYS)).alias("chg_13w"),
        (pl.col("net") - pl.col("net").shift(26).over(KEYS)).alias("_chg_26w"),
        _cot(W1, 26).cast(pl.Float32).alias("cot_index_1y"),
        _cot(W3, 78).cast(pl.Float32).alias("cot_index_3y"),
        _z("_n", W1, 26).cast(pl.Float32).alias("z_1y"),
        _z("_n", W3, 78).cast(pl.Float32).alias("z_3y"),
    )
    df = df.with_columns(
        (pl.col("chg_1w") / pl.col("chg_1w").cast(pl.Float64).rolling_std(W1, min_samples=26).over(KEYS))
        .cast(pl.Float32).alias("flow_z"))
    gid = df.select(pl.struct(KEYS).rank("dense")).to_series().to_numpy().astype(np.int64)
    pr = _rolling_rank(df.get_column("_n").fill_null(float("nan")).to_numpy().astype(np.float64), gid, W3, 78)
    df = df.with_columns(pl.Series("pct_rank_3y", pr, dtype=pl.Float64).fill_nan(None).cast(pl.Float32))
    # returns of the mapped price instrument as of the report date
    if prices is not None and markets is not None and prices.height and markets.height:
        mp = markets.select("market_code", "price_security_id").drop_nulls()
        px = prices.select(pl.col("security_id").alias("price_security_id"), pl.col("date").alias("report_date"),
                           pl.col("adj_close").cast(pl.Float64).alias("_px")).drop_nulls().sort("price_security_id", "report_date")
        df = df.join(mp, on="market_code", how="left")
        df = df.sort("price_security_id", "report_date").join_asof(px, on="report_date", by="price_security_id",
                                                                    strategy="backward", tolerance="7d", check_sortedness=False)
        df = df.sort([*KEYS, "report_date"])
    else:
        df = df.with_columns(pl.lit(None, pl.Float64).alias("_px"))
    lp = pl.when(pl.col("_px") > 0).then(pl.col("_px").log()).otherwise(None)
    df = df.with_columns(lp.alias("_lp")).with_columns(
        (pl.col("_lp") - pl.col("_lp").shift(13).over(KEYS)).alias("_r13"),
        (pl.col("_lp") - pl.col("_lp").shift(26).over(KEYS)).alias("_r26"),
        pl.col("chg_13w").cast(pl.Float64).alias("_c13"), pl.col("_chg_26w").cast(pl.Float64).alias("_c26"),
    )
    df = df.with_columns(_z("_c13", W3, 78).alias("_zc13"), _z("_r13", W3, 78).alias("_zr13"),
                         _z("_c26", W3, 78).alias("_zc26"), _z("_r26", W3, 78).alias("_zr26"))
    df = df.with_columns(
        pl.col("_r13").cast(pl.Float32).alias("ret_13w"), pl.col("_r26").cast(pl.Float32).alias("ret_26w"),
        (pl.col("_zc13") - pl.col("_zr13")).cast(pl.Float32).alias("divergence_13w"),
        (pl.col("_zc26") - pl.col("_zr26")).cast(pl.Float32).alias("divergence_26w"),
        pl.when((pl.col("_zc13") >= DIV_Z) & (pl.col("_zr13") <= -DIV_Z)).then(pl.lit("bullish"))
        .when((pl.col("_zc13") <= -DIV_Z) & (pl.col("_zr13") >= DIV_Z)).then(pl.lit("bearish"))
        .otherwise(None).alias("divergence_flag"),
        pl.when(pl.col("cot_index_3y") >= EXTREME_HI).then(pl.lit("long"))
        .when(pl.col("cot_index_3y") <= EXTREME_LO).then(pl.lit("short")).otherwise(None).alias("extreme"),
    ).with_columns(
        pl.when(pl.col("cot_index_3y").is_null()).then(None)
        .when(pl.col("extreme").is_null()).then(pl.lit(False))
        .otherwise((pl.col("z_3y").abs() >= CROWD_Z).fill_null(False)).alias("crowded"),
        pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at"), pl.lit(run_id).cast(pl.Utf8).alias("run_id"),
    )
    cols = ["report_type", "market_code", "report_date", "category", "net", "net_pct_oi", "long_share", "chg_1w", "chg_4w",
            "chg_13w", "flow_z", "cot_index_1y", "cot_index_3y", "z_1y", "z_3y", "pct_rank_3y", "extreme", "crowded",
            "ret_13w", "ret_26w", "divergence_13w", "divergence_26w", "divergence_flag", "knowledge_date", "computed_at",
            "run_id"]
    out = df.select(cols)
    flt = [c for c, t in out.schema.items() if t == pl.Float32]
    return out.with_columns([pl.col(c).fill_nan(None) for c in flt])


# ------------------------------------------------------------------------------------------------ event study


def _events(cot: np.ndarray, side: str) -> list[int]:
    out, last = [], -10_000
    for i in range(1, len(cot)):
        a, b = cot[i - 1], cot[i]
        if math.isnan(a) or math.isnan(b):
            continue
        hit = (b >= EXTREME_HI > a) if side == "long" else (b <= EXTREME_LO < a)
        if hit and i - last >= COOLDOWN:
            out.append(i)
            last = i
    return out


def forward_returns(know: list[date], px_dates: np.ndarray, px_log: np.ndarray, h: int) -> np.ndarray:
    """Log return from the first close strictly after each knowledge date to the first close >= entry + 7h days."""
    kd = np.array([np.datetime64(d, "D") for d in know])
    i0 = np.searchsorted(px_dates, kd, side="right")
    out = np.full(len(kd), np.nan)
    ok = i0 < len(px_dates)
    entry_d = np.where(ok, px_dates[np.minimum(i0, len(px_dates) - 1)], kd)
    i1 = np.searchsorted(px_dates, entry_d + np.timedelta64(7 * h, "D"), side="left")
    ok &= i1 < len(px_dates)
    out[ok] = px_log[i1[ok]] - px_log[i0[ok]]
    return out


def event_stats(sig: pl.DataFrame, prices: pl.DataFrame, markets: pl.DataFrame, now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    mk = markets.filter(pl.col("price_security_id").is_not_null()).select("market_code", "price_security_id", "price_is_proxy")
    pxd = {k[0]: g.sort("date") for k, g in prices.group_by("security_id")}
    rows = []
    for m in mk.iter_rows(named=True):
        p = pxd.get(m["price_security_id"])
        if p is None or p.height < 300:
            continue
        pdates = p.get_column("date").to_numpy().astype("datetime64[D]")
        plog = np.log(p.get_column("adj_close").cast(pl.Float64).to_numpy())
        good = np.isfinite(plog)
        pdates, plog = pdates[good], plog[good]
        for rt, cats in EVENT_CATS.items():
            s = sig.filter((pl.col("market_code") == m["market_code"]) & (pl.col("report_type") == rt)
                           & pl.col("category").is_in(list(cats)))
            if s.is_empty():
                continue
            for (cat,), g in s.group_by("category"):
                g = g.sort("report_date")
                cot = g.get_column("cot_index_3y").cast(pl.Float64).fill_null(float("nan")).to_numpy()
                know = g.get_column("knowledge_date").to_list()
                rdates = g.get_column("report_date").to_list()
                for h in HORIZONS:
                    fr = forward_returns(know, pdates, plog, h)
                    unc = fr[np.isfinite(fr)]
                    if unc.size < 52:
                        continue
                    for side in ("long", "short"):
                        idx = [i for i in _events(cot, side) if np.isfinite(fr[i])]
                        ev = fr[idx]
                        n = len(ev)
                        mu = float(unc.mean())
                        mean = float(ev.mean()) if n else None
                        sd = float(ev.std(ddof=1)) if n > 1 else None
                        rows.append({
                            "report_type": rt, "market_code": m["market_code"], "category": cat,
                            "signal": f"extreme_{side}", "horizon_w": h, "n_events": n, "mean_fwd": mean,
                            "median_fwd": float(np.median(ev)) if n else None,
                            "hit_rate": float((ev > 0).mean()) if n else None, "mean_uncond": mu,
                            "excess_mean": (mean - mu) if mean is not None else None,
                            "t_stat": ((mean - mu) / (sd / math.sqrt(n))) if (sd and n > 1) else None,
                            "first_event": rdates[idx[0]] if n else None, "last_event": rdates[idx[-1]] if n else None,
                            "price_security_id": m["price_security_id"], "price_is_proxy": bool(m["price_is_proxy"]),
                            "computed_at": now,
                        })
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None)


# ------------------------------------------------------------------------------------------------ job


def load_prices(sids: list[str], settings: Any = None, start: date = date(1985, 1, 1)) -> pl.DataFrame:
    if not sids:
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "adj_close": pl.Float64})
    return (scan_table("prices_daily", settings)
            .filter(pl.col("security_id").is_in(sids) & (pl.col("date") >= start))
            .select("security_id", "date", "adj_close").collect())


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    try:
        markets = read_table("cftc_markets", settings=s)
    except Exception:  # noqa: BLE001
        return JobResult(status="skipped", message="cftc_markets empty (run cftc_cot first)")
    net = net_frame(scan_table("cftc_positions", s))
    ctx.check_cancelled()
    sids = markets.get_column("price_security_id").drop_nulls().unique().to_list()
    prices = load_prices(sids, s)
    now = datetime.now(UTC)
    sig = compute_signals(net, prices, markets, ctx.run_id, now)
    ctx.check_cancelled()
    rows = {"cftc_signals": write_table(sig, "cftc_signals", mode="overwrite", settings=s)}
    ev = event_stats(sig, prices, markets, now)
    if ev.height:
        rows["cftc_event_stats"] = write_table(ev, "cftc_event_stats", mode="overwrite", settings=s)
    last = sig.get_column("report_date").max()
    return JobResult(status="success", rows_written=rows, watermark=str(last) if last else None,
                     message=f"{sig.height} signal rows through {last}; {ev.height} event-study cells")


def latest_extremes(sig: pl.DataFrame, markets: pl.DataFrame, since_days: int = 21) -> pl.DataFrame:
    """Latest headline-category extremes of active major markets (for reports)."""
    last = sig.get_column("report_date").max()
    if last is None:
        return pl.DataFrame()
    head = pl.DataFrame({"report_type": ["disagg_fut", "tff_fut", "legacy_fut"], "category": ["m_money", "lev_money", "noncomm"],
                         "primary_report": ["disagg", "tff", "legacy"]})
    mk = markets.filter(pl.col("is_major") & pl.col("is_active")).select("market_code", "short_name", "primary_report")
    f = (sig.filter(pl.col("report_date") >= last - timedelta(days=since_days)).join(mk, on="market_code")
         .join(head, on=["report_type", "category", "primary_report"]))
    return (f.sort("report_date").group_by("market_code").last().filter(pl.col("extreme").is_not_null())
            .select("market_code", "short_name", "report_type", "category", "report_date", "net", "cot_index_3y", "z_3y",
                    "extreme", "crowded").sort("cot_index_3y", descending=True))
