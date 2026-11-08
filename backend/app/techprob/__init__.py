"""Technicals v2 probability models (docs/TECHNICALS_V2_PLAN.md T2-T4, validation docs/TECHNICALS_V2_VALIDATION.md).

    barriers    T2 two-barrier first-passage probabilities (empirical grid estimator, Brownian baseline, expected R)
    history     point-in-time reconstruction of zones / setups / scores at historical origins + their outcomes
    zone_model  T3 P(hold | touch) calibrated classifier, random-control pipeline and edge vs random
    score_cal   T4 percentile sub-scores and score-bucket -> forward-outcome mapping
    calib       reliability / ECE / Brier / log-loss / block bootstrap helpers
    job         nightly `technicals_probabilities` + weekly `technicals_probabilities_fit`
    serve       query-time helpers for the API

Everything here is MODEL-BASED and marginal over history: "calibrated on history", never "X% chance today".
"""
