/**
 * Technicals v3 #2 meta-labelled setups (contract v1.35.0; gate docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"):
 *   GET /technicals/{security_id}/meta-setup        context-adjusted P(win) / P(loss) / P(neither), expected R, take / skip,
 *                                                   quarter-Kelly risk fraction, top drivers vs the T2 base (H = 10 / 20 / 40)
 *   TechnicalsSetupProbabilities.meta               the same view attached to setup-probabilities (stored, or query-time for a
 *                                                   thesis target / stop)
 * Model-based, calibrated on history. Outputs that failed the gate carry experimental = true (hidden unless toggled).
 */
import type { components } from '../generated/schema'

export type TmDriver = components['schemas']['TechnicalsMetaDriver']
export type TmHorizon = components['schemas']['TechnicalsMetaHorizon']
export type TmSetup = components['schemas']['TechnicalsMetaSetup']
export type TmView = components['schemas']['TechnicalsMetaView']
