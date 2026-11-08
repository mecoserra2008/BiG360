/** Pure helpers for the Cross-asset pages (unit-tested in __tests__/crossasset.test.ts). */
import type { XaCockpit, XaCockpitRow, XaCoverage, XaCurveSnapshot, XaPositioning, XaRisk, XaSignalDef } from './types'
import { CLASS_LABEL, COT_CATEGORY_LABEL } from './types'
import type { CTransform } from '../studio/types'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const clamp = (x: number, lo = -1, hi = 1) => Math.max(lo, Math.min(hi, x))
export const DASH = '—'
const sgn = (v: number) => (v > 0 ? '+' : v < 0 ? '−' : '')

/** Cell colour intensity in [-1, 1]: z-scores saturate at ±2.5σ; bounded scores (trend signal) are already in [-1, 1]. */
export function intensity(v: number | null | undefined, scale: XaSignalDef['scale']): number | null {
  if (!fin(v)) return null
  return scale === 'score' ? clamp(v) : clamp(v / 2.5)
}

/** Diverging background: --c2 (blue) for high readings, --c1 (orange) for low, neutral surface at 0 (two hues, no rainbow). */
export function cellBackground(v: number | null | undefined, scale: XaSignalDef['scale']): string {
  const t = intensity(v, scale)
  if (t === null) return 'var(--surface-2)'
  const pct = Math.round(Math.abs(t) * 62)
  return `color-mix(in srgb, ${t >= 0 ? 'var(--c2)' : 'var(--c1)'} ${pct}%, var(--surface))`
}

export function fmtPctS(v: number | null | undefined, d = 1): string {
  return fin(v) ? `${sgn(v)}${Math.abs(v * 100).toFixed(d)}%` : DASH
}

/** One decimal with a sign; values that round to zero print without a sign (never "−0.0"). */
function signed1(v: number): string {
  const r = Math.round(v * 10) / 10
  return `${sgn(r)}${Math.abs(r).toFixed(1)}`
}

export function fmtZ(v: number | null | undefined): string {
  return fin(v) ? `${signed1(v)}σ` : DASH
}

/** Raw value of a heatmap signal, formatted by its meaning (value depends on the asset's value_method). */
export function fmtSignalRaw(id: string, v: number | null | undefined, row?: Pick<XaCockpitRow, 'value_method'>): string {
  if (!fin(v)) return DASH
  switch (id) {
    case 'trend':
      return `${sgn(Math.round(v * 100))}${Math.abs(v).toFixed(2)}`
    case 'momentum':
    case 'carry':
    case 'curve':
      return fmtPctS(v)
    case 'vol':
      return `${(v * 100).toFixed(1)}%`
    case 'iv_rv':
      return `${v.toFixed(2)}×`
    case 'positioning':
      return Math.round(v * 100).toString()
    case 'value':
      return row?.value_method === 'real_yield' || row?.value_method === 'oas_level' ? `${v.toFixed(2)}%` : fmtPctS(v)
    default:
      return v.toFixed(2)
  }
}

/** Heatmap cell text: z-score when the signal is coloured by z, else the bounded score. */
export function cellText(sig: XaSignalDef, cell: { z: number | null; raw: number | null }, row?: Pick<XaCockpitRow, 'value_method'>): string {
  if (sig.scale === 'score') return fmtSignalRaw(sig.id, cell.raw, row)
  if (sig.id === 'positioning' && !fin(cell.z) && fin(cell.raw)) return fmtSignalRaw(sig.id, cell.raw, row)
  return fin(cell.z) ? signed1(cell.z) : DASH
}

/** Rows grouped by asset class, in API (registry) order. */
export function groupRows(rows: XaCockpit['rows']): { asset_class: string; label: string; rows: XaCockpit['rows'] }[] {
  const out: { asset_class: string; label: string; rows: XaCockpit['rows'] }[] = []
  for (const r of rows) {
    const g = out.find((x) => x.asset_class === r.asset_class)
    if (g) g.rows.push(r)
    else out.push({ asset_class: r.asset_class, label: CLASS_LABEL[r.asset_class] ?? r.asset_class, rows: [r] })
  }
  return out
}

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
export function fmtDay(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(`${iso.slice(0, 10)}T00:00:00Z`)
  return Number.isNaN(d.getTime()) ? DASH : `${d.getUTCDate()} ${MON[d.getUTCMonth()]} ${d.getUTCFullYear()}`
}

/** "CFTC (official), as of report date 6 Oct 2026 (released 9 Oct 2026) · leveraged funds". */
export function positioningLine(p: Pick<XaPositioning, 'report_date' | 'release_date' | 'category' | 'inverted'> | null | undefined): string {
  if (!p || !p.report_date) return 'No CFTC positioning for this asset'
  const cat = COT_CATEGORY_LABEL[p.category] ?? p.category
  return `CFTC (official), as of report date ${fmtDay(p.report_date)}${p.release_date ? ` (released ${fmtDay(p.release_date)})` : ''} · ${cat}${p.inverted ? ' · inverted (quoted against the future)' : ''}`
}

/** Curve snapshot -> [months to expiry, price] points (nearest first), the contract kept for tooltips. */
export function curvePoints(s: Pick<XaCurveSnapshot, 'points'>): { x: number; y: number; contract: string; expiry: string }[] {
  return s.points
    .filter((p) => fin(p.close) && fin(p.days_to_expiry))
    .map((p) => ({ x: Math.round((p.days_to_expiry / 30.4375) * 100) / 100, y: p.close as number, contract: p.contract, expiry: p.expiry }))
    .sort((a, b) => a.x - b.x)
}

export type CoverageTone = 'up' | 'warn' | 'down' | 'neutral'
export const COVERAGE_TONE: Record<XaCoverage['status'], CoverageTone> = { served: 'up', unchecked: 'warn', missing: 'down', expired: 'neutral' }

/** Current listing first (served / missing / unchecked by contract month), expired ones last (most recent first). */
export function coverageOrder(rows: XaCoverage[]): XaCoverage[] {
  const live = rows.filter((r) => r.status !== 'expired').sort((a, b) => a.contract_month.localeCompare(b.contract_month))
  const old = rows.filter((r) => r.status === 'expired').sort((a, b) => b.contract_month.localeCompare(a.contract_month))
  return [...live, ...old]
}

/** Contiguous runs of the same curve shape (for shaded contango / backwardation bands on the history chart). */
export function shapeRuns(dates: string[], shapes: (string | null)[]): { shape: string; from: string; to: string; n: number }[] {
  const out: { shape: string; from: string; to: string; n: number }[] = []
  for (let i = 0; i < dates.length; i++) {
    const s = shapes[i]
    if (!s) continue
    const last = out[out.length - 1]
    if (last && last.shape === s && last.to === dates[i - 1]) {
      last.to = dates[i]
      last.n += 1
    } else out.push({ shape: s, from: dates[i], to: dates[i], n: 1 })
  }
  return out
}

/** Share of dates in contango / backwardation / flat. */
export function shapeShares(shapes: (string | null)[]): { contango: number; backwardation: number; flat: number; n: number } {
  const n = shapes.filter(Boolean).length
  const c = (k: string) => (n ? shapes.filter((s) => s === k).length / n : 0)
  return { contango: c('contango'), backwardation: c('backwardation'), flat: c('flat'), n }
}

/** Correlation matrix -> ECharts heatmap triples [x, y, value] (y reversed so the first asset is on top). */
export function matrixCells(values: (number | null)[][]): [number, number, number | null][] {
  const n = values.length
  const out: [number, number, number | null][] = []
  for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) out.push([j, n - 1 - i, fin(values[i][j]) ? values[i][j] : null])
  return out
}

/** Mean off-diagonal correlation of a matrix (for the "now vs then" summary). */
export function avgOffDiag(values: (number | null)[][]): number | null {
  let s = 0
  let k = 0
  values.forEach((row, i) =>
    row.forEach((v, j) => {
      if (i !== j && fin(v)) {
        s += v
        k += 1
      }
    }),
  )
  return k ? s / k : null
}

export type RegimeTone = 'up' | 'down' | 'neutral'
export function regimeTone(r: string | null | undefined): RegimeTone {
  return r === 'risk_on' ? 'up' : r === 'risk_off' ? 'down' : 'neutral'
}

/** Gauge needle position in [0, 1] for a composite clipped to ±range (0.5 = neutral). */
export function gaugePos(v: number | null | undefined, range = 2): number | null {
  return fin(v) ? (clamp(v, -range, range) + range) / (2 * range) : null
}

/** Components sorted by contribution (largest positive first), nulls last. */
export function sortedComponents(r: Pick<NonNullable<XaRisk['latest']>, 'components'>): NonNullable<XaRisk['latest']>['components'] {
  return [...r.components].sort((a, b) => (fin(b.z) ? b.z : -99) - (fin(a.z) ? a.z : -99))
}

/** Model Studio variable seeds for an asset: weekly log return (y) vs lagged trend / carry / positioning (x). */
export interface XaStudioSeed {
  name: string
  field: string
  title: string
  role: 'y' | 'x'
  transforms: CTransform[]
}
export function studioSeeds(asset: { asset_id: string; name: string }, hasPositioning: boolean, hasCarry = true): XaStudioSeed[] {
  const id = asset.asset_id
  const out: XaStudioSeed[] = [
    { name: 'ret', field: `${id}:price`, title: `${asset.name} weekly log return`, role: 'y', transforms: [{ op: 'log_diff' }] },
    { name: 'trend', field: `${id}:trend_signal`, title: `${asset.name} trend signal (model-based)`, role: 'x', transforms: [{ op: 'lag', k: 1 }] },
  ]
  if (hasCarry) out.push({ name: 'carry', field: `${id}:carry`, title: `${asset.name} carry`, role: 'x', transforms: [{ op: 'lag', k: 1 }] })
  if (hasPositioning) out.push({ name: 'cot', field: `${id}:cot_index_3y`, title: `${asset.name} CFTC COT index 3y`, role: 'x', transforms: [{ op: 'lag', k: 1 }] })
  return out
}

/** Downsample aligned arrays to at most `max` points (last point kept) for light sparkline-like charts. */
export function thin<T>(xs: T[], max = 600): T[] {
  if (xs.length <= max) return xs
  const step = Math.ceil(xs.length / max)
  const out = xs.filter((_, i) => i % step === 0)
  if ((xs.length - 1) % step !== 0) out.push(xs[xs.length - 1])
  return out
}

/** Drop leading dates before the first non-null value of `key` (old curve dates hold far-dated contracts only). */
type SeriesMap = Record<string, (number | null)[] | undefined>
export function trimLeading(dates: string[], series: SeriesMap, shapes: (string | null)[], key: string): { dates: string[]; series: SeriesMap; shapes: (string | null)[] } {
  const ref = series[key] ?? []
  const i0 = ref.findIndex((v) => fin(v))
  if (i0 <= 0) return { dates, series, shapes }
  const out: SeriesMap = {}
  for (const k of Object.keys(series)) out[k] = (series[k] ?? []).slice(i0)
  return { dates: dates.slice(i0), series: out, shapes: shapes.slice(i0) }
}
