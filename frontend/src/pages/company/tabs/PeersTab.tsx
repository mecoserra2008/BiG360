import { useQueries } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { type ReactNode, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import type { CompanyPeers, PeerRow } from '@/api/types'
import { useCompanyPeers } from '@/api/hooks'
import { USE_MOCK } from '@/api/http'
import { studioApi } from '@/api/studio/client'
import { useStudioPeerSets } from '@/api/studio/hooks'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { PeerGroupPanel } from '@/components/conformal/PeerGroupPanel'
import { DataTable } from '@/components/tables/DataTable'
import { SecurityCell } from '@/components/tables/cells'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { ScorePill } from '@/components/ui/data'
import { PendingState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { fmtCompactCurrency, fmtValue } from '@/lib/format'
import { quantile } from '@/lib/utils'

export default function PeersTab({ id }: { id: string }) {
  const [peerSet, setPeerSet] = useState<string | null>(null)
  const q = useCompanyPeers(id, peerSet)
  const sets = useStudioPeerSets(!USE_MOCK)
  const subjectId = q.data?.peers.find((p) => p.isSubject)?.companyId ?? null
  // the list endpoint omits members: load each set to keep only those that contain this company
  const details = useQueries({
    queries: (sets.data ?? []).map((s) => ({ queryKey: ['studio', 'peer-set', s.peer_set_id], queryFn: () => studioApi.peerSet(s.peer_set_id), staleTime: 5 * 60_000, enabled: !!subjectId })),
  })
  const usable = details.map((d) => d.data).filter((s): s is NonNullable<typeof s> => !!s && !!subjectId && s.members.some((m) => m.security_id === subjectId))
  const current = usable.find((s) => s.peer_set_id === peerSet) ?? null
  const selector =
    usable.length > 0 || peerSet ? (
      <Select
        label="Peer group"
        value={peerSet ?? ''}
        onChange={(v) => setPeerSet(v || null)}
        options={[{ value: '', label: 'Subsector peers (default)' }, ...usable.map((s) => ({ value: s.peer_set_id, label: `Peer set: ${s.name}` }))]}
      />
    ) : null
  const cluster = current?.members.find((m) => m.security_id === subjectId)?.cluster
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
      {(d) =>
        d.metrics.length && d.peers.some((p) => p.isSubject) ? (
          <PeersBody id={id} d={d} selector={selector} groupLabel={d.level === 'peer_set' && current ? `peer set “${current.name}”, cluster ${cluster ?? '?'} (${current.method})` : null} />
        ) : (
          <PendingState detail="Peer comparison not published for this company yet." />
        )
      }
    </QueryView>
  )
}

function PeersBody({ d, selector, groupLabel, id }: { d: CompanyPeers; selector?: ReactNode; groupLabel?: string | null; id: string }) {
  const t = useChartTokens()
  const navigate = useNavigate()
  const [x, setX] = useState(d.scatter.xMetric)
  const [y, setY] = useState(d.scatter.yMetric)
  const subject = d.peers.find((p) => p.isSubject) as PeerRow
  const mx = d.metrics.find((m) => m.id === x) ?? d.metrics[0]
  const my = d.metrics.find((m) => m.id === y) ?? d.metrics[1]

  const columns = useMemo<ColumnDef<PeerRow>[]>(
    () => [
      { id: 'sec', header: 'Company', size: 230, accessorFn: (r) => r.ticker, cell: (c) => <SecurityCell id={c.row.original.companyId} ticker={c.row.original.ticker} name={c.row.original.name} country={c.row.original.country} /> },
      { id: 'mcap', header: 'Mkt cap', size: 84, accessorFn: (r) => r.marketCapUsd, cell: (c) => <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span>, meta: { align: 'right' } },
      { id: 'score', header: 'Score', size: 72, accessorFn: (r) => r.compositeScore, cell: (c) => <ScorePill value={c.getValue<number>()} />, meta: { align: 'right' } },
      ...d.metrics.map<ColumnDef<PeerRow>>((m) => ({
        id: m.id,
        header: m.label,
        size: 118,
        accessorFn: (r) => r.metrics[m.id],
        sortUndefined: 'last',
        cell: (c) => <span className="num">{fmtValue(c.getValue<number | null>(), m.unit)}</span>,
        meta: { align: 'right', title: `${m.description} (${m.higherIsBetter ? 'higher' : 'lower'} is better)` },
      })),
    ],
    [d.metrics],
  )

  const scatter = useMemo<EChartsOption>(() => {
    const pts = d.peers.filter((p) => p.metrics[mx.id] !== null && p.metrics[my.id] !== null)
    const xs = pts.map((p) => p.metrics[mx.id] as number)
    const ys = pts.map((p) => p.metrics[my.id] as number)
    const maxCap = Math.max(...pts.map((p) => p.marketCapUsd))
    const size = (cap: number) => 8 + Math.sqrt(cap / maxCap) * 34
    const toPct = (u: string) => (u === 'pct' ? 100 : 1)
    const mk = (p: PeerRow) => ({
      name: p.ticker,
      value: [(p.metrics[mx.id] as number) * toPct(mx.unit), (p.metrics[my.id] as number) * toPct(my.unit), p.marketCapUsd],
      symbolSize: size(p.marketCapUsd),
    })
    return {
      grid: { left: 8, right: 24, top: 20, bottom: 24, containLabel: true },
      legend: { top: 0, right: 0, data: [subject.ticker, 'Peers'] },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const v = (p as { name: string; value: number[]; color: string })
          return ttTitle(v.name) + ttRow(v.color, mx.label, fmtValue(v.value[0] / toPct(mx.unit), mx.unit)) + ttRow(v.color, my.label, fmtValue(v.value[1] / toPct(my.unit), my.unit)) + ttRow(v.color, 'Mkt cap', fmtCompactCurrency(v.value[2]))
        },
      },
      xAxis: { type: 'value', name: mx.label, nameLocation: 'middle', nameGap: 22, scale: true, axisLabel: { formatter: (v: number) => (mx.unit === 'pct' ? `${v.toFixed(0)}%` : v.toFixed(1)) } },
      yAxis: { type: 'value', name: my.label, nameLocation: 'middle', nameGap: 36, scale: true, axisLabel: { formatter: (v: number) => (my.unit === 'pct' ? `${v.toFixed(0)}%` : v.toFixed(1)) } },
      series: [
        {
          name: 'Peers',
          type: 'scatter',
          data: pts.filter((p) => !p.isSubject).map(mk),
          itemStyle: { color: alpha(t.series[1], 0.55), borderColor: t.surface, borderWidth: 2 },
          label: { show: true, formatter: '{b}', position: 'right', fontSize: 9, color: t.muted },
          labelLayout: { hideOverlap: true },
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { color: t.borderStrong, type: 'dashed' },
            label: { show: true, fontSize: 9, color: t.faint, formatter: 'median' },
            data: [{ xAxis: quantile(xs, 0.5) * toPct(mx.unit) }, { yAxis: quantile(ys, 0.5) * toPct(my.unit) }],
          },
        },
        {
          name: subject.ticker,
          type: 'scatter',
          data: [mk(subject)],
          z: 5,
          itemStyle: { color: t.accent, borderColor: t.surface, borderWidth: 2 },
          label: { show: true, formatter: '{b}', position: 'top', fontSize: 11, fontWeight: 600, color: t.fg },
        },
      ],
    }
  }, [d.peers, mx, my, subject, t])

  const rankOption = useMemo<EChartsOption>(() => {
    const sorted = [...d.peers].sort((a, b) => b.marketCapUsd - a.marketCapUsd)
    return {
      grid: { left: 8, right: 12, top: 12, bottom: 8, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps: unknown) => { const p = (ps as { dataIndex: number }[])[0]; const r = sorted[p.dataIndex]; return ttTitle(`${r.ticker} · ${r.name}`) + ttRow(t.accent, 'Composite', String(Math.round(r.compositeScore))) + ttRow(t.ref, 'Mkt cap', fmtCompactCurrency(r.marketCapUsd)) } },
      xAxis: { type: 'category', data: sorted.map((p) => p.ticker), axisLabel: { rotate: 45, fontSize: 9 } },
      yAxis: { type: 'value', max: 100, splitNumber: 4 },
      series: [{ type: 'bar', barMaxWidth: 18, data: sorted.map((p) => ({ value: Math.round(p.compositeScore), itemStyle: { color: p.isSubject ? t.accent : alpha(t.ref, 0.55), borderRadius: [3, 3, 0, 0] } })) }],
    }
  }, [d.peers, t])

  const metricOpts = d.metrics.map((m) => ({ value: m.id, label: m.label }))

  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel
        title="Peer comparison"
        subtitle={`Most relevant KPIs for this business model · ${d.peers.length - 1} peers ${groupLabel ? `from ${groupLabel}` : 'by subsector and size'}`}
        className="col-span-12"
        flush
        actions={selector}
      >
        <div className="flex flex-wrap gap-1.5 border-b border-border px-3 py-2 text-xs">
          <span className="label-caps mr-1 self-center">{subject.ticker} rank</span>
          {d.metrics.map((m) => {
            const r = d.subjectRankByMetric[m.id]
            const top = r && r.rank <= Math.ceil(r.of / 3)
            const bottom = r && r.rank > Math.floor((2 * r.of) / 3)
            return (
              <span key={m.id} className="inline-flex items-center gap-1 rounded-sm border border-border px-1.5 py-0.5">
                <span className="text-muted">{m.label}</span>
                <span className={`num font-semibold ${top ? 'text-up' : bottom ? 'text-down' : 'text-fg'}`}>{r ? `${r.rank}/${r.of}` : '—'}</span>
              </span>
            )
          })}
        </div>
        <DataTable
          data={d.peers}
          columns={columns}
          ariaLabel="Peer comparison"
          initialSorting={[{ id: 'mcap', desc: true }]}
          columnPinning={{ left: ['sec'] }}
          getRowId={(r) => r.companyId}
          rowClassName={(r) => (r.isSubject ? '[&>td]:bg-surface-3! [&>td:first-child]:shadow-[inset_2px_0_0_var(--accent)]' : undefined)}
          onRowClick={(r) => !r.isSubject && navigate(`/company/${encodeURIComponent(r.companyId)}`)}
        />
      </Panel>
      <Panel
        title="Positioning"
        subtitle="Bubble = market cap · dashed lines = peer medians"
        className="col-span-12 xl:col-span-7"
        actions={
          <>
            <Select label="X metric" value={x} onChange={setX} options={metricOpts} className="w-40" />
            <span className="text-xs text-faint">vs</span>
            <Select label="Y metric" value={y} onChange={setY} options={metricOpts} className="w-40" />
          </>
        }
      >
        <EChart option={scatter} height={340} ariaLabel={`${my.label} versus ${mx.label} for peers`} />
      </Panel>
      <Panel title="Composite score by size" subtitle="Peers ordered by market cap" actions={<CompositeLabel />} className="col-span-12 xl:col-span-5">
        <EChart option={rankOption} height={340} ariaLabel="Composite score of peers ordered by market cap" />
      </Panel>
      {/* the resolved security id (the route may hold a ticker): highlights the company's row and defaults the pair leg */}
      {!USE_MOCK && <PeerGroupPanel query={{ securityId: subject.companyId || id }} title="Peer-group joint anomaly (this company's group)" className="col-span-12" />}
    </div>
  )
}
