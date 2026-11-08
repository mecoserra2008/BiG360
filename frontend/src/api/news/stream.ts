/**
 * News Terminal live store: the REST seed (GET /news/terminal) + a dedicated EventSource on the SSE topics
 * news.item / news.story (opened only while the terminal is mounted; released when the page is hidden, resumed from
 * the last event id - same lifecycle as the app-wide stream). Incoming frames are buffered and applied once per
 * animation frame (one merge + one React commit per frame however busy the feed is): the render budget is < 100 ms
 * per live update, measured in `perf` (and by the QA smoke suite through window.__BIG360_NEWS_PERF__).
 */
import { create } from 'zustand'
import { API_BASE_URL, API_PREFIX, USE_MOCK } from '../http'
import { bindPageLifecycle } from '../live/store'
import { type NewsStory, type TermRow, mergeRows, rowFromStream } from './types'

export type NewsConn = 'off' | 'connecting' | 'open' | 'reconnecting' | 'error'

export interface NewsPerf {
  /** navigation start -> first rows painted (ms) */
  firstRowsMs: number | null
  /** recent live-update render times (frame flush -> commit painted, ms) */
  updates: number[]
}

interface NewsLiveState {
  conn: NewsConn
  rows: TermRow[]
  lastEventId: number | null
  received: number
  stories: Record<string, NewsStory>
  /** set at each flush; the page reports the commit time against it */
  flushAt: number | null
  perf: NewsPerf
  seed: (rows: TermRow[], headId: number | null) => void
  appendOlder: (rows: TermRow[]) => void
  push: (rows: TermRow[]) => void
  reportRender: (ms: number) => void
}

export const useNewsLive = create<NewsLiveState>()((set) => ({
  conn: 'off',
  rows: [],
  lastEventId: null,
  received: 0,
  stories: {},
  flushAt: null,
  perf: { firstRowsMs: null, updates: [] },
  seed: (rows, headId) => set((s) => ({ rows: mergeRows(s.rows.filter((r) => r.arrivedAt !== null), rows), lastEventId: s.lastEventId ?? headId })),
  appendOlder: (rows) => set((s) => ({ rows: mergeRows(s.rows, rows) })),
  push: (rows) => set((s) => ({ rows: mergeRows(s.rows, rows), flushAt: performance.now() })),
  reportRender: (ms) => set((s) => ({ perf: { ...s.perf, updates: [...s.perf.updates, Math.round(ms * 10) / 10].slice(-50) } })),
}))

const BASE = `${API_BASE_URL}${API_PREFIX}`
const raf: (cb: () => void) => void = typeof requestAnimationFrame === 'function' ? (cb) => requestAnimationFrame(() => cb()) : (cb) => setTimeout(cb, 16)

export function newsStreamUrl(lastEventId: number | null): string {
  const u = new URL(`${BASE}/live/stream`)
  u.searchParams.set('topics', 'news.item,news.story')
  u.searchParams.set('heartbeat_s', '15')
  if (lastEventId !== null) u.searchParams.set('last_event_id', String(lastEventId))
  return u.toString()
}

export class NewsStream {
  private es: EventSource | null = null
  private buf: TermRow[] = []
  private storyBuf: Record<string, NewsStory> = {}
  private scheduled = false
  private retry = 0
  private timer: ReturnType<typeof setTimeout> | null = null
  private unbind: (() => void) | null = null
  private started = false
  private suspended = false

  start(): void {
    if (USE_MOCK || this.started) return
    this.started = true
    if (typeof window !== 'undefined' && typeof document !== 'undefined')
      this.unbind = bindPageLifecycle(window, document, { suspend: () => this.suspend(), resume: () => this.resume() })
    this.open()
  }

  stop(): void {
    this.started = false
    this.unbind?.()
    this.unbind = null
    this.close()
    useNewsLive.setState({ conn: 'off' })
  }

  private suspend(): void {
    if (!this.started) return
    this.suspended = true
    this.close()
  }

  private resume(): void {
    if (!this.suspended) return
    this.suspended = false
    if (this.started) this.open()
  }

  private close(): void {
    if (this.timer) clearTimeout(this.timer)
    this.timer = null
    this.es?.close()
    this.es = null
  }

  /** Buffer + one flush per animation frame. */
  enqueue(rows: TermRow[], stories: NewsStory[] = []): void {
    this.buf.push(...rows)
    for (const s of stories) this.storyBuf[s.story_id] = s
    if (this.scheduled) return
    this.scheduled = true
    raf(() => this.flush())
  }

  flush(): void {
    this.scheduled = false
    const rows = this.buf
    const stories = this.storyBuf
    this.buf = []
    this.storyBuf = {}
    if (rows.length) useNewsLive.getState().push(rows)
    if (Object.keys(stories).length) useNewsLive.setState((s) => ({ stories: { ...s.stories, ...stories } }))
  }

  private open(): void {
    this.close()
    const st = useNewsLive.getState()
    useNewsLive.setState({ conn: st.lastEventId === null ? 'connecting' : 'reconnecting' })
    let es: EventSource
    try {
      es = new EventSource(newsStreamUrl(st.lastEventId))
    } catch {
      useNewsLive.setState({ conn: 'error' })
      return
    }
    this.es = es
    es.onopen = () => {
      this.retry = 0
      useNewsLive.setState({ conn: 'open' })
    }
    es.addEventListener('heartbeat', (e) => {
      try {
        const h = JSON.parse((e as MessageEvent<string>).data) as { last_event_id: number }
        useNewsLive.setState((s) => ({ lastEventId: Math.max(s.lastEventId ?? 0, h.last_event_id) }))
      } catch {
        /* ignore */
      }
    })
    es.addEventListener('news.item', (e) => {
      try {
        const ev = JSON.parse((e as MessageEvent<string>).data) as { id: number; item: Record<string, unknown> }
        const r = rowFromStream(ev.item, Date.now())
        useNewsLive.setState((s) => ({ lastEventId: Math.max(s.lastEventId ?? 0, ev.id), received: s.received + 1 }))
        if (r) this.enqueue([{ ...r, live_id: ev.id }])
      } catch {
        /* malformed frame */
      }
    })
    es.addEventListener('news.story', (e) => {
      try {
        const ev = JSON.parse((e as MessageEvent<string>).data) as { id: number; item: NewsStory }
        useNewsLive.setState((s) => ({ lastEventId: Math.max(s.lastEventId ?? 0, ev.id) }))
        if (ev.item?.story_id) this.enqueue([], [ev.item])
      } catch {
        /* malformed frame */
      }
    })
    es.onerror = () => {
      if (this.es !== es) return
      if (es.readyState === EventSource.CLOSED) {
        this.es = null
        const delay = Math.min(30_000, 3000 * 2 ** this.retry++)
        useNewsLive.setState({ conn: 'error' })
        this.timer = setTimeout(() => {
          if (this.started && !this.suspended) this.open()
        }, delay)
      } else useNewsLive.setState({ conn: 'reconnecting' })
    }
  }
}

export const newsStream = new NewsStream()

/** QA hook: inject n synthetic rows through the live path, resolve with the measured render time (ms). */
export function simulateLiveUpdate(n = 5): Promise<number> {
  const now = Date.now()
  const iso = new Date(now).toISOString()
  const rows: TermRow[] = Array.from({ length: n }, (_, i) => ({
    origin: 'live',
    item_key: `__perf_${now}_${i}`,
    kind: 'news',
    tier: 1,
    title: `Synthetic live update ${i + 1} (QA latency probe)`,
    tickers: [],
    security_ids: [],
    issuer_ids: [],
    countries: [],
    sectors: [],
    themes: [],
    story_size: 1,
    story_sources: 1,
    first_seen_at: iso,
    published_at: iso,
    arrivedAt: now,
    synthetic: true,
  }))
  const before = useNewsLive.getState().perf.updates.length
  return new Promise((resolve) => {
    newsStream.enqueue(rows)
    const t0 = performance.now()
    const check = () => {
      const p = useNewsLive.getState().perf.updates
      if (p.length > before) {
        // remove the synthetic rows again
        useNewsLive.setState((s) => ({ rows: s.rows.filter((r) => !r.synthetic) }))
        resolve(p[p.length - 1])
      } else if (performance.now() - t0 > 5000) resolve(-1)
      else setTimeout(check, 10)
    }
    setTimeout(check, 10)
  })
}
