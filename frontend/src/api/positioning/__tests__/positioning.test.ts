import { describe, expect, it } from 'vitest'
import { asOfLine, cellBackground, familyOptions, fmtContracts, fmtDow, fmtMetric, groupRows, intensity, nPoints, studioSeeds } from '../adapters'
import type { PosDetail, PosHeatmap } from '../types'
import { positioningDraft } from '@/pages/positioning/draft'

describe('positioning metric scales', () => {
  it('centres COT index / pct rank on 0.5 and clamps', () => {
    expect(intensity(0.5, 'cot_index_3y')).toBe(0)
    expect(intensity(1, 'cot_index_3y')).toBe(1)
    expect(intensity(0, 'cot_index_1y')).toBe(-1)
    expect(intensity(0.75, 'pct_rank_3y')).toBeCloseTo(0.5)
    expect(intensity(5, 'z_3y')).toBe(1)
    expect(intensity(-1.25, 'z_1y')).toBeCloseTo(-0.5)
    expect(intensity(0.2, 'net_pct_oi')).toBeCloseTo(0.5)
    expect(intensity(null, 'z_3y')).toBeNull()
    expect(intensity(Number.NaN, 'cot_index_3y')).toBeNull()
  })
  it('colours high readings blue (--c2), low orange (--c1), neutral at the midpoint', () => {
    expect(cellBackground(0.95, 'cot_index_3y')).toContain('var(--c2)')
    expect(cellBackground(0.05, 'cot_index_3y')).toContain('var(--c1)')
    expect(cellBackground(0.5, 'cot_index_3y')).toContain(' 0%')
    expect(cellBackground(null, 'cot_index_3y')).toBe('var(--surface-2)')
  })
  it('formats metrics', () => {
    expect(fmtMetric(0.934, 'cot_index_3y')).toBe('93')
    expect(fmtMetric(-1.27, 'z_3y')).toBe('−1.3')
    expect(fmtMetric(0.12, 'net_pct_oi')).toBe('+12%')
    expect(fmtMetric(null, 'z_3y')).toBe('—')
    expect(fmtContracts(1_234_567)).toBe('1.23M')
    expect(fmtContracts(-45_600)).toBe('−46k')
    expect(fmtContracts(1500)).toBe('1.5k')
    expect(fmtContracts(undefined)).toBe('—')
    expect(nPoints([1, null, 2, Number.NaN])).toBe(2)
  })
})

describe('as-of line', () => {
  it('states Tuesday data and Friday release with the official source', () => {
    expect(fmtDow('2026-10-13')).toBe('Tue 13 Oct 2026')
    expect(asOfLine({ report_date: '2026-10-13', release_date: '2026-10-16', release_estimated: false })).toBe('Data as of Tue 13 Oct 2026, released Fri 16 Oct 2026 · CFTC (official)')
    expect(asOfLine({ report_date: '2019-01-08', release_date: '2019-03-08', release_estimated: true })).toContain('(scheduled)')
    expect(asOfLine({ report_date: null })).toContain('No COT report')
  })
})

describe('heatmap grouping and families', () => {
  it('groups rows by asset class preserving order', () => {
    const row = (code: string, ac: string) => ({ market_code: code, name: code, asset_class: ac, is_major: true, report_date: '2026-10-13', values: [], extremes: [], crowded: [] })
    const g = groupRows({ rows: [row('a', 'energy'), row('b', 'energy'), row('c', 'metals')] } as unknown as PosHeatmap)
    expect(g.map((x) => [x.asset_class, x.rows.length])).toEqual([
      ['energy', 2],
      ['metals', 1],
    ])
  })
  it('offers report families in display order', () => {
    expect(familyOptions(['tff', 'legacy'])).toEqual(['legacy', 'tff'])
  })
})

describe('Model Studio hand-off', () => {
  const d = {
    market: { market_code: '067651', name: 'WTI crude (NYMEX)' },
    report_type: 'disagg_fut',
    headline_category: 'm_money',
    price_instrument: { security_id: 'IDX:CL=F', ticker: 'CL=F', is_proxy: false },
  } as unknown as PosDetail
  it('builds cftc fields market:report_type:category:metric with the price return as y', () => {
    const s = studioSeeds(d)
    expect(s[0]).toMatchObject({ source: 'prices', field: 'log_ret', role: 'y', ticker: 'CL=F' })
    expect(s.map((x) => x.field)).toContain('067651:disagg_fut:m_money:cot_index_3y')
    expect(s.map((x) => x.field)).toContain('067651:disagg_fut:spec_hedger:z_3y')
  })
  it('makes a weekly OLS draft with lagged positioning regressors', () => {
    const dr = positioningDraft(d)
    expect(dr.frequency).toBe('W')
    expect(dr.method).toBe('ols')
    const x = dr.variables.filter((v) => v.source === 'cftc')
    expect(x.length).toBe(3)
    expect(x.every((v) => v.transforms[0]?.op === 'lag' && v.entities === null)).toBe(true)
  })
  it('uses positioning as y when no price instrument is mapped', () => {
    const s = studioSeeds({ ...d, price_instrument: null } as unknown as PosDetail)
    expect(s[0]).toMatchObject({ source: 'cftc', role: 'y' })
  })
})
