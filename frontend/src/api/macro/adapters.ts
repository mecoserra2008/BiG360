/** Pure helpers for Macro v2 payloads (unit-tested in __tests__/macro.test.ts). */
import type { CalendarDay, MCalendarItem, MCell, MHeatmap, MNowcastTarget, MPoint, MRecession, MRecessionRow, MSeriesData, NowcastTrackRow, RevisionOverlay } from './types'

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/**
 * z-score oriented so that positive = economically good for this indicator
 * (good_direction −1 for inflation / unemployment …, +1 for growth; 0 / null = neutral, e.g. rates).
 */
export function orientedZ(c: Pick<MCell, 'z_10y' | 'good_direction'>): number | null {
  const z = num(c.z_10y)
  if (z === null) return null
  const g = num(c.good_direction)
  return g ? z * Math.sign(g) : null
}

/** Colour bucket −3…3 for an oriented z (null = neutral, no judgement). */
export function zBucket(oz: number | null): number | null {
  if (oz === null) return null
  return Math.max(-3, Math.min(3, Math.round(oz * 1.5)))
}

/** heatmap → lookup by "CC|indicator". */
export function cellIndex(h: Pick<MHeatmap, 'cells'>): Map<string, MCell> {
  return new Map(h.cells.map((c) => [`${c.country}|${c.indicator}`, c]))
}

/** Calendar filter + group by local release date. */
export function groupCalendar(
  items: MCalendarItem[],
  opts: { countries?: string[]; maxTier?: number; when?: 'upcoming' | 'past' | 'all'; today: string; kinds?: ('data' | 'policy_meeting')[] },
): CalendarDay[] {
  const set = opts.countries?.length ? new Set(opts.countries) : null
  const rows = items
    .filter((i) => !set || set.has(i.country))
    .filter((i) => i.importance <= (opts.maxTier ?? 3))
    .filter((i) => !opts.kinds || opts.kinds.includes(i.kind))
    .filter((i) => (opts.when === 'upcoming' ? i.release_date >= opts.today : opts.when === 'past' ? i.release_date < opts.today : true))
    .sort((a, b) => (a.scheduled_at < b.scheduled_at ? -1 : a.scheduled_at > b.scheduled_at ? 1 : a.importance - b.importance))
  const days = new Map<string, MCalendarItem[]>()
  for (const r of rows) {
    const d = r.release_date
    if (!days.has(d)) days.set(d, [])
    days.get(d)?.push(r)
  }
  const out = [...days.entries()].map(([date, its]) => ({ date, items: its }))
  return opts.when === 'past' ? out.reverse() : out
}

/** Surprise colour bucket: signed z (economic direction) −3…3. */
export function surpriseBucket(signedZ: number | null | undefined): number | null {
  const z = num(signedZ)
  return z === null ? null : Math.max(-3, Math.min(3, Math.round(z)))
}

/** Nowcast track record: latest pre-release nowcast of ours vs the official benchmarks against the first print. */
export function nowcastTrack(targets: MNowcastTarget[]): NowcastTrackRow[] {
  const last = (t: MNowcastTarget, pred: (id: string) => boolean): number | null => {
    const m = t.models.find((x) => pred(x.model_id))
    if (!m) return null
    return num(m.latest?.nowcast) ?? num(m.history[m.history.length - 1]?.nowcast)
  }
  return targets.map((t) => {
    const actual = num(t.actual)
    const ours = last(t, (id) => id.startsWith('dfm'))
    const gdpnow = last(t, (id) => id.includes('gdpnow'))
    const nyfed = last(t, (id) => id.includes('nyfed'))
    const err = (v: number | null) => (actual !== null && v !== null ? v - actual : null)
    return { target: t.target_label, actual, ours, gdpnow, nyfed, errOurs: err(ours), errGdpnow: err(gdpnow), errNyfed: err(nyfed) }
  })
}

/** Recession panel → method → country → row. */
export function recessionGrid(r: MRecession): { methods: MRecession['methods']; countries: string[]; get: (country: string, method: string) => MRecessionRow | undefined } {
  const map = new Map<string, MRecessionRow>()
  for (const c of r.countries) for (const row of c.rows) map.set(`${c.country}|${row.method}`, row)
  return { methods: r.methods, countries: r.countries.map((c) => c.country), get: (c, m) => map.get(`${c}|${m}`) }
}

/** First release vs latest revised, aligned by period (transformed values when available). */
export function revisionOverlay(v: MSeriesData['vintages'], fromDate?: string): RevisionOverlay | null {
  if (!v || !v.first_release.length) return null
  const transformed = v.first_release.some((p) => num(p.value_tf) !== null)
  const pick = (p: { value: number | null; value_tf?: number | null }) => (transformed ? num(p.value_tf) : num(p.value))
  const latest = new Map(v.latest.map((p) => [p.date, pick(p)]))
  const rows = v.first_release.filter((p) => !fromDate || p.date >= fromDate)
  return { dates: rows.map((p) => p.date), first: rows.map(pick), latest: rows.map((p) => latest.get(p.date) ?? null), transformed }
}

/** Contiguous periods where a 0/1 indicator (e.g. fred:USREC) is 1 → shading bands. */
export function recessionBands(points: MPoint[]): { start: string; end: string }[] {
  const out: { start: string; end: string }[] = []
  let start: string | null = null
  let prev: string | null = null
  for (const p of points) {
    if (num(p.value) === 1) {
      if (start === null) start = p.date
    } else if (start !== null) {
      out.push({ start, end: prev ?? start })
      start = null
    }
    prev = p.date
  }
  if (start !== null) out.push({ start, end: prev ?? start })
  return out
}

/** Studio frequency for a macro series (Studio datasets are D / W / M / Q). */
export function studioFrequency(freq: string | null | undefined): 'D' | 'W' | 'M' | 'Q' {
  return freq === 'Q' || freq === 'A' ? 'Q' : freq === 'W' ? 'W' : 'M'
}

export const fmtSigned = (v: number | null, digits = 2): string => (v === null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(digits)}`)
