import { Keyboard, Layers, Pause, Play, RotateCw, Star } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate, useSearchParams } from 'react-router'
import { useEvTaxonomy } from '@/api/events/hooks'
import { prefetchFirstPage, useNewsHealth, useNewsStories, useTerminalSeed } from '@/api/news/hooks'
import { newsStream, simulateLiveUpdate, useNewsLive } from '@/api/news/stream'
import { EMPTY_FILTER, type NewsStory, type TermFilter, type TermRow, filterFromParams, groupByStory, matches, moveSel, unreadCount } from '@/api/news/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtInt, fmtRelative } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useWatchlist } from '@/store'
import { NewsAlerts } from './NewsAlerts'
import { StoryPane } from './StoryPane'
import { type ListEntry, TerminalList } from './TerminalList'

const MUTE_KEY = 'big360.news.muted'

// start the first page while React mounts (the route chunk loads before the first render)
if (typeof window !== 'undefined' && window.location.pathname === '/news' && !new URLSearchParams(window.location.search).get('view')) {
  try {
    const p = new URLSearchParams(window.location.search)
    const f = { ...EMPTY_FILTER, ...filterFromParams(p) }
    if (!f.watchlistOnly) prefetchFirstPage({ ...f, watchlistOnly: false, mutedStories: [] })
  } catch {
    /* the seed fetches it */
  }
}
const LANGS = ['en', 'pt', 'es', 'fr', 'de', 'it', 'ja', 'zh', 'ko', 'ru', 'ar']

function loadMuted(): string[] {
  try {
    const v = JSON.parse(globalThis.localStorage?.getItem(MUTE_KEY) ?? '[]')
    return Array.isArray(v) ? v.slice(-500) : []
  } catch {
    return []
  }
}

declare global {
  interface Window {
    __BIG360_NEWS_PERF__?: { firstRowsMs: number | null; updates: number[]; simulate: (n?: number) => Promise<number> }
  }
}

function LiveState({ paused }: { paused: boolean }) {
  const conn = useNewsLive((s) => s.conn)
  const h = useNewsHealth(30_000, 1200)
  const running = h.data?.running
  const label = paused ? 'Paused' : conn === 'open' ? (running === false ? 'Connected · service stopped' : 'LIVE') : conn === 'connecting' ? 'Connecting…' : conn === 'reconnecting' ? 'Reconnecting…' : conn === 'error' ? 'Disconnected · retrying' : 'Off'
  const color = paused ? 'bg-faint' : conn === 'open' ? (running === false ? 'bg-warn' : 'bg-up') : conn === 'error' ? 'bg-down' : 'bg-warn'
  const ov = h.data?.overall
  return (
    <div className="flex flex-wrap items-center gap-3 text-xs" data-testid="news-live-state">
      <span className="inline-flex items-center gap-1.5 font-semibold text-fg" role="status" aria-live="polite">
        <span className={cn('size-2 rounded-full', color, conn === 'open' && !paused && running !== false && 'animate-pulse')} aria-hidden />
        {label}
      </span>
      {ov && ov.publish_to_bus_s_p50 !== null && ov.publish_to_bus_s_p50 !== undefined && (
        <Tip content="Publish -> bus latency (news.item first seen - source publication time) over recent items">
          <span className="num text-muted">
            latency p50 {Math.round(ov.publish_to_bus_s_p50)} s · p95 {Math.round(ov.publish_to_bus_s_p95 ?? 0)} s
          </span>
        </Tip>
      )}
      {h.data && <span className="num text-muted">{fmtInt(h.data.sources.reduce((a, s) => a + s.items_1h, 0))} items / h</span>}
      {h.data?.intraday && (h.data.intraday as { built_at?: string }).built_at && <span className="text-faint">intraday {fmtRelative((h.data.intraday as { built_at: string }).built_at)}</span>}
    </div>
  )
}

function TopStories({ onPick, selected }: { onPick: (s: NewsStory) => void; selected: string | null }) {
  const [sort, setSort] = useState<'salience' | 'velocity' | 'recent' | 'size'>('salience')
  const [hours, setHours] = useState('24')
  const q = useNewsStories({ hours: Number(hours), sort })
  const live = useNewsLive((s) => s.stories)
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
        <Segmented label="Sort" size="xs" value={sort} onChange={setSort} options={[{ value: 'salience', label: 'Salience' }, { value: 'velocity', label: 'Velocity' }, { value: 'size', label: 'Size' }, { value: 'recent', label: 'Recent' }]} />
        <Select label="Window" value={hours} onChange={setHours} options={[{ value: '6', label: '6 h' }, { value: '24', label: '24 h' }, { value: '72', label: '3 d' }, { value: '168', label: '7 d' }]} className="w-20" />
      </div>
      {q.isLoading && <SkeletonRows rows={10} className="p-3" />}
      {q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
      {q.data && q.data.items.length === 0 && <EmptyState title="No stories yet" message="Stories appear as the live service and the hourly fold run." />}
      <ul className="min-h-0 flex-1 overflow-auto" data-testid="news-top">
        {q.data?.items.map((s) => {
          const lv = live[s.story_id]
          const v = lv?.velocity_1h ?? s.velocity_1h
          return (
            <li key={s.story_id}>
              <button type="button" onClick={() => onPick(s)} className={cn('flex w-full items-start gap-2 border-b border-border/60 px-3 py-2 text-left hover:bg-surface-2', selected === s.story_id && 'bg-surface-3')}>
                <span className="num mt-0.5 w-9 shrink-0 text-right text-xs font-semibold text-accent-text">{s.salience ? s.salience.toFixed(1) : '—'}</span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium text-fg">{s.title}</span>
                  <span className="mt-0.5 flex flex-wrap items-center gap-1.5 text-2xs text-muted">
                    <span className="num">
                      {fmtInt(Math.max(s.n_articles, lv?.n_articles ?? 0))} articles · {fmtInt(Math.max(s.n_sources, lv?.n_sources ?? 0))} sources
                    </span>
                    {s.last_ts && <span>last {fmtRelative(s.last_ts)}</span>}
                    {v ? <Badge tone="warn">{v}/h</Badge> : null}
                    {s.securities.slice(0, 3).map((x) => (
                      <span key={x.security_id ?? x.ticker ?? ''} className="rounded-sm bg-surface-3 px-1 font-semibold text-fg">
                        {x.ticker}
                      </span>
                    ))}
                    {s.themes.slice(0, 2).map((t) => (
                      <Badge key={t}>{t.replace(/_/g, ' ')}</Badge>
                    ))}
                  </span>
                </span>
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

export default function NewsTerminal() {
  const loc = useLocation()
  const nav = useNavigate()
  const [params] = useSearchParams()
  const view: 'live' | 'top' = loc.pathname.endsWith('/top') || params.get('view') === 'top' ? 'top' : 'live'
  const tax = useEvTaxonomy()
  const watchIds = useWatchlist((s) => s.ids)
  const [filter, setFilter] = useState<TermFilter>(() => ({ ...EMPTY_FILTER, ...filterFromParams(params), mutedStories: loadMuted() }))
  const [qText, setQText] = useState(filter.q)
  const [tickerText, setTickerText] = useState(filter.tickers.join(','))
  const [grouped, setGrouped] = useState(false)
  const [paused, setPaused] = useState(false)
  const [sel, setSel] = useState(0)
  const [topStory, setTopStory] = useState<NewsStory | null>(null)
  const qc = useQueryClient()
  const [seenAt, setSeenAt] = useState(() => Date.now())
  const [readKeys, setReadKeys] = useState<Set<string>>(() => new Set())
  const [now, setNow] = useState(() => Date.now())
  const searchRef = useRef<HTMLInputElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)
  const q = useDeferredValue(qText)

  // server-side filter (everything but the watchlist and muted stories, which are client-side)
  const serverFilter = useMemo(() => ({ ...filter, q, watchlistOnly: false, mutedStories: [] }), [filter, q])
  const seed = useTerminalSeed(serverFilter)

  useEffect(() => {
    newsStream.start()
    window.__BIG360_NEWS_PERF__ = {
      get firstRowsMs() {
        return useNewsLive.getState().perf.firstRowsMs
      },
      get updates() {
        return useNewsLive.getState().perf.updates
      },
      simulate: (n = 5) => simulateLiveUpdate(n),
    }
    return () => newsStream.stop()
  }, [])
  useEffect(() => {
    if (paused) newsStream.stop()
    else newsStream.start()
  }, [paused])
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    try {
      globalThis.localStorage?.setItem(MUTE_KEY, JSON.stringify(filter.mutedStories.slice(-500)))
    } catch {
      /* ignore */
    }
  }, [filter.mutedStories])

  const rowsAll = useNewsLive((s) => s.rows)
  const f = useMemo(() => ({ ...filter, q, watchSecurityIds: watchIds }), [filter, q, watchIds])
  const rows = useMemo(() => rowsAll.filter((r) => r.synthetic || matches(r, f)), [rowsAll, f])
  const entries: ListEntry[] = useMemo(() => (grouped ? groupByStory(rows).map((g) => ({ row: g.head, group: g })) : rows.map((row) => ({ row }))), [rows, grouped])
  const unread = useMemo(() => unreadCount(rows, seenAt) - rows.filter((r) => r.arrivedAt !== null && r.arrivedAt > seenAt && readKeys.has(r.item_key)).length, [rows, seenAt, readKeys])
  const selIdx = entries.length ? Math.min(Math.max(sel, 0), entries.length - 1) : -1
  const cur: TermRow | null = selIdx >= 0 ? (entries[selIdx]?.row ?? null) : null
  const storyId = view === 'top' ? (topStory?.story_id ?? null) : (cur?.story_id ?? null)
  const select = useCallback(
    (i: number) => {
      setSel(i)
      const r = entries[i]?.row
      if (r) setReadKeys((s) => (s.has(r.item_key) ? s : new Set(s).add(r.item_key)))
    },
    [entries],
  )

  const mute = useCallback((id: string) => setFilter((x) => ({ ...x, mutedStories: [...new Set([...x.mutedStories, id])] })), [])
  const applyTickers = () => setFilter((x) => ({ ...x, tickers: tickerText.split(/[\s,]+/).map((t) => t.trim().toUpperCase()).filter(Boolean) }))

  // keyboard: j / k move, / filter, w watchlist only, o open, s mute story, Enter detail, Esc leave the filter
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      const typing = !!t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)
      if (typing) {
        if (e.key === 'Escape') t?.blur()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === 'j' || e.key === 'ArrowDown') {
        e.preventDefault()
        select(moveSel(selIdx, entries.length, 1))
      } else if (e.key === 'k' || e.key === 'ArrowUp') {
        e.preventDefault()
        select(moveSel(selIdx, entries.length, -1))
      } else if (e.key === '/') {
        e.preventDefault()
        searchRef.current?.focus()
      } else if (e.key === 'w') {
        setFilter((x) => ({ ...x, watchlistOnly: !x.watchlistOnly }))
      } else if (e.key === 'o' && cur?.url) {
        window.open(cur.url, '_blank', 'noopener,noreferrer')
      } else if (e.key === 's' && cur?.story_id) {
        mute(cur.story_id)
      } else if (e.key === 'Enter' && cur) {
        detailRef.current?.focus()
        detailRef.current?.scrollIntoView({ block: 'nearest' })
      } else if (e.key === 'g') {
        setGrouped((g) => !g)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [entries.length, cur, mute, select, selIdx])

  const themeOpts = [{ value: '', label: 'All themes' }, ...(tax.data?.themes ?? []).map((t) => ({ value: t.id, label: t.name }))]
  const sectorOpts = [{ value: '', label: 'All sectors' }, ...(tax.data?.sectors ?? []).map((s) => ({ value: s.id, label: s.name }))]
  const tierOpts = [
    { value: '', label: 'All sources' },
    { value: '1', label: 'Official' },
    { value: '1,2', label: 'Official + major' },
    { value: '1,2,3', label: 'No GDELT web' },
    { value: '4', label: 'GDELT web only' },
  ]
  const langOpts = [{ value: '', label: 'All languages' }, ...LANGS.map((l) => ({ value: l, label: l.toUpperCase() }))]

  return (
    <div className="flex flex-col" style={{ height: 'calc(100dvh - 7.5rem)', minHeight: 520 }}>
      <PageHeader
        title="News Terminal"
        subtitle="Live headlines (official, regulators, exchanges, publishers, Polygon, GDELT 15-min) grouped into stories · sentiment is model-based"
        actions={
          <>
            <LiveState paused={paused} />
            <Tip content={paused ? 'Resume the live stream (missed items are replayed)' : 'Pause the live stream'}>
              <Button size="icon-sm" variant="ghost" aria-label={paused ? 'Resume' : 'Pause'} aria-pressed={paused} onClick={() => setPaused((p) => !p)}>
                {paused ? <Play /> : <Pause />}
              </Button>
            </Tip>
            <NewsAlerts />
          </>
        }
      />
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <Segmented label="View" size="xs" value={view} onChange={(v) => nav(v === 'top' ? '/news/top' : '/news')} options={[{ value: 'live', label: 'Live' }, { value: 'top', label: 'Top stories' }]} />
        {view === 'live' && (
          <>
            <Input ref={searchRef} aria-label="Filter headlines" placeholder="/ filter headlines" className="w-48" value={qText} onChange={(e) => setQText(e.target.value)} />
            <Input
              aria-label="Tickers"
              placeholder="Tickers (AAPL,MSFT)"
              className="w-36"
              value={tickerText}
              onChange={(e) => setTickerText(e.target.value)}
              onBlur={applyTickers}
              onKeyDown={(e) => {
                if (e.key === 'Enter') applyTickers()
              }}
            />
            <Tip content="Watchlist names only (w)">
              <Button size="xs" variant={filter.watchlistOnly ? 'primary' : 'outline'} aria-pressed={filter.watchlistOnly} onClick={() => setFilter((x) => ({ ...x, watchlistOnly: !x.watchlistOnly }))}>
                <Star /> Watchlist
              </Button>
            </Tip>
            <Input aria-label="Countries" placeholder="Country (US,PT)" className="w-28" defaultValue={filter.countries.join(',')} onBlur={(e) => setFilter((x) => ({ ...x, countries: e.target.value.split(/[\s,]+/).map((c) => c.trim().toUpperCase()).filter(Boolean) }))} />
            <Select label="Sector" value={filter.sectors[0] ?? ''} onChange={(v) => setFilter((x) => ({ ...x, sectors: v ? [v] : [] }))} options={sectorOpts} className="w-36" />
            <Select label="Theme" value={filter.themes[0] ?? ''} onChange={(v) => setFilter((x) => ({ ...x, themes: v ? [v] : [] }))} options={themeOpts} className="w-40" />
            <Select label="Source tier" value={filter.tiers.join(',')} onChange={(v) => setFilter((x) => ({ ...x, tiers: v ? v.split(',').map(Number) : [] }))} options={tierOpts} className="w-36" />
            <Select label="Language" value={filter.langs[0] ?? ''} onChange={(v) => setFilter((x) => ({ ...x, langs: v ? [v] : [] }))} options={langOpts} className="w-32" />
            <Tip content="Group by story (g)">
              <Button size="xs" variant={grouped ? 'primary' : 'outline'} aria-pressed={grouped} onClick={() => setGrouped((g) => !g)}>
                <Layers /> Stories
              </Button>
            </Tip>
            {filter.mutedStories.length > 0 && (
              <Button size="xs" variant="ghost" onClick={() => setFilter((x) => ({ ...x, mutedStories: [] }))} title="Show muted stories again">
                {filter.mutedStories.length} muted · unmute
              </Button>
            )}
          </>
        )}
      </div>
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Panel
          flush
          className="min-h-[320px]"
          bodyClassName="flex min-h-0 flex-col"
          title={view === 'top' ? 'Top stories' : grouped ? 'Stories (live)' : 'Live feed'}
          subtitle={
            view === 'top'
              ? 'Ranked by salience / velocity over the window'
              : `${fmtInt(entries.length)} ${grouped ? 'stories' : 'items'}${seed.loading ? ' · loading…' : !seed.done ? ' · scroll for older' : ''}`
          }
          actions={
            view === 'live' ? (
              <>
                {unread > 0 && (
                  <Button size="xs" variant="outline" onClick={() => setSeenAt(Date.now())} title="Mark all as read">
                    {unread} new
                  </Button>
                )}
                <Tip content="j / k move · / filter · w watchlist · o open · s mute story · g group · Enter detail">
                  <span className="inline-flex items-center gap-1 text-2xs text-faint">
                    <Keyboard className="size-3.5" aria-hidden /> keys
                  </span>
                </Tip>
              </>
            ) : (
              <Button
                size="icon-sm"
                variant="ghost"
                aria-label="Refresh top stories"
                title="Reload the top stories"
                onClick={() => {
                  setTopStory(null)
                  void qc.invalidateQueries({ queryKey: ['news', 'stories'] })
                }}
              >
                <RotateCw />
              </Button>
            )
          }
        >
          {view === 'top' ? (
            <TopStories onPick={setTopStory} selected={topStory?.story_id ?? null} />
          ) : seed.loading && entries.length === 0 ? (
            <SkeletonRows rows={14} className="p-3" />
          ) : seed.error && entries.length === 0 ? (
            <ErrorState error={seed.error} />
          ) : entries.length === 0 ? (
            <EmptyState title="No news matches" message="Widen the filters, or wait for the live feed (the realtime service must be running)." />
          ) : (
            <TerminalList entries={entries} sel={selIdx} onSelect={select} seenAt={seenAt} readKeys={readKeys} now={now} onEndReached={() => void seed.more()} />
          )}
        </Panel>
        <Panel flush className="min-h-[320px]" bodyClassName="flex min-h-0 flex-col" title="Detail" subtitle={storyId ? `story ${storyId}` : 'item'}>
          <div ref={detailRef} tabIndex={-1} className="flex min-h-0 flex-1 flex-col focus-visible:outline-none">
            <StoryPane storyId={storyId} item={view === 'top' ? null : cur} onMute={mute} />
          </div>
        </Panel>
      </div>
    </div>
  )
}
