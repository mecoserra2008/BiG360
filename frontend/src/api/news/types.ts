/**
 * News Terminal (roadmap v3 N4): contract aliases + pure adapters (tested in __tests__/news.test.ts).
 * Items come from GET /news/terminal (bus + 7-day intraday store) and the SSE topics news.item / news.story.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type NewsItem = S['NewsTerminalItem']
export type NewsPage = S['NewsTerminalPage']
export type NewsStory = S['NewsStory']
export type NewsStoryPage = S['NewsStoryPage']
export type NewsStoryDetail = S['NewsStoryDetail']
export type NewsHourly = S['NewsHourly']
export type NewsFeeds = S['NewsFeeds']
export type NewsFeed = S['NewsFeed']
export type NewsLiveHealth = S['NewsLiveHealth']
export type NewsImpact = S['NewsImpact']

/** Terminal row: the item plus client state (arrival time for the flash, synthetic perf rows). */
export interface TermRow extends NewsItem {
  /** client receive time (ms epoch) for items that arrived over SSE; null for the initial page */
  arrivedAt: number | null
  synthetic?: boolean
}

export const TIER_LABEL: Record<number, string> = { 1: 'Official', 2: 'Major press', 3: 'Publisher', 4: 'GDELT web' }
export const TIER_SHORT: Record<number, string> = { 1: 'OFF', 2: 'MAJ', 3: 'PUB', 4: 'WEB' }

export interface TermFilter {
  tickers: string[]
  watchlistOnly: boolean
  /** security ids of the watchlist (client-side filter when watchlistOnly) */
  watchSecurityIds: string[]
  countries: string[]
  sectors: string[]
  themes: string[]
  tiers: number[]
  langs: string[]
  q: string
  mutedStories: string[]
}

export const EMPTY_FILTER: TermFilter = { tickers: [], watchlistOnly: false, watchSecurityIds: [], countries: [], sectors: [], themes: [], tiers: [], langs: [], q: '', mutedStories: [] }

const up = (xs: readonly string[]) => xs.map((x) => x.toUpperCase())

/** Client-side filter (live SSE rows are filtered the same way as the server filters the REST page). */
export function matches(r: NewsItem, f: TermFilter): boolean {
  if (r.story_id && f.mutedStories.includes(r.story_id)) return false
  if (f.tickers.length && !up(r.tickers).some((t) => f.tickers.includes(t.toUpperCase()))) return false
  if (f.watchlistOnly) {
    // watchlist entries are security ids or tickers (local watchlist): either matches
    const w = new Set(up(f.watchSecurityIds))
    if (!r.security_ids.some((s) => w.has(s.toUpperCase())) && !r.tickers.some((t) => w.has(t.toUpperCase()) || w.has(t.toUpperCase().split('.')[0]))) return false
  }
  if (f.countries.length && !up(r.countries).some((c) => f.countries.includes(c))) return false
  if (f.sectors.length && !up(r.sectors).some((c) => f.sectors.includes(c))) return false
  if (f.themes.length && !r.themes.some((t) => f.themes.includes(t))) return false
  if (f.tiers.length && !f.tiers.includes(r.tier)) return false
  if (f.langs.length && !f.langs.includes((r.language ?? '').toLowerCase())) return false
  if (f.q) {
    const q = f.q.toLowerCase()
    if (!(r.title ?? '').toLowerCase().includes(q) && !(r.publisher ?? '').toLowerCase().includes(q)) return false
  }
  return true
}

/** REST query of a filter (the server applies the same rules; watchlist-only is applied client-side). */
export function filterQuery(f: TermFilter, limit: number, before?: string | null): Record<string, string | number | undefined> {
  return {
    limit,
    before: before ?? undefined,
    tickers: f.tickers.length ? f.tickers.join(',') : undefined,
    country: f.countries.length ? f.countries.join(',') : undefined,
    sector: f.sectors.length ? f.sectors.join(',') : undefined,
    theme: f.themes.length ? f.themes.join(',') : undefined,
    tier: f.tiers.length ? f.tiers.join(',') : undefined,
    lang: f.langs.length ? f.langs.join(',') : undefined,
    q: f.q || undefined,
  }
}

export const rowTs = (r: NewsItem): string => r.first_seen_at ?? r.published_at ?? ''

/**
 * Merge incoming rows into the list: dedupe by item_key (incoming wins, keeps the earliest arrival flash), newest
 * first, bounded. Pure and O(n) - one pass per batch (the SSE batches are applied once per animation frame).
 */
export function mergeRows(prev: TermRow[], incoming: TermRow[], max = 8000): TermRow[] {
  if (!incoming.length) return prev
  const byKey = new Map<string, TermRow>()
  for (const r of incoming) byKey.set(r.item_key, r)
  const out: TermRow[] = []
  const fresh = [...byKey.values()].sort((a, b) => rowTs(b).localeCompare(rowTs(a)))
  // fast path: everything new is newer than the current head (the normal live case)
  const head = prev.length ? rowTs(prev[0]) : ''
  if (fresh.every((r) => rowTs(r) >= head)) {
    const kept = prev.filter((r) => !byKey.has(r.item_key))
    return [...fresh, ...kept].slice(0, max)
  }
  let i = 0
  const prevKept = prev.filter((r) => !byKey.has(r.item_key))
  for (const r of prevKept) {
    while (i < fresh.length && rowTs(fresh[i]) >= rowTs(r)) out.push(fresh[i++])
    out.push(r)
  }
  while (i < fresh.length) out.push(fresh[i++])
  return out.slice(0, max)
}

export interface StoryGroup {
  key: string
  storyId: string | null
  /** newest item of the story (row shown in grouped mode) */
  head: TermRow
  items: TermRow[]
  nSources: number
  firstSeen: string
  lastSeen: string
  sentiment: number | null
}

/** Group rows by story (one row per story, newest first); items without a story are their own group. */
export function groupByStory(rows: TermRow[]): StoryGroup[] {
  const map = new Map<string, StoryGroup>()
  const order: string[] = []
  for (const r of rows) {
    const key = r.story_id ?? `item:${r.item_key}`
    let g = map.get(key)
    if (!g) {
      g = { key, storyId: r.story_id ?? null, head: r, items: [], nSources: 0, firstSeen: rowTs(r), lastSeen: rowTs(r), sentiment: null }
      map.set(key, g)
      order.push(key)
    }
    g.items.push(r)
    const t = rowTs(r)
    if (t < g.firstSeen) g.firstSeen = t
    if (t > g.lastSeen) g.lastSeen = t
  }
  for (const g of map.values()) {
    g.nSources = new Set(g.items.map((x) => x.publisher ?? x.source_id ?? '')).size
    const s = g.items.map((x) => x.sentiment).filter((x): x is number => typeof x === 'number')
    g.sentiment = s.length ? s.reduce((a, b) => a + b, 0) / s.length : null
    // the story's own counter can be larger than what the list holds (older members)
    g.nSources = Math.max(g.nSources, g.head.story_sources ?? 1)
  }
  return order.map((k) => map.get(k) as StoryGroup)
}

/** Sentiment colour class (model-based score -1..1); GDELT tone as fallback. */
export function sentimentTone(r: Pick<NewsItem, 'sentiment' | 'tone'>): 'up' | 'down' | 'neutral' {
  const s = r.sentiment ?? (typeof r.tone === 'number' ? r.tone / 10 : null)
  if (s === null || s === undefined) return 'neutral'
  if (s > 0.2) return 'up'
  if (s < -0.2) return 'down'
  return 'neutral'
}

/** Salience bucket 0..3 for the left bar (story size, sources, tier, linking). */
export function salienceLevel(s: number | null | undefined): 0 | 1 | 2 | 3 {
  if (s === null || s === undefined) return 0
  if (s >= 4.5) return 3
  if (s >= 3) return 2
  if (s >= 1.8) return 1
  return 0
}

/** Keyboard: next index for j / k within [0, n). */
export function moveSel(cur: number, n: number, dir: 1 | -1): number {
  if (n <= 0) return -1
  if (cur < 0) return dir > 0 ? 0 : n - 1
  return Math.max(0, Math.min(n - 1, cur + dir))
}

/** Unread = arrived over SSE after the user last looked (or never selected). */
export function unreadCount(rows: TermRow[], seenAt: number): number {
  let n = 0
  for (const r of rows) if (r.arrivedAt !== null && r.arrivedAt > seenAt) n++
  return n
}

/** Price move label "+1.23%" (fraction in). */
/** Price move given as a FRACTION (0.0123 = +1.23 %). */
export function moveLabel(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const p = v * 100
  return `${p > 0 ? '+' : p < 0 ? '−' : ''}${Math.abs(p).toFixed(2)}%`
}

/** Event-study CAR / CI given in PERCENT (NewsImpact.unit = 'percent': 1.23 = +1.23 %); never multiplied by 100. */
export function carPctLabel(v: number | null | undefined, unit: string | null | undefined = 'percent'): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return unit === 'fraction' ? moveLabel(v) : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%`
}

/** SSE data frame (LiveStreamEvent) of topic news.item -> terminal row. */
export function rowFromStream(item: Record<string, unknown>, now: number): TermRow | null {
  const r = item as unknown as NewsItem
  if (!r || typeof r.item_key !== 'string' || !r.title) return null
  return {
    ...r,
    tickers: r.tickers ?? [],
    security_ids: r.security_ids ?? [],
    issuer_ids: r.issuer_ids ?? [],
    countries: r.countries ?? [],
    sectors: r.sectors ?? [],
    themes: r.themes ?? [],
    story_size: r.story_size ?? 1,
    story_sources: r.story_sources ?? 1,
    arrivedAt: now,
  }
}

/** URL search params <-> filter (shareable links; `ticker=` from the command bar CN mnemonic). */
export function filterFromParams(p: URLSearchParams): Partial<TermFilter> {
  const list = (k: string) => (p.get(k) ?? '').split(',').map((x) => x.trim()).filter(Boolean)
  const out: Partial<TermFilter> = {}
  const tick = [...list('ticker'), ...list('tickers')].map((x) => x.toUpperCase())
  if (tick.length) out.tickers = tick
  if (list('country').length) out.countries = up(list('country'))
  if (list('sector').length) out.sectors = up(list('sector'))
  if (list('theme').length) out.themes = list('theme')
  if (list('tier').length) out.tiers = list('tier').map(Number).filter((x) => x >= 1 && x <= 4)
  if (list('lang').length) out.langs = list('lang').map((x) => x.toLowerCase())
  if (p.get('q')) out.q = p.get('q') as string
  if (p.get('watchlist') === '1') out.watchlistOnly = true
  return out
}
