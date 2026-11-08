"""Event dynamics: Hawkes EM, BOCPD, event-study math, shock definitions, hotspot classes, z-score fill."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import polars as pl
import pytest

from app.events_models import bocpd as B
from app.events_models import hawkes as H
from app.events_models.common import fill_z
from app.events_models.event_study import expected_reaction, shocks
from app.events_models.hotspots import emerging_class, mann_kendall
from app.modeling._algos import car_study


def test_hawkes_recovers_branching_ratio():
    true = H.HawkesFit(np.array([2.0, 1.0]), np.array([[0.4, 0.2], [0.1, 0.3]]), 0.6, np.ones((2, 7)), 0.0, True, 0,
                       np.zeros(2), np.zeros(2), np.ones(2))
    N = H.simulate(true, 4000, 1, None, seed=1, overdispersed=False)[0]
    f = H.fit_hawkes(N)
    assert f.branching_ratio == pytest.approx(true.branching_ratio, abs=0.05)
    assert f.alpha == pytest.approx(true.alpha, abs=0.06)
    assert f.mu == pytest.approx(true.mu, rel=0.15)
    assert f.beta == pytest.approx(0.6, abs=0.25) and f.converged


def test_hawkes_escalation_probability_monotone_in_state():
    f = H.HawkesFit(np.array([5.0]), np.array([[0.6]]), 0.5, np.ones((1, 7)), 0.0, True, 0, np.array([5.0]),
                    np.array([5.0]), np.array([1.0]))
    calm = H.simulate(f, 7, 2000, None, seed=0)[:, :, 0].sum(axis=1)
    hot = H.HawkesFit(f.mu, f.alpha, f.beta, f.season, 0.0, True, 0, np.array([60.0]), np.array([60.0]), np.array([1.0]))
    burst = H.simulate(hot, 7, 2000, None, seed=0)[:, :, 0].sum(axis=1)
    thr = np.quantile(calm, 0.9)
    assert (burst > thr).mean() > 0.9 > (calm > thr).mean()
    ep = H.expected_path(f, 200)[:, 0]
    assert ep[-1] == pytest.approx(5.0 / (1 - 0.6), rel=0.02)      # stationary mean mu / (1 - n)


def test_bocpd_detects_changes_and_is_online():
    g = np.random.default_rng(0)
    x = np.concatenate([g.normal(0, 1, 80), g.normal(3, 1, 60), g.normal(-1, 0.5, 60)])
    cps = B.detect(x)
    idx = [c["index"] for c in cps]
    assert any(abs(i - 80) <= 3 for i in idx) and any(abs(i - 140) <= 3 for i in idx)
    assert len(idx) <= 3
    # online == batch, and state survives serialization
    d = B.BOCPD.from_warmup(x[:14])
    for v in x[:100]:
        d.update(v)
    d2 = B.BOCPD.from_dict(d.to_dict())
    for v in x[100:120]:
        a, b = d.update(v), d2.update(v)
        assert a == b
    assert d.update(float("nan"))["t"] == d.t                       # missing value: no state change
    # false-alarm rate on stationary noise
    fa = sum(len(B.detect(g.normal(5, 1, 200))) for _ in range(20))
    assert fa <= 6


def test_car_study_recovers_planted_abnormal_return():
    g = np.random.default_rng(1)
    T, n = 600, 30
    dates = pd.bdate_range("2020-01-01", periods=T)
    m = pd.Series(g.normal(0, 0.01, T), index=dates)
    R = pd.DataFrame({f"S{i}": 0.0002 + 1.1 * m.to_numpy() + g.normal(0, 0.01, T) for i in range(n)}, index=dates)
    ev = []
    for i in range(n):
        d = dates[300 + 7 * i]
        R.loc[d, f"S{i}"] += -0.03                                  # planted -3% on day 0
        ev.append((f"S{i}", d))
    out = car_study(R, m, ev, (-250, -30), (0, 5), "market")
    assert out["n_events"] == n
    assert out["caar"][-1] == pytest.approx(-0.03, abs=0.008) and out["p_cs"] < 1e-4


def test_shock_definition_dedupes():
    d0 = date(2026, 1, 1)
    rows = [{"date": d0 + timedelta(days=i), "label": "US", "theme": -1, "ev_n": 50, "art_n": 50,
             "attention_z": z, "tone_z": 0.0} for i, z in enumerate([0, 3, 3.2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 2.6])]
    s = shocks(pl.DataFrame(rows))
    a = s.filter(pl.col("kind") == "attention")["date"].to_list()
    assert a == [d0 + timedelta(days=1), d0 + timedelta(days=13)]


def test_expected_reaction_fallback():
    base = {"shock_kind": "attention", "window": "0,5", "level": "country_index", "sector": "ALL"}
    lib = pl.DataFrame([base | {"theme": "conflict_military", "iso2": "ALL", "n_events": 20, "mean_car": -1.0},
                        base | {"theme": "conflict_military", "iso2": "IL", "n_events": 2, "mean_car": -5.0},
                        base | {"theme": "conflict_military", "iso2": "UA", "n_events": 8, "mean_car": -2.0}])
    assert expected_reaction(lib, "conflict_military", "UA")["mean_car"] == -2.0
    r = expected_reaction(lib, "conflict_military", "IL")      # n < 3 -> falls back to the theme cell
    assert r["mean_car"] == -1.0 and r["match"] == "theme"
    assert expected_reaction(lib, "sanctions", "UA") is None


def test_emerging_hotspot_classes():
    z = np.array([0.1, 0.2, 0.0, 0.3, 2.5])
    p = np.array([0.9, 0.8, 0.9, 0.7, 0.01])
    assert emerging_class(z, p)[0] == "new_hot"
    z = np.linspace(2.0, 6.0, 12)
    assert emerging_class(z, np.full(12, 0.001))[0] == "intensifying_hot"
    assert emerging_class(-np.full(12, 3.0) + np.random.default_rng(0).normal(0, 0.1, 12), np.full(12, 0.001))[0] == "persistent_cold"
    z = np.array([0, 0, 0, 2.5, 2.6, 2.7])
    p = np.array([0.5, 0.5, 0.5, 0.01, 0.01, 0.01])
    assert emerging_class(z, p)[0] == "consecutive_hot"
    tau, mp = mann_kendall(np.arange(20.0))
    assert tau == pytest.approx(1.0) and mp < 1e-4


def test_fill_z_matches_definition():
    d0 = date(2026, 1, 1)
    g = np.random.default_rng(2)
    n = g.integers(50, 150, 40)
    df = pl.DataFrame({"date": [d0 + timedelta(days=i) for i in range(40)], "label": "US", "theme": -1,
                       "ev_n": n.astype(np.int32), "art_n": np.zeros(40, np.int32), "ev_conflict_n": np.zeros(40, np.int32),
                       "ev_goldstein": None, "ev_tone": np.full(40, -1.0, np.float32), "art_tone": None,
                       "art_neg_share": None, "attention_z": None, "tone_z": None},
                      schema_overrides={"ev_goldstein": pl.Float32, "art_tone": pl.Float32, "art_neg_share": pl.Float32,
                                        "attention_z": pl.Float32, "tone_z": pl.Float32})
    z = fill_z(df)["attention_z"].to_numpy()
    a = np.log1p(n)
    k = 25
    ref = (a[k] - a[:k].mean()) / a[:k].std(ddof=1)
    assert z[k] == pytest.approx(ref, rel=1e-4)
    assert np.isnan(z[5]) or z[5] is None
