import { describe, expect, it } from 'vitest'
import type { CMacroRegime, CMarketOverview, components } from '../../contract'
import { adaptBeneish, adaptCompanyModel } from '../company'
import {
  adaptCreditSpread,
  adaptIndex,
  adaptIndicator,
  adaptMarketOverview,
  adaptRegime,
  adaptYieldCurve,
  dedupeIndicators,
  fmtDriverLevel,
  liquidMovers,
  regimeName,
  winsorBound,
} from '../market'
import * as fx from './fixtures'

type S = components['schemas']

const regimeKR: CMacroRegime = {
  model_id: '',
  scope: 'KR',
  modelled: false,
  status: 'not_modelled',
  message: 'No regime model for KR; modelled scopes: GLOBAL, US',
  available_scopes: ['GLOBAL', 'US'],
  current: { date: '2026-10-16', label: 'not_modelled', probabilities: {}, modelled: false, scope: 'KR', requested_scope: 'KR', fallback: false, model_id: null },
  history: [],
  inputs: [],
  data_status: 'pending',
}

const regimeGlobal: CMacroRegime = {
  model_id: 'macro_hmm_GLOBAL_v2',
  scope: 'GLOBAL',
  modelled: true,
  status: 'modelled',
  current: { date: '2026-10-12', label: 'expansion', probabilities: { expansion: 0.9, slowdown: 0.06, contraction: 0.01, recovery: 0.03 } },
  history: [
    { date: '2026-06-30', label: 'expansion', probabilities: { expansion: 0.8 } },
    { date: '2026-10-12', label: 'expansion', probabilities: { expansion: 0.9 } },
  ],
  inputs: [
    { id: 'growth', label: 'growth', unit: 'index', points: [{ date: '2026-09-30', value: 100.5 }, { date: '2026-10-12', value: 100.88 }], z_points: [], latest_value: 100.88, latest_z: 0.42, z_basis: 'model_fit' },
    { id: 'credit', label: 'credit', unit: 'percent', points: [{ date: '2026-09-30', value: 1.7 }, { date: '2026-10-12', value: 1.59 }], z_points: [], latest_value: 1.59, latest_z: -0.8, z_basis: 'model_fit' },
    { id: 'inflation', label: 'inflation', unit: 'fraction', points: [{ date: '2026-10-12', value: 0.029 }], z_points: [], latest_value: 0.029, latest_z: 0.1, z_basis: 'model_fit' },
  ],
}

describe('regime (F07 / F08 / F38)', () => {
  it('never turns an unknown label into Expansion', () => {
    expect(regimeName('not_modelled')).toBeNull()
    expect(regimeName('banana')).toBeNull()
    expect(regimeName('slowdown')).toBe('Slowdown')
  })
  it('shows "not modelled" for KR instead of a silent US regime', () => {
    const usSnapshot = fx.marketOverview.regime
    const r = adaptRegime(usSnapshot, regimeKR, 'KR')
    expect(r.status).toBe('not_modelled')
    expect(r.current).toBeNull()
    expect(r.message).toContain('No regime model for KR')
  })
  it('labels the GLOBAL fallback', () => {
    const r = adaptRegime(null, regimeGlobal, 'KR', { requested_scope: 'KR', scope: 'GLOBAL', modelled: false, fallback: true })
    expect(r).toMatchObject({ status: 'fallback', scope: 'GLOBAL', requestedScope: 'KR', current: 'Expansion' })
  })
  it('treats the pre-1.29 pending stub (EU "expansion", no probabilities) as not modelled', () => {
    const stub = { model_id: '', scope: 'EU', current: { date: '2026-10-12', label: 'expansion', probabilities: {} }, history: [], data_status: 'pending' } as CMacroRegime
    expect(adaptRegime(fx.marketOverview.regime, stub, 'EU').status).toBe('not_modelled')
  })
  it('shows only the model states and drivers as level (z) in their true unit', () => {
    const r = adaptRegime(null, regimeGlobal, 'GLOBAL')
    expect(r.status).toBe('modelled')
    expect(r.states).toEqual(['Expansion', 'Slowdown', 'Contraction', 'Recovery'])
    expect(r.drivers[0]).toMatchObject({ value: '100.9', z: 0.42, direction: 'up' })
    expect(r.drivers[1]).toMatchObject({ value: '1.59%', z: -0.8, direction: 'down' })
    expect(r.drivers[2].value).toBe('2.9%')
    for (const d of r.drivers) expect(d.value).not.toMatch(/\bz\b/)
  })
  it('never prints "z" on legacy levels whose unit said z', () => {
    expect(fmtDriverLevel(14.92, 'z', true)).toBe('14.92')
    expect(fmtDriverLevel(100.88, 'z', true)).toBe('100.9')
    const legacy = { ...regimeGlobal, inputs: [{ id: 'vol', label: 'Vol', unit: 'z', points: [{ date: '2026-10-12', value: 14.92 }] }] }
    expect(adaptRegime(null, legacy, 'GLOBAL').drivers[0]).toMatchObject({ value: '14.92', z: null })
  })
})

describe('credit spreads v1.29 (F09)', () => {
  const baa: S['CreditSpread'] = {
    series_id: 'fred:BAA10Y', label: "Moody's Baa − 10Y", region: 'US', spread_type: 'yield_spread', segment: 'baa', unit: 'percent', as_of: '2026-10-16',
    last: 1.59, change_1d: -0.01, change_1m: 0.05, percentile: 0.2, percentile_window_years: 10, spark_window_days: 365,
    spark_points: [{ date: '2025-10-16', value: 1.7 }, { date: '2026-10-16', value: 1.59 }],
  }
  it('uses the calendar 1M change and the empirical percentile with its window', () => {
    const c = adaptCreditSpread(baa)
    expect(c).toMatchObject({ spreadType: 'yield_spread', percentileWindowYears: 10, sparkWindowDays: 365 })
    expect(c.valueBps).toBeCloseTo(159)
    expect(c.change1dBps).toBeCloseTo(-1)
    expect(c.change1mBps).toBeCloseTo(5)
    expect(c.percentile).toBeCloseTo(20)
    expect(c.history[1].value).toBeCloseTo(159)
  })
  it('prefers credit_spreads[] over key_macro credit series', () => {
    const ov = { ...fx.marketOverview, credit_spreads: [baa] } as CMarketOverview
    const m = adaptMarketOverview(ov, null, [])
    expect(m.creditSpreads.map((c) => c.id)).toEqual(['fred:BAA10Y'])
  })
})

describe('macro tiles v1.29 (F10 / F40)', () => {
  const cpi = {
    series_id: 'fred:CPIAUCSL', source_id: 'fred', title: 'CPI all items', country: 'US', category: 'inflation', frequency: 'M', units: 'Index', scale: 'level',
    last_observation_date: '2026-09-01', last_value: 334.1, change_1p: 1.3,
    display_transform: 'yoy', display_label: 'y/y', display_unit: 'fraction', display_value: 0.029, display_change_1p: 0.001, observation_date: '2026-09-01',
    spark_window_days: 1826, display_spark_points: [{ date: '2021-09-01', value: 0.05 }, { date: '2026-09-01', value: 0.029 }],
  } as S['MacroSeriesSummary']
  it('shows the transformed value with its label, observation date and calendar spark', () => {
    const i = adaptIndicator(cpi)
    expect(i).toMatchObject({ value: 0.029, unit: 'pct', displayLabel: 'y/y', asOf: '2026-09-01', sparkWindowDays: 1826 })
    expect(i.history).toHaveLength(2)
  })
  it('percent display units become fractions', () => {
    const i = adaptIndicator({ ...cpi, display_unit: 'percent', display_value: 4.3, display_change_1p: 0.1 })
    expect(i.value).toBeCloseTo(0.043)
    expect(i.change).toBeCloseTo(0.001)
  })
  it('keeps the official source when two sources publish the same print', () => {
    const a = adaptIndicator({ ...cpi, series_id: 'akshare:cn_cpi', source_id: 'akshare', country: 'CN', title: 'CPI y/y (AKShare)' })
    const b = adaptIndicator({ ...cpi, series_id: 'imf:cn_cpi', source_id: 'imf', country: 'CN', title: 'CPI (IMF)' })
    expect(dedupeIndicators([a, b]).map((x) => x.id)).toEqual(['imf:cn_cpi'])
  })
})

describe('yield curves v1.29 (F12 / F20)', () => {
  const cn: S['YieldCurve'] = {
    curve_id: 'CGB', country: 'CN', label: 'China CGB', region: 'CN', scale: 'percent', as_of: '2026-10-13', as_of_min: '2026-10-10', n_market_tenors: 3,
    points: [
      { tenor: 'O/N', tenor_months: 0, yield: 3.0, is_policy_rate: true },
      { tenor: '3M', tenor_months: 3, yield: 1.43, yield_1m_ago: 1.4 },
      { tenor: '2Y', tenor_months: 24, yield: 1.5, yield_1m_ago: 1.45 },
      { tenor: '10Y', tenor_months: 120, yield: 1.8, yield_1m_ago: 1.9 },
    ],
  }
  it('keeps the policy rate out of the curve and reports dates / tenor counts', () => {
    const c = adaptYieldCurve(cn)
    expect(c.current.map((p) => p.tenor)).toEqual(['3M', '2Y', '10Y'])
    expect(c.policyRate).toBeCloseTo(0.03)
    expect(c).toMatchObject({ asOf: '2026-10-13', asOfMin: '2026-10-10', nMarketTenors: 3, spread2s10sBps: 30, change10y1mBps: -10 })
  })
  it('falls back to O/N = policy rate on older payloads', () => {
    const old = { ...cn, n_market_tenors: undefined, as_of_min: undefined, points: cn.points.map((p) => ({ ...p, is_policy_rate: undefined })) }
    const c = adaptYieldCurve(old)
    expect(c.policyRate).toBeCloseTo(0.03)
    expect(c.nMarketTenors).toBe(3)
  })
})

describe('indices, movers, sectors, dates (F06 / F11 / F19 / F21)', () => {
  it('computes 1W from the dated spark and flags > 2σ moves', () => {
    const pts = Array.from({ length: 30 }, (_, k) => ({ date: new Date(Date.UTC(2026, 8, 1 + k)).toISOString().slice(0, 10), value: 100 + (k % 2) * 0.1 }))
    const i = adaptIndex({ id: 'X', name: 'X', region: 'US', last: 110, chg_1d: 0.08, chg_1m: 0.1, chg_ytd: 0.2, as_of: pts[29].date, spark_points: [...pts.slice(0, 29), { date: pts[29].date, value: 110 }] })
    expect(i.change1w).toBeCloseTo(110 / pts[22].value - 1)
    expect(i.bigMove).toBe(true)
  })
  it('liquid movers need a known cap ≥ $1bn and a US price ≥ $2', () => {
    const m = (id: string, cap: number | null, close: number) => ({ id, ticker: id, name: id, region: 'US', currency: 'USD', close, change1d: 0.5, volumeZ: null, marketCapUsd: cap, pxDate: null })
    expect(liquidMovers([m('A', null, 3), m('B', 5e9, 1.5), m('C', 5e9, 30), m('D', 5e8, 30)]).map((x) => x.id)).toEqual(['C'])
  })
  it('winsorises the colour bound', () => {
    const v = [...Array.from({ length: 40 }, (_, k) => k / 10), 236]
    expect(winsorBound(v)).toBeLessThan(10)
    expect(winsorBound([])).toBe(1)
  })
  it('maps data_as_of, alignment and sector counts', () => {
    const ov = {
      ...fx.marketOverview,
      data_as_of: [{ region: 'US', as_of: '2026-10-16', n_securities: 5900 }, { region: 'JP', as_of: '2026-10-16' }],
      dates_aligned: true,
      sector_performance: [{ ...fx.marketOverview.sector_performance[0], n_securities: 600, n_ret_1d: 590, as_of: '2026-10-16', as_of_by_region: { US: '2026-10-16' } }],
      gainers: [{ security: fx.secMsft, close: 500, chg_1d: 0.05, px_date: '2026-10-16' }],
    } as CMarketOverview
    const m = adaptMarketOverview(ov, null, [{ region: 'US', ov }])
    expect(m.dataAsOf[0]).toMatchObject({ region: 'US', asOf: '2026-10-16', nSecurities: 5900 })
    expect(m.datesAligned).toBe(true)
    expect(m.sectorPerformance[0]).toMatchObject({ nSecurities: 600, nRet1d: 590, asOf: '2026-10-16', marketCapUsd: null })
    expect(m.gainers[0]).toMatchObject({ ticker: 'MSFT', pxDate: '2026-10-16' })
  })
  it('degrades without any 1.29 field', () => {
    const m = adaptMarketOverview(fx.marketOverview, null, [])
    expect(m.dataAsOf).toEqual([])
    expect(m.datesAligned).toBeNull()
    expect(m.gainers).toEqual([])
  })
})

describe('company 360 (F15 / F35)', () => {
  it('maps the Beneish breakdown (model-based)', () => {
    const b = adaptBeneish({
      beneish_m: -0.39,
      beneish_flag: true,
      beneish_level: 'watch',
      beneish: {
        m_score: -0.39, m_score_growth_adj: -2.4, threshold: -1.78, textbook_flag: true, flag: false, level: 'watch', growth_driven: true,
        red_flags: ['dsri'], components: { dsri: 1.5, sgi: 1.9 }, contributions: { dsri: 0.46, sgi: 0.81 }, manipulator_means: { dsri: 1.46, sgi: 1.61 },
        applicable: true, model_based: true, method: 'Beneish (1999) 8-variable', corroboration: [],
      } as S['BeneishDetail'],
    })
    expect(b).toMatchObject({ level: 'watch', mScore: -0.39, mScoreGrowthNeutral: -2.4, growthDriven: true, redFlags: ['dsri'] })
    expect(b?.components.map((c) => c.id)).toEqual(['dsri', 'sgi'])
    expect(b?.components[0]).toMatchObject({ redFlag: true, contribution: 0.46, manipulatorMean: 1.46 })
    expect(adaptBeneish({ beneish_m: -1, beneish_flag: false })).toBeNull()
  })
  it('keeps the raw model type so ensemble and xgboost do not collide', () => {
    const m = adaptCompanyModel({
      ...fx.modelExplanation,
      models: [
        { model_type: 'xgboost_rank', model_run_id: 'M1', horizon: '3M', date: '2026-10-09', rank_pct: 0.5 },
        { model_type: 'ensemble', model_run_id: 'M2', horizon: '3M', date: '2026-10-09', rank_pct: 0.6 },
      ],
    } as Parameters<typeof adaptCompanyModel>[0])
    const keys = m.predictions.map((p) => `${p.modelVersion}-${p.horizonDays}`)
    expect(new Set(keys).size).toBe(2)
    expect(m.predictions.map((p) => p.modelType)).toEqual(['xgboost_rank', 'ensemble'])
  })
})
