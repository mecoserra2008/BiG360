import { useMemo, useState } from 'react'
import type { CompanyInsiders, InsiderTransaction } from '@/api/types'
import { useCompanyInsiders } from '@/api/hooks'
import type { ChartMarker } from '@/components/charts/PriceChart'
import { RangePriceChart } from '@/components/charts/RangePriceChart'
import { useChartTokens } from '@/components/charts/tokens'
import { DataTable } from '@/components/tables/DataTable'
import { insiderColumns } from '@/components/tables/insiderColumns'
import { Delta } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtCompactCurrency, fmtInt, fmtNum } from '@/lib/format'

export default function InsidersTab({ id }: { id: string }) {
  const q = useCompanyInsiders(id)
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
      {(d) => <InsidersBody d={d} id={id} />}
    </QueryView>
  )
}

type Filter = 'all' | 'open' | 'anomalous'

function InsidersBody({ d, id }: { d: CompanyInsiders; id: string }) {
  const t = useChartTokens()
  const [filter, setFilter] = useState<Filter>('all')
  // one marker per ISO week and kind (busy sellers would otherwise bury the chart); text = count when > 1
  const markers = useMemo<ChartMarker[]>(() => {
    const first = d.priceHistory[0]?.time ?? ''
    const bars = d.priceHistory.map((b) => b.time)
    const weekOf = (iso: string) => {
      const dt = new Date(`${iso}T00:00:00Z`)
      dt.setUTCDate(dt.getUTCDate() - ((dt.getUTCDay() + 6) % 7))
      return dt.toISOString().slice(0, 10)
    }
    const groups = new Map<string, { kind: 'CB' | 'P' | 'S'; date: string; n: number }>()
    for (const x of d.transactions) {
      if (x.tradeDate < first) continue
      const kind = x.isClusterBuy ? 'CB' : x.code === 'P' ? 'P' : x.code === 'S' ? 'S' : null
      if (!kind) continue
      const key = `${weekOf(x.tradeDate)}|${kind}`
      const g = groups.get(key)
      if (g) {
        g.n++
        if (x.tradeDate > g.date) g.date = x.tradeDate
      } else groups.set(key, { kind, date: x.tradeDate, n: 1 })
    }
    // snap to a bar date so the marker lands on the chart
    const snap = (iso: string) => bars.find((b) => b >= iso) ?? bars[bars.length - 1] ?? iso
    return [...groups.values()].map((g) => {
      const n = g.n > 1 ? `${g.kind}×${g.n}` : g.kind
      if (g.kind === 'CB') return { time: snap(g.date), position: 'belowBar' as const, color: t.accent, shape: 'circle' as const, text: n }
      if (g.kind === 'P') return { time: snap(g.date), position: 'belowBar' as const, color: t.up, shape: 'arrowUp' as const, text: n }
      return { time: snap(g.date), position: 'aboveBar' as const, color: t.down, shape: 'arrowDown' as const, text: n }
    })
  }, [d, t])
  const rows = useMemo<InsiderTransaction[]>(
    () => d.transactions.filter((x) => filter === 'all' || (filter === 'open' ? x.code === 'P' || x.code === 'S' : x.anomalyScore >= 0.6)),
    [d.transactions, filter],
  )
  const columns = useMemo(() => insiderColumns<InsiderTransaction>(), [])

  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel className="col-span-12">
        <div className="flex flex-wrap gap-x-10 gap-y-2">
          <Stat label="Anomaly score (max, recent)" value={fmtNum(d.anomalyScore, 2)} sub={d.anomalyScore >= 0.75 ? 'Unusual activity' : 'Within normal range'} />
          <Stat label="Net open-market 90d" value={<Delta value={d.netBuyValue90dUsd} format={(v) => fmtCompactCurrency(v, 'USD', 1)} />} />
          <Stat label="Buyers / sellers 90d" value={`${fmtInt(d.buyers90d)} / ${fmtInt(d.sellers90d)}`} />
          <Stat label="Cluster buys" value={d.clusterBuys.length} sub={d.clusterBuys[0] ? `${d.clusterBuys[0].insiders} insiders · ${fmtCompactCurrency(d.clusterBuys[0].valueUsd)}` : 'none in window'} />
        </div>
      </Panel>
      <Panel title="Price with insider transactions" subtitle="▲ P purchase · ▼ S sale · ● CB cluster buy — grouped per week (×n = count)" className="col-span-12" bodyClassName="p-1.5">
        <RangePriceChart id={id} initial={d.priceHistory} initialRange="1Y" mode="candles" height={320} markers={markers} overlaysLabel="insider markers" ariaLabel="Price chart with insider transaction markers" />
      </Panel>
      <Panel
        title="Transactions"
        subtitle="Form-4-style ledger (national equivalents outside the US)"
        className="col-span-12"
        flush
        actions={<Segmented label="Filter transactions" value={filter} onChange={setFilter} options={[{ value: 'all', label: 'All' }, { value: 'open', label: 'Open-market P/S' }, { value: 'anomalous', label: 'Anomalous ≥0.6' }]} />}
      >
        <DataTable data={rows} columns={columns} ariaLabel="Insider transactions" height={420} initialSorting={[{ id: 'tradeDate', desc: true }]} getRowId={(r, i) => `${r.id}-${i}`} />
      </Panel>
    </div>
  )
}
