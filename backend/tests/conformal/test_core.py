"""P1 core library tests (docs/CONFORMAL_TECHNICALS_PLAN.md): exact oracle validity (Proposition 8), Woodbury vs
dense, projection = support function, ACI under a volatility regime shift, leakage checks, affine invariance."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.conformal import aci as A
from app.conformal import scores as S
from app.conformal.filters import EWMAFilter, FactorFilter, GarchFilter, StaticLWFilter, garch_grid_fit
from app.conformal.leakage import PeekingFilter, causal_invariance, shift_check
from app.conformal.protocol import Calibrator, chronological_split, mc_band, run_split


def _rand_spd(rng, N, T=1):
    M = rng.normal(size=(T, N, N))
    return np.einsum("tij,tkj->tik", M, M) + 0.5 * np.eye(N)


# ------------------------------------------------------------------------------------------------ scores


def test_conformal_index_and_quantile_edges():
    assert S.conformal_index(19, 0.1) == 18
    assert S.conformal_index(99, 0.05) == 95
    s = np.arange(1, 20, dtype=float)
    assert S.conformal_quantile(s, 0.1) == 18.0
    assert math.isinf(S.conformal_quantile(s[:5], 0.1))       # ceil(6 * 0.9) = 6 > 5 -> unbounded
    assert S.conformal_quantile(s, 1.0) == 0.0
    q = S.conformal_quantile(s, np.array([0.2, 0.1, 0.05]))
    assert q.tolist() == [16.0, 18.0, 19.0]


def test_woodbury_equals_dense():
    rng = np.random.default_rng(1)
    T, N, r = 40, 12, 4
    d = rng.uniform(0.1, 2.0, (T, N))
    L = rng.normal(size=(T, N, r))
    lr = S.Cov(d=d, L=L)
    de = S.Cov(dense=lr.to_dense())
    x = rng.normal(size=(T, N))
    np.testing.assert_allclose(S.mahalanobis(x, lr), S.mahalanobis(x, de), rtol=1e-9)
    np.testing.assert_allclose(S.logdet(lr), S.logdet(de), rtol=1e-10)
    np.testing.assert_allclose(S.solve(lr, x), S.solve(de, x), rtol=1e-8, atol=1e-10)
    np.testing.assert_allclose(S.inv_diag(lr), S.inv_diag(de), rtol=1e-8)
    np.testing.assert_allclose(lr.diag(), de.diag(), rtol=1e-12)
    A_ = rng.normal(size=(3, N))
    np.testing.assert_allclose(S.quad_form(lr, A_), S.quad_form(de, A_), rtol=1e-10)
    dec = S.decompose(x, lr)
    np.testing.assert_allclose(dec["contrib"].sum(1), S.mahalanobis(x, lr), rtol=1e-8)


def test_to_lowrank_preserves_diagonal():
    rng = np.random.default_rng(2)
    Sd = _rand_spd(rng, 10, 3)
    c = S.to_lowrank(Sd, 4)
    np.testing.assert_allclose(c.diag(), np.diagonal(Sd, axis1=1, axis2=2), rtol=1e-10)


def test_projection_equals_support_function():
    rng = np.random.default_rng(3)
    N = 4
    Sig = _rand_spd(rng, N)[0]
    mu = rng.normal(size=N)
    q = 7.3
    a = rng.normal(size=N)
    lo, hi = S.project_band(mu[None], S.Cov(dense=Sig), q, a[None])
    # maximiser of a'y on the ellipsoid: y* = mu + sqrt(q) Sigma a / sqrt(a' Sigma a); it lies on the boundary
    sa = Sig @ a
    ystar = mu + math.sqrt(q) * sa / math.sqrt(a @ sa)
    assert abs(S.mahalanobis((ystar - mu)[None], S.Cov(dense=Sig))[0] - q) < 1e-9
    assert abs(a @ ystar - hi[0, 0]) < 1e-10
    # no boundary point exceeds the support value; the sampled max approaches it
    U = rng.normal(size=(200_000, N))
    U /= np.linalg.norm(U, axis=1, keepdims=True)
    Lc = np.linalg.cholesky(Sig)
    Yb = mu + math.sqrt(q) * U @ Lc.T
    proj = Yb @ a
    assert proj.max() <= hi[0, 0] + 1e-9 and proj.min() >= lo[0, 0] - 1e-9
    assert hi[0, 0] - proj.max() < 0.02 * (hi[0, 0] - lo[0, 0])
    # coordinate half-widths are the e_i projections
    h = S.coord_halfwidth(S.Cov(dense=Sig), q)
    lo_e, hi_e = S.project_band(mu[None], S.Cov(dense=Sig), q, np.eye(N))
    np.testing.assert_allclose((hi_e - lo_e)[0] / 2, h[0], rtol=1e-12)


def test_mahalanobis_affine_invariance():
    rng = np.random.default_rng(4)
    T, N = 25, 5
    Sig = _rand_spd(rng, N, T)
    mu = rng.normal(size=(T, N))
    y = rng.normal(size=(T, N)) * 3
    Am = rng.normal(size=(N, N)) + 3 * np.eye(N)
    b = rng.normal(size=N)
    R1 = S.mahalanobis(y - mu, S.Cov(dense=Sig))
    y2, mu2 = y @ Am.T + b, mu @ Am.T + b
    Sig2 = np.einsum("ij,tjk,lk->til", Am, Sig, Am)
    R2 = S.mahalanobis(y2 - mu2, S.Cov(dense=Sig2))
    np.testing.assert_allclose(R1, R2, rtol=1e-8)


def test_log_volume_matches_chi2_ellipsoid_in_1d():
    # N = 1: the ellipsoid is [mu - sqrt(q s2), mu + sqrt(q s2)], length 2 sqrt(q s2); v_1 = 2
    lv = S.log_volume(4.0, np.log(np.array([9.0])), 1)
    assert abs(lv[0] - math.log(2 * 2.0 * 3.0)) < 1e-12


# ------------------------------------------------------------------------------------------------ Proposition 8


@pytest.mark.parametrize("innov,n,alpha", [("gauss", 19, 0.1), ("t3", 19, 0.1), ("gauss", 99, 0.05)])
def test_oracle_exact_validity(innov, n, alpha):
    """Y_t = mu_t + Sigma_t^{1/2} Z_t with i.i.d. spherically symmetric Z: R_t i.i.d., split conformal is exactly
    valid: E coverage in [1 - alpha, 1 - alpha + 1/(n+1)] (Theorem 1 + Proposition 8), within Monte-Carlo error."""
    rng = np.random.default_rng(10 + n)
    reps, N, r = 6000, 5, 2
    T = reps * (n + 1)
    # time-varying oracle: diag + low rank with a GARCH-like scale path
    scale = np.exp(np.cumsum(rng.normal(0, 0.05, T)) * 0.1)
    d = rng.uniform(0.2, 1.0, (T, N)) * scale[:, None]
    L = rng.normal(size=(T, N, r)) * np.sqrt(scale)[:, None, None]
    cov = S.Cov(d=d, L=L)
    if innov == "gauss":
        Z = rng.normal(size=(T, N))
    else:  # multivariate t3: spherical, heavy tailed
        Z = rng.normal(size=(T, N)) / np.sqrt(rng.chisquare(3, T) / 3)[:, None]
    Sd = cov.to_dense()
    Y = np.einsum("tij,tj->ti", np.linalg.cholesky(Sd), Z)
    R = S.mahalanobis(Y, cov).reshape(reps, n + 1)
    k = S.conformal_index(n, alpha)
    qh = np.sort(R[:, :n], axis=1)[:, k - 1]
    cover = (R[:, n] <= qh).mean()
    lo, hi = mc_band(alpha, n, reps, z=3.5)
    assert lo <= cover <= hi, (cover, lo, hi)
    assert abs(cover - k / (n + 1)) < 3.5 * math.sqrt(alpha * (1 - alpha) / reps)


# ------------------------------------------------------------------------------------------------ ACI


def _regime_series(rng, T=3000, N=3, shift_at=1500, mult=2.0):
    Sig = _rand_spd(rng, N)[0] * 0.1
    Lc = np.linalg.cholesky(Sig)
    X = rng.normal(size=(T, N)) @ Lc.T
    X[shift_at:] *= mult
    return X


def test_aci_restores_coverage_under_vol_shift():
    rng = np.random.default_rng(5)
    X = _regime_series(rng)
    T = len(X)
    sp = chronological_split(T, (0.25, 0.25, 0.5))
    f = StaticLWFilter().fit(X[: sp.pretrain_end])
    mu, cov = f.emit(X)
    R = np.full(T, np.nan)
    R[:-1] = S.mahalanobis(X[1:] - mu[:-1], cov.take(slice(0, T - 1)))
    test = np.arange(sp.test_start, T - 1)
    cal = Calibrator(R, start=sp.cal_start, window=None, fixed_end=sp.test_start)
    plain = run_split(R, cal, test, alphas=(0.1,))
    cov_plain = np.nanmean(plain[0.1]["covered"])
    assert cov_plain < 0.75                                 # static shape + frozen radius break under the shift
    wrapped = run_split(R, cal, test, alphas=(0.1,), aci="aci", gamma=0.01)
    cov_aci = np.nanmean(wrapped[0.1]["covered"])
    assert abs(cov_aci - 0.9) < 0.02, cov_aci
    ag = run_split(R, cal, test, alphas=(0.1,), aci="agaci")
    assert abs(np.nanmean(ag[0.1]["covered"]) - 0.9) < 0.03


def test_aci_long_run_bound_with_delay():
    rng = np.random.default_rng(6)
    T = 4000
    Rs = rng.chisquare(3, T) * np.where(np.arange(T) > 2000, 3.0, 1.0)
    cal_scores = np.sort(rng.chisquare(3, 500))

    def qfun(t, a):
        return float(S.quantile_from_sorted(cal_scores, a)) if a > 0 else float("inf")

    res = A.aci_path(Rs, qfun, 0.1, gamma=0.01, lag=5)
    err = np.nanmean(res["err_t"])
    assert abs(err - 0.1) <= A.long_run_bound(0.1, 0.01, T, lag=5) + 0.01


def test_pooled_rolling_calibrator_respects_lag():
    R = np.arange(100, dtype=float)[:, None] * np.ones((1, 3))
    cal = Calibrator(R, start=0, window=10, lag=5)
    s = cal.sorted_at(50)
    # decision at t = 50 with lag 5: origins 35..44 (outcomes known), never 45..50
    assert s.min() == 35 and s.max() == 44 and len(s) == 30


# ------------------------------------------------------------------------------------------------ filters + leakage


def _iid(rng, T=600, N=4):
    return rng.normal(size=(T, N)) * 0.01


def _filters(N):
    return [EWMAFilter(), EWMAFilter(lam_corr=0.985, shrink=0.1), EWMAFilter(mean="ewma"), StaticLWFilter(),
            FactorFilter(r=2), FactorFilter(r=2, vol_scale=True), GarchFilter(corr="diag"), GarchFilter(corr="dcc")]


def test_filters_pass_leakage_checks():
    rng = np.random.default_rng(7)
    X = _iid(rng)
    for f in _filters(X.shape[1]):
        f.fit(X[:200])
        ci = causal_invariance(f, X)
        assert ci["passed"], (f.name, ci)
        sc = shift_check(f, X)
        assert sc["passed"], (f.name, sc)


def test_peeking_filter_is_caught():
    rng = np.random.default_rng(8)
    X = _iid(rng)
    f = PeekingFilter(inner=EWMAFilter()).fit(X[:200])
    assert not causal_invariance(f, X)["passed"]
    sc = shift_check(f, X)
    assert not sc["passed"] and sc["ratio"] < 0.5, sc


def test_gcn_gru_trains_and_passes_leakage():
    pytest.importorskip("torch")
    from app.conformal.learned import GCNGRUFilter, graph_shift

    rng = np.random.default_rng(9)
    N, T = 6, 260
    A_ = (rng.uniform(size=(N, N)) > 0.5).astype(float)
    Sg = graph_shift(A_)
    assert np.allclose(Sg, Sg.T) and np.linalg.norm(Sg, 2) <= 1 + 1e-9
    X = _iid(rng, T, N)
    f = GCNGRUFilter(A=A_, epochs=2, d=8).fit(X[:150])
    mu, cov = f.emit(X)
    assert cov.lowrank and cov.L.shape == (T, N, 4) and (cov.d > 0).all()
    assert causal_invariance(f, X, tol=1e-6)["passed"]
    assert shift_check(f, X)["passed"]


def test_garch_grid_recovers_persistence():
    rng = np.random.default_rng(11)
    T, a, b = 4000, 0.08, 0.90
    x = np.empty(T)
    s2 = 1e-4
    for t in range(T):
        x[t] = math.sqrt(s2) * rng.normal()
        s2 = 1e-4 * (1 - a - b) + a * x[t] ** 2 + b * s2
    p = garch_grid_fit(x[:, None])
    assert abs((p["a"] + p["b"])[0] - 0.98) < 0.03


def test_protocol_ewma_near_target_on_garch_data():
    rng = np.random.default_rng(12)
    T, N = 3000, 3
    x = np.empty((T, N))
    s2 = np.full(N, 1e-4)
    C = np.linalg.cholesky(np.array([[1, .5, .3], [.5, 1, .4], [.3, .4, 1]]))
    for t in range(T):
        x[t] = np.sqrt(s2) * (C @ rng.normal(size=N))
        s2 = 1e-4 * 0.03 + 0.07 * x[t] ** 2 + 0.9 * s2
    sp = chronological_split(T)
    f = EWMAFilter(lam_corr=0.985).fit(x[: sp.pretrain_end])
    mu, cov = f.emit(x)
    R = np.full(T, np.nan)
    R[:-1] = S.mahalanobis(x[1:] - mu[:-1], cov.take(slice(0, T - 1)))
    cal = Calibrator(R, start=sp.cal_start, window=500, lag=0)
    out = run_split(R, cal, np.arange(sp.test_start, T - 1), alphas=(0.1, 0.05))
    assert abs(np.nanmean(out[0.1]["covered"]) - 0.9) < 0.03
    assert abs(np.nanmean(out[0.05]["covered"]) - 0.95) < 0.025
