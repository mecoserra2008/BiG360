"""Country x trading-day panel of equity responses and event shocks (B2 / B4 / C4 inputs; owner: event-models).

* ret: daily log return of the country index (%), holidays inside the common calendar -> 0
* vol: Parkinson high-low range volatility (%), |ret| when the range is missing
* shocks on trading day t aggregate the UTC event days (previous trading day, t] (weekend news lands on Monday):
  attention_all / tone_all = mean T1 attention_z / tone_z (all themes); attention_geo = z of log1p(items in the
  geo theme group) vs the country's trailing 90 days
* macro_surprise: daily change of macro_surprise_index (theme all), countries in that table only
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.events_models import common as C

SHOCKS = ("attention_all", "tone_all", "attention_geo")
GEO_GROUP = "geo"


def _rolling_z(s: pd.Series, window: int = 90, min_obs: int = 10) -> pd.Series:
    m = s.shift(1).rolling(window, min_periods=min_obs).mean()
    sd = s.shift(1).rolling(window, min_periods=min_obs).std()
    return (s - m) / sd.where(sd > 1e-9)


def event_shocks(settings: Settings | None = None, start: date | None = None) -> pd.DataFrame:
    """Calendar-day shocks per country: [date, iso2, attention_all, tone_all, attention_geo, items]."""
    t1 = C.load_t1("country", start=start - timedelta(days=120) if start else None, settings=settings)
    if t1.is_empty():
        return pd.DataFrame(columns=["date", "iso2", *SHOCKS, "items"])
    geo_bits = [b for b, g in C.THEME_GROUP.items() if g == GEO_GROUP]
    allt = t1.filter(pl.col("theme") == -1).select(
        "date", pl.col("label").alias("iso2"), pl.col("attention_z").alias("attention_all"),
        pl.col("tone_z").alias("tone_all"), (pl.col("ev_n") + pl.col("art_n")).alias("items"))
    geo = t1.filter(pl.col("theme").is_in(geo_bits)).group_by("date", "label").agg(
        (pl.col("ev_n") + pl.col("art_n")).sum().alias("geo_items")).rename({"label": "iso2"})
    df = allt.join(geo, on=["date", "iso2"], how="left").with_columns(pl.col("geo_items").fill_null(0)) \
        .filter(pl.col("iso2") != "GLOBAL").to_pandas()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["iso2", "date"])
    df["attention_geo"] = df.groupby("iso2", group_keys=False)["geo_items"].apply(lambda s: _rolling_z(np.log1p(s)))
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    return df[["date", "iso2", *SHOCKS, "items"]].reset_index(drop=True)


def _to_trading(ev: pd.DataFrame, cal: pd.DatetimeIndex) -> pd.DataFrame:
    """Map calendar event days to the first trading day on or after them and average."""
    cal = cal.sort_values()
    pos = np.searchsorted(cal.values, ev["date"].values, side="left")
    ok = pos < len(cal)
    e = ev[ok].copy()
    e["tdate"] = cal.values[pos[ok]]
    return e.groupby(["iso2", "tdate"], as_index=False)[[*SHOCKS, "items"]].mean().rename(columns={"tdate": "date"})


def price_frames(countries: list[str], start: date, end: date | None, settings: Settings) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, str]]:
    """(ret %, range vol %) wide frames by ISO2 on the common calendar (>= 70% of countries trading)."""
    ids = sorted({i for c in countries for i in C.COUNTRY_INDEX.get(c, [])})
    con = C.lake_con(["prices_daily"], settings)
    q = (f"SELECT security_id, date, coalesce(adj_close, close) AS px, high, low FROM prices_daily "
         f"WHERE security_id IN ({','.join('?' * len(ids))}) AND date >= ?" + (" AND date <= ?" if end else ""))
    raw = con.execute(q, list(ids) + [start - timedelta(days=10)] + ([end] if end else [])).df()
    if raw.empty:
        return pd.DataFrame(), pd.DataFrame(), {}
    raw["date"] = pd.to_datetime(raw["date"])
    cnt = raw.groupby("security_id").size()
    need = max(5, int(0.5 * raw["date"].nunique()))
    used: dict[str, str] = {}
    for c in countries:
        for sid in C.COUNTRY_INDEX.get(c, []):
            if cnt.get(sid, 0) >= need:
                used[c] = sid
                break
    sub = raw[raw["security_id"].isin(used.values())].copy()
    sub["iso2"] = sub["security_id"].map({v: k for k, v in used.items()})
    sub = sub.sort_values(["iso2", "date"])
    sub["ret"] = sub.groupby("iso2")["px"].transform(lambda s: np.log(s.where(s > 0)).diff()) * 100
    sub.loc[sub["ret"].abs() > 40, "ret"] = np.nan
    hl = np.log(sub["high"].where(sub["high"] > 0) / sub["low"].where(sub["low"] > 0))
    sub["vol"] = (np.sqrt(0.3607 * hl ** 2) * 100).where((hl > 0) & (hl < 0.5)).fillna(sub["ret"].abs())
    R = sub.pivot_table(index="date", columns="iso2", values="ret", aggfunc="last").sort_index()
    V = sub.pivot_table(index="date", columns="iso2", values="vol", aggfunc="last").sort_index().reindex(R.index)
    keep = R.notna().mean(axis=1) >= 0.7
    R, V = R[keep], V[keep]
    R = R[R.index >= pd.Timestamp(start)]
    V = V[V.index >= pd.Timestamp(start)]
    return R, V, used


def macro_surprise_changes(countries: list[str], start: date, settings: Settings) -> pd.DataFrame:
    try:
        con = C.lake_con(["macro_surprise_index"], settings)
        df = con.execute("SELECT country AS iso2, date, value FROM macro_surprise_index WHERE theme = 'all' AND date >= ?",
                         [start - timedelta(days=10)]).df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    W = df.pivot_table(index="date", columns="iso2", values="value", aggfunc="last").sort_index()
    W = W[[c for c in W.columns if c in countries]]
    return W.diff()[W.index >= pd.Timestamp(start)]


def build_panel(settings: Settings | None = None, start: date | None = None, end: date | None = None,
                freq: str = "auto") -> dict[str, Any]:
    """-> {freq, dates, countries, Y: {ret, vol, macro_surprise}: wide frames, S: {shock: wide frame}, used, window}.
    Daily panel when < 26 weeks of event history (or freq='D'), else weekly sums (ret) / means (vol, shocks)."""
    s = settings or get_settings()
    rng = C.t1_range(s)
    if rng is None:
        return {}
    lo = start or rng[0]
    hi = end or rng[1]
    ev = event_shocks(s, lo)
    if ev.empty:
        return {}
    countries = sorted(set(ev["iso2"]) & set(C.COUNTRY_INDEX))
    R, V, used = price_frames(countries, lo, hi + timedelta(days=4), s)
    if R.empty:
        return {}
    cal = R.index
    evt = _to_trading(ev[ev["iso2"].isin(R.columns)], cal)
    S = {k: evt.pivot_table(index="date", columns="iso2", values=k, aggfunc="mean").reindex(cal) for k in SHOCKS}
    M = macro_surprise_changes(list(R.columns), lo, s)
    if not M.empty:
        M = M.reindex(cal, method="ffill").fillna(0.0) if len(M) else M
    nweeks = (hi - lo).days / 7
    f = ("W" if nweeks >= 26 else "D") if freq == "auto" else freq
    Y = {"ret": R, "vol": V, "macro_surprise": M}
    if f == "W":
        wk = lambda X, how: getattr(X.groupby(X.index.to_period("W-SUN").start_time), how)() if len(X) else X  # noqa: E731
        Y = {"ret": wk(R, "sum"), "vol": wk(V, "mean"), "macro_surprise": wk(M, "sum") if len(M) else M}
        S = {k: wk(v, "mean") for k, v in S.items()}
    return {"freq": f, "Y": Y, "S": S, "used": used, "countries": list(R.columns),
            "window": {"start": lo, "end": min(hi, R.index.max().date())}}


def balanced(Y: pd.DataFrame, X: pd.DataFrame, min_cov: float = 0.8) -> tuple[list[str], pd.DatetimeIndex, np.ndarray, np.ndarray]:
    """Balanced block of (y, x): countries with >= min_cov of periods for both, then periods complete for all of them.
    -> (codes, dates, y (T*N time-major), x (T*N))."""
    common = [c for c in Y.columns if c in X.columns]
    Yc, Xc = Y[common], X.reindex(Y.index)[common]
    live = (Xc.notna().mean(axis=1) >= 0.5) & (Yc.notna().mean(axis=1) >= 0.5)  # warm-up periods of the z-scores
    Yc, Xc = Yc[live], Xc[live]
    ok =(Yc.notna() & Xc.notna()).mean() >= min_cov
    cols = [c for c in common if ok.get(c, False)]
    if len(cols) < 5:
        return [], pd.DatetimeIndex([]), np.empty(0), np.empty(0)
    Yc, Xc = Yc[cols], Xc[cols]
    rows = (Yc.notna() & Xc.notna()).all(axis=1)
    Yc, Xc = Yc[rows], Xc[rows]
    return cols, Yc.index, Yc.to_numpy(float).reshape(-1), Xc.to_numpy(float).reshape(-1)
