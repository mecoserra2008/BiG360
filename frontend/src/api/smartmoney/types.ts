/**
 * Smart money (contract v1.25.0, /api/v1/smart-money/*, /companies/{id}/ownership). Payloads keep the contract shape;
 * ./adapters derives what the pages need (lag text, regulator labels, chart series, holder change tones).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type SmOverview = S['SmartMoneyOverview']
export type SmRow = S['SmartMoneyRow']
export type SmEvent = S['SmartMoneyEvent']
export type SmEuShort = S['SmartMoneyEuShort']
export type SmEuShorts = S['SmartMoneyEuShorts']
export type SmRegulator = S['SmartMoneyRegulator']
export type SmActivism = S['SmartMoneyActivism']
export type SmSectors = S['SmartMoneySectors']
export type SmSectorRow = S['SmartMoneySectorRow']
export type SmLag = S['SmartMoneyLag']
export type SmSource = S['SmartMoneySource']
export type SmFutures = S['SmartMoneyFuturesRow']
export type CoOwnership = S['CompanyOwnership']
export type CoHolder = S['CompanyOwnershipHolder']
export type CoTrendPoint = CoOwnership['ownership_trend'][number]
export type CoSiPoint = CoOwnership['short_interest'][number]
export type CoInsider = CoOwnership['insider_timeline'][number]
export type CoScorePoint = CoOwnership['score_history'][number]

export const MODEL_BASED = 'Model-based (our definition, not an official statistic)'

export interface EuShortQuery {
  regulator?: string
  q?: string
  holder?: string
  current?: boolean
  positionType?: '' | 'individual' | 'aggregate'
  limit?: number
  offset?: number
}

export interface ActivismQuery {
  days?: number
  eventTypes?: string[]
}
