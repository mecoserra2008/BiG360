import { describe, expect, it } from 'vitest'
import { LEVELS, fanAt, fanGeometry, fanStrip, fanTooltip, topZones, zoneBadges } from '../adapters'
import type { CfEnvelope, CfFanPoint, CfLevel, CfTouch } from '../types'

const Z: Record<CfLevel, number> = { 80: 1.28, 90: 1.65, 95: 1.96 }
const day = (h: number) => {
  const d = new Date(Date.UTC(2026, 9, 9))
  let n = 0
  while (n < h) {
    d.setUTCDate(d.getUTCDate() + 1)
    if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6) n++
  }
  return d.toISOString().slice(0, 10)
}
/** fan with sigma 1%/day; `wobble` makes h = 12 narrower than h = 11 and the 90% band wider than the 95% one at h = 3 */
function mkFan(wobble = false): CfFanPoint[] {
  return Array.from({ length: 20 }, (_, i) => {
    const h = i + 1
    const p: Record<string, number | string | boolean> = { h, date: day(h), event_pool: h >= 7, earnings: h === 7 }
    for (const l of LEVELS) {
      let hw = 0.01 * Math.sqrt(h) * Z[l]
      if (wobble && h === 12) hw *= 0.9
      if (wobble && h === 3 && l === 90) hw *= 1.3
      p[`lo_${l}`] = 100 * Math.exp(-hw)
      p[`hi_${l}`] = 100 * Math.exp(hw)
    }
    return p as unknown as CfFanPoint
  })
}
const mkEnv = (fan: CfFanPoint[]) =>
  ({
    as_of: '2026-10-09',
    last_close: 100,
    bands: [],
    fan,
    fan_info: {
      source: 'calibrated',
      calendar: 'XNYS',
      horizons: 20,
      monotone: true,
      note: 'x',
      earnings: { date: day(7), h: 7, session: 'post_market', date_source: 'provider_estimate', source: 'earnings_calendar' },
    },
  }) as unknown as CfEnvelope

function checkGeometry(g: NonNullable<ReturnType<typeof fanGeometry>>) {
  for (const p of g.points) {
    // nested levels: 80 inside 90 inside 95, around the last close
    expect(p.lo[95]).toBeLessThanOrEqual(p.lo[90])
    expect(p.lo[90]).toBeLessThanOrEqual(p.lo[80])
    expect(p.lo[80]).toBeLessThan(100)
    expect(p.hi[80]).toBeGreaterThan(100)
    expect(p.hi[80]).toBeLessThanOrEqual(p.hi[90])
    expect(p.hi[90]).toBeLessThanOrEqual(p.hi[95])
  }
  // monotone widening: no level ever narrows with the horizon
  for (let i = 1; i < g.points.length; i++) {
    for (const l of LEVELS) {
      expect(g.points[i].hi[l]).toBeGreaterThanOrEqual(g.points[i - 1].hi[l])
      expect(g.points[i].lo[l]).toBeLessThanOrEqual(g.points[i - 1].lo[l])
    }
    expect(g.points[i].time > g.points[i - 1].time).toBe(true)
  }
}

describe('fan geometry', () => {
  it('keeps calibrated bands unchanged when already nested and widening', () => {
    const g = fanGeometry(mkEnv(mkFan()))!
    expect(g.points).toHaveLength(20)
    expect(g.adjusted).toBe(false)
    expect(g.origin).toEqual({ time: '2026-10-09', value: 100 })
    checkGeometry(g)
    expect(g.points[4].hi[90]).toBeCloseTo(100 * Math.exp(0.01 * Math.sqrt(5) * 1.65), 8)
    expect(g.earnings).toEqual({ time: day(7), h: 7, session: 'post_market', source: 'earnings_calendar' })
    expect(g.points.filter((p) => p.earnings).map((p) => p.h)).toEqual([7])
  })
  it('enforces monotone widening and nesting (only ever wider)', () => {
    const raw = mkFan(true)
    const g = fanGeometry(mkEnv(raw))!
    expect(g.adjusted).toBe(true)
    checkGeometry(g)
    // h = 12 lifted to h = 11; the 95% band at h = 3 lifted to the (wider) 90% one
    expect(g.points[11].hi[80]).toBeCloseTo(g.points[10].hi[80], 10)
    expect(g.points[2].hi[95]).toBeCloseTo(raw[2].hi_90, 10)
    for (let i = 0; i < 20; i++) {
      const r = raw[i] as unknown as Record<string, number>
      for (const l of LEVELS) {
        expect(g.points[i].hi[l]).toBeGreaterThanOrEqual(r[`hi_${l}`] - 1e-9)
        expect(g.points[i].lo[l]).toBeLessThanOrEqual(r[`lo_${l}`] + 1e-9)
      }
    }
  })
  it('empty / missing fan gives null', () => {
    expect(fanGeometry(undefined)).toBeNull()
    expect(fanGeometry(mkEnv([]))).toBeNull()
  })
  it('strip, tooltip and lookup', () => {
    const g = fanGeometry(mkEnv(mkFan()))!
    const s = fanStrip(g, 90)
    expect(s.map((c) => c.h)).toEqual([1, 5, 10, 20])
    expect(s[3].hiPct).toBeGreaterThan(s[2].hiPct)
    expect(s[0].loPct).toBeLessThan(0)
    const p = fanAt(g, day(10))!
    expect(p.h).toBe(10)
    const txt = fanTooltip(p, (v) => v.toFixed(1), (d) => `D${d.slice(8)}`)!
    expect(txt.startsWith(`by D${day(10).slice(8)}: 80% `)).toBe(true)
    expect(txt).toContain(' · 90% ')
    expect(txt).toContain(' · 95% ')
    expect(txt.toLowerCase()).not.toContain('chance')
    expect(fanTooltip(undefined, String, String)).toBeNull()
  })
})

describe('zones on the fan', () => {
  const zones = [
    { id: 'z1', low: 99, high: 101, kind: 'support' as const, strength: 0.76, rank: 1 },
    { id: 'z2', low: 104, high: 106, kind: 'resistance' as const, strength: 0.3, rank: 4 },
    { id: 'z3', low: 150, high: 160, kind: 'resistance' as const, strength: 0.9, rank: 2 },
    { id: 'z4', low: 90, high: 92, kind: 'support' as const, strength: 0.5, rank: 3 },
  ]
  const touch = {
    levels: [
      { source: 'zone', zone_id: 1, inside: true, price: 100, side: null, p_touch: 1, by_horizon: [] },
      { source: 'zone', zone_id: 2, inside: false, price: 104, side: 'up', p_touch: 0.45, by_horizon: [{ horizon: 20, p_touch: 0.45 }, { horizon: 5, p_touch: 0.2 }] },
      { source: 'zone', zone_id: 4, inside: false, price: 92, side: 'down', p_touch: 0.3, by_horizon: [{ horizon: 20, p_touch: 0.3 }] },
    ],
  } as unknown as CfTouch
  it('top zones by strength', () => {
    expect(topZones(zones, 3).map((z) => z.id)).toEqual(['z3', 'z1', 'z4'])
  })
  it('badges only for zones inside the 95% fan, at the selected horizon', () => {
    const g = fanGeometry(mkEnv(mkFan()))!
    const b = zoneBadges(zones, touch, g, 20)
    expect(b.map((x) => x.zoneId)).toEqual(['z1', 'z2', 'z4']) // z3 (150-160) lies outside the 95% fan
    expect(b[0].text).toBe('in zone')
    expect(b[1].text).toBe('touch 20d: 45%')
    expect(zoneBadges(zones, touch, g, 5)[1].text).toBe('touch 5d: 20%')
    expect(zoneBadges(zones, touch, null, 20)).toEqual([])
  })
})
