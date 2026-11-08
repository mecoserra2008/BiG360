"""C3 job `event_gpr`: model-based geopolitical-risk index per country from labelled GDELT coverage, validated against
the published Caldara-Iacoviello GPR (daily GPRD and monthly country GPRC; downloaded from matteoiacoviello.com and
cached under data/raw/event_models/, refreshed weekly).

Index: share_t = items with GPR themes / all labelled items (items = events + articles of the country-day);
threats = geopolitical_tension | sanctions | trade_tariffs, acts = conflict_military | terrorism | coup_regime.
gpr = 100 * share / mean(share over the base window) (base = calendar 2021 when covered, else all days so far).
"""

from __future__ import annotations

import json
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
from scipy import stats

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C

log = get_logger(__name__)

GPR_URL = "https://www.matteoiacoviello.com/gpr_files/{name}"
FILES = {"daily": "data_gpr_daily_recent.xls", "monthly": "data_gpr_export.xls"}
BASE = (date(2021, 1, 1), date(2021, 12, 31))
ISO3_TO_2 = {"ARG": "AR", "AUS": "AU", "BEL": "BE", "BRA": "BR", "CAN": "CA", "CHE": "CH", "CHL": "CL", "CHN": "CN",
             "COL": "CO", "DEU": "DE", "DNK": "DK", "EGY": "EG", "ESP": "ES", "FIN": "FI", "FRA": "FR", "GBR": "GB",
             "HKG": "HK", "HUN": "HU", "IDN": "ID", "IND": "IN", "ISR": "IL", "ITA": "IT", "JPN": "JP", "KOR": "KR",
             "MEX": "MX", "MYS": "MY", "NLD": "NL", "NOR": "NO", "PER": "PE", "PHL": "PH", "POL": "PL", "PRT": "PT",
             "RUS": "RU", "SAU": "SA", "SWE": "SE", "THA": "TH", "TUN": "TN", "TUR": "TR", "TWN": "TW", "UKR": "UA",
             "USA": "US", "VEN": "VE", "VNM": "VN", "ZAF": "ZA"}


def _raw_dir(settings: Settings) -> Path:
    d = settings.data_dir / "raw" / "event_models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_official(settings: Settings | None = None, max_age_days: int = 7) -> dict[str, pd.DataFrame]:
    """Published GPR (daily GPRD + monthly GPRC_*), cached; returns {} when unavailable."""
    s = settings or get_settings()
    out: dict[str, pd.DataFrame] = {}
    for k, name in FILES.items():
        p = _raw_dir(s) / name
        stale = not p.exists() or (time.time() - p.stat().st_mtime) > max_age_days * 86400
        if stale:
            try:
                req = urllib.request.Request(GPR_URL.format(name=name), headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=120) as f:
                    data = f.read()
                if data[:4] == b"\xd0\xcf\x11\xe0":   # OLE2 .xls
                    p.write_bytes(data)
            except Exception as e:  # noqa: BLE001
                log.warning("gpr: download of %s failed (%s); using cache if present", name, e)
        if p.exists():
            try:
                out[k] = pd.read_excel(p)
            except Exception as e:  # noqa: BLE001
                log.warning("gpr: cannot read %s: %s", p, e)
    return out


def compute_index(settings: Settings | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    s = settings or get_settings()
    t1 = C.load_t1("country", settings=s)
    if t1.is_empty():
        return pl.DataFrame(), {}
    thr = [C.THEME_BIT[t] for t in C.GPR_THREATS]
    act = [C.THEME_BIT[t] for t in C.GPR_ACTS]
    it = (pl.col("ev_n") + pl.col("art_n"))
    tot = t1.filter(pl.col("theme") == -1).group_by("date", "label").agg(it.sum().alias("n"))
    th = t1.filter(pl.col("theme").is_in(thr)).group_by("date", "label").agg(it.sum().alias("thr"))
    ac = t1.filter(pl.col("theme").is_in(act)).group_by("date", "label").agg(it.sum().alias("act"))
    d = tot.join(th, on=["date", "label"], how="left").join(ac, on=["date", "label"], how="left").fill_null(0)
    glob = d.group_by("date").agg(pl.col("n").sum(), pl.col("thr").sum(), pl.col("act").sum()) \
        .with_columns(pl.lit("GLOBAL").alias("label"))
    d = pl.concat([d.filter(pl.col("label") != "GLOBAL"), glob.select(d.columns)])
    d = d.filter(pl.col("n") > 0).with_columns(
        ((pl.col("thr") + pl.col("act")) / pl.col("n")).alias("share"), (pl.col("thr") / pl.col("n")).alias("s_thr"),
        (pl.col("act") / pl.col("n")).alias("s_act"))
    lo, hi = d["date"].min(), d["date"].max()
    covered = lo <= BASE[0] and hi >= BASE[1]
    b0, b1 = BASE if covered else (lo, hi)
    base = d.filter(pl.col("date").is_between(b0, b1)).group_by("label").agg(
        pl.col("share").mean().alias("b"), pl.col("s_thr").mean().alias("bt"), pl.col("s_act").mean().alias("ba"),
        pl.col("n").mean().alias("bn"))
    # countries need a minimum of coverage for a stable share
    base = base.filter(pl.col("bn") >= 20)
    d = d.join(base, on="label", how="inner").with_columns(
        (100 * pl.col("share") / pl.col("b")).alias("gpr"), (100 * pl.col("s_thr") / pl.col("bt")).alias("gpr_threats"),
        (100 * pl.col("s_act") / pl.col("ba")).alias("gpr_acts")).sort("label", "date")
    d = d.with_columns(pl.col("gpr").rolling_mean_by("date", "7d").over("label").alias("gpr_ma7"))
    meta = {"base_start": b0, "base_end": b1, "window": C.window_desc(t1)}
    return d.rename({"label": "iso2", "n": "n_items"}), meta


def validate(idx: pl.DataFrame, off: dict[str, pd.DataFrame]) -> dict[str, Any]:
    rep: dict[str, Any] = {}
    if "daily" in off and idx.height:
        o = off["daily"].copy()
        o["date"] = pd.to_datetime(o["DAY"].astype(str), format="%Y%m%d")
        g = idx.filter(pl.col("iso2") == "GLOBAL").select("date", "gpr", "gpr_threats", "gpr_acts").to_pandas()
        g["date"] = pd.to_datetime(g["date"])
        m = g.merge(o[["date", "GPRD", "GPRD_THREAT", "GPRD_ACT", "GPRD_MA7"]], on="date").dropna()
        if len(m) >= 10:
            def corr(a: str, b: str, frame: pd.DataFrame) -> dict[str, Any]:
                return {"pearson": round(float(frame[a].corr(frame[b])), 3),
                        "spearman": round(float(stats.spearmanr(frame[a], frame[b]).statistic), 3), "n": int(len(frame))}

            dw = m.copy()
            for c in ("gpr", "GPRD"):   # remove each index's day-of-week cycle (news volume / weekend effects)
                dw[c] = np.log(dw[c]) - np.log(dw[c]).groupby(dw["date"].dt.dayofweek).transform("mean")
            m7 = m.set_index("date")[["gpr", "GPRD"]].rolling(7).mean().dropna()
            dl = np.log(m.set_index("date")[["gpr", "GPRD"]]).diff().dropna()
            rep["daily_global"] = {"window": [str(m["date"].min().date()), str(m["date"].max().date())],
                                   "level": corr("gpr", "GPRD", m), "threats": corr("gpr_threats", "GPRD_THREAT", m),
                                   "acts": corr("gpr_acts", "GPRD_ACT", m),
                                   "dow_adjusted_log": corr("gpr", "GPRD", dw),
                                   "ma7": corr("gpr", "GPRD", m7.reset_index()) if len(m7) >= 10 else None,
                                   "log_changes": corr("gpr", "GPRD", dl.reset_index()) if len(dl) >= 10 else None}
    if "monthly" in off and idx.height:
        mo = off["monthly"].copy()
        mo["month"] = pd.to_datetime(mo["month"])
        cols = {c: ISO3_TO_2[c[5:]] for c in mo.columns if c.startswith("GPRC_") and c[5:] in ISO3_TO_2}
        d = idx.filter(pl.col("iso2") != "GLOBAL").to_pandas()
        d["month"] = pd.to_datetime(d["date"]).dt.to_period("M").dt.to_timestamp()
        tot = d.groupby("month")["n_items"].sum()
        full_months = d.groupby("month")["date"].nunique()
        full_months = full_months[full_months >= 25].index
        # GPRC_X = share of ALL articles mentioning GPR terms and country X -> analog: geo items of X / all items
        d["geo"] = d["share"] * d["n_items"]
        agg = d.groupby(["month", "iso2"])["geo"].sum().reset_index()
        agg["ours"] = agg["geo"] / agg["month"].map(tot)
        cs = []
        for mth in full_months:
            row = mo[mo["month"] == mth]
            if row.empty:
                continue
            offv = pd.Series({cols[c]: float(row[c].iloc[0]) for c in cols}).dropna()
            ours = agg[agg["month"] == mth].set_index("iso2")["ours"]
            j = pd.concat([ours, offv], axis=1, join="inner").dropna()
            if len(j) >= 10:
                cs.append({"month": str(mth.date()), "n_countries": int(len(j)),
                           "spearman": round(float(stats.spearmanr(j.iloc[:, 0], j.iloc[:, 1]).statistic), 3),
                           "pearson_log": round(float(np.corrcoef(np.log(j.iloc[:, 0]), np.log(j.iloc[:, 1]))[0, 1]), 3)})
        rep["monthly_country_cross_section"] = cs
        # per-country monthly time-series correlation once >= 12 full months overlap
        ts = []
        if len(full_months) >= 12:
            for c3, c2 in cols.items():
                a = agg[agg["iso2"] == c2].set_index("month")["ours"]
                b = mo.set_index("month")[c3]
                j = pd.concat([a, b], axis=1, join="inner").dropna()
                j = j[j.index.isin(full_months)]
                if len(j) >= 12:
                    ts.append({"iso2": c2, "n_months": int(len(j)), "pearson": round(float(j.iloc[:, 0].corr(j.iloc[:, 1])), 3)})
        rep["monthly_country_timeseries"] = ts
    return rep


def run_gpr(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    idx, meta = compute_index(s)
    if idx.is_empty():
        return {"rows": 0}
    off = fetch_official(s)
    ref = None
    if "daily" in off:
        o = off["daily"]
        ref = pl.DataFrame({"date": pd.to_datetime(o["DAY"].astype(str), format="%Y%m%d").dt.date.tolist(),
                            "reference_gpr": o["GPRD"].astype(float).tolist(), "iso2": ["GLOBAL"] * len(o)})
    now = C.now_utc()
    out = idx.select("iso2", "date", "gpr", "gpr_threats", "gpr_acts", "gpr_ma7", "share", "n_items")
    out = out.join(ref, on=["iso2", "date"], how="left") if ref is not None else out.with_columns(pl.lit(None, pl.Float64).alias("reference_gpr"))
    out = out.with_columns(pl.lit(meta["base_start"]).alias("base_start"), pl.lit(meta["base_end"]).alias("base_end"),
                           pl.lit(True).alias("model_based"), pl.lit(now).alias("computed_at"))
    n = C.write(out, "gpr_index", mode="overwrite", settings=s)
    val = validate(idx, off)
    rep = {"rows": n, "countries": int(out["iso2"].n_unique()), "window": meta["window"],
           "base": [str(meta["base_start"]), str(meta["base_end"])], "validation": val,
           "seconds": round(time.perf_counter() - t0, 1)}
    rd = s.data_dir / "logs" / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"gpr_validation_{date.today():%Y%m%d}.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    C.update_state("event_gpr", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_gpr(ctx.settings)
    return JobResult(status="success", rows_written={"gpr_index": rep.get("rows", 0)}, message=str(rep)[:2000])
