/**
 * Real-time layer (R1): /live/* REST pages + the SSE stream. Contract aliases and pure adapters to a single
 * feed-item view model (unit-tested in __tests__/live.test.ts).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type LiveFiling = S['LiveFiling']
export type LiveNews = S['LiveNewsItem']
export type LiveEvent = S['LiveEventItem']
export type LiveMacro = S['LiveMacroItem']
export type LiveAlert = S['LiveAlertItem']
export type LiveSignal = S['LiveSignalItem']
export type LiveStatus = S['LiveStatus']
export type LiveStreamHealth = S['LiveStreamHealth']
export type LiveStreamEvent = S['LiveStreamEvent']
export type LiveHello = S['LiveStreamHello']
export type LiveHeartbeat = S['LiveStreamHeartbeat']
export interface LivePage<T> {
  data_status: S['DataStatus']
  generated_at?: string
  head_id?: number | null
  n_live?: number
  n_snapshot?: number
  items: T[]
}

export type LiveTopic = 'filings' | 'news' | 'events' | 'macro' | 'alerts' | 'signals'
export const FEED_TOPICS: LiveTopic[] = ['filings', 'news', 'macro', 'alerts']
/** workstream B (v1.40.0): delayed quotes ride the same stream (they update the quotes store, not the feed) */
export const QUOTE_TOPICS = ['quotes.tick', 'quotes.session'] as const
export const STREAM_TOPICS: readonly string[] = [...FEED_TOPICS, ...QUOTE_TOPICS]

export interface FeedItem {
  /** topic:key — dedupe key across REST seeds and the stream */
  uid: string
  /** bus id (null for snapshot rows) */
  busId: number | null
  topic: LiveTopic
  type: string
  /** source publication time, else ingest / first-seen time (ISO) */
  ts: string | null
  title: string
  detail: string | null
  /** external source link */
  href: string | null
  /** internal route (Company 360 / country page) */
  link: string | null
  securityId: string | null
  ticker: string | null
  /** direction for colouring (macro surprise sign / news sentiment / alert) */
  tone: -1 | 0 | 1
  modelBased: boolean
  origin: 'live' | 'snapshot' | 'app_state' | 'stream'
}

const s = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v : null)
const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const sign = (v: number | null, dead = 0): -1 | 0 | 1 => (v === null || Math.abs(v) <= dead ? 0 : v > 0 ? 1 : -1)

export function feedFromFiling(f: LiveFiling, busId?: number | null): FeedItem {
  const who = s(f.ticker) ?? s(f.company) ?? s(f.source_entity_id) ?? f.source_id
  return {
    uid: `filings:${f.filing_id}`,
    busId: busId ?? f.live_id ?? null,
    topic: 'filings',
    type: s(f.form_class) ?? 'filing',
    ts: s(f.source_ts) ?? s(f.first_seen_at),
    title: `${who} · ${s(f.form_type) ?? 'filing'}`,
    detail: s(f.title) ?? s(f.company),
    href: s(f.url),
    link: f.security_id ? `/company/${encodeURIComponent(f.security_id)}` : null,
    securityId: s(f.security_id),
    ticker: s(f.ticker),
    tone: 0,
    modelBased: false,
    origin: (f.origin as FeedItem['origin']) ?? 'live',
  }
}

export function feedFromNews(x: LiveNews, busId?: number | null): FeedItem {
  const tk = x.tickers?.[0] ?? null
  const senti = n(x.sentiment_score)
  return {
    uid: `news:${x.item_id}`,
    busId: busId ?? x.live_id ?? null,
    topic: 'news',
    type: s(x.category) ?? 'news',
    ts: s(x.published_at) ?? s(x.first_seen_at),
    title: x.title ?? '(untitled)',
    detail: [s(x.publisher) ?? s(x.source_id), x.tickers?.length ? x.tickers.slice(0, 3).join(', ') : null, x.story_size && x.story_size > 1 ? `${x.story_size} similar` : null].filter(Boolean).join(' · ') || null,
    href: s(x.url),
    link: tk ? `/company/${encodeURIComponent(tk)}` : null,
    securityId: null,
    ticker: tk,
    tone: sign(senti, 0.2),
    modelBased: false,
    origin: (x.origin as FeedItem['origin']) ?? 'live',
  }
}

/** Macro release: actual vs the model-based expectation (never "consensus"). */
export function feedFromMacro(m: LiveMacro, busId?: number | null): FeedItem {
  const z = n(m.signed_z) ?? n(m.surprise_z)
  const fmt = (v: unknown) => (n(v) === null ? '—' : Math.abs(n(v) as number) >= 1000 ? (n(v) as number).toFixed(0) : Number((n(v) as number).toPrecision(4)).toString())
  return {
    uid: `macro:${m.release_event_id}:${m.series_id}`,
    busId: busId ?? m.live_id ?? null,
    topic: 'macro',
    type: 'release',
    ts: s(m.captured_at) ?? s(m.scheduled_at),
    title: `${m.country ?? ''} ${m.release_name ?? m.series_id}`.trim(),
    detail: `actual ${fmt(m.actual)} vs model ${fmt(m.expectation)}${z === null ? '' : ` · surprise z ${z > 0 ? '+' : z < 0 ? '−' : ''}${Math.abs(z).toFixed(1)}`}`,
    href: null,
    link: m.country ? `/macro/country/${encodeURIComponent(m.country)}` : null,
    securityId: null,
    ticker: null,
    tone: sign(z, 0.5),
    modelBased: true,
    origin: (m.origin as FeedItem['origin']) ?? 'live',
  }
}

export function feedFromAlert(a: LiveAlert, busId?: number | null): FeedItem {
  const macro = a.security_id?.startsWith('macro:') ? a.security_id.slice(6) : null
  return {
    uid: `alerts:${a.event_id}`,
    busId: busId ?? a.live_id ?? null,
    topic: 'alerts',
    type: s(a.rule_type) ?? 'alert',
    ts: s(a.fired_at) ?? s(a.as_of_date),
    title: s(a.message) ?? `${(a.rule_type ?? 'alert').replace(/_/g, ' ')} · ${macro ?? a.security_id}`,
    detail: [s(a.rule_type)?.replace(/_/g, ' '), a.live ? 'real-time' : 'nightly'].filter(Boolean).join(' · '),
    href: null,
    link: macro ? `/macro/country/${encodeURIComponent(macro)}` : a.security_id ? `/company/${encodeURIComponent(a.security_id)}` : null,
    securityId: macro ? null : s(a.security_id),
    ticker: null,
    tone: 0,
    modelBased: (a.rule_type ?? '').startsWith('macro_surprise'),
    origin: (a.origin as FeedItem['origin']) ?? 'live',
  }
}

export function feedFromEvent(e: LiveEvent, busId?: number | null): FeedItem {
  return {
    uid: `events:${e.key}`,
    busId: busId ?? e.live_id ?? null,
    topic: 'events',
    type: s(e.kind) ?? 'event',
    ts: s(e.published_at) ?? s(e.first_seen_at),
    title: s(e.title) ?? s(e.domain) ?? e.key,
    detail: [e.themes?.slice(0, 2).join(', '), e.countries?.slice(0, 3).join(', ')].filter(Boolean).join(' · ') || null,
    href: s(e.url),
    link: e.countries?.[0] && e.countries[0] !== 'GLOBAL' ? `/macro/country/${encodeURIComponent(e.countries[0])}` : null,
    securityId: null,
    ticker: null,
    tone: sign(n(e.tone), 2),
    modelBased: false,
    origin: (e.origin as FeedItem['origin']) ?? 'live',
  }
}

/** SSE data frame → feed item (null for topics the feed does not show). */
export function feedFromStream(ev: LiveStreamEvent): FeedItem | null {
  const it = ev.item as Record<string, unknown>
  let f: FeedItem | null
  switch (ev.topic) {
    case 'filings':
      f = feedFromFiling(it as unknown as LiveFiling, ev.id)
      break
    case 'news':
      f = feedFromNews(it as unknown as LiveNews, ev.id)
      break
    case 'macro':
      f = feedFromMacro(it as unknown as LiveMacro, ev.id)
      break
    case 'alerts':
      f = feedFromAlert(it as unknown as LiveAlert, ev.id)
      break
    case 'events':
      f = feedFromEvent(it as unknown as LiveEvent, ev.id)
      break
    default:
      return null
  }
  // the bus key is the authoritative idempotency key
  return {
    ...f,
    uid: `${ev.topic}:${ev.key}`,
    busId: ev.id,
    ts: f.ts ?? ev.ts_source ?? ev.ts_ingest,
    origin: 'stream',
  }
}

/** Merge new items into the feed: dedupe by uid (newest wins), newest first, bounded. */
export function mergeFeed(prev: FeedItem[], incoming: FeedItem[], max = 300): FeedItem[] {
  const seen = new Set<string>()
  const out: FeedItem[] = []
  for (const x of [...incoming, ...prev]) {
    if (seen.has(x.uid)) continue
    seen.add(x.uid)
    out.push(x)
  }
  out.sort((a, b) => (b.ts ?? '').localeCompare(a.ts ?? '') || (b.busId ?? 0) - (a.busId ?? 0))
  return out.slice(0, max)
}

/** Stream URL: topics + resume id (EventSource cannot set Last-Event-ID itself on a manual reconnect). */
export function streamUrl(base: string, topics: readonly string[], lastEventId: number | null, heartbeatS = 15): string {
  const u = new URL(`${base}/live/stream`)
  u.searchParams.set('topics', topics.join(','))
  u.searchParams.set('heartbeat_s', String(heartbeatS))
  if (lastEventId !== null) u.searchParams.set('last_event_id', String(lastEventId))
  return u.toString()
}

/** Health tone of one stream status for the System page. */
export function streamTone(status: string): 'up' | 'warn' | 'down' | 'neutral' {
  if (status === 'ok' || status === 'idle') return 'up'
  if (status === 'starting' || status === 'backoff') return 'warn'
  if (status === 'failed') return 'down'
  return 'neutral'
}
