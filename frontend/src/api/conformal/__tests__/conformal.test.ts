import { describe, expect, it } from 'vitest'
import { band, bandHints, calibrationText, coneLines, coverageFor, fmtTouch, headlineCoverage, memberRows, pctTone, scoreHistory, thesisSessions, touchAt, weightsParam, zoneTouch } from '../adapters'
import type { CfCoverageRow, CfEnvelope, CfPeerGroupDetail, CfTouch, CfTouchLevel } from '../types'

const mk = (h: number, lvl: number, hw: number, date: string) => ({ horizon: h, level: lvl, lo: 100 * Math.exp(-hw), hi: 100 * Math.exp(hw), half_width_log: hw, lo_pct: null, hi_pct: null, target_date: date })
const dates: Record<number, string> = { 1: '2026-10-12', 5: '2026-10-16', 10: '2026-10-23', 20: '2026-11-06' }
const env = {
  security: { security_id: 'S1', issuer_id: 'I1', ticker: 'AAA', name: 'Alpha', region: 'US', country: 'US', currency: 'USD' },
  as_of: '2026-10-09',
  last_close: 100,
  model_based: true,
  horizons: [1, 5, 10, 20],
  levels: [80, 90, 95],
  bands: [1, 5, 10, 20].flatMap((h) => [80, 90, 95].map((l) => mk(h, l, 0.01 * Math.sqrt(h) * (l === 80 ? 1.3 : l === 90 ? 1.65 : 1.96), dates[h]))),
  nextbar: { experimental: true, note: 'x', bands: [] },
  coverage: [
    { source: 'walk_forward', horizon: 20, level: 90, coverage: 0.896, n: 1000, status: 'ok' },
    { source: 'rolling', segment: 'US', horizon: 20, level: 90, coverage: 0.9021, n: 1000, status: 'ok' },
  ],
  labels: {},
  validation_doc: 'docs/CONFORMAL_VALIDATION.md',
  data_status: 'ok',
} as unknown as CfEnvelope

describe('calibrated range', () => {
  it('builds a forward cone with increasing times and nested levels', () => {
    const lines = coneLines(env)
    expect(lines).toHaveLength(6)
    const hi95 = lines.find((l) => l.id === 'cf-95-hi')!
    expect(hi95.data.map((p) => p.time)).toEqual(['2026-10-09', '2026-10-12', '2026-10-16', '2026-10-23', '2026-11-06'])
    expect(hi95.data[0].value).toBe(100)
    const hi80 = lines.find((l) => l.id === 'cf-80-hi')!
    expect(hi80.data[4].value).toBeLessThan(hi95.data[4].value)
    expect(coneLines(env, [90], [20])).toHaveLength(2)
    expect(coneLines(undefined)).toEqual([])
  })
  it('wording is calibrated-over-history, never chance today', () => {
    const t = calibrationText(env, 20, 90)
    expect(t).toContain('calibrated to 90% over history')
    expect(t).toContain('89.6%')
    expect(t).toContain('US: 90.2%')
    expect(t.toLowerCase()).not.toContain('chance')
    expect(coverageFor(env, 5, 90).rolling).toBeNull()
    expect(band(env, 5, 95)!.hi).toBeGreaterThan(band(env, 5, 90)!.hi)
  })
  it('thesis hints follow the direction', () => {
    const long = bandHints(env, 'long')!
    expect(long.entryLo).toBeLessThan(long.entryHi)
    expect(long.stop).toBeLessThan(long.entryLo)
    expect(long.target).toBeGreaterThan(100)
    const short = bandHints(env, 'short')!
    expect(short.stop).toBeGreaterThan(short.entryHi)
    expect(short.target).toBeLessThan(100)
    expect(bandHints(undefined, 'long')).toBeNull()
    expect(thesisSessions('3M')).toEqual({ sessions: 63, extrapolated: true })
    expect(thesisSessions('1M').sessions).toBe(21)
  })
})

describe('touch', () => {
  const lvl = (p: Partial<CfTouchLevel>): CfTouchLevel => ({ source: 'zone', price: 98, side: 'down', p_touch: 0.4, by_horizon: [{ horizon: 1, p_touch: 0.05 }, { horizon: 20, p_touch: 0.6 }], ...p }) as CfTouchLevel
  it('formats tails honestly and indexes zones', () => {
    expect(fmtTouch(0.004)).toBe('<1%')
    expect(fmtTouch(0.999)).toBe('>99%')
    expect(fmtTouch(0.4249)).toBe('42%')
    expect(fmtTouch(null)).toBe('—')
    const t = { levels: [lvl({ zone_id: 3 }), lvl({ source: 'level', zone_id: null })] } as unknown as CfTouch
    const m = zoneTouch(t)
    expect([...m.keys()]).toEqual([3])
    expect(touchAt(m.get(3), 20)).toBe(0.6)
    expect(touchAt(m.get(3), 5)).toBeNull()
  })
})

describe('peer groups', () => {
  const d = {
    group_id: 'G', label: 'g', kind: 'industry', region: 'US', n_members: 3,
    history: [{ date: '2026-10-08', score_pct: 0.5 }, { date: '2026-10-09', score_pct: 0.97 }, { date: '2026-10-10', score_pct: null }],
    members: [
      { security_id: 'A', ticker: 'A', contrib: 1.0, contrib_share: 0.1, is_breakaway: false, is_top: false },
      { security_id: 'B', ticker: 'B', contrib: 6.0, contrib_share: 0.6, is_breakaway: true, is_top: true, z_cond: 3.1 },
    ],
    definition: { version: 'v', kind: 'industry', label: 'g', region: 'US', members: [] },
  } as unknown as CfPeerGroupDetail
  it('sorts members by contribution and builds the sparkline', () => {
    const rows = memberRows(d)
    expect(rows.map((r) => r.securityId)).toEqual(['B', 'A'])
    expect(rows[0].isBreakaway && rows[0].zCond === 3.1).toBe(true)
    expect(scoreHistory(d)).toEqual([0.5, 0.97])
    expect(pctTone(0.97)).toBe('down')
    expect(pctTone(0.92)).toBe('warn')
    expect(pctTone(null)).toBe('neutral')
  })
  it('weights parameter and headline coverage', () => {
    expect(weightsParam([{ securityId: 'A', weight: 0.5 }, { securityId: 'B', weight: 0 }, { securityId: 'C', weight: -1 }])).toBe('A:0.5,C:-1')
    const r = (p: Partial<CfCoverageRow>): CfCoverageRow => ({ application: 'peer', filter: 'gcn_gru', segment_type: 'overall', segment: 'all', horizon: 'joint', level: 90, n: 1, status: 'ok', ...p }) as CfCoverageRow
    const rows = [r({}), r({ level: 80 }), r({ application: 'envelope', filter: 'ewma', horizon: 'm5' }), r({ application: 'envelope', filter: 'ewma', horizon: '5' }), r({ segment_type: 'region' })]
    expect(headlineCoverage(rows).map((x) => `${x.application}:${x.horizon}`)).toEqual(['peer:joint', 'envelope:m5'])
  })
})
