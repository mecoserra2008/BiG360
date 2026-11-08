"""macro v2 wave 2 (macro-w2): calendar helpers, point-in-time model-based expectations, surprise math, DFM nowcast on
synthetic data, official benchmark parsers, recession helpers, macro alert evaluators, empty-snapshot API shapes."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import polars as pl
import pytest

from app.macro import calendar as cal
from app.macro import expectations as ex
from app.macro import nowcast as nc
from app.macro import recession as rc
from app.macro import surprise as su

NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------------------------------------------ synthetic vintages


def _vintage_obs(n: int = 120, seed: int = 1) -> pd.DataFrame:
    """Monthly index with ALFRED-like vintages: each period first released ~12 days after month end, revised once a
    month later (+0.1%)."""
    rng = np.random.default_rng(seed)
    periods = pd.period_range("2015-01", periods=n, freq="M")
    lvl = 100 * np.exp(np.cumsum(0.002 + rng.normal(0, 0.003, n)))
    rows = []
    for p, v in zip(periods, lvl, strict=True):
        pe = p.end_time.date()
        k1 = pe + timedelta(days=12)
        rows.append((p.start_time.date(), float(v), k1, False))
        rows.append((p.start_time.date(), float(v) * 1.001, k1 + timedelta(days=30), False))
    df = pd.DataFrame(rows, columns=["date", "value", "knowledge_date", "est"])
    return df.sort_values(["date", "knowledge_date"]).reset_index(drop=True)


T_CPI = cal.Tracked("fred:TEST", "US", "cpi_yoy", "mom_pct", "M", title="Test CPI", vintages=True, publisher="BLS")


def test_importance_tier_rule():
    assert cal.importance_tier("Consumer Price Index") == 1
    assert cal.importance_tier("Gross Domestic Product, 3rd quarter") == 1
    assert cal.importance_tier("Industrial Production and Capacity Utilization") == 2
    assert cal.importance_tier("Quarterly Services Survey") == 3
    assert cal.importance_tier("anything", "policy_meeting") == 1


def test_apply_measure_and_asof():
    obs = _vintage_obs(24)
    cut = obs["knowledge_date"].iloc[20]
    s = cal.asof_series(obs, cut, "M", strict=True)
    assert (obs[obs["knowledge_date"] < cut]["date"].max()) == s.index[-1]
    m = cal.apply_measure(pd.Series([100.0, 101.0, 99.99]), "mom_pct", "M")
    assert abs(m.iloc[1] - 1.0) < 1e-9 and np.isnan(m.iloc[0])
    assert cal.apply_measure(pd.Series([1.0, 3.0]), "diff", "M").iloc[1] == 2.0


def test_build_events_and_fred_twin_merge():
    obs = _vintage_obs(60)
    fr = cal.first_releases(obs)
    entries = []
    for kd in fr["knowledge_date"].tolist()[-6:]:
        entries.append(cal.Entry("fred_releases", "fred:10", "Consumer Price Index", "FRED", "US", "data",
                                 datetime(kd.year, kd.month, kd.day, 8, 30, tzinfo=NY), "America/New_York", False,
                                 series_ids=[T_CPI.series_id]))
    kd = fr["knowledge_date"].tolist()[-1]
    bls = cal.Entry("bls_cal", "bls|cpi", "Consumer Price Index for September", "BLS", "US", "data",
                    datetime(kd.year, kd.month, kd.day, 8, 30, tzinfo=NY), "America/New_York", True)
    merged = cal.merge_fred_twins(entries + [bls])
    assert len(merged) == 6 and T_CPI.series_id in bls.series_ids          # FRED row folded into the BLS row
    start = fr["knowledge_date"].tolist()[-6]
    rows, inferred = cal.build_events(T_CPI, obs, merged, start, kd + timedelta(days=1), datetime.now(UTC))
    assert not inferred and len(rows) == 6
    last = next(r for r in rows if r["release_event_id"] == bls.rid)
    assert last["status"] == "released" and last["period"] == fr["date"].iloc[-1]
    # prior = previous period's m/m as known before the release, prior_revised = after (one revision happened)
    assert last["prior"] is not None and last["prior_revised"] is not None


def test_no_actual_on_later_or_future_release():
    """Regression (weekly claims): an observation first known on day K (estimated) must never attach to a calendar
    entry dated after K, nor to a release scheduled in the future; invariants hold for every row."""
    t = cal.Tracked("fred:ICSATEST", "US", "claims", "level", "W", direction=-1)
    today = date(2026, 10, 12)
    now = datetime(2026, 10, 12, 15, 0, tzinfo=UTC)
    weeks = [date(2026, 8, 1) + timedelta(days=7 * i) for i in range(10)]          # Saturdays, last = 2026-10-03
    obs = pd.DataFrame({"date": weeks, "value": [2.0e5 + i for i in range(10)],
                        "knowledge_date": [w + timedelta(days=5) for w in weeks], "est": True})
    entries = []
    for w in weeks + [weeks[-1] + timedelta(days=7), weeks[-1] + timedelta(days=14)]:  # Thursday releases
        d = w + timedelta(days=12)                                                  # one week later than knowledge
        entries.append(cal.Entry("fred_releases", "fred:180", "Claims", "FRED", "US", "data",
                                 datetime(d.year, d.month, d.day, 8, 30, tzinfo=NY), "America/New_York", False,
                                 series_ids=[t.series_id]))
    rows, _ = cal.build_events(t, obs, entries, date(2026, 7, 1), today, now)
    ev = cal.events_frame(rows)
    assert cal.invariant_violations(ev, now).height == 0
    j = ev.filter(pl.col("actual").is_not_null())
    assert (j["scheduled_at"] <= now).all() and (j["release_date"] <= j["knowledge_date"]).all()
    assert ev.filter((pl.col("release_date") > today) & pl.col("actual").is_not_null()).is_empty()
    bad = ev.with_columns(pl.lit(1.0).alias("actual"), pl.lit("pending").alias("status"))
    assert cal.invariant_violations(bad, now).height == bad.height


def test_link_entries_eurostat_members():
    t_ea = cal.Tracked("ecb:HICP", "EA", "cpi_yoy", "level", "M")
    t_de = cal.Tracked("eurostat:prc_hicp_minr.DE.TOTAL", "DE", "cpi_yoy", "level", "M")
    e = cal.Entry("eurostat_cal", "k", "Euro area annual inflation", "Eurostat", "EA", "data",
                  datetime(2026, 10, 17, 11, 0, tzinfo=ZoneInfo("Europe/Luxembourg")), "Europe/Luxembourg", False)
    cal.link_entries([e], [t_ea, t_de])
    assert set(e.series_ids) == {"ecb:HICP", "eurostat:prc_hicp_minr.DE.TOTAL"}


# ------------------------------------------------------------------------------------------------ expectations PIT


def test_expectations_point_in_time_property():
    """No data known at or after the release date may influence the expectation: perturbing every vintage with
    knowledge_date >= release date leaves the forecast unchanged, and forecast_made_at < scheduled_at."""
    obs = _vintage_obs(100)
    fr = cal.first_releases(obs)
    evs = []
    for i in (-12, -6, -1):
        kd, per = fr["knowledge_date"].iloc[i], fr["date"].iloc[i]
        evs.append({"release_event_id": f"RE{i}", "period": per, "release_date": kd,
                    "scheduled_at": datetime(kd.year, kd.month, kd.day, 8, 30, tzinfo=NY).astimezone(UTC)})
    now = datetime.now(UTC)
    base = ex.expectations_for_series(T_CPI, obs, evs, True, now)
    assert base and all(r["model_based"] and r["label"] == "Model-based expectation (not consensus)" for r in base)
    for r in base:
        ev = next(e for e in evs if e["release_event_id"] == r["release_event_id"])
        assert r["forecast_made_at"] < r["scheduled_at"]
        assert r["data_cutoff"] < ev["release_date"]
    poisoned = obs.copy()
    for ev in evs:
        m = poisoned["knowledge_date"] >= ev["release_date"]
        poisoned.loc[m, "value"] = poisoned.loc[m, "value"] * 50.0
        # each event checked separately: only rows known after *that* release are poisoned
        one = ex.expectations_for_series(T_CPI, poisoned, [ev], True, now)
        ref = ex.expectations_for_series(T_CPI, obs, [ev], True, now)  # same conditions (fresh order search)
        assert [(r["model"], round(r["point"], 9)) for r in one] == [(r["model"], round(r["point"], 9)) for r in ref]
        poisoned = obs.copy()
    assert {r["model"] for r in base} >= {"sarima", "naive"}
    assert sum(r["is_primary"] for r in base) == len(evs)


def test_live_expectation_only_before_release():
    obs = _vintage_obs(80)
    now = datetime.now(UTC)
    past = {"release_event_id": "REp", "period": date(2099, 1, 1), "release_date": now.date() - timedelta(days=1),
            "scheduled_at": now - timedelta(hours=20)}
    assert ex.expectations_for_series(T_CPI, obs, [past], False, now) == []


def test_select_primary_rule():
    d = [date(2025, m, 15) for m in range(1, 13)]
    # sarima errors 1.0, nowcast errors 0.5 -> nowcast wins once >= 4 common releases precede the quarter start
    hist = [(x, "sarima", 0.0, 1.0) for x in d] + [(x, "dfm_nowcast", 0.5, 1.0) for x in d]
    assert ex.select_primary(hist, date(2026, 1, 20), {"sarima", "dfm_nowcast", "naive"})[0] == "sarima"  # naive unscored
    assert ex.select_primary(hist, date(2026, 1, 20), {"sarima", "dfm_nowcast"})[0] == "dfm_nowcast"
    # point-in-time + quarterly: only releases before the quarter start count (3 releases < 2025-04-01 -> default)
    m, info = ex.select_primary(hist, date(2025, 5, 1), {"sarima", "dfm_nowcast"})
    assert m == "sarima" and info["rule"] == "default_rank" and info["n"] == 3
    hist2 = [(x, "sarima", 0.0, 1.0) for x in d] + [(x, "naive", 0.9, 1.0) for x in d]
    assert ex.select_primary(hist2, date(2026, 1, 2), {"sarima", "naive"})[0] == "naive"


def test_naive_and_sarima_forecasts():
    y = pd.Series(np.sin(np.arange(60) * 2 * np.pi / 12) + 5.0)
    nv = ex.naive_forecast(y, 1, 12)
    assert nv["model"] == "seasonal_naive" and abs(nv["point"] - y.iloc[48]) < 1e-12
    sm = ex.sarima_forecast(pd.Series(np.random.default_rng(0).normal(0, 1, 80).cumsum() * 0.1 + 2.0), 1, 0)
    assert sm and sm["model"] == "sarima" and sm["sd"] > 0 and "order" in sm["params"]


# ------------------------------------------------------------------------------------------------ surprise math


def _ev_ex(errors: list[float]) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    n = len(errors)
    d0 = date(2024, 1, 15)
    ids = [f"R{i}" for i in range(n)]
    ev = pl.DataFrame({"release_event_id": ids, "series_id": ["s"] * n, "country": ["US"] * n, "theme": ["labor"] * n,
                       "measure": ["level"] * n, "release_date": [d0 + timedelta(days=30 * i) for i in range(n)],
                       "scheduled_at": [datetime(2024, 1, 15, tzinfo=UTC) + timedelta(days=30 * i) for i in range(n)],
                       "period": [d0 for _ in range(n)], "status": ["released"] * n,
                       "actual": [10.0 + e for e in errors]})
    x = pl.DataFrame({"release_event_id": ids, "series_id": ["s"] * n, "target_period": [d0] * n, "point": [10.0] * n, "lo80": [9.0] * n,
                      "hi80": [11.0] * n, "model": ["sarima"] * n})
    rel = pl.DataFrame({"release_event_id": ids, "importance": [1] * n}, schema_overrides={"importance": pl.Int8})
    return ev, x, rel


def test_surprise_z_uses_only_past_errors_and_direction():
    errs = [1.0, -1.0, 2.0, -2.0, 1.0, -1.0, 0.5, 30.0]
    ev, x, rel = _ev_ex(errs)
    s = su.surprise_rows(ev, x, rel, {"s": -1}).sort("release_date")
    last = s.row(-1, named=True)
    sd = float(np.std(errs[:-1], ddof=1))
    assert last["n_hist_errors"] == 7 and abs(last["error_std"] - sd) < 1e-12
    assert last["surprise_z"] == su.Z_CLIP and last["signed_z"] == -su.Z_CLIP     # clipped, unemployment-like sign
    assert s.row(0, named=True)["surprise_z"] is None                             # no history yet


def test_surprise_index_decay_and_pit():
    days = np.array([100, 101, 200])
    val, n, last = su.index_series(days, np.array([100]), np.array([2.0]), np.array([1.0]), halflife=10, window=365)
    assert n.tolist() == [1, 1, 1] and last.tolist() == [100, 100, 100]
    assert abs(val[0] - 200.0) < 1e-9                       # w = 1 -> 100 * z
    assert abs(val[1] - 100 * 2 * 0.5 ** 0.1) < 1e-9        # decaying weight below 1 -> shrinks toward 0
    val2, n2, _ = su.index_series(np.array([99]), np.array([100]), np.array([2.0]), np.array([1.0]))
    assert n2[0] == 0 and val2[0] == 0                       # future release does not leak


# ------------------------------------------------------------------------------------------------ nowcast


def test_dfm_nowcast_on_synthetic_data():
    """1-factor mixed-frequency world: the nowcast of the unobserved last quarter tracks the true value."""
    rng = np.random.default_rng(5)
    T = 180
    f = np.zeros(T)
    for t in range(1, T):
        f[t] = 0.7 * f[t - 1] + rng.normal(0, 1)
    midx = pd.period_range("2005-01", periods=T, freq="M")
    m = pd.DataFrame({f"x{i}": 0.8 * f + rng.normal(0, 0.5, T) for i in range(5)}, index=midx)
    w = np.array([1, 2, 3, 2, 1]) / 3
    gq = {}
    for t in range(4, T):
        if midx[t].month % 3 == 0:
            gq[midx[t].asfreq("Q")] = 0.5 + float(w @ f[t - 4:t + 1][::-1]) + rng.normal(0, 0.2)
    q = pd.DataFrame({nc.GDP_NAME: pd.Series(gq)}, index=pd.period_range(midx[0].asfreq("Q"), midx[-1].asfreq("Q"), freq="Q"))
    truth = q[nc.GDP_NAME].iloc[-1]
    q.iloc[-1, 0] = np.nan
    res = nc.fit(m, q, maxiter=50)
    tgt = q.index[-1]
    est = nc.extract(res, [tgt], annualize=False)[tgt]
    assert est[1] < est[0] < est[2]
    assert abs(est[0] - truth) < 1.5
    # news: removing the last month of data and re-applying gives a decomposition that sums to the change
    m_prev = m.copy()
    m_prev.iloc[-1] = np.nan
    res_prev = nc.apply(res, m_prev, q)
    news = nc.news_items(res, res_prev, tgt, False, {})
    prev = nc.extract(res_prev, [tgt], False)[tgt][0]
    assert news and abs(sum(n["contribution"] for n in news) - (est[0] - prev)) < 0.05


def test_target_quarters():
    q = pd.DataFrame({nc.GDP_NAME: [1.0, 2.0, np.nan, np.nan]}, index=pd.period_range("2026Q1", periods=4, freq="Q"))
    assert [str(x) for x in nc.target_quarters(q, date(2026, 10, 12))] == ["2026Q3", "2026Q4"]


def _wb(sheets: dict[str, list[list]]) -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def test_benchmark_parsers():
    hdr = ["Forecast Date", "Quarter being forecasted"] + [f"c{i}" for i in range(25)] + ["GDP Nowcast"]
    gd = _wb({"TrackingArchives": [hdr, [datetime(2026, 7, 1), datetime(2026, 6, 30)] + [0] * 25 + [1.4],
                                   [datetime(2026, 7, 28), datetime(2026, 6, 30)] + [0] * 25 + [1.54]],
              "CurrentQtrEvolution": [["Date", "Major Releases", "GDP*"],
                                      [datetime(2026, 7, 30), "Initial GDPNow 26:Q3 forecast", 4.95],
                                      [datetime(2026, 10, 8), "Retail", 3.59]]})
    rows = nc.parse_gdpnow(gd)
    assert {(r["target_label"], r["as_of"].isoformat(), r["nowcast"]) for r in rows} == {
        ("2026Q2", "2026-07-01", 1.4), ("2026Q2", "2026-07-28", 1.54), ("2026Q3", "2026-07-30", 4.95),
        ("2026Q3", "2026-10-08", 3.59)}
    assert all(not r["model_based"] and "official" in r["label"] for r in rows)
    ny = _wb({"Forecasts By Quarter": [[None], ["NEW YORK FED"], ["Forecast Date", "2026Q2", "2026Q3"],
                                       [datetime(2026, 7, 31), 2.85, 2.1], [datetime(2026, 10, 9), None, 2.65]]})
    meta = ("year,title,quarter,landingQuarter,concluded,advancedGDP,advancedReleaseDate,latestGDP,latestReleaseDate,"
            "finalNowcast,finalNowcastReleaseDate,80_lb,80_ub\n2026,x,Q2,,1,1.5,30-Jul-26,2.2,30-Sep-26,2.85,"
            "2026-07-31T08:30:00,0.69,5.01\n")
    r2 = nc.parse_nyfed(ny, meta)
    q2 = next(r for r in r2 if r["target_label"] == "2026Q2")
    assert len(r2) == 3 and q2["lo"] == 0.69 and q2["hi"] == 5.01 and q2["model_id"] == "nyfed_staff_nowcast"


# ------------------------------------------------------------------------------------------------ recession


def test_recession_helpers():
    g = pd.Series([0.5, -0.2, -0.3, 0.4, -0.1, 0.6], index=pd.period_range("2020Q1", periods=6, freq="Q"))
    rec = rc.technical_recession_months(g)
    assert rec[pd.Period("2020-04", "M")] == 1 and rec[pd.Period("2020-09", "M")] == 1
    assert rec[pd.Period("2021-02", "M")] == 0          # single negative quarter is not a recession
    s = pd.Series(np.sin(np.arange(120) * 2 * np.pi / 40) + 100, index=pd.period_range("2000-01", periods=120, freq="M"))
    tps = rc.turning_points(s)
    assert [t for _, t in tps[:2]] in (["peak", "trough"], ["trough", "peak"])
    assert all(a[1] != b[1] for a, b in zip(tps, tps[1:], strict=False))
    df = pd.DataFrame({"Date": pd.to_datetime(["2025-08-31", "2026-08-31"]), "Spread": [0.5, np.nan],
                       "Rec_prob": [np.nan, 0.14], "NBER_Rec": [0, np.nan]})
    b = io.BytesIO()
    with pd.ExcelWriter(b) as w:
        df.to_excel(w, sheet_name="rec_prob", index=False)
    rows = rc.parse_nyfed_recprob(b.getvalue())
    assert len(rows) == 1 and rows[0]["date"] == date(2025, 8, 1) and rows[0]["value"] == 0.5
    assert rows[0]["official"] and rows[0]["method_label"].startswith("NY Fed")


# ------------------------------------------------------------------------------------------------ alerts


def test_macro_alert_evaluators():
    from app.core.schema import empty_frame
    from app.scheduler import alerts as al

    d = date(2026, 10, 14)
    rel = empty_frame("macro_releases").vstack(pl.DataFrame({
        "release_event_id": ["R1"], "source_id": ["bls_cal"], "release_key": ["k"], "release_name": ["CPI"],
        "publisher": ["BLS"], "country": ["US"], "kind": ["data"], "scheduled_at": [datetime(2026, 10, 14, 12, 30, tzinfo=UTC)],
        "scheduled_tz": ["America/New_York"], "scheduled_local": ["x"], "release_date": [d], "time_known": [True],
        "schedule_source": ["official_calendar"], "importance": [1], "series_ids": [["fred:CPIAUCSL"]],
        "reference_period": [None], "url": [None], "first_seen_at": [datetime.now(UTC)], "updated_at": [datetime.now(UTC)]},
        schema=empty_frame("macro_releases").schema))
    ev = pl.DataFrame({"release_event_id": ["R1"], "series_id": ["fred:CPIAUCSL"], "country": ["US"], "theme": ["inflation"],
                       "measure": ["mom_pct"], "release_date": [d], "scheduled_at": [datetime(2026, 10, 14, 12, 30, tzinfo=UTC)],
                       "period": [date(2026, 9, 1)], "status": ["released"], "actual": [0.5], "prior": [0.2]})
    sur = pl.DataFrame({"release_event_id": ["R1"], "series_id": ["fred:CPIAUCSL"], "country": ["US"], "theme": ["inflation"],
                        "importance": [1], "release_date": [d], "period": [date(2026, 9, 1)], "actual": [0.5],
                        "expectation": [0.25], "surprise_z": [2.6], "signed_z": [2.6]})
    recs = pl.DataFrame({"method": ["yield_curve_probit"] * 2, "country": ["DE"] * 2, "date": [date(2026, 9, 1), date(2026, 8, 1)],
                         "probability": [0.55, 0.35], "signal": [True, False]})
    cyc = pl.DataFrame({"country": ["US", "US"], "date": [date(2026, 9, 1), date(2026, 8, 1)],
                        "quadrant": ["slowdown", "expansion"]})
    dp = al.frames_provider({"macro_releases": rel, "macro_release_events": ev, "macro_surprises": sur,
                             "macro_recession": recs, "macro_cycle": cyc})
    alerts = [
        {"alert_id": "A1", "security_id": None, "watchlist_id": None, "rule_type": "macro_release",
         "params": {"countries": ["US"]}, "enabled": True, "cooldown_days": 0},
        {"alert_id": "A2", "security_id": None, "watchlist_id": None, "rule_type": "macro_surprise",
         "params": {"threshold_z": 2.0}, "enabled": True, "cooldown_days": 0},
        {"alert_id": "A3", "security_id": None, "watchlist_id": None, "rule_type": "macro_surprise",
         "params": {"threshold_z": 3.0}, "enabled": True, "cooldown_days": 0},
        {"alert_id": "A4", "security_id": None, "watchlist_id": None, "rule_type": "regime_change",
         "params": {"level": 0.5}, "enabled": True, "cooldown_days": 0},
    ]
    evs = al.evaluate_alerts(alerts, dp, d)
    fired = sorted((e["alert_id"], e["security_id"]) for e in evs)
    assert fired == [("A1", "macro:US"), ("A2", "macro:US"), ("A4", "macro:DE"), ("A4", "macro:US")]
    assert "model-based expectation" in next(e for e in evs if e["alert_id"] == "A2")["message"]


def test_macro_rule_types_registered():
    from app.core.schema import get_contract
    from app.scheduler import alerts as al

    enum = set(get_contract("alerts").column("rule_type").values)
    assert {"macro_release", "macro_surprise", "regime_change"} <= enum <= set(al.EVALUATORS)


# ------------------------------------------------------------------------------------------------ API (empty snapshot)


@pytest.mark.parametrize("url,tpl", [
    ("/api/v1/macro/v2/calendar", "/api/v1/macro/v2/calendar"),
    ("/api/v1/macro/v2/surprise", "/api/v1/macro/v2/surprise"),
    ("/api/v1/macro/v2/nowcast/US", "/api/v1/macro/v2/nowcast/{cc}"),
    ("/api/v1/macro/v2/recession", "/api/v1/macro/v2/recession")])
def test_w2_empty_snapshot_shapes(tmp_data, url, tpl):  # noqa: ANN001
    from fastapi.testclient import TestClient

    from app.api.deps import snapshots
    from app.api.main import create_app
    from tests.api_contract import validate

    snapshots.reset()
    c = TestClient(create_app())
    r = c.get(url)
    assert r.status_code == 200, r.text[:300]
    validate("GET", tpl, 200, r.json())
    assert r.json()["data_status"] == "pending"
    assert c.get("/api/v1/macro/v2/releases/RE0").status_code == 404
    snapshots.reset()
