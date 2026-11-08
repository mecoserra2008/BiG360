/** Pure helpers for the earnings pages (date grids, labels, series). All dates are ISO yyyy-mm-dd (UTC arithmetic). */
import type { CalendarView, ECalendarItem, EExpectationRow, EHistoryRow, EventGroup } from './types'

export const SECTOR_NAME: Record<string, string> = {
  CDIS: 'Consumer Discretionary',
  COMM: 'Communication & Media',
  CSTA: 'Consumer Staples',
  ENRG: 'Energy',
  FINS: 'Financials',
  HLTH: 'Healthcare',
  INDU: 'Industrials',
  MATR: 'Materials',
  REAL: 'Real Estate',
  TECH: 'Technology',
  UTIL: 'Utilities',
  UNKNOWN: 'Unclassified',
  ALL: 'All sectors',
}
export const sectorName = (id: string | null | undefined): string => (id ? (SECTOR_NAME[id] ?? id) : '—')

export type Tone = 'neutral' | 'accent' | 'up' | 'down' | 'warn' | 'info'

/** Date-source badges: what the date is based on. Provider and model-based dates are always labelled. */
export const DATE_SOURCE_META: Record<string, { short: string; long: string; tone: Tone }> = {
  official_release: { short: 'Official', long: 'Official earnings release (filing / exchange disclosure time)', tone: 'up' },
  official_filing: { short: 'Filing', long: 'Official periodic report filing (may lag the first public release)', tone: 'neutral' },
  provider: { short: 'Provider', long: 'Provider date (Yahoo)', tone: 'info' },
  company_announced: { short: 'Announced', long: 'Company-announced (exchange schedule)', tone: 'up' },
  provider_estimate: { short: 'Provider est.', long: 'Provider estimate (Yahoo)', tone: 'info' },
  model_projection: { short: 'Projection', long: 'Model-based projection (reporting cadence)', tone: 'warn' },
}
export const dateSourceMeta = (s: string | null | undefined) => (s ? DATE_SOURCE_META[s] : undefined) ?? { short: s ?? '—', long: s ?? '', tone: 'neutral' as Tone }

export const CORP_LABEL: Record<string, string> = {
  dividend_ex: 'Dividend (ex-date)',
  dividend_pay: 'Dividend payment',
  special_dividend: 'Special dividend',
  stock_dividend: 'Stock dividend',
  split: 'Split',
  spinoff: 'Spin-off',
  rights: 'Rights issue',
  mna_agreement: 'Material agreement',
  mna_completed: 'Acquisition / disposal completed',
  exec_change: 'Executive / director change',
  financing: 'Financing obligation',
  equity_issue: 'Equity issuance',
  other_material: 'Other material event',
  agm_results: 'Shareholder vote results',
  agm_notice: 'AGM notice / proxy',
  buyback: 'Share buyback',
  guidance_revision: 'Guidance revision',
  board_meeting: 'Board meeting',
  general_meeting: 'General meeting',
}

const DIVIDEND = new Set(['dividend_ex', 'dividend_pay', 'special_dividend', 'stock_dividend'])
const SPLIT = new Set(['split', 'spinoff', 'rights'])

export function eventGroup(it: Pick<ECalendarItem, 'kind' | 'event_type'>): EventGroup {
  if (it.kind === 'earnings') return 'earnings'
  if (DIVIDEND.has(it.event_type)) return 'dividend'
  if (SPLIT.has(it.event_type)) return 'split'
  return 'corporate'
}

export const SESSION_LABEL: Record<string, string> = {
  pre_market: 'Before open',
  intraday: 'During session',
  post_market: 'After close',
  unknown: 'Time n/a',
}
export const sessionLabel = (s: string | null | undefined): string => (s ? (SESSION_LABEL[s] ?? s) : '—')

/* ───────────── dates ───────────── */

const toDate = (iso: string): Date => new Date(`${iso}T00:00:00Z`)
const toIso = (d: Date): string => d.toISOString().slice(0, 10)

export function addDays(iso: string, n: number): string {
  const d = toDate(iso)
  d.setUTCDate(d.getUTCDate() + n)
  return toIso(d)
}

/** Monday of the ISO week containing iso. */
export function weekStart(iso: string): string {
  const d = toDate(iso)
  const wd = (d.getUTCDay() + 6) % 7
  return addDays(iso, -wd)
}

export function weekDays(iso: string): string[] {
  const s = weekStart(iso)
  return Array.from({ length: 7 }, (_, i) => addDays(s, i))
}

/** Weeks (Mon-Sun) covering the month of iso; each cell flags whether it belongs to the month. */
export function monthGrid(iso: string): { date: string; inMonth: boolean }[][] {
  const d = toDate(iso)
  const first = toIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1)))
  const last = toIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)))
  const weeks: { date: string; inMonth: boolean }[][] = []
  let cur = weekStart(first)
  while (cur <= last) {
    weeks.push(weekDays(cur).map((x) => ({ date: x, inMonth: x >= first && x <= last })))
    cur = addDays(cur, 7)
  }
  return weeks
}

export function rangeFor(view: CalendarView, anchor: string): { from: string; to: string } {
  if (view === 'day') return { from: anchor, to: anchor }
  if (view === 'week') {
    const ds = weekDays(anchor)
    return { from: ds[0], to: ds[6] }
  }
  const g = monthGrid(anchor)
  return { from: g[0][0].date, to: g[g.length - 1][6].date }
}

export function shiftAnchor(view: CalendarView, anchor: string, dir: -1 | 1): string {
  if (view === 'day') {
    let x = addDays(anchor, dir)
    while ([0, 6].includes(toDate(x).getUTCDay())) x = addDays(x, dir)
    return x
  }
  if (view === 'week') return addDays(anchor, 7 * dir)
  const d = toDate(anchor)
  return toIso(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + dir, 1)))
}

export function rangeTitle(view: CalendarView, anchor: string): string {
  const fmt = (iso: string, o: Intl.DateTimeFormatOptions) => toDate(iso).toLocaleDateString('en-GB', { timeZone: 'UTC', ...o })
  if (view === 'day') return fmt(anchor, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })
  if (view === 'week') {
    const ds = weekDays(anchor)
    return `${fmt(ds[0], { day: 'numeric', month: 'short' })} – ${fmt(ds[6], { day: 'numeric', month: 'short', year: 'numeric' })}`
  }
  return fmt(anchor, { month: 'long', year: 'numeric' })
}

/* ───────────── calendar items ───────────── */

export function bucketByDay(items: ECalendarItem[]): Map<string, ECalendarItem[]> {
  const m = new Map<string, ECalendarItem[]>()
  for (const it of items) {
    const arr = m.get(it.date)
    if (arr) arr.push(it)
    else m.set(it.date, [it])
  }
  return m
}

/** Earnings first, then market cap (largest first). */
export function sortDay(items: ECalendarItem[]): ECalendarItem[] {
  return [...items].sort((a, b) => (a.kind === b.kind ? (b.security.market_cap_usd ?? 0) - (a.security.market_cap_usd ?? 0) : a.kind === 'earnings' ? -1 : 1))
}

export function filterGroups(items: ECalendarItem[], groups: EventGroup[]): ECalendarItem[] {
  const g = new Set(groups)
  return items.filter((it) => g.has(eventGroup(it)))
}

/** Upcoming item whose (projected) date already passed: release not yet seen in the data. */
export const isOverdue = (it: ECalendarItem, asOf: string): boolean => it.kind === 'earnings' && it.status === 'upcoming' && it.date < asOf

/* ───────────── company series ───────────── */

export function reactionSeries(history: EHistoryRow[]): { label: string; date: string; car: number | null; baseline: number | null; drift: number | null }[] {
  return [...history]
    .sort((a, b) => (a.period_end < b.period_end ? -1 : 1))
    .map((h) => ({
      label: `${h.fiscal_period ?? ''} ${h.fiscal_year ?? h.period_end.slice(0, 4)}`.trim(),
      date: h.release_date,
      car: h.car_m1_p1 ?? null,
      baseline: h.baseline_move ?? null,
      drift: h.car_p1_p20 ?? null,
    }))
}

export function expectationSeries(rows: EExpectationRow[], item: 'eps_diluted' | 'revenue'): EExpectationRow[] {
  return rows.filter((r) => r.item_id === item && r.is_primary).sort((a, b) => (a.period_end < b.period_end ? -1 : 1))
}

export function seasonLabel(season: string): string {
  const m = /^(\d{4})Q([1-4])$/.exec(season)
  if (!m) return season
  const months = ['Jan–Mar', 'Apr–Jun', 'Jul–Sep', 'Oct–Dec'][Number(m[2]) - 1]
  return `Q${m[2]} ${m[1]} · periods ending ${months} ${m[1]}`
}
