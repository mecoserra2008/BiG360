"""Backtesting lab against the synthetic lake (tests/fixtures/serving_small.py): PIT panel, screens, price fields,
end-to-end runs (rank, assets, events), Studio OOS source, panel cache and the standalone export."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import timedelta

import polars as pl
import pytest

from app.core import config as config_mod
from app.core import schema as schema_mod

N_SEC = 150  # the shared fixture needs >= 50 US names (smart-money block)


@pytest.fixture(scope="module")
def lake(tmp_path_factory):
    d = tmp_path_factory.mktemp("bt") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from tests.fixtures.serving_small import build_lake

    info = build_lake(s, n_securities=N_SEC)
    yield {"settings": s, "info": info}
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def spec(**kw):
    from app.backtest.spec import BacktestSpec

    base = {"universe": {"universe_id": "mvp_us"},
            "signal": {"variables": [{"name": "val", "source": "factor_scores", "field": "value"}]},
            "robustness": {"permutations": 4}}
    for k, v in kw.items():
        base[k] = v
    return BacktestSpec.model_validate(base)


def panel_for(sp):
    from app.backtest.runner import load_panel
    from app.backtest.spec import make_plan

    plan, _ = make_plan(sp)
    return plan, *load_panel(plan)


def con_for(tables):
    from app.modeling.datasets import lake_connection

    return lake_connection(tables)


def test_factor_signal_is_point_in_time(lake):
    plan, panel, px, ev, meta = panel_for(spec())
    assert meta["n_entities"] > 10 and not meta["cached"]
    con = con_for(["factor_scores"])
    fs = con.execute("SELECT security_id AS entity_id, as_of_date, score FROM factor_scores WHERE factor_id = 'value'").pl()
    con.close()
    sample = panel.filter(pl.col("signal").is_not_null()).sample(25, seed=1)
    for e, t, v in sample.select("entity_id", "date", "signal").iter_rows():
        known = fs.filter((pl.col("entity_id") == e) & (pl.col("as_of_date") <= t)).sort("as_of_date")
        assert known.height, (e, t)
        last = known.row(-1, named=True)
        assert last["as_of_date"] > t - timedelta(days=62)  # value from the same / previous month bucket
        if last["as_of_date"].month == t.month:
            assert v == pytest.approx(last["score"])


def test_screen_and_price_fields_pit(lake):
    sp = spec(universe={"universe_id": "mvp_us", "screen": [{"field": "momentum", "op": "gte", "value": 50},
                                                             {"field": "mom_12_1", "op": "gt", "value": -0.9},
                                                             {"field": "fv_upside", "op": "gt", "value": 0}]})
    from app.backtest.spec import make_plan

    plan, warns = make_plan(sp)
    assert any("fv_upside" in w for w in warns)
    _, panel, px, _, meta = panel_for(sp)
    rows = panel.filter(pl.col("s_momentum").is_not_null() & pl.col("mom_12_1").is_not_null())
    assert rows.height > 50
    bad = rows.filter(pl.col("eligible") != ((pl.col("s_momentum") >= 50) & (pl.col("mom_12_1") > -0.9)))
    assert bad.height == 0
    # mom_12_1 from prints <= t only
    con = con_for(["prices_daily"])
    e, t, m = rows.select("entity_id", "date", "mom_12_1").row(rows.height // 2)
    p = con.execute("SELECT date, adj_close * usd_per_unit AS px FROM prices_daily WHERE security_id = ? AND date <= ? "
                    "ORDER BY date", [e, t]).pl()
    con.close()
    pxs = p["px"].to_list()
    assert m == pytest.approx(pxs[-1 - 21] / pxs[-1 - 252] - 1, rel=1e-9)


def test_compute_rank_end_to_end_and_cache(lake):
    from app.backtest.runner import compute, load_panel
    from app.backtest.spec import make_plan

    sp = spec(construction={"side": "long_short", "quantiles": 3},
              robustness={"permutations": 6, "sweep": {"construction.quantiles": [3, 5]}})
    r = compute(sp)
    assert r["summary"]["n_rebalances"] > 10
    full = r["metrics"]["full"]
    assert math.isfinite(full["sharpe"]) and full["avg_n_long"] > 0 and full["avg_n_short"] > 0
    assert "in_sample" in r["metrics"] and "out_of_sample" in r["metrics"]
    rb = r["robustness"]
    assert rb["permutation"]["n"] == 6 and abs(rb["permutation"]["mean"]) < 1.5
    assert rb["dsr"]["n_trials"] == 2 and 0 <= rb["dsr"]["dsr"] <= 1
    assert rb["sweep"]["n"] == 2 and rb["stale"]["sharpe"] is not None
    assert r["deciles"]["n_periods"] > 5 and r["caveats"]["survivorship"].startswith("Survivorship")
    assert r["_holdings"].height > 0 and {"label", "weight"} <= set(r["_holdings"].columns)
    assert set(r["flat_metrics"]) >= {"sharpe", "cagr", "max_drawdown", "dsr"}
    plan, _ = make_plan(sp)
    assert load_panel(plan)[3]["cached"] is True


def test_assets_universe_and_cftc_events(lake):
    from app.backtest.runner import compute

    r = compute(spec(universe={"kind": "assets"},
                     signal={"variables": [{"name": "t", "source": "cross_asset", "field": "trend_signal"}]},
                     construction={"side": "long_short", "quantiles": 3, "rebalance": "W"}))
    assert r["summary"]["benchmark"] == "universe_ew" and r["metrics"]["full"]["borrow_cost_bps"] == 0
    ev = compute(spec(mode="event", universe={"kind": "assets"},
                      signal={"variables": [{"name": "c", "source": "cftc", "field": "cot_index_3y"}]},
                      event={"trigger": "cftc_extreme", "params": {"high": 0.8, "low": 0.2}, "hold_days": 10,
                             "allow_short": True}))
    assert ev["events"]["n_events"] > 0 and ev["events"]["n_trades"] > 0
    assert ev["events"]["path"]["n"] > 0
    assert all(t["holding_days"] <= 10 for t in ev["_trades"].to_dicts() if t["exit_reason"] == "time")


def test_studio_run_oos_predictions_source(lake):
    from app.backtest.runner import compute

    s = lake["settings"]
    _, panel, px, _, _ = panel_for(spec())
    rows = panel.select("entity_id", "date", pl.col("signal").alias("prediction")).drop_nulls()
    rd = s.data_dir / "modeling" / "runs" / "SRTESTBT0001"
    rd.mkdir(parents=True, exist_ok=True)
    rows.with_columns(pl.lit(0).alias("fold"), pl.lit("purged_kfold").alias("scheme"), pl.lit("xgb").alias("model")) \
        .write_parquet(rd / "oos_predictions.parquet")
    r = compute(spec(signal={"variables": [{"name": "st", "source": "studio_run", "field": "SRTESTBT0001"}]}))
    assert any("LOOK-AHEAD IN FITTING" in w for w in r["warnings"])
    assert r["metrics"]["full"]["avg_n_long"] > 0


def test_export_script_reproduces_metrics(lake, tmp_path):
    from app.backtest.export import export_script
    from app.backtest.runner import compute, run_dict
    from app.backtest.spec import make_plan

    sp = spec(construction={"side": "long_short", "quantiles": 3, "weighting": "inverse_vol"},
              robustness={"permutations": 0, "stale_check": False, "attribution": False})
    r = compute(sp)
    plan, _ = make_plan(sp)
    env = {"run_id": "BTTEST", "run_spec": run_dict(sp), "plan": plan, "result": r,
           "reproducibility": {"spec_hash": sp.spec_hash()}}
    src = export_script(env, lake["settings"].data_dir)
    f = tmp_path / "bt_export.py"
    f.write_text(src, encoding="utf-8")
    env_vars = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run([sys.executable, str(f), "--data-dir", str(lake["settings"].data_dir)], capture_output=True,
                         text=True, cwd=str(tmp_path), env=env_vars, timeout=600)
    assert out.returncode == 0, out.stderr[-3000:]
    line = next(x for x in out.stdout.splitlines() if x.startswith("RESULT_JSON "))
    got = json.loads(line[len("RESULT_JSON "):])
    assert got["sharpe"] == pytest.approx(r["metrics"]["full"]["sharpe"], rel=1e-6)
    assert got["total_return"] == pytest.approx(r["metrics"]["full"]["total_return"], rel=1e-6, abs=1e-9)
    assert got["n_days"] == r["metrics"]["full"]["n_days"]


def test_adv_zero_volume_window_has_no_float_residue():
    """A 20-day window of zero volume after large prints must give ADV 0, not a ~1e-12 rolling-mean residue (which
    blew up the square-root impact cost; guide 11, 'ML 3M top decile')."""
    from datetime import date as _date

    from app.backtest import _panel as P

    days = [_date(2020, 1, 1) + timedelta(days=i) for i in range(60)]
    dv = [123456789.123] * 30 + [0.0] * 30
    daily = pl.DataFrame({"entity_id": ["X"] * 60, "date": days, "px": [10.0 + 0.01 * i for i in range(60)], "dv": dv})
    out = P._bp_price_features(daily, [], None).sort("date")
    tail = out["adv_usd_20d"].to_list()[-5:]
    assert tail == [0.0] * 5
