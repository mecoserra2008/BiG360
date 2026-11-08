"""C4 job `event_study_library`: CAR / BHAR after labelled event shocks by theme x country x sector (reusing the
Model Studio market-model event study `app.modeling._algos.car_study`), plus the "expected reaction" lookup.

Levels: country_index (country x theme shocks -> country index vs ACWI), sector_etf (sector x theme shocks -> US
sector SPDR vs SPY), issuer (issuer shocks -> primary security vs its country index; theme = the issuer's dominant
article theme that day from gdelt_article_labels, plus 'all').
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
from scipy import stats

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C
from app.modeling._algos import car_study

log = get_logger(__name__)

WINDOWS = {"0,1": (0, 1), "0,5": (0, 5), "-1,5": (-1, 5), "0,10": (0, 10)}
EST = (-250, -30)
Z_ATT, Z_TONE, GAP_DAYS, MIN_ITEMS = 2.5, -2.5, 10, 20
FALLBACKS = (("theme", "iso2", "sector"), ("theme", "iso2"), ("theme", "sector"), ("theme",))


def shocks(t1: pl.DataFrame, min_items: int = MIN_ITEMS) -> pl.DataFrame:
    """(label, theme, date, kind) shock days: attention_z >= 2.5 / tone_z <= -2.5 with >= min_items items, and no
    shock of the same kind for that (label, theme) in the previous GAP_DAYS days."""
    t = t1.with_columns((pl.col("ev_n") + pl.col("art_n")).alias("items")).filter(pl.col("items") >= min_items)
    out = []
    for kind, cond in (("attention", pl.col("attention_z") >= Z_ATT), ("neg_tone", pl.col("tone_z") <= Z_TONE)):
        s = t.filter(cond).select("label", "theme", "date", "attention_z", "tone_z").sort("label", "theme", "date")
        s = s.with_columns(pl.col("date").shift(1).over("label", "theme").alias("prev"))
        s = s.filter(pl.col("prev").is_null() | ((pl.col("date") - pl.col("prev")).dt.total_days() > GAP_DAYS))
        out.append(s.drop("prev").with_columns(pl.lit(kind).alias("kind")))
    return pl.concat(out)


def _simple_returns(px: pd.DataFrame) -> pd.DataFrame:
    r = px.pct_change(fill_method=None)
    return r.where(r.abs() < 0.5)


def _run_level(level: str, ev: pd.DataFrame, R: pd.DataFrame, mkt: pd.Series | dict[str, pd.Series],
               bench_name: str | dict[str, str]) -> list[dict[str, Any]]:
    """ev: [entity, date, kind, theme, iso2, sector]. mkt: one benchmark or per-entity benchmarks.
    Returns per-event CARs for every window: [entity, tdate, kind, theme, iso2, sector, window, car, bhar]."""
    rows = []
    if ev.empty or R.empty:
        return rows
    cal = R.index
    pos = np.searchsorted(cal.values, pd.to_datetime(ev["date"]).values, side="left")
    ev = ev.assign(tdate=[cal[p] if p < len(cal) else pd.NaT for p in pos]).dropna(subset=["tdate"])
    groups = [(None, mkt)] if isinstance(mkt, pd.Series) else [(b, s) for b, s in mkt.items()]
    for bkey, m in groups:
        sub = ev if bkey is None else ev[ev["bench"] == bkey]
        if sub.empty:
            continue
        uniq = sub[["entity", "tdate"]].drop_duplicates()
        events = [(e, d) for e, d in uniq.itertuples(index=False) if e in R.columns]
        for wname, win in WINDOWS.items():
            try:
                out = car_study(R, m.reindex(R.index), events, EST, win, "market")
            except ValueError:
                continue
            e = out["events"].rename(columns={"entity_id": "entity", "event_date": "tdate"})
            e["tdate"] = pd.to_datetime(e["tdate"])
            j = sub.merge(e[["entity", "tdate", "car", "bhar"]], on=["entity", "tdate"], how="inner")
            j["window"] = wname
            rows.append(j)
    return rows


def _aggregate(per: pd.DataFrame, level: str, bench: str) -> list[dict[str, Any]]:
    out = []
    now = C.now_utc()
    keys_all = []
    for keys in (("theme", "iso2", "sector"), ("theme", "iso2"), ("theme", "sector"), ("theme",)):
        keys_all.append(keys)
    seen = set()
    for keys in keys_all:
        g = per.copy()
        for k in ("iso2", "sector"):
            if k not in keys:
                g[k] = "ALL"
        for (kind, th, cc, sec, win), x in g.groupby(["kind", "theme", "iso2", "sector", "window"]):
            if (kind, th, cc, sec, win) in seen:
                continue
            seen.add((kind, th, cc, sec, win))
            car = x["car"].to_numpy(float) * 100
            n = len(car)
            if n < 2:
                continue
            m, sd = float(car.mean()), float(car.std(ddof=1))
            se = sd / np.sqrt(n) if sd > 0 else None
            t = m / se if se else None
            p = float(2 * stats.t.sf(abs(t), n - 1)) if t is not None else None
            q = stats.t.ppf(0.975, n - 1)
            out.append({"shock_kind": kind, "level": level, "theme": th, "iso2": cc, "sector": sec, "window": win,
                        "n_events": n, "mean_car": m, "median_car": float(np.median(car)), "std_error": se,
                        "ci_low": m - q * se if se else None, "ci_high": m + q * se if se else None, "t_stat": t,
                        "p_value": p, "mean_bhar": float(x["bhar"].mean() * 100), "share_negative": float((car < 0).mean()),
                        "first_event": pd.Timestamp(x["date"].min()).date(), "last_event": pd.Timestamp(x["date"].max()).date(),
                        "benchmark": bench, "computed_at": now})
    return out


def _issuer_meta(settings: Settings) -> pd.DataFrame:
    con = C.lake_con(["securities_master", "issuers"], settings)
    return con.execute("""
        SELECT i.issuer_id, i.country, i.sector_id, s.security_id
        FROM issuers i JOIN securities_master s ON s.issuer_id = i.issuer_id
        QUALIFY row_number() OVER (PARTITION BY i.issuer_id ORDER BY s.is_primary_security DESC NULLS LAST,
                                   s.is_active DESC NULLS LAST, s.security_id) = 1""").df()


def _issuer_themes(dates: list[date], settings: Settings) -> pl.DataFrame:
    """Dominant article theme per (issuer label id -> issuer_id, date) from gdelt_article_labels."""
    from app.events_v2 import store  # noqa: PLC0415

    if not dates:
        return pl.DataFrame()
    a = store.read_range("gdelt_article_labels", min(dates), max(dates), settings,
                         columns=["date", "theme_mask", "issuer_ids"])
    a = a.filter(pl.col("date").is_in(dates) & (pl.col("issuer_ids").list.len() > 0) & (pl.col("theme_mask") != 0))
    if a.is_empty():
        return pl.DataFrame()
    a = a.explode("issuer_ids").rename({"issuer_ids": "iid"})
    bits = [(pl.col("theme_mask") & (1 << b) != 0).cast(pl.Int32).alias(f"b{b}") for b in C.THEME_ID]
    g = a.with_columns(bits).group_by("iid", "date").agg([pl.col(f"b{b}").sum() for b in C.THEME_ID])
    cols = [f"b{b}" for b in C.THEME_ID]
    arr = g.select(cols).to_numpy()
    best = arr.argmax(axis=1)
    has = arr.max(axis=1) > 0
    th = [C.THEME_ID[int(cols[k][1:])] if h else None for k, h in zip(best, has)]
    from app.events_v2.dicts import load_dict  # noqa: PLC0415

    d = load_dict("issuer", settings)
    m = dict(zip(d["id"].to_list(), d["code"].to_list()))
    return pl.DataFrame({"label": [m.get(int(i)) for i in g["iid"].to_list()], "date": g["date"].to_list(), "dom_theme": th})


def run_library(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    info: dict[str, Any] = {}
    rng = C.t1_range(s)
    if rng is None:
        return {"rows": 0}
    px_start = rng[0] - timedelta(days=420)
    # ---- country index level
    ct = C.load_t1("country", settings=s)
    sh = shocks(ct.filter(pl.col("label").is_in(list(C.COUNTRY_INDEX))))
    info["window"] = C.window_desc(ct)
    if sh.height:
        px, used = C.country_closes(sorted(set(sh["label"])), px_start, None, s)
        R = _simple_returns(px)
        wb = C.world_benchmark(px_start, None, s)
        ev = sh.to_pandas().rename(columns={"label": "entity"})
        ev["iso2"] = ev["entity"]
        ev["sector"] = "ALL"
        ev["theme"] = ev["theme"].map(lambda b: "all" if b == -1 else C.THEME_ID.get(int(b), str(b)))
        per = _run_level("country_index", ev, R, _simple_returns(wb.to_frame("m"))["m"], "ETF:ACWI")
        if per:
            P = pd.concat(per)
            rows += _aggregate(P, "country_index", "ETF:ACWI")
            info["country_index_events"] = int(P[P["window"] == "0,5"].shape[0])
    # ---- sector ETF level
    st = C.load_t1("sector", settings=s)
    sh = shocks(st)
    if sh.height:
        px = C.load_closes(list(C.SECTOR_ETF.values()) + ["ETF:SPY"], px_start, None, s)
        R = _simple_returns(px.rename(columns={v: k for k, v in C.SECTOR_ETF.items()}))
        ev = sh.to_pandas().rename(columns={"label": "entity"})
        ev["iso2"], ev["sector"] = "ALL", ev["entity"]
        ev["theme"] = ev["theme"].map(lambda b: "all" if b == -1 else C.THEME_ID.get(int(b), str(b)))
        spy = R.pop("ETF:SPY") if "ETF:SPY" in R.columns else pd.Series(dtype=float)
        per = _run_level("sector_etf", ev, R, spy, "ETF:SPY")
        if per:
            P = pd.concat(per)
            rows += _aggregate(P, "sector_etf", "ETF:SPY")
            info["sector_etf_events"] = int(P[P["window"] == "0,5"].shape[0])
    # ---- issuer level
    it = C.load_t1("issuer", settings=s)
    sh = shocks(it, min_items=5)
    if sh.height:
        meta = _issuer_meta(s)
        ev = sh.to_pandas().merge(meta, left_on="label", right_on="issuer_id", how="inner")
        dom = _issuer_themes(sorted(set(sh["date"].to_list())), s)
        if dom.height:
            ev = ev.merge(dom.to_pandas(), on=["label", "date"], how="left")
        else:
            ev["dom_theme"] = None
        ev = ev.rename(columns={"security_id": "entity", "country": "iso2", "sector_id": "sector"})
        ev["sector"] = ev["sector"].fillna("UNKNOWN")
        ev["bench"] = ev["iso2"].map(lambda c: C.COUNTRY_INDEX.get(c, ["ETF:ACWI"])[0])
        px = C.load_closes(sorted(set(ev["entity"])), px_start, None, s)
        R = _simple_returns(px)
        bids = sorted(set(ev["bench"]) | {"ETF:ACWI"})
        bpx = _simple_returns(C.load_closes(bids, px_start, None, s))
        ev.loc[~ev["bench"].isin(bpx.columns), "bench"] = "ETF:ACWI"
        mk = {b: bpx[b] for b in set(ev["bench"]) if b in bpx.columns}
        ev_all = ev.assign(theme="all")
        ev_dom = ev[ev["dom_theme"].notna()].assign(theme=lambda d: d["dom_theme"])
        evx = pd.concat([ev_all, ev_dom])[["entity", "date", "kind", "theme", "iso2", "sector", "bench"]]
        per = _run_level("issuer", evx, R, mk, "country index")
        if per:
            P = pd.concat(per)
            rows += _aggregate(P, "issuer", "country index (ACWI fallback)")
            info["issuer_events"] = int(P[(P["window"] == "0,5") & (P["theme"] == "all")].shape[0])
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    n = C.write(df, "event_study_library", mode="overwrite", settings=s)
    top = []
    if n:
        top = df.filter((pl.col("window") == "0,5") & (pl.col("n_events") >= 5) & pl.col("p_value").is_not_null()) \
            .sort("p_value").head(10).select("shock_kind", "level", "theme", "iso2", "sector", "n_events", "mean_car",
                                             "p_value").to_dicts()
    rep = {"rows": n, **info, "strongest_0_5": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in top],
           "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_study_library", rep | {"at": C.now_utc().isoformat(timespec="seconds")}, s)
    return rep


def expected_reaction(lib: pl.DataFrame, theme: str, iso2: str | None = None, sector: str | None = None,
                      window: str = "0,5", shock_kind: str = "attention", level: str | None = None,
                      min_n: int = 3) -> dict[str, Any] | None:
    """Most specific library cell with >= min_n events, falling back (theme, iso2, sector) -> (theme, iso2) ->
    (theme, sector) -> theme; level preference issuer -> country_index -> sector_etf unless given."""
    want = {"theme": theme, "iso2": iso2 or "ALL", "sector": sector or "ALL"}
    levels = [level] if level else ["issuer", "country_index", "sector_etf"]
    base = lib.filter((pl.col("window") == window) & (pl.col("shock_kind") == shock_kind) & (pl.col("n_events") >= min_n))
    seen: set[tuple[str, ...]] = set()
    for keys in FALLBACKS:
        keys = tuple(k for k in keys if not ((k == "iso2" and not iso2) or (k == "sector" and not sector)))
        if keys in seen:
            continue
        seen.add(keys)
        cond = pl.lit(True)
        for k in ("theme", "iso2", "sector"):
            cond = cond & (pl.col(k) == (want[k] if k in keys else "ALL"))
        hit = base.filter(cond)
        for lv in levels:
            h = hit.filter(pl.col("level") == lv)
            if h.height:
                r = h.row(0, named=True)
                return r | {"match": "+".join(keys)}
    return None


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_library(ctx.settings)
    return JobResult(status="success", rows_written={"event_study_library": rep.get("rows", 0)}, message=str(rep)[:2000])
