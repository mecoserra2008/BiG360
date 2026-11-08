import { describe, expect, it } from 'vitest'
import { type EmConnectedness, type EmStudyCell, type EvMapCountry, carPath, connectednessGraph, emergingLabel, isExplosive, metricValue, pearson, relativeAttention, storyQuery, studyKey, toneBucket } from '../types'

const country = (p: Partial<EvMapCountry>): EvMapCountry =>
  ({
    country: 'US',
    name: 'United States',
    events: 10,
    articles: 5,
    mentions: 20,
    conflict_share: 0.3,
    goldstein: 0.1,
    tone: -1.5,
    attention_z: null,
    attention_z_last: null,
    tone_z_min: null,
    intensity: 0.8,
    ...p,
  }) as EvMapCountry

describe('map metrics', () => {
  it('reads the selected metric and returns null when missing', () => {
    const c = country({ attention_z: 2.4 })
    expect(metricValue(c, 'intensity')).toBe(0.8)
    expect(metricValue(c, 'attention_z')).toBe(2.4)
    expect(metricValue(c, 'tone')).toBe(-1.5)
    expect(metricValue(c, 'conflict_share')).toBe(0.3)
    expect(metricValue(country({}), 'attention_z')).toBeNull()
  })
  it('computes relative attention as a log2 share ratio', () => {
    // country has 10% of all coverage but 40% of the theme → log2(4) = 2
    expect(relativeAttention(40, 100, 100, 1000)).toBeCloseTo(2)
    expect(relativeAttention(0, 100, 100, 1000)).toBeNull()
  })
})

describe('model labels', () => {
  it('labels emerging hotspot classes', () => {
    expect(emergingLabel('consecutive_hot')).toBe('Consecutive hot spot')
    expect(emergingLabel('diminishing_cold')).toBe('Diminishing cold spot')
    expect(emergingLabel('none')).toBe('No pattern')
    expect(emergingLabel(null)).toBe('No pattern')
  })
  it('flags explosive Hawkes fits (branching ratio > 1)', () => {
    expect(isExplosive({ branching_ratio: 1.04 })).toBe(true)
    expect(isExplosive({ branching_ratio: 0.68 })).toBe(false)
    expect(isExplosive({ branching_ratio: null })).toBe(false)
  })
  it('buckets story tone', () => {
    expect(toneBucket(-7.3)).toBe(-1)
    expect(toneBucket(2.5)).toBe(1)
    expect(toneBucket(0.4)).toBe(0)
    expect(toneBucket(null)).toBe(0)
  })
})

describe('event-study CAR path', () => {
  const cell = (window: string, car: number, n = 10): EmStudyCell => ({
    shock_kind: 'attention',
    level: 'issuer',
    theme: 'all',
    iso2: 'ALL',
    sector: 'REAL',
    window,
    n_events: n,
    mean_car: car,
    median_car: null,
    std_error: null,
    ci_low: car - 1,
    ci_high: car + 1,
    t_stat: null,
    p_value: 0.01,
    mean_bhar: null,
    share_negative: null,
    first_event: null,
    last_event: null,
    benchmark: null,
  })
  it('orders windows by their end day and keeps the pre-event window', () => {
    const p = carPath([cell('0,10', -3), cell('0,1', -1), cell('-1,5', -2.4), cell('0,5', -2.6)])
    expect(p.map((x) => x.window)).toEqual(['0,1', '-1,5', '0,5', '0,10'])
    expect(p[2]).toMatchObject({
      end: 5,
      car: -2.6,
      lo: -3.6,
      hi: -1.6,
      n: 10,
    })
  })
  it('keys cells without the window', () => {
    expect(studyKey(cell('0,5', 1))).toBe(studyKey(cell('0,10', 2)))
  })
})

describe('GPR validation correlation', () => {
  it('computes Pearson on paired finite values', () => {
    expect(pearson([1, 2, 3, 4], [2, 4, 6, 8]).r).toBeCloseTo(1)
    expect(pearson([1, 2, 3, 4], [8, 6, 4, 2]).r).toBeCloseTo(-1)
    const r = pearson([1, null, 3, 4, 5], [1, 9, null, 4, 5])
    expect(r.n).toBe(3)
    expect(pearson([1, 2], [1, 2]).r).toBeNull()
  })
})

describe('connectedness network', () => {
  const c = {
    universe: 'country_ret',
    date: '2026-10-09',
    total: 85,
    window: 200,
    horizon: 10,
    lags: 1,
    n_nodes: 3,
    nodes: [
      {
        node: 'ZA',
        name: 'South Africa',
        series_id: 'ETF:EZA',
        to_others: 150,
        from_others: 90,
        net: 60,
      },
      {
        node: 'US',
        name: 'United States',
        series_id: 'ETF:SPY',
        to_others: 80,
        from_others: 110,
        net: -30,
      },
      {
        node: 'FI',
        name: 'Finland',
        series_id: 'ETF:EFNL',
        to_others: 90,
        from_others: 90,
        net: 0,
      },
    ],
    pairs: [
      { from: 'ZA', to: 'US', net: 4.2 },
      { from: 'XX', to: 'US', net: 1 },
    ],
    history: [],
    model_based: true,
    data_status: 'ok',
  } as unknown as EmConnectedness
  it('sizes nodes by |net| and keeps edges between known nodes', () => {
    const g = connectednessGraph(c)
    expect(g.nodes.find((n) => n.id === 'ZA')).toMatchObject({
      role: 'transmitter',
      size: 34,
    })
    expect(g.nodes.find((n) => n.id === 'US')).toMatchObject({
      role: 'receiver',
      size: 21,
    })
    expect(g.edges).toEqual([{ source: 'ZA', target: 'US', value: 4.2 }])
  })
})

describe('story query', () => {
  it('maps filters to API params', () => {
    expect(storyQuery({ country: 'DE', tone: 'neg', days: 7 })).toMatchObject({
      country: 'DE',
      tone_max: -2,
      tone_min: undefined,
      days: 7,
      sort: 'salience',
    })
    expect(storyQuery({ start: '2026-10-01', end: '2026-10-05', tone: 'pos' })).toMatchObject({
      start: '2026-10-01',
      end: '2026-10-05',
      days: undefined,
      tone_min: 2,
    })
  })
})

describe('history ranges (v1.20.0)', () => {
  it('maps presets to days and picks weekly beyond a year', async () => {
    const { rangeDays, rangeFreq, coverageText, EV_MAX_DAYS } = await import('../types')
    const cov = { first_date: '2025-05-17', last_date: '2026-10-13' }
    expect(rangeDays('7D', cov)).toBe(7)
    expect(rangeDays('3Y', cov)).toBe(1095)
    expect(rangeDays('All', cov, '2026-10-13')).toBe(515)
    expect(rangeDays('All', { first_date: '2015-01-01', last_date: '2026-10-13' })).toBe(EV_MAX_DAYS)
    expect(rangeDays('All', null)).toBe(1095)
    expect(rangeFreq(365)).toBe('day')
    expect(rangeFreq(366)).toBe('week')
    expect(coverageText(cov)).toBe('2025-05-17 → 2026-10-13')
    expect(coverageText(null)).toBeNull()
  })
})
