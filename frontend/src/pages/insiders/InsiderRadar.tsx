import type { ColumnDef } from '@tanstack/react-table'
import { useDeferredValue, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import type { InsiderRadarItem } from '@/api/types'
import { useInsiderRadar } from '@/api/hooks'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { DataTable } from '@/components/tables/DataTable'
import { SecurityCell } from '@/components/tables/cells'
import { insiderColumns } from '@/components/tables/insiderColumns'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented, Select, Slider, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { fmtCompactCurrency, fmtInt, fmtNum } from '@/lib/format'
import { useUi } from '@/store'

type CodeFilter = 'all' | 'P' | 'S' | 'other'

export default function InsiderRadar() {
  const region = useUi((s) => s.region)
  const q = useInsiderRadar(region)
  return (
    <>
      <PageHeader title="Insider Radar" subtitle="Anomalous insider activity across markets · rules-based insider score + IsolationForest anomaly score (unsupervised) on Form 4 and national equivalents · the insider XGBoost model failed its out-of-sample gate and is not used" />
      <QueryView query={q} skeleton={<Panel><SkeletonRows rows={18} /></Panel>}>
        {(items) => <RadarBody items={items} />}
      </QueryView>
    </>
  )
}

function RadarBody({ items }: { items: InsiderRadarItem[] }) {
  const t = useChartTokens()
  const navigate = useNavigate()
  const [code, setCode] = useState<CodeFilter>('all')
  const [minScore, setMinScore] = useState(0.6)
  const [clusterOnly, setClusterOnly] = useState(false)
  const [sector, setSector] = useState('')
  const [text, setText] = useState('')
  const dText = useDeferredValue(text)
  const sectors = useMemo(() => [...new Set(items.map((i) => i.sector))].sort(), [items])

  const rows = useMemo(() => {
    const s = dText.trim().toLowerCase()
    return items.filter(
      (i) =>
        !(i.anomalyScore < minScore) &&
        (code === 'all' || (code === 'other' ? i.code !== 'P' && i.code !== 'S' : i.code === code)) &&
        (!clusterOnly || i.isClusterBuy) &&
        (!sector || i.sector === sector) &&
        (!s || i.ticker.toLowerCase().includes(s) || i.companyName.toLowerCase().includes(s) || i.insiderName.toLowerCase().includes(s)),
    )
  }, [items, minScore, code, clusterOnly, sector, dText])

  const columns = useMemo<ColumnDef<InsiderRadarItem>[]>(() => {
    const base = insiderColumns<InsiderRadarItem>().filter((c) => !['filedAt', 'owned', 'source'].includes(c.id as string))
    return [
      { id: 'sec', header: 'Company', size: 220, accessorFn: (r) => r.ticker, cell: (c) => <SecurityCell id={c.row.original.companyId} ticker={c.row.original.ticker} name={c.row.original.companyName} country={c.row.original.country} /> },
      ...base,
      { id: 'mcap', header: 'Mkt cap', size: 80, accessorFn: (r) => r.marketCapUsd, cell: (c) => <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span>, meta: { align: 'right' } },
      { id: 'reason', header: 'Why flagged', size: 330, accessorFn: (r) => r.reason, enableSorting: false, cell: (c) => <span className="text-muted" title={c.getValue<string>()}>{c.getValue<string>()}</span> },
    ]
  }, [])

  const bySector = useMemo<EChartsOption>(() => {
    const m = new Map<string, number>()
    for (const r of rows) m.set(r.sector, (m.get(r.sector) ?? 0) + 1)
    const arr = [...m.entries()].sort((a, b) => a[1] - b[1])
    return {
      grid: { left: 4, right: 28, top: 4, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: { type: 'value', minInterval: 1, splitNumber: 3 },
      yAxis: { type: 'category', data: arr.map((a) => a[0]), axisLine: { show: false }, axisLabel: { width: 140, overflow: 'truncate' } },
      series: [{ name: 'Flags', type: 'bar', data: arr.map((a) => a[1]), barMaxWidth: 12, itemStyle: { color: alpha(t.accent, 0.85), borderRadius: [0, 3, 3, 0] }, label: { show: true, position: 'right', fontSize: 10, color: t.muted } }],
    }
  }, [rows, t])

  const buys = rows.filter((r) => r.code === 'P')
  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel className="col-span-12 xl:col-span-8">
        <div className="flex flex-wrap gap-x-10 gap-y-2">
          <Stat label="Flags" value={fmtInt(rows.length)} sub={`of ${fmtInt(items.length)} scored filings`} />
          <Stat label="Cluster buys" value={fmtInt(rows.filter((r) => r.isClusterBuy).length)} />
          <Stat label="Open-market buys" value={fmtCompactCurrency(buys.reduce((a, r) => a + r.valueUsd, 0))} sub={`${buys.length} purchases`} />
          <Stat label="Countries" value={new Set(rows.map((r) => r.country)).size} />
          <Stat label="Median anomaly" value={rows.length ? fmtNum([...rows].sort((a, b) => a.anomalyScore - b.anomalyScore)[Math.floor(rows.length / 2)].anomalyScore, 2) : '—'} />
        </div>
      </Panel>
      <Panel title="Flags by sector" className="col-span-12 row-span-2 xl:col-span-4" bodyClassName="p-1.5">
        <EChart option={bySector} height={200} ariaLabel="Number of insider flags by sector" />
      </Panel>
      <Panel className="col-span-12 xl:col-span-8" bodyClassName="py-2">
        <div className="flex flex-wrap items-center gap-3">
          <Input aria-label="Search company or insider" placeholder="Company / insider" value={text} onChange={(e) => setText(e.target.value)} className="w-44" />
          <Segmented label="Transaction code" value={code} onChange={setCode} options={[{ value: 'all', label: 'All' }, { value: 'P', label: 'Buys (P)' }, { value: 'S', label: 'Sells (S)' }, { value: 'other', label: 'Other' }]} />
          <Select label="Sector" value={sector} onChange={setSector} options={[{ value: '', label: 'All sectors' }, ...sectors.map((s) => ({ value: s, label: s }))]} className="w-44" />
          <label className="flex items-center gap-2 text-xs text-muted">
            Min anomaly
            <Slider label="Minimum anomaly score" min={0.4} max={0.95} step={0.05} value={[minScore]} onValueChange={(v) => setMinScore(v[0])} className="w-24" />
            <span className="num w-8 text-fg">{minScore.toFixed(2)}</span>
          </label>
          <label className="flex items-center gap-2 text-xs text-muted">
            <Switch label="Cluster buys only" checked={clusterOnly} onCheckedChange={setClusterOnly} /> Cluster only
          </label>
        </div>
      </Panel>
      <Panel title="Activity feed" subtitle="Sorted by anomaly score; click a row for Company 360 › Insiders" className="col-span-12" flush>
        <DataTable
          data={rows}
          columns={columns}
          ariaLabel="Anomalous insider transactions"
          height="max(360px, calc(100vh - 360px))"
          initialSorting={[{ id: 'anomaly', desc: true }]}
          columnPinning={{ left: ['sec'] }}
          getRowId={(r, i) => `${r.id}-${i}`}
          onRowClick={(r) => navigate(`/company/${encodeURIComponent(r.companyId)}?tab=insiders`)}
        />
      </Panel>
    </div>
  )
}
