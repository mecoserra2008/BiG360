"""Technicals v3, Bundle B #1 / #5 (docs/TECHNICALS_V3_IDEAS.md; gate docs/TECHNICALS_V3_VALIDATION.md).

    factors     #1 evidence-based technical factors (52-week-high proximity, 12-1 momentum, residual momentum,
                1-month reversal, MAX, idiosyncratic volatility, industry momentum): pure numpy on wide (T, n) arrays
    breakouts   #5 volume-confirmed breakouts: N-day high / low breakout events, volume / accumulation features,
                first-passage follow-through labels and geometry-matched random controls (pure numpy)
    stats       rank IC, Newey-West t, decile tables, BH / Holm adjustment, block bootstrap
    panel       point-in-time data access (prices_daily per region, chunked by security; reads only)
    build       per-region pipeline shared by the research walk-forward and the nightly jobs
    bo_model    calibrated P(follow-through) model (logistic + isotonic) and the confirmation score
    job         nightly `tech_factors` / `tech_breakouts`, monthly `tech_factor_eval`
    serve       query-time helpers for the API

Point-in-time: every value dated t uses bars <= t only; labels (forward returns, follow-through) start at t + 1.
Everything is MODEL-BASED / historical frequency: "calibrated on history", never "X% chance today".
"""
