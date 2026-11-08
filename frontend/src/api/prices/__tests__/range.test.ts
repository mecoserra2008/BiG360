import { describe, expect, it } from 'vitest'
import { groupItems, maxStats, priceDigits } from '../../instruments/types'
import { isInstrumentId, securityRoute } from '../../../lib/routes'
import { autoInterval, effectiveInterval, hasWeeklyPart, rangeStart, seriesView } from '../range'

const end = new Date(Date.UTC(2026, 9, 14))

describe('price ranges', () => {
  it('computes range starts', () => {
    expect(rangeStart('1M', end)).toBe('2026-09-14')
    expect(rangeStart('6M', end)).toBe('2026-04-14')
    expect(rangeStart('YTD', end)).toBe('2026-01-01')
    expect(rangeStart('1Y', end)).toBe('2025-10-14')
    expect(rangeStart('10Y', end)).toBe('2016-10-14')
    expect(rangeStart('Max', end)).toBeNull()
  })
  it('auto-picks D up to 2Y, W up to 10Y, M for Max, with override', () => {
    expect(['1M', '6M', 'YTD', '1Y'].map((r) => autoInterval(r as never))).toEqual(['D', 'D', 'D', 'D'])
    expect(autoInterval('5Y')).toBe('W')
    expect(autoInterval('10Y')).toBe('W')
    expect(autoInterval('Max')).toBe('M')
    expect(effectiveInterval('Max', 'D')).toBe('D')
  })
  it('adapts a series and flags the weekly part of a daily view', () => {
    const v = seriesView({ security_id: 'S', currency: 'USD', adjusted: true, interval: 'D', first_date: '1980-12-12', daily_from: '2011-10-14', bars: [{ t: '2020-01-03', c: 2, o: null }, { t: '2020-01-02', c: 1 }] })
    expect(v.bars.map((b) => b.time)).toEqual(['2020-01-02', '2020-01-03'])
    expect(v.bars[1].open).toBe(2)
    expect(v).toMatchObject({ firstDate: '1980-12-12', dailyFrom: '2011-10-14', interval: 'D' })
    expect(hasWeeklyPart('D', null, '2011-10-14')).toBe(true)
    expect(hasWeeklyPart('D', '2020-01-01', '2011-10-14')).toBe(false)
    expect(hasWeeklyPart('W', null, '2011-10-14')).toBe(false)
  })
})

describe('instrument routing + helpers', () => {
  it('routes instruments to the Instruments page', () => {
    expect(isInstrumentId('IDX:EURUSD=X')).toBe(true)
    expect(isInstrumentId('ETF:SPY')).toBe(true)
    expect(isInstrumentId('CL=F')).toBe(true)
    expect(isInstrumentId('AAPL')).toBe(false)
    expect(securityRoute('IDX:^VIX')).toBe('/instruments/IDX%3A%5EVIX')
    expect(securityRoute('EURUSD=X')).toBe('/instruments/IDX%3AEURUSD%3DX')
    expect(securityRoute('S123')).toBe('/company/S123')
  })
  it('groups and filters items, picks price precision', () => {
    const d = { groups: [{ group: 'fx_majors', label: 'FX majors', type: 'fx', n: 2 }, { group: 'energy', label: 'Energy', type: 'future', n: 1 }], items: [{ group: 'energy', ticker: 'CL=F', name: 'WTI' }, { group: 'fx_majors', ticker: 'EURUSD=X', name: 'EUR/USD' }, { group: 'fx_majors', ticker: 'GBPUSD=X', name: 'GBP/USD' }] } as never
    expect(groupItems(d).map((g) => [g.group, g.items.length])).toEqual([['fx_majors', 2], ['energy', 1]])
    expect(groupItems(d, 'eur').map((g) => g.group)).toEqual(['fx_majors'])
    expect(priceDigits(1.0712, 'fx')).toBe(4)
    expect(priceDigits(151.2, 'fx')).toBe(2)
    expect(priceDigits(5321.4, 'index')).toBe(0)
    expect(maxStats({ stats: [{ window: '1Y', n: 52 }, { window: 'Max', n: 900 }] } as never)?.n).toBe(900)
  })
})
