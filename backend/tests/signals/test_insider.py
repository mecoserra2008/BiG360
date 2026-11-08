"""signals-alt: insider features, clusters, PIT signals, labels, walk-forward purge, IsolationForest."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.analytics import insider as I
from app.analytics import insider_models as M


def _txn(i: int, insider: str, d: date, kind: str = "buy", value: float = 1e5, role: str = "director",
         issuer: str = "I1", kd_lag: int = 2, plan: bool | None = False, **kw) -> dict:
    return {"txn_id": f"t{i}", "issuer_key": issuer, "issuer_id": issuer, "security_id": "S" + issuer,
            "source_id": "sec_edgar", "source_entity_id": "000" + issuer, "insider_id": insider, "insider_name": insider,
            "role_norm": role, "transaction_date": d, "knowledge_date": d + timedelta(days=kd_lag),
            "txn_type": kind, "direction": 1 if kind == "buy" else -1, "shares": value / 10, "price": 10.0,
            "currency": "USD", "value_usd": value, "is_10b5_1": plan, "holding_change_pct": 0.1, **kw}


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(pl.col("direction").cast(pl.Int8))


def test_cluster_ids_and_counts():
    d0 = date(2024, 3, 1)
    rows = [_txn(1, "a", d0), _txn(2, "b", d0 + timedelta(days=10)), _txn(3, "c", d0 + timedelta(days=25)),
            _txn(4, "d", d0 + timedelta(days=80)),  # > 30 days after the previous buy: new (singleton) episode
            _txn(5, "e", d0 + timedelta(days=5), kind="sell")]
    feat = I.assign_clusters(I.transaction_features(_frame(rows)))
    by = {r["txn_id"]: r for r in feat.iter_rows(named=True)}
    assert by["t1"]["cluster_id"] == by["t3"]["cluster_id"] is not None
    assert by["t1"]["cluster_n"] == 3
    assert by["t4"]["cluster_id"] is None and by["t5"]["cluster_id"] is None
    # PIT count of distinct same-direction insiders filed within 30 days
    assert by["t3"]["n_same_dir_30d"] == 3 and by["t1"]["n_same_dir_30d"] == 1


def test_first_buy_routine_and_size_history():
    rows = [_txn(i, "a", date(2020 + i, 5, 3)) for i in range(4)]  # same month 4 years running
    rows.append(_txn(10, "b", date(2023, 5, 3), value=1e6))
    feat = I.transaction_features(_frame(rows)).sort("txn_id")
    by = {r["txn_id"]: r for r in feat.iter_rows(named=True)}
    assert by["t0"]["first_buy_2y"] == 1.0 and by["t1"]["first_buy_2y"] == 0.0
    assert by["t3"]["routine"] == 1.0 and by["t2"]["routine"] == 0.0
    assert by["t0"]["size_vs_hist"] is None and abs(by["t3"]["size_vs_hist"]) < 1e-9


def test_signals_direction_neutral_and_pit():
    d0 = date(2024, 6, 3)
    buys = [_txn(i, f"x{i}", d0 + timedelta(days=i), role="ceo" if i == 0 else "director", issuer="IB")
            for i in range(3)]
    sells = [_txn(10 + i, f"y{i}", d0 + timedelta(days=i), kind="sell", value=5e6, issuer="IS") for i in range(3)]
    feat = I.assign_clusters(I.transaction_features(_frame(buys + sells))).with_columns(
        pl.lit(None, pl.Float64).alias("anomaly_score"))
    asof = d0 + timedelta(days=10)
    sig = I.compute_signals(feat, [asof])
    by = {r["issuer_key"]: r for r in sig.iter_rows(named=True)}
    assert by["IB"]["signal_score"] > 70 and by["IB"]["cluster_buy"] and by["IB"]["c_suite_buy"]
    assert by["IS"]["signal_score"] < 50 and by["IS"]["n_sellers_90d"] == 3
    # nothing known before the first filing date -> no row (scoring treats missing as neutral 50)
    assert I.compute_signals(feat, [d0]).is_empty()
    # PIT: adding a row filed AFTER the as-of date must not change the signal
    late = _txn(99, "z", asof - timedelta(days=5), kind="sell", value=1e9, issuer="IB", kd_lag=20)
    feat2 = I.assign_clusters(I.transaction_features(_frame(buys + sells + [late]))).with_columns(
        pl.lit(None, pl.Float64).alias("anomaly_score"))
    sig2 = I.compute_signals(feat2, [asof])
    a = sig.sort("issuer_key").drop("top_transactions")
    b = sig2.sort("issuer_key").drop("top_transactions")
    assert a.equals(b)


def test_load_open_market_filters_and_supersedes(tmp_data):
    from app.core.storage import write_table

    base = {"region": "US", "year": 2024, "source_id": "sec_edgar", "source_entity_id": "0000000001",
            "insider_name": "A", "is_director": True, "is_officer": False, "is_ten_pct_owner": False,
            "transaction_date": date(2024, 1, 2), "knowledge_date": date(2024, 1, 4), "is_derivative": False,
            "shares": 100.0, "price": 10.0, "is_amendment": False}
    rows = [
        {**base, "txn_id": "f1:nd:1", "filing_id": "f1", "insider_id": "a", "txn_code": "P", "txn_type": "buy",
         "is_open_market": True, "superseded_by_filing_id": "f3"},
        {**base, "txn_id": "f2:nd:1", "filing_id": "f2", "insider_id": "a", "txn_code": "A", "txn_type": "grant",
         "is_open_market": False},
        {**base, "txn_id": "f3:nd:1", "filing_id": "f3", "insider_id": "a", "txn_code": "P", "txn_type": "buy",
         "is_open_market": True, "knowledge_date": date(2024, 2, 1), "is_amendment": True},
    ]
    df = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.lit(datetime(2024, 1, 5, tzinfo=UTC)).alias("ingested_at"))
    write_table(df, "insider_transactions")
    early = I.load_open_market(date(2024, 1, 10), resolve=False)
    assert early["txn_id"].to_list() == ["f1:nd:1"]  # original valid until the 4/A is filed; grant excluded
    late = I.load_open_market(date(2024, 3, 1), resolve=False)
    assert late["txn_id"].to_list() == ["f3:nd:1"]
    assert sorted(I.load_open_market(None, resolve=False)["txn_id"].to_list()) == ["f3:nd:1"]


def _prices(sec: str, start: date, n: int, drift: float) -> pl.DataFrame:
    days = [start + timedelta(days=i) for i in range(n)]
    return pl.DataFrame({"security_id": sec, "date": days, "adj_close": [100 * (1 + drift) ** i for i in range(n)],
                         "close_usd": None, "dollar_volume_usd": 1e6}).with_columns(pl.col("close_usd").cast(pl.Float64))


def test_labels_forward_excess_from_next_close():
    start = date(2024, 1, 1)
    px = pl.concat([_prices("SA", start, 200, 0.01), _prices("ETF:SPY", start, 200, 0.0)])
    ev = pl.DataFrame({"issuer_key": ["A"], "issuer_id": ["A"], "security_id": ["SA"], "knowledge_date": [date(2024, 1, 10)]})
    lab = M.add_labels(ev, px, None)
    r = lab.row(0, named=True)
    # entry = close(D+1) = day index 10, exit = index 10 + 21
    assert r["fwd_excess_1m"] == pytest.approx(1.01 ** 21 - 1, rel=1e-9)
    assert r["label_end_1m"] == start + timedelta(days=31)


def test_walk_forward_purges_overlapping_labels():
    rng = np.random.default_rng(0)
    n = 6000
    kd = [date(2016, 1, 1) + timedelta(days=int(x)) for x in rng.integers(0, 365 * 5, n)]
    df = pl.DataFrame({c: rng.normal(size=n) for c in M.EVENT_FEATURES}).with_columns(
        pl.Series("knowledge_date", kd), pl.lit("k").alias("issuer_key"))
    df = df.with_columns((pl.col("knowledge_date") + timedelta(days=95)).alias("label_end_3m"),
                         (pl.col("ret_63") * 0.01 + pl.Series(rng.normal(scale=0.1, size=n))).alias("fwd_excess_3m"),
                         pl.Series(rng.normal(scale=0.05, size=n)).alias("fwd_excess_1m"), pl.lit(0.0).alias("rule_raw"))
    oos, folds = M.walk_forward(df, first_test_year=2019, params={"n_estimators": 20, "n_jobs": 2})
    assert folds and oos.height
    for f in folds:
        # last training filing must leave room for the 95-day label + embargo before the test block
        assert date.fromisoformat(f["train_end"]) + timedelta(days=95) < date.fromisoformat(f["test_start"])
    m = M.oos_metrics(oos, "pred_excess_3m", "fwd_excess_3m", "prob_up_3m")
    assert m["n"] == oos.height and -1 <= m["rank_ic_pooled"] <= 1 and 0 <= m["auc"] <= 1


def test_iforest_scores_in_unit_interval():
    rng = np.random.default_rng(1)
    n = 3000
    feat = pl.DataFrame({c: rng.normal(size=n) for c in I.TXN_MODEL_FEATURES})
    feat = feat.with_columns(pl.when(pl.int_range(pl.len()) == 0).then(50.0).otherwise(pl.col("log_value"))
                             .alias("log_value"))
    iso = M.fit_iforest(feat, max_rows=2000)
    out = M.score_anomaly(feat, {"iforest": iso})["anomaly_score"]
    assert out.min() >= 0 and out.max() <= 1 and out[0] > 0.95
