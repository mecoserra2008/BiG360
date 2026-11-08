import { describe, expect, it } from 'vitest'
import { bucketLines, expectancy, fmtExcess, fmtProb, fmtR, hasExperimental, holdCell, pickHorizon, probSegments, subScoreRows, thesisProbRows, visibleHorizons, zoneHoldIndex } from '../adapters'
import type { TpScore, TpSetup, TpSetupHorizon, TpZone, TpZones } from '../types'

const hz = (horizon: number, p_win: number, p_loss: number, o: Partial<TpSetupHorizon> = {}): TpSetupHorizon => ({
  horizon, is_setup_horizon: false, p_win, p_loss, p_neither: 1 - p_win - p_loss, expected_r: p_win * 1.5 - p_loss - 0.02,
  break_even_p: 0.4, gate: 'pass', experimental: false, ...o,
})

const security = { security_id: 'S1', issuer_id: 'I1', ticker: 'AAA', name: 'Alpha', region: 'US', country: 'US', currency: 'USD' }
const setup = {
  security, as_of: '2026-10-16', last_close: 100, model_based: true, mode: 'setup',
  setup: { source: 'setup', bias: 'long', direction: 'long', entry: 99, target: 105, stop: 96, reward_risk: 1.45, reward_risk_close: 1.25 },
  setup_horizon: 10,
  horizons: [hz(5, 0.2, 0.25), hz(10, 0.36, 0.38, { is_setup_horizon: true }), hz(20, 0.5, 0.45), hz(60, 0.55, 0.44, { experimental: true, gate: 'fail' })],
  pool: { region: 'US', vol_regime: 'mid', fit_date: '2026-10-17' },
  validation: [{ output: 't2_h60', verdict: 'fail', experimental: true }],
  labels: {}, validation_doc: 'docs/TECHNICALS_V2_VALIDATION.md', data_status: 'ok',
} as unknown as TpSetup

describe('setup probabilities', () => {
  it('hides experimental horizons unless toggled and picks the setup horizon', () => {
    expect(visibleHorizons(setup, false).map((h) => h.horizon)).toEqual([5, 10, 20])
    expect(visibleHorizons(setup, true).map((h) => h.horizon)).toEqual([5, 10, 20, 60])
    expect(pickHorizon(setup, false)?.horizon).toBe(10)
    expect(pickHorizon(setup, false, 60)?.horizon).toBe(10)            // requested but experimental -> fallback
    expect(pickHorizon(setup, true, 60)?.horizon).toBe(60)
    expect(pickHorizon(undefined, true)).toBeNull()
    expect(hasExperimental(setup)).toBe(true)
  })
  it('builds a stacked bar that sums to one', () => {
    const seg = probSegments(pickHorizon(setup, false))
    expect(seg.map((s) => s.key)).toEqual(['win', 'loss', 'neither'])
    expect(seg.reduce((a, s) => a + s.value, 0)).toBeCloseTo(1, 9)
    expect(seg[2].label).toBe('Neither by 10d')
    expect(probSegments(null)).toEqual([])
  })
  it('formats probabilities, R and the expectancy line', () => {
    expect(fmtProb(0.4123)).toBe('41%')
    expect(fmtProb(0.001)).toBe('<1%')
    expect(fmtProb(null)).toBe('—')
    expect(fmtR(0.123)).toBe('+0.12R')
    expect(fmtR(-0.5)).toBe('−0.50R')
    expect(fmtExcess(Math.log(1.0042))).toBe('+0.42%')
    const e = expectancy(hz(20, 0.5, 0.45))
    expect(e?.tone).toBe('up')
    expect(e?.text).toContain('break-even 40%')
    expect(expectancy(hz(20, 0.2, 0.7))?.tone).toBe('down')
  })
  it('thesis rows follow the toggle', () => {
    expect(thesisProbRows(setup, false)).toHaveLength(3)
    expect(thesisProbRows(setup, false)[1]).toMatchObject({ horizon: 10, win: '36%', own: true })
  })
})

const zone = (o: Partial<TpZone>): TpZone => ({
  zone_id: 1, side: 'support', zone_low: 95, zone_high: 97, p_touch: 0.6, p_hold: 0.55, p_hold_ctrl: 0.53, p_touch_hold: 0.33, edge: 0.02,
  edge_significant: false, edge_direction: 'none', edge_ci: [-0.01, 0.012], edge_label: 'no reliable edge', gate: 'pass', experimental: false,
  edge_experimental: true, in_zone: false, member_families: [], member_count: 2, ...o,
} as TpZone)

describe('zone hold column', () => {
  it('shows P(hold | touch) with "no reliable edge" when not significant', () => {
    const c = holdCell(zone({}), false)
    expect(c.text).toBe('55%')
    expect(c.badge).toEqual({ label: 'no reliable edge', tone: 'neutral' })
    expect(c.sub).toBe('touch+hold 33%')
    expect(c.title).toContain('No reliable edge')
  })
  it('shows the edge badge only when significant and not experimental (or toggled)', () => {
    const z = zone({ edge_significant: true, edge_direction: 'better_than_random', edge: 0.031, edge_ci: [0.01, 0.05], edge_experimental: false })
    expect(holdCell(z, false).badge).toEqual({ label: '+3.1pp vs random', tone: 'up' })
    const ze = zone({ edge_significant: true, edge_direction: 'better_than_random', edge: 0.031, edge_experimental: true })
    expect(holdCell(ze, false).badge?.label).toBe('no reliable edge')
    expect(holdCell(ze, true).badge?.label).toBe('+3.1pp vs random')
  })
  it('hides an experimental hold model unless toggled', () => {
    expect(holdCell(zone({ experimental: true }), false)).toMatchObject({ text: '—', hidden: true, badge: null })
    // the edge flag is its own (validated) output: still shown when only the hold probability is experimental
    const ze = zone({ experimental: true, edge_significant: true, edge_direction: 'worse_than_random', edge: -0.044, edge_experimental: false })
    expect(holdCell(ze, false)).toMatchObject({ text: '—', hidden: true, badge: { label: '-4.4pp vs random', tone: 'down' } })
    expect(holdCell(zone({ experimental: true }), true).text).toBe('55%')
    expect(holdCell(undefined, true).text).toBe('—')
  })
  it('indexes zones by the UI zone id', () => {
    const d = { zones: [zone({ zone_id: 3 })], data_status: 'ok' } as unknown as TpZones
    expect(zoneHoldIndex(d).get('z3')?.p_hold).toBe(0.55)
    expect(zoneHoldIndex(undefined).size).toBe(0)
  })
})

describe('score calibration', () => {
  const sc = {
    security, as_of: '2026-10-16', model_based: true, technical_score: 72, technical_score_pct: 61, pct_technical: 64,
    cross_section: 'region x sector', n_peers: 40,
    sub_scores: [{ name: 'trend', raw: 100, percentile: 71.5 }, { name: 'rr', raw: 12.5, percentile: 22 }],
    buckets: [
      { horizon: 5, decile: 8, n: 4000, median: 0.001, hit_rate: 0.51, edge_significant: false, line: 'Historically this score bucket -> next 5 sessions ...', gate: 'pass', experimental: false },
      { horizon: 20, decile: 8, n: 4000, median: 0.004, hit_rate: 0.53, edge_significant: true, line: 'Historically this score bucket -> next 20 sessions ...', gate: 'pass', experimental: false },
      { horizon: 60, decile: 8, n: 4000, median: 0.01, hit_rate: 0.55, edge_significant: false, line: 'x', gate: 'fail', experimental: true },
    ],
    tables: [], validation: [], labels: {}, validation_doc: 'x', data_status: 'ok',
  } as unknown as TpScore
  it('maps sub-scores with labels and percentiles', () => {
    expect(subScoreRows(sc)).toEqual([{ key: 'trend', label: 'Trend', raw: 100, percentile: 71.5 }, { key: 'rr', label: 'Reward:risk', raw: 12.5, percentile: 22 }])
  })
  it('bucket lines carry the edge badge and respect the toggle', () => {
    const b = bucketLines(sc, false)
    expect(b.map((x) => x.horizon)).toEqual([5, 20])
    expect(b[0].badge.label).toBe('no out-of-sample edge')
    expect(b[1].badge).toEqual({ label: 'out-of-sample edge', tone: 'up' })
    expect(bucketLines(sc, true)).toHaveLength(3)
  })
})
