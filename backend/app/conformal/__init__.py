"""Filtered conformal ellipsoids (docs/CONFORMAL_TECHNICALS_PLAN.md; Limmer 2026, arXiv:2606.17014v2).

A frozen filter emits a one-step predictive mean and covariance (Y_hat_{t|t-1}, Sigma_hat_{t|t-1}) strictly before Y_t
is observed; split-conformal calibration of the squared Mahalanobis score R_t = r_t' Sigma_hat^-1 r_t sets the scalar
radius q_hat, so the prediction set is the ellipsoid {y : (y - Y_hat)' Sigma_hat^-1 (y - Y_hat) <= q_hat}.

Modules
    scores    Mahalanobis (dense / diagonal + low rank via Woodbury), conformal quantile, projections, w_proj, log-volume
    aci       ACI (Gibbs & Candes 2021) and AgACI (Zaffran et al. 2022) online level adaptation
    filters   EWMA, Ledoit-Wolf static, PCA factor, GARCH-diag / DCC-lite, learned GCN-GRU (torch CPU)
    protocol  Algorithm 1: chronological pretrain / calibration / test, warm-up discard, rolling recalibration, ACI
    leakage   causal-invariance and shift checks for filters

Everything here is MODEL-BASED. Coverage is marginal (over time), never conditional on a given day.
"""

LIB_VERSION = "1.0"
