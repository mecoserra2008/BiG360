import { describe, expect, it } from 'vitest'
import type { IndexQuote, MacroIndicator, MacroRegime, SectorPerformance, SourceFreshness, YieldCurve } from '@/api/types'
import { groupSources } from '../../system/sourceGroups'
import { asOfChips, benchmarkFor, curveOptions, headlineTiles, regimeChipText, sectorCells, sessionsBetween, sparkWindowLabel } from './helpers'
import { assetPoints, seriesChanges, valueDaysBefore } from './tapeData'

const idx = (id: string, region: string, asOf: string, c1d = 0.01): IndexQuote => ({ id, name: id, region, currency: null, last: 100, change1d: c1d, change1w: 0, change1m: 0.02, changeYtd: 0.1, asOf, spark: [], bigMove: false })

describe('overview helpers', () => {
  it('per-region as-of chips from data_as_of, else labelled index dates', () => {
    expect(asOfChips([{ region: 'JP', asOf: '2026-10-16', latest: null, nSecurities: 1, shareAtAsOf: 1, staleShare: 0 }, { region: 'US', asOf: '2026-10-15', latest: null, nSecurities: 1, shareAtAsOf: 1, staleShare: 0.3 }], []).chips.map((c) => c.region)).toEqual(['US', 'JP'])
    const r = asOfChips([], [idx('^GSPC', 'US', '2026-10-12'), idx('^N225', 'JP', '2026-10-13')])
    expect(r.source).toBe('index')
    expect(r.chips).toEqual([{ region: 'US', asOf: '2026-10-12' }, { region: 'JP', asOf: '2026-10-13' }])
    expect(sessionsBetween('2026-10-16', '2026-10-20')).toBe(2)
  })
  it('regime chip text: modelled, fallback, not modelled', () => {
    const base: MacroRegime = { status: 'modelled', requestedScope: 'US', scope: 'US', modelId: 'm', current: 'Expansion', date: '2026-10-12', probability: 1, probabilities: { Expansion: 1, Slowdown: 0, Contraction: 0, Recovery: 0, Stagflation: 0 }, states: ['Expansion', 'Slowdown', 'Contraction', 'Recovery'], history: [], drivers: [] }
    expect(regimeChipText(base)).toMatch(/^US regime: Expansion \(HMM, 4 states/)
    expect(regimeChipText({ ...base, status: 'fallback', requestedScope: 'KR', scope: 'GLOBAL' })).toMatch(/^No KR model · GLOBAL regime \(fallback\)/)
    expect(regimeChipText({ ...base, status: 'not_modelled', current: null, requestedScope: 'LATAM' })).toBe('No regime model for LATAM')
  })
  it('sector minus region benchmark', () => {
    const p: SectorPerformance = { sector: 'Tech', region: 'US', return1d: 0.02, return1m: 0.05, returnYtd: 0.3, marketCapUsd: 1, nSecurities: 10, nRet1d: 10, asOf: null, asOfByRegion: {} }
    const ix = [idx('^GSPC', 'US', '2026-10-16', 0.005)]
    expect(benchmarkFor('US', ix)?.id).toBe('^GSPC')
    expect(sectorCells([p], 'return1d', 'relative', ix)[0].value).toBeCloseTo(1.5)
    expect(sectorCells([p], 'return1d', 'total', ix)[0].value).toBeCloseTo(2)
    expect(sectorCells([{ ...p, region: 'KR' }], 'return1d', 'relative', ix)).toEqual([])
  })
  it('curve options hide < 2 market tenors and flag 2-tenor curves', () => {
    const c = (country: string, n: number, region = 'EU'): YieldCurve => ({ country, label: country, region, current: [], oneMonthAgo: [], oneYearAgo: [], policyRate: null, spread2s10sBps: NaN, spread5s30sBps: NaN, change10y1mBps: NaN, asOf: null, asOfMin: null, nMarketTenors: n })
    const o = curveOptions([c('FR', 1), c('DE', 5), c('IT', 2), c('US', 10, 'US')])
    expect(o.map((x) => x.value)).toEqual(['US', 'DE', 'IT'])
    expect(o[2].label).toContain('(2 tenors)')
  })
  it('tile helpers', () => {
    expect(sparkWindowLabel(365, 10)).toBe('1y')
    expect(sparkWindowLabel(null, 60)).toBe('last 60 obs')
    const t = (id: string, region: MacroIndicator['region'], displayLabel: string | null): MacroIndicator => ({ id, name: id, region, unit: 'pct', value: 1, change: 0, changeLabel: '', higherIsBetter: null, asOf: '', source: 'x', history: [], displayLabel })
    const out = headlineTiles([t('lvl', 'US', null), t('yoy', 'US', 'y/y'), t('o', 'OTHER', null), t('b', 'US', null)], 2)
    expect(out.map((x) => x.id)).toEqual(['yoy', 'lvl'])
  })
})

describe('tape data', () => {
  it('1D vs previous observation, 1M vs 30 calendar days earlier', () => {
    const pts = [
      { date: '2026-09-10', value: 100 },
      { date: '2026-09-15', value: 104 },
      { date: '2026-10-15', value: 108 },
      { date: '2026-10-16', value: 110 },
    ]
    expect(valueDaysBefore(pts, 30)).toBe(104)
    const ch = seriesChanges(pts, 'pct')
    expect(ch.change1d).toBeCloseTo(110 / 108 - 1)
    expect(ch.change1m).toBeCloseTo(110 / 104 - 1)
    expect(seriesChanges(pts, 'diff').change1d).toBeCloseTo(2)
    expect(seriesChanges([], 'pct').last).toBeNull()
    expect(assetPoints(['2026-10-15', '2026-10-16'], [1, null])).toEqual([{ date: '2026-10-15', value: 1 }, { date: '2026-10-16', value: null }])
  })
})

describe('system: freshness rows per dataset (F22)', () => {
  it('groups region rows of a table, keeps the worst status, sorts prices / FX / macro first', () => {
    const r = (source: string, region: SourceFreshness['region'], status: SourceFreshness['status'], lagHours: number, description = `Data through 2026-10-1${lagHours % 10}`): SourceFreshness => ({ source, description, region, cadence: 'daily', lastSuccess: '2026-10-19T00:00:00Z', lagHours, status, coverage: 10 })
    const g = groupSources([r('company_geo_revenue', 'US', 'stale', 9000), r('company_geo_revenue', 'EU', 'fresh', 1), r('prices_daily', 'US', 'fresh', 10), r('prices_daily', 'JP', 'stale', 30), r('macro_observations', 'GLOBAL', 'fresh', 5)])
    expect(g.map((x) => x.source)).toEqual(['prices_daily', 'macro_observations', 'company_geo_revenue'])
    expect(g[0]).toMatchObject({ status: 'stale', nRows: 2, coverage: 20, counts: { fresh: 1, stale: 1, error: 0 } })
    expect(g[0].worst?.region).toBe('JP')
    expect(g[0].best?.region).toBe('US')
  })
})
