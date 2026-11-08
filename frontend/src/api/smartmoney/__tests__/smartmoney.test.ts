import { describe, expect, it } from 'vitest'
import { euChange, euShortSeries, eventMeta, holderMix, lagDays, lagText, regulatorLabel, signTone, siSeries, trendSeries } from '../adapters'
import type { CoHolder, CoOwnership, CoTrendPoint, SmEuShort } from '../types'

const eu = (p: Partial<SmEuShort>): SmEuShort => ({
  regulator: 'fca',
  regulator_label: 'UK FCA',
  country: 'GB',
  position_type: 'individual',
  holder_name: 'A',
  isin: 'GB0000000001',
  position_date: '2026-01-01',
  knowledge_date: '2026-01-05',
  knowledge_date_estimated: true,
  below_threshold: false,
  source_url: 'u',
  position_pct: 0.6,
  ...p,
})

describe('labels', () => {
  it('shows the 13F lag as quarter end + filing cutoff', () => {
    const lag = { period_end: '2026-06-30', cutoff: '2026-08-17', is_complete: true, n_securities: 10, label: '' }
    expect(lagText(lag)).toBe('Positions as of quarter end 2026-06-30, filed by 2026-08-17')
    expect(lagText({ ...lag, is_complete: false })).toContain('filing window still open')
    expect(lagText(null)).toContain('no quarter')
    expect(lagDays(lag)).toBe(48)
  })
  it('maps regulators and event types', () => {
    expect(regulatorLabel('fi_se')).toBe('Sweden FI')
    expect(regulatorLabel('xyz')).toBe('XYZ')
    expect(eventMeta('switch_13g_to_13d').label).toBe('13G → 13D')
    expect(eventMeta('other').tone).toBe('neutral')
  })
  it('tones signed values (inverted for short interest)', () => {
    expect(signTone(0.1)).toBe('up')
    expect(signTone(0.1, true)).toBe('down')
    expect(signTone(0)).toBe('neutral')
    expect(signTone(null)).toBe('neutral')
  })
})

describe('series', () => {
  it('sorts ownership trend by quarter', () => {
    const pts = [
      { period_end: '2026-06-30', knowledge_date: '2026-08-17', is_complete: true, n_holders: 12, inst_own_pct: 0.6 },
      { period_end: '2026-03-31', knowledge_date: '2026-05-18', is_complete: true, n_holders: 10, inst_own_pct: null },
    ] as CoTrendPoint[]
    const s = trendSeries(pts)
    expect(s.x).toEqual(['2026-03-31', '2026-06-30'])
    expect(s.pct).toEqual([null, 0.6])
    expect(s.holders).toEqual([10, 12])
  })
  it('falls back to shares when SI % is unknown', () => {
    const rows = [
      { settlement_date: '2026-09-30', knowledge_date: '2026-10-09', squeeze_flag: false, ftd_spike: false, si_shares: 200, si_pct_out: null },
      { settlement_date: '2026-09-15', knowledge_date: '2026-09-24', squeeze_flag: false, ftd_spike: false, si_shares: 100, si_pct_out: null },
    ] as CoOwnership['short_interest']
    expect(siSeries(rows)).toEqual({ x: ['2026-09-15', '2026-09-30'], y: [100, 200], unit: 'shares' })
  })
  it('sums active holder positions over time (closing notifications drop out)', () => {
    const s = euShortSeries([
      eu({ holder_name: 'A', position_date: '2026-01-01', position_pct: 0.6 }),
      eu({ holder_name: 'B', position_date: '2026-01-02', position_pct: 0.8 }),
      eu({ holder_name: 'A', position_date: '2026-01-03', position_pct: 0.4, below_threshold: true }),
      eu({ holder_name: 'B', position_date: '2026-01-03', position_pct: 0.9 }),
    ])
    expect(s.x).toEqual(['2026-01-01', '2026-01-02', '2026-01-03'])
    expect(s.y.map((v) => Math.round(v * 100) / 100)).toEqual([0.6, 1.4, 0.9])
  })
  it('prefers regulator aggregates when present', () => {
    const s = euShortSeries([eu({ position_type: 'aggregate', holder_name: '(aggregate)', position_pct: 2.1, position_date: '2026-08-01' }), eu({})])
    expect(s).toEqual({ x: ['2026-08-01'], y: [2.1] })
  })
  it('computes register changes in percentage points', () => {
    expect(euChange({ position_pct: 0.75, prev_pct: 0.6 })).toBe(0.15)
    expect(euChange({ position_pct: 0.75, prev_pct: null })).toBeNull()
  })
  it('counts the holder mix', () => {
    const h = (p: Partial<CoHolder>): CoHolder => ({ filer_cik: 'x', status: 'unchanged', hedge_fund_like: false, in_smart_cohort: false, ...p })
    expect(holderMix([h({ status: 'new', hedge_fund_like: true }), h({ status: 'trimmed', in_smart_cohort: true, hedge_fund_like: true }), h({})])).toEqual({
      total: 3,
      hf: 2,
      smart: 1,
      newOrAdded: 1,
      trimmed: 1,
    })
  })
})
