/**
 * Conformal ellipsoids (contract v1.32.0, tag conformal): /conformal/envelope/{id}, /conformal/touch, /conformal/peer-groups,
 * /conformal/pair-band, /conformal/coverage. Scope follows the P4 gate (docs/CONFORMAL_VALIDATION.md): marginal
 * "calibrated range" bands and touch probabilities; peer-group anomaly as a descriptive (not trading) measure; next-bar
 * bands experimental. Payloads keep the contract shape; ./adapters derives chart / table structures.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type CfEnvelope = S['ConformalEnvelope']
export type CfBand = S['ConformalBand']
export type CfCoverageStat = S['ConformalCoverageStat']
export type CfTouch = S['ConformalTouch']
export type CfTouchLevel = S['ConformalTouchLevel']
export type CfPeerGroups = S['ConformalPeerGroups']
export type CfPeerGroupSummary = S['ConformalPeerGroupSummary']
export type CfPeerGroupDetail = S['ConformalPeerGroupDetail']
export type CfPairBand = S['ConformalPairBand']
export type CfCoverage = S['ConformalCoverage']
export type CfCoverageRow = S['ConformalCoverageRow']
export type CfFanPoint = S['ConformalFanPoint']
export type CfFanInfo = S['ConformalFanInfo']

export type CfLevel = 80 | 90 | 95
export type CfHorizon = 1 | 5 | 10 | 20

export interface PeerGroupQuery {
  securityId?: string
  groupId?: string
  region?: string
  sectorId?: string
  subsectorId?: string
  kind?: 'industry' | 'supply_chain'
  anomaliesOnly?: boolean
  limit?: number
}

export interface PairQuery {
  groupId: string
  i?: string
  j?: string
  beta?: number | null
  weights?: string
}
