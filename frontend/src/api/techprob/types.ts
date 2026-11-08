/**
 * Technicals v2 T2 / T3 / T4 (contract v1.34.0; docs/TECHNICALS_V2_PLAN.md, gate docs/TECHNICALS_V2_VALIDATION.md):
 *   GET /technicals/{security_id}/setup-probabilities   two-barrier P(win) / P(loss) / P(neither), expected R, break-even
 *   GET /technicals/{security_id}/zones                 zones + P(hold | touch), edge vs random, P(touch) x P(hold)
 *   GET /technicals/{security_id}/score-calibration     percentile sub-scores + score-bucket historical outcomes
 * Model-based, calibrated on history. Outputs that failed the T6 gate carry experimental = true (hidden unless toggled).
 */
import type { components } from '../generated/schema'

export type TpGate = components['schemas']['TechnicalsGate']
export type TpSetupHorizon = components['schemas']['TechnicalsSetupHorizon']
export type TpSetup = components['schemas']['TechnicalsSetupProbabilities']
export type TpZone = components['schemas']['TechnicalsZone']
export type TpZones = components['schemas']['TechnicalsZones']
export type TpScoreBucket = components['schemas']['TechnicalsScoreBucket']
export type TpScore = components['schemas']['TechnicalsScoreCalibration']
