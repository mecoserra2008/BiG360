import { describe, expect, it } from 'vitest'
import { driverChip, fmtKelly, fmtPp, metaGateLine, metaHorizon, metaRow, pickMetaHorizon, thesisMetaRows } from '../adapters'
import type { TmHorizon, TmView } from '../types'

const drv = [
  { feature: 'c_rw_pct', label: 'Range-width percentile (squeeze)', group: 'techctx', value: 0.08, contribution: -0.21, direction: 'lowers' as const },
  { feature: 'm_risk', label: 'Risk-on / off gauge', group: 'market', value: 0.9, contribution: 0.12, direction: 'raises' as const },
  { feature: 'p_z_signed', label: 'Peer break-away z (with the setup)', group: 'peer', value: 2.4, contribution: 0.05, direction: 'raises' as const },
  { feature: 'e_days_ahead', label: 'Sessions to earnings', group: 'earnings', value: 5, contribution: 0.01, direction: 'raises' as const },
]

const mh = (horizon: number, o: Partial<TmHorizon> = {}): TmHorizon => ({
  horizon, p_win: 0.46, p_loss: 0.4, p_neither: 0.14, expected_r: 0.08, take: true, decision: 'take', kelly_fraction: 0.0081,
  size_rel: 1.4, threshold: 0.025, t2_p_win: 0.42, t2_p_loss: 0.43, t2_expected_r: 0.02, delta_p_win: 0.04, drivers: drv,
  model_kind: 'xgb', in_domain: true, gate: 'pass', experimental: false, decision_gate: 'fail', decision_experimental: true, ...o,
})

const view: TmView = {
  source: 'stored', fit_date: '2026-10-22',
  horizons: [mh(10, { gate: 'fail', experimental: true }), mh(20), mh(40, { gate: 'fail', experimental: true, decision: 'skip', take: false, kelly_fraction: 0 })],
  validation: [{ output: 'meta_h20', verdict: 'pass', experimental: false }, { output: 'decision_h20', verdict: 'fail', experimental: true }],
  note: null,
}

describe('meta-labelled setup adapters', () => {
  it('picks the horizon on screen, else 20', () => {
    expect(metaHorizon(view, 40)?.horizon).toBe(40)
    expect(metaHorizon(view, 5)?.horizon).toBe(20)
    expect(metaHorizon(null, 20)).toBeNull()
    expect(metaHorizon({ ...view, horizons: [mh(10)] }, 60)?.horizon).toBe(10)
  })
  it('falls back to a production horizon when the one on screen is experimental (toggle off)', () => {
    expect(pickMetaHorizon(view, 40, false)?.horizon).toBe(20)
    expect(pickMetaHorizon(view, 40, true)?.horizon).toBe(40)
    expect(pickMetaHorizon(view, 20, false)?.horizon).toBe(20)
    const allExp: TmView = { ...view, horizons: view.horizons.map((h) => ({ ...h, experimental: true })) }
    expect(pickMetaHorizon(allExp, 20, false)?.horizon).toBe(20)          // nothing passes: the (hidden) row on screen
  })
  it('builds the context-adjusted row with T2 delta, decision gating and the top 3 drivers', () => {
    const r = metaRow(metaHorizon(view, 20), false)!
    expect(r.pWin).toBe('46%')
    expect(r.t2).toBe('42%')
    expect(r.delta).toBe('+4.0 pp')
    expect(r.deltaTone).toBe('up')
    expect(r.er).toBe('+0.08R')
    expect(r.hidden).toBe(false)
    expect(r.decision).toBe('take')
    expect(r.decisionHidden).toBe(true)                    // the economic gate failed: hidden without the toggle
    expect(metaRow(metaHorizon(view, 20), true)!.decisionHidden).toBe(false)
    expect(r.size).toBe('0.81% of equity at risk · 1.4× the median taken setup')
    expect(r.drivers.map((d) => d.key)).toEqual(['c_rw_pct', 'm_risk', 'p_z_signed'])
    expect(r.drivers[0].tone).toBe('down')
    expect(r.drivers[0].text.startsWith('↓')).toBe(true)
  })
  it('hides experimental rows unless toggled; skip has no size', () => {
    expect(metaRow(metaHorizon(view, 40), false)!.hidden).toBe(true)
    const r40 = metaRow(metaHorizon(view, 40), true)!
    expect(r40.hidden).toBe(false)
    expect(r40.size).toBeNull()
    expect(metaRow(mh(20, { p_win: null }), true)).toBeNull()
    expect(thesisMetaRows(view, false).map((r) => r.horizon)).toEqual([20])
    expect(thesisMetaRows(view, true).map((r) => r.horizon)).toEqual([10, 20, 40])
    expect(thesisMetaRows(undefined, true)).toEqual([])
  })
  it('formats Kelly, pp, driver chips and the gate line', () => {
    expect(fmtKelly(0.02)).toBe('2.00% of equity at risk')
    expect(fmtKelly(null)).toBe('—')
    expect(fmtPp(-0.031)).toBe('−3.1 pp')
    const c = driverChip(drv[1])
    expect(c.tone).toBe('up')
    expect(c.title).toContain('raises')
    expect(c.title).toContain('+0.12')
    expect(metaGateLine(view, 20)).toBe('Calibration gate passed · take / skip + size failed (H = 20)')
    expect(metaGateLine(view, 10)).toContain('not validated')
  })
})
