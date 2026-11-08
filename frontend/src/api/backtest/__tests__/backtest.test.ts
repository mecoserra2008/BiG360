import { describe, expect, it } from 'vitest'
import {
  crossAssetSpec,
  defaultSpec,
  describeSpec,
  fmtMetric,
  histogram,
  isTerminal,
  monthlyGrid,
  overfitVerdict,
  parseSweepValues,
  positioningSpec,
  screenRequest,
  seriesPoints,
  stepFor,
  studioRunSpec,
  sweepSize,
  toDraft,
  varName,
} from '../adapters'
import type { BtSeries } from '../types'

describe('specs and drafts', () => {
  it('default spec is a complete monthly long-short value backtest with a 30% holdout', () => {
    const s = defaultSpec()
    expect(s.universe.universe_id).toBe('mvp_us')
    expect(s.signal.variables[0]).toMatchObject({ source: 'factor_scores', field: 'value' })
    expect(s.construction).toMatchObject({ side: 'long_short', quantiles: 5, rebalance: 'M', execution_lag_days: 1 })
    expect(s.period.oos_fraction).toBe(0.3)
    expect(describeSpec(s)).toContain('L/S q5')
  })

  it('toDraft fills missing blocks from the defaults and keeps the given ones', () => {
    const d = toDraft({ name: 'x', mode: 'rank', signal: { variables: [{ name: 'q', source: 'factor_scores', field: 'quality', weight: 1, higher_is_better: true }], combine: 'single', higher_is_better: true, lag_periods: 0 }, delisting_return: 0, seed: 1, timeout_s: 60, construction: { ...defaultSpec().construction, quantiles: 10 } })
    expect(d.construction.quantiles).toBe(10)
    expect(d.costs.commission_bps).toBe(1)
    expect(d.universe.kind).toBe('securities')
    expect(d.event).toBeNull()
  })

  it('entry-point drafts: cross-asset, CFTC events and Studio runs', () => {
    const xa = crossAssetSpec('carry', 'carry', 'fx')
    expect(xa.universe).toMatchObject({ kind: 'assets', asset_classes: ['fx'] })
    expect(xa.signal.variables[0]).toMatchObject({ source: 'cross_asset', field: 'carry', higher_is_better: true })
    const cot = crossAssetSpec('cot_index_3y', 'CFTC', null)
    expect(cot.signal.variables[0]).toMatchObject({ source: 'cftc', higher_is_better: false })
    const pos = positioningSpec({ assetIds: ['CL'], assetClass: 'commodity', marketName: 'WTI' })
    expect(pos.mode).toBe('event')
    expect(pos.event).toMatchObject({ trigger: 'cftc_extreme', allow_short: true })
    expect(pos.universe.asset_ids).toEqual(['CL'])
    expect(pos.universe.asset_classes).toEqual([])
    const st = studioRunSpec('SR123', 'GBM', 'mvp_global')
    expect(st.signal.variables[0]).toMatchObject({ source: 'studio_run', field: 'SR123', name: 'studio_pred' })
    expect(st.universe.universe_id).toBe('mvp_global')
  })

  it('variable names are identifier-safe, unique and avoid the reserved prefixes', () => {
    expect(varName('3M:rank_pct', [])).toBe('M_rank_pct')
    expect(varName('value', ['value'])).toBe('value2')
    expect(varName('f_value', [])).toBe('xf_value')
  })

  it('sweep size and value parsing', () => {
    const s = { ...defaultSpec(), robustness: { ...defaultSpec().robustness, sweep: { 'construction.quantiles': [3, 5, 10], 'construction.weighting': ['equal', 'inverse_vol'] } } }
    expect(sweepSize(s)).toBe(6)
    expect(sweepSize(defaultSpec())).toBe(0)
    expect(parseSweepValues('3, 5 ,x, true')).toEqual([3, 5, 'x', true])
  })
})

describe('screen -> backtest request', () => {
  const base = { regions: ['US'], country: '', sector: 'Technology', subsector: '', bucket: '' as const, moat: '', minScore: 0, macroFilter: '', text: '', sortId: 'score', sortDesc: true, factorWeights: { value: 2, quality: 1 } }
  it('live re-ranking -> factor weights; filters -> PIT screen rules', () => {
    const { body, notes } = screenRequest({ ...base, bucket: 'large', minScore: 60 })
    expect(body.factor_weights).toEqual({ value: 2, quality: 1 })
    expect(body.sector_ids).toEqual(['Technology'])
    expect(body.filters).toEqual([
      { field: 'composite', op: 'gte', value: 60 },
      { field: 'market_cap_usd', op: 'gte', value: 10e9 },
      { field: 'market_cap_usd', op: 'lt', value: 200e9 },
    ])
    expect(notes).toEqual([])
  })
  it('column sorts map to point-in-time fields; unknown ones fall back with a note', () => {
    expect(screenRequest({ ...base, sortId: 'f_balanceSheet', sortDesc: false }).body).toMatchObject({ sort_by: 'balance_sheet', sort_dir: 'asc', factor_weights: null })
    expect(screenRequest({ ...base, sortId: 'roic' }).body.sort_by).toBe('roic')
    const pe = screenRequest({ ...base, sortId: 'pe' })
    expect(pe.body.sort_by).toBe('pe_ttm')
    expect(pe.notes[0]).toContain('P/E NTM')
    expect(screenRequest({ ...base, sortId: 'dq', text: 'aapl' }).notes).toHaveLength(2)
  })
})

describe('result helpers', () => {
  it('series points down-sample and keep the last point', () => {
    const s: BtSeries = { dates: ['a', 'b', 'c', 'd', 'e'], nav: [1, 1.1, null, 1.2, 1.3], drawdown: [0, 0, 0, 0, 0] }
    expect(seriesPoints(s, 'nav', 2)).toEqual([
      { date: 'a', value: 1 },
      { date: 'c', value: null },
      { date: 'e', value: 1.3 },
    ])
    expect(seriesPoints(s, 'nav', 3).at(-1)).toEqual({ date: 'e', value: 1.3 })
    expect(stepFor(3000, 1500)).toBe(2)
  })

  it('monthly grid compounds the year and sorts newest first', () => {
    const g = monthlyGrid([
      { month: '2024-01-01', ret: 0.1, bench: null },
      { month: '2024-02-01', ret: -0.1, bench: null },
      { month: '2025-03-01', ret: 0.02, bench: null },
    ])
    expect(g[0].year).toBe(2025)
    expect(g[0].months[2]).toBe(0.02)
    expect(g[1].total).toBeCloseTo(1.1 * 0.9 - 1)
  })

  it('histogram, metric formatting, verdicts', () => {
    const h = histogram([0, 0.5, 1, 1], 2)
    expect(h.map((b) => b.n)).toEqual([1, 3])
    expect(fmtMetric(0.1234, 'pct')).toBe('12.3%')
    expect(fmtMetric(0.0123, 'pct')).toBe('1.23%')
    expect(fmtMetric(null, 'num')).toBe('–')
    expect(fmtMetric(1.234, 'num')).toBe('1.23')
    expect(overfitVerdict(0.97, 0.1, 0.01).tone).toBe('up')
    expect(overfitVerdict(0.3, 0.6, 0.4).tone).toBe('down')
    expect(overfitVerdict(0.8, null, 0.1).tone).toBe('warn')
    expect(overfitVerdict(null, null, null).tone).toBe('neutral')
    expect(isTerminal('completed') && isTerminal('failed') && !isTerminal('running')).toBe(true)
  })
})
