/**
 * Delayed live quotes in the UI (workstream B): a price + change + "delayed · hh:mm" tag wherever a security is shown
 * (Company 360 header, Watchlist, Instruments, Markets Overview index board, header ticker). Quotes are DELAYED and
 * never replace the official close; when no quote is available the official snapshot value is shown as before.
 */
import { Link } from 'react-router'
import { type QuoteView, delayedLabel, quoteTitle, quoteTone, serviceChip, useLiveQuotes, useQuote } from '@/api/live/quotes'
import type { CurrencyCode } from '@/api/types'
import { Delta } from '@/components/ui/data'
import { Tip } from '@/components/ui/tooltip'
import { fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { cn } from '@/lib/utils'

const DOT: Record<'up' | 'warn' | 'neutral', string> = { up: 'bg-up', warn: 'bg-warn', neutral: 'bg-faint' }

/** "● delayed · 14:32" (green = market open, grey = closed, amber = stale), tooltip explains the number. */
export function QuoteTag({ q, className }: { q: QuoteView | null; className?: string }) {
  if (!q) return null
  const tone = quoteTone(q)
  return (
    <Tip content={quoteTitle(q)}>
      <span tabIndex={0} className={cn('inline-flex items-center gap-1 whitespace-nowrap text-2xs text-faint', className)} aria-label={quoteTitle(q)}>
        <span className={cn('size-1.5 rounded-full', DOT[tone])} aria-hidden />
        {delayedLabel(q)}
        {!q.marketOpen && <span className="text-faint">· closed</span>}
      </span>
    </Tip>
  )
}

/** Price cell: live delayed price when there is one (with its tag), else the snapshot value. */
export function LivePriceCell({
  q,
  fallback,
  currency,
  digits,
  showTag = true,
}: {
  q: QuoteView | null
  fallback: number | null | undefined
  currency?: string | null
  digits?: number
  showTag?: boolean
}) {
  const v = q?.price ?? fallback
  const txt = digits !== undefined ? fmtNum(v, digits) : fmtPrice(v, (currency ?? undefined) as CurrencyCode | undefined)
  return (
    <span className="inline-flex flex-col items-end leading-tight">
      <span className="num">{txt}</span>
      {showTag && q && <QuoteTag q={q} />}
    </span>
  )
}

/** Change cell: live change vs the previous official close, else the snapshot 1D change. */
export function LiveChangeCell({ q, fallback, digits = 2 }: { q: QuoteView | null; fallback: number | null | undefined; digits?: number }) {
  return <Delta value={q?.changePct ?? fallback} format={(v) => fmtPct(v, digits, true)} showIcon={false} />
}

/** Table cells by id (the table calls useLiveQuotes(ids) once; each cell subscribes to its own quote). */
export function LivePriceById(p: { id: string; fallback: number | null | undefined; currency?: string | null; digits?: number; showTag?: boolean }) {
  return <LivePriceCell q={useQuote(p.id)} fallback={p.fallback} currency={p.currency} digits={p.digits} showTag={p.showTag} />
}

export function LiveChangeById({ id, fallback, digits }: { id: string; fallback: number | null | undefined; digits?: number }) {
  return <LiveChangeCell q={useQuote(id)} fallback={fallback} digits={digits} />
}

/** Company 360 header "Last" block: live delayed price, change and tag; the official close stays visible. */
export function LastPriceBlock({
  id,
  currency,
  close,
  closeChange,
}: {
  id: string
  currency: string
  close: number | null | undefined
  closeChange: number | null | undefined
}) {
  const lq = useLiveQuotes([id])
  const q = lq.get(id)
  if (!q)
    return (
      <div>
        <div className="label-caps">Last close ({currency})</div>
        <div className="num text-2xl font-semibold">{fmtPrice(close, currency as CurrencyCode)}</div>
        <Delta value={closeChange} className="text-sm" />
      </div>
    )
  return (
    <div>
      <div className="label-caps">Live · delayed ({q.currency ?? currency})</div>
      <div className="num text-2xl font-semibold">{fmtPrice(q.price, (q.currency ?? currency) as CurrencyCode)}</div>
      <div className="flex items-center gap-2">
        <Delta value={q.changePct} className="text-sm" />
        <QuoteTag q={q} />
      </div>
      <div className="num text-2xs text-faint">Official close {fmtPrice(close, currency as CurrencyCode)}</div>
    </div>
  )
}

/** Header ticker (LIVE strip): main indices, VIX, 10y, FX, gold, oil, bitcoin. */
const HEADER_LABEL: Record<string, string> = {
  'IDX:^GSPC': 'S&P 500',
  'IDX:^NDX': 'Nasdaq 100',
  'IDX:^DJI': 'Dow',
  'IDX:^STOXX50E': 'Euro Stoxx 50',
  'IDX:^GDAXI': 'DAX',
  'IDX:^FTSE': 'FTSE 100',
  'IDX:^N225': 'Nikkei',
  'IDX:^HSI': 'Hang Seng',
  'IDX:^VIX': 'VIX',
  'IDX:^TNX': 'US 10y',
  'IDX:EURUSD=X': 'EUR/USD',
  'IDX:USDJPY=X': 'USD/JPY',
  'IDX:GBPUSD=X': 'GBP/USD',
  'IDX:GC=F': 'Gold',
  'IDX:CL=F': 'WTI',
  'IDX:BTC=F': 'Bitcoin',
}
const HEADER_IDS = Object.keys(HEADER_LABEL)

function digitsFor(id: string, price: number): number {
  if (id.endsWith('=X')) return price < 20 ? 4 : 2
  if (id === 'IDX:^TNX' || id === 'IDX:^VIX') return 2
  return price >= 1000 ? 0 : 2
}

export function HeaderQuotes({ className }: { className?: string }) {
  const lq = useLiveQuotes([], { set: 'header' })
  const items = HEADER_IDS.map((id) => ({ id, q: lq.get(id) })).filter((x): x is { id: string; q: QuoteView } => !!x.q)
  if (!items.length) return null
  return (
    <div className={cn('flex min-w-0 items-center gap-3 overflow-hidden', className)} aria-label="Delayed market quotes">
      {items.map(({ id, q }) => (
        <Tip key={id} content={`${HEADER_LABEL[id]}: ${quoteTitle(q)}`}>
          <Link to={securityRoute(id)} className={cn('inline-flex shrink-0 items-baseline gap-1 text-xs hover:underline', !q.marketOpen && 'opacity-70')}>
            <span className="text-muted">{HEADER_LABEL[id]}</span>
            <span className="num font-medium text-fg">{fmtNum(q.price, digitsFor(id, q.price))}</span>
            <Delta value={q.changePct} format={(v) => fmtPct(v, 2, true)} showIcon={false} className="text-2xs" />
          </Link>
        </Tip>
      ))}
      <QuoteTag q={items[0].q} className="shrink-0" />
    </div>
  )
}

/** LIVE strip status chip of the quotes service (ok / idle with reason / source down / off). */
export function QuotesStatusChip() {
  const lq = useLiveQuotes([], { set: 'header' })
  const chip = serviceChip(lq.service)
  const color = chip.tone === 'up' ? 'bg-up' : chip.tone === 'warn' ? 'bg-warn' : chip.tone === 'down' ? 'bg-down' : 'bg-faint'
  return (
    <Tip content={chip.title}>
      <span tabIndex={0} className="inline-flex shrink-0 items-center gap-1.5 text-xs text-muted" role="status" aria-label={chip.title}>
        <span className={cn('size-2 rounded-full', color)} aria-hidden />
        {chip.label}
      </span>
    </Tip>
  )
}
