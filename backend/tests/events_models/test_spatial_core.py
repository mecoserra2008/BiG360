"""Spatial / spillover estimation core (app.modeling._spatial) on synthetic data with known parameters."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.modeling import _spatial as SP


def lattice(n: int) -> np.ndarray:
    """Rook contiguity on an n x n grid (binary)."""
    N = n * n
    W = np.zeros((N, N))
    for r in range(n):
        for c in range(n):
            i = r * n + c
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                rr, cc = r + dr, c + dc
                if 0 <= rr < n and 0 <= cc < n:
                    W[i, rr * n + cc] = 1.0
    return W


def sar_panel_data(rho: float, beta: np.ndarray, theta: np.ndarray | None = None, n: int = 7, T: int = 40, seed: int = 0):
    g = np.random.default_rng(seed)
    W = lattice(n)
    Wr = SP.row_standardize(W)
    N = n * n
    A = np.linalg.inv(np.eye(N) - rho * Wr)
    alpha = g.normal(size=N)
    X = g.normal(size=(T * N, len(beta)))
    ys = []
    for t in range(T):
        Xt = X[t * N:(t + 1) * N]
        mu = alpha + Xt @ beta + (Wr @ Xt @ theta if theta is not None else 0)
        ys.append(A @ (mu + g.normal(size=N)))
    return np.concatenate(ys), X, W, N


def test_sar_panel_recovers_rho_and_beta():
    y, X, W, N = sar_panel_data(0.45, np.array([1.0, -0.5]))
    f = SP.sar_panel(y, X, W, N, fe="entity")
    assert f["rho"] == pytest.approx(0.45, abs=0.05)
    assert f["beta"] == pytest.approx([1.0, -0.5], abs=0.05)
    k = f["k"]
    assert 0.005 < f["se"][k] < 0.05


def test_sar_cross_section_matches_spreg():
    spreg = pytest.importorskip("spreg")
    libpysal = pytest.importorskip("libpysal")
    g = np.random.default_rng(3)
    w = libpysal.weights.lat2W(9, 9)
    w.transform = "r"
    W = w.full()[0]
    N = 81
    X = g.normal(size=(N, 2))
    y = np.linalg.solve(np.eye(N) - 0.5 * W, 1 + X @ [1.0, -2.0] + g.normal(size=N))
    f = SP.sar_panel(y, X, W, N, fe="none")
    m = spreg.ML_Lag(y[:, None], X, w=w, method="full")
    assert f["rho"] == pytest.approx(float(m.rho), abs=1e-5)
    assert f["beta"] == pytest.approx(m.betas.ravel()[:3], abs=1e-4)


def test_sem_and_sdm_effects():
    y, X, W, N = sar_panel_data(0.0, np.array([1.0]), seed=4)
    # SEM data: u = lambda W u + e
    g = np.random.default_rng(5)
    Wr = SP.row_standardize(W)
    T = len(y) // N
    B = np.linalg.inv(np.eye(N) - 0.5 * Wr)
    alpha = g.normal(size=N)
    ys = [alpha + X[t * N:(t + 1) * N, 0] * 2.0 + B @ g.normal(size=N) for t in range(T)]
    fe = SP.sem_panel(np.concatenate(ys), X, W, N)
    assert fe["lambda"] == pytest.approx(0.5, abs=0.07) and fe["beta"][0] == pytest.approx(2.0, abs=0.06)
    # SDM: direct / indirect effects vs the analytic values
    rho, b, th = 0.3, np.array([1.0]), np.array([0.5])
    y, X, W, N = sar_panel_data(rho, b, th, seed=6)
    f = SP.sar_panel(y, X, W, N, durbin=True)
    eff = SP.effects(f, 1, draws=300, seed=1)[0]
    Wr = SP.row_standardize(W)
    S = np.linalg.solve(np.eye(N) - rho * Wr, b[0] * np.eye(N) + th[0] * Wr)
    assert eff["direct"] == pytest.approx(np.trace(S) / N, abs=0.06)
    assert eff["total"] == pytest.approx((b[0] + th[0]) / (1 - rho), abs=0.15)
    assert eff["indirect_lo"] < eff["indirect"] < eff["indirect_hi"]
    M = SP.impact_matrix(f, 0, 1)
    assert M.shape == (N, N) and (M - np.diag(np.diag(M))).sum() / N == pytest.approx(eff["indirect"], rel=1e-6)


def test_lm_tests_point_to_lag():
    y, X, W, N = sar_panel_data(0.5, np.array([1.0]), seed=7)
    lm = SP.lm_tests(y, X, W, N)
    assert lm["lm_lag"][1] < 1e-6 and lm["rlm_lag"][1] < 0.01


def test_moran_lisa_planted_cluster():
    g = np.random.default_rng(8)
    n = 12
    W = lattice(n)
    x = g.normal(size=n * n)
    cluster = [r * n + c for r in range(4) for c in range(4)]
    x[cluster] += 3.0
    m = SP.moran_perm(x, W, 499, 0)
    assert m["I"] > 0.3 and m["p_sim"] <= 0.01
    ref = SP.moran_numpy(x, W)
    assert ref["I"] == pytest.approx(m["I"], rel=1e-9)
    L = SP.lisa_perm(x, W, 499, 0)
    hh = [i for i in cluster if L["q"][i] == 1 and L["p_sim"][i] < 0.05]
    assert len(hh) >= 10
    gz, gp = SP.gi_star(x, W)
    assert np.mean(gz[cluster] > 1.645) > 0.6
    # no clustering -> not significant
    m0 = SP.moran_perm(g.normal(size=n * n), W, 499, 1)
    assert m0["p_sim"] > 0.01


def test_lisa_statistic_and_null_calibration():
    # statistic = Anselin's local Moran (esda.Moran_Local up to its (n-1)/n scaling; esda's numba kernels are not
    # used here because their JIT compile costs ~70 s per process); p-values ~ uniform under the null
    g = np.random.default_rng(9)
    W = lattice(10)
    Wr = SP.row_standardize(W)
    x = g.normal(size=100)
    L = SP.lisa_perm(x, W, 999, 0)
    z = (x - x.mean()) / x.std()
    assert L["Is"] == pytest.approx(z * (Wr @ z) / (z @ z / 100))
    ps = np.concatenate([SP.lisa_perm(g.normal(size=100), W, 199, k)["p_sim"] for k in range(20)])
    assert 0.06 < (ps < 0.05).mean() < 0.14     # folded (directed) pseudo p as in esda: ~2 alpha under the null


def test_dy_connectedness_simulated_var():
    g = np.random.default_rng(10)
    T, k = 4000, 3
    A = np.array([[0.5, 0.0, 0.0], [0.4, 0.2, 0.0], [0.4, 0.0, 0.2]])   # series 0 drives 1 and 2
    Y = np.zeros((T, k))
    for t in range(1, T):
        Y[t] = A @ Y[t - 1] + g.normal(size=k)
    est = SP.dy_connectedness(Y, 1, 10)
    true = SP.connectedness_table(SP.gfevd([A], np.eye(k), 10))
    assert est["total"] == pytest.approx(true["total"], abs=2.0)
    assert np.argmax(est["net"]) == 0 and est["net"][0] > 10
    assert est["pairwise_net"][0, 1] > 0 and est["pairwise_net"][0, 1] == pytest.approx(-est["pairwise_net"][1, 0])
    roll = SP.rolling_connectedness(pd.DataFrame(Y), window=500, step=500)
    assert len(roll) >= 7 and all(abs(r[2]["total"] - true["total"]) < 6 for r in roll)


def test_local_projection_recovers_irf():
    g = np.random.default_rng(11)
    T = 3000
    s = g.normal(size=T)
    irf = np.array([1.0, 0.6, 0.3, 0.1, 0.0])
    y = np.convolve(s, irf)[:T] + g.normal(scale=0.5, size=T)
    df = pd.DataFrame({"date": pd.date_range("2000-01-01", periods=T), "y": y, "s": s})
    out = SP.local_projection(df, "y", ["s"], H=4, y_lags=0, cumulative=False, fe="none")
    assert out["coef"].to_numpy() == pytest.approx(irf, abs=0.06)
    cum = SP.local_projection(df, "y", ["s"], H=4, y_lags=0, cumulative=True, fe="none")
    assert cum["coef"].iloc[-1] == pytest.approx(irf.sum(), abs=0.15)


def test_spatial_var_irf():
    g = np.random.default_rng(12)
    W = lattice(5)
    Wr = SP.row_standardize(W)
    N, T = 25, 600
    phi, psi, c, d = 0.3, 0.2, 1.0, 0.5
    S = g.normal(size=(T, N))
    Y = np.zeros((T, N))
    for t in range(1, T):
        Y[t] = phi * Y[t - 1] + psi * Wr @ Y[t - 1] + c * S[t] + d * Wr @ S[t] + g.normal(scale=0.5, size=N)
    f = SP.spatial_var(Y, S, W)
    assert f["params"] == pytest.approx([phi, psi, c, d], abs=0.05)
    r = SP.spatial_var_irf(f, H=5, unit=12)
    assert r[0, 12] == pytest.approx(c, abs=0.05) and r[0, 13] > 0.05      # neighbour of the shocked unit responds
    pt, se, lo, hi = SP.spatial_var_irf_bands(f, H=5, unit=12, draws=100, agg="others")
    assert (lo <= pt + 1e-12).all() and (pt <= hi + 1e-12).all()
