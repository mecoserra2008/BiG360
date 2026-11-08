"""Technicals v3 #2: meta-labelled setups (triple barrier; Lopez de Prado, AFML ch. 3-4).

Primary model = the existing setup generator (direction, entry, T1, invalidation; app.technicals.confluence) with the
v2 T2 two-barrier probabilities (app.techprob). Secondary (meta) model = P(primary setup wins | context), calibrated:

    features   point-in-time feature construction shared by the history build and the nightly scoring (bar context:
               squeeze percentile, ADX, trend alignment; market regime, risk gauge, VIX; peer break-away; insider /
               short interest / smart money; earnings proximity; sector / region; T2 base + setup geometry)
    model      regularised logistic regression and XGBoost (base margin = T2 logit) with Platt / isotonic calibration,
               average-uniqueness sample weights, expected R, take / skip, capped fractional Kelly, contributions
    job        weekly `tech_meta_fit` + nightly `tech_meta_score`
    serve      query-time helpers for GET /technicals/{id}/meta-setup and the thesis view
    state      data/state/technicals_v3.json key "meta"

Gate: docs/TECHNICALS_V3_VALIDATION.md section "Meta-labelling"; raw results data/reports/technicals_v3/meta/.
Everything here is MODEL-BASED: "calibrated on history", never "X% chance today".
"""
