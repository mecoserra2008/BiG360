/** Pure helpers for the Positioning (CFTC) pages (unit-tested in __tests__/positioning.test.ts). */
import type { PosDetail, PosFamily, PosHeatmap, PosMetric } from './types'
import { SOURCE_LABEL } from './types'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const clamp = (x: number, lo = -1, hi = 1) => Math.max(lo, Math.min(hi, x))
const DASH = '—'

/** Metric value -> signed intensity in [-1, 1] (0 = neutral). COT index / pct rank are centred on 0.5. */
export function intensity(v: number | null | undefined, metric: PosMetric): number | null {
  if (!fin(v)) return null
  switch (metric) {
    case 'cot_index_3y':
    case 'cot_index_1y':
    case 'pct_rank_3y':
      return clamp((v - 0.5) * 2)
    case 'z_3y':
    case 'z_1y':
      return clamp(v / 2.5)
    case 'net_pct_oi':
      return clamp(v / 0.4)
  }
}

/** Diverging cell background: --c2 (blue) for high / net-long readings, --c1 (orange) for low / net-short, neutral
 * surface at the midpoint (two hues + neutral, never a rainbow). */
export function cellBackground(v: number | null | undefined, metric: PosMetric): string {
  const t = intensity(v, metric)
  if (t === null) return 'var(--surface-2)'
  const pct = Math.round(Math.abs(t) * 62)
  return `color-mix(in srgb, ${t >= 0 ? 'var(--c2)' : 'var(--c1)'} ${pct}%, var(--surface))`
}

export function fmtMetric(v: number | null | undefined, metric: PosMetric): string {
  if (!fin(v)) return DASH
  if (metric === 'z_3y' || metric === 'z_1y') return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(1)}`
  if (metric === 'net_pct_oi') return `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(0)}%`
  return Math.round(v * 100).toString()
}

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
export function fmtDow(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`)
  if (Number.isNaN(d.getTime())) return DASH
  return `${DOW[d.getUTCDay()]} ${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`
}

/** "Data as of Tue 13 Oct 2026, released Fri 16 Oct 2026 · CFTC (official)". */
export function asOfLine(k: { report_date?: string | null; release_date?: string | null; release_estimated?: boolean | null }): string {
  if (!k.report_date) return `No COT report yet · ${SOURCE_LABEL}`
  const rel = k.release_date ? `, released ${fmtDow(k.release_date)}${k.release_estimated ? ' (scheduled)' : ''}` : ''
  return `Data as of ${fmtDow(k.report_date)}${rel} · ${SOURCE_LABEL}`
}

/** Heatmap rows grouped by asset class, in API order. */
export function groupRows(h: Pick<PosHeatmap, 'rows'>): { asset_class: string; rows: PosHeatmap['rows'] }[] {
  const out: { asset_class: string; rows: PosHeatmap['rows'] }[] = []
  for (const r of h.rows) {
    const g = out.find((x) => x.asset_class === r.asset_class)
    if (g) g.rows.push(r)
    else out.push({ asset_class: r.asset_class, rows: [r] })
  }
  return out
}

export function fmtContracts(v: number | null | undefined): string {
  if (!fin(v)) return DASH
  const a = Math.abs(v)
  const s = v < 0 ? '−' : ''
  if (a >= 1e6) return `${s}${(a / 1e6).toFixed(2)}M`
  if (a >= 1e4) return `${s}${(a / 1e3).toFixed(0)}k`
  if (a >= 1e3) return `${s}${(a / 1e3).toFixed(1)}k`
  return `${s}${a.toFixed(0)}`
}

/** Number of non-null points of a series (panels with no data are hidden). */
export const nPoints = (xs: (number | null | undefined)[]): number => xs.reduce<number>((n, x) => n + (fin(x) ? 1 : 0), 0)

/** Model Studio variable fields for a market: "<code>:<report_type>:<category>:<metric>". */
export function studioField(code: string, reportType: string, category: string, metric: string): string {
  return `${code}:${reportType}:${category}:${metric}`
}

export interface StudioVarSeed {
  name: string
  source: 'cftc' | 'prices'
  field: string
  title: string
  role: 'y' | 'x'
  ticker?: string
}

/** Variables for "Open in Model Studio": price log return (y, when mapped) vs headline COT index / net / spread. */
export function studioSeeds(d: Pick<PosDetail, 'market' | 'report_type' | 'headline_category' | 'price_instrument'>): StudioVarSeed[] {
  const code = d.market.market_code
  const rt = d.report_type
  const name = d.market.name
  const head = d.headline_category
  const seeds: StudioVarSeed[] = []
  const px = d.price_instrument
  if (px) seeds.push({ name: 'ret', source: 'prices', field: 'log_ret', title: `${px.ticker} log return`, role: 'y', ticker: px.ticker })
  seeds.push(
    { name: 'cot_index', source: 'cftc', field: studioField(code, rt, head, 'cot_index_3y'), title: `${name} ${head} COT index 3y`, role: px ? 'x' : 'y' },
    { name: 'net_chg', source: 'cftc', field: studioField(code, rt, head, 'chg_1w'), title: `${name} ${head} weekly net change`, role: 'x' },
    { name: 'spec_hedger', source: 'cftc', field: studioField(code, rt, 'spec_hedger', 'z_3y'), title: `${name} speculators − hedgers z 3y`, role: 'x' },
  )
  return seeds
}

/** Report families offered for a market (detail page selector), in display order. */
export function familyOptions(available: string[]): PosFamily[] {
  return (['legacy', 'disagg', 'tff', 'cit'] as PosFamily[]).filter((f) => available.includes(f))
}
