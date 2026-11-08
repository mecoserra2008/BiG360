/** Cross-asset tape: pure helpers (level, 1D and 1M changes from dated series). */

export type TapeGroup = 'rates' | 'fx' | 'commodities' | 'vol' | 'crypto' | 'credit'

export interface TapeItem {
  id: string
  group: TapeGroup
  label: string
  /** formatted level */
  level: number | null
  /** how to read level/changes: price (pct changes), yield (bp changes), bps (bp changes), index (pct changes) */
  kind: 'price' | 'yield' | 'bps' | 'index'
  /** pct (fraction) for price/index, bp for yield/bps; null = not available */
  change1d: number | null
  change1m: number | null
  asOf: string | null
  /** higher = risk-off (vol, spreads) → colour inverted */
  invert?: boolean
  source: string
  href?: string
  /** z-score of the asset's 21-day realised vol (cross-asset cockpit), when known */
  volZ?: number | null
  /** cockpit trend signal in [-1, 1] (model-based) */
  trend?: number | null
}

export const TAPE_GROUP_LABEL: Record<TapeGroup, string> = {
  rates: 'Rates',
  fx: 'FX',
  commodities: 'Commodities',
  vol: 'Volatility',
  crypto: 'Crypto',
  credit: 'Credit',
}

/** Cross-asset cockpit assets on the tape (asset_id → group, short label). */
export const TAPE_ASSETS: { id: string; group: TapeGroup; label: string }[] = [
  { id: 'EURUSD', group: 'fx', label: 'EUR/USD' },
  { id: 'USDJPY', group: 'fx', label: 'USD/JPY' },
  { id: 'GBPUSD', group: 'fx', label: 'GBP/USD' },
  { id: 'USDCNY', group: 'fx', label: 'USD/CNY' },
  { id: 'CL', group: 'commodities', label: 'WTI' },
  { id: 'BZ', group: 'commodities', label: 'Brent' },
  { id: 'GC', group: 'commodities', label: 'Gold' },
  { id: 'HG', group: 'commodities', label: 'Copper' },
  { id: 'BTC', group: 'crypto', label: 'Bitcoin' },
  { id: 'ETH', group: 'crypto', label: 'Ether' },
]

/** FRED (official) series on the tape. */
export const TAPE_SERIES: { id: string; group: TapeGroup; label: string; kind: TapeItem['kind']; invert?: boolean }[] = [
  { id: 'fred:VIXCLS', group: 'vol', label: 'VIX', kind: 'index', invert: true },
  { id: 'fred:DTWEXBGS', group: 'fx', label: 'Broad USD', kind: 'index' },
]

const t = (d: string) => Date.parse(`${d}T00:00:00Z`)

/** Value on or before `date - days` (calendar), scanning a date-sorted series. */
export function valueDaysBefore(points: { date: string; value: number }[], days: number): number | null {
  if (!points.length) return null
  const cut = t(points[points.length - 1].date) - days * 86_400_000
  for (let i = points.length - 1; i >= 0; i--) if (t(points[i].date) <= cut) return points[i].value
  return null
}

/** Last value, 1-observation change and calendar-1M change of a dated series. */
export function seriesChanges(
  raw: { date: string; value: number | null | undefined }[],
  mode: 'pct' | 'diff',
): { last: number | null; asOf: string | null; change1d: number | null; change1m: number | null } {
  const pts = raw.filter((p): p is { date: string; value: number } => typeof p.value === 'number' && Number.isFinite(p.value)).sort((a, b) => t(a.date) - t(b.date))
  if (!pts.length) return { last: null, asOf: null, change1d: null, change1m: null }
  const last = pts[pts.length - 1].value
  const prev = pts.length > 1 ? pts[pts.length - 2].value : null
  const m1 = valueDaysBefore(pts, 30)
  const ch = (base: number | null) => (base === null ? null : mode === 'pct' ? (base ? last / base - 1 : null) : last - base)
  return { last, asOf: pts[pts.length - 1].date, change1d: ch(prev), change1m: ch(m1) }
}

/** Cross-asset asset detail (dates[] + series.price[]) → dated points. */
export function assetPoints(dates: string[] | undefined, prices: (number | null)[] | undefined): { date: string; value: number | null }[] {
  if (!dates || !prices) return []
  return dates.map((date, i) => ({ date, value: prices[i] ?? null }))
}
