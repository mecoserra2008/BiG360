"""P2 application helpers (synthetic data, no lake access)."""

from __future__ import annotations

import numpy as np

from app.conformal import envelope as V
from app.conformal import scores as S
from app.conformal.learned import horizon_cov_from_segments


def test_nested_score_equals_dense_mahalanobis():
    rng = np.random.default_rng(0)
    T, N = 50, 7
    s2 = rng.uniform(1e-4, 4e-4, (T, N))
    J = rng.uniform(0, 1e-3, (T, N))
    ke = rng.integers(0, 21, (T, N)).astype(np.int16)
    Vh = V.horizon_V(s2, J, ke)
    Y = rng.normal(size=(T, N, 4)) * np.sqrt(Vh)
    R = V.nested_score(Y, 0.0, Vh)
    idx = np.minimum.outer(np.arange(4), np.arange(4))
    Sig = Vh[..., idx]
    R2 = S.mahalanobis(Y.reshape(-1, 4), S.Cov(dense=Sig.reshape(-1, 4, 4))).reshape(T, N)
    np.testing.assert_allclose(R, R2, rtol=1e-8)


def test_horizon_segments_shape_is_psd_and_nested():
    v = np.array([1.0, 2.0, 0.5, 1.5])
    Sg = horizon_cov_from_segments(v)
    assert np.all(np.linalg.eigvalsh(Sg) > 0)
    assert Sg[3, 1] == Sg[1, 1] and Sg[2, 0] == Sg[0, 0]


def test_ewma_var_skips_earnings_days():
    rng = np.random.default_rng(1)
    r = rng.normal(0, 0.01, (300, 2))
    E = np.zeros_like(r, dtype=bool)
    r[200, 0] = 0.25
    E[200, 0] = True
    s2 = V.ewma_var(r, E)
    assert s2[201, 0] < 5e-4                     # the earnings jump does not inflate the normal-day variance
    J = V.jump_var(r, E, s2)
    assert J[201, 0] > J[199, 0]                 # it feeds the jump term instead


def test_poolcal_lag_and_event_pools():
    rng = np.random.default_rng(2)
    T, N = 900, 40
    R = rng.chisquare(4, (T, N))
    ev = np.zeros((T, N), dtype=bool)
    ev[:, :10] = True
    R[:, :10] *= 3.0                              # event origins have larger scores
    pool = np.zeros(N, dtype=np.int64)
    pc = V.PoolCal(R, pool, ev, lag=20, window=300, min_n=100).run(600)
    qn, qe = pc.q[700, 0, 0, 1], pc.q[700, 0, 1, 1]
    assert qe > 2 * qn
    cov = (R[600:] <= np.where(ev[600:], pc.q[600:, 0, 1, 1][:, None], pc.q[600:, 0, 0, 1][:, None])).mean()
    assert abs(cov - 0.9) < 0.02


def test_touch_probability_monotone_and_bounded():
    rng = np.random.default_rng(3)
    M = np.abs(rng.normal(size=5000)) * 1.2
    curve = np.quantile(M, V.TOUCH_PROBS)
    ps = [V.touch_probability(100 * np.exp(x * 0.05), 100.0, 5, 0.0025, curve, curve) for x in (0.1, 0.5, 1.0, 2.0, 4.0)]
    assert all(a >= b for a, b in zip(ps, ps[1:], strict=False))
    assert 0 < ps[-1] < 0.02 and ps[0] > 0.9


def test_running_extremes_and_targets_mask_bad_bars():
    T, N = 60, 1
    C = np.full((T, N), 100.0)
    C[30] = 10000.0                               # unit flip
    a = V.make_arrays(C, C * 1.01, C * 0.99, C, np.zeros((T, N), dtype=bool))
    Y = V.forward_targets(a)
    assert np.isnan(Y[25, 0, 1]) and np.isfinite(Y[40, 0, 1])
    up, dn = V.running_extremes(a)
    assert np.isnan(up[25, 0, 1]) and abs(up[40, 0, 1] - np.log(1.01)) < 1e-9
