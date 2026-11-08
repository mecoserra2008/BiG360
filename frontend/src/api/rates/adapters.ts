/** Pure helpers for the rates & credit pages (unit-tested in __tests__/rates.test.ts). Rates arrive in percent,
 * spreads in basis points. */
import type { CompanyDebtPeriod, CreditSeries, CreditWallBucket, RatesCurveMeta, RatesFrames, RatesMonitorCountry, RatesSnapshot } from './types'
import { BUCKET_LABEL } from './types'

export const DASH = '—'
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const MINUS = '−'

/** 0 → ON, 3 → 3M, 18 → 18M, 24 → 2Y, 120 → 10Y. */
export function tenorLabel(m: number): string {
  if (m === 0) return 'ON'
  if (m < 24 && m % 12 !== 0) return `${m}M`
  return `${+(m / 12).toFixed(2)}Y`
}

/** "4.25%" from a percent value (two decimals; three under 1 %). */
export function fmtRate(v: number | null | undefined, digits?: number): string {
  if (!fin(v)) return DASH
  const d = digits ?? (Math.abs(v) < 1 ? 3 : 2)
  return `${v.toFixed(d).replace('-', MINUS)}%`
}

/** "+12 bp" / "−3 bp" (rounded). */
export function fmtBp(v: number | null | undefined, sign = true): string {
  if (!fin(v)) return DASH
  const r = Math.round(v)
  return `${sign && r > 0 ? '+' : ''}${String(r).replace('-', MINUS)} bp`
}

/** Curve snapshot → [years, percent] points (x in years so tenors share one axis). */
export function curveXY(s: Pick<RatesSnapshot, 'points'>): [number, number][] {
  return s.points.filter((p) => fin(p.value)).map((p) => [p.tenor_months / 12, p.value as number])
}

/** Animation frame i → [years, percent] points (nulls dropped). */
export function frameXY(f: RatesFrames, i: number): [number, number][] {
  const row = f.values[i] ?? []
  const out: [number, number][] = []
  f.tenors.forEach((t, j) => {
    const v = row[j]
    if (fin(v)) out.push([t / 12, v])
  })
  return out
}

/** Index of the last frame dated on or before `date` (0 when before the first). */
export function frameIndexAt(f: RatesFrames, date: string): number {
  let lo = 0
  let hi = f.dates.length - 1
  if (hi < 0 || date <= f.dates[0]) return 0
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1
    if (f.dates[mid] <= date) lo = mid
    else hi = mid - 1
  }
  return lo
}

/** y-axis extent over every frame (so the animation does not rescale while playing), padded by 5 %. */
export function framesExtent(f: RatesFrames): [number, number] | null {
  let lo = Infinity
  let hi = -Infinity
  for (const row of f.values) for (const v of row) if (fin(v)) {
    lo = Math.min(lo, v)
    hi = Math.max(hi, v)
  }
  if (!Number.isFinite(lo)) return null
  const pad = Math.max((hi - lo) * 0.05, 0.05)
  return [Math.floor((lo - pad) * 10) / 10, Math.ceil((hi + pad) * 10) / 10]
}

/** "US_TSY:120:par" (the /rates/series id). */
export const seriesId = (curve: string, tenor: number, type: string) => `${curve}:${tenor}:${type}`

/** Default spread set for a country row: its 10y vs the Bund and vs the UST (only when the country is not the base). */
export function defaultSpreads(c: Pick<RatesMonitorCountry, 'curve_id' | 'type' | 'country'>, bund?: Pick<RatesMonitorCountry, 'curve_id' | 'type'>, ust?: Pick<RatesMonitorCountry, 'curve_id' | 'type'>, tenor = 120): string[] {
  const out: string[] = []
  const me = seriesId(c.curve_id, tenor, c.type)
  if (bund && c.country !== 'DE') out.push(`${me}-${seriesId(bund.curve_id, tenor, bund.type)}`)
  if (ust && c.country !== 'US') out.push(`${me}-${seriesId(ust.curve_id, tenor, ust.type)}`)
  return out
}

/** 2s10s / 5s30s spread series ids of a curve. */
export function slopeSpreads(curve: string, type: string): string[] {
  return [`${seriesId(curve, 120, type)}-${seriesId(curve, 24, type)}`, `${seriesId(curve, 360, type)}-${seriesId(curve, 60, type)}`]
}

/** Curves of the catalogue grouped for the picker: sovereign benchmarks first, then other sovereign, money market, monthly 10y. */
export function groupCurves(items: RatesCurveMeta[]): { label: string; items: RatesCurveMeta[] }[] {
  const ok = items.filter((c) => c.status === 'ok')
  const groups = [
    { label: 'Country benchmarks', items: ok.filter((c) => c.is_country_benchmark) },
    { label: 'Other official curves', items: ok.filter((c) => !c.is_country_benchmark && c.group === 'sovereign') },
    { label: 'Money market', items: ok.filter((c) => c.group === 'money_market') },
    { label: 'Monthly 10y (rest of world)', items: ok.filter((c) => c.group === 'lt10') },
  ]
  return groups.filter((g) => g.items.length)
}

/** Maturity wall bars in display order (y1..after5, then the IFRS 1-5y bucket when present). */
export function wallBars<T extends { bucket: string }>(rows: T[]): (T & { label: string })[] {
  const order = ['y1', 'y2', 'y3', 'y4', 'y5', 'y2_5', 'after5']
  return rows
    .filter((r) => r.bucket !== 'total' && order.includes(r.bucket))
    .sort((a, b) => order.indexOf(a.bucket) - order.indexOf(b.bucket))
    .map((r) => ({ ...r, label: BUCKET_LABEL[r.bucket] ?? r.bucket }))
}

/** Total of a wall's buckets (null when nothing is tagged). */
export function wallTotal(rows: { bucket: string; amount?: number | null; amount_usd?: number | null }[]): number | null {
  const vals = rows.filter((r) => r.bucket !== 'total').map((r) => (r.amount ?? r.amount_usd ?? null)).filter(fin)
  return vals.length ? vals.reduce((a, b) => a + b, 0) : null
}

/** Credit series grouped by `group` in API order. */
export function groupCredit(series: CreditSeries[]): Record<string, CreditSeries[]> {
  const out: Record<string, CreditSeries[]> = {}
  for (const s of series) (out[s.group] ??= []).push(s)
  return out
}

/** Debt KPIs of a period with the reason a value is missing. */
export function debtKpis(p: CompanyDebtPeriod | null | undefined): { key: string; label: string; value: number | null; unit: 'x' | 'pct' | 'years' }[] {
  const v = (x: number | null | undefined) => (fin(x) ? x : null)
  return [
    { key: 'avg_cost_of_debt', label: 'Average cost of debt', value: v(p?.avg_cost_of_debt), unit: 'pct' },
    { key: 'interest_coverage', label: 'Interest coverage (EBIT / interest)', value: v(p?.interest_coverage), unit: 'x' },
    { key: 'net_debt_to_ebitda', label: 'Net debt / EBITDA', value: v(p?.net_debt_to_ebitda), unit: 'x' },
    { key: 'floating_share', label: 'Floating-rate share', value: v(p?.floating_share), unit: 'pct' },
    { key: 'refi_share_24m', label: 'Maturing within 24 months', value: v(p?.refi_share_24m), unit: 'pct' },
    { key: 'wam_years', label: 'Weighted-average maturity', value: v(p?.wam_years), unit: 'years' },
  ]
}

/** Thin a dated series to at most n points (keeps the last). */
export function thinPoints<T>(pts: T[], n = 600): T[] {
  if (pts.length <= n) return pts
  const step = Math.ceil(pts.length / n)
  const out = pts.filter((_, i) => i % step === 0)
  if (out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1])
  return out
}

/** Bucket share bar width in percent (0..100) of the wall's largest bucket. */
export function barPct(v: number | null | undefined, max: number | null): number {
  if (!fin(v) || !fin(max) || max <= 0) return 0
  return Math.max(0, Math.min(100, (v / max) * 100))
}

export type { CreditWallBucket }
