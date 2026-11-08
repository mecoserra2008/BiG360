"""Jobs of the meta-labelling model (Technicals v3 #2; gate docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling").

`tech_meta_fit` (weekly), function `run_fit_job`:
  1. training rows (data/models/technicals_v3/meta/dataset/ds_h<H>.parquet; params mode=seed imports the walk-forward
     build from scratchpad/tech_v3_meta/ds): incremental mode appends the setup origins of tech_prob_setup_events
     whose H-session labels completed since the last fit (features built with app.techmeta.dataset, T2 from the latest
     tech_prob_surfaces fit, live peer tables);
  2. per H = 10 / 20 / 40: LR and XGB meta models (win + loss) on origins < fit date - 1y - embargo, hyperparameters on
     the inner year, Platt / isotonic + learner choice + take / skip threshold + size reference on the last year
     (in-sample), global importance (mean |contribution| on the calibration block) -> data/models/technicals_v3/meta/
     <fit_date>/meta_model.pkl + meta.json (last 6 fits kept);
  3. gate verdicts (data/reports/technicals_v3/meta/gate.json, written by the walk-forward report) -> tech_meta_gates.

`tech_meta_score` (nightly, after technicals_probabilities and the context sources), function `run_score_job`:
  every security with T2 rows on the latest tech_prob_setups as_of -> meta P(win) / P(loss) / P(neither), meta
  expected R, take / skip, capped fractional Kelly risk fraction, relative size, top drivers, and the context feature
  vector (for query-time thesis barriers) -> tech_meta_setups (latest + month-end snapshots).

    python -m app.techmeta.job [--fit] [--mode seed|incremental] [--as-of YYYY-MM-DD] [--tickers A,B] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import shutil
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.techmeta import dataset as DS
from app.techmeta import features as F
from app.techmeta import model as M
from app.techmeta import state as ST

log = get_logger("app.techmeta.job")

HORIZONS = DS.HORIZONS
OUTPUTS = (*[f"meta_h{h}" for h in HORIZONS], *[f"decision_h{h}" for h in HORIZONS])
NIGHTLY_HISTORY_DAYS = 760          # 504-session percentiles + warm-up
NIGHTLY_CHUNK = 1200
KEEP_FITS = 6
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}
IMPORTANCE_SAMPLE = 20000


def model_root(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "models" / "technicals_v3" / "meta"
    p.mkdir(parents=True, exist_ok=True)
    return p


def dataset_dir(settings: Settings | None = None) -> Path:
    p = model_root(settings) / "dataset"
    p.mkdir(parents=True, exist_ok=True)
    return p


def seed_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).project_root / "scratchpad" / "tech_v3_meta" / "ds"


def gate_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "reports" / "technicals_v3" / "meta" / "gate.json"


def load_gate(settings: Settings | None = None) -> dict[str, Any]:
    p = gate_file(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def verdict(gate: dict[str, Any], output: str) -> str:
    return ((gate.get("outputs") or {}).get(output) or {}).get("verdict") or "not_validated"


def latest_artifact(settings: Settings | None = None) -> dict[str, Any] | None:
    root = model_root(settings)
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta_model.pkl").exists()) if root.exists() else []
    if not dirs:
        return None
    with open(dirs[-1] / "meta_model.pkl", "rb") as f:
        return pickle.load(f)  # noqa: S301 - our own artifact


def _scan(table: str) -> pl.LazyFrame | None:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        lf = scan_table(table)
        lf.collect_schema()
        return lf
    except Exception as e:  # noqa: BLE001
        log.info("%s unavailable: %s", table, e)
        return None


# ------------------------------------------------------------------------------------------------ fitting


def fit_production(df: pl.DataFrame, H: int, fit_date: date, extra: list[str] | None = None) -> M.MetaModel:
    """The production meta model for one horizon from all complete rows (protocol = one walk-forward step)."""
    cols = [c for c in F.all_features(extra=extra) if c in df.columns]
    df = df.filter(pl.col("out") >= 0).sort(["date", "security_id"])
    d = df["date"].to_numpy().astype("datetime64[D]")
    e = np.timedelta64(DS.embargo_days(H), "D")
    end = np.datetime64(fit_date, "D")
    yr = np.timedelta64(365, "D")
    fit = d < end - yr - e
    cal = (d >= end - yr) & (d < end)
    inner_val = fit & (d >= end - 2 * yr - e)
    inner_train = d < end - 2 * yr - 2 * e
    X = F.matrix(df, cols)
    Yw = df["y_win"].cast(pl.Float64).to_numpy()
    Yl = df["y_loss"].cast(pl.Float64).to_numpy()
    w = df["w_uniq"].cast(pl.Float64).to_numpy()
    fits: dict[str, dict[str, Any]] = {}
    for kind in ("lr", "xgb"):
        rec: dict[str, Any] = {}
        for tgt, y, base in (("win", Yw, "t2_logit_pw"), ("loss", Yl, "t2_logit_pl")):
            L = M.fit_learner(kind, X[fit], y[fit], w[fit], cols, base, inner_val=inner_val[fit], inner_train=inner_train[fit])
            cal_, ci = M.fit_calibrator(L.margin(X[cal]), y[cal], w[cal], d[cal])
            rec[tgt] = (L, cal_, ci)
        fits[kind] = rec
    hl = {k: min(fits[k]["win"][2]["half_logloss"].values()) for k in fits}
    kind = min(hl, key=lambda k: hl[k])
    (Lw, cw, ciw), (Ll, cl, cil) = fits[kind]["win"], fits[kind]["loss"]
    mm = M.MetaModel(H, kind, cols, Lw, Ll, cw, cl)
    pr = mm.predict(X[cal])
    rr, cost, mtm = df["rr_close"].to_numpy()[cal], df["cost_r"].to_numpy()[cal], df["mtm_r"].to_numpy()[cal]
    er = M.expected_r(pr["p_win"], pr["p_loss"], rr, cost, mtm)
    thr, tinfo = M.choose_threshold(er, df["real_r_mtm"].to_numpy()[cal], M.week_index(d[cal]))
    k = M.kelly_fraction(pr["p_win"], pr["p_loss"], rr, mtm, er)
    tk = (er > thr) & (k > 0)
    mm.threshold = thr
    mm.size_ref = float(np.median(k[tk])) if tk.any() else None
    imp = {}
    rng = np.random.default_rng(0)
    ic = np.nonzero(cal)[0]
    ic = ic[rng.permutation(len(ic))[:IMPORTANCE_SAMPLE]]
    for kk, rec in fits.items():
        C, names = rec["win"][0].contributions(X[ic])
        a = np.abs(C).mean(0)
        imp[kk] = {"feature": {n: float(v) for n, v in sorted(zip(names, a, strict=True), key=lambda x: -x[1])},
                   "group": group_importance(dict(zip(names, a, strict=True)))}
    mm.info = {"fit_date": str(fit_date), "H": H, "n_fit": int(fit.sum()), "n_cal": int(cal.sum()), "selection": hl,
               "selected": kind, "calibration": {"win": ciw, "loss": cil}, "threshold_info": tinfo,
               "params": {kk: {t: {x: y for x, y in rec[t][0].params.items() if x != "inner_logloss"} for t in ("win", "loss")}
                          for kk, rec in fits.items()},
               "importance": imp, "cal_take_share": float((er > thr).mean()), "extra": extra or []}
    return mm


def available_extra(s: Settings, as_of: date) -> list[str]:
    """The #1 / #5 features announced ready AND fed live (tech_factors / tech_breakouts rows within the last 10 / 14
    days): a feature the nightly scoring could not fill would be a train / serve skew, so it is left out of the fit."""
    out: list[str] = []
    for k, v in ST.parallel_features(s).items():
        table, col, days = ("tech_factors", "as_of_date", 10) if k == "b1" else ("tech_breakouts", "date", 14)
        lf = _scan(table)
        last = None
        if lf is not None:
            try:
                last = lf.select(pl.col(col).max()).collect().item()
            except Exception:  # noqa: BLE001
                last = None
        if last is not None and last >= as_of - timedelta(days=days):
            out += v["features"]
        else:
            log.info("meta extras %s skipped: %s not fed live (last %s)", k, table, last)
    return out


def group_of(feature: str) -> str:
    base = feature.split("__")[0]
    for g, cs in F.GROUPS.items():
        if base in cs:
            return g
    return F.EXTRA_GROUP if base.startswith("x_") else "other"


def group_importance(a: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    for n, v in a.items():
        g = group_of(n)
        out[g] = out.get(g, 0.0) + float(v)
    return dict(sorted(out.items(), key=lambda x: -x[1]))


def _append_incremental(s: Settings, as_of: date, rep: dict[str, Any]) -> None:
    """Append setup origins whose labels completed after the dataset's last origin (per horizon)."""
    from app.techprob import history as Hh  # noqa: PLC0415
    from app.techprob import job as TJ  # noqa: PLC0415

    ev = _scan("tech_prob_setup_events")
    surf, sfit = TJ.latest_surface()
    extra = available_extra(s, as_of)
    if ev is None or surf is None:
        rep["incremental"] = "no setup events / surface"
        return
    for H in HORIZONS:
        f = dataset_dir(s) / f"ds_h{H}.parquet"
        if not f.exists():
            continue
        last = pl.scan_parquet(f).select(pl.col("date").max()).collect().item()
        S = ev.filter((pl.col("date") > last) & (pl.col("direction") != 0) & pl.col(f"out_{H}").is_not_null()).collect()
        if S.is_empty():
            rep.setdefault("incremental", {})[f"h{H}"] = 0
            continue
        T = DS.t2_from_surface(S, surf, H, COSTS)
        rows = []
        for (rg,), g in T.group_by(["region"]):
            ids = sorted(g["security_id"].unique().to_list())
            start = g["date"].min() - timedelta(days=NIGHTLY_HISTORY_DAYS)
            items = Hh.load_inputs(ids, str(rg), start, as_of, s)
            bars = DS.bar_rows(items, g.select("security_id", "date", "direction").unique(["security_id", "date"]))
            if bars.is_empty():
                continue
            X = g.join(bars, on=["security_id", "date"], how="inner")
            ctx = DS.Context(start, as_of, s, peer_source="live", as_of=as_of, extra=extra)
            rows.append(ctx.join(X, ids))
        if not rows:
            continue
        D_ = DS.finalize(pl.concat(rows, how="diagonal_relaxed"), H).with_columns(pl.lit(sfit).alias("t2_source"))
        old = pl.read_parquet(f)
        new = pl.concat([old, D_.select([c for c in old.columns if c in D_.columns])], how="diagonal_relaxed")
        tmp = f.with_suffix(".tmp.parquet")
        new.write_parquet(tmp)
        tmp.replace(f)
        rep.setdefault("incremental", {})[f"h{H}"] = D_.height


def run_fit(settings: Settings | None, as_of: date | None, params: dict[str, Any]) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    mode = str(params.get("mode", "incremental"))
    rep: dict[str, Any] = {"as_of": str(as_of), "mode": mode}
    dd = dataset_dir(s)
    if mode == "seed" or not any((dd / f"ds_h{H}.parquet").exists() for H in HORIZONS):
        src = Path(params.get("seed_dir") or seed_dir(s))
        for H in HORIZONS:
            if (src / f"ds_h{H}.parquet").exists():
                shutil.copyfile(src / f"ds_h{H}.parquet", dd / f"ds_h{H}.parquet")
        rep["seeded_from"] = str(src)
    elif mode == "incremental":
        try:
            _append_incremental(s, as_of, rep)
        except Exception as e:  # noqa: BLE001 - a failed append must not block the refit on the existing rows
            log.warning("tech_meta_fit incremental append failed: %s", e)
            rep["incremental_error"] = str(e)[:300]
    extra = available_extra(s, as_of)
    rep["extra"] = extra
    art: dict[str, Any] = {"fit_date": str(as_of), "models": {}, "gate": load_gate(s), "extra": extra}
    for H in [int(h) for h in params.get("horizons", HORIZONS)]:
        f = dd / f"ds_h{H}.parquet"
        if not f.exists():
            continue
        df = pl.read_parquet(f)
        mm = fit_production(df, H, as_of, extra)
        art["models"][H] = mm
        rep[f"h{H}"] = {k: mm.info[k] for k in ("n_fit", "n_cal", "selected", "cal_take_share")} | {"threshold": mm.threshold}
        del df
    if not art["models"]:
        rep["note"] = "no training rows"
        return rep
    d_out = model_root(s) / as_of.isoformat()
    d_out.mkdir(parents=True, exist_ok=True)
    with open(d_out / "meta_model.pkl", "wb") as fh:
        pickle.dump(art, fh)
    (d_out / "meta.json").write_text(json.dumps({"fit_date": str(as_of), "models": {H: m.info for H, m in art["models"].items()}},
                                                indent=1, default=str), encoding="utf-8")
    dirs = sorted(p for p in model_root(s).iterdir() if p.is_dir() and p.name != "dataset")
    for p in dirs[:-KEEP_FITS]:
        shutil.rmtree(p, ignore_errors=True)
    rep["tech_meta_gates"] = write_gates(s, as_of, art["gate"])
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"tech_meta_fit": rep})
    return rep


def write_gates(s: Settings, fit_date: date, gate: dict[str, Any]) -> int:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    rows = []
    for o in OUTPUTS:
        g = (gate.get("outputs") or {}).get(o) or {}
        v = g.get("verdict") or "not_validated"
        rows.append({"fit_date": fit_date, "output": o, "verdict": v, "shown_as": "production" if v == "pass" else "experimental",
                     "ece": g.get("ece"), "max_regime_gap": g.get("max_regime_gap"), "beats_baselines": g.get("beats_baselines"),
                     "economic_t": g.get("economic_t"), "summary": g.get("summary"), "detail": json.dumps(g, default=str),
                     "validated_at": gate.get("generated_at"), "computed_at": datetime.now(UTC)})
    new = pl.DataFrame(rows, infer_schema_length=None)
    try:
        old = read_table("tech_meta_gates")
        keep = sorted(set(old["fit_date"].to_list()) - {fit_date})[-(KEEP_FITS + 1):]
        new = pl.concat([old.filter(pl.col("fit_date").is_in(keep)), new], how="diagonal_relaxed")
    except Exception:  # noqa: BLE001 - first run
        pass
    return write_table(new, "tech_meta_gates", mode="overwrite", settings=s)


def run_fit_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_fit(ctx.settings, ctx.as_of, ctx.params or {})
    ok = any(k.startswith("h") and isinstance(v, dict) for k, v in rep.items())
    return JobResult(status="success" if ok else "failed", rows_written={"tech_meta_gates": rep.get("tech_meta_gates", 0)},
                     entities_ok=sum(1 for k in rep if k.startswith("h") and k[1:].isdigit()), entities_failed=0,
                     watermark=rep.get("as_of"), message=json.dumps(rep, default=str)[:4000])


# ------------------------------------------------------------------------------------------------ scoring


def _drivers(C: np.ndarray, names: list[str], X: np.ndarray, cols: list[str], k: int = 5) -> list[list[dict[str, Any]]]:
    from app.techmeta import serve as SV  # noqa: PLC0415

    out = []
    for i in range(C.shape[0]):
        order = [j for j in np.argsort(-np.abs(C[i])) if group_of(names[j]) not in ("t2",) and abs(C[i, j]) > 1e-6][:k]
        row = []
        for j in order:
            base = names[j].split("__")[0]
            v = X[i, cols.index(base)] if base in cols else None
            row.append({"feature": names[j], "group": group_of(names[j]), "label": SV.feature_label(names[j]),
                        "value": None if v is None or not math.isfinite(v) else float(v), "contribution": float(C[i, j])})
        out.append(row)
    return out


def score_frame(mm: M.MetaModel, D: pl.DataFrame) -> pl.DataFrame:
    """Predictions + decisions + drivers for a model frame of one horizon (columns as in DS.finalize)."""
    X = F.matrix(D, mm.cols)
    p = mm.predict(X)
    rr, cost, mtm = D["rr_close"].to_numpy(), D["cost_r"].to_numpy(), D["mtm_r"].to_numpy()
    er = M.expected_r(p["p_win"], p["p_loss"], rr, cost, mtm)
    kf = M.kelly_fraction(p["p_win"], p["p_loss"], rr, mtm, er)
    take = er > mm.threshold
    rel = np.where(take, np.clip(kf / mm.size_ref, 0.0, 3.0), 0.0) if mm.size_ref else take.astype(float)
    C, names = mm.win.contributions(X)
    drv = _drivers(C, names, X, mm.cols)
    adj = mm.win.margin(X) - np.where(np.isfinite(X[:, mm.cols.index("t2_logit_pw")]), X[:, mm.cols.index("t2_logit_pw")], 0.0)
    return D.with_columns(pl.Series("m_p_win", p["p_win"]), pl.Series("m_p_loss", p["p_loss"]), pl.Series("m_p_neither", p["p_neither"]),
                          pl.Series("m_expected_r", er), pl.Series("m_take", take), pl.Series("m_kelly", np.where(take, kf, 0.0)),
                          pl.Series("m_size_rel", rel), pl.Series("m_logit_adj", adj),
                          pl.Series("m_drivers", [json.dumps(r) for r in drv]))


def live_frame(s: Settings, a_of: date, tp: pl.DataFrame, setups: pl.DataFrame, cancel: Any = None,
               extra: list[str] | None = None) -> pl.DataFrame:
    """Feature rows (one per security x horizon) for the nightly scoring of the latest T2 setups."""
    from app.techprob import job as TJ  # noqa: PLC0415
    from app.technicals.data import load_meta  # noqa: PLC0415

    sids = sorted(tp["security_id"].unique().to_list())
    meta = load_meta(sids)
    reg = {r["security_id"]: (r.get("region") or "OTHER", r.get("sector_id") or "NA") for r in meta.iter_rows(named=True)}
    env = _scan("conformal_envelopes")
    eda: dict[str, int | None] = {}
    if env is not None:
        e = (env.filter((pl.col("as_of_date") <= a_of) & (pl.col("as_of_date") >= a_of - timedelta(days=10)))
             .select("as_of_date", "security_id", "earn_days_ahead").collect().sort("as_of_date").unique("security_id", keep="last"))
        eda = {r["security_id"]: (int(r["earn_days_ahead"]) if r["earn_days_ahead"] is not None else None) for r in e.iter_rows(named=True)}
    start = a_of - timedelta(days=NIGHTLY_HISTORY_DAYS)
    dirn = tp.group_by("security_id").agg(pl.col("direction").first())
    origins = dirn.with_columns(pl.lit(a_of).alias("date"))
    parts = []
    ctx = DS.Context(start, a_of, s, peer_source="live", as_of=a_of, extra=extra)
    for c0 in range(0, len(sids), NIGHTLY_CHUNK):
        if cancel:
            cancel()
        chunk = sids[c0:c0 + NIGHTLY_CHUNK]
        items = TJ._load_bars(chunk, start, a_of, s)  # noqa: SLF001
        sig = {}
        for si in items:
            b = si.bars
            if b.n >= 60 and b.dates[-1] == np.datetime64(a_of, "D"):
                sig[b.security_id] = True
        items = [si for si in items if si.bars.security_id in sig]
        bars = DS.bar_rows(items, origins.filter(pl.col("security_id").is_in(chunk)), live_ke={k: eda.get(k) for k in chunk})
        if bars.is_empty():
            continue
        X = bars.join(origins, on=["security_id", "date"], how="inner").with_columns(
            pl.col("security_id").replace_strict({k: v[0] for k, v in reg.items()}, default="OTHER").alias("region"),
            pl.col("security_id").replace_strict({k: v[1] for k, v in reg.items()}, default="NA").alias("sector_id"),
            pl.col("security_id").replace_strict({k: (v if v is not None else 0) for k, v in eda.items()}, default=0,
                                                 return_dtype=pl.Int64).alias("earn_days_ahead"))
        parts.append(ctx.join(X, chunk))
    if not parts:
        return pl.DataFrame()
    Xall = pl.concat(parts, how="diagonal_relaxed")
    st = setups.select("security_id", "entry", "trend", "structure_state", "score_trend", "score_structure", "score_location",
                       "score_momentum")
    T = tp.rename({"horizon": "H", "p_win": "pw", "p_loss": "pl", "p_neither": "pn", "expected_r_mtm": "exp_r_mtm",
                   "reward_risk_close": "rr_close", "earn_window": "earn"})
    T = T.with_columns(pl.col("vol_regime").replace_strict({"low": 0, "mid": 1, "high": 2}, default=1, return_dtype=pl.Int64),
                       pl.when(pl.col("pn") > 1e-9).then((pl.col("exp_r_mtm") - pl.col("expected_r")) / pl.col("pn"))
                       .otherwise(0.0).alias("mtm_r"))
    T = T.drop([c for c in ("entry",) if c in T.columns]).join(st, on="security_id", how="left")
    D_ = T.join(Xall.drop("direction"), on="security_id", how="inner").with_columns(pl.lit(a_of).alias("date"))
    return D_.rename({"sigma_d_bar": "sigma_d"}).with_columns(pl.col("sigma_d").cast(pl.Float64))


def run_score(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None,
              write: bool = True, art: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    art = art or latest_artifact(s)
    if art is None:
        return {"note": "no meta model fitted yet (run tech_meta_fit)"}
    lf = _scan("tech_prob_setups")
    if lf is None:
        return {"note": "tech_prob_setups missing"}
    if as_of is not None:
        lf = lf.filter(pl.col("as_of_date") <= as_of)
    a_of = lf.select(pl.col("as_of_date").max()).collect().item()
    if a_of is None:
        return {"note": "tech_prob_setups empty"}
    tp = lf.filter((pl.col("as_of_date") == a_of) & pl.col("horizon").is_in(list(art["models"]))).collect()
    if params.get("tickers"):
        tp = tp.filter(pl.col("security_id").is_in(list(params["tickers"])))
    su = _scan("tech_setups")
    setups = su.filter(pl.col("as_of_date") == a_of).collect() if su is not None else pl.DataFrame()
    D_ = live_frame(s, a_of, tp, setups, cancel, art.get("extra"))
    if D_.is_empty():
        return {"as_of": str(a_of), "note": "no feature rows"}
    gate = art.get("gate") or load_gate(s)
    now = datetime.now(UTC)
    rows = []
    for H, mm in art["models"].items():
        sub = D_.filter(pl.col("H") == H)
        if sub.is_empty():
            continue
        sub = F.base_columns(sub)
        sc = score_frame(mm, sub)
        ctx_cols = [c for c in [*F.all_features(F.CONTEXT), *F.all_features([F.EXTRA_GROUP], extra=art.get("extra"))]
                    if c in sc.columns]
        g_meta, g_dec = verdict(gate, f"meta_h{H}"), verdict(gate, f"decision_h{H}")
        for r in sc.iter_rows(named=True):
            dom = r["region"] in F.REGIONS and not str(r["security_id"]).startswith(("ETF:", "IDX:"))   # validated: stocks
            rows.append({
                "as_of_date": a_of, "security_id": r["security_id"], "horizon": int(H), "direction": int(r["direction"]),
                "region": r["region"], "in_domain": dom, "p_win": r["m_p_win"], "p_loss": r["m_p_loss"],
                "p_neither": r["m_p_neither"], "expected_r": r["m_expected_r"], "take": bool(r["m_take"]),
                "kelly_fraction": r["m_kelly"], "size_rel": r["m_size_rel"], "threshold": mm.threshold,
                "logit_adj": r["m_logit_adj"], "t2_p_win": r["pw"], "t2_p_loss": r["pl"], "t2_expected_r": r["expected_r"],
                "t2_expected_r_mtm": r["exp_r_mtm"], "reward_risk_close": r["rr_close"], "cost_r": r["cost_r"],
                "mtm_r": r["mtm_r"], "drivers": r["m_drivers"],
                "features": json.dumps({"_direction": int(r["direction"]),
                                        **{c: (None if r.get(c) is None or not math.isfinite(float(r[c])) else round(float(r[c]), 6))
                                           for c in ctx_cols}}),
                "model_kind": mm.kind, "fit_date": art["fit_date"],
                "gate": "production" if (g_meta == "pass" and dom) else "experimental",
                "gate_decision": "production" if (g_dec == "pass" and g_meta == "pass" and dom) else "experimental",
                "computed_at": now})
    rep: dict[str, Any] = {"as_of": str(a_of), "securities": D_["security_id"].n_unique(), "rows": len(rows),
                           "fit_date": art["fit_date"]}
    if write and rows:
        from app.core.storage import read_table, write_table  # noqa: PLC0415
        from app.technicals.levels import month_end_retention  # noqa: PLC0415

        df = pl.DataFrame(rows, infer_schema_length=None)
        try:
            keep = month_end_retention(read_table("tech_meta_setups"), a_of)
        except Exception:  # noqa: BLE001 - first run
            keep = pl.DataFrame()
        if not keep.is_empty():
            df = pl.concat([keep.filter(pl.col("as_of_date") != a_of), df], how="diagonal_relaxed")
        rep["tech_meta_setups"] = write_table(df, "tech_meta_setups", mode="overwrite", settings=s)
    elif not write:
        rep["_rows"] = rows
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"tech_meta_score": {k: v for k, v in rep.items() if k != "_rows"}})
    return rep


def run_score_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_score(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = rep.get("rows", 0) > 0
    return JobResult(status="success" if ok else ("skipped" if "note" in rep else "failed"),
                     rows_written={"tech_meta_setups": rep.get("tech_meta_setups", 0)},
                     entities_ok=rep.get("securities", 0), entities_failed=0, watermark=rep.get("as_of"),
                     message=json.dumps(rep, default=str)[:4000])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--mode", default="incremental")
    ap.add_argument("--as-of")
    ap.add_argument("--tickers")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    if a.fit:
        rep = run_fit(None, as_of, {"mode": a.mode})
    else:
        rep = run_score(None, as_of, {"tickers": a.tickers.split(",")} if a.tickers else {}, write=not a.dry_run)
        rep.pop("_rows", None)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
