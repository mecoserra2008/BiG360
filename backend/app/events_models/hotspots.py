"""B1 job `event_hotspots`: Moran's I, LISA, Getis-Ord Gi* and emerging-hotspot classes by country x theme x week.

Incremental: a week is recomputed when the fingerprint of its T1 country input changed (newest-first backfills land
older weeks later; the open current week changes every day). params: full (recompute all weeks), weeks (cap on
weeks per run, newest first, default 400), w_kind (contiguity), perms (499), max_minutes (time budget, default 25).

Refresh-pipeline fixes 2026-10-26: a back-fill (GDELT E6 / redo days + the events_agg full z recompute they trigger)
changes the fingerprint of hundreds of weeks at once; the nightly run then tried up to 400 weeks x themes x measures
x 499 permutations and hit the 60-min timeout (as a thread job, its process pool kept running). Now the run stops
submitting weeks once `max_minutes` is spent (newest weeks first; the rest follow on the next runs), writes what is
done, and the job runs on the process executor (a timeout kills the whole pool).
"""

from __future__ import annotations

import functools
import time
import warnings
from datetime import date
from typing import Any

import numpy as np
import polars as pl
from scipy import stats

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table
from app.events_models import common as C
from app.events_models.weights import get_weights
from app.modeling._spatial import gi_star, lisa_perm, moran_perm  # noqa: F401 (gi_star re-exported)

log = get_logger(__name__)

MIN_ITEMS_PER_DAY = 20
MIN_UNITS = 10
MIN_THEME_ITEMS = 5
MEASURES = ("attention", "tone", "intensity")


# ------------------------------------------------------------------------------------------------ statistics


def mann_kendall(x: np.ndarray) -> tuple[float | None, float | None]:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 4:
        return None, None
    d = np.sign(x[None, :] - x[:, None])
    s = float(np.triu(d, 1).sum())
    var = n * (n - 1) * (2 * n + 5) / 18
    z = (s - np.sign(s)) / np.sqrt(var) if s != 0 else 0.0
    tau = s / (n * (n - 1) / 2)
    return tau, float(2 * stats.norm.sf(abs(z)))


def emerging_class(z: np.ndarray, p: np.ndarray) -> tuple[str, float | None, float | None]:
    """ESRI-style emerging hotspot class from a location's Gi* (z, p) history (oldest -> newest)."""
    n = len(z)
    if n < 2:
        return "none", None, None
    tau, mkp = mann_kendall(z)
    for side, sgn in (("hot", 1), ("cold", -1)):
        hot = (p < 0.10) & (np.sign(z) == sgn)
        opp = (p < 0.10) & (np.sign(z) == -sgn)
        share = hot.mean()
        if hot[-1]:
            if hot.sum() == 1:
                return f"new_{side}", tau, mkp
            if share >= 0.9:
                if mkp is not None and mkp < 0.05 and tau is not None and np.sign(tau) == sgn:
                    return f"intensifying_{side}", tau, mkp
                if mkp is not None and mkp < 0.05 and tau is not None and np.sign(tau) == -sgn:
                    return f"diminishing_{side}", tau, mkp
                return f"persistent_{side}", tau, mkp
            first = int(np.argmax(hot))
            if hot[first:].all():
                return f"consecutive_{side}", tau, mkp
            if opp[:-1].any():
                return f"oscillating_{side}", tau, mkp
            return f"sporadic_{side}", tau, mkp
        if share >= 0.9 * (n - 1) / n and hot[:-1].mean() >= 0.9:
            return f"historical_{side}", tau, mkp
    return "none", tau, mkp


@functools.lru_cache(maxsize=512)
def _W(kind: str, codes: tuple[str, ...]) -> tuple[tuple[str, ...], np.ndarray, str]:
    have, W, meta = get_weights(kind, list(codes))
    return tuple(have), W, meta["source"]


def cross_section(vals: dict[str, float], kind: str, perms: int, seed: int = 0) -> dict[str, Any] | None:
    # numpy re-implementation of esda's Moran / Moran_Local permutation inference (app.modeling._spatial; matches
    # esda to Monte-Carlo error) - esda's numba kernels cost ~70 s of JIT compile in every new process
    codes = tuple(sorted(vals))
    have, W, src = _W(kind, codes)
    if len(have) < MIN_UNITS:
        return None
    x = np.array([vals[c] for c in have], float)
    if np.nanstd(x) <= 1e-12:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mg = moran_perm(x, W, perms, seed)
        ml = lisa_perm(x, W, perms, seed)
    gz, gp = gi_star(x, W)
    q = {1: "HH", 2: "LH", 3: "LL", 4: "HL"}
    return {"codes": list(have), "x": x, "moran": (mg["I"], mg["p_sim"], mg["z_sim"]),
            "lisa_i": ml["Is"], "lisa_p": ml["p_sim"], "lisa_q": [q.get(int(v), "ns") if pv < 0.05 else "ns"
                                                                  for v, pv in zip(ml["q"], ml["p_sim"])],
            "gi_z": gz, "gi_p": gp, "w_source": src}


def _bin(z: float, p: float) -> str:
    if not np.isfinite(z) or p >= 0.10:
        return "ns"
    lvl = "99" if p < 0.01 else "95" if p < 0.05 else "90"
    return ("hot" if z > 0 else "cold") + lvl


# ------------------------------------------------------------------------------------------------ weekly inputs


def weekly_frame(t1: pl.DataFrame) -> pl.DataFrame:
    """(week, iso2, theme, measure, value, n_items) from T1 country rows."""
    d = t1.filter(pl.col("label") != "GLOBAL").with_columns(
        C.week_start(pl.col("date")).alias("week"), (pl.col("ev_n") + pl.col("art_n")).alias("items"))
    allt = d.filter(pl.col("theme") == -1).group_by("week", "label").agg(
        pl.col("items").sum().alias("tot"), pl.col("date").n_unique().alias("ndays"),
        pl.col("ev_conflict_n").sum().alias("conf"), pl.col("ev_n").sum().alias("evs"))
    allt = allt.filter(pl.col("tot") >= MIN_ITEMS_PER_DAY * pl.col("ndays"))
    g = d.join(allt.select("week", "label", "tot", "conf", "evs"), on=["week", "label"], how="inner") \
        .group_by("week", "label", "theme").agg(
            pl.col("items").sum().alias("n_items"), pl.col("tot").first(), pl.col("conf").first(), pl.col("evs").first(),
            pl.col("attention_z").mean().alias("attention"),
            pl.coalesce("art_tone", "ev_tone").mean().alias("tone"))
    g = g.with_columns(pl.when(pl.col("theme") == -1).then(pl.col("conf") / pl.col("evs").clip(1))
                       .otherwise(pl.col("n_items") / pl.col("tot")).alias("intensity"))
    # theme cells with no rows for a qualifying country: intensity 0 (attention / tone stay missing)
    themes = g.select("theme").unique()
    full = allt.select("week", "label").join(themes, how="cross").join(g, on=["week", "label", "theme"], how="left") \
        .with_columns(pl.col("n_items").fill_null(0), pl.col("intensity").fill_null(0.0))
    full = full.with_columns(pl.when(pl.col("n_items") < MIN_THEME_ITEMS).then(None).otherwise(pl.col("attention")).alias("attention"),
                             pl.when(pl.col("n_items") < MIN_THEME_ITEMS).then(None).otherwise(pl.col("tone")).alias("tone"))
    long = full.unpivot(index=["week", "label", "theme", "n_items"], on=list(MEASURES), variable_name="measure",
                        value_name="value").filter(pl.col("value").is_not_null() & pl.col("value").is_finite())
    return long.rename({"label": "iso2"})


def week_hashes(t1: pl.DataFrame) -> dict[date, str]:
    h = t1.with_columns(C.week_start(pl.col("date")).alias("week")).group_by("week").agg(
        pl.len().alias("n"), (pl.col("ev_n") + pl.col("art_n")).sum().alias("s"),
        pl.col("attention_z").fill_null(0).sum().round(3).alias("z"), pl.col("date").n_unique().alias("d"))
    return {r["week"]: f"{r['n']}:{r['s']}:{r['z']}:{r['d']}" for r in h.iter_rows(named=True)}


# ------------------------------------------------------------------------------------------------ job


def _week_rows(args: tuple) -> list[dict[str, Any]]:
    w, sub, h, w_kind, perms, now = args
    rows: list[dict[str, Any]] = []
    for (theme, measure), g in sub.group_by(["theme", "measure"]):
        vals = dict(zip(g["iso2"].to_list(), g["value"].to_list()))
        nit = dict(zip(g["iso2"].to_list(), g["n_items"].to_list()))
        if len(vals) < MIN_UNITS:
            continue
        cs = cross_section(vals, w_kind, perms)
        if cs is None:
            continue
        mi, mp, mz = cs["moran"]
        tid = "all" if theme == -1 else C.THEME_ID.get(int(theme), str(theme))
        for i, c in enumerate(cs["codes"]):
            rows.append({"year": w.year, "week": w, "theme": tid, "measure": measure, "iso2": c,
                         "value": float(cs["x"][i]), "n_items": int(nit.get(c, 0)), "lisa_i": float(cs["lisa_i"][i]),
                         "lisa_p": float(cs["lisa_p"][i]), "lisa_quadrant": cs["lisa_q"][i],
                         "gi_z": float(cs["gi_z"][i]), "gi_p": float(cs["gi_p"][i]),
                         "hotspot": _bin(float(cs["gi_z"][i]), float(cs["gi_p"][i])), "emerging_class": None,
                         "n_weeks_hist": 0, "mk_tau": None, "mk_p": None, "moran_i": mi, "moran_p": mp,
                         "moran_z": mz, "n_units": len(cs["codes"]), "w_kind": w_kind, "w_source": cs["w_source"],
                         "input_hash": h, "computed_at": now})
    return rows


def run_hotspots(settings: Settings | None = None, full: bool = False, max_weeks: int = 400, w_kind: str = "contiguity",
                 perms: int = 499, max_minutes: float = 25.0) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    t1 = C.load_t1("country", settings=s)
    if t1.is_empty():
        return {"rows": 0, "weeks": 0, "note": "no T1 data"}
    hashes = week_hashes(t1)
    try:
        src_now = get_weights(w_kind, ["US", "CA", "MX", "DE", "FR"])[2]["source"]   # recompute when W source changes
        old = read_table("event_hotspots", columns=["week", "input_hash", "w_kind", "w_source"], settings=s).unique()
        done = {(r["week"], r["input_hash"]) for r in old.filter((pl.col("w_kind") == w_kind) &
                                                                 (pl.col("w_source") == src_now)).iter_rows(named=True)}
    except Exception:  # noqa: BLE001 - table not created yet
        old, done = None, set()
    todo = sorted([w for w, h in hashes.items() if full or (w, h) not in done], reverse=True)[:max_weeks]
    if not todo:
        return {"rows": 0, "weeks": 0, "window": C.window_desc(t1), "note": "up to date"}
    wk = weekly_frame(t1.filter(C.week_start(pl.col("date")).is_in(todo)))
    now = C.now_utc()
    parts = [(w, wk.filter(pl.col("week") == w), hashes[w], w_kind, perms, now) for w in todo]
    rows: list[dict[str, Any]] = []
    deadline = time.monotonic() + max(0.5, float(max_minutes)) * 60.0
    done_weeks = 0
    if len(parts) >= 4:
        import concurrent.futures as cf  # noqa: PLC0415
        import os  # noqa: PLC0415

        for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(k, "1")
        from app.ops.ram_budget import pool_workers, small_worker_threads  # noqa: PLC0415

        nw = pool_workers(max(1, min(8, (os.cpu_count() or 8) - 6, len(parts))), per_worker_gb=0.5)  # ram_fix
        with small_worker_threads(1), cf.ProcessPoolExecutor(max_workers=nw) as ex:
            # newest weeks first, a batch per worker round; stop submitting when the time budget is spent
            for i in range(0, len(parts), nw * 2):
                if time.monotonic() > deadline:
                    break
                for r in ex.map(_week_rows, parts[i:i + nw * 2]):
                    rows.extend(r)
                done_weeks = min(len(parts), i + nw * 2)
    else:
        for a in parts:
            if time.monotonic() > deadline:
                break
            rows.extend(_week_rows(a))
            done_weeks += 1
    left = len(parts) - done_weeks
    if left:
        log.info("event_hotspots: time budget (%.0f min) spent after %d of %d weeks; %d left for the next run",
                 max_minutes, done_weeks, len(parts), left)
    new = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if new.is_empty():
        return {"rows": 0, "weeks": done_weeks, "weeks_left": left, "window": C.window_desc(t1)}
    new = add_emerging(new, s, w_kind)
    n = C.write(new, "event_hotspots", settings=s)
    rep = {"rows": n, "weeks": done_weeks, "weeks_left": left, "window": C.window_desc(t1), "w_kind": w_kind,
           "w_source": new["w_source"][0], "seconds": round(time.perf_counter() - t0, 1)}
    C.update_state("event_hotspots", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def add_emerging(new: pl.DataFrame, settings: Settings, w_kind: str) -> pl.DataFrame:
    """Emerging class for the new rows using the stored Gi* history of earlier weeks plus the new weeks."""
    try:
        hist = read_table("event_hotspots", columns=["week", "theme", "measure", "iso2", "gi_z", "gi_p", "w_kind"],
                          settings=settings).filter(pl.col("w_kind") == w_kind).drop("w_kind")
    except Exception:  # noqa: BLE001
        hist = pl.DataFrame()
    cur = new.select("week", "theme", "measure", "iso2", "gi_z", "gi_p")
    if hist.height:
        hist = hist.join(cur.select("week").unique(), on="week", how="anti")
        allh = pl.concat([hist.select(cur.columns).cast(cur.schema), cur])
    else:
        allh = cur
    allh = allh.sort("theme", "measure", "iso2", "week")
    targets = set(new["week"].unique().to_list())
    out: dict[tuple, tuple] = {}
    for key, g in allh.group_by(["theme", "measure", "iso2"]):
        wks = g["week"].to_list()
        z, p = g["gi_z"].to_numpy(), g["gi_p"].to_numpy()
        for k, wd in enumerate(wks):
            if wd in targets:
                cls, tau, mkp = emerging_class(z[: k + 1], p[: k + 1])
                out[(wd, *key)] = (cls, k + 1, tau, mkp)
    keys = list(out)
    em = pl.DataFrame({"week": [k[0] for k in keys], "theme": [k[1] for k in keys], "measure": [k[2] for k in keys],
                       "iso2": [k[3] for k in keys], "emerging_class": [out[k][0] for k in keys],
                       "n_weeks_hist": [out[k][1] for k in keys], "mk_tau": [out[k][2] for k in keys],
                       "mk_p": [out[k][3] for k in keys]},
                      schema={"week": pl.Date, "theme": pl.Utf8, "measure": pl.Utf8, "iso2": pl.Utf8,
                              "emerging_class": pl.Utf8, "n_weeks_hist": pl.Int64, "mk_tau": pl.Float64, "mk_p": pl.Float64})
    return new.drop("emerging_class", "n_weeks_hist", "mk_tau", "mk_p").join(
        em, on=["week", "theme", "measure", "iso2"], how="left")


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_hotspots(ctx.settings, full=str(p.get("full", "false")).lower() in ("1", "true", "yes"),
                       max_weeks=int(p.get("weeks", 400)), w_kind=str(p.get("w_kind", "contiguity")),
                       perms=int(p.get("perms", 499)), max_minutes=float(p.get("max_minutes", 25)))
    return JobResult(status="success", rows_written={"event_hotspots": rep.get("rows", 0)}, message=str(rep))
