"""Backtesting lab: point-in-time panel builder (owner: backtest, roadmap v2 item #4).

Executes a normalized *plan* (built by `app.backtest.spec.make_plan`) against DuckDB views over the Parquet lake and
returns (panel, PricePanel, events, meta). Like the Model Studio core it imports nothing from `app` except the Studio
dataset engine, the formula engine and the backtest engine core, each with a fallback to the embedded copies, so
`app.backtest.export` can embed this file verbatim in a standalone script.

PIT rules (docs/ROADMAP_V2_PLAN.md item 4):
  * Studio-sourced signal / screen variables come from the Studio dataset engine (`_core.build`): event sources are
    bucketed at the rebalance frequency (last value <= bucket end), knowledge-dated sources (ratios, fundamentals)
    are sampled as of the bucket end on knowledge_date, universe membership is applied per date (survivorship-free
    where the membership history exists; it starts 2026-10-10, earlier dates use the initial member list).
  * Extra sources (ML OOS predictions, earnings PEAD, event exposure, Studio OOS predictions, cross-asset signals)
    are as-of joined on their knowledge / signal date with a maximum age.
  * Price-derived fields (momentum, volatility, ADV, trend) use daily prints <= the rebalance date only.
  * Macro regime states (macro_factors, monthly) are used for overlays with a one-month publication lag.
"""

from __future__ import annotations

import math
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
import polars as pl

try:  # backend
    from app.modeling import _core as _bp_core
except ImportError:  # embedded in an exported script: the Studio core is defined in the same module
    _bp_core = sys.modules[__name__]
try:
    from app.normalize.formula import compile_expr as _bp_compile_expr
    from app.normalize.formula import uses_panel_functions as _bp_uses_panel
except ImportError:
    _bp_compile_expr = globals().get("compile_expr")  # type: ignore[assignment]
    _bp_uses_panel = globals().get("uses_panel_functions")  # type: ignore[assignment]
try:
    from app.backtest import _engine as _bp_engine
except ImportError:
    _bp_engine = sys.modules[__name__]

PANEL_VERSION = "1.1"  # 1.1: sub-dollar ADV residue -> 0

#: price-derived fields computed from daily prints <= the rebalance date (trading-day windows per entity)
PRICE_FIELDS: dict[str, str] = {
    "mom_12_1": "12-1 month momentum: px[t-21] / px[t-252] - 1",
    "mom_6_1": "6-1 month momentum: px[t-21] / px[t-126] - 1",
    "ret_1m": "1-month return (21 trading days)", "ret_3m": "3-month return (63 days)",
    "ret_6m": "6-month return (126 days)", "ret_12m": "12-month return (252 days)",
    "vol_3m": "annualised volatility of daily returns, 63 days", "vol_1y": "annualised volatility, 252 days",
    "high_52w_proximity": "px / 252-day high", "trend_200d": "px / 200-day average - 1",
    "above_200d": "1 if px > 200-day average else 0", "adv_usd_20d": "20-day average dollar volume (USD)",
    "beta_1y": "252-day beta of daily returns to the benchmark",
}

EXTRA_SOURCES = ("ml", "pead", "event_exposure", "studio_run", "bql")


class PanelError(ValueError):
    """Unsatisfiable plan (maps to HTTP 422)."""


def _bp_q(v: Any) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _bp_d(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def plan_tables(plan: dict[str, Any]) -> list[str]:
    out: set[str] = {"prices_daily", "securities_master"}
    if plan["kind"] == "assets":
        out = {"cross_asset_signals"}
    if plan.get("dataset"):
        out.update(_bp_core.tables_needed(plan["dataset"]))
    for x in plan.get("extra") or []:
        out.update({"ml": ["ml_predictions", "model_runs"], "pead": ["earnings_pead"],
                    "event_exposure": ["company_event_exposure"], "studio_run": [], "bql": []}[x["source"]])
    if plan.get("regime") or plan.get("regime_breakdown", True):
        out.add("macro_factors")
    if plan.get("rf", "cash") == "cash":
        out.add("macro_factors")
    ev = plan.get("events") or {}
    trig = ev.get("trigger")
    out.update({"insider_cluster": ["insider_signals"], "earnings_surprise": ["earnings_pead"],
                "cftc_extreme": ["cross_asset_signals"], "macro_surprise": ["macro_surprises"],
                "confluence_touch": ["confluence_zones"], "signal_threshold": [],
                "calibrated_band_breakout": [],
                "peer_breakaway": ["conformal_peer_scores", "conformal_peer_members"],
                "meta_setups": ["tech_meta_setups", "tech_prob_setups"]}.get(trig, []))
    bench = plan.get("benchmark")
    if bench and bench not in ("universe_ew", "none") and plan["kind"] == "assets":
        out.update({"prices_daily"})
    return sorted(out)


def _bp_has(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    try:
        con.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchall()
        return True
    except duckdb.Error:
        return False


# ------------------------------------------------------------------------------------------------ securities panel


def _bp_build(norm: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None]) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Studio core build. In the app it goes through the one dataset cache shared with Studio (S1.5); in an exported
    standalone script (no `app` package) it builds directly."""
    try:
        from app.modeling.datasets import core_build_cached  # noqa: PLC0415
    except ImportError:
        return _bp_core.build(norm, con, check=check)
    frame, meta, _hit = core_build_cached(norm, con, check)
    return frame, meta


def _bp_studio_panel(plan: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None],
                     warns: list[str]) -> pl.DataFrame:
    """Studio panel for the universe (+ delisted names), dropping optional variables that have no data."""
    ds = dict(plan["dataset"])
    optional = set(plan.get("optional_vars") or [])
    neutral = set(plan.get("grid_neutral_vars") or [])
    req = [v for v in ds["variables"] if v["name"] not in optional and v["name"] not in neutral]
    opt = [v for v in ds["variables"] if v["name"] in optional or v["name"] in neutral]
    if not req:
        raise PanelError("the signal / screen needs at least one variable")
    # required variables (signal, screen) define the grid; optional ones (market cap, attribution factors) are
    # sampled on the same grid and never narrow it
    frame, meta = _bp_build({**ds, "variables": req}, con, check)
    for w in meta.get("warnings", []):
        if w not in warns:
            warns.append(w)
    if frame.is_empty():
        raise PanelError("the universe / signal produced an empty panel")
    g0, g1 = frame["date"].min().isoformat(), frame["date"].max().isoformat()
    dso = {**ds, "start": g0, "end": g1, "variables": opt}
    for _ in range(len(opt) + 1):
        if not dso["variables"]:
            break
        try:
            fo, _m = _bp_build(dso, con, check)
            frame = frame.join(fo.select(["entity_id", "date"] + [v["name"] for v in dso["variables"]]),
                               on=["entity_id", "date"], how="left")
            break
        except _bp_core.SpecError as e:
            msg = str(e)
            name = msg.split(":")[0].strip()
            if name in neutral:
                raise PanelError(f"screen variable {name[2:]} has no data for this universe / period ({msg})") from e
            if any(v["name"] == name for v in dso["variables"]):
                dso = {**dso, "variables": [v for v in dso["variables"] if v["name"] != name]}
                warns.append(f"optional variable {name} skipped ({msg})")
                continue
            warns.append(f"optional variables skipped ({msg})")
            break
    ds = {**ds, "variables": req + [v for v in opt if v["name"] in frame.columns]}
    frame = frame.with_columns(pl.lit(True).alias("member"), pl.lit(False).alias("delisted"))
    dl = plan.get("delisted") or {}
    if dl.get("enabled"):
        regs = dl.get("regions") or ["US"]
        start = _bp_d(plan.get("start")) or date(2000, 1, 1)
        rows = con.execute(
            "SELECT s.security_id, s.last_trade_date FROM securities_master s LEFT JOIN issuers i USING (issuer_id) "
            "WHERE NOT coalesce(s.is_active, TRUE) AND s.last_trade_date >= ? AND CAST(i.region AS VARCHAR) IN ("
            + ", ".join(_bp_q(r) for r in regs) + ")", [start]).fetchall()
        have = set(frame["entity_id"].unique().to_list())
        ids = sorted({r[0] for r in rows} - have)
        if ids:
            dds = {**ds, "entities": {"security_ids": ids, "primary_only": False}, "pit_universe": False,
                   "max_entities": max(len(ids), 1)}
            try:
                f2, _ = _bp_core.build(dds, con, check=check)
                last = pl.DataFrame({"entity_id": [r[0] for r in rows], "_last": [r[1] for r in rows]},
                                    schema={"entity_id": pl.Utf8, "_last": pl.Date})
                f2 = f2.join(last, on="entity_id", how="left").filter(pl.col("date") <= pl.col("_last")).drop("_last")
                f2 = f2.with_columns(pl.lit(True).alias("member"), pl.lit(True).alias("delisted"))
                frame = pl.concat([frame, f2.select(frame.columns)], how="vertical_relaxed")
                warns.append(f"{f2['entity_id'].n_unique()} delisted {'/'.join(regs)} names included while they traded")
            except _bp_core.SpecError as e:
                warns.append(f"delisted names not added: {e}")
    return frame


def _bp_load_prices(con: duckdb.DuckDBPyConnection, ids: list[str], lo: date, hi: date, base: str) -> pl.DataFrame:
    """Daily [entity_id, date, px (base-currency adjusted), dv (USD dollar volume), currency] from prices_daily."""
    pxe = "adj_close * usd_per_unit" if base == "USD" else "adj_close"
    con.register("_bt_ids", pl.DataFrame({"entity_id": ids}).to_arrow())
    try:
        df = _bp_core._retry(lambda: con.execute(
            f"SELECT security_id AS entity_id, date, {pxe} AS px, close * usd_per_unit * volume AS dv, currency "
            f"FROM prices_daily WHERE security_id IN (SELECT entity_id FROM _bt_ids) AND date BETWEEN ? AND ?",
            [lo, hi]).pl())
    finally:
        con.unregister("_bt_ids")
    return df.with_columns(pl.col("date").cast(pl.Date), pl.col("px").cast(pl.Float64), pl.col("dv").cast(pl.Float64))


def _bp_price_features(daily: pl.DataFrame, fields: list[str], bench: pl.DataFrame | None) -> pl.DataFrame:
    """Rolling price features per entity on its own print days ([entity_id, date] + fields + vol (daily))."""
    d = daily.filter(pl.col("px").is_not_null() & (pl.col("px") > 0)).sort(["entity_id", "date"])
    px = pl.col("px")

    def sh(k: int) -> pl.Expr:
        return px.shift(k).over("entity_id")

    r = (px / sh(1) - 1.0)
    d = d.with_columns(r.alias("_r"))
    ex: list[pl.Expr] = [pl.col("_r").rolling_std(63, min_samples=40).over("entity_id").alias("vol")]
    need = set(fields)
    if "mom_12_1" in need:
        ex.append((sh(21) / sh(252) - 1.0).alias("mom_12_1"))
    if "mom_6_1" in need:
        ex.append((sh(21) / sh(126) - 1.0).alias("mom_6_1"))
    for f, k in (("ret_1m", 21), ("ret_3m", 63), ("ret_6m", 126), ("ret_12m", 252)):
        if f in need:
            ex.append((px / sh(k) - 1.0).alias(f))
    if "vol_3m" in need:
        ex.append((pl.col("_r").rolling_std(63, min_samples=40).over("entity_id") * math.sqrt(252)).alias("vol_3m"))
    if "vol_1y" in need:
        ex.append((pl.col("_r").rolling_std(252, min_samples=150).over("entity_id") * math.sqrt(252)).alias("vol_1y"))
    if "high_52w_proximity" in need:
        ex.append((px / px.rolling_max(252, min_samples=60).over("entity_id")).alias("high_52w_proximity"))
    if "trend_200d" in need or "above_200d" in need:
        ma = px.rolling_mean(200, min_samples=150).over("entity_id")
        ex.append((px / ma - 1.0).alias("trend_200d"))
    if True:  # ADV is always needed (spread bucket, impact, liquidity screens)
        # rolling means over a window of zero volume leave a ~1e-12 residue: round sub-dollar ADV to 0 (no volume)
        adv = pl.col("dv").rolling_mean(20, min_samples=5).over("entity_id")
        ex.append(pl.when(adv < 1.0).then(0.0).otherwise(adv).alias("adv_usd_20d"))
    d = d.with_columns(ex)
    if "above_200d" in need:
        d = d.with_columns(pl.when(pl.col("trend_200d").is_null()).then(None)
                           .otherwise((pl.col("trend_200d") > 0).cast(pl.Float64)).alias("above_200d"))
    if "beta_1y" in need:
        if bench is not None and not bench.is_empty():
            d = d.join(bench.select("date", pl.col("r").alias("_b")), on="date", how="left")
            cov = pl.rolling_cov(pl.col("_r"), pl.col("_b"), window_size=252, min_samples=150).over("entity_id")
            var = pl.col("_b").rolling_var(252, min_samples=150).over("entity_id")
            d = d.with_columns((cov / var).alias("beta_1y")).drop("_b")
        else:
            d = d.with_columns(pl.lit(None, pl.Float64).alias("beta_1y"))
    keep = ["entity_id", "date", "vol", "adv_usd_20d"] + [f for f in fields if f in d.columns and f not in ("adv_usd_20d",)]
    return d.select(list(dict.fromkeys(keep)))


def _bp_asof(left: pl.DataFrame, right: pl.DataFrame, on_right: str, cols: list[str], max_age_days: int | None,
             by: str = "entity_id") -> pl.DataFrame:
    """left [by, date] <- right [by, on_right, cols...]: latest right row with on_right <= date (and age <= max)."""
    if right.is_empty():
        return left.with_columns([pl.lit(None, pl.Float64).alias(c) for c in cols if c not in left.columns])
    r = right.select([by, pl.col(on_right).cast(pl.Date).alias("_k")] + cols).drop_nulls("_k").sort([by, "_k"])
    r = r.unique(subset=[by, "_k"], keep="last", maintain_order=True)
    lf = left.with_row_index("_ri").sort([by, "date"])
    j = lf.join_asof(r, left_on="date", right_on="_k", by=by, strategy="backward", check_sortedness=False)
    if max_age_days:
        j = j.with_columns([pl.when((pl.col("date") - pl.col("_k")).dt.total_days() <= max_age_days)
                            .then(pl.col(c)).otherwise(None).alias(c) for c in cols])
    return j.sort("_ri").drop(["_ri", "_k"])


def _bp_extra(plan: dict[str, Any], x: dict[str, Any], con: duckdb.DuckDBPyConnection, ids: list[str],
              hi: date, data_dir: str | None, dates: list[date] | None = None) -> tuple[pl.DataFrame, str]:
    """Rows [entity_id, kd, v] for an extra source; kd = date the value became known."""
    src, field, opt = x["source"], x["field"], x.get("options") or {}
    con.register("_bt_ids", pl.DataFrame({"entity_id": ids}).to_arrow())
    try:
        if src == "ml":
            hz, col = (field.split(":") + ["rank_pct"])[:2]
            if col not in ("rank_pct", "score", "expected_excess_return", "decile", "confidence"):
                raise PanelError(f"{x['name']}: ML field must be <horizon>:<rank_pct|score|expected_excess_return|decile|confidence>")
            mt = opt.get("model_type") or "xgboost_rank"
            run = con.execute(
                "SELECT model_run_id FROM model_runs WHERE model_type = ? AND horizon = ? "
                "ORDER BY coalesce(is_production, FALSE) DESC, created_at DESC LIMIT 1", [mt, hz]).fetchone()
            if not run:
                raise PanelError(f"{x['name']}: no {mt} model run for horizon {hz}")
            df = con.execute(
                f"SELECT security_id AS entity_id, date AS kd, CAST({col} AS DOUBLE) AS v FROM ml_predictions "
                "WHERE model_run_id = ? AND (is_oos OR is_live) AND date <= ? "
                "AND security_id IN (SELECT entity_id FROM _bt_ids)", [run[0], hi]).pl()
            note = f"ML {mt} {hz} {col} (walk-forward out-of-sample predictions, run {run[0]})"
            if mt != "xgboost_rank":
                note += "; MODEL DEMOTED: ft_transformer failed the label-permutation test"
        elif src == "pead":
            if field not in ("pead_score", "sue", "ear_z", "rev_surprise_z", "decile"):
                raise PanelError(f"{x['name']}: PEAD field must be pead_score|sue|ear_z|rev_surprise_z|decile")
            df = con.execute(
                f"SELECT security_id AS entity_id, signal_date AS kd, CAST({field} AS DOUBLE) AS v FROM earnings_pead "
                "WHERE signal_date <= ? AND security_id IN (SELECT entity_id FROM _bt_ids)", [hi]).pl()
            note = f"earnings PEAD {field} (model-based expectations; known on signal_date)"
        elif src == "event_exposure":
            if field not in ("exposure_score", "event_pressure", "expected_impact"):
                raise PanelError(f"{x['name']}: event exposure field must be exposure_score|event_pressure|expected_impact")
            df = con.execute(
                f"SELECT security_id AS entity_id, as_of AS kd, CAST({field} AS DOUBLE) AS v FROM company_event_exposure "
                "WHERE as_of <= ? AND security_id IN (SELECT entity_id FROM _bt_ids)", [hi]).pl()
            note = f"event exposure {field} (model-based)"
        elif src == "studio_run":
            from pathlib import Path  # noqa: PLC0415

            base = Path(opt.get("runs_dir") or (Path(data_dir or ".") / "modeling" / "runs"))
            p = base / str(field) / "oos_predictions.parquet"
            if not p.exists():
                raise PanelError(f"{x['name']}: Studio run {field} has no out-of-sample predictions (needs a panel "
                                 "tree_ensemble / regularized / lightgbm / stacking / ltr / mlp / rnn run with walk-forward CV)")
            raw = pl.read_parquet(p)
            schemes = sorted(set(raw["scheme"].to_list())) if "scheme" in raw.columns else ["unknown"]
            df = raw.select(pl.col("entity_id"), pl.col("date").cast(pl.Date).alias("kd"),
                            pl.col("prediction").cast(pl.Float64).alias("v"))
            df = df.filter(pl.col("entity_id").is_in(ids) & (pl.col("kd") <= hi))
            note = f"Studio run {field} out-of-fold predictions (CV scheme {', '.join(schemes)})"
            if schemes != ["expanding"]:
                note += ("; LOOK-AHEAD IN FITTING: non walk-forward CV trains on data after some test dates - use "
                         "cv.scheme=expanding for a point-in-time backtest")
        elif src == "bql":
            # roadmap v3 B1: a BQL-lite expression evaluated point-in-time at every rebalance date on the serving
            # snapshot (cross-sectional functions run over the universe at that date); needs the backend
            try:
                from app.bql.engine import panel as _bql_panel  # noqa: PLC0415
                from app.bql.engine import serving_connection as _bql_con  # noqa: PLC0415
                from app.bql.grammar import BqlError as _BqlError  # noqa: PLC0415
            except ImportError as e:
                raise PanelError(f"{x['name']}: BQL signals need the BiG Equity 360 backend") from e
            grid = sorted(set(dates or [])) or [hi]
            scon = _bql_con()
            try:
                rows, bw = _bql_panel(str(field), scon, ids, dates=grid, universe=opt.get("universe"), tmo=900.0)
            except _BqlError as e:
                raise PanelError(f"{x['name']}: BQL {field!r}: {e}") from e
            finally:
                scon.close()
            vals = [(r[0], r[1], float(r[2])) for r in rows if isinstance(r[2], (int, float, bool))]
            df = pl.DataFrame(vals, schema={"entity_id": pl.Utf8, "kd": pl.Date, "v": pl.Float64}, orient="row")
            note = (f"BQL expression {field} evaluated point-in-time at each of the {len(grid)} rebalance dates "
                    "(bars and knowledge dates <= the date; cross-sectional functions over the universe)")
            if bw:
                note += "; " + "; ".join(bw[:3])
        else:
            raise PanelError(f"unknown extra source {src!r}")
    finally:
        con.unregister("_bt_ids")
    return df.with_columns(pl.col("kd").cast(pl.Date), pl.col("v").cast(pl.Float64)), note


def _bp_securities(plan: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None],
                   warns: list[str], notes: list[str], data_dir: str | None) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None]:
    frame = _bp_studio_panel(plan, con, check, warns)
    if frame.is_empty():
        raise PanelError("the universe / signal produced an empty panel")
    ids = sorted(frame["entity_id"].unique().to_list())
    grid_lo, grid_hi = frame["date"].min(), frame["date"].max()
    hi = _bp_d(plan.get("end")) or grid_hi
    hi = max(hi, grid_hi)
    plo = grid_lo - timedelta(days=int(plan.get("price_warmup_days") or 400))
    bench_id = plan.get("benchmark")
    pids = ids + ([bench_id] if bench_id and bench_id not in ("universe_ew", "none") else [])
    daily = _bp_load_prices(con, pids, plo, hi + timedelta(days=10), plan.get("currency") or "USD")
    check()
    bench = None
    if bench_id and bench_id not in ("universe_ew", "none"):
        b = daily.filter(pl.col("entity_id") == bench_id).sort("date")
        if b.is_empty():
            warns.append(f"benchmark {bench_id} has no prices: no benchmark")
        else:
            bench = b.with_columns((pl.col("px") / pl.col("px").shift(1) - 1.0).alias("r")).select("date", "r")
        daily = daily.filter(pl.col("entity_id") != bench_id) if bench_id not in ids else daily
    feats = _bp_price_features(daily, list(plan.get("price_fields") or []), bench)
    out = _bp_asof(frame.select("entity_id", "date"), feats, "date",
                   [c for c in feats.columns if c not in ("entity_id", "date")], 10)
    frame = frame.join(out, on=["entity_id", "date"], how="left")
    cur = daily.group_by("entity_id").agg(pl.col("currency").last())
    frame = frame.join(cur, on="entity_id", how="left")
    # extra sources
    for x in plan.get("extra") or []:
        df, note = _bp_extra(plan, x, con, ids, hi, data_dir,
                             dates=sorted(frame["date"].unique().to_list()) if x["source"] == "bql" else None)
        notes.append(note)
        if df.is_empty():
            warns.append(f"{x['name']}: no {x['source']} data for this universe")
        j = _bp_asof(frame.select("entity_id", "date"), df.rename({"v": x["name"]}), "kd", [x["name"]],
                     int(x.get("max_age_days") or 0) or None)
        frame = frame.with_columns(j[x["name"]])
        check()
    return frame, daily, bench


# ------------------------------------------------------------------------------------------------ assets panel


def _bp_assets(plan: dict[str, Any], con: duckdb.DuckDBPyConnection, warns: list[str], notes: list[str]
               ) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame | None]:
    a = plan.get("assets") or {}
    metrics = sorted(set(a.get("metrics") or []))
    where = []
    if a.get("asset_ids"):
        where.append("asset_id IN (" + ", ".join(_bp_q(x) for x in a["asset_ids"]) + ")")
    if a.get("asset_classes"):
        where.append("CAST(asset_class AS VARCHAR) IN (" + ", ".join(_bp_q(x) for x in a["asset_classes"]) + ")")
    w = ("WHERE " + " AND ".join(where)) if where else ""
    cols = ", ".join(f"TRY_CAST(\"{m}\" AS DOUBLE) AS \"{m}\"" for m in metrics)
    raw = _bp_core._retry(lambda: con.execute(
        f"SELECT asset_id AS entity_id, CAST(asset_class AS VARCHAR) AS asset_class, CAST(date AS DATE) AS date, "
        f"CAST(knowledge_date AS DATE) AS kd, TRY_CAST(price AS DOUBLE) AS px{', ' + cols if cols else ''} "
        f"FROM cross_asset_signals {w}").pl())
    if raw.is_empty():
        raise PanelError("no cross-asset signals for the selected assets")
    start = _bp_d(plan.get("start")) or raw["date"].min()
    end = _bp_d(plan.get("end")) or raw["date"].max()
    grid = _bp_core.make_grid(_bp_core.bucket_of(start, plan["frequency"]) if plan["frequency"] != "D" else start,
                              end, plan["frequency"], sorted(raw["date"].unique().to_list()) if plan["frequency"] == "D" else None)
    grid = [g for g in grid if start <= g <= end]
    ents = sorted(raw["entity_id"].unique().to_list())
    left = pl.DataFrame({"entity_id": ents}).join(pl.DataFrame({"date": grid}, schema={"date": pl.Date}), how="cross")
    sig = _bp_asof(left, raw.select(["entity_id", "kd"] + metrics), "kd", metrics, 10)
    cls = raw.group_by("entity_id").agg(pl.col("asset_class").last())
    sig = sig.join(cls, on="entity_id", how="left").with_columns(
        pl.col("entity_id").alias("entity_label"), pl.col("entity_id").alias("entity_name"),
        pl.col("asset_class").alias("sector_id"), pl.col("asset_class").alias("country"), pl.lit(None, pl.Utf8).alias("region"),
        pl.lit(True).alias("member"), pl.lit(False).alias("delisted"), pl.lit("USD").alias("currency"))
    daily = raw.select("entity_id", "date", "px").with_columns(pl.lit(None, pl.Float64).alias("dv"),
                                                              pl.lit("USD").alias("currency"))
    bench = None
    feats = _bp_price_features(daily, list(plan.get("price_fields") or []), None)
    out = _bp_asof(sig.select("entity_id", "date"), feats, "date", [c for c in feats.columns if c not in ("entity_id", "date")], 10)
    sig = sig.join(out, on=["entity_id", "date"], how="left")
    # liquid futures / FX / index markets: no dollar volume in the signal table -> class-level liquidity proxy
    # (top spread bucket, negligible impact); shorts carry no borrow fee (set by the runner)
    adv = {"fx": 5e10, "rates": 5e10, "equity_index": 2e10, "commodity": 5e9, "credit": 1e9, "crypto": 1e9}
    sig = sig.with_columns(pl.col("asset_class").replace_strict(adv, default=1e9, return_dtype=pl.Float64)
                           .alias("adv_usd_20d"))
    notes.append("assets universe costs: class-level liquidity proxy (top ADV spread bucket) and no borrow fee on "
                 "futures / FX shorts")
    notes.append("cross-asset prices: Yahoo front-month futures (roll gaps included, spot-like), official FX fixings, "
                 "synthetic UST total-return indices; trend / curve / value / z metrics are model-based")
    return sig, daily, bench


# ------------------------------------------------------------------------------------------------ signal, screen


def _bp_eval_screen(frame: pl.DataFrame, rules: list[dict[str, Any]], warns: list[str]) -> pl.Expr:
    ok = pl.lit(True)
    for r in rules or []:
        c, op, v, v2 = r["column"], r["op"], r.get("value"), r.get("value2")
        if c not in frame.columns:
            warns.append(f"screen rule on {r.get('field', c)} ignored: no point-in-time data")
            continue
        e = pl.col(c)
        if op == "is_null":
            cond = e.is_null()
        elif op == "not_null":
            cond = e.is_not_null()
        elif op in ("in", "not_in"):
            vals = v if isinstance(v, list) else [v]
            cond = e.is_in(vals)
            cond = cond.fill_null(False) if op == "in" else (~cond).fill_null(True)
        elif op == "between":
            lo, hi = (v if isinstance(v, list) and v2 is None else [v, v2])[:2]
            cond = (e >= lo) & (e <= hi)
        else:
            cond = {"eq": e == v, "neq": e != v, "gt": e > v, "gte": e >= v, "lt": e < v, "lte": e <= v}[op]
        ok = ok & cond.fill_null(False)
    return ok


def _bp_signal(frame: pl.DataFrame, sig: dict[str, Any], warns: list[str]) -> pl.DataFrame:
    """Combine signal variables into `signal` (higher = better) among eligible rows per date."""
    vs = sig.get("vars") or []
    comb = sig.get("combine") or "single"
    elig = pl.col("eligible")

    def directed(v: dict[str, Any]) -> pl.Expr:
        e = pl.col(v["name"]).cast(pl.Float64)
        e = pl.when(e.is_nan()).then(None).otherwise(e)
        return e if v.get("higher_is_better", True) else -e

    if comb == "formula":
        expr = sig.get("formula") or ""
        if _bp_uses_panel is not None and _bp_uses_panel(expr):
            raise PanelError("signal formula: panel functions (lag, avg, growth_yoy, ...) are not allowed; use "
                             "variable transforms instead")
        s = _bp_compile_expr(expr)
    elif comb in ("zscore", "rank") and len(vs) > 1:
        parts, wts = [], []
        for v in vs:
            e = pl.when(elig).then(directed(v)).otherwise(None)
            if comb == "zscore":
                sd = e.std().over("date")
                z = pl.when(sd > 0).then((e - e.mean().over("date")) / sd).otherwise(None).clip(-3, 3)
            else:
                n = e.count().over("date")
                z = pl.when(n > 1).then((e.rank("average").over("date") - 0.5) / n).otherwise(None)
            parts.append(z)
            wts.append(float(v.get("weight", 1.0)))
        num = pl.sum_horizontal([pl.when(p.is_not_null()).then(p * w).otherwise(0.0) for p, w in zip(parts, wts, strict=True)])
        den = pl.sum_horizontal([pl.when(p.is_not_null()).then(abs(w)).otherwise(0.0) for p, w in zip(parts, wts, strict=True)])
        cnt = pl.sum_horizontal([p.is_not_null().cast(pl.Int32) for p in parts])
        s = pl.when((den > 0) & (cnt * 2 >= len(parts))).then(num / den).otherwise(None)
    else:
        if not vs:
            raise PanelError("signal has no variables")
        s = directed(vs[0])
    frame = frame.with_columns(s.cast(pl.Float64).alias("signal"))
    frame = frame.with_columns(pl.when(pl.col("signal").is_nan() | pl.col("signal").is_infinite()).then(None)
                               .otherwise(pl.col("signal")).alias("signal"))
    if not sig.get("higher_is_better", True):
        frame = frame.with_columns(-pl.col("signal"))
    k = int(sig.get("lag_periods") or 0)
    if k:
        frame = frame.sort(["entity_id", "date"]).with_columns(pl.col("signal").shift(k).over("entity_id"))
    return frame


def _bp_derived_screen_cols(frame: pl.DataFrame, plan: dict[str, Any]) -> pl.DataFrame:
    mc = plan.get("mcap_var")
    if mc and mc in frame.columns:
        frame = frame.with_columns(
            pl.when(pl.col(mc).is_null()).then(None).when(pl.col(mc) >= 10e9).then(pl.lit("large"))
            .when(pl.col(mc) >= 2e9).then(pl.lit("mid")).otherwise(pl.lit("small")).alias("_size_bucket"),
            pl.col(mc).alias("mcap"))
    elif "mcap" not in frame.columns:
        frame = frame.with_columns(pl.lit(None, pl.Float64).alias("mcap"))
    comp = plan.get("composite_var")
    if comp and comp in frame.columns:
        e = pl.when(pl.col("member")).then(pl.col(comp)).otherwise(None)
        n = e.count().over("date")
        rk = e.rank("ordinal", descending=True).over("date")
        frame = frame.with_columns(rk.cast(pl.Float64).alias("_rank"),
                                   pl.when(e.is_null()).then(None).otherwise(
                                       pl.lit(["A", "B", "C", "D", "E"]).list.get(
                                           ((rk - 1) * 5 / n).floor().cast(pl.Int64).clip(0, 4))).alias("_grade"))
    return frame


# ------------------------------------------------------------------------------------------------ regimes, rf, events


def regime_states(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """macro_factors monthly state rows -> [regime_type, scope, month, state, model_based]."""
    try:
        s = con.execute("SELECT factor_id, CAST(date AS DATE) AS month, state, model_based FROM macro_factors "
                        "WHERE CAST(frequency AS VARCHAR) = 'M' AND CAST(unit AS VARCHAR) = 'state' AND state IS NOT NULL").pl()
    except duckdb.Error:
        return pl.DataFrame(schema={"regime_type": pl.Utf8, "scope": pl.Utf8, "month": pl.Date, "state": pl.Utf8,
                                    "model_based": pl.Boolean})
    rt = (pl.when(pl.col("factor_id").str.starts_with("cycle_quadrant_")).then(pl.lit("cycle_quadrant"))
          .when(pl.col("factor_id").str.starts_with("hmm_regime_")).then(pl.lit("hmm_regime"))
          .when(pl.col("factor_id").str.starts_with("rate_cycle_")).then(pl.lit("rate_cycle"))
          .otherwise(pl.lit("inflation_regime")))
    scope = pl.col("factor_id").str.replace(r"^(cycle_quadrant|hmm_regime|rate_cycle|inflation_regime)_", "")
    return s.select(rt.alias("regime_type"), scope.alias("scope"), "month", "state", "model_based")


def regime_overlay(states: pl.DataFrame, grid: list[date], rf: dict[str, Any]) -> tuple[dict[date, float], list[str]]:
    """Exposure scale per rebalance date from the latest regime state *known* at t (one-month publication lag)."""
    st = states.filter((pl.col("regime_type") == rf["regime_type"]) & (pl.col("scope") == rf["scope"])).sort("month")
    if st.is_empty():
        return {}, [f"regime filter {rf['regime_type']}:{rf['scope']}: no states (filter ignored)"]
    # state of month m (keyed by its first day) usable from the last day of month m + 1
    avail = [(_bp_core.bucket_of(_bp_core.bucket_of(m, "M") + timedelta(days=1), "M"), s)
             for m, s in st.select("month", "state").iter_rows()]
    allowed = set(rf.get("states") or [])
    scale_off = 0.5 if rf.get("action") == "half" else 0.0
    out, j, cur = {}, 0, None
    for t in sorted(grid):
        while j < len(avail) and avail[j][0] <= t:
            cur = avail[j][1]
            j += 1
        out[t] = 1.0 if (cur is None or cur in allowed) else scale_off
    return out, ["regime overlay uses each month's state from the end of the following month (macro_factors has no "
                 "knowledge date; conservative publication lag)"]


def rf_daily(con: duckdb.DuckDBPyConnection, dates: list[date]) -> np.ndarray | None:
    try:
        df = con.execute("SELECT CAST(date AS DATE) AS date, value FROM macro_factors WHERE factor_id = 'cash' "
                         "AND CAST(frequency AS VARCHAR) = 'D' AND value IS NOT NULL ORDER BY 1").pl()
    except duckdb.Error:
        return None
    if df.is_empty():
        return None
    # value = 3m T-bill yield / periods per year (macro_factors cash, FRED DGS3MO): daily cash return
    left = pl.DataFrame({"date": dates}, schema={"date": pl.Date}).with_row_index("_i")
    j = left.join_asof(df.sort("date"), on="date", strategy="backward").sort("_i")
    return j["value"].fill_null(0.0).to_numpy().astype(np.float64)


def build_events(plan: dict[str, Any], con: duckdb.DuckDBPyConnection, frame: pl.DataFrame, ids: list[str],
                 warns: list[str], notes: list[str], px: Any = None, data_dir: str | None = None) -> pl.DataFrame:
    """Event rows [entity_id, date (known at close), direction, strength] for event-driven mode (+ per-event
    stop_loss / take_profit when the calibrated band stop mode is on)."""
    ev = plan["events"]
    trig, p = ev["trigger"], ev.get("params") or {}
    lo = _bp_d(plan.get("start")) or date(1900, 1, 1)
    hi = _bp_d(plan.get("end")) or date(2100, 1, 1)
    empty = pl.DataFrame(schema={"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int8, "strength": pl.Float64})
    con.register("_bt_ids", pl.DataFrame({"entity_id": ids}).to_arrow())
    try:
        if trig == "insider_cluster":
            df = con.execute(
                "SELECT security_id AS entity_id, CAST(as_of_date AS DATE) AS date, coalesce(cluster_buy, FALSE) AS cb, "
                "TRY_CAST(signal_score AS DOUBLE) AS strength FROM insider_signals "
                "WHERE security_id IN (SELECT entity_id FROM _bt_ids) ORDER BY 1, 2").pl()
            df = df.with_columns(pl.col("cb").shift(1).over("entity_id").fill_null(False).alias("_prev"))
            out = df.filter(pl.col("cb") & ~pl.col("_prev")).select(
                "entity_id", "date", pl.lit(1, pl.Int8).alias("direction"), "strength")
            notes.append("insider cluster buy: first insider_signals snapshot with cluster_buy (>= 3 insiders buying, "
                         "Form 4 filing dates) per name")
        elif trig == "earnings_surprise":
            f = p.get("field") or "sue"
            thr = float(p.get("threshold") if p.get("threshold") is not None else 2.0)
            df = con.execute(
                f"SELECT security_id AS entity_id, CAST(signal_date AS DATE) AS date, TRY_CAST({f} AS DOUBLE) AS v "
                "FROM earnings_pead WHERE security_id IN (SELECT entity_id FROM _bt_ids)").pl().drop_nulls("v")
            out = df.filter(pl.col("v").abs() >= thr).select(
                "entity_id", "date", pl.col("v").sign().cast(pl.Int8).alias("direction"),
                pl.col("v").abs().alias("strength"))
            notes.append(f"earnings surprise: |{f}| >= {thr} vs MODEL-BASED expectations (SARIMA, not consensus), "
                         "known on signal_date")
        elif trig == "cftc_extreme":
            hi_t, lo_t = float(p.get("high", 0.9)), float(p.get("low", 0.1))
            mode = p.get("mode", "contrarian")
            df = con.execute(
                "SELECT asset_id AS entity_id, CAST(knowledge_date AS DATE) AS date, TRY_CAST(cot_index_3y AS DOUBLE) AS v "
                "FROM cross_asset_signals WHERE asset_id IN (SELECT entity_id FROM _bt_ids) AND cot_index_3y IS NOT NULL "
                "ORDER BY 1, 2").pl()
            df = df.with_columns(pl.col("v").shift(1).over("entity_id").alias("_p"))
            up = (pl.col("v") >= hi_t) & (pl.col("_p") < hi_t)
            dn = (pl.col("v") <= lo_t) & (pl.col("_p") > lo_t)
            sign = -1 if mode == "contrarian" else 1
            out = df.filter(up | dn).select(
                "entity_id", "date",
                (pl.when(up).then(sign).otherwise(-sign)).cast(pl.Int8).alias("direction"),
                (pl.col("v") - 0.5).abs().alias("strength")).unique(["entity_id", "date"])
            notes.append(f"CFTC extreme: COT index (3y) crossing >= {hi_t} / <= {lo_t} ({mode}); official CFTC "
                         "positioning, known on the release date")
        elif trig == "macro_surprise":
            thr = float(p.get("threshold") if p.get("threshold") is not None else 1.5)
            where = ["abs(signed_z) >= ?"]
            prm: list[Any] = [thr]
            if p.get("country"):
                where.append("country = ?")
                prm.append(p["country"])
            if p.get("theme"):
                where.append("CAST(theme AS VARCHAR) = ?")
                prm.append(p["theme"])
            if p.get("series_ids"):
                where.append("series_id IN (" + ", ".join(_bp_q(s) for s in p["series_ids"]) + ")")
            df = con.execute(f"SELECT CAST(release_date AS DATE) AS date, TRY_CAST(signed_z AS DOUBLE) AS z FROM "
                             f"macro_surprises WHERE {' AND '.join(where)}", prm).pl()
            targets = [t for t in (p.get("targets") or ids) if t in set(ids)]
            sgn = -1 if p.get("sign") == "inverse" else 1
            rows = [(t, d, int(np.sign(z)) * sgn, abs(z)) for d, z in df.iter_rows() for t in targets]
            out = pl.DataFrame(rows, schema={"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int8,
                                             "strength": pl.Float64}, orient="row") if rows else empty
            notes.append(f"macro surprise: |signed z| >= {thr} vs MODEL-BASED expectations (macro_surprises), release date")
        elif trig == "confluence_touch":
            thr = float(p.get("max_distance_atr", 0.25))
            df = con.execute(
                "SELECT security_id AS entity_id, CAST(as_of_date AS DATE) AS date, side, TRY_CAST(strength AS DOUBLE) AS s, "
                "coalesce(in_zone, FALSE) AS iz, TRY_CAST(distance_atr AS DOUBLE) AS da FROM confluence_zones "
                "WHERE rank = 1 AND security_id IN (SELECT entity_id FROM _bt_ids)").pl()
            n_dates = df["date"].n_unique() if df.height else 0
            if n_dates < 3:
                warns.append(f"confluence zones are stored for {n_dates} as-of date(s) only: no point-in-time history "
                             "to backtest touches yet")
            out = df.filter(pl.col("iz") | (pl.col("da").abs() <= thr)).select(
                "entity_id", "date", pl.when(pl.col("side") == "support").then(1).otherwise(-1).cast(pl.Int8).alias("direction"),
                pl.col("s").alias("strength"))
        elif trig == "signal_threshold":
            thr = float(p.get("threshold", 0.0))
            f = frame.filter(pl.col("eligible")).sort(["entity_id", "date"]).with_columns(
                pl.col("signal").shift(1).over("entity_id").alias("_p"))
            cross = (pl.col("signal") >= thr) & (pl.col("_p") < thr)
            out = f.filter(cross).select("entity_id", "date", pl.lit(1, pl.Int8).alias("direction"),
                                         pl.col("signal").alias("strength"))
            if p.get("short_below") is not None:
                t2 = float(p["short_below"])
                o2 = f.filter((pl.col("signal") <= t2) & (pl.col("_p") > t2)).select(
                    "entity_id", "date", pl.lit(-1, pl.Int8).alias("direction"), (-pl.col("signal")).alias("strength"))
                out = pl.concat([out, o2])
            notes.append(f"signal threshold: signal crossing {thr} at a rebalance date")
        elif trig == "calibrated_band_breakout":
            from app.conformal import backtest_hooks as CH  # noqa: PLC0415

            if px is None:
                raise PanelError("calibrated_band_breakout needs the price panel")
            hz, lvl = int(p.get("horizon", 5)), int(p.get("level", 90))
            if hz not in CH.HORIZONS or lvl not in CH.LEVELS:
                raise PanelError("calibrated_band_breakout: horizon in 1/5/10/20, level in 80/90/95")
            out = CH.breakout_events(px, hz, lvl, "both" if p.get("direction") == "both" else "up")
            notes.append(f"calibrated-band breakout: the {hz}-session move left the {lvl}% marginal conformal band emitted "
                         f"{hz} sessions earlier (walk-forward EWMA band on the panel prices). " + CH.note("calibrated_band_breakout"))
        elif trig == "peer_breakaway":
            from app.conformal import backtest_hooks as CH  # noqa: PLC0415
            from app.core.config import get_settings  # noqa: PLC0415

            from pathlib import Path  # noqa: PLC0415

            dd = Path(data_dir) if data_dir else get_settings().data_dir
            out, used = CH.peer_breakaway_events(ids, float(p.get("z", 2.5)), str(p.get("source", "auto")), dd, con,
                                                 warns=warns)
            notes.append(f"peer break-away ({used}): fade the member with |z_cond| >= {float(p.get('z', 2.5))} on days the "
                         "peer group's joint score exceeded its 90% radius (shorts need allow_short). " + CH.note("peer_breakaway"))
        elif trig == "meta_setups":
            from pathlib import Path  # noqa: PLC0415

            from app.core.config import get_settings  # noqa: PLC0415
            from app.techmeta import backtest_hooks as MH  # noqa: PLC0415

            dd = Path(data_dir) if data_dir else get_settings().data_dir
            hz = int(p.get("horizon", 20))
            try:
                out, used = MH.meta_events(ids, hz, str(p.get("mode", "take")), str(p.get("source", "auto")),
                                           str(p.get("direction", "long")), dd, con, MH.base_weight_of(ev), warns)
            except ValueError as e:
                raise PanelError(str(e)) from e
            gnote, prod = MH.gate_note(dd, hz)
            notes.append(f"meta-labelled setups ({used}, mode {p.get('mode', 'take')}): v2 weekly setups, exits at T1 / the "
                         f"invalidation (close-based) or after the hold; {gnote}")
            if not prod:
                warns.append(f"event trigger meta_setups: {gnote}")
        else:
            raise PanelError(f"unknown event trigger {trig!r}")
    finally:
        con.unregister("_bt_ids")
    bs = ev.get("band_stops")
    if bs and px is not None:
        from app.conformal import backtest_hooks as CH  # noqa: PLC0415

        out = out.with_columns(pl.col("date").cast(pl.Date), pl.col("direction").cast(pl.Int8))
        out = CH.band_stops(out, px, int(bs.get("horizon", 20)), int(bs.get("level", 90)), bool(bs.get("target")))
        notes.append(f"stop mode calibrated band: per-trade stop at the {bs.get('level', 90)}% / {bs.get('horizon', 20)}-session "
                     "band edge emitted on the event date" + (" (target at the opposite edge)" if bs.get("target") else "")
                     + ". " + CH.note("band_stops"))
    out = out.with_columns(pl.col("date").cast(pl.Date), pl.col("strength").cast(pl.Float64).fill_null(0.0),
                           pl.col("direction").cast(pl.Int8))
    return out.filter((pl.col("date") >= lo) & (pl.col("date") <= hi)).sort(["date", "entity_id"])


# ------------------------------------------------------------------------------------------------ build


def build_panel(plan: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None] | None = None,
          data_dir: str | None = None) -> tuple[pl.DataFrame, Any, pl.DataFrame | None, dict[str, Any]]:
    """Execute a plan -> (panel, PricePanel, events | None, meta)."""
    t0 = time.perf_counter()
    check = check or (lambda: None)
    warns: list[str] = []
    notes: list[str] = []
    if plan["kind"] == "assets":
        frame, daily, bench = _bp_assets(plan, con, warns, notes)
    else:
        frame, daily, bench = _bp_securities(plan, con, check, warns, notes, data_dir)
    # ---- screen, eligibility, signal
    frame = _bp_derived_screen_cols(frame, plan)
    elig = pl.col("member").fill_null(False) & _bp_eval_screen(frame, plan.get("screen") or [], warns)
    if plan.get("min_adv_usd"):
        elig = elig & (pl.col("adv_usd_20d") >= float(plan["min_adv_usd"])).fill_null(False)
    if plan.get("min_market_cap_usd"):
        elig = elig & (pl.col("mcap") >= float(plan["min_market_cap_usd"])).fill_null(False)
    frame = frame.with_columns(elig.alias("eligible"))
    frame = _bp_signal(frame, plan["signal"], warns)
    frame = frame.with_columns(pl.col("sector_id").alias("group_sector"), pl.col("country").alias("group_country"),
                               (pl.col("vol") if "vol" in frame.columns else pl.lit(None, pl.Float64)).alias("vol"),
                               (pl.col("adv_usd_20d") if "adv_usd_20d" in frame.columns else pl.lit(None, pl.Float64)).alias("adv_usd"))
    s0 = _bp_d(plan.get("start"))
    if s0:
        frame = frame.filter(pl.col("date") >= s0)
    e0 = _bp_d(plan.get("end"))
    if e0:
        frame = frame.filter(pl.col("date") <= e0)
    if frame.is_empty():
        raise PanelError("no rebalance dates in the requested period")
    # ---- price panel
    ids = sorted(frame["entity_id"].unique().to_list())
    cur = dict(frame.group_by("entity_id").agg(pl.col("currency").last()).iter_rows()) if "currency" in frame.columns else {}
    pend = (e0 or frame["date"].max()) + timedelta(days=int(plan.get("tail_days") or 45))
    pp = _bp_engine.price_panel_from_long(daily.select("entity_id", "date", "px"), ids, cur, None, pend)
    if pp.T == 0:
        raise PanelError("no prices for the universe in the requested period")
    if bench is not None:
        pp.bench = _bp_engine.align_series(pp.dates, bench.filter(pl.col("date").is_in(pp.dates)))
        pp.bench_id = plan.get("benchmark")
    elif plan.get("benchmark") == "universe_ew":
        alive = np.zeros_like(pp.R, dtype=bool)
        for j in range(len(pp.ids)):
            if pp.first[j] >= 0:
                alive[pp.first[j] + 1:pp.last[j] + 1, j] = True
        cnt = alive.sum(axis=1)
        pp.bench = np.where(cnt > 0, (pp.R * alive).sum(axis=1) / np.maximum(cnt, 1), 0.0)
        pp.bench_id = "universe_ew"
    pp.rf = rf_daily(con, pp.dates) if plan.get("rf", "cash") == "cash" else None
    if pp.rf is None:
        pp.rf = np.zeros(pp.T)
        if plan.get("rf", "cash") == "cash":
            warns.append("risk-free series (macro_factors cash) unavailable: rf = 0")
    events = build_events(plan, con, frame, ids, warns, notes, pp, data_dir) if plan.get("events") else None
    states = regime_states(con) if _bp_has(con, "macro_factors") else None
    meta = {
        "panel_version": PANEL_VERSION, "kind": plan["kind"], "frequency": plan["frequency"],
        "n_rows": frame.height, "n_entities": len(ids), "n_dates": frame["date"].n_unique(),
        "start": frame["date"].min().isoformat(), "end": frame["date"].max().isoformat(),
        "price_start": pp.dates[0].isoformat(), "price_end": pp.dates[-1].isoformat(), "n_price_days": pp.T,
        "n_eligible_avg": float(frame.group_by("date").agg(pl.col("eligible").sum())["eligible"].mean()),
        "n_with_signal_avg": float(frame.filter(pl.col("eligible") & pl.col("signal").is_not_null())
                                   .group_by("date").len()["len"].mean() or 0.0) if frame.height else 0.0,
        "n_delisted": int(frame.filter(pl.col("delisted"))["entity_id"].n_unique()) if "delisted" in frame.columns else 0,
        "n_events": events.height if events is not None else None,
        "warnings": warns, "notes": notes, "build_seconds": round(time.perf_counter() - t0, 3),
    }
    meta["_states"] = states
    return frame, pp, events, meta
