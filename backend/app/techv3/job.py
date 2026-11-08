"""Jobs of Technicals v3 #1 / #5 (docs/TECHNICALS_V3_IDEAS.md; gate docs/TECHNICALS_V3_VALIDATION.md).

`tech_factors` (nightly, after prices_finalize), function `run_factors`:
  per price region, a ~4.3-year window of daily bars (residual momentum needs 36 + 12 months), every factor of the
  sessions after the last stored one (the last 5 sessions are always recomputed: late prints), percentiles within
  region x sector x date among the liquid names, the composite of the gate-passing factors -> tech_factors (upsert per
  region x year). params mode=seed imports the research build (scratchpad/tech_v3_b15/wf, 2004 ->) for the gate
  regions and computes the other regions' window; mode=full recomputes every region's window.
`tech_breakouts` (nightly), function `run_breakouts`:
  per region, a ~2.6-year window: breakout events of the last 80 sessions with features, labels (open until their
  window completes) and the calibrated P(follow-through) of the latest fit -> tech_breakouts (upsert);
  the latest breakout state per liquid security -> tech_breakout_state (latest + month-end snapshots).
  params mode=seed imports the research events (labels + walk-forward OOS probabilities where available).
`tech_factor_eval` (monthly), function `run_eval`:
  validation rows (data/reports/technicals_v3/eval_rows.parquet, written by scripts/technicals_v3/report.py) + a
  rolling monitor (trailing 156 weekly origins: rank IC of every factor on the live history) -> tech_factor_eval;
  refits the P(follow-through) model on the complete events of tech_breakouts -> data/models/technicals_v3/breakouts.

    python -m app.techv3.job factors|breakouts|eval [--mode seed|incremental|full] [--as-of YYYY-MM-DD] [--regions US,EU]
"""

from __future__ import annotations

import argparse
import json
import pickle
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.techv3 import bo_model as BM
from app.techv3 import build as BL
from app.techv3 import factors as F
from app.techv3 import panel as P
from app.techv3 import state as ST

log = get_logger("app.techv3.job")

FACTOR_WINDOW_DAYS = 1580
BREAKOUT_WINDOW_DAYS = 950
BREAKOUT_RECENT = 80
STATE_SESSIONS = 20
GATE_REGIONS = P.GATE_REGIONS
OUT_COLS = ["as_of_date", "security_id", "region", "year", "sector_id", *F.FACTORS, *BL.PCT_COLS, "composite", "composite_pct",
            "adv20_usd", "liquid", "size_bucket", "n_peers", "pct_scope", "computed_at"]
OUTCOME = {0: "success", 1: "fail", 2: "neither", 3: "open"}


def wf_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.project_root / "scratchpad" / "tech_v3_b15" / "wf"


def report_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "reports" / "technicals_v3"
    p.mkdir(parents=True, exist_ok=True)
    return p


def model_root(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "models" / "technicals_v3" / "breakouts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_gate(settings: Settings | None = None) -> dict[str, Any]:
    p = report_dir(settings) / "gate.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def composite_members(gate: dict[str, Any]) -> list[str]:
    """Factors of the live composite: the gate's fixed ex-ante member list, only when the composite PASSED."""
    c = gate.get("composite") or {}
    if c.get("verdict") != "pass":
        return []
    return [f for f in (c.get("members") or []) if f in F.FACTORS]


def _scan(table: str) -> pl.LazyFrame | None:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        lf = scan_table(table)
        lf.collect_schema()
        return lf
    except Exception as e:  # noqa: BLE001
        log.info("%s unavailable: %s", table, e)
        return None


def _regions(con: Any) -> list[str]:
    try:
        rs = [r[0] for r in con.execute("SELECT DISTINCT region FROM prices_daily WHERE region <> 'FX'").fetchall()]
    except Exception:  # noqa: BLE001
        rs = list(P.ALL_REGIONS)
    order = {r: i for i, r in enumerate(P.ALL_REGIONS)}
    return sorted([r for r in rs if r], key=lambda r: order.get(r, 99))


# ------------------------------------------------------------------------------------------------ factors


def finish_factors(df: pl.DataFrame, region: str, members: list[str]) -> pl.DataFrame:
    """Percentiles (if missing), size bucket, composite + its percentile, contract column names."""
    if df.is_empty():
        return df
    if "pct_hi52" not in df.columns:
        df = BL.add_percentiles(df, region)
    if "size_bucket" not in df.columns:
        df = df.with_columns(BL.size_bucket_expr().alias("size_bucket"))
    df = BL.composite(df, members)
    if members:
        d = df["date"].to_numpy().astype("datetime64[D]").astype(np.int64)
        sec = df["sector_id"].fill_null("NA").to_numpy().astype(str)
        _u, code = np.unique(sec, return_inverse=True)
        key = np.where(df["pct_scope"].to_numpy().astype(str) == "sector", d * 1000 + code, d * 1000 + 999)
        pc, _n = BL.group_percentiles(key, df["composite"].cast(pl.Float64).fill_null(np.nan).to_numpy(),
                                      df["liquid"].fill_null(False).to_numpy())
        df = df.with_columns(pl.Series("composite_pct", pc.astype(np.float32)).fill_nan(None))
    else:
        df = df.with_columns(pl.lit(None, pl.Float32).alias("composite_pct"))
    df = df.rename({"date": "as_of_date", "adv20": "adv20_usd"}).with_columns(
        pl.lit(region).alias("region"), pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"),
        pl.lit(datetime.now(UTC)).alias("computed_at"))
    return df.select([c for c in OUT_COLS if c in df.columns])


def factors_window(con: Any, region: str, as_of: date, out_start: date | None, members: list[str],
                   settings: Settings, check: Any = None, chunk: int = BL.CHUNK) -> pl.DataFrame:
    start = as_of - timedelta(days=FACTOR_WINDOW_DAYS)
    secs = P.region_securities(con, region, start, as_of, min_bars=60)
    if secs.is_empty():
        return pl.DataFrame()
    cal = P.region_calendar(con, region, start, as_of)
    if len(cal) < 300:
        return pl.DataFrame()
    mkt = BL.market_pass(con, region, secs, cal, start, as_of, chunk=chunk, check=check)
    out_from = int(np.searchsorted(cal, np.datetime64(out_start, "D"))) if out_start else len(cal) - 5
    out_from = min(max(out_from, 0), max(len(cal) - 5, 0))
    keep = P.universe_members(("mvp_us", "mvp_global"), settings)
    ids = secs["security_id"].to_list()
    sec_of = dict(zip(ids, secs["sector_id"].to_list(), strict=True))
    frames = []
    for c0 in range(0, len(ids), chunk):
        if check:
            check()
        part = ids[c0:c0 + chunk]
        ch = P.load_chunk(con, region, part, cal, start, as_of)
        frames.append(BL.factor_chunk(ch, [sec_of[x] for x in part], mkt, out_from, labels=False, keep_all=keep))
    df = pl.concat([f for f in frames if f.height], how="diagonal_relaxed") if frames else pl.DataFrame()
    return finish_factors(df, region, members)


def _last_date(table: str, region: str, col: str = "as_of_date") -> date | None:
    lf = _scan(table)
    if lf is None:
        return None
    try:
        return lf.filter(pl.col("region") == region).select(pl.col(col).max()).collect().item()
    except Exception:  # noqa: BLE001
        return None


def seed_factors(region: str, members: list[str], settings: Settings) -> int:
    """Import the research build (daily 2004 -> its end) of a gate region into tech_factors, year by year."""
    from app.core.storage import write_table  # noqa: PLC0415

    n = 0
    for f in sorted((wf_dir(settings) / region).glob("f_[0-9][0-9][0-9][0-9].parquet")):
        df = pl.read_parquet(f).drop([c for c in ("fwd_5", "fwd_20", "fwd_60", "adv60", "close_usd") if c in pl.read_parquet_schema(f)])
        df = finish_factors(df, region, members)
        n += write_table(df, "tech_factors", mode="overwrite_partition", settings=settings)
        del df
    return n


def run_factors_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_factors(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = rep.get("rows", 0) > 0
    return JobResult(status="success" if ok and not rep.get("failed") else ("partial" if ok else "failed"),
                     rows_written={"tech_factors": rep.get("rows", 0)}, entities_ok=rep.get("regions_ok", 0),
                     entities_failed=len(rep.get("failed") or []), watermark=rep.get("as_of"),
                     message=json.dumps(rep, default=str)[:4000])


def run_factors(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    mode = str(params.get("mode", "incremental"))
    gate = load_gate(s)
    members = composite_members(gate)
    con = P.lake(["prices_daily", "securities_master", "issuers"], s, mem="900MB", threads=2)
    rep: dict[str, Any] = {"as_of": str(as_of), "mode": mode, "composite_members": members, "regions": {}, "failed": [], "rows": 0}
    try:
        regions = list(params.get("regions") or _regions(con))
        for rg in regions:
            try:
                if cancel:
                    cancel()
                if mode == "seed" and rg in GATE_REGIONS and (wf_dir(s) / rg).exists():
                    n = seed_factors(rg, members, s)
                    last = _last_date("tech_factors", rg)
                    df = factors_window(con, rg, as_of, (last + timedelta(days=1)) if last else None, members, s, cancel)
                    n += write_table(df, "tech_factors", mode="upsert", settings=s) if df.height else 0
                else:
                    last = None if mode == "full" else _last_date("tech_factors", rg)
                    out_start = (last + timedelta(days=1)) if last else (as_of - timedelta(days=14))
                    if mode == "full":
                        out_start = as_of - timedelta(days=int(params.get("days", 30)))
                    df = factors_window(con, rg, as_of, out_start, members, s, cancel)
                    n = write_table(df, "tech_factors", mode="upsert", settings=s) if df.height else 0
                rep["regions"][rg] = n
                rep["rows"] += n
            except Exception as e:
                if e.__class__.__name__ == "JobCancelled":
                    raise
                log.warning("tech_factors %s failed: %s", rg, e)
                rep["failed"].append(rg)
    finally:
        con.close()
    rep["regions_ok"] = len(rep["regions"])
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"tech_factors": rep})
    return rep


# ------------------------------------------------------------------------------------------------ breakouts


def latest_model(settings: Settings | None = None) -> dict[str, Any] | None:
    root = model_root(settings)
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "bo_model.pkl").exists())
    if not dirs:
        return None
    with open(dirs[-1] / "bo_model.pkl", "rb") as f:
        return pickle.load(f)  # noqa: S301 - our own artifact


def predict_events(ev: pl.DataFrame, model: dict[str, Any] | None) -> pl.DataFrame:
    if ev.is_empty():
        return ev
    cols: dict[str, Any] = {}
    for H in (10, 20):
        m = (model or {}).get(f"full_{H}")
        if m is not None:
            cols[f"p_follow_{H}"] = m.predict(BM.matrix(BM.design(ev, H), m.cols)).astype(np.float32)
    g = (model or {}).get("geo_20")
    if g is not None:
        cols["p_geo_20"] = g.predict(BM.matrix(BM.design(ev, 20), g.cols)).astype(np.float32)
    out = ev.with_columns([pl.Series(k, v) for k, v in cols.items()])
    for k in ("p_follow_10", "p_follow_20", "p_geo_20"):
        if k not in out.columns:
            out = out.with_columns(pl.lit(None, pl.Float32).alias(k))
    return out


def to_contract(ev: pl.DataFrame, region: str, fit: str | None) -> pl.DataFrame:
    if ev.is_empty():
        return ev
    ren = {"N": "lookback", "adv20": "adv20_usd"}
    out = ev.rename({k: v for k, v in ren.items() if k in ev.columns})
    out = out.with_columns(pl.lit(region).alias("region"), pl.lit(fit).alias("model_fit"),
                           pl.col("date").dt.year().cast(pl.Int16).alias("year"),
                           pl.lit(datetime.now(UTC)).alias("computed_at"),
                           *[pl.col(f"out_{H}").cast(pl.Int8).replace_strict(OUTCOME, return_dtype=pl.Utf8).alias(f"out_{H}")
                             for H in (10, 20) if f"out_{H}" in out.columns])
    from app.core.schema import get_contract  # noqa: PLC0415

    keep = [c for c in get_contract("tech_breakouts").column_names if c in out.columns]
    return out.select(keep)


def state_rows(ev: pl.DataFrame, last_close: dict[str, float], as_of: date, cal: np.ndarray, gate_bo: str,
               base_rates: dict[str, float]) -> list[dict[str, Any]]:
    """Latest breakout of the last STATE_SESSIONS sessions per security -> state (from its open / resolved label)."""
    if ev.is_empty():
        return []
    n_cal = len(cal)
    pos = {d: i for i, d in enumerate(cal.astype("datetime64[D]").astype(object))}
    rows = []
    recent = ev.filter(pl.col("date") >= pl.lit(cal[max(0, n_cal - STATE_SESSIONS)].astype("datetime64[D]").astype(object)))
    if recent.is_empty():
        return []
    recent = recent.sort(["security_id", "date", "N"]).group_by("security_id").agg(pl.all().last())
    last_session = cal[-1].astype("datetime64[D]").astype(object)
    as_of = min(as_of, last_session)              # the state is "as of the region's last session"
    for r in recent.iter_rows(named=True):
        code = int(r["out_20"]) if r.get("out_20") is not None else 3
        state = {0: "followed_through", 1: "failed", 3: "active"}.get(code, "none")
        k = pos.get(r["date"])
        rows.append({"as_of_date": as_of, "security_id": r["security_id"], "state": state, "direction": int(r["direction"]),
                     "lookback": int(r["N"]), "event_date": r["date"],
                     "sessions_since": (n_cal - 1 - k) if k is not None else None, "level": r["level"],
                     "target_20": r.get("target_20"), "last_close": last_close.get(r["security_id"]),
                     "confirmed": bool(r["confirmed"]), "confirm_score": r.get("confirm_score"), "relvol50": r.get("relvol50"),
                     "clv": r.get("clv"), "p_follow_20": r.get("p_follow_20"), "p_geo_20": r.get("p_geo_20"),
                     "base_rate_20": base_rates.get(f"{int(r['N'])}:{int(r['direction'])}"), "gate": gate_bo,
                     "computed_at": datetime.now(UTC)})
    return rows


def run_breakouts_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_breakouts(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = rep.get("events", 0) > 0 or rep.get("state_rows", 0) > 0
    return JobResult(status="success" if ok and not rep.get("failed") else ("partial" if ok else "failed"),
                     rows_written={"tech_breakouts": rep.get("events", 0), "tech_breakout_state": rep.get("state_rows", 0)},
                     entities_ok=len(rep.get("regions") or {}), entities_failed=len(rep.get("failed") or []),
                     watermark=rep.get("as_of"), message=json.dumps(rep, default=str)[:4000])


def gate_status(gate: dict[str, Any], key: str) -> str:
    g = ((gate.get("breakouts") or {}).get(key)) or {}
    return "production" if g.get("verdict") == "pass" else "experimental"


def run_breakouts(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None) -> dict[str, Any]:
    from app.core.storage import read_table, write_table  # noqa: PLC0415
    from app.technicals.levels import month_end_retention  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    mode = str(params.get("mode", "incremental"))
    gate = load_gate(s)
    g_bo = gate_status(gate, "p_follow_20")
    base_rates = ((gate.get("breakouts") or {}).get("base_rates")) or {}
    model = latest_model(s)
    fit = (model or {}).get("fit_date")
    rep: dict[str, Any] = {"as_of": str(as_of), "mode": mode, "model_fit": fit, "regions": {}, "failed": [], "events": 0,
                           "state_rows": 0}
    if mode == "seed":
        rep["seed"] = seed_breakouts(s, model)
        rep["events"] += sum(rep["seed"].values())
    con = P.lake(["prices_daily", "securities_master", "issuers"], s, mem="900MB", threads=2)
    states: list[dict[str, Any]] = []
    try:
        regions = list(params.get("regions") or _regions(con))
        for rg in regions:
            try:
                if cancel:
                    cancel()
                start = as_of - timedelta(days=BREAKOUT_WINDOW_DAYS)
                secs = P.region_securities(con, rg, start, as_of, min_bars=120)
                cal = P.region_calendar(con, rg, start, as_of)
                if secs.is_empty() or len(cal) < 300:
                    continue
                ids = secs["security_id"].to_list()
                sec_of = dict(zip(ids, secs["sector_id"].to_list(), strict=True))
                out_from = max(0, len(cal) - BREAKOUT_RECENT)
                frames, lc = [], {}
                for c0 in range(0, len(ids), BL.CHUNK):
                    part = ids[c0:c0 + BL.CHUNK]
                    ch = P.load_chunk(con, rg, part, cal, start, as_of)
                    e, _c = BL.breakout_chunk(ch, out_from, controls=False)
                    cf = F.ffill(ch.C)[-1]
                    lc.update({sid: float(v) for sid, v in zip(part, cf, strict=True) if np.isfinite(v)})
                    if e.height:
                        frames.append(e.filter(pl.col("liquid")))
                ev = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
                if ev.height:
                    ev = ev.with_columns(pl.Series("sector_id", [sec_of.get(x) for x in ev["security_id"].to_list()]),
                                         pl.lit(rg).alias("region"))
                    ev = predict_events(ev, model)
                    rows = to_contract(ev, rg, fit)
                    rep["events"] += write_table(rows, "tech_breakouts", mode="upsert", settings=s)
                    states += state_rows(ev, lc, as_of, cal, g_bo, base_rates)
                rep["regions"][rg] = ev.height
            except Exception as e:
                if e.__class__.__name__ == "JobCancelled":
                    raise
                log.warning("tech_breakouts %s failed: %s", rg, e)
                rep["failed"].append(rg)
    finally:
        con.close()
    if states:
        df = pl.DataFrame(states, infer_schema_length=None)
        try:
            keep = month_end_retention(read_table("tech_breakout_state"), as_of)
        except Exception:  # noqa: BLE001
            keep = pl.DataFrame()
        if not keep.is_empty():
            df = pl.concat([keep.filter(pl.col("as_of_date") != as_of), df], how="diagonal_relaxed")
        rep["state_rows"] = write_table(df, "tech_breakout_state", mode="overwrite", settings=s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"tech_breakouts": rep})
    return rep


def seed_breakouts(s: Settings, model: dict[str, Any] | None) -> dict[str, int]:
    """Import the research events (liquid; labels from the build) with the walk-forward OOS probabilities where the
    validation produced them (bo_oos.parquet), else the latest fit's predictions."""
    from app.core.storage import write_table  # noqa: PLC0415

    out: dict[str, int] = {}
    oos_p = report_dir(s) / "bo_oos.parquet"
    oos = pl.read_parquet(oos_p) if oos_p.exists() else pl.DataFrame()
    for rg in GATE_REGIONS:
        files = sorted((wf_dir(s) / rg).glob("bo_c*.parquet"))
        if not files:
            continue
        ev = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed").filter(pl.col("liquid"))
        ev = ev.with_columns(pl.lit(rg).alias("region"))
        ev = predict_events(ev, model)
        if oos.height:
            for H in (10, 20):
                o = oos.filter((pl.col("H") == H) & (pl.col("region") == rg)).select(
                    pl.col("security_id").cast(pl.Utf8), "date", "N", "direction", pl.col("p_full").cast(pl.Float32).alias("_p"),
                    *([pl.col("p_geo").cast(pl.Float32).alias("_g")] if H == 20 else []))
                ev = ev.join(o, on=["security_id", "date", "N", "direction"], how="left").with_columns(
                    pl.coalesce("_p", f"p_follow_{H}").alias(f"p_follow_{H}"))
                if H == 20:
                    ev = ev.with_columns(pl.coalesce("_g", "p_geo_20").alias("p_geo_20")).drop("_g")
                ev = ev.drop("_p")
        n = 0
        for (yy,), part in ev.with_columns(pl.col("date").dt.year().alias("_y")).partition_by("_y", as_dict=True).items():
            n += write_table(to_contract(part.drop("_y"), rg, (model or {}).get("fit_date") or "walk_forward"), "tech_breakouts",
                             mode="overwrite_partition", settings=s)
            _ = yy
        out[rg] = n
    return out


# ------------------------------------------------------------------------------------------------ monthly eval + fit


def fit_breakout_model(events: pl.DataFrame, fit_date: date, s: Settings) -> dict[str, Any]:
    """Production P(follow-through) fit: train on complete events < fit_date - 425 days, isotonic calibration on
    [fit_date - 365, fit_date - 60) (the walk-forward protocol of the validation, applied at the latest date)."""
    out: dict[str, Any] = {"fit_date": str(fit_date)}
    art: dict[str, Any] = {"fit_date": str(fit_date)}
    d_end = np.datetime64(fit_date, "D")
    for H in (10, 20):
        ev = events.filter(pl.col(f"complete_{H}") & pl.col("liquid")) if "liquid" in events.columns else events.filter(pl.col(f"complete_{H}"))
        if ev.height < 20000:
            out[f"H{H}"] = f"too few events ({ev.height})"
            continue
        D = BM.design(ev, H)
        y = (ev[f"out_{H}"] == 0).cast(pl.Float64).to_numpy()
        d = ev["date"].to_numpy().astype("datetime64[D]")
        tr = d < d_end - np.timedelta64(425, "D")
        ca = (d >= d_end - np.timedelta64(365, "D")) & (d < d_end - np.timedelta64(60, "D"))
        itr = np.nonzero(tr)[0]
        itr = itr[BM.subsample(len(itr), 900_000, seed=0)]
        ica = np.nonzero(ca)[0]
        Xf, Xg = BM.matrix(D, BM.FULL), BM.matrix(D, BM.GEOMETRY)
        art[f"full_{H}"] = BM.fit(Xf[itr], y[itr], Xf[ica], y[ica], BM.FULL)
        if H == 20:
            art["geo_20"] = BM.fit(Xg[itr], y[itr], Xg[ica], y[ica], BM.GEOMETRY)
        out[f"H{H}"] = {"train": int(len(itr)), "cal": int(len(ica)), "base_rate": float(y[itr].mean()),
                        "coefficients": BM.coefficients(art[f"full_{H}"])}
    if "full_20" in art:
        dd = model_root(s) / fit_date.isoformat()
        dd.mkdir(parents=True, exist_ok=True)
        with open(dd / "bo_model.pkl", "wb") as f:
            pickle.dump(art, f)
        (dd / "meta.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
        for p in sorted(x for x in model_root(s).iterdir() if x.is_dir())[:-6]:
            for x in p.iterdir():
                x.unlink()
            p.rmdir()
    return out


def run_eval_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_eval(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = rep.get("rows", 0) > 0
    return JobResult(status="success" if ok else "partial", rows_written={"tech_factor_eval": rep.get("rows", 0)},
                     watermark=rep.get("as_of"), message=json.dumps(rep, default=str)[:4000])


def monitor_rows(s: Settings, as_of: date, weeks: int = 156) -> list[dict[str, Any]]:
    """Rolling monitor: weekly rank IC (20-session forward return from the close of t + 1, sector-neutral) of every
    factor percentile on the stored live history of the gate regions over the last `weeks` weekly origins."""
    from app.techv3 import stats as S  # noqa: PLC0415

    lf = _scan("tech_factors")
    if lf is None:
        return []
    start = as_of - timedelta(days=7 * weeks + 60)
    rows: list[dict[str, Any]] = []
    con = P.lake(["prices_daily"], s, mem="700MB", threads=2)
    try:
        for rg in GATE_REGIONS:
            df = lf.filter((pl.col("region") == rg) & (pl.col("as_of_date") >= start) & pl.col("liquid")).select(
                "as_of_date", "security_id", "sector_id", "pct_scope", *BL.PCT_COLS).collect()
            if df.is_empty():
                continue
            dts = df["as_of_date"].unique().sort()
            wk = dts.to_frame().with_columns(pl.col("as_of_date").dt.strftime("%G-%V").alias("w")).group_by("w").agg(
                pl.col("as_of_date").max())["as_of_date"]
            df = df.filter(pl.col("as_of_date").is_in(wk.implode()))
            ids = df["security_id"].unique().to_list()
            con.register("_tv3m", pl.DataFrame({"security_id": ids}).to_arrow())
            px = con.execute("""SELECT security_id, date, close * coalesce(adj_factor, 1.0) AS c FROM prices_daily
                                WHERE region = ? AND date >= ? AND security_id IN (SELECT security_id FROM _tv3m)""",
                             [rg, start]).pl()
            con.unregister("_tv3m")
            px = px.with_columns(pl.col("date").cast(pl.Date)).sort("security_id", "date").with_columns(
                (pl.col("c").shift(-21).over("security_id").log() - pl.col("c").shift(-1).over("security_id").log()).alias("f20"))
            j = df.join(px.select(pl.col("date").alias("as_of_date"), "security_id", "f20"), on=["as_of_date", "security_id"], how="left")
            j = j.filter(pl.col("f20").is_not_null())
            if j.is_empty():
                continue
            j = j.with_columns((pl.col("f20") - pl.when(pl.col("pct_scope") == "sector").then(pl.col("f20").mean().over(["as_of_date", "sector_id"]))
                                .otherwise(pl.col("f20").mean().over("as_of_date"))).alias("y"),
                               (pl.col("f20") - pl.col("f20").mean().over("as_of_date")).alias("yr"))
            for f in F.FACTORS:
                ic = S.ic_series(j, f"pct_{f}", "yr" if f == "indmom" else "y", ["as_of_date"]).rename({"as_of_date": "date"})
                st = S.ic_summary(ic, 20, F.SIGN[f])
                rows.append({"eval_date": as_of, "eval_kind": "monitor", "signal": f, "horizon": 20, "region": rg,
                             "segment_type": "all", "segment": "trailing_156w", "n_dates": st.get("n_dates"),
                             "mean_names": st.get("mean_names"), "mean_ic": st.get("mean_ic"), "aligned_ic": st.get("aligned_ic"),
                             "t_stat": st.get("t_stat"), "p_value": st.get("p_value"), "share_aligned": st.get("share_aligned"),
                             "computed_at": datetime.now(UTC)})
    finally:
        con.close()
    return rows


def run_eval(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None) -> dict[str, Any]:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    rep: dict[str, Any] = {"as_of": str(as_of)}
    frames = []
    vp = report_dir(s) / "eval_rows.parquet"
    if vp.exists():
        frames.append(pl.read_parquet(vp).with_columns(pl.lit("validation").alias("eval_kind")))
    if not params.get("skip_monitor"):
        mon = monitor_rows(s, as_of)
        if mon:
            frames.append(pl.DataFrame(mon, infer_schema_length=None))
        rep["monitor_rows"] = len(mon)
    try:
        old = read_table("tech_factor_eval").filter(pl.col("eval_kind") == "monitor")
        keep = sorted(set(old["eval_date"].to_list()) - {as_of})[-11:]
        frames.append(old.filter(pl.col("eval_date").is_in(keep)))
    except Exception:  # noqa: BLE001 - first run
        pass
    if frames:
        df = pl.concat(frames, how="diagonal_relaxed").with_columns(pl.lit(datetime.now(UTC)).alias("computed_at"))
        rep["rows"] = write_table(df, "tech_factor_eval", mode="overwrite", settings=s)
    if not params.get("skip_fit"):
        if cancel:
            cancel()
        lf = _scan("tech_breakouts")
        if lf is not None:
            ev = lf.filter(pl.col("date") <= as_of).collect()
            if ev.height:
                ev = ev.rename({"lookback": "N"}).with_columns(
                    *[(pl.col(f"out_{H}") != "open").alias(f"complete_{H}") for H in (10, 20)],
                    *[pl.col(f"out_{H}").replace_strict({v: k for k, v in OUTCOME.items()}, default=3, return_dtype=pl.Int8)
                      .alias(f"out_{H}") for H in (10, 20)], pl.lit(True).alias("liquid"))
                rep["breakout_fit"] = fit_breakout_model(ev, as_of, s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"tech_factor_eval": rep})
    return rep


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["factors", "breakouts", "eval", "fit_seed"])
    ap.add_argument("--mode", default="incremental")
    ap.add_argument("--as-of")
    ap.add_argument("--regions")
    a = ap.parse_args()
    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    p: dict[str, Any] = {"mode": a.mode}
    if a.regions:
        p["regions"] = a.regions.split(",")
    if a.what == "factors":
        rep = run_factors(None, as_of, p)
    elif a.what == "breakouts":
        rep = run_breakouts(None, as_of, p)
    elif a.what == "fit_seed":
        s = get_settings()
        files = [f for rg in GATE_REGIONS for f in sorted((wf_dir(s) / rg).glob("bo_c*.parquet"))]
        ev = pl.concat([pl.read_parquet(f).with_columns(pl.lit(rg).alias("region")) for rg in GATE_REGIONS
                        for f in sorted((wf_dir(s) / rg).glob("bo_c*.parquet"))], how="diagonal_relaxed")
        _ = files
        rep = fit_breakout_model(ev, as_of or datetime.now(UTC).date(), s)
    else:
        rep = run_eval(None, as_of, p)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
