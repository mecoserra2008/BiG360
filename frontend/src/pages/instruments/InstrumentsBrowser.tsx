import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useInstruments } from '@/api/instruments/hooks'
import { type InstrumentItem, groupItems, priceDigits } from '@/api/instruments/types'
import { Sparkline } from '@/components/charts/Sparkline'
import { useLiveQuotes } from '@/api/live/quotes'
import { LiveChangeById, LivePriceById } from '@/components/live/LiveQuote'
import { Delta } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Input, Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'

const TYPES = [
  { value: 'all', label: 'All' },
  { value: 'fx', label: 'FX' },
  { value: 'future', label: 'Futures' },
  { value: 'index', label: 'Indices' },
  { value: 'etf', label: 'ETFs' },
] as const

/** Markets → Instruments: FX / futures / indices / ETFs grouped, with last price, changes and a 1-year sparkline. */
export function InstrumentsBrowser() {
  const [type, setType] = useState<(typeof TYPES)[number]['value']>('all')
  const [q, setQ] = useState('')
  const list = useInstruments(type === 'all' ? undefined : type)
  return (
    <QueryView query={list} skeleton={<Skeleton className="h-[600px]" />}>
      {(d) => {
        const groups = groupItems(d, q)
        return (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Segmented label="Instrument type" size="xs" value={type} onChange={setType} options={[...TYPES]} />
              <Input aria-label="Filter instruments" placeholder="Filter ticker / name" value={q} onChange={(e) => setQ(e.target.value)} className="w-48" />
              <span className="ml-auto text-2xs text-muted">
                {d.items.length} instruments · as of {fmtDate(d.as_of)} · {d.source}
              </span>
            </div>
            <p className="text-2xs text-muted">{d.fx_note}</p>
            <div className="grid gap-3 2xl:grid-cols-2">
              {groups.map((g) => (
                <Panel key={g.group} title={g.label} subtitle={`${g.items.length} instruments`} flush>
                  <GroupTable items={g.items} />
                </Panel>
              ))}
            </div>
            {groups.length === 0 && <p className="py-8 text-center text-xs text-muted">No instruments match.</p>}
          </div>
        )
      }}
    </QueryView>
  )
}

function GroupTable({ items }: { items: InstrumentItem[] }) {
  const rows = useMemo(() => items, [items])
  // workstream B: delayed live last / 1D for instruments the quotes service polls (indices, FX, futures, cockpit)
  useLiveQuotes(rows.map((x) => x.security_id), { record: false })
  const pct = (v: number | null | undefined) => <Delta value={v ?? null} showIcon={false} format={(x) => `${x > 0 ? '+' : ''}${(x * 100).toFixed(1)}%`} />
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs" aria-label="Instruments">
        <thead>
          <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
            <th className="px-3 py-1">Instrument</th>
            <th className="px-2 py-1 text-right">Last</th>
            <th className="px-2 py-1 text-right">1D</th>
            <th className="px-2 py-1 text-right">1M</th>
            <th className="px-2 py-1 text-right">YTD</th>
            <th className="px-2 py-1 text-right">1Y</th>
            <th className="px-2 py-1">1 year</th>
            <th className="px-3 py-1 text-right">Since</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((x) => (
            <tr key={x.security_id} className="border-b border-border/60 hover:bg-surface-2">
              <td className="max-w-[260px] px-3 py-1">
                <Link to={`/instruments/${encodeURIComponent(x.security_id)}`} className="font-semibold text-accent-text hover:underline">
                  {x.ticker}
                </Link>
                <span className="ml-1.5 truncate text-muted" title={x.name}>
                  {x.name.replace(' (Yahoo market rate)', '').replace(' (continuous front month)', '')}
                </span>
              </td>
              <td className="num px-2 py-1 text-right">{x.last == null ? DASH : <LivePriceById id={x.security_id} fallback={x.last} digits={priceDigits(x.last, x.type)} />}</td>
              <td className="num px-2 py-1 text-right"><LiveChangeById id={x.security_id} fallback={x.chg_1d} digits={1} /></td>
              <td className="num px-2 py-1 text-right">{pct(x.chg_1m)}</td>
              <td className="num px-2 py-1 text-right">{pct(x.chg_ytd)}</td>
              <td className="num px-2 py-1 text-right">{pct(x.chg_1y)}</td>
              <td className="px-2 py-1">{x.spark.length > 1 ? <Sparkline data={x.spark} width={84} height={20} ariaLabel={`${x.ticker} one-year weekly closes`} /> : <span className="text-faint">{DASH}</span>}</td>
              <td className="num px-3 py-1 text-right text-muted">{x.first_date ? x.first_date.slice(0, 4) : DASH}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
