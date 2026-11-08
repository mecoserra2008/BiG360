"""app.ml.macro_regime: forward-filter PIT safety and state-alignment (label-switching) stability."""

from __future__ import annotations

import numpy as np
import pytest

from app.ml.macro_regime import _align_to_reference, forward_filter, label_states


def _toy_2state():
    startprob = np.array([0.5, 0.5])
    transmat = np.array([[0.9, 0.1], [0.1, 0.9]])
    means = np.array([[-2.0], [2.0]])
    covars = np.array([[1.0], [1.0]])
    return startprob, transmat, means, covars


def test_forward_filter_probabilities_sum_to_one_every_row():
    startprob, transmat, means, covars = _toy_2state()
    rng = np.random.default_rng(0)
    X = np.concatenate([rng.normal(-2, 1, (20, 1)), rng.normal(2, 1, (20, 1))])
    probs = forward_filter(startprob, transmat, means, covars, X)
    assert probs.shape == (40, 2)
    np.testing.assert_allclose(probs.sum(axis=1), 1.0, atol=1e-8)
    assert (probs >= 0).all() and (probs <= 1).all()


def test_forward_filter_tracks_the_generating_state():
    """A strongly separated 2-state series: state 0 (mean -2) for t<20, state 1 (mean 2) for t>=20. The filter's
    argmax should recover that split (this is the PIT-safety-relevant property: it must do so using only
    observations up to t, which `forward_filter`'s alpha-recursion guarantees by construction)."""
    startprob, transmat, means, covars = _toy_2state()
    X = np.concatenate([np.full((20, 1), -2.0), np.full((20, 1), 2.0)])
    probs = forward_filter(startprob, transmat, means, covars, X)
    state = probs.argmax(axis=1)
    assert state[:18].sum() == 0          # first block confidently state 0 (allow brief transition lag)
    assert state[22:].sum() == len(state[22:])  # second block confidently state 1


def test_forward_filter_is_causal_not_smoothed():
    """Changing an observation *after* time t must not change the filtered probability *at* t (that would mean
    future information leaked backward -- exactly what the smoother, which this function deliberately is not,
    would do)."""
    startprob, transmat, means, covars = _toy_2state()
    X = np.concatenate([np.full((10, 1), -2.0), np.full((10, 1), 2.0)])
    probs_a = forward_filter(startprob, transmat, means, covars, X)
    X2 = X.copy()
    X2[15:] = -2.0  # rewrite the future (indices >= 15); t < 10 must be unaffected
    probs_b = forward_filter(startprob, transmat, means, covars, X2)
    np.testing.assert_allclose(probs_a[:10], probs_b[:10], atol=1e-10)


def test_align_to_reference_recovers_a_shuffled_permutation():
    """`idx = _align_to_reference(shuffled, ref)` must satisfy `shuffled[idx] == ref` elementwise (that is
    literally how every call site uses it: `means = means[idx]`), not merely map each row to *some* match."""
    ref_means = np.array([[-2.0, 0.0], [2.0, 0.0], [0.0, 3.0], [0.0, -3.0]])
    true_perm = np.array([2, 0, 3, 1])  # shuffled[i] = ref_means[true_perm[i]]
    shuffled = ref_means[true_perm]
    idx = _align_to_reference(shuffled, ref_means)
    np.testing.assert_allclose(shuffled[idx], ref_means)


def test_align_to_reference_is_identity_when_no_reference():
    means = np.array([[1.0, 2.0], [3.0, 4.0]])
    np.testing.assert_array_equal(_align_to_reference(means, None), [0, 1])


def test_label_states_assigns_expansion_to_highest_growth_and_distinguishes_stagflation():
    feats = ["growth", "inflation", "curve", "credit", "vol", "usd"]
    # state 0: low growth, high inflation -> stagflation; state 1: low growth, low inflation -> contraction
    # state 2 & 3: mid growth (recovery/slowdown by transition structure); state order picked so growth ranks are 0<{2,3}<1...
    means = np.zeros((4, 6))
    means[:, 0] = [-2.0, 2.0, -0.5, 0.5]   # growth: state0 lowest, state1 highest
    means[:, 1] = [1.5, 0.0, 0.0, 0.0]     # inflation: state0 high (above median) -> stagflation not contraction
    transmat = np.array([
        [0.8, 0.0, 0.1, 0.1],
        [0.0, 0.8, 0.1, 0.1],
        [0.3, 0.1, 0.5, 0.1],   # state2 (mid growth) leans toward the low-growth state -> slowdown
        [0.1, 0.3, 0.1, 0.5],   # state3 leans toward the high-growth state -> recovery
    ])
    labels = label_states(means, transmat, feats)
    assert labels[0] == "stagflation"
    assert labels[1] == "expansion"
    assert set(labels) == {"stagflation", "expansion", "slowdown", "recovery"}


@pytest.mark.parametrize("seed", range(5))
def test_label_states_always_returns_four_distinct_labels(seed):
    rng = np.random.default_rng(seed)
    means = rng.normal(size=(4, 6))
    transmat = rng.dirichlet(np.ones(4), size=4)
    labels = label_states(means, transmat, ["growth", "inflation", "curve", "credit", "vol", "usd"])
    assert len(labels) == 4
    assert len(set(labels)) == 4  # no state left unlabeled / duplicated


def test_regional_scopes_are_official_and_declared():
    """F27 (O2.2c): EU / JP / CN / KR regime models use only official series, declare their curve short leg and the
    true unit of every factor, and never reuse US proxies (no credit / vol / usd factor outside US / GLOBAL)."""
    from app.ml import macro_regime as MR

    for sc in ("EU", "JP", "CN", "KR"):
        spec = MR.REGION_SERIES[sc]
        assert {"growth", "inflation", "curve"} <= set(spec) and set(spec) <= set(MR.FEATURES)
        assert not ({"credit", "vol", "usd"} & set(spec))
        assert all(sid.split(":")[0] in ("eurostat", "ecb", "oecd", "imf", "bis") for sid, _ in spec.values())
        assert sc in MR.CURVE_SHORT_LEG and set(MR.REGION_UNITS[sc]) == set(spec)
        assert sc in MR.DEFAULT_SCOPES and sc in MR.SCOPE_SOURCES
    assert MR.DEFAULT_SCOPES[:2] == ["US", "GLOBAL"]
