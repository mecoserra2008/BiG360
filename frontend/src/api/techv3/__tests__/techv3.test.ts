import { describe, expect, it } from 'vitest'
import { breakoutChip, compositeHeadline, evidenceLine, factorRows, fmtIc, fmtPct, fmtProb, hiddenCount, visibleFactors } from '../adapters'
import type { Tv3Breakout, Tv3Factor, Tv3Factors } from '../types'

const security = { security_id: 'S1', issuer_id: 'I1', ticker: 'AAA', name: 'Alpha', region: 'US' as const, country: 'US', currency: 'USD' }

const fac = (id: string, sign: 1 | -1, pct: number, verdict: 'pass' | 'fail', ic20: number): Tv3Factor => ({
  id: id as Tv3Factor['id'], label: id.toUpperCase(), reference: `Ref ${id}`, sign, direction_note: `note ${id}`, value: 0.5,
  percentile: pct, aligned_percentile: sign > 0 ? pct : 100 - pct, cross_section: 'sector',
  ic: [5, 20, 60].map((h) => ({ horizon: h, mean_ic: ic20 * sign, aligned_ic: ic20, t_stat: (h === 20 ? 4.2 : 1.1) * sign, p_bh: h === 20 ? 0.0004 : 0.3, region_ic: ic20 * sign, region_t: 2.1 })),
  verdict, experimental: verdict !== 'pass', tradable: id === 'hi52' ? true : verdict === 'pass' ? false : null, note: verdict === 'pass' && id !== 'hi52' ? 'Rank (median) edge only' : null, badge: verdict === 'pass' ? { label: `edge · IC +${ic20.toFixed(3)}`, tone: 'up' } : { label: 'no reliable edge', tone: 'neutral' },
})

const factors = (compositePass: boolean): Tv3Factors => ({
  security, as_of: '2026-10-16', region: 'US', sector_id: 'tech', cross_section: 'region x sector', n_peers: 120, liquid: true,
  size_bucket: 'large', adv20_usd: 5e8, region_validated: true,
  factors: [fac('hi52', 1, 80, 'pass', 0.021), fac('maxret', -1, 90, 'pass', 0.015), fac('strev', -1, 30, 'fail', 0.004)],
  composite: { verdict: compositePass ? 'pass' : 'fail', experimental: !compositePass, members: ['hi52', 'maxret'], variant: 'comp_wf',
               value: compositePass ? 45 : null, percentile: compositePass ? 52 : null, ic_20: 0.025, t_20: 4.8, v2_ic_20: -0.012, beats_v2: compositePass,
               note: compositePass ? 'Composite passed' : 'No composite' },
  validation: { generated_at: null, n_cells_tested: 105, multiple_testing: 'BH', passing: ['hi52', 'maxret'], period: null, universe: null },
  labels: {}, validation_doc: 'docs/TECHNICALS_V3_VALIDATION.md', data_status: 'ok',
})

describe('technical factors', () => {
  it('hides factors that failed the gate unless the experimental toggle is on', () => {
    const d = factors(true)
    expect(visibleFactors(d, false).map((f) => f.id)).toEqual(['hi52', 'maxret'])
    expect(visibleFactors(d, true)).toHaveLength(3)
    expect(hiddenCount(d, false)).toBe(1)
    expect(hiddenCount(d, true)).toBe(0)
    expect(visibleFactors({ ...d, data_status: 'pending' }, true)).toEqual([])
  })
  it('aligns percentiles with the literature sign and builds the tooltip', () => {
    const rows = factorRows(factors(true), false)
    const mx = rows.find((r) => r.id === 'maxret')!
    expect(mx.percentile).toBe(90)
    expect(mx.aligned).toBe(10)                          // a lottery-like stock is unfavourable for MAX (sign -1)
    expect(mx.favourable).toBe(false)
    expect(rows[0].favourable).toBe(true)
    expect(rows[0].short).toBe('52-week-high proximity')
    expect(rows[0].tooltip).toContain('Ref hi52')
    expect(rows[0].tooltip).toContain('20d IC +0.021 (t 4.2, BH p <0.001)')
    expect(rows[0].badge).toEqual({ label: 'edge · IC +0.021', tone: 'up' })
    expect(mx.tradable).toBe(false)
    expect(mx.tooltip).toContain('Rank (median) edge only')
    expect(mx.tooltip).toContain('20d IC +0.015 (t 4.2')                 // aligned t for a negative-sign factor
  })
  it('uses the composite as the score headline only when it passed', () => {
    const h = compositeHeadline(factors(true))
    expect(h.mode).toBe('composite')
    expect(h.value).toBe(45)
    expect(h.ic).toContain('vs v2 score −0.012')
    const f = compositeHeadline(factors(false))
    expect(f.mode).toBe('no_edge')
    expect(f.value).toBeNull()
    expect(compositeHeadline(undefined).mode).toBe('none')
  })
  it('formats', () => {
    expect(fmtIc(0.0213)).toBe('+0.021')
    expect(fmtIc(-0.004)).toBe('−0.004')
    expect(fmtIc(null)).toBe('—')
    expect(fmtPct(61.6)).toBe('p62')
    expect(fmtProb(0.3141)).toBe('31%')
  })
})

const bo = (o: Partial<Tv3Breakout> = {}): Tv3Breakout => ({
  security, as_of: '2026-10-16', model_based: true, state: 'active', direction: 1, lookback: 55, event_date: '2026-10-10', sessions_since: 4,
  level: 100, target: 108, last_close: 103, confirmed: true, confirm_score: 72, relvol50: 2.1, clv: 0.8, p_follow: 0.31, p_geo: 0.27, base_rate: 0.24,
  horizon: 20, gate: 'pass', experimental: false,
  evidence: { confirmed_rate: 0.26, unconfirmed_rate: 0.22, control_rate: 0.23, base_rate: 0.23, diff: 0.04, diff_ci: [0.02, 0.06],
              geometry_adjusted_diff: 0.01, geometry_adjusted_ci: [0.0, 0.02], significant: false, verdict: 'fail', ece: 0.01,
              brier_vs_geometry: -0.0003, n_events: 1000, summary: null },
  recent: [], labels: { breakout: 'B.', confirmation: 'C.' }, validation_doc: 'docs/TECHNICALS_V3_VALIDATION.md', data_status: 'ok', ...o,
})

describe('breakout chip', () => {
  it('shows the state and the calibrated probability when the gate passed', () => {
    const c = breakoutChip(bo(), false)
    expect(c.visible).toBe(true)
    expect(c.label).toBe('▲ 55d high breakout · active')
    expect(c.tone).toBe('up')
    expect(c.prob).toBe('31%')
    expect(c.sub).toBe('confirmation score 72 · 4 sessions ago')
    expect(c.confirmation).toBe('volume-confirmed')
  })
  it('hides an experimental probability unless toggled, keeps the state', () => {
    const b = bo({ experimental: true, gate: 'fail', state: 'failed', direction: -1 })
    const c = breakoutChip(b, false)
    expect(c.visible && c.prob === null && c.probHidden).toBe(true)
    expect(c.tone).toBe('warn')
    expect(c.label).toContain('▼ 55d low breakout · failed')
    expect(breakoutChip(b, true).prob).toBe('31%')
  })
  it('renders nothing without a breakout and an honest evidence line', () => {
    expect(breakoutChip(bo({ state: 'none' }), true).visible).toBe(false)
    expect(breakoutChip(undefined, true).visible).toBe(false)
    const e = evidenceLine(bo())!
    expect(e.tone).toBe('neutral')
    expect(e.text).toContain('confirmed 26% vs unconfirmed 22%')
    expect(e.text).toContain('no reliable edge')
    const e2 = evidenceLine(bo({ evidence: { ...bo().evidence, significant: true } }))!
    expect(e2.tone).toBe('up')
    const e3 = evidenceLine(bo({ evidence: { ...bo().evidence, significant: true, summary: 'No tradable edge after costs.' } }))!
    expect(e3.tone).toBe('info')
    expect(e3.text).toContain('follow through significantly more often. Not a tradable edge after costs')
    expect(e3.title).toBe('No tradable edge after costs.')
  })
})
