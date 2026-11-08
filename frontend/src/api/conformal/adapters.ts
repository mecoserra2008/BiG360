import type { CfBand, CfCoverageRow, CfEnvelope, CfFanInfo, CfHorizon, CfLevel, CfPeerGroupDetail, CfTouch, CfTouchLevel } from './types'

export const LEVELS: CfLevel[] = [80, 90, 95]
export const HORIZONS: CfHorizon[] = [1, 5, 10, 20]
export const VALIDATION_ROUTE = '/system/conformal'

/** Opacity / dash per calibration level for the chart cone (95% widest and faintest). */
export const LEVEL_STYLE: Record<CfLevel, { width: 1 | 2; dashed: boolean; alpha: number }> = {
  80: { width: 2, dashed: false, alpha: 0.95 },
  90: { width: 1, dashed: false, alpha: 0.8 },
  95: { width: 1, dashed: true, alpha: 0.65 },
}

export function band(env: CfEnvelope | undefined, h: number, lvl: number): CfBand | undefined {
  return env?.bands.find((b) => b.horizon === h && b.level === lvl)
}

export interface ConeLine {
  id: string
  level: CfLevel
  edge: 'lo' | 'hi'
  data: { time: string; value: number }[]
}

/**
 * Forward "cone" of the calibrated range: per level two lines (lower / upper edge) from the origin close to the band
 * edges at 1 / 5 / 10 / 20 sessions ahead (approximate weekday target dates). Times strictly increase.
 */
export function coneLines(env: CfEnvelope | undefined, levels: CfLevel[] = LEVELS, horizons: number[] = HORIZONS): ConeLine[] {
  if (!env || !env.as_of || env.last_close == null || !env.bands.length) return []
  const out: ConeLine[] = []
  for (const lvl of levels) {
    for (const edge of ['lo', 'hi'] as const) {
      const pts = [{ time: env.as_of, value: env.last_close }]
      for (const h of [...horizons].sort((a, b) => a - b)) {
        const b = band(env, h, lvl)
        if (!b || !b.target_date || b.target_date <= pts[pts.length - 1].time) continue
        pts.push({ time: b.target_date, value: edge === 'lo' ? b.lo : b.hi })
      }
      if (pts.length > 1) out.push({ id: `cf-${lvl}-${edge}`, level: lvl, edge, data: pts })
    }
  }
  return out
}

/** "<1%" below the calibrated tail (the API floors far levels at 0.5%), ">99%" near certain. */
export function fmtTouch(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return '—'
  if (p <= 0.005) return '<1%'
  if (p >= 0.995) return '>99%'
  return `${Math.round(p * 100)}%`
}

export function touchAt(l: CfTouchLevel | undefined, h: number): number | null {
  if (!l) return null
  return l.by_horizon.find((x) => x.horizon === h)?.p_touch ?? null
}

export function zoneTouch(t: CfTouch | undefined): Map<number, CfTouchLevel> {
  const m = new Map<number, CfTouchLevel>()
  for (const l of t?.levels ?? []) if (l.source === 'zone' && l.zone_id != null) m.set(l.zone_id, l)
  return m
}

/** Realised coverage for a horizon / level: rolling (the security's region, last ~250 origins) and walk-forward. */
export function coverageFor(env: CfEnvelope | undefined, h: number, lvl: number) {
  const rows = env?.coverage ?? []
  return {
    rolling: rows.find((r) => r.source === 'rolling' && r.horizon === h && r.level === lvl) ?? null,
    walkForward: rows.find((r) => r.source === 'walk_forward' && r.horizon === h && r.level === lvl) ?? null,
  }
}

/** Honest wording for a band: "calibrated to 90% over history (2010–2026 walk-forward: 89.8%)". Never "X% chance today". */
export function calibrationText(env: CfEnvelope | undefined, h: number, lvl: number): string {
  const c = coverageFor(env, h, lvl)
  const wf = c.walkForward?.coverage
  const rl = c.rolling?.coverage
  const parts = [`calibrated to ${lvl}% over history`]
  if (wf != null) parts.push(`2010–2026 walk-forward: ${(wf * 100).toFixed(1)}% realised`)
  if (rl != null) parts.push(`last year, ${c.rolling?.segment ?? 'region'}: ${(rl * 100).toFixed(1)}%`)
  return parts.length > 1 ? `${parts[0]} (${parts.slice(1).join('; ')})` : parts[0]
}

/** Thesis horizon -> sessions for the touch endpoint (> 20 sessions is an extrapolation; capped at 252). */
export function thesisSessions(h: string | null | undefined): { sessions: number; extrapolated: boolean } {
  const m: Record<string, number> = { '1M': 21, '3M': 63, '6M': 126, '12M': 252, '24M': 252, '36M+': 252 }
  const s = m[h ?? ''] ?? 63
  return { sessions: s, extrapolated: s > 20 }
}

export interface BandHints {
  entryLo: number
  entryHi: number
  stop: number
  target: number
  horizon: number
}

/**
 * Optional thesis hints from the calibrated range (20 sessions): long = entry between the 80% lower edge and the close,
 * stop below the 95% lower edge, target at the 95% upper edge; short mirrored. Hints only - the bands carry no direction.
 */
export function bandHints(env: CfEnvelope | undefined, direction: string, h: CfHorizon = 20): BandHints | null {
  const b80 = band(env, h, 80)
  const b95 = band(env, h, 95)
  if (!env || env.last_close == null || !b80 || !b95) return null
  const c = env.last_close
  if (direction === 'short') return { entryLo: c, entryHi: b80.hi, stop: b95.hi, target: b95.lo, horizon: h }
  return { entryLo: b80.lo, entryHi: c, stop: b95.lo, target: b95.hi, horizon: h }
}

export interface MemberRow {
  securityId: string
  ticker: string
  name: string | null
  r: number | null
  zMarg: number | null
  zCond: number | null
  contrib: number | null
  share: number | null
  isBreakaway: boolean
  isTop: boolean
}

export function memberRows(d: CfPeerGroupDetail | null | undefined): MemberRow[] {
  return (d?.members ?? [])
    .map((m) => ({
      securityId: m.security_id,
      ticker: m.ticker,
      name: m.name ?? null,
      r: m.r ?? null,
      zMarg: m.z_marg ?? null,
      zCond: m.z_cond ?? null,
      contrib: m.contrib ?? null,
      share: m.contrib_share ?? null,
      isBreakaway: !!m.is_breakaway,
      isTop: !!m.is_top,
    }))
    .sort((a, b) => (b.contrib ?? -Infinity) - (a.contrib ?? -Infinity))
}

export function pctTone(p: number | null | undefined): 'down' | 'warn' | 'neutral' {
  if (p == null) return 'neutral'
  return p >= 0.95 ? 'down' : p >= 0.9 ? 'warn' : 'neutral'
}

export function scoreHistory(d: CfPeerGroupDetail | null | undefined): number[] {
  return (d?.history ?? []).map((h) => h.score_pct).filter((x): x is number => x != null && Number.isFinite(x))
}

/** weights=SID:w,... for the pair-band endpoint (zero weights dropped). */
export function weightsParam(rows: { securityId: string; weight: number }[]): string {
  return rows
    .filter((r) => r.securityId && Number.isFinite(r.weight) && r.weight !== 0)
    .map((r) => `${r.securityId}:${+r.weight.toPrecision(8)}`)
    .join(',')
}

export const VERDICT: Record<string, { label: string; tone: 'up' | 'warn' | 'down' | 'neutral' }> = {
  pass: { label: 'PASS', tone: 'up' },
  pass_coverage: { label: 'PASS (coverage)', tone: 'up' },
  mixed: { label: 'MIXED', tone: 'warn' },
  fail_sharpness: { label: 'FAIL (sharpness)', tone: 'down' },
  fail: { label: 'FAIL', tone: 'down' },
}

/** Headline coverage rows: rolling envelope marginal bands, next bar, peer joint (+ walk-forward counterparts). */
export function headlineCoverage(rows: CfCoverageRow[], level = 90): CfCoverageRow[] {
  const keep = (r: CfCoverageRow) =>
    r.level === level &&
    r.segment_type === 'overall' &&
    ((r.application === 'envelope' && r.filter === 'ewma' && r.horizon.startsWith('m')) ||
      (r.application === 'envelope_wf' && r.filter === 'm_ewma') ||
      (r.application === 'nextbar' && r.horizon === 'joint' && r.filter.endsWith('+aci')) ||
      (r.application === 'peer' && r.horizon === 'joint') ||
      (r.application === 'peer_wf' && r.filter === 'gcn_gru+aci'))
  return rows.filter(keep)
}

export const APP_LABEL: Record<string, string> = {
  envelope: 'Calibrated range (rolling)',
  envelope_wf: 'Calibrated range (walk-forward)',
  nextbar: 'Next bar (experimental, rolling)',
  nextbar_wf: 'Next bar (walk-forward)',
  peer: 'Peer groups (rolling)',
  peer_wf: 'Peer groups (walk-forward)',
}

/* ───────────── fan (Technicals v2 T1): calibrated band at every horizon 1..20 ───────────── */

export interface FanGeomPoint {
  h: number
  time: string
  lo: Record<CfLevel, number>
  hi: Record<CfLevel, number>
  earnings: boolean
}

export interface FanGeom {
  origin: { time: string; value: number }
  points: FanGeomPoint[]
  /** the drawn envelope differs from the raw calibrated bands somewhere (non-narrowing / nesting enforced) */
  adjusted: boolean
  earnings: { time: string; h: number; session: string | null; source: string } | null
  source: CfFanInfo['source']
}

/**
 * Chart geometry of the fan from /conformal/envelope `fan`: per level the lower / upper edge at every horizon, with the
 * display envelope made non-narrowing in h (running max of the half-widths) and strictly nested (80 ⊂ 90 ⊂ 95). Both
 * adjustments only widen a band (conservative). Points are in time order; the origin is the envelope's last close.
 */
export function fanGeometry(env: CfEnvelope | undefined): FanGeom | null {
  const fan = env?.fan ?? []
  if (!env || !env.as_of || env.last_close == null || !fan.length) return null
  const c = env.last_close
  const pts = [...fan].sort((a, b) => a.h - b.h).filter((p) => p.date > env.as_of!)
  if (!pts.length) return null
  const up: Record<CfLevel, number> = { 80: 0, 90: 0, 95: 0 }
  const dn: Record<CfLevel, number> = { 80: 0, 90: 0, 95: 0 }
  let adjusted = false
  const out: FanGeomPoint[] = []
  for (const p of pts) {
    const lo = { 80: p.lo_80, 90: p.lo_90, 95: p.lo_95 } as Record<CfLevel, number>
    const hi = { 80: p.hi_80, 90: p.hi_90, 95: p.hi_95 } as Record<CfLevel, number>
    let prevUp = 0
    let prevDn = 0
    for (const lvl of LEVELS) {
      // log half-widths above / below the centre, non-narrowing in h and nested across levels
      const u = Math.max(Math.log(hi[lvl] / c), up[lvl], prevUp)
      const d = Math.max(Math.log(c / lo[lvl]), dn[lvl], prevDn)
      if (u > Math.log(hi[lvl] / c) + 1e-12 || d > Math.log(c / lo[lvl]) + 1e-12) adjusted = true
      up[lvl] = u
      dn[lvl] = d
      prevUp = u
      prevDn = d
      hi[lvl] = c * Math.exp(u)
      lo[lvl] = c * Math.exp(-d)
    }
    out.push({ h: p.h, time: p.date, lo, hi, earnings: !!p.earnings })
  }
  const e = env.fan_info?.earnings
  return {
    origin: { time: env.as_of, value: c },
    points: out,
    adjusted,
    earnings: e ? { time: out.find((p) => p.h === e.h)?.time ?? e.date, h: e.h, session: e.session ?? null, source: e.source } : null,
    source: env.fan_info?.source ?? 'none',
  }
}

export function fanAt(g: FanGeom | null, time: string): FanGeomPoint | undefined {
  return g?.points.find((p) => p.time === time)
}

/** "by Nov 6: 80% 318–371 · 90% 312–378 · 95% 306–385" (prices formatted by `fmt`). */
export function fanTooltip(p: FanGeomPoint | undefined, fmt: (v: number) => string, fmtDay: (iso: string) => string): string | null {
  if (!p) return null
  return `by ${fmtDay(p.time)}: ${LEVELS.map((l) => `${l}% ${fmt(p.lo[l])}–${fmt(p.hi[l])}`).join(' · ')}`
}

export interface FanStripCell {
  h: number
  time: string
  lo: number
  hi: number
  loPct: number
  hiPct: number
}

/** Compact strip under the chart: the (drawn) band at 1 / 5 / 10 / 20 sessions for one level. */
export function fanStrip(g: FanGeom | null, level: CfLevel, horizons: number[] = HORIZONS): FanStripCell[] {
  if (!g) return []
  const c = g.origin.value
  return horizons
    .map((h) => g.points.find((p) => p.h === h))
    .filter((p): p is FanGeomPoint => !!p)
    .map((p) => ({ h: p.h, time: p.time, lo: p.lo[level], hi: p.hi[level], loPct: p.lo[level] / c - 1, hiPct: p.hi[level] / c - 1 }))
}

export interface ZoneLike {
  id: string
  low: number
  high: number
  kind: 'support' | 'resistance'
  strength: number
  rank: number
}

/** The n strongest zones (strength desc, then rank). */
export function topZones<Z extends ZoneLike>(zones: Z[], n = 3): Z[] {
  return [...zones].sort((a, b) => b.strength - a.strength || a.rank - b.rank).slice(0, n)
}

export interface ZoneBadge {
  zoneId: string
  price: number
  p: number | null
  text: string
  kind: 'support' | 'resistance'
}

/**
 * Badges "touch 20d: 45%" for the zones that lie (at least partly) within the 95% fan at its widest horizon, placed at
 * the zone's mid price. Inside-zone = "in zone".
 */
export function zoneBadges<Z extends ZoneLike>(zones: Z[], touch: CfTouch | undefined, g: FanGeom | null, h: number): ZoneBadge[] {
  if (!g || !g.points.length) return []
  const last = g.points[g.points.length - 1]
  const lo = last.lo[95]
  const hi = last.hi[95]
  const zt = zoneTouch(touch)
  const out: ZoneBadge[] = []
  for (const z of zones) {
    if (z.high < lo || z.low > hi) continue
    const lv = zt.get(Number(String(z.id).replace(/^z/, '')))
    const p = lv?.inside ? 1 : touchAt(lv, h)
    const txt = lv?.inside ? 'in zone' : `touch ${h}d: ${fmtTouch(p)}`
    out.push({ zoneId: z.id, price: (Math.max(z.low, lo) + Math.min(z.high, hi)) / 2, p, text: txt, kind: z.kind })
  }
  return out
}
