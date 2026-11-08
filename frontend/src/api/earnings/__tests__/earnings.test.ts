import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  addDays,
  bucketByDay,
  dateSourceMeta,
  eventGroup,
  expectationSeries,
  filterGroups,
  isOverdue,
  monthGrid,
  rangeFor,
  reactionSeries,
  seasonLabel,
  sectorName,
  shiftAnchor,
  sortDay,
  weekDays,
  weekStart,
} from '../adapters'
import type { ECalendarItem, EExpectationRow, EHistoryRow } from '../types'

const sec = (ticker: string, mcap: number) => ({ security_id: `S${ticker}`, issuer_id: `I${ticker}`, ticker, name: ticker, region: 'US', country: 'US', currency: 'USD', market_cap_usd: mcap }) as ECalendarItem['security']
const item = (p: Partial<ECalendarItem>): ECalendarItem => ({ kind: 'earnings', event_type: 'earnings', status: 'upcoming', date: '2026-10-21', security: sec('AAA', 1e9), ...p })

describe('date grids', () => {
  it('finds the ISO week (Mon-Sun)', () => {
    expect(weekStart('2026-10-21')).toBe('2026-10-19') // Wednesday -> Monday
    expect(weekStart('2026-10-25')).toBe('2026-10-19') // Sunday belongs to the same week
    expect(weekDays('2026-10-21')).toHaveLength(7)
    expect(weekDays('2026-10-21')[6]).toBe('2026-10-25')
  })
  it('builds a month grid of whole weeks', () => {
    const g = monthGrid('2026-10-15')
    expect(g[0][0].date).toBe('2026-09-28')
    expect(g[0][0].inMonth).toBe(false)
    expect(g.flat().filter((c) => c.inMonth)).toHaveLength(31)
    expect(g.every((w) => w.length === 7)).toBe(true)
  })
  it('computes ranges and navigation', () => {
    expect(rangeFor('day', '2026-10-21')).toEqual({ from: '2026-10-21', to: '2026-10-21' })
    expect(rangeFor('week', '2026-10-21')).toEqual({ from: '2026-10-19', to: '2026-10-25' })
    expect(rangeFor('month', '2026-10-21').from).toBe('2026-09-28')
    expect(shiftAnchor('day', '2026-10-23', 1)).toBe('2026-10-26') // Friday -> Monday (skips the weekend)
    expect(shiftAnchor('week', '2026-10-21', -1)).toBe('2026-10-14')
    expect(shiftAnchor('month', '2026-01-31', 1)).toBe('2026-02-01')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
  })
})

describe('calendar items', () => {
  it('groups corporate events and sorts days', () => {
    expect(eventGroup(item({}))).toBe('earnings')
    expect(eventGroup(item({ kind: 'corporate', event_type: 'dividend_ex' }))).toBe('dividend')
    expect(eventGroup(item({ kind: 'corporate', event_type: 'split' }))).toBe('split')
    expect(eventGroup(item({ kind: 'corporate', event_type: 'exec_change' }))).toBe('corporate')
    const its = [item({ security: sec('SMALL', 1e8) }), item({ kind: 'corporate', event_type: 'dividend_ex', security: sec('BIG', 1e12) }), item({ security: sec('MID', 1e10) })]
    expect(sortDay(its).map((x) => x.security.ticker)).toEqual(['MID', 'SMALL', 'BIG'])
    expect(filterGroups(its, ['dividend']).map((x) => x.security.ticker)).toEqual(['BIG'])
    const by = bucketByDay([...its, item({ date: '2026-10-22' })])
    expect(by.get('2026-10-21')).toHaveLength(3)
    expect(by.get('2026-10-22')).toHaveLength(1)
  })
  it('labels date sources and flags overdue projections', () => {
    expect(dateSourceMeta('model_projection')).toMatchObject({ short: 'Projection', tone: 'warn' })
    expect(dateSourceMeta('provider_estimate').long).toContain('Provider estimate')
    expect(dateSourceMeta('company_announced').tone).toBe('up')
    expect(dateSourceMeta('weird').short).toBe('weird')
    expect(isOverdue(item({ date: '2026-10-10' }), '2026-10-13')).toBe(true)
    expect(isOverdue(item({ date: '2026-10-10', status: 'released' }), '2026-10-13')).toBe(false)
    expect(sectorName('TECH')).toBe('Technology')
    expect(seasonLabel('2026Q3')).toBe('Q3 2026 · periods ending Jul–Sep 2026')
  })
})

describe('company series', () => {
  it('orders reactions by period and keeps the baseline', () => {
    const h = [
      { period_end: '2026-06-30', release_date: '2026-07-29', fiscal_period: 'Q4', fiscal_year: 2026, car_m1_p1: 0.05, baseline_move: 0.02, car_p1_p20: 0.01 },
      { period_end: '2026-03-31', release_date: '2026-04-29', fiscal_period: 'Q3', fiscal_year: 2026, car_m1_p1: -0.03, baseline_move: 0.02, car_p1_p20: null },
    ] as EHistoryRow[]
    const s = reactionSeries(h)
    expect(s.map((x) => x.label)).toEqual(['Q3 2026', 'Q4 2026'])
    expect(s[0].car).toBe(-0.03)
    expect(s[1].baseline).toBe(0.02)
  })
  it('keeps primary expectations of one item in period order', () => {
    const rows = [
      { item_id: 'eps_diluted', period_end: '2026-06-30', model: 'sarima', is_primary: true, status: 'released' },
      { item_id: 'eps_diluted', period_end: '2026-03-31', model: 'sarima', is_primary: true, status: 'released' },
      { item_id: 'eps_diluted', period_end: '2026-03-31', model: 'seasonal_naive', is_primary: false, status: 'released' },
      { item_id: 'revenue', period_end: '2026-03-31', model: 'sarima', is_primary: true, status: 'released' },
    ] as EExpectationRow[]
    expect(expectationSeries(rows, 'eps_diluted').map((r) => r.period_end)).toEqual(['2026-03-31', '2026-06-30'])
    expect(expectationSeries(rows, 'revenue')).toHaveLength(1)
  })
})

describe('client', () => {
  const get = vi.fn()
  beforeEach(() => {
    vi.resetModules()
    get.mockReset().mockResolvedValue({ items: [] })
    vi.doMock('../../http', () => ({ USE_MOCK: false, API_BASE_URL: 'http://x', API_PREFIX: '/api/v1', http: { get } }))
    vi.doMock('../../client', () => ({ resolveId: async (id: string) => `S_${id}` }))
  })
  it('builds the calendar query (watchlist tickers resolved to security ids)', async () => {
    const { earningsApi } = await import('../client')
    await earningsApi.calendar({ from: '2026-10-19', to: '2026-10-25', watchlistIds: ['MSFT', '7203.T'], types: ['earnings', 'dividend'], dateSources: ['model_projection'], region: 'US' })
    expect(get).toHaveBeenCalledWith('/earnings/calendar', expect.objectContaining({ from: '2026-10-19', to: '2026-10-25', security_ids: 'S_MSFT,S_7203.T', types: 'earnings,dividend', date_source: 'model_projection', region: 'US', universe: undefined }))
  })
  it('resolves the company id', async () => {
    const { earningsApi } = await import('../client')
    await earningsApi.company('AAPL')
    expect(get).toHaveBeenCalledWith('/companies/S_AAPL/earnings', { history: 40 })
  })
})

describe('earnings alert rules', () => {
  it('maps the UI rules to the contract and back', async () => {
    const { alertFromContract, alertToContract } = await import('../../adapters/crossSection')
    const up = alertToContract({ companyId: 'S1', ticker: 'AAPL', metric: 'earningsUpcoming', operator: '<', threshold: 3.4, enabled: true })
    expect(up).toMatchObject({ security_id: 'S1', rule_type: 'earnings_upcoming', params: { days_ahead: 3, ui_metric: 'earningsUpcoming' } })
    const sp = alertToContract({ companyId: 'S1', ticker: 'AAPL', metric: 'earningsSurprise', operator: '>', threshold: -2.5, enabled: true })
    expect(sp).toMatchObject({ rule_type: 'earnings_surprise', params: { threshold_z: 2.5 } })
    const back = alertFromContract({ alert_id: 'a', security_id: 'S1', rule_type: 'earnings_upcoming', params: { days_ahead: 5 }, enabled: true, cooldown_days: 1, created_at: '2026-10-19T00:00:00Z' })
    expect(back).toMatchObject({ metric: 'earningsUpcoming', threshold: 5 })
  })
})
