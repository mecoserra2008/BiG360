import { describe, expect, it } from 'vitest'
import { contextChips, pctl } from '../adapters'
import type { TechContext } from '../types'

const base = {
  security: { security_id: 'S1', ticker: 'AAA', name: 'A', region: 'US', country: 'US', currency: 'USD' },
  as_of: '2026-10-09',
  last_close: 100,
  model_based: true,
  lookback_sessions: 504,
  range_width: { level: 90, horizon: 20, width_log: 0.2, width_pct: 0.2, percentile: 0.06, state: 'squeeze', n: 504, radius: 2.8 },
  realised_vol: { window: 20, vol: 0.24, percentile: 0.45, state: 'normal', n: 504 },
  implied_vol: { available: false, value: null, source: null, note: 'Implied volatility: n/a - no per-stock source' },
  atr: { window: 14, atr: 2, atr_pct: 0.02, percentile: 0.95, state: 'expansion', n: 504 },
  adx: { window: 14, adx: 31.4, plus_di: 28, minus_di: 12, state: 'trending', direction: 'up' },
  trend: { daily: 'up', weekly: 'up', alignment: 'aligned_up' },
  earnings: { next_date: '2026-10-28', session: 'post_market', date_source: 'provider_estimate', sessions_ahead: 13, in_window: true, event_pool: true, typical_abs_move: 0.042, n_events: 12 },
  notes: { range_width: 'RW.', realised_vol: 'RV.', atr: 'ATR.', adx: 'ADX.', trend: 'TR.', earnings: 'EA.' },
  data_status: 'ok',
} as unknown as TechContext

describe('context chips', () => {
  it('builds one chip per available measure, each with a tooltip', () => {
    const c = contextChips(base)
    expect(c.map((x) => x.key)).toEqual(['range', 'rv', 'iv', 'atr', 'adx', 'trend', 'earnings'])
    const by = Object.fromEntries(c.map((x) => [x.key, x]))
    expect(by.range.label).toBe('Squeeze')
    expect(by.range.value).toBe('20d ±10.0% · p6')
    expect(by.range.tone).toBe('info')
    expect(by.range.tooltip).toContain('RW.')
    expect(by.iv.value).toBe('n/a')
    expect(by.iv.tooltip).toContain('n/a')
    expect(by.atr.tone).toBe('warn')
    expect(by.adx.value).toBe('31 trending ▲')
    expect(by.adx.tone).toBe('up')
    expect(by.trend.label).toBe('Trend aligned')
    expect(by.trend.value).toBe('D ▲ · W ▲')
    expect(by.earnings.value).toBe('in 13 sessions · in range · typical ±4.2%')
    expect(by.earnings.tooltip).toContain('earnings-event pool')
    for (const x of c) expect(x.tooltip.length).toBeGreaterThan(0)
  })
  it('states: expansion, mixed trend, earnings outside the window; pending gives none', () => {
    const c = contextChips({
      ...base,
      range_width: { ...base.range_width, percentile: 0.93, state: 'expansion' },
      trend: { daily: 'up', weekly: 'down', alignment: 'mixed' },
      earnings: { ...base.earnings, sessions_ahead: 40, in_window: false, typical_abs_move: null, n_events: 0 },
    } as TechContext)
    const by = Object.fromEntries(c.map((x) => [x.key, x]))
    expect(by.range.label).toBe('Expansion')
    expect(by.range.tone).toBe('warn')
    expect(by.trend.tone).toBe('warn')
    expect(by.earnings.value).toBe('in 40 sessions')
    expect(contextChips({ ...base, data_status: 'pending' } as TechContext)).toEqual([])
    expect(contextChips(undefined)).toEqual([])
    expect(pctl(0.456)).toBe('p46')
    expect(pctl(null)).toBeNull()
  })
})
