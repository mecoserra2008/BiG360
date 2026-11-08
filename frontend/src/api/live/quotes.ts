/**
 * Delayed quotes (workstream B, contract v1.40.0): GET /live/quotes seeds, SSE `quotes.tick` / `quotes.session`
 * frames update one app-wide store. Pure adapters are unit-tested in __tests__/quotes.test.ts.
 * Quotes are DELAYED (0-20 min by exchange) and never replace the official close of the daily bars.
 */
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { create } from 'zustand'
import type { components } from '../generated/schema'
import { USE_MOCK } from '../http'
import { liveApi } from './client'

type S = components['schemas']
export type LiveQuote = S['LiveQuote']
export type LiveQuotesPage = S['LiveQuotesPage']
export type LiveQuotesService = S['LiveQuotesService']
export type LiveQuotesTick = S['LiveQuotesTick']
export type LiveMarketSession = S['LiveMarketSession']
export type LiveFeeds = S['LiveFeeds']
export type LiveFeedHealth = S['LiveFeedHealth']
type TickQuote = LiveQuotesTick['quotes'][number]

export interface QuoteView {
  securityId: string
  price: number
  change: number | null
  /** fraction (0.012 = +1.2%) vs the previous official close */
  changePct: number | null
  prevClose: number | null
  currency: string | null
  market: string | null
  /** last print time at the source (ISO) */
  quoteTs: string | null
  fetchedAt: string | null
  marketOpen: boolean
  marketReason: string | null
  nextOpenAt: string | null
  stale: boolean
  source: string | null
  delayMin: number | null
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null)

export function quoteView(q: LiveQuote): QuoteView {
  return {
    securityId: q.security_id,
    price: q.price,
    change: num(q.change),
    changePct: num(q.change_pct),
    prevClose: num(q.prev_close),
    currency: str(q.currency),
    market: str(q.market),
    quoteTs: str(q.quote_ts),
    fetchedAt: str(q.fetched_at),
    marketOpen: !!q.market_open,
    marketReason: str(q.market_reason),
    nextOpenAt: str(q.next_open_at),
    stale: !!q.stale,
    source: str(q.source),
    delayMin: num(q.delay_min),
  }
}

/** One quote of an SSE quotes.tick frame -> view (market state carried over from the previous view). */
export function quoteFromTick(t: TickQuote, prev?: QuoteView | null, receivedAt?: string): QuoteView {
  return {
    securityId: t.security_id,
    price: t.price,
    change: num(t.change),
    changePct: num(t.change_pct),
    prevClose: num(t.prev_close) ?? prev?.prevClose ?? null,
    currency: str(t.currency) ?? prev?.currency ?? null,
    market: str(t.market) ?? prev?.market ?? null,
    quoteTs: str(t.quote_ts) ?? prev?.quoteTs ?? null,
    fetchedAt: receivedAt ?? prev?.fetchedAt ?? null,
    marketOpen: prev?.marketOpen ?? true,
    marketReason: prev?.marketReason ?? 'open',
    nextOpenAt: prev?.nextOpenAt ?? null,
    stale: false,
    source: str(t.source) ?? prev?.source ?? null,
    delayMin: num(t.delay_min) ?? prev?.delayMin ?? null,
  }
}

/** Apply a quotes.tick frame to the quote map (pure). */
export function applyTick(prev: Record<string, QuoteView>, tick: LiveQuotesTick): Record<string, QuoteView> {
  const out = { ...prev }
  for (const t of tick.quotes ?? []) out[t.security_id] = quoteFromTick(t, prev[t.security_id], tick.fetched_at ?? undefined)
  return out
}

/** Apply a quotes.session frame (a market opened / closed) to every quote of that market (pure). */
export function applySession(prev: Record<string, QuoteView>, s: LiveMarketSession): Record<string, QuoteView> {
  const out: Record<string, QuoteView> = {}
  for (const [k, v] of Object.entries(prev))
    out[k] = v.market === s.market ? { ...v, marketOpen: !!s.open, marketReason: s.reason ?? null, nextOpenAt: s.opens_at ?? null, stale: false } : v
  return out
}

function hhmm(iso: string, timeZone?: string, now: Date = new Date()): string {
  const d = new Date(iso)
  const sameDay = d.toDateString() === now.toDateString()
  const opts: Intl.DateTimeFormatOptions = sameDay
    ? { hour: '2-digit', minute: '2-digit', hour12: false, timeZone }
    : { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone }
  return new Intl.DateTimeFormat('en-GB', opts).format(d)
}

/** "delayed · 14:32" (print time in the viewer's time zone; date added when not today). */
export function delayedLabel(v: QuoteView | null, timeZone?: string, now: Date = new Date()): string {
  if (!v) return 'delayed'
  const ts = v.quoteTs ?? v.fetchedAt
  return ts ? `delayed · ${hhmm(ts, timeZone, now)}` : 'delayed'
}

export const MARKET_REASON: Record<string, string> = {
  open: 'market open',
  closing_print: 'after the close (closing print)',
  before_open: 'market not open yet',
  after_close: 'market closed',
  closed_weekend: 'closed (weekend)',
  closed_holiday: 'closed (holiday)',
  daily_break: 'daily break',
}

/** Tooltip text: what this number is and is not. */
export function quoteTitle(v: QuoteView, timeZone?: string, now: Date = new Date()): string {
  const src = v.source === 'polygon' ? 'Polygon' : v.source === 'yahoo' ? 'Yahoo' : (v.source ?? 'source')
  const parts = [
    `Delayed quote (${src}${v.delayMin ? `, ~${v.delayMin} min` : ''})`,
    v.quoteTs ? `last print ${hhmm(v.quoteTs, timeZone, now)}` : null,
    v.fetchedAt ? `fetched ${hhmm(v.fetchedAt, timeZone, now)}` : null,
    v.marketReason ? (MARKET_REASON[v.marketReason] ?? v.marketReason) : null,
    !v.marketOpen && v.nextOpenAt ? `opens ${hhmm(v.nextOpenAt, timeZone, now)}` : null,
    v.stale ? 'no fresh quote for 15 min' : null,
    'not the official close',
  ]
  return parts.filter(Boolean).join(' · ')
}

/** Tone of the live tag: up = open & fresh, warn = stale, neutral = market closed. */
export function quoteTone(v: QuoteView | null): 'up' | 'warn' | 'neutral' {
  if (!v) return 'neutral'
  if (v.stale) return 'warn'
  return v.marketOpen ? 'up' : 'neutral'
}

/** Header LIVE strip: quotes service state -> short label + tone. */
export function serviceChip(s: LiveQuotesService | null | undefined): { label: string; tone: 'up' | 'warn' | 'down' | 'neutral'; title: string } {
  if (!s) return { label: 'Quotes …', tone: 'neutral', title: 'Checking the quotes service…' }
  const reason = s.reason ?? ''
  switch (s.state) {
    case 'ok':
      return { label: 'Quotes live', tone: 'up', title: `Delayed quotes refreshing (${s.markets_open ?? 0} market sessions open, ${s.n_securities ?? 0} securities)` }
    case 'idle':
      return { label: 'Quotes idle', tone: 'neutral', title: `Delayed quotes idle: ${reason || 'all markets closed'}` }
    case 'source_down':
      return { label: 'Quotes source down', tone: 'warn', title: `Delayed quotes: ${reason || 'source down, backing off'}` }
    case 'off':
      return { label: 'Quotes off', tone: 'neutral', title: reason || 'Switched off in the tray (Live quotes on/off)' }
    case 'not_started':
      return { label: 'Quotes not started', tone: 'neutral', title: reason || 'The quotes service starts with the app' }
    default:
      return { label: 'Quotes stopped', tone: 'down', title: reason || 'The quotes service is not running' }
  }
}

/** Live feeds panel: feed status -> tone. */
export function feedTone(status: string): 'up' | 'warn' | 'down' | 'neutral' {
  if (status === 'ok') return 'up'
  if (status === 'idle' || status === 'disabled') return 'neutral'
  if (status === 'backoff' || status === 'starting') return 'warn'
  return 'down'
}

/** seconds -> "45 s" / "3.2 min" / "2.1 h" (latency columns). */
export function fmtLatency(s: number | null | undefined): string {
  if (typeof s !== 'number' || !Number.isFinite(s)) return '—'
  if (s < 90) return `${Math.round(s)} s`
  if (s < 5400) return `${(s / 60).toFixed(1)} min`
  return `${(s / 3600).toFixed(1)} h`
}

// ------------------------------------------------------------------------------------------------ store + hook
interface QuoteState {
  quotes: Record<string, QuoteView>
  /** requested id (ticker, ^GSPC, ...) -> security id */
  req: Record<string, string>
  service: LiveQuotesService | null
  upsert: (p: LiveQuotesPage) => void
  tick: (t: LiveQuotesTick) => void
  session: (s: LiveMarketSession) => void
}

export const useQuoteStore = create<QuoteState>()((set) => ({
  quotes: {},
  req: {},
  service: null,
  upsert: (p) =>
    set((s) => {
      const quotes = { ...s.quotes }
      const req = { ...s.req }
      for (const q of p.items ?? []) {
        quotes[q.security_id] = quoteView(q)
        req[q.id] = q.security_id
      }
      return { quotes, req, service: p.service ?? s.service }
    }),
  tick: (t) => set((s) => ({ quotes: applyTick(s.quotes, t) })),
  session: (m) => set((s) => ({ quotes: applySession(s.quotes, m) })),
}))

/**
 * Live delayed quotes for ids (security ids, tickers or ^GSPC-style symbols); `set: 'header'` adds the LIVE strip
 * set; record=false does not register the ids for on-demand polling (long lists). Polls /live/quotes every 60 s (the service polls the sources every 1-5 min while markets are open); SSE
 * quotes.tick frames update the same store in between. get(id) -> QuoteView | null.
 */
export function useLiveQuotes(ids: ReadonlyArray<string | null | undefined>, opts: { set?: 'header'; enabled?: boolean; record?: boolean } = {}) {
  const key = [...new Set(ids.filter((x): x is string => !!x))].sort().join(',')
  const want = useMemo(() => (key ? key.split(',') : []), [key])
  const q = useQuery({
    queryKey: ['live', 'quotes', opts.set ?? '', key, opts.record ?? true],
    queryFn: async () => {
      const p = await liveApi.quotes(want, opts.set, opts.record ?? true)
      useQuoteStore.getState().upsert(p)
      return p
    },
    enabled: !USE_MOCK && (opts.enabled ?? true) && (want.length > 0 || !!opts.set),
    refetchInterval: 60_000,
    staleTime: 20_000,
    retry: false,
  })
  const quotes = useQuoteStore((s) => s.quotes)
  const req = useQuoteStore((s) => s.req)
  const service = useQuoteStore((s) => s.service)
  return {
    get: (id: string | null | undefined): QuoteView | null => (id ? (quotes[req[id] ?? id] ?? quotes[`IDX:${id}`] ?? null) : null),
    page: q.data,
    service: q.data?.service ?? service,
    isLoading: q.isLoading,
    isError: q.isError,
  }
}

/** One quote from the store (subscribe in a cell; the table / page calls useLiveQuotes once to fetch). */
export function useQuote(id: string | null | undefined): QuoteView | null {
  return useQuoteStore((s) => (id ? (s.quotes[s.req[id] ?? id] ?? s.quotes[`IDX:${id}`] ?? null) : null))
}
