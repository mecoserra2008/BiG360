import { useState } from 'react'
import { Link } from 'react-router'
import { liquidMovers, moversFilterText } from '@/api/adapters/market'
import { useLiveQuotes, useQuote } from '@/api/live/quotes'
import type { IndexQuote, MarketBreadth, MarketMover, MoversFilter } from '@/api/types'
import { QuoteTag } from '@/components/live/LiveQuote'
import { Sparkline } from '@/components/charts/Sparkline'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { PendingState } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtCompact, fmtCompactCurrency, fmtDate, fmtDateShort, fmtInt, fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtMacro } from '../../macro/fmt'

const REGION_ORDER = ['US', 'CA', 'LATAM', 'UK', 'EU', 'JP', 'KR', 'CN', 'HK', 'CNHK', 'APAC', 'OTHER']
const th = 'h-7 border-b border-border px-2 text-right text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'
const td = 'h-8 border-b border-border px-2 text-right whitespace-nowrap'

/* ── index board ── */

/** Level / 1D: the delayed live quote while the quotes service has one (workstream B), else the last official close. */
function LiveLevel({ id, last }: { id: string; last: number | null | undefined }) {
  const q = useQuote(`IDX:${id}`)
  return (
    <span className="inline-flex flex-col items-end leading-tight">
      <span>{fmtMacro(q?.price ?? last, 5)}</span>
      {q && <QuoteTag q={q} />}
    </span>
  )
}

function LiveChange1d({ id, change }: { id: string; change: number | null | undefined }) {
  const q = useQuote(`IDX:${id}`)
  return <Delta value={q?.changePct ?? change} format={(v) => fmtPct(v, 2, true)} showIcon={false} />
}

export function IndexBoard({ indices, className }: { indices: IndexQuote[]; className?: string }) {
  const rows = [...indices].sort((a, b) => REGION_ORDER.indexOf(a.region) - REGION_ORDER.indexOf(b.region) || a.name.localeCompare(b.name))
  const dates = new Set(rows.map((r) => r.asOf).filter(Boolean))
  useLiveQuotes(rows.map((r) => `IDX:${r.id}`), { record: false })
  return (
    <Panel
      title="Index board"
      subtitle={`Benchmarks in local currency · price returns · Level / 1D delayed live while markets are open · ● = 1D move above 2σ of the last 60 sessions${dates.size > 1 ? ' · dates differ by index' : ''}`}
      className={className}
      flush
    >
      {rows.length === 0 ? (
        <PendingState compact detail="No benchmark index bars in this snapshot." />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" aria-label="Benchmark indices">
            <thead>
              <tr>
                <th scope="col" className={cn(th, 'pl-3 text-left')}>Index</th>
                <th scope="col" className={th}>Level</th>
                <th scope="col" className={th}>1D</th>
                <th scope="col" className={th}>1W</th>
                <th scope="col" className={th}>1M</th>
                <th scope="col" className={th}>YTD</th>
                <th scope="col" className={th}>As of</th>
                <th scope="col" className={cn(th, 'pr-3')}>60d</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id} className="hover:bg-surface-2">
                  <td className={cn(td, 'pl-3 text-left')}>
                    <div className="flex min-w-0 items-center gap-1.5">
                      <span className="w-10 shrink-0 rounded-sm bg-surface-3 px-1 text-center text-2xs font-semibold text-muted">{r.region}</span>
                      <span className="truncate text-fg" title={`${r.name} (${r.id})`}>{r.name}</span>
                      {r.bigMove && (
                        <Tip content="1D move larger than two standard deviations of the last 60 daily moves">
                          <span tabIndex={0} className="text-accent" aria-label="Large move">●</span>
                        </Tip>
                      )}
                    </div>
                  </td>
                  <td className={cn(td, 'num text-fg')}><LiveLevel id={r.id} last={r.last} /></td>
                  <td className={td}><LiveChange1d id={r.id} change={r.change1d} /></td>
                  <td className={td}><Delta value={r.change1w} format={(v) => fmtPct(v, 1, true)} showIcon={false} /></td>
                  <td className={td}><Delta value={r.change1m} format={(v) => fmtPct(v, 1, true)} showIcon={false} /></td>
                  <td className={td}><Delta value={r.changeYtd} format={(v) => fmtPct(v, 1, true)} showIcon={false} /></td>
                  <td className={cn(td, 'num text-2xs text-muted')}>{r.asOf ? fmtDateShort(r.asOf) : DASH}</td>
                  <td className={cn(td, 'pr-3')}>
                    <Sparkline data={r.spark.map((p) => p.value)} width={76} height={18} ariaLabel={`${r.name} last 60 sessions`} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

/* ── breadth ── */

function Bar({ v, label }: { v: number; label: string }) {
  if (!Number.isFinite(v)) return <span className="num text-muted">{DASH}</span>
  return (
    <span className="inline-flex items-center gap-1.5" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(v * 100)} aria-label={label}>
      <span className="num w-8 text-right text-fg">{fmtPct(v, 0)}</span>
      <span className="relative hidden h-1.5 w-10 overflow-hidden rounded-full bg-surface-3 2xl:inline-block">
        <span className={cn('absolute inset-y-0 left-0 rounded-full', v >= 0.6 ? 'bg-up' : v <= 0.4 ? 'bg-down' : 'bg-muted')} style={{ width: `${v * 100}%` }} />
      </span>
    </span>
  )
}

export function BreadthPanel({ rows, total, className }: { rows: MarketBreadth[]; total: MarketBreadth | null; className?: string }) {
  const list = rows.length ? rows : total ? [total] : []
  const dates = total ? Object.entries(total.asOfByRegion) : []
  return (
    <Panel
      title="Breadth"
      subtitle={dates.length ? `Equities on their region's last session · ${[...new Set(dates.map((d) => d[1]))].map(fmtDateShort).join(' / ')}` : 'Share of names above their 50 / 200-day averages, advancers / decliners, 52-week highs / lows'}
      className={className}
      flush
    >
      {list.length === 0 ? (
        <PendingState compact detail="Breadth not published for this region." />
      ) : (
        <table className="w-full text-sm" aria-label="Market breadth">
          <thead>
            <tr>
              <th scope="col" className={cn(th, 'pl-3 text-left')}>Region</th>
              <th scope="col" className={th}>&gt; 50d</th>
              <th scope="col" className={th}>&gt; 200d</th>
              <th scope="col" className={th} title="Advancers / decliners on the day">A / D</th>
              <th scope="col" className={cn(th, 'pr-3')} title="New 52-week highs / lows">NH / NL</th>
            </tr>
          </thead>
          <tbody>
            {list.map((b) => {
              const ad = b.advancers + b.decliners ? b.advancers / (b.advancers + b.decliners) : NaN
              return (
                <tr key={b.region} className="hover:bg-surface-2">
                  <td className={cn(td, 'pl-3 text-left font-medium text-fg')}>{b.region === 'ALL' ? 'All' : b.region === 'CNHK' ? 'CN/HK' : b.region}</td>
                  <td className={td}><Bar v={b.pctAbove50d} label={`${b.region} share above 50-day average`} /></td>
                  <td className={td}><Bar v={b.pctAbove200d} label={`${b.region} share above 200-day average`} /></td>
                  <td className={cn(td, 'num text-xs whitespace-nowrap')} title={`${fmtInt(b.advancers)} up · ${fmtInt(b.decliners)} down`}>
                    <span className={ad >= 0.5 ? 'text-up' : 'text-down'}>{fmtCompact(b.advancers, 1)}</span>
                    <span className="text-faint">/</span>
                    <span className={ad < 0.5 ? 'text-down' : 'text-muted'}>{fmtCompact(b.decliners, 1)}</span>
                  </td>
                  <td className={cn(td, 'num pr-3 text-xs whitespace-nowrap')}>
                    <span className="text-up">{fmtInt(b.newHighs)}</span>
                    <span className="text-faint">/</span>
                    <span className="text-down">{fmtInt(b.newLows)}</span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Panel>
  )
}

/* ── movers ── */

export function MoversPanel({ gainers, losers, filter, className }: { gainers: MarketMover[]; losers: MarketMover[]; filter?: MoversFilter | null; className?: string }) {
  const [mode, setMode] = useState<'liquid' | 'all'>('liquid')
  // v1.30: the API screens movers server-side (known ADV, cap floor, USD price) and returns enough rows
  if (filter?.liquid) {
    return (
      <Panel title="Movers (1D)" subtitle={`Liquid names (server screen): ${moversFilterText(filter)}`} className={className} flush>
        <div className="grid grid-cols-2 divide-x divide-border">
          <MoverList title="Gainers" rows={gainers} />
          <MoverList title="Losers" rows={losers} />
        </div>
      </Panel>
    )
  }
  const g = mode === 'liquid' ? liquidMovers(gainers) : gainers
  const l = mode === 'liquid' ? liquidMovers(losers) : losers
  return (
    <Panel
      title="Movers (1D)"
      subtitle={mode === 'liquid' ? 'Liquid only: market cap ≥ $1bn known, US price ≥ $2 (from the API’s top 10 each side)' : 'All of the API’s top 10 each side, incl. illiquid / unknown-cap names'}
      className={className}
      flush
      actions={<Segmented size="xs" label="Mover filter" value={mode} onChange={setMode} options={[{ value: 'liquid', label: 'Liquid' }, { value: 'all', label: 'All' }]} />}
    >
      <div className="grid grid-cols-2 divide-x divide-border">
        <MoverList title="Gainers" rows={g} />
        <MoverList title="Losers" rows={l} />
      </div>
    </Panel>
  )
}

function MoverList({ title, rows }: { title: string; rows: MarketMover[] }) {
  return (
    <div className="min-w-0">
      <div className="label-caps border-b border-border px-3 py-1">{title}</div>
      {rows.length === 0 ? (
        <div className="px-3 py-4 text-center text-2xs text-muted">None pass the filter.</div>
      ) : (
        <ul>
          {rows.slice(0, 8).map((m) => (
            <li key={m.id} className="border-b border-border last:border-b-0">
              <Link
                to={`/company/${encodeURIComponent(m.id)}`}
                className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 px-3 py-1 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-focus"
                title={`${m.name} · ${m.region} · close ${fmtPrice(m.close, m.currency)} ${m.currency} · bar ${fmtDate(m.pxDate)}${m.marketCapUsd ? ` · mcap ${fmtCompactCurrency(m.marketCapUsd)}` : ' · mcap unknown'}${m.volumeZ !== null ? ` · volume z ${m.volumeZ.toFixed(1)}` : ''}`}
              >
                <span className="min-w-0">
                  <span className="flex items-center gap-1">
                    <span className="num truncate text-xs font-semibold text-fg">{m.ticker}</span>
                    <Badge className="h-4 px-1">{m.region}</Badge>
                  </span>
                  <span className="block truncate text-2xs text-muted">{m.name}</span>
                </span>
                <span className="text-right">
                  <Delta value={m.change1d} format={(v) => fmtPct(v, 1, true)} showIcon={false} className="text-xs" />
                  <span className="num block text-2xs text-faint">{m.marketCapUsd ? fmtCompactCurrency(m.marketCapUsd, 'USD', 0) : 'cap n/a'}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
