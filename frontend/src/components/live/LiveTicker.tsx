import * as Dialog from '@radix-ui/react-dialog'
import { useQuery } from '@tanstack/react-query'
import { Bell, FileText, Landmark, Newspaper, Pause, Play, Radio, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { USE_MOCK } from '@/api/http'
import { liveApi } from '@/api/live/client'
import { controller, useLive } from '@/api/live/store'
import type { FeedItem, LiveTopic } from '@/api/live/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Segmented } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { Tip } from '@/components/ui/tooltip'
import { fmtDateTime, fmtRelative } from '@/lib/format'
import { playChime } from '@/lib/sound'
import { cn } from '@/lib/utils'
import { HeaderQuotes, QuotesStatusChip } from './LiveQuote'

const ICON: Record<string, typeof FileText> = {
  filings: FileText,
  news: Newspaper,
  macro: Landmark,
  alerts: Bell,
  events: Radio,
}
const TOPIC_LABEL: Record<string, string> = {
  filings: 'Filing',
  news: 'News',
  macro: 'Macro',
  alerts: 'Alert',
  events: 'Event',
}

/** Starts the app-wide stream once and turns stream alerts into toasts. */
export function LiveBridge() {
  const queue = useLive((s) => s.toastQueue)
  useEffect(() => {
    controller.start()
    return () => controller.stop()
  }, [])
  useEffect(() => {
    if (!queue.length) return
    for (const a of queue) {
      toast({
        tone: 'info',
        title: `Alert · ${a.type.replace(/_/g, ' ')}`,
        message: a.title,
      })
      // roadmap v3 N4.3: optional chime for live news alerts (News Terminal > Alerts > Alert sound)
      if (a.type === 'watchlist_news' || a.type === 'story_velocity') playChime()
    }
    useLive.setState({ toastQueue: [] })
  }, [queue])
  return null
}

function useRealtimeStatus() {
  return useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => liveApi.status(),
    refetchInterval: 30_000,
    staleTime: 15_000,
    enabled: !USE_MOCK,
    retry: false,
  })
}

function ConnDot() {
  const conn = useLive((s) => s.conn)
  const st = useRealtimeStatus()
  const rtOff = st.data && !st.data.running
  const label =
    conn === 'open' ? (rtOff ? 'News stopped · realtime layer not running' : 'LIVE · news') : conn === 'paused' ? 'Paused' : conn === 'reconnecting' ? 'Reconnecting…' : conn === 'connecting' ? 'Connecting…' : conn === 'error' ? 'Disconnected · retrying' : 'Off'
  const color = conn === 'open' ? (rtOff ? 'bg-warn' : 'bg-up') : conn === 'paused' ? 'bg-faint' : conn === 'error' ? 'bg-down' : 'bg-warn'
  return (
    <span className="inline-flex shrink-0 items-center gap-1.5 text-xs font-medium text-fg" role="status" aria-live="polite">
      <span className={cn('size-2 rounded-full', color, conn === 'open' && !rtOff && 'animate-pulse')} aria-hidden />
      {label}
    </span>
  )
}

function itemTime(x: FeedItem): string {
  return x.ts ? fmtRelative(x.ts) : ''
}

function FeedLine({ x, compact = false }: { x: FeedItem; compact?: boolean }) {
  const Icon = ICON[x.topic] ?? Radio
  const tone = x.tone > 0 ? 'text-up' : x.tone < 0 ? 'text-down' : 'text-muted'
  const body = (
    <>
      <Icon className={cn('size-3.5 shrink-0', x.topic === 'alerts' ? 'text-accent' : tone)} aria-hidden />
      <span className="truncate font-medium text-fg">{x.title}</span>
      {!compact && x.detail && <span className="truncate text-muted">{x.detail}</span>}
      {x.modelBased && <Badge tone="info">model</Badge>}
      <span className="num shrink-0 text-2xs text-faint">{itemTime(x)}</span>
    </>
  )
  const cls = 'flex min-w-0 items-center gap-1.5 text-xs'
  return x.link ? (
    <Link to={x.link} className={cn(cls, 'hover:underline')} title={x.detail ?? x.title}>
      {body}
    </Link>
  ) : x.href ? (
    <a href={x.href} target="_blank" rel="noreferrer" className={cn(cls, 'hover:underline')} title={x.detail ?? x.title}>
      {body}
    </a>
  ) : (
    <span className={cls}>{body}</span>
  )
}

/** Slim live strip under the top bar: news stream state, quotes service state, delayed index / FX / commodity ticker,
 * pause, newest items, feed sheet. */
export function LiveTicker() {
  const feed = useLive((s) => s.feed)
  const paused = useLive((s) => s.paused)
  const setPaused = useLive((s) => s.setPaused)
  const unseen = useLive((s) => s.unseen)
  const [open, setOpen] = useState(false)
  const st = useRealtimeStatus()
  if (USE_MOCK) return null
  const latest = feed.slice(0, 4)
  // audit F23: with the realtime supervisor stopped, the strip must not present old items as live
  const rtOff = !!st.data && !st.data.running
  const beat = st.data?.heartbeat_at ?? null
  return (
    <div className="flex h-8 shrink-0 items-center gap-3 border-b border-border bg-surface-2/60 px-3" aria-label="Live feed">
      <ConnDot />
      <QuotesStatusChip />
      <Tip content={paused ? 'Resume the live stream (missed items are replayed)' : 'Pause the live stream'}>
        <Button size="icon-sm" variant="ghost" aria-label={paused ? 'Resume live stream' : 'Pause live stream'} aria-pressed={paused} onClick={() => setPaused(!paused)}>
          {paused ? <Play /> : <Pause />}
        </Button>
      </Tip>
      <HeaderQuotes className="max-w-[46%] shrink-0 border-r border-border pr-3 max-xl:hidden" />
      <div className={cn('flex min-w-0 flex-1 items-center gap-4 overflow-hidden', rtOff && 'opacity-60')}>
        {rtOff && (
          <span className="shrink-0 text-2xs font-semibold tracking-wide text-warn uppercase" title={beat ? `Last realtime heartbeat ${fmtDateTime(beat)}` : 'No realtime heartbeat'}>
            Not live{beat ? ` · last heartbeat ${fmtRelative(beat)}` : ''} · last items received:
          </span>
        )}
        {latest.length === 0 ? (
          <span className="text-xs text-faint">Waiting for filings, news, macro releases and alerts…</span>
        ) : (
          latest.map((x) => (
            <div key={x.uid} className="min-w-0 max-w-[340px] shrink">
              <FeedLine x={x} compact />
            </div>
          ))
        )}
      </div>
      <Button
        size="xs"
        variant="outline"
        onClick={() => {
          setOpen(true)
          useLive.getState().markSeen()
        }}
      >
        Feed{unseen > 0 ? ` · ${unseen} new` : ''}
      </Button>
      <FeedSheet open={open} onOpenChange={setOpen} />
    </div>
  )
}

function FeedSheet({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const feed = useLive((s) => s.feed)
  const received = useLive((s) => s.received)
  const lastId = useLive((s) => s.lastEventId)
  const beat = useLive((s) => s.lastBeatAt)
  const [topic, setTopic] = useState<'all' | LiveTopic>('all')
  const rows = useMemo(() => (topic === 'all' ? feed : feed.filter((x) => x.topic === topic)), [feed, topic])
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="fixed top-0 right-0 z-50 flex h-full w-[min(560px,100vw)] flex-col border-l border-border-strong bg-surface focus-visible:outline-none" aria-describedby={undefined}>
          <header className="flex items-center gap-2 border-b border-border px-4 py-3">
            <Dialog.Title className="flex-1 text-md font-semibold text-fg">Live feed</Dialog.Title>
            <ConnDot />
            <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={() => onOpenChange(false)}>
              <X />
            </Button>
          </header>
          <div className="flex items-center gap-2 border-b border-border px-4 py-2">
            <Segmented
              label="Topic"
              size="xs"
              value={topic}
              onChange={setTopic}
              options={[
                { value: 'all', label: 'All' },
                { value: 'filings', label: 'Filings' },
                { value: 'news', label: 'News' },
                { value: 'macro', label: 'Macro' },
                { value: 'alerts', label: 'Alerts' },
              ]}
            />
            <span className="ml-auto text-2xs text-muted">
              {received} streamed · last id {lastId ?? '—'}
              {beat ? ` · heartbeat ${fmtDateTime(beat)}` : ''}
            </span>
          </div>
          <ul className="min-h-0 flex-1 divide-y divide-border overflow-y-auto">
            {rows.length === 0 && <li className="px-4 py-8 text-center text-xs text-muted">Nothing yet for this topic.</li>}
            {rows.map((x) => (
              <li key={x.uid} className="px-4 py-2">
                <div className="mb-0.5 flex items-center gap-2 text-2xs text-muted">
                  <span className="font-semibold tracking-wide uppercase">{TOPIC_LABEL[x.topic] ?? x.topic}</span>
                  <span>{x.type.replace(/_/g, ' ')}</span>
                  {x.origin === 'stream' ? <Badge tone="up">streamed</Badge> : x.origin === 'snapshot' ? <Badge tone="neutral">nightly</Badge> : null}
                  <span className="num ml-auto">{x.ts ? fmtDateTime(x.ts) : ''}</span>
                </div>
                <FeedLine x={x} />
              </li>
            ))}
          </ul>
          <p className="border-t border-border px-4 py-2 text-2xs text-muted">Macro surprises are measured against model-based expectations (not consensus). Pausing closes the stream; resuming replays what was missed from the last event id.</p>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
