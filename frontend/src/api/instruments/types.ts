/** Markets → Instruments (contract v1.19.0): FX pairs, futures, indices and ETFs from Yahoo market series. */
import type { components } from '../generated/schema'

type S = components['schemas']
export type InstrumentList = S['InstrumentList']
export type InstrumentItem = S['InstrumentItem']
export type InstrumentDetail = S['InstrumentDetail']
export type InstrumentStats = S['InstrumentStats']

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Items grouped in the order of `groups` (unknown groups last). */
export function groupItems(d: Pick<InstrumentList, 'groups' | 'items'>, filter = ''): { group: string; label: string; items: InstrumentItem[] }[] {
  const q = filter.trim().toLowerCase()
  const match = (x: InstrumentItem) => !q || x.ticker.toLowerCase().includes(q) || x.name.toLowerCase().includes(q)
  return d.groups.map((g) => ({ group: g.group, label: g.label, items: d.items.filter((x) => x.group === g.group && match(x)) })).filter((g) => g.items.length > 0)
}

/** Price precision for display: FX / small prices need more decimals. */
export function priceDigits(v: number | null | undefined, type: string): number {
  if (!fin(v)) return 2
  const a = Math.abs(v)
  if (type === 'fx') return a >= 100 ? 2 : a >= 10 ? 3 : 4
  return a >= 1000 ? 0 : a >= 1 ? 2 : 4
}

/** The full-history window stats row (window "Max"). */
export const maxStats = (d: Pick<InstrumentDetail, 'stats'>): InstrumentStats | undefined => d.stats.find((s) => s.window === 'Max')
