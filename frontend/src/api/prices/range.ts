/**
 * Price-range selection for every price chart: 1M / 6M / YTD / 1Y / 5Y / 10Y / Max, with an automatic bar interval
 * (D ≤ 2Y, W ≤ 10Y, M for Max) and a manual override. Full history comes from /companies/{id}/prices or
 * /instruments/{id}/prices (daily for the last 15 years, weekly bars before). Pure helpers are unit-tested.
 */
import type { components } from '../generated/schema'
import type { PriceBar } from '../types'

export type PriceRange = '1M' | '6M' | 'YTD' | '1Y' | '5Y' | '10Y' | 'Max'
export type BarInterval = 'D' | 'W' | 'M'
export type IntervalChoice = 'auto' | BarInterval
export type CPriceSeries = components['schemas']['PriceSeries']

export const RANGES: PriceRange[] = ['1M', '6M', 'YTD', '1Y', '5Y', '10Y', 'Max']

const iso = (d: Date) => d.toISOString().slice(0, 10)

/** Start date (ISO) of a range ending at `end`; null = full history (Max). */
export function rangeStart(r: PriceRange, end: Date = new Date()): string | null {
  const d = new Date(Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate()))
  switch (r) {
    case '1M':
      d.setUTCMonth(d.getUTCMonth() - 1)
      break
    case '6M':
      d.setUTCMonth(d.getUTCMonth() - 6)
      break
    case 'YTD':
      return `${end.getUTCFullYear()}-01-01`
    case '1Y':
      d.setUTCFullYear(d.getUTCFullYear() - 1)
      break
    case '5Y':
      d.setUTCFullYear(d.getUTCFullYear() - 5)
      break
    case '10Y':
      d.setUTCFullYear(d.getUTCFullYear() - 10)
      break
    case 'Max':
      return null
  }
  return iso(d)
}

/** Auto interval: daily up to 2 years, weekly up to 10 years, monthly for Max. */
export function autoInterval(r: PriceRange): BarInterval {
  return r === 'Max' ? 'M' : r === '5Y' || r === '10Y' ? 'W' : 'D'
}

export function effectiveInterval(r: PriceRange, choice: IntervalChoice): BarInterval {
  return choice === 'auto' ? autoInterval(r) : choice
}

/** Contract series → chart bars plus the history meta (first bar, start of daily bars, served interval). */
export function seriesView(s: CPriceSeries): { bars: PriceBar[]; firstDate: string | null; dailyFrom: string | null; interval: BarInterval } {
  const bars = (s.bars ?? [])
    .filter((b) => typeof b.c === 'number' && Number.isFinite(b.c))
    .map((b) => ({ time: b.t, open: b.o ?? b.c, high: b.h ?? b.c, low: b.l ?? b.c, close: b.c, volume: b.v ?? 0 }))
    .sort((a, b) => a.time.localeCompare(b.time))
  return { bars, firstDate: s.first_date ?? bars[0]?.time ?? null, dailyFrom: s.daily_from ?? null, interval: s.interval }
}

/** Daily view requested but part of the range predates the daily table → those bars are weekly. */
export function hasWeeklyPart(interval: BarInterval, start: string | null, dailyFrom: string | null): boolean {
  return interval === 'D' && !!dailyFrom && (start === null || start < dailyFrom)
}
