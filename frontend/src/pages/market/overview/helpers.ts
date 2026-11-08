/** Pure helpers of the Markets Overview (kept out of component files for fast refresh and tests). */
import type { IndexQuote, MacroIndicator, MacroRegime, RegionCode, RegionDataAsOf, SectorPerformance, YieldCurve } from '@/api/types'
import { fmtDateShort } from '@/lib/format'

export const REGION_ORDER = ['US', 'CA', 'LATAM', 'UK', 'EU', 'JP', 'KR', 'CN', 'HK', 'CNHK', 'APAC', 'OTHER']

export const isoDaysAgo = (n: number): string => new Date(Date.now() - n * 86_400_000).toISOString().slice(0, 10)

/** Weekdays strictly after a, up to and including b. */
export const sessionsBetween = (a: string, b: string): number => {
  // weekdays strictly after a, up to and including b
  let n = 0
  const d = new Date(`${a}T00:00:00Z`)
  const end = Date.parse(`${b}T00:00:00Z`)
  while (d.getTime() < end) {
    d.setUTCDate(d.getUTCDate() + 1)
    const w = d.getUTCDay()
    if (w !== 0 && w !== 6) n++
  }
  return n
}

export interface AsOfChip {
  region: string
  asOf: string
  note?: string
  staleShare?: number | null
}

/** Per-region data date chips: v1.29 data_as_of, else the benchmark index dates (labelled). */
export function asOfChips(dataAsOf: RegionDataAsOf[], indices: IndexQuote[]): { chips: AsOfChip[]; source: 'data_as_of' | 'index' | 'none' } {
  if (dataAsOf.length)
    return {
      chips: [...dataAsOf].sort((a, b) => REGION_ORDER.indexOf(a.region) - REGION_ORDER.indexOf(b.region)).map((d) => ({ region: d.region, asOf: d.asOf, staleShare: d.staleShare })),
      source: 'data_as_of',
    }
  const by = new Map<string, string>()
  for (const i of indices) if (i.asOf && (!by.has(i.region) || (by.get(i.region) as string) < i.asOf)) by.set(i.region, i.asOf)
  if (!by.size) return { chips: [], source: 'none' }
  return { chips: [...by.entries()].sort((a, b) => REGION_ORDER.indexOf(a[0]) - REGION_ORDER.indexOf(b[0])).map(([region, asOf]) => ({ region, asOf })), source: 'index' }
}

export function regimeChipText(r: MacroRegime): string {
  if (r.status === 'not_modelled' || !r.current) return `No regime model for ${r.requestedScope}`
  const states = r.states.length ? `${r.states.length} states` : 'HMM'
  const date = r.date ? ` · as of ${fmtDateShort(r.date)}` : ''
  if (r.status === 'fallback') return `No ${r.requestedScope} model · ${r.scope} regime (fallback): ${r.current} (HMM, ${states}${date})`
  return `${r.scope ?? r.requestedScope} regime: ${r.current} (HMM, ${states}${date})`
}

export type Horizon = 'return1d' | 'return1m' | 'returnYtd'
export type SectorBasis = 'total' | 'relative'

/** Region benchmark for sector-minus-index returns (first index found in the payload). */
const BENCH: Record<string, string[]> = {
  US: ['^GSPC'],
  EU: ['^STOXX50E', '^GDAXI'],
  UK: ['^FTSE'],
  JP: ['^N225'],
  KR: ['^KS11'],
  CNHK: ['^HSI', '000300.SS'],
  LATAM: ['^BVSP'],
}
export const IDX_FIELD: Record<Horizon, 'change1d' | 'change1m' | 'changeYtd'> = { return1d: 'change1d', return1m: 'change1m', returnYtd: 'changeYtd' }

export function benchmarkFor(region: string, indices: IndexQuote[]): IndexQuote | null {
  for (const id of BENCH[region] ?? []) {
    const hit = indices.find((i) => i.id === id)
    if (hit) return hit
  }
  return null
}

export interface SectorCell {
  sector: string
  region: RegionCode
  /** percent */
  value: number
  raw: number
  bench: IndexQuote | null
  row: SectorPerformance
}

/** Sector × region cells in percent; `relative` subtracts the region benchmark's return over the same horizon. */
export function sectorCells(perf: SectorPerformance[], h: Horizon, basis: SectorBasis, indices: IndexQuote[]): SectorCell[] {
  return perf
    .map((p) => {
      const raw = p[h]
      const bench = benchmarkFor(p.region, indices)
      const b = bench ? bench[IDX_FIELD[h]] : NaN
      const v = basis === 'relative' ? raw - b : raw
      return { sector: p.sector, region: p.region, value: v * 100, raw: raw * 100, bench, row: p }
    })
    .filter((c) => Number.isFinite(c.value))
}


/** Curves worth drawing: at least 2 market tenors (policy rate excluded); 2-tenor ones are flagged. */
export function curveOptions(curves: YieldCurve[]): { value: string; label: string }[] {
  return curves
    .filter((c) => c.nMarketTenors >= 2)
    .sort((a, b) => REGION_ORDER.indexOf(a.region ?? 'OTHER') - REGION_ORDER.indexOf(b.region ?? 'OTHER') || a.label.localeCompare(b.label))
    .map((c) => ({ value: c.country, label: `${c.region ?? ''} · ${c.label}${c.nMarketTenors < 3 ? ` (${c.nMarketTenors} tenors)` : ''}` }))
}

/** "1y" / "5y" / "60 obs" label of a spark window. */
export function sparkWindowLabel(days: number | null | undefined, n: number): string {
  if (!days) return `last ${n} obs`
  if (days >= 360) return `${Math.round(days / 365.25)}y`
  return `${days}d`
}

/** At most `perRegion` headline tiles per region, rates of change first. */
export function headlineTiles(xs: MacroIndicator[], perRegion = 6): MacroIndicator[] {
  const by = new Map<string, MacroIndicator[]>()
  for (const i of xs) {
    if (i.region === 'OTHER' && xs.some((x) => x.region !== 'OTHER')) continue
    const k = i.region
    by.set(k, [...(by.get(k) ?? []), i])
  }
  // transformed series (y/y, q/q SAAR, m/m) before raw levels; stable within each group
  return [...by.values()].flatMap((list) => [...list].sort((a, b) => Number(!a.displayLabel) - Number(!b.displayLabel)).slice(0, perRegion))
}
