/**
 * Earnings & corporate-events calendar (contract v1.22.0, /api/v1/earnings/*, /companies/{id}/earnings).
 * Payloads keep the contract shape; ./adapters derives what the pages need (day buckets, week / month grids,
 * reaction series, source tones).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type ECalendar = S['EarningsCalendar']
export type ECalendarItem = S['EarningsCalendarItem']
export type ECompany = S['EarningsCompany']
export type EHistoryRow = S['EarningsHistoryRow']
export type EExpectationRow = S['EarningsExpectationRow']
export type ESeason = S['EarningsSeason']
export type ESeasonRow = S['EarningsSeasonRow']
export type ETopSurprise = ESeason['top_surprises'][number]
export type ECorporateEvent = ECompany['corporate_events'][number]

export type EventGroup = 'earnings' | 'dividend' | 'split' | 'corporate'
export type CalendarView = 'day' | 'week' | 'month'

export const EXPECTATION_LABEL = 'Model-based expectation (not consensus)'

export interface CalendarQuery {
  from: string
  to: string
  universe?: string
  watchlistIds?: string[]
  region?: string
  sector?: string
  types?: EventGroup[]
  dateSources?: string[]
}
