"""Insider models (job `insider_models_fit`, weekly; owner signals-alt).

(a) IsolationForest on per-transaction features (app.analytics.insider.TXN_MODEL_FEATURES) -> anomaly_score 0..1
    (empirical CDF of the training anomaly scores; 1 = most anomalous). Unsupervised "strange moves".
(b) XGBoost on filing-day insider EVENTS (issuer x knowledge_date aggregates of open-market trades):
      cls_3m : P(3M excess return > 0)          reg_1m / reg_3m : expected 1M / 3M excess return
    Excess return = security total return minus its sector SPDR ETF (SPY when the sector is unknown or the ETF did
    not trade yet), from close(D+1) to close(D+1+h) with h = 21 / 63 trading days (ARCHITECTURE §7 label rule).
    Walk-forward: expanding window, yearly test blocks; PURGE training events whose label_end_date >= test_start and
    an EMBARGO gap of `embargo_days` calendar days before each test block. OOS metrics: AUC, pooled and monthly rank
    IC (Spearman), decile spread (D10-D1 mean realized excess), hit rate, plus the rule-based insider score's IC as a
    benchmark. TreeSHAP (xgboost pred_contribs) mean |SHAP| on the OOS rows.

Artifacts: data/models/insider/<version>/{iforest.joblib, xgb_cls_3m.json, xgb_reg_1m.json, xgb_reg_3m.json,
meta.json, oos_predictions.parquet}; data/models/insider/CURRENT holds the active version. model_runs rows are written
for model_type insider_xgb (one per horizon) and insider_iforest.

PIT use in insider_signals: for an event inside the walk-forward OOS range the OOS prediction is used; the final model
(trained on everything) is only applied to events filed after its training window.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.nlp.common import MARKET_BENCHMARK, load_prices, security_map, sector_benchmark

log = get_logger(__name__)

HORIZONS = {"1M": 21, "3M": 63}
EVENT_FEATURES = [
    "direction", "log_buy_value", "log_sell_value", "log_net_value_to_adv", "n_insiders", "n_buyers", "n_sellers",
    "max_role_w_buy", "max_role_w_sell", "c_suite_buy", "c_suite_sell", "n_same_dir_30d_max", "first_buy_any",
    "share_10b5_1", "share_routine", "filing_lag_mean", "holding_change_max", "holding_change_min",
    "size_vs_hist_max", "ret_21", "ret_63", "ret_126", "drawdown_252", "vol_63", "log_adv20",
    "issuer_buys_365d", "issuer_sells_365d",
]
XGB_PARAMS = {"max_depth": 3, "learning_rate": 0.03, "n_estimators": 250, "subsample": 0.7, "colsample_bytree": 0.7,
              "min_child_weight": 200, "reg_lambda": 10.0, "tree_method": "hist", "n_jobs": 16}
FEATURE_SET_VERSION = "insider_ev_v2"


def models_root(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "models" / "insider"


# ================================================================================================ events


def event_frame(feat: pl.DataFrame) -> pl.DataFrame:
    """Filing-day events: one row per (issuer_key, knowledge_date) with aggregated open-market activity."""
    if feat.is_empty():
        return pl.DataFrame()
    buy = pl.col("direction") == 1
    sell = pl.col("direction") == -1
    ev = feat.group_by("issuer_key", "knowledge_date").agg(
        pl.col("issuer_id").drop_nulls().first(), pl.col("security_id").drop_nulls().first(),
        pl.col("value_usd_est").filter(buy).sum().alias("buy_value"),
        pl.col("value_usd_est").filter(sell).sum().alias("sell_value"),
        pl.col("insider_id").n_unique().alias("n_insiders"),
        pl.col("insider_id").filter(buy).n_unique().alias("n_buyers"),
        pl.col("insider_id").filter(sell).n_unique().alias("n_sellers"),
        pl.col("role_w").filter(buy).max().fill_null(0.0).alias("max_role_w_buy"),
        pl.col("role_w").filter(sell).max().fill_null(0.0).alias("max_role_w_sell"),
        (buy & pl.col("role_norm").is_in(["ceo", "cfo"])).any().cast(pl.Float64).alias("c_suite_buy"),
        (sell & pl.col("role_norm").is_in(["ceo", "cfo"])).any().cast(pl.Float64).alias("c_suite_sell"),
        pl.col("n_same_dir_30d").max().cast(pl.Float64).alias("n_same_dir_30d_max"),
        pl.col("first_buy_2y").max().alias("first_buy_any"),
        pl.col("is_10b5_1_f").mean().alias("share_10b5_1"),
        pl.col("routine").mean().alias("share_routine"),
        pl.col("filing_lag").mean().alias("filing_lag_mean"),
        pl.col("holding_change").max().alias("holding_change_max"),
        pl.col("holding_change").min().alias("holding_change_min"),
        pl.col("size_vs_hist").max().alias("size_vs_hist_max"),
        pl.col("ret_21").first(), pl.col("ret_63").first(), pl.col("ret_126").first(),
        pl.col("drawdown_252").first(), pl.col("vol_63").first(), pl.col("adv20_usd").first(),
    )
    ev = ev.with_columns(
        (pl.col("buy_value") - pl.col("sell_value")).alias("net_value"),
        pl.col("buy_value").log1p().alias("log_buy_value"), pl.col("sell_value").log1p().alias("log_sell_value"),
        pl.col("adv20_usd").log1p().alias("log_adv20"),
    ).with_columns(
        pl.col("net_value").sign().cast(pl.Float64).alias("direction"),
        (pl.col("net_value").sign() * (pl.col("net_value").abs() / pl.col("adv20_usd")).log1p())
        .alias("log_net_value_to_adv"),
    )
    # issuer activity in the prior 365 days (number of buy / sell filing days, PIT by knowledge_date)
    ev = ev.sort(["issuer_key", "knowledge_date"])
    roll = (ev.select("issuer_key", "knowledge_date", (pl.col("buy_value") > 0).cast(pl.Int32).alias("__b"),
                      (pl.col("sell_value") > 0).cast(pl.Int32).alias("__s"))
            .rolling(index_column="knowledge_date", period="365d", group_by="issuer_key")
            .agg((pl.col("__b").sum() - pl.col("__b").last()).alias("issuer_buys_365d"),
                 (pl.col("__s").sum() - pl.col("__s").last()).alias("issuer_sells_365d")))
    ev = ev.join(roll, on=["issuer_key", "knowledge_date"], how="left")
    return ev.with_columns([pl.col(c).cast(pl.Float64) for c in EVENT_FEATURES])


def add_labels(ev: pl.DataFrame, prices: pl.DataFrame, sectors: pl.DataFrame | None = None) -> pl.DataFrame:
    """Forward excess returns vs sector ETF (fallback SPY): fwd_excess_1m/3m, label_end_3m. Close(D+1)->close(D+1+h)."""
    if ev.is_empty() or prices.is_empty():
        return ev
    px = prices.sort(["security_id", "date"]).with_columns(pl.int_range(pl.len()).over("security_id").alias("idx"))
    e = ev.filter(pl.col("security_id").is_not_null()).with_columns(
        (pl.col("knowledge_date") + timedelta(days=1)).alias("__t"))
    if sectors is not None and sectors.height:
        e = e.join(sectors.select("issuer_id", "sector_id"), on="issuer_id", how="left")
    else:
        e = e.with_columns(pl.lit(None, pl.Utf8).alias("sector_id"))
    e = e.with_columns(pl.col("sector_id").map_elements(sector_benchmark, return_dtype=pl.Utf8,
                                                        skip_nulls=False).alias("bench"))
    e = e.sort("__t").join_asof(px.select("security_id", pl.col("date").alias("d0"), pl.col("adj_close").alias("p0"),
                                          "idx").sort("d0"),
                                left_on="__t", right_on="d0", by="security_id", strategy="forward",
                                tolerance=timedelta(days=7), check_sortedness=False)
    bench = px.filter(pl.col("security_id").str.starts_with("ETF:")).select(
        pl.col("security_id").alias("bench"), pl.col("date").alias("__bd"), pl.col("adj_close").alias("__bp"))
    for name, h in HORIZONS.items():
        tgt = px.select("security_id", (pl.col("idx") - h).alias("idx"), pl.col("date").alias(f"d_{name}"),
                        pl.col("adj_close").alias(f"p_{name}"))
        e = e.join(tgt, on=["security_id", "idx"], how="left")
        e = e.with_columns(pl.when((pl.col(f"d_{name}") - pl.col("d0")).dt.total_days() <= int(h * 1.6 + 10))
                           .then(pl.col(f"p_{name}")).otherwise(None).alias(f"p_{name}"))
    # sector ETF only if it traded at the base date, else SPY (e.g. XLC before 2018-06, XLRE before 2015-10)
    e = _bench_join(e, bench, "d0", "b0")
    e = e.with_columns(pl.when(pl.col("b0").is_null()).then(pl.lit(MARKET_BENCHMARK)).otherwise(pl.col("bench"))
                       .alias("bench")).drop("b0")
    e = _bench_join(e, bench, "d0", "b0")
    for name in HORIZONS:
        e = _bench_join(e, bench, f"d_{name}", f"b_{name}")
        e = e.with_columns(
            ((pl.col(f"p_{name}") / pl.col("p0") - 1) - (pl.col(f"b_{name}") / pl.col("b0") - 1))
            .alias(f"fwd_excess_{name.lower()}"))
    return e.with_columns(pl.col("d_3M").alias("label_end_3m"), pl.col("d_1M").alias("label_end_1m")).drop(
        [c for c in e.columns if c.startswith(("p_", "b_", "d_")) or c in ("__t", "idx", "p0", "b0", "d0")])


def _bench_join(e: pl.DataFrame, bench: pl.DataFrame, date_col: str, out: str) -> pl.DataFrame:
    """Close of the row's benchmark (`bench` column) on or before `date_col` (<= 7 days stale)."""
    e = e.with_row_index("__r")
    has = e.filter(pl.col(date_col).is_not_null()).sort(date_col)
    j = has.join_asof(bench.sort("__bd"), left_on=date_col, right_on="__bd", by="bench", strategy="backward",
                      tolerance=timedelta(days=7), check_sortedness=False).select("__r", pl.col("__bp").alias(out))
    return e.join(j, on="__r", how="left").drop("__r")


# ================================================================================================ metrics


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 5:
        return float("nan")
    ra = pl.Series(a).rank().to_numpy()
    rb = pl.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return float("nan")
    return float(np.corrcoef(ra, rb)[0, 1])


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score  # noqa: PLC0415

    if len(np.unique(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, p))


def oos_metrics(df: pl.DataFrame, pred: str, target: str, prob: str | None = None) -> dict[str, Any]:
    d = df.filter(pl.col(pred).is_not_null() & pl.col(target).is_not_null())
    if d.height < 50:
        return {"n": d.height}
    # winsorize realized returns at 0.5/99.5% (yfinance bad ticks otherwise dominate decile means)
    d = d.with_columns(_winsor(d, target, 0.005, 0.995))
    y = d.get_column(target).to_numpy()
    p = d.get_column(pred).to_numpy()
    out: dict[str, Any] = {"n": d.height, "rank_ic_pooled": _spearman(p, y)}
    m = d.with_columns(pl.col("knowledge_date").dt.truncate("1mo").alias("__m")).group_by("__m").agg(
        pl.map_groups([pl.col(pred), pl.col(target)], lambda s: _spearman(s[0].to_numpy(), s[1].to_numpy()),
                      return_dtype=pl.Float64, returns_scalar=True).alias("ic"), pl.len().alias("n")).filter(
        pl.col("n") >= 20)
    ics = m.get_column("ic").drop_nans().drop_nulls().to_numpy()
    if len(ics):
        out["rank_ic_monthly_mean"] = float(np.mean(ics))
        out["rank_ic_monthly_ir"] = float(np.mean(ics) / np.std(ics)) if np.std(ics) > 0 else None
        out["rank_ic_tstat"] = _nw_t(ics)
        out["hit_rate"] = float(np.mean(ics > 0))
        out["n_months"] = int(len(ics))
    dec = d.with_columns((pl.col(pred).rank("ordinal") * 10 / (d.height + 1)).floor().clip(0, 9).alias("__dec"))
    dm = dec.group_by("__dec").agg(pl.col(target).mean()).sort("__dec")
    vals = dict(zip(dm["__dec"].to_list(), dm[target].to_list(), strict=True))
    out["decile_means"] = [round(vals.get(i, float("nan")), 5) for i in range(10)]
    out["decile_spread"] = float(vals.get(9, np.nan) - vals.get(0, np.nan))
    if prob:
        out["auc"] = _auc((y > 0).astype(int), d.get_column(prob).to_numpy())
    out["base_rate_pos"] = float(np.mean(y > 0))
    return out


def _nw_t(x: np.ndarray, lags: int = 3) -> float | None:
    """Newey-West t-stat of the mean (monthly ICs of overlapping 3M labels -> lag 3)."""
    n = len(x)
    if n < 6:
        return None
    e = x - x.mean()
    s = float(np.dot(e, e) / n)
    for k in range(1, min(lags, n - 1) + 1):
        w = 1 - k / (lags + 1)
        s += 2 * w * float(np.dot(e[k:], e[:-k]) / n)
    se = math.sqrt(max(s, 1e-18) / n)
    return float(x.mean() / se)


# ================================================================================================ training


def _xy(df: pl.DataFrame, target: str | None = None) -> tuple[np.ndarray, np.ndarray | None]:
    X = df.select(EVENT_FEATURES).to_numpy().astype(np.float32)
    X[~np.isfinite(X)] = np.nan
    y =df.get_column(target).to_numpy().astype(np.float32) if target else None
    return X, y


def _winsor(df: pl.DataFrame, col: str, lo: float = 0.01, hi: float = 0.99) -> pl.Expr:
    a, b = df.get_column(col).quantile(lo), df.get_column(col).quantile(hi)
    return pl.col(col).clip(a, b)


def _reg_target(tr: pl.DataFrame, h: str) -> np.ndarray:
    """Regression label: winsorized excess return minus its filing-month cross-sectional mean (removes market/regime
    level, keeps return units). Only uses rows of the training set itself."""
    c = f"fwd_excess_{h}"
    t = tr.with_columns(_winsor(tr, c), pl.col("knowledge_date").dt.truncate("1mo").alias("__m"))
    return t.select(pl.col(c) - pl.col(c).mean().over("__m")).to_series().to_numpy()


def walk_forward(ev: pl.DataFrame, first_test_year: int = 2019, embargo_days: int = 14,
                 params: dict[str, Any] | None = None, log_fn=None) -> tuple[pl.DataFrame, list[dict[str, Any]]]:
    """Expanding-window yearly walk-forward. Returns (OOS predictions frame, fold metadata)."""
    import xgboost as xgb  # noqa: PLC0415

    params = {**XGB_PARAMS, **(params or {})}
    lab = ev.filter(pl.col("fwd_excess_3m").is_not_null())
    last_year = lab.get_column("knowledge_date").max().year
    preds = []
    folds = []
    for yr in range(first_test_year, last_year + 1):
        ts, te = date(yr, 1, 1), date(yr, 12, 31)
        test = lab.filter((pl.col("knowledge_date") >= ts) & (pl.col("knowledge_date") <= te))
        train = lab.filter((pl.col("knowledge_date") < ts - timedelta(days=embargo_days))
                           & (pl.col("label_end_3m") < ts))
        if test.height < 200 or train.height < 2000:
            continue
        t0 = time.perf_counter()
        Xtr, _ = _xy(train)
        Xte, _ = _xy(test)
        out = test.select("issuer_key", "knowledge_date", "fwd_excess_1m", "fwd_excess_3m", "direction", "rule_raw")
        y_cls = (train.get_column("fwd_excess_3m").to_numpy() > 0).astype(np.int32)
        clf = xgb.XGBClassifier(objective="binary:logistic", eval_metric="auc", **params)
        clf.fit(Xtr, y_cls)
        out = out.with_columns(pl.Series("prob_up_3m", clf.predict_proba(Xte)[:, 1]))
        for h in ("1m", "3m"):
            tr = train.filter(pl.col(f"fwd_excess_{h}").is_not_null())
            y = _reg_target(tr, h)
            reg = xgb.XGBRegressor(objective="reg:squarederror", **params)
            reg.fit(_xy(tr)[0], y)
            out = out.with_columns(pl.Series(f"pred_excess_{h}", reg.predict(Xte)))
        preds.append(out.with_columns(pl.lit(yr).alias("fold")))
        f = {"fold": yr, "train_start": str(train["knowledge_date"].min()), "train_end": str(train["knowledge_date"].max()),
             "test_start": str(ts), "test_end": str(min(te, test["knowledge_date"].max())), "n_train": train.height,
             "n_test": test.height, "seconds": round(time.perf_counter() - t0, 1),
             "auc_3m": _auc((test["fwd_excess_3m"].to_numpy() > 0).astype(int), out["prob_up_3m"].to_numpy()),
             "rank_ic_3m": _spearman(out["pred_excess_3m"].to_numpy(), test["fwd_excess_3m"].to_numpy())}
        folds.append(f)
        if log_fn:
            log_fn(f)
    return (pl.concat(preds) if preds else pl.DataFrame()), folds


def fit_iforest(feat: pl.DataFrame, max_rows: int = 300_000, seed: int = 7) -> dict[str, Any]:
    from sklearn.ensemble import IsolationForest  # noqa: PLC0415

    X = _txn_matrix(feat)
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(X), size=min(max_rows, len(X)), replace=False) if len(X) else np.array([], dtype=int)
    Xs = X[idx]
    model = IsolationForest(n_estimators=200, max_samples=min(4096, max(len(Xs), 1)), contamination="auto", random_state=seed, n_jobs=8)
    model.fit(Xs)
    ref = np.sort(-model.score_samples(Xs))  # higher = more anomalous
    return {"model": model, "ref": ref, "medians": np.nanmedian(X, axis=0) if len(X) else None}


def _txn_matrix(feat: pl.DataFrame, medians: np.ndarray | None = None) -> np.ndarray:
    from app.analytics.insider import TXN_MODEL_FEATURES  # noqa: PLC0415

    X = feat.select([pl.col(c).cast(pl.Float64) for c in TXN_MODEL_FEATURES]).to_numpy()
    if medians is None:
        medians = np.nanmedian(X, axis=0) if len(X) else np.zeros(X.shape[1])
        medians = np.where(np.isnan(medians), 0.0, medians)
    inds = np.where(np.isnan(X))
    X[inds] = np.take(medians, inds[1])
    return X


def score_anomaly(feat: pl.DataFrame, models: dict[str, Any] | None = None) -> pl.DataFrame:
    models = models if models is not None else load_models()
    if not models or "iforest" not in models:
        raise RuntimeError("no insider IsolationForest model persisted yet (run insider_models_fit)")
    m = models["iforest"]
    X = _txn_matrix(feat, m.get("medians"))
    raw = -m["model"].score_samples(X)
    pct = np.searchsorted(m["ref"], raw, side="right") / max(len(m["ref"]), 1)
    return feat.with_columns(pl.Series("anomaly_score", np.round(pct, 4)))


# ================================================================================================ persistence


def save_models(version: str, iforest: dict[str, Any], boosters: dict[str, Any], meta: dict[str, Any],
                oos: pl.DataFrame, settings: Settings | None = None) -> Path:
    import joblib  # noqa: PLC0415

    root = models_root(settings)
    d = root / version
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump(iforest, d / "iforest.joblib")
    for name, m in boosters.items():
        m.save_model(str(d / f"xgb_{name}.json"))
    (d / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    if oos.height:
        oos.write_parquet(d / "oos_predictions.parquet")
    tmp = root / "CURRENT.tmp"
    tmp.write_text(version, encoding="utf-8")
    tmp.replace(root / "CURRENT")
    return d


def load_models(settings: Settings | None = None) -> dict[str, Any] | None:
    import joblib  # noqa: PLC0415

    root = models_root(settings)
    ptr = root / "CURRENT"
    if not ptr.exists():
        return None
    d = root / ptr.read_text(encoding="utf-8").strip()
    if not (d / "meta.json").exists():
        return None
    out: dict[str, Any] = {"meta": json.loads((d / "meta.json").read_text(encoding="utf-8")), "dir": d}
    if (d / "iforest.joblib").exists():
        out["iforest"] = joblib.load(d / "iforest.joblib")
    try:
        import xgboost as xgb  # noqa: PLC0415

        for name in ("cls_3m", "reg_1m", "reg_3m"):
            p = d / f"xgb_{name}.json"
            if p.exists():
                b = xgb.Booster()
                b.load_model(str(p))
                out[name] = b
    except Exception as e:  # noqa: BLE001
        log.warning("xgboost models not loadable: %s", e)
    if (d / "oos_predictions.parquet").exists():
        out["oos"] = pl.read_parquet(d / "oos_predictions.parquet")
    return out


def predict_events(ev: pl.DataFrame, models: dict[str, Any] | None) -> pl.DataFrame | None:
    """PIT predictions per event: OOS walk-forward prediction where available, else the final model only for events
    filed after its training window. Returns [issuer_key, knowledge_date, xgb_prob_up, pred_excess_3m]."""
    if not models or ev.is_empty() or "cls_3m" not in models:
        return None
    import xgboost as xgb  # noqa: PLC0415

    train_end = date.fromisoformat(models["meta"]["final_train_end"])
    post = ev.filter(pl.col("knowledge_date") > train_end)
    parts = []
    if post.height:
        dm = xgb.DMatrix(_xy(post)[0], feature_names=EVENT_FEATURES)
        part = post.select("issuer_key", "knowledge_date").with_columns(
            pl.Series("xgb_prob_up", models["cls_3m"].predict(dm)))
        if "reg_3m" in models:
            part = part.with_columns(pl.Series("pred_excess_3m", models["reg_3m"].predict(dm)))
        parts.append(part)
    oos = models.get("oos")
    if oos is not None and oos.height:
        parts.append(oos.select("issuer_key", "knowledge_date", pl.col("prob_up_3m").alias("xgb_prob_up"),
                                "pred_excess_3m"))
    if not parts:
        return None
    return pl.concat(parts, how="diagonal_relaxed").unique(["issuer_key", "knowledge_date"], keep="first")


# ================================================================================================ job


def _shap_top(booster: Any, X: np.ndarray, k: int = 15) -> list[dict[str, Any]]:
    import xgboost as xgb  # noqa: PLC0415

    contrib = booster.predict(xgb.DMatrix(X, feature_names=EVENT_FEATURES), pred_contribs=True)[:, :-1]
    imp = np.abs(contrib).mean(axis=0)
    order = np.argsort(-imp)[:k]
    sign = []
    for i in order:
        ok = np.isfinite(X[:, i])
        sign.append(float(np.corrcoef(X[ok, i], contrib[ok, i])[0, 1]) if ok.sum() > 10 and np.std(X[ok, i]) > 0
                    else 0.0)
    return [{"feature": EVENT_FEATURES[i], "importance": round(float(imp[i]), 6), "direction_corr": round(s, 3)}
            for i, s in zip(order, sign, strict=True)]


def _clean(o: Any) -> Any:
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        return _clean(float(o))
    return o


def fit_all(settings: Settings | None = None, first_test_year: int = 2019, embargo_days: int = 14,
            ctx: Any | None = None) -> dict[str, Any]:
    """Build features from the full insider history, fit IsolationForest + walk-forward XGBoost, persist."""
    import xgboost as xgb  # noqa: PLC0415

    from app.analytics.insider import (  # noqa: PLC0415
        assign_clusters,
        compute_rule_raw,
        load_open_market,
        price_features,
        transaction_features,
    )

    s = settings or get_settings()
    t0 = time.perf_counter()
    om = load_open_market(None, s)
    secs = om.get_column("security_id").drop_nulls().unique().to_list()
    prices = load_prices(secs + [f"ETF:{x}" for x in ("SPY", "XLK", "XLC", "XLY", "XLP", "XLV", "XLF", "XLI", "XLB",
                                                       "XLE", "XLU", "XLRE")],
                         om.get_column("knowledge_date").min() - timedelta(days=400), s)
    pf = price_features(prices.filter(~pl.col("security_id").str.starts_with("ETF:")))
    feat = assign_clusters(transaction_features(om, pf))
    t_feat = time.perf_counter() - t0
    if ctx:
        ctx.check_cancelled()
    # (a) IsolationForest on recent 6 years of transactions
    recent = feat.filter(pl.col("knowledge_date") >= feat.get_column("knowledge_date").max() - timedelta(days=6 * 365))
    iso = fit_iforest(recent)
    feat = score_anomaly(feat, {"iforest": iso})
    # (b) events + labels
    ev = event_frame(feat)
    ev = ev.join(compute_rule_raw(feat), on=["issuer_key", "knowledge_date"], how="left")
    ev = add_labels(ev, prices, security_map(s))
    lab = ev.filter(pl.col("fwd_excess_3m").is_not_null())
    log.info("insider events=%d labelled=%d features=%.0fs", ev.height, lab.height, t_feat)
    if ctx:
        ctx.check_cancelled()
    oos, folds = walk_forward(lab, first_test_year, embargo_days, log_fn=lambda f: log.info("fold %s", f))
    metrics: dict[str, Any] = {}
    if oos.height:
        metrics["all_3m"] = oos_metrics(oos, "pred_excess_3m", "fwd_excess_3m", "prob_up_3m")
        metrics["all_1m"] = oos_metrics(oos, "pred_excess_1m", "fwd_excess_1m")
        metrics["prob_vs_3m"] = oos_metrics(oos, "prob_up_3m", "fwd_excess_3m", "prob_up_3m")
        metrics["buys_3m"] = oos_metrics(oos.filter(pl.col("direction") > 0), "pred_excess_3m", "fwd_excess_3m",
                                         "prob_up_3m")
        metrics["sells_3m"] = oos_metrics(oos.filter(pl.col("direction") < 0), "pred_excess_3m", "fwd_excess_3m",
                                          "prob_up_3m")
        metrics["rule_score_3m_benchmark"] = oos_metrics(oos, "rule_raw", "fwd_excess_3m")
        metrics["rule_score_buys_3m_benchmark"] = oos_metrics(oos.filter(pl.col("direction") > 0), "rule_raw",
                                                              "fwd_excess_3m")
    # final models on all labelled events
    params = dict(XGB_PARAMS)
    X, _ = _xy(lab)
    clf = xgb.XGBClassifier(objective="binary:logistic", **params).fit(X, (lab["fwd_excess_3m"].to_numpy() > 0)
                                                                         .astype(np.int32))
    boosters = {"cls_3m": clf.get_booster()}
    for h in ("1m", "3m"):
        tr = lab.filter(pl.col(f"fwd_excess_{h}").is_not_null())
        reg = xgb.XGBRegressor(objective="reg:squarederror", **params).fit(
            _xy(tr)[0], _reg_target(tr, h))
        boosters[f"reg_{h}"] = reg.get_booster()
    shap_rows = oos if oos.height else lab
    Xs = _xy(lab.join(shap_rows.select("issuer_key", "knowledge_date"), on=["issuer_key", "knowledge_date"])
             .head(50_000))[0]
    shap_cls = _shap_top(boosters["cls_3m"], Xs)
    shap_reg = _shap_top(boosters["reg_3m"], Xs)
    version = datetime.now(UTC).strftime("v%Y%m%dT%H%M%S")
    meta = _clean({
        "version": version, "trained_at": datetime.now(UTC).isoformat(), "feature_set_version": FEATURE_SET_VERSION,
        "features": EVENT_FEATURES, "txn_features": __import__("app.analytics.insider", fromlist=["x"]).TXN_MODEL_FEATURES,
        "params": params, "cv_scheme": {"type": "expanding", "test_block": "calendar year", "first_test_year":
                                        first_test_year, "purge": "label_end_3m < test_start",
                                        "embargo_days": embargo_days},
        "labels": "excess total return vs sector SPDR ETF (SPY fallback), close(D+1)->close(D+1+h), h=21/63 sessions",
        "n_transactions": feat.height, "n_events": ev.height, "n_labelled": lab.height,
        "data_start": str(lab["knowledge_date"].min()), "final_train_end": str(lab["knowledge_date"].max()),
        "base_rate_3m": float((lab["fwd_excess_3m"].to_numpy() > 0).mean()), "folds": folds, "oos_metrics": metrics,
        "gate_passed": bool(((metrics.get("all_3m") or {}).get("rank_ic_tstat") or 0) > 2
                            and ((metrics.get("all_3m") or {}).get("auc") or 0) > 0.52),
        "gate_rule": "OOS monthly rank-IC Newey-West t > 2 and AUC > 0.52 (ARCHITECTURE §11 promotion rule); "
                     "if false the model probability is reported but NOT blended into signal_score",
        "shap_top_cls_3m": shap_cls, "shap_top_reg_3m": shap_reg, "seconds": round(time.perf_counter() - t0, 1),
    })
    d = save_models(version, iso, boosters, meta, oos, s)
    meta["artifact_dir"] = str(d)
    return meta


def model_run_rows(meta: dict[str, Any], run_id: str | None = None) -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = []
    m3 = (meta.get("oos_metrics") or {}).get("all_3m") or {}
    m1 = (meta.get("oos_metrics") or {}).get("all_1m") or {}
    folds = meta.get("folds") or []
    for hz, m, imp in (("3M", m3, meta.get("shap_top_reg_3m")), ("1M", m1, None)):
        rows.append({
            "model_run_id": f"M{meta['version'][1:]}INS{hz}", "model_type": "insider_xgb", "horizon": hz,
            "universe_id": "sec_form4_open_market", "feature_set_version": meta["feature_set_version"],
            "train_start": date.fromisoformat(meta["data_start"]), "train_end": date.fromisoformat(meta["final_train_end"]),
            "oos_start": date.fromisoformat(folds[0]["test_start"]) if folds else None,
            "oos_end": date.fromisoformat(folds[-1]["test_end"]) if folds else None,
            "cv_scheme": json.dumps(meta["cv_scheme"]), "params": json.dumps(meta["params"]),
            "ic_mean": None, "rank_ic_mean": m.get("rank_ic_monthly_mean"), "rank_ic_ir": m.get("rank_ic_monthly_ir"),
            "rank_ic_tstat": m.get("rank_ic_tstat"), "decile_spread_ann": None if m.get("decile_spread") is None
            else m["decile_spread"] * (12 / (3 if hz == "3M" else 1)),
            "long_short_sharpe": None, "turnover_monthly": None, "hit_rate": m.get("hit_rate"), "status": "completed",
            "is_production": bool(meta.get("gate_passed")), "artifact_path": f"../data/models/insider/{meta['version']}",
            "feature_importance": json.dumps(imp) if imp else None,
            "notes": f"OOS auc_3m={m3.get('auc')} pooled_rank_ic={m.get('rank_ic_pooled')} n={m.get('n')}",
            "created_at": now, "finished_at": now})
    rows.append({
        "model_run_id": f"M{meta['version'][1:]}INSIF", "model_type": "insider_iforest", "horizon": None,
        "universe_id": "sec_form4_open_market", "feature_set_version": "insider_txn_v1",
        "train_start": date.fromisoformat(meta["data_start"]), "train_end": date.fromisoformat(meta["final_train_end"]),
        "oos_start": None, "oos_end": None, "cv_scheme": json.dumps({"type": "unsupervised"}),
        "params": json.dumps({"n_estimators": 200, "max_samples": 4096, "rows": "last 6y, <=300k"}),
        "status": "completed", "is_production": True, "artifact_path": f"../data/models/insider/{meta['version']}",
        "notes": "anomaly_score = ECDF of -score_samples on the training sample", "created_at": now,
        "finished_at": now})
    return pl.DataFrame(rows, infer_schema_length=None)


def run(ctx):  # noqa: ANN001 - JobContext
    from app.core.storage import write_table  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    meta = fit_all(ctx.settings, int(p.get("first_test_year", 2019)), int(p.get("embargo_days", 14)), ctx)
    rows = {}
    try:
        new = model_run_rows(meta, ctx.run_id)
        from app.core.storage import scan_table  # noqa: PLC0415

        old = scan_table("model_runs", ctx.settings).filter(
            pl.col("model_type").is_in(["insider_xgb", "insider_iforest"]) & (pl.col("status") != "archived")
            & ~pl.col("model_run_id").is_in(new["model_run_id"].implode())).collect()
        if old.height:  # one production row per model_type x horizon
            new = pl.concat([new, old.with_columns(pl.lit(False).alias("is_production"),
                                                   pl.lit("archived").alias("status"))], how="diagonal_relaxed")
        rows["model_runs"] = write_table(new, "model_runs", settings=ctx.settings)
    except Exception as e:  # noqa: BLE001 - model_runs is owned by ml; never fail the fit on it
        log.warning("model_runs write failed: %s", e)
    m = (meta.get("oos_metrics") or {}).get("all_3m") or {}
    return JobResult(status="success", rows_written=rows, message=(
        f"version={meta['version']} events={meta['n_labelled']} oos_auc_3m={m.get('auc')} "
        f"rank_ic_pooled={m.get('rank_ic_pooled')} decile_spread={m.get('decile_spread')}"))
