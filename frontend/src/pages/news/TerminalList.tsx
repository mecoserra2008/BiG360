import { useVirtualizer } from '@tanstack/react-virtual'
import { Layers } from 'lucide-react'
import { memo, useEffect, useLayoutEffect, useRef } from 'react'
import { useNewsLive } from '@/api/news/stream'
import { type StoryGroup, TIER_LABEL, TIER_SHORT, type TermRow, salienceLevel, sentimentTone } from '@/api/news/types'
import { cn } from '@/lib/utils'

const ROW_H = 54
const FLASH_MS = 3000

const fmtClock = (iso: string | null | undefined): string => {
  if (!iso) return '--:--'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '--:--' : d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
}
const fmtDay = (iso: string | null | undefined): string => {
  if (!iso) return ''
  const d = new Date(iso)
  const now = new Date()
  return d.toDateString() === now.toDateString() ? '' : d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' })
}

const SAL = ['bg-transparent', 'bg-border-strong', 'bg-info', 'bg-accent'] as const

export interface ListEntry {
  row: TermRow
  group?: StoryGroup
}

const Row = memo(function Row({ e, selected, unread, onSelect, now }: { e: ListEntry; selected: boolean; unread: boolean; onSelect: () => void; now: number }) {
  const r = e.row
  const tone = sentimentTone(r)
  const flash = r.arrivedAt !== null && now - r.arrivedAt < FLASH_MS
  const sal = salienceLevel(r.salience)
  const n = e.group ? e.group.items.length : r.story_size
  const src = e.group ? e.group.nSources : r.story_sources
  return (
    <div
      role="option"
      aria-selected={selected}
      data-testid="news-row"
      tabIndex={-1}
      onClick={onSelect}
      className={cn(
        'flex h-full cursor-pointer items-stretch gap-2 border-b border-border/60 pr-2 text-left',
        flash && 'news-flash',
        selected ? 'bg-surface-3' : 'hover:bg-surface-2',
      )}
    >
      <span className={cn('w-[3px] shrink-0', SAL[sal])} title={`Salience ${r.salience ?? '—'}`} aria-hidden />
      <div className="flex w-16 shrink-0 flex-col justify-center text-right">
        <span className="num text-xs text-fg">{fmtClock(r.first_seen_at ?? r.published_at)}</span>
        <span className="num text-2xs text-faint">{fmtDay(r.first_seen_at ?? r.published_at) || (r.latency_s !== null && r.latency_s !== undefined && r.latency_s >= 0 && r.latency_s < 3600 ? `+${Math.round(r.latency_s)}s` : '')}</span>
      </div>
      <div className="flex min-w-0 flex-1 flex-col justify-center">
        <div className="flex min-w-0 items-center gap-1.5 text-2xs text-muted">
          <span className={cn('rounded-sm px-1 font-semibold', r.tier === 1 ? 'bg-accent-soft text-accent-text' : r.tier === 2 ? 'bg-info-soft text-info' : 'bg-surface-3 text-muted')} title={TIER_LABEL[r.tier]}>
            {TIER_SHORT[r.tier] ?? r.tier}
          </span>
          <span className="max-w-[160px] truncate font-medium">{r.publisher ?? r.source_id}</span>
          {r.language && r.language !== 'en' && <span className="uppercase">{r.language}</span>}
          {r.tickers.slice(0, 4).map((t) => (
            <span key={t} className="rounded-sm bg-surface-3 px-1 font-semibold text-fg">
              {t}
            </span>
          ))}
          {r.countries.slice(0, 2).map((c) => (
            <span key={c} className="text-faint">
              {c}
            </span>
          ))}
          <span className="ml-auto flex shrink-0 items-center gap-1.5">
            {n > 1 && (
              <span className="num inline-flex items-center gap-0.5 rounded-sm border border-border px-1" title={`${n} articles · ${src} sources in this story`}>
                <Layers className="size-3" aria-hidden />
                {n}·{src}
              </span>
            )}
            {typeof r.sentiment === 'number' && (
              <span className={cn('num font-semibold', tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-muted')} title={`Sentiment ${r.sentiment.toFixed(2)} (model-based: ${r.sentiment_model ?? 'model'})`}>
                {r.sentiment > 0 ? '+' : r.sentiment < 0 ? '−' : ''}
                {Math.abs(r.sentiment).toFixed(2)}
              </span>
            )}
            {unread && <span className="size-1.5 rounded-full bg-accent" aria-label="unread" />}
          </span>
        </div>
        <div className={cn('truncate text-sm', unread ? 'font-semibold' : 'font-medium', tone === 'up' ? 'text-up' : tone === 'down' ? 'text-down' : 'text-fg')} title={r.title}>
          {r.title}
        </div>
      </div>
    </div>
  )
})

/** Virtualised terminal list (5 000+ rows: only the visible ~30 rows are in the DOM). */
export function TerminalList({ entries, sel, onSelect, seenAt, readKeys, now, onEndReached }: { entries: ListEntry[]; sel: number; onSelect: (i: number) => void; seenAt: number; readKeys: Set<string>; now: number; onEndReached?: () => void }) {
  const ref = useRef<HTMLDivElement>(null)
  // oxlint-disable-next-line react/incompatible-library
  const v = useVirtualizer({ count: entries.length, getScrollElement: () => ref.current, estimateSize: () => ROW_H, overscan: 12, getItemKey: (i) => entries[i]?.row.item_key ?? i })
  const flushAt = useNewsLive((s) => s.flushAt)
  const rowsLen = entries.length
  // perf: first rows painted + live update render time (flush -> commit painted)
  const firstDone = useRef(false)
  useLayoutEffect(() => {
    if (!firstDone.current && rowsLen > 0) {
      firstDone.current = true
      requestAnimationFrame(() => {
        const ms = Math.round(performance.now())
        useNewsLive.setState((s) => ({ perf: { ...s.perf, firstRowsMs: s.perf.firstRowsMs ?? ms } }))
      })
    }
  }, [rowsLen])
  useLayoutEffect(() => {
    if (flushAt === null) return
    const t = flushAt
    requestAnimationFrame(() => useNewsLive.getState().reportRender(performance.now() - t))
  }, [flushAt])
  useEffect(() => {
    if (sel >= 0) v.scrollToIndex(sel, { align: 'auto' })
  }, [sel, v])
  const items = v.getVirtualItems()
  const lastIdx = items.length ? items[items.length - 1].index : 0
  useEffect(() => {
    if (onEndReached && entries.length && lastIdx >= entries.length - 60) onEndReached()
  }, [lastIdx, entries.length, onEndReached])
  return (
    <div ref={ref} role="listbox" aria-label="News items" aria-activedescendant={sel >= 0 ? `news-row-${sel}` : undefined} className="relative min-h-0 flex-1 overflow-auto" data-testid="news-list">
      <div style={{ height: v.getTotalSize(), position: 'relative' }}>
        {items.map((it) => {
          const e = entries[it.index]
          if (!e) return null
          const unread = e.row.arrivedAt !== null && e.row.arrivedAt > seenAt && !readKeys.has(e.row.item_key)
          return (
            <div key={it.key} id={`news-row-${it.index}`} style={{ position: 'absolute', top: 0, left: 0, right: 0, height: ROW_H, transform: `translateY(${it.start}px)` }}>
              <Row e={e} selected={it.index === sel} unread={unread} onSelect={() => onSelect(it.index)} now={now} />
            </div>
          )
        })}
      </div>
    </div>
  )
}
