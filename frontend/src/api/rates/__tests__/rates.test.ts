import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  barPct,
  curveXY,
  debtKpis,
  defaultSpreads,
  fmtBp,
  fmtRate,
  frameIndexAt,
  frameXY,
  framesExtent,
  groupCredit,
  groupCurves,
  seriesId,
  slopeSpreads,
  tenorLabel,
  thinPoints,
  wallBars,
  wallTotal,
} from '../adapters'
import type { CreditSeries, RatesCurveMeta, RatesFrames } from '../types'

const frames: RatesFrames = {
  frequency: 'M',
  tenors: [12, 24, 120],
  dates: ['2024-01-31', '2024-02-29', '2024-03-28'],
  values: [
    [3.1, 3.0, null],
    [3.2, 3.1, 3.5],
    [3.3, null, 3.6],
  ],
}

describe('rates adapters', () => {
  it('labels tenors', () => {
    expect([0, 1, 3, 6, 18, 24, 120, 360].map(tenorLabel)).toEqual(['ON', '1M', '3M', '6M', '18M', '2Y', '10Y', '30Y'])
  })
  it('formats rates and bp', () => {
    expect(fmtRate(4.2512)).toBe('4.25%')
    expect(fmtRate(-0.0961)).toBe('−0.096%')
    expect(fmtRate(null)).toBe('—')
    expect(fmtBp(12.4)).toBe('+12 bp')
    expect(fmtBp(-3.6)).toBe('−4 bp')
    expect(fmtBp(5, false)).toBe('5 bp')
  })
  it('maps snapshots and frames to [years, %]', () => {
    expect(curveXY({ points: [{ tenor_months: 6, value: 3 }, { tenor_months: 120, value: null }] })).toEqual([[0.5, 3]])
    expect(frameXY(frames, 0)).toEqual([[1, 3.1], [2, 3]])
    expect(frameXY(frames, 9)).toEqual([])
  })
  it('finds the frame on or before a date', () => {
    expect(frameIndexAt(frames, '2023-12-01')).toBe(0)
    expect(frameIndexAt(frames, '2024-02-29')).toBe(1)
    expect(frameIndexAt(frames, '2024-03-15')).toBe(1)
    expect(frameIndexAt(frames, '2030-01-01')).toBe(2)
  })
  it('keeps one y extent for the animation', () => {
    expect(framesExtent(frames)).toEqual([2.9, 3.7])
    expect(framesExtent({ ...frames, values: [[null]] })).toBeNull()
  })
  it('builds series and spread ids', () => {
    expect(seriesId('US_TSY', 120, 'par')).toBe('US_TSY:120:par')
    const bund = { curve_id: 'DE_BUND', type: 'par' }
    const ust = { curve_id: 'US_TSY', type: 'par' }
    expect(defaultSpreads({ curve_id: 'GB_GILT', type: 'spot', country: 'GB' }, bund, ust)).toEqual([
      'GB_GILT:120:spot-DE_BUND:120:par',
      'GB_GILT:120:spot-US_TSY:120:par',
    ])
    expect(defaultSpreads({ curve_id: 'DE_BUND', type: 'par', country: 'DE' }, bund, ust)).toEqual(['DE_BUND:120:par-US_TSY:120:par'])
    expect(slopeSpreads('US_TSY', 'par')).toEqual(['US_TSY:120:par-US_TSY:24:par', 'US_TSY:360:par-US_TSY:60:par'])
  })
  it('groups the curve catalogue', () => {
    const base = { kind: 'nominal', types: ['par'], tenors: [120], source_id: 'x', source: 'x', frequency: 'D', licence: '', publish: '', monitor_type: 'par', n_dates: 1 } as const
    const items = [
      { ...base, curve_id: 'US_TSY', country: 'US', label: 'UST', group: 'sovereign', is_country_benchmark: true, status: 'ok' },
      { ...base, curve_id: 'US_H15', country: 'US', label: 'H15', group: 'sovereign', is_country_benchmark: false, status: 'ok' },
      { ...base, curve_id: 'IT_LT10', country: 'IT', label: 'IT', group: 'lt10', is_country_benchmark: false, status: 'ok' },
      { ...base, curve_id: 'NO_DATA', country: 'XX', label: 'x', group: 'sovereign', is_country_benchmark: true, status: 'no_data' },
    ] as unknown as RatesCurveMeta[]
    expect(groupCurves(items).map((g) => [g.label, g.items.map((c) => c.curve_id)])).toEqual([
      ['Country benchmarks', ['US_TSY']],
      ['Other official curves', ['US_H15']],
      ['Monthly 10y (rest of world)', ['IT_LT10']],
    ])
  })
  it('orders maturity walls and totals them', () => {
    const rows = [{ bucket: 'after5', amount: 5 }, { bucket: 'total', amount: 99 }, { bucket: 'y1', amount: 1 }, { bucket: 'y2', amount: null }]
    expect(wallBars(rows).map((r) => r.label)).toEqual(['< 1y', '1-2y', '> 5y'])
    expect(wallTotal(rows)).toBe(6)
    expect(wallTotal([{ bucket: 'y1', amount: null }])).toBeNull()
    expect(barPct(5, 10)).toBe(50)
    expect(barPct(null, 10)).toBe(0)
  })
  it('groups credit series and debt KPIs', () => {
    const s = [{ group: 'rating', series_id: 'a' }, { group: 'moodys', series_id: 'b' }, { group: 'rating', series_id: 'c' }] as CreditSeries[]
    expect(Object.fromEntries(Object.entries(groupCredit(s)).map(([k, v]) => [k, v.map((x) => x.series_id)]))).toEqual({ rating: ['a', 'c'], moodys: ['b'] })
    const k = debtKpis({ period_end: '2025-12-31', period_kind: 'FY', knowledge_date: '2026-02-20', avg_cost_of_debt: 0.045, interest_coverage: null })
    expect(k[0].value).toBe(0.045)
    expect(k[1].value).toBeNull()
    expect(debtKpis(null).every((x) => x.value === null)).toBe(true)
  })
  it('thins long series keeping the last point', () => {
    const pts = Array.from({ length: 1000 }, (_, i) => i)
    const t = thinPoints(pts, 100)
    expect(t.length).toBeLessThanOrEqual(101)
    expect(t[t.length - 1]).toBe(999)
  })
})

describe('rates client', () => {
  const get = vi.fn()
  beforeEach(() => {
    vi.resetModules()
    get.mockReset().mockResolvedValue({ security_id: 'S_AAPL', history: [], bonds: [], reasons: [], labels: {}, data_status: 'pending' })
    vi.doMock('../../http', () => ({ USE_MOCK: false, API_BASE_URL: 'http://x', API_PREFIX: '/api/v1', http: { get } }))
    vi.doMock('../../client', () => ({ resolveId: async (id: string) => (/^S[0-9A-Z]{12}$/.test(id) ? id : `S_${id}`) }))
  })
  it('resolves a ticker route id before calling the Debt endpoint (coverage v3 A7: /company/AAPL returned 404)', async () => {
    const { ratesApi } = await import('../client')
    await ratesApi.companyDebt('AAPL')
    expect(get).toHaveBeenCalledWith('/companies/S_AAPL/debt')
    await ratesApi.companyDebt('7203.T')
    expect(get).toHaveBeenLastCalledWith('/companies/S_7203.T/debt')
    await ratesApi.companyDebt('SS8MA4AQCCX39')
    expect(get).toHaveBeenLastCalledWith('/companies/SS8MA4AQCCX39/debt')
  })
})
