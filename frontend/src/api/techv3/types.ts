/**
 * Technicals v3 #1 / #5 (contract v1.36.0; gate docs/TECHNICALS_V3_VALIDATION.md):
 *   GET /technicals/{security_id}/factors    evidence-based technical factors: percentile within region x sector, each
 *                                            factor's own out-of-sample rank IC + gate verdict, composite (only if it passed)
 *   GET /technicals/{security_id}/breakout   latest breakout state, volume confirmation, calibrated P(follow-through)
 * Items that failed the gate carry experimental = true (hidden unless the viewer enables experimental outputs).
 */
import type { components } from '../generated/schema'

export type Tv3Factors = components['schemas']['TechnicalsFactors']
export type Tv3Factor = components['schemas']['TechnicalsFactor']
export type Tv3FactorIc = components['schemas']['TechnicalsFactorIc']
export type Tv3Composite = components['schemas']['TechnicalsFactorComposite']
export type Tv3Breakout = components['schemas']['TechnicalsBreakout']
export type Tv3BreakoutEvent = components['schemas']['TechnicalsBreakoutEvent']
