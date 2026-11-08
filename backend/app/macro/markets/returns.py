"""Return panels for W4 (weekly for exposures, monthly for regime performance) from the price lake.

* Securities: local-currency total returns from adj_close (last close of the ISO week / month). A return needs both
  consecutive period closes; |weekly| moves beyond [-90%, +200%] (monthly [-95%, +400%]) are treated as bad ticks.
* Portfolios (sector, subsector, region-sector 'US:TECH', region): every primary common share (incl. delisted US names,
  current classification). ew = mean of member returns winsorised to [-50%, +100%] weekly ([-70%, +200%] monthly);
  cw = weights from the member's market_cap_usd (features.valuation_daily, 2010+) at the previous period end. A
  period needs >= MIN_MEMBERS returns.
* Instruments: ETFs / equity indices / continuous front-month futures (benchmark rows of securities_master; futures
  roll effects are not adjusted). Yield / volatility indices (^TNX, ^FVX, ^IRX, ^TYX, ^VIX) are excluded.
* Factor long-short: Model Studio dataset source `factor_ls` (app.modeling: top minus bottom quintile of the previous
  period's factor score, equal weight, scored universe).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table

log = get_logger(__name__)

PORTFOLIO_TYPES = ("common", "reit_unit")
INSTRUMENT_TYPES = ("etf", "index", "future")
EXCLUDED_INSTRUMENTS = {"IDX:^TNX", "IDX:^FVX", "IDX:^IRX", "IDX:^TYX", "IDX:^VIX", "ETF:VIXY",
                        # price-history volatility / skew indices (levels, not investable returns)
                        "IDX:^VXN", "IDX:^VVIX", "IDX:^VIX9D", "IDX:^VIX3M", "IDX:^VIX6M", "IDX:^VIX1Y", "IDX:^VXD",
                        "IDX:^OVX", "IDX:^GVZ", "IDX:^VXEEM", "IDX:^SKEW", "IDX:^MOVE", "IDX:^INDIAVIX", "IDX:^AXVI"}
MIN_MEMBERS = 5
BAD = {"W": (-0.9, 2.0), "M": (-0.95, 4.0)}
WINSOR = {"W": (-0.5, 1.0), "M": (-0.7, 2.0)}
FACTORS_LS = ("quality", "profitability", "growth", "value", "balance_sheet", "momentum", "moat")
REGION_BENCH = {"US": "IDX:^GSPC", "CA": "IDX:^GSPTSE", "EU": "IDX:^STOXX", "UK": "IDX:^FTSE", "JP": "IDX:^N225",
                "KR": "IDX:^KS11", "HK": "IDX:^HSI", "CN": "IDX:000300.SS", "LATAM": "IDX:^BVSP", "APAC": "IDX:^AXJO",
                "OTHER": "IDX:^GSPC"}
GLOBAL_BENCH = "IDX:^GSPC"


@dataclass
class Panel:
    """Return matrix R (T x N) with NaN for missing, period keys `dates` (T) and entity keys (N)."""

    freq: str
    dates: list[date]
    keys: list[tuple[str, str, str]]          # (entity_type, entity_id, weighting)
    R: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)

    def subset(self, idx: np.ndarray | list[int]) -> Panel:
        idx = np.asarray(idx, dtype=int)
        return Panel(self.freq, self.dates, [self.keys[i] for i in idx], self.R[:, idx], self.meta)

    @staticmethod
    def concat(panels: list[Panel]) -> Panel:
        ps = [p for p in panels if p.keys]
        if not ps:
            return Panel(panels[0].freq if panels else "W", [], [], np.empty((0, 0)))
        dates = sorted(set().union(*[set(p.dates) for p in ps]))
        pos = {d: i for i, d in enumerate(dates)}
        R = np.full((len(dates), sum(len(p.keys) for p in ps)), np.nan)
        keys: list[tuple[str, str, str]] = []
        c = 0
        for p in ps:
            rows = np.array([pos[d] for d in p.dates], dtype=int)
            R[rows, c:c + len(p.keys)] = p.R
            keys += p.keys
            c += len(p.keys)
        return Panel(ps[0].freq, dates, keys, R)


def period_start(d: date, freq: str) -> date:
    return d - timedelta(days=d.weekday()) if freq == "W" else date(d.year, d.month, 1)


def period_end(p: date, freq: str) -> date:
    """Last calendar day of the period keyed by `p` (Friday for weeks: returns are Friday-to-Friday closes)."""
    if freq == "W":
        return p + timedelta(days=4)
    nm = date(p.year + (p.month == 12), p.month % 12 + 1, 1)
    return nm - timedelta(days=1)


def security_meta(settings: Settings) -> pl.DataFrame:
    sm = read_table("securities_master", columns=["security_id", "issuer_id", "security_type", "is_primary_security",
                                                  "display_ticker", "name"], settings=settings)
    iss = read_table("issuers", columns=["issuer_id", "sector_id", "subsector_id", "region", "country"],
                     settings=settings)
    um = read_table("universe_membership", settings=settings).filter(pl.col("valid_to").is_null())
    full = um.filter(pl.col("universe_id") == "full").select("security_id").unique().with_columns(
        pl.lit(True).alias("in_full"))
    return (sm.join(iss, on="issuer_id", how="left").join(full, on="security_id", how="left")
            .with_columns(pl.col("in_full").fill_null(False)))


def close_matrix(settings: Settings, ids: list[str], freq: str, start: date, end: date
                 ) -> tuple[list[date], list[str], np.ndarray]:
    """Period-end adj_close matrix (T periods x N securities)."""
    lf = (scan_table("prices_daily", settings).filter(pl.col("security_id").is_in(ids)
                                                      & (pl.col("date") >= start) & (pl.col("date") <= end))
          .select("security_id", "date", "adj_close").filter(pl.col("adj_close") > 0))
    key = pl.col("date").dt.truncate("1w" if freq == "W" else "1mo")
    # ram_fix 2026-10-31: last close per (security, period) as max(date) + join instead of sort_by("date").last():
    # the sorted aggregate is not streamable and materialised ~24k securities x daily bars since 1995 (the job hit
    # 9 GB). prices_daily's PK (security_id, date) is unique, so both give the same row.
    lfp = lf.with_columns(key.alias("p"))
    last = lfp.group_by("security_id", "p").agg(pl.col("date").max())
    df = (lfp.join(last, on=["security_id", "p", "date"], how="inner").select("security_id", "p", "adj_close")
          .collect(engine="streaming"))
    dates = sorted(df["p"].unique().to_list())
    sids = sorted(df["security_id"].unique().to_list())
    dpos = {d: i for i, d in enumerate(dates)}
    spos = {s: i for i, s in enumerate(sids)}
    C = np.full((len(dates), len(sids)), np.nan)
    r = np.fromiter((dpos[d] for d in df["p"].to_list()), dtype=np.int64, count=df.height)
    c = np.fromiter((spos[s] for s in df["security_id"].to_list()), dtype=np.int64, count=df.height)
    C[r, c] = df["adj_close"].to_numpy()
    return dates, sids, C


def returns_from_close(dates: list[date], C: np.ndarray, freq: str) -> tuple[list[date], np.ndarray]:
    """Simple returns between consecutive periods (a calendar gap of more than one period -> NaN)."""
    if len(dates) < 2:
        return [], np.empty((0, C.shape[1]))
    step = 7 if freq == "W" else 31
    gaps = np.array([(dates[i] - dates[i - 1]).days <= step for i in range(1, len(dates))])
    with np.errstate(divide="ignore", invalid="ignore"):
        R = C[1:] / C[:-1] - 1.0
    R[~gaps] = np.nan
    lo, hi = BAD[freq]
    R[(R < lo) | (R > hi)] = np.nan
    return dates[1:], R


def mcap_matrix(settings: Settings, dates: list[date], sids: list[str], freq: str) -> np.ndarray:
    """Market cap (USD) at the end of the PREVIOUS period, aligned to `dates` x `sids` (NaN when unknown)."""
    if not dates:
        return np.full((0, len(sids)), np.nan)
    key = pl.col("date").dt.truncate("1w" if freq == "W" else "1mo")
    try:
        lfv = (scan_table("valuation_daily", settings).filter(pl.col("security_id").is_in(sids)
                                                              & (pl.col("date") >= dates[0] - timedelta(days=40)))
               .select("security_id", "date", "market_cap_usd").drop_nulls().with_columns(key.alias("p")))
        last = lfv.group_by("security_id", "p").agg(pl.col("date").max())   # streamable (see close_matrix)
        df = (lfv.join(last, on=["security_id", "p", "date"], how="inner")
              .select("security_id", "p", "market_cap_usd").collect(engine="streaming"))
    except Exception as e:  # noqa: BLE001
        log.info("valuation_daily unavailable (cap weights off): %s", e)
        return np.full((len(dates), len(sids)), np.nan)
    dpos = {d: i for i, d in enumerate(dates)}
    spos = {s: i for i, s in enumerate(sids)}
    M = np.full((len(dates), len(sids)), np.nan)
    df = df.filter(pl.col("p").is_in(dates))
    r = np.fromiter((dpos[d] for d in df["p"].to_list()), dtype=np.int64, count=df.height)
    c = np.fromiter((spos[s] for s in df["security_id"].to_list()), dtype=np.int64, count=df.height)
    M[r, c] = df["market_cap_usd"].to_numpy()
    out = np.full_like(M, np.nan)
    out[1:] = M[:-1]          # weights known at the start of the period
    out[(out <= 0) | ~np.isfinite(out)] = np.nan
    return out


def group_returns(R: np.ndarray, W: np.ndarray | None, cols: np.ndarray, freq: str) -> tuple[np.ndarray, np.ndarray]:
    """(ew, cw) portfolio returns (T,) for member columns `cols`."""
    sub = R[:, cols]
    lo, hi = WINSOR[freq]
    sub = np.clip(sub, lo, hi)
    ok = np.isfinite(sub)
    n = ok.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ew = np.where(n >= MIN_MEMBERS, np.nansum(sub, axis=1) / np.maximum(n, 1), np.nan)
        if W is None:
            return ew, np.full_like(ew, np.nan)
        w = np.where(ok, W[:, cols], np.nan)
        wok = np.isfinite(w)
        nw = wok.sum(axis=1)
        ws = np.nansum(w, axis=1)
        cw = np.where((nw >= MIN_MEMBERS) & (ws > 0), np.nansum(np.where(wok, w * sub, 0.0), axis=1) / ws, np.nan)
    return ew, cw


def portfolio_panel(R: np.ndarray, W: np.ndarray | None, dates: list[date], meta: pl.DataFrame, sids: list[str],
                    freq: str, min_periods: int = 12) -> Panel:
    """Sector / subsector / region-sector / region portfolios (ew + cw)."""
    spos = {s: i for i, s in enumerate(sids)}
    m = meta.filter(pl.col("security_type").is_in(list(PORTFOLIO_TYPES)) & pl.col("is_primary_security")
                    & pl.col("security_id").is_in(sids))
    groups: dict[tuple[str, str], list[int]] = {}
    for r in m.select("security_id", "sector_id", "subsector_id", "region").iter_rows(named=True):
        i = spos[r["security_id"]]
        if r["sector_id"]:
            groups.setdefault(("sector", r["sector_id"]), []).append(i)
            if r["region"]:
                groups.setdefault(("sector", f"{r['region']}:{r['sector_id']}"), []).append(i)
        if r["subsector_id"]:
            groups.setdefault(("subsector", r["subsector_id"]), []).append(i)
        if r["region"]:
            groups.setdefault(("region", r["region"]), []).append(i)
    keys: list[tuple[str, str, str]] = []
    cols: list[np.ndarray] = []
    for (et, eid), idx in sorted(groups.items()):
        if len(idx) < MIN_MEMBERS:
            continue
        ew, cw = group_returns(R, W, np.array(idx), freq)
        for wt, v in (("ew", ew), ("cw", cw)):
            if np.isfinite(v).sum() >= min_periods:
                keys.append((et, eid, wt))
                cols.append(v)
    M = np.column_stack(cols) if cols else np.empty((len(dates), 0))
    return Panel(freq, dates, keys, M)


def factor_ls_panel(settings: Settings, freq: str, start: date) -> Panel:
    """Factor long-short returns through Model Studio's `factor_ls` dataset source."""
    from app.modeling.datasets import DatasetSpec, build_dataset  # noqa: PLC0415

    spec = DatasetSpec(kind="timeseries", frequency=freq, start=max(start, date(2016, 11, 1)),
                       variables=[{"name": f"ls_{f}", "source": "factor_ls", "field": f} for f in FACTORS_LS],
                       missing={"ffill_limit": 5 if freq == "W" else 1})
    try:
        fr = build_dataset(spec, settings=settings).frame
    except Exception as e:  # noqa: BLE001 - factor scores not built yet
        log.warning("factor_ls panel unavailable: %s", e)
        return Panel(freq, [], [], np.empty((0, 0)))
    fr = fr.with_columns(pl.col("date").map_elements(lambda d: period_start(d, freq), return_dtype=pl.Date))
    fr = fr.group_by("date").agg(pl.all().last()).sort("date")
    keys = [("factor", f, "none") for f in FACTORS_LS if f"ls_{f}" in fr.columns]
    R = fr.select([f"ls_{k[1]}" for k in keys]).to_numpy().astype(float) if keys else np.empty((fr.height, 0))
    return Panel(freq, fr["date"].to_list(), keys, R)


def build_panels(settings: Settings, freq: str, start: date, end: date, securities: bool = True,
                 aggregates: bool = True, min_periods: int = 12) -> dict[str, Panel]:
    """{'security': Panel (universe full), 'aggregate': Panel (portfolios + instruments + factor L-S)}."""
    meta = security_meta(settings)
    want_sec = meta.filter(pl.col("in_full")) if securities else meta.head(0)
    port = meta.filter(pl.col("security_type").is_in(list(PORTFOLIO_TYPES)) & pl.col("is_primary_security")) \
        if aggregates else meta.head(0)
    inst = meta.filter(pl.col("security_type").is_in(list(INSTRUMENT_TYPES))
                       & ~pl.col("security_id").is_in(list(EXCLUDED_INSTRUMENTS))) if aggregates else meta.head(0)
    ids = sorted(set(want_sec["security_id"]) | set(port["security_id"]) | set(inst["security_id"])
                 | {GLOBAL_BENCH} | set(REGION_BENCH.values()))
    log.info("returns: loading %s closes for %d securities", freq, len(ids))
    pdates, sids, C = close_matrix(settings, ids, freq, start - timedelta(days=40), end)
    dates, R = returns_from_close(pdates, C, freq)
    del C
    dates_keep = np.array([d >= start for d in dates], dtype=bool)
    dates = [d for d, k in zip(dates, dates_keep, strict=True) if k]
    R = R[dates_keep]
    spos = {s: i for i, s in enumerate(sids)}
    out: dict[str, Panel] = {}
    if securities:
        sel = [spos[s] for s in want_sec["security_id"].to_list() if s in spos]
        out["security"] = Panel(freq, dates, [("security", sids[i], "none") for i in sel], R[:, sel])
    if aggregates:
        W = mcap_matrix(settings, dates, sids, freq)
        parts = [portfolio_panel(R, W, dates, meta, sids, freq, min_periods)]
        types = dict(zip(inst["security_id"].to_list(), inst["security_type"].to_list(), strict=True))
        isel = [spos[s] for s in sorted(types) if s in spos]
        parts.append(Panel(freq, dates, [(types[sids[i]], sids[i], "none") for i in isel], R[:, isel]))
        parts.append(factor_ls_panel(settings, freq, start))
        out["aggregate"] = Panel.concat(parts)
    bench = {b: R[:, spos[b]] for b in {GLOBAL_BENCH} | set(REGION_BENCH.values()) if b in spos}
    for p in out.values():
        p.meta["bench"] = {b: (dates, v) for b, v in bench.items()}
    return out
