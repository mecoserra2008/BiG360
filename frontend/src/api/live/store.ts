/**
 * Live stream controller + store: one EventSource for the app (topics filings / news / macro / alerts),
 * resume by last event id (browser Last-Event-ID on its own retries; ?last_event_id on manual reconnects
 * and after a pause), exponential backoff when the server closes the stream, pause / unpause.
 */
import { create } from 'zustand'
import { API_BASE_URL, API_PREFIX, USE_MOCK } from '../http'
import { liveApi } from './client'
import { type LiveMarketSession, type LiveQuotesTick, useQuoteStore } from './quotes'
import { FEED_TOPICS, type FeedItem, type LiveHello, type LiveHeartbeat, type LiveStreamEvent, STREAM_TOPICS, feedFromAlert, feedFromFiling, feedFromMacro, feedFromNews, feedFromStream, mergeFeed, streamUrl } from './types'

export type LiveConn = 'off' | 'connecting' | 'open' | 'reconnecting' | 'paused' | 'error'

interface LiveState {
  conn: LiveConn
  paused: boolean
  lastEventId: number | null
  lastBeatAt: string | null
  /** stream frames received this session */
  received: number
  /** items received from the stream since the feed was last opened */
  unseen: number
  feed: FeedItem[]
  seeded: boolean
  /** alerts that arrived on the stream, for toasts (consumed by the bridge) */
  toastQueue: FeedItem[]
  setPaused: (p: boolean) => void
  markSeen: () => void
}

export const useLive = create<LiveState>()((set) => ({
  conn: USE_MOCK ? 'off' : 'connecting',
  paused: false,
  lastEventId: null,
  lastBeatAt: null,
  received: 0,
  unseen: 0,
  feed: [],
  seeded: false,
  toastQueue: [],
  setPaused: (p) => {
    set({ paused: p })
    if (p) controller.pause()
    else controller.resume()
  },
  markSeen: () => set({ unseen: 0 }),
}))

const BASE = `${API_BASE_URL}${API_PREFIX}`

/** A tab hidden this long releases its stream (resumed with the last event id when it is shown again). */
export const HIDDEN_RELEASE_MS = 60_000

/**
 * Perf pass: release the EventSource's HTTP/1.1 connection whenever the page cannot use it. Browsers allow 6
 * connections per host; a document kept in the back/forward cache (or a background tab) that still holds its
 * stream blocks one of them, and a handful of such documents queue every other API request of the visible page
 * until the browser evicts them (~50 s stalls on full page loads). `pagehide` -> suspend at once, `pageshow`
 * (restored from the bfcache) -> resume; hidden for `hiddenMs` -> suspend, visible again -> resume.
 */
export function bindPageLifecycle(
  win: EventTarget,
  doc: EventTarget & { hidden: boolean },
  h: { suspend: () => void; resume: () => void },
  hiddenMs = HIDDEN_RELEASE_MS,
): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null
  const clear = () => {
    if (timer) clearTimeout(timer)
    timer = null
  }
  const onHide = () => {
    clear()
    h.suspend()
  }
  const onShow = (e: Event) => {
    if (!(e as Event & { persisted?: boolean }).persisted) return
    clear()
    h.resume()
  }
  const onVisibility = () => {
    clear()
    if (doc.hidden) timer = setTimeout(() => h.suspend(), hiddenMs)
    else h.resume()
  }
  win.addEventListener('pagehide', onHide)
  win.addEventListener('pageshow', onShow)
  doc.addEventListener('visibilitychange', onVisibility)
  return () => {
    clear()
    win.removeEventListener('pagehide', onHide)
    win.removeEventListener('pageshow', onShow)
    doc.removeEventListener('visibilitychange', onVisibility)
  }
}

export class LiveController {
  private es: EventSource | null = null
  private retry = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private started = false
  /** stream released by the page lifecycle (hidden / bfcache), reopened by resumeVisible() */
  private suspended = false
  private unbind: (() => void) | null = null

  start(): void {
    if (USE_MOCK || this.started) return
    this.started = true
    this.suspended = false
    if (typeof window !== 'undefined' && typeof document !== 'undefined')
      this.unbind = bindPageLifecycle(window, document, { suspend: () => this.suspend(), resume: () => this.resumeVisible() })
    void this.seed()
    if (!useLive.getState().paused) this.open()
  }

  stop(): void {
    this.started = false
    this.unbind?.()
    this.unbind = null
    this.close()
  }

  /** Release the connection (page hidden / entering the bfcache); no-op when nothing is open or scheduled. */
  suspend(): void {
    if (!this.started || (this.es === null && this.timer === null)) return
    this.suspended = true
    this.close()
  }

  /** Reopen after suspend(), resuming from the last event id (missed events are replayed by the server). */
  resumeVisible(): void {
    if (!this.suspended) return
    this.suspended = false
    if (!this.started || useLive.getState().paused) return
    this.retry = 0
    this.open()
  }

  pause(): void {
    this.close()
    useLive.setState({ conn: USE_MOCK ? 'off' : 'paused' })
  }

  resume(): void {
    if (USE_MOCK) return
    this.retry = 0
    this.open()
  }

  private close(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.es?.close()
    this.es = null
  }

  /** Initial feed from the REST pages (snapshot + hot store), so the ticker is useful even when realtime is off. */
  private async seed(): Promise<void> {
    // News Terminal (roadmap v3 N4): its first page goes first (6 connections per host on HTTP/1.1)
    if (typeof window !== 'undefined' && window.location?.pathname?.startsWith('/news')) await new Promise((r) => setTimeout(r, 1500))
    const [f, n, m, a] = await Promise.allSettled([liveApi.filings({ limit: 15 }), liveApi.news(15), liveApi.macro(15), liveApi.alerts(15)])
    const items: FeedItem[] = [
      ...(f.status === 'fulfilled' ? f.value.items.map((x) => feedFromFiling(x)) : []),
      ...(n.status === 'fulfilled' ? n.value.items.map((x) => feedFromNews(x)) : []),
      ...(m.status === 'fulfilled' ? m.value.items.map((x) => feedFromMacro(x)) : []),
      ...(a.status === 'fulfilled' ? a.value.items.map((x) => feedFromAlert(x)) : []),
    ]
    useLive.setState((s) => ({ feed: mergeFeed(s.feed, items), seeded: true }))
  }

  private open(): void {
    this.close()
    const st = useLive.getState()
    useLive.setState({
      conn: st.lastEventId === null ? 'connecting' : 'reconnecting',
    })
    let es: EventSource
    try {
      es = new EventSource(streamUrl(BASE, STREAM_TOPICS, st.lastEventId))
    } catch {
      useLive.setState({ conn: 'error' })
      return
    }
    this.es = es
    es.onopen = () => {
      this.retry = 0
      useLive.setState({ conn: 'open' })
    }
    es.addEventListener('hello', (e) => {
      const h = JSON.parse((e as MessageEvent<string>).data) as LiveHello
      useLive.setState((s) => ({
        conn: 'open',
        lastEventId: s.lastEventId ?? h.last_event_id,
      }))
    })
    es.addEventListener('heartbeat', (e) => {
      const h = JSON.parse((e as MessageEvent<string>).data) as LiveHeartbeat
      useLive.setState((s) => ({
        lastBeatAt: h.server_time,
        lastEventId: Math.max(s.lastEventId ?? 0, h.last_event_id),
      }))
    })
    for (const topic of FEED_TOPICS) {
      es.addEventListener(topic, (e) => {
        let ev: LiveStreamEvent
        try {
          ev = JSON.parse((e as MessageEvent<string>).data) as LiveStreamEvent
        } catch {
          return
        }
        const item = feedFromStream(ev)
        useLive.setState((s) => ({
          lastEventId: Math.max(s.lastEventId ?? 0, ev.id),
          received: s.received + 1,
          unseen: s.unseen + (item ? 1 : 0),
          feed: item ? mergeFeed(s.feed, [item]) : s.feed,
          toastQueue: item && item.topic === 'alerts' ? [...s.toastQueue, item].slice(-5) : s.toastQueue,
        }))
      })
    }
    // workstream B: delayed quotes -> quotes store (never the feed / toasts)
    es.addEventListener('quotes.tick', (e) => {
      try {
        const ev = JSON.parse((e as MessageEvent<string>).data) as LiveStreamEvent
        useQuoteStore.getState().tick(ev.item as unknown as LiveQuotesTick)
        useLive.setState((s) => ({ lastEventId: Math.max(s.lastEventId ?? 0, ev.id) }))
      } catch {
        /* malformed frame: ignore */
      }
    })
    es.addEventListener('quotes.session', (e) => {
      try {
        const ev = JSON.parse((e as MessageEvent<string>).data) as LiveStreamEvent
        useQuoteStore.getState().session(ev.item as unknown as LiveMarketSession)
        useLive.setState((s) => ({ lastEventId: Math.max(s.lastEventId ?? 0, ev.id) }))
      } catch {
        /* malformed frame: ignore */
      }
    })
    es.onerror = () => {
      if (this.es !== es) return
      if (es.readyState === EventSource.CLOSED) {
        // server closed / unreachable: reconnect ourselves with the resume id and backoff
        this.es = null
        const delay = Math.min(30_000, 3000 * 2 ** this.retry++)
        useLive.setState({ conn: 'error' })
        this.timer = setTimeout(() => {
          if (!useLive.getState().paused && this.started && !this.suspended) this.open()
        }, delay)
      } else {
        // the browser retries itself (retry: 3000) and sends Last-Event-ID
        useLive.setState({ conn: 'reconnecting' })
      }
    }
  }
}

export const controller = new LiveController()
