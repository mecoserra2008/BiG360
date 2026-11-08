"""Roadmap #9 propagation math (app.supply_chain.contagion): toy graphs with hand-computed answers."""

from __future__ import annotations

import pytest

from app.supply_chain.contagion import (Edge, aggregate_exposure, country_seeds, path_to, propagate,
                                        statistical_channel)

# A supplies B (30% of A's revenue), B supplies C (20% of B's revenue); C buys 40% of its inputs from B
EDGES = [Edge("A", "B", s_rev=0.3, s_pur=None, confidence=1.0), Edge("B", "C", s_rev=0.2, s_pur=0.4, confidence=1.0)]


def test_demand_channel_damped_hops():
    r = propagate(EDGES, {"C": -0.10}, damping=0.5, hops=3, supply=False)
    assert r.impact["C"] == pytest.approx(-0.10)
    assert r.impact["B"] == pytest.approx(0.2 * -0.10)                  # hop 1 undamped
    assert r.impact["A"] == pytest.approx(0.5 * 0.3 * 0.2 * -0.10)      # hop 2 x damping
    assert r.first_hop == {"C": 0, "B": 1, "A": 2}
    assert r.channel["B"] == "demand" and path_to(r, "A") == ["C", "B", "A"]
    assert r.by_hop["A"] == pytest.approx([0.0, 0.0, -0.003, 0.0])


def test_supply_channel_uses_purchase_share_and_factor():
    r = propagate(EDGES, {"B": -0.5}, damping=0.5, hops=1, demand=False, supply_factor=0.5)
    assert r.impact["C"] == pytest.approx(0.5 * 0.4 * -0.5)
    assert "A" not in r.impact or r.impact["A"] == 0                     # no demand channel
    r2 = propagate(EDGES, {"A": -0.5}, hops=1, demand=False, default_share=0.05, supply_factor=0.5)
    assert r2.impact["B"] == pytest.approx(0.5 * 0.05 * -0.5)            # missing s_pur -> default share, flagged
    assert ("A", "B") in r2.assumed


def test_confidence_scales_transmission_and_clip():
    e = [Edge("A", "B", s_rev=1.0, confidence=0.5), Edge("B", "A", s_rev=1.0, confidence=1.0)]
    r = propagate(e, {"B": -0.2}, damping=1.0, hops=4, supply=False)
    assert abs(r.impact["A"]) <= 0.2 + 1e-12 and abs(r.impact["B"]) <= 0.2 + 1e-12   # cycle clipped to |seed|
    r1 = propagate(e, {"B": -0.2}, hops=1, supply=False)
    assert r1.impact["A"] == pytest.approx(0.5 * -0.2)


def test_zero_hops_and_unknown_nodes():
    r = propagate(EDGES, {"Z": 0.1}, hops=2)
    assert r.impact == {"Z": pytest.approx(0.1)}


def test_country_seeds_statistical_and_aggregation():
    expo = {"A": {"CN": 0.5, "US": 0.5}, "B": {"US": 1.0}}
    assert country_seeds(expo, "CN", -0.1) == {"A": pytest.approx(-0.05)}
    st = statistical_channel([("A", "N1", 0.8), ("A", "B", 0.6), ("X", "N2", 0.9)], {"A": -0.1, "B": -0.01}, damping=0.5)
    assert st == {"N1": pytest.approx(-0.04)}
    exp = {"A": {"direct": {"CN": 0.5, "US": 0.5}}, "B": {"direct": {"US": 1.0}}, "C": {"direct": {}}}
    agg = aggregate_exposure({"A": 1.0, "B": 3.0, "C": 5.0}, exp)
    assert agg == {"CN": pytest.approx(0.125), "US": pytest.approx(0.875)}
