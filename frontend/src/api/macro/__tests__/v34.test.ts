import { describe, expect, it } from 'vitest'
import { IMPLIED_LABEL, betaInterval, cleanShocks, exposureMatrix, pricedBucket, pricedText, regimeRows, replayPoints, scopeOptions, toneSpark, type CbToneTrend, type MxExposureItem, type MxRegimeScope, type MxFactorMeta, type MxRegimeItem, type MxReplayRow } from '../v34'

describe('central-bank priced moves', () => {
  it('buckets priced moves (hikes positive, cuts negative)', () => {
    expect(pricedBucket(null)).toBe(0)
    expect(pricedBucket(0.1)).toBe(0)
    expect(pricedBucket(0.8)).toBe(1)
    expect(pricedBucket(2.3)).toBe(2)
    expect(pricedBucket(-0.6)).toBe(-1)
    expect(pricedBucket(-1.5)).toBe(-2)
    expect(pricedBucket(Number.NaN)).toBe(0)
  })
  it('formats bp and 25bp-move counts', () => {
    expect(pricedText({ change_bp: 58, moves_priced: 2.32 })).toBe('+58bp · 2.3 hikes of 25bp')
    expect(pricedText({ change_bp: -40, moves_priced: -1.6 })).toBe('−40bp · 1.6 cuts of 25bp')
    expect(pricedText({ change_bp: 0.4, moves_priced: 0.01 })).toBe('0bp · 0.0 moves of 25bp')
    expect(pricedText({ change_bp: Number.NaN, moves_priced: Number.NaN })).toBe('—')
    expect(pricedText(null)).toBe('—')
  })
  it('keeps the required implied-path wording', () => {
    expect(IMPLIED_LABEL).toBe('Approximation from official curves (not futures/OIS-implied probabilities)')
  })
})

describe('exposure matrix', () => {
  const factors = [{ factor_id: 'd2y' }, { factor_id: 'usd' }] as MxFactorMeta[]
  const items = [
    { entity_id: 'TECH', name: 'Technology', weighting: 'cw', r2: 0.4, betas: { d2y: { beta: -0.05, t_stat: -2.7 }, usd: { beta: 0.2, t_stat: 2.0 } } },
    { entity_id: 'UTIL', name: '', ticker: 'XLU', weighting: 'cw', r2: null, betas: { d2y: { beta: -0.1, t_stat: 1.5 } } },
  ] as unknown as MxExposureItem[]
  it('maps betas, t-stats and significance levels', () => {
    const m = exposureMatrix(items, factors)
    expect(m[0].cells).toEqual([
      { factor: 'd2y', beta: -0.05, t: -2.7, sig: 2 },
      { factor: 'usd', beta: 0.2, t: 2.0, sig: 1 },
    ])
    expect(m[1].label).toBe('XLU')
    expect(m[1].r2).toBeNull()
    expect(m[1].cells[0].sig).toBe(0)
    expect(m[1].cells[1]).toEqual({ factor: 'usd', beta: null, t: null, sig: 0 })
  })
})

describe('scenario + replay helpers', () => {
  it('drops zero / empty shocks', () => {
    expect(cleanShocks({ rates_2y_bp: -100, rates_10y_bp: 0, oil_pct: null, usd_pct: Number.NaN, credit_bp: 50 })).toEqual({ rates_2y_bp: -100, credit_bp: 50 })
  })
  it('keeps only replay rows with both model and realized values', () => {
    const rows = [
      { entity_id: 'A', name: 'Alpha', ticker: 'AAA', model_implied: 0.1, realized: 0.05 },
      { entity_id: 'B', name: 'Beta', model_implied: 0.1, realized: null },
      { entity_id: 'C', name: 'Gamma', model_implied: null, realized: 0.2 },
    ] as MxReplayRow[]
    expect(replayPoints(rows)).toEqual([{ label: 'AAA', model: 0.1, realized: 0.05 }])
  })
  it('builds 90% beta intervals', () => {
    const ci = betaInterval(-0.05, 0.01)
    expect(ci?.[0]).toBeCloseTo(-0.06645)
    expect(ci?.[1]).toBeCloseTo(-0.03355)
    expect(betaInterval(null, 0.01)).toBeNull()
    expect(betaInterval(0.1, undefined)).toBeNull()
  })
  it('sorts regime rows by the current regime mean', () => {
    const items = [
      { entity_id: 'A', cells: { expansion: { mean: 0.001 } } },
      { entity_id: 'B', cells: { expansion: { mean: 0.01 } } },
      { entity_id: 'C', cells: {} },
    ] as unknown as MxRegimeItem[]
    expect(regimeRows(items, 'expansion').map((r) => r.entity_id)).toEqual(['B', 'A', 'C'])
    expect(regimeRows(items, null)).toBe(items)
  })
})

describe('regime scopes (v1.15)', () => {
  it('lists scopes with performance first and flags the rest', () => {
    const scopes = [
      { scope: 'bcb', states: ['cutting', 'hiking', 'pausing'], current_state: 'cutting', model_based: false, has_performance: false },
      { scope: 'fed', states: ['cutting', 'hiking', 'pausing'], current_state: 'hiking', model_based: false, has_performance: true },
      { scope: 'boe', states: ['cutting', 'hiking', 'pausing'], current_state: null, model_based: false, has_performance: true },
    ] as MxRegimeScope[]
    expect(scopeOptions(scopes)).toEqual([
      { value: 'boe', label: 'BOE', hasPerformance: true },
      { value: 'fed', label: 'FED · now Hiking', hasPerformance: true },
      { value: 'bcb', label: 'BCB · now Cutting (no performance data)', hasPerformance: false },
    ])
  })
})

describe('tone trend sparkline', () => {
  const trend: CbToneTrend = [
    { date: '2025-01-29', tone_model: -1, tone_lexicon: null, action: 'cut' },
    { date: '2025-03-19', tone_model: null, tone_lexicon: 0.2, action: 'hold' },
    { date: '2025-05-07', tone_model: 1, tone_lexicon: null, action: 'hike' },
  ]
  it('maps tone on a fixed -1..+1 scale, skipping missing values', () => {
    const g = toneSpark(trend, 84, 24)
    expect(g.line).toBe('3.0,21.0 81.0,3.0')
    expect(g.zeroY).toBe(12)
  })
  it('places decision markers at each document', () => {
    expect(toneSpark(trend, 84, 24).marks).toEqual([
      { x: 3, action: 'cut' },
      { x: 42, action: 'hold' },
      { x: 81, action: 'hike' },
    ])
    expect(toneSpark([], 84, 24)).toEqual({ line: '', marks: [], zeroY: 12 })
  })
})
