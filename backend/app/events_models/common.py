"""Shared loaders / helpers for the event models (owner: event-models).

* T1 (event_daily_agg) country / sector / issuer panels decoded to ISO2 / sector id / issuer_id + theme ids
* country equity index map (local index first, iShares country ETF fallback) and price loaders
* state file data/state/event_models.json and table writers
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import conform
from app.core.storage import write_table
from app.events_v2 import store
from app.events_v2 import taxonomy as tx

log = get_logger(__name__)

THEME_BY_BIT = {t.bit: t for t in tx.THEMES}
THEME_ID = {t.bit: t.id for t in tx.THEMES}
THEME_BIT = {t.id: t.bit for t in tx.THEMES}
THEME_GROUP = {t.bit: t.group for t in tx.THEMES}
GROUPS = sorted({t.group for t in tx.THEMES})
#: geopolitical-risk themes (C3): threats vs acts split follows Caldara-Iacoviello (2022)
GPR_THREATS = ("geopolitical_tension", "sanctions", "trade_tariffs")
GPR_ACTS = ("conflict_military", "terrorism", "coup_regime")

#: country -> equity price series (local index preferred; USD country ETF fallback)
COUNTRY_INDEX: dict[str, list[str]] = {
    "US": ["IDX:^GSPC", "ETF:SPY"], "CA": ["IDX:^GSPTSE", "ETF:EWC"], "MX": ["IDX:^MXX", "ETF:EWW"],
    "BR": ["IDX:^BVSP", "ETF:EWZ"], "AR": ["IDX:^MERV", "ETF:ARGT"], "CL": ["ETF:ECH"], "PE": ["ETF:EPU"],
    "GB": ["IDX:^FTSE", "ETF:EWU"], "DE": ["IDX:^GDAXI", "ETF:EWG"], "FR": ["IDX:^FCHI", "ETF:EWQ"],
    "IT": ["IDX:FTSEMIB.MI", "ETF:EWI"], "ES": ["IDX:^IBEX", "ETF:EWP"], "PT": ["IDX:PSI20.LS"],
    "NL": ["IDX:^AEX", "ETF:EWN"], "BE": ["IDX:^BFX", "ETF:EWK"], "AT": ["IDX:^ATX", "ETF:EWO"],
    "CH": ["IDX:^SSMI", "ETF:EWL"], "SE": ["IDX:^OMX", "ETF:EWD"], "DK": ["ETF:EDEN"], "NO": ["ETF:ENOR"],
    "FI": ["ETF:EFNL"], "IE": ["ETF:EIRL"], "GR": ["ETF:GREK"], "PL": ["ETF:EPOL"], "TR": ["ETF:TUR"],
    "ZA": ["ETF:EZA"], "SA": ["ETF:KSA"], "AE": ["ETF:UAE"], "QA": ["ETF:QAT"],
    "JP": ["IDX:^N225", "ETF:EWJ"], "KR": ["IDX:^KS11", "ETF:EWY"], "CN": ["IDX:000300.SS", "ETF:MCHI"],
    "HK": ["IDX:^HSI", "ETF:EWH"], "TW": ["IDX:^TWII", "ETF:EWT"], "IN": ["IDX:^BSESN", "ETF:INDA"],
    "AU": ["IDX:^AXJO", "ETF:EWA"], "SG": ["IDX:^STI", "ETF:EWS"], "MY": ["IDX:^KLSE", "ETF:EWM"],
    "ID": ["IDX:^JKSE", "ETF:EIDO"], "TH": ["ETF:THD"], "PH": ["ETF:EPHE"], "VN": ["ETF:VNM"],
}
WORLD_BENCHMARK = ["ETF:ACWI", "ETF:VT", "IDX:^GSPC"]
SECTOR_ETF = {"TECH": "ETF:XLK", "COMM": "ETF:XLC", "CDIS": "ETF:XLY", "CSTA": "ETF:XLP", "HLTH": "ETF:XLV",
              "FINS": "ETF:XLF", "INDU": "ETF:XLI", "MATR": "ETF:XLB", "ENRG": "ETF:XLE", "UTIL": "ETF:XLU",
              "REAL": "ETF:XLRE"}

STATE_FILE = "event_models.json"


# ------------------------------------------------------------------------------------------------ state


def state_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.data_dir / "state" / STATE_FILE


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"owner": "event-models", "items": {}, "notes": [], "runs": {}}


def update_state(key: str, value: Any, settings: Settings | None = None, section: str = "runs") -> None:
    """Merge one entry into data/state/event_models.json (atomic replace, retried on Windows sharing errors)."""
    p = state_path(settings)
    st = load_state(settings)
    st.setdefault(section, {})[key] = value
    st["updated"] = datetime.now(UTC).isoformat(timespec="seconds")
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:
            time.sleep(0.2 * (i + 1))


def now_utc() -> datetime:
    return datetime.now(UTC)


def write(df: pl.DataFrame, table: str, mode: str | None = None, settings: Settings | None = None) -> int:
    if df.is_empty():
        return 0
    df = conform(df, table)
    write_table(df, table, mode=mode, settings=settings)
    return df.height


# ------------------------------------------------------------------------------------------------ T1 loaders


def t1_range(settings: Settings | None = None) -> tuple[date, date] | None:
    from app.events_v2.agg import t1_range as _r  # noqa: PLC0415

    return _r(settings)


def _dict_map(dict_type: str, settings: Settings | None = None) -> dict[int, str]:
    from app.events_v2.dicts import load_dict  # noqa: PLC0415

    d = load_dict(dict_type, settings)
    return dict(zip(d["id"].to_list(), d["code"].to_list())) if d.height else {}


def load_t1(label_type: str, start: date | None = None, end: date | None = None, themes: list[int] | None = None,
            settings: Settings | None = None) -> pl.DataFrame:
    """T1 rows of one label type with `label` decoded (ISO2 | sector id | issuer_id) and `theme` kept as the bit
    (-1 = all themes). Columns: date, label, theme, ev_n, art_n, ev_conflict_n, ev_goldstein, ev_tone, art_tone,
    art_neg_share, attention_z, tone_z."""
    s = settings or get_settings()
    rng = t1_range(s)
    if rng is None:
        return pl.DataFrame()
    lo, hi = start or rng[0], end or rng[1]
    df = store.read_range("event_daily_agg", lo, hi, s)
    if df.is_empty():
        return df
    df = df.filter(pl.col("label_type") == label_type)
    if themes is not None:
        df = df.filter(pl.col("theme").is_in(themes))
    m = _dict_map(label_type, s)
    lab = pl.DataFrame({"label_id": list(m.keys()), "label": list(m.values())},
                       schema={"label_id": pl.Int32, "label": pl.Utf8})
    df = store.dedupe_pk("event_daily_agg", df)  # never two rows per (date, label_type, label_id, theme)
    df = df.join(lab, on="label_id", how="left").filter(pl.col("label").is_not_null())
    df = df.select("date", "label", "theme", "ev_n", "art_n", "ev_conflict_n", "ev_goldstein", "ev_tone", "art_tone",
                   "art_neg_share", "attention_z", "tone_z").sort("label", "theme", "date")
    return fill_z(df)


def fill_z(df: pl.DataFrame, window: str = "90d", min_obs: int = 10) -> pl.DataFrame:
    """Fill attention_z / tone_z where events_agg has not computed them yet (newest-first backfills: T1 z-scores
    converge only after an events_agg full=true run). Same definition as T1: z of log1p(ev_n + art_n) and of
    art_tone (else ev_tone) vs the label/theme's own trailing `window` (current day excluded, >= min_obs rows)."""
    if df.is_empty() or (df["attention_z"].null_count() == 0 and df["tone_z"].null_count() == 0):
        return df
    x = pl.col("_a")
    t = pl.col("_t")
    d = df.with_columns((pl.col("ev_n") + pl.col("art_n")).log1p().alias("_a"),
                        pl.coalesce("art_tone", "ev_tone").cast(pl.Float64).alias("_t"))
    over = ["label", "theme"]
    roll = {}
    for c, e in (("a", x), ("t", t)):
        roll[f"_m{c}"] = e.rolling_mean_by("date", window, closed="left").over(over)
        roll[f"_s{c}"] = e.rolling_std_by("date", window, closed="left").over(over)
        roll[f"_n{c}"] = e.is_not_null().cast(pl.Float64).rolling_sum_by("date", window, closed="left").over(over)
    d = d.with_columns(**roll)

    def z(c: str, e: pl.Expr) -> pl.Expr:
        return pl.when((pl.col(f"_n{c}") >= min_obs) & (pl.col(f"_s{c}") > 1e-9)) \
            .then((e - pl.col(f"_m{c}")) / pl.col(f"_s{c}")).otherwise(None)

    d = d.with_columns(pl.coalesce(pl.col("attention_z"), z("a", x).cast(pl.Float32)).alias("attention_z"),
                       pl.coalesce(pl.col("tone_z"), z("t", t).cast(pl.Float32)).alias("tone_z"))
    return d.drop([c for c in d.columns if c.startswith("_")])


def window_desc(df: pl.DataFrame) -> dict[str, Any]:
    if df.is_empty():
        return {"start": None, "end": None, "days": 0}
    lo, hi = df["date"].min(), df["date"].max()
    return {"start": lo.isoformat(), "end": hi.isoformat(), "days": int(df["date"].n_unique())}


def week_start(d: pl.Expr) -> pl.Expr:
    return d.dt.truncate("1w")


# ------------------------------------------------------------------------------------------------ prices


def lake_con(tables: list[str], settings: Settings | None = None):
    from app.modeling.datasets import lake_connection  # noqa: PLC0415

    return lake_connection(tables, settings)


def load_closes(ids: list[str], start: date, end: date | None = None, settings: Settings | None = None,
                con=None) -> pd.DataFrame:
    """Wide adj_close (dates x security_id) for the ids that have data."""
    if not ids:
        return pd.DataFrame()
    con = con or lake_con(["prices_daily"], settings)
    q = ("SELECT security_id, date, coalesce(adj_close, close) AS px FROM prices_daily WHERE security_id IN "
         f"({','.join('?' * len(ids))}) AND date >= ?" + (" AND date <= ?" if end else ""))
    args = list(ids) + [start] + ([end] if end else [])
    df = con.execute(q, args).df()
    if df.empty:
        return pd.DataFrame()
    w = df.pivot_table(index="date", columns="security_id", values="px", aggfunc="last").sort_index()
    w.index = pd.to_datetime(w.index)
    return w


def country_closes(countries: list[str] | None, start: date, end: date | None = None,
                   settings: Settings | None = None, min_obs: int = 60) -> tuple[pd.DataFrame, dict[str, str]]:
    """Wide closes by ISO2 (first preference with >= min_obs points since start). -> (frame, {iso2: security_id})."""
    cc = countries or list(COUNTRY_INDEX)
    ids = sorted({i for c in cc for i in COUNTRY_INDEX.get(c, [])})
    W = load_closes(ids, start, end, settings)
    out, used = {}, {}
    for c in cc:
        for sid in COUNTRY_INDEX.get(c, []):
            if sid in W.columns and W[sid].notna().sum() >= min_obs:
                out[c], used[c] = W[sid], sid
                break
    return (pd.DataFrame(out) if out else pd.DataFrame()), used


def log_returns(px: pd.DataFrame) -> pd.DataFrame:
    """Daily log returns per column on that column's own trading days (no fill across holidays), |r| > 40% dropped."""
    out = {}
    for c in px.columns:
        s = px[c].dropna()
        s = s[s > 0]
        r = np.log(s).diff()
        out[c] = r.where(r.abs() < 0.4)
    return pd.DataFrame(out).sort_index()


def world_benchmark(start: date, end: date | None = None, settings: Settings | None = None) -> pd.Series:
    W = load_closes(WORLD_BENCHMARK, start, end, settings)
    for sid in WORLD_BENCHMARK:
        if sid in W.columns and W[sid].notna().sum() > 100:
            return W[sid]
    return pd.Series(dtype=float)


def iso_week(d: date) -> date:
    return d - timedelta(days=d.weekday())
