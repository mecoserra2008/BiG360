import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router'
import type { KpiProfile, ScreenerRow, SectorDetail, SectorNode } from '@/api/types'
import { useSectorDetail, useSectorTree } from '@/api/hooks'
import { DistributionChart } from '@/components/charts/Distribution'
import { USE_MOCK } from '@/api/http'
import { PeerGroupPanel } from '@/components/conformal/PeerGroupPanel'
import { DataTable } from '@/components/tables/DataTable'
import { SecurityCell } from '@/components/tables/cells'
import { Badge } from '@/components/ui/badge'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { Delta, ScorePill } from '@/components/ui/data'
import { QueryView, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtCompactCurrency, fmtInt, fmtPct, fmtScore } from '@/lib/format'
import { kpiDirectionLabel } from '@/lib/kpi'
import { cn } from '@/lib/utils'
import { useUi } from '@/store'

const PROFILE_LABEL: Record<KpiProfile, string> = {
  general: 'Industrial KPI set',
  bank: 'Bank KPI set',
  insurance: 'Insurance KPI set',
  software: 'Software / platform KPI set',
  semis: 'Semiconductor KPI set',
  reit: 'Real-estate KPI set',
  energy: 'Energy KPI set',
  pharma: 'Pharma KPI set',
  retail: 'Retail KPI set',
  utility: 'Utility / telecom KPI set',
}

export default function SectorExplorer() {
  const region = useUi((s) => s.region)
  const [params, setParams] = useSearchParams()
  const tree = useSectorTree(region)
  // '' = let the API layer pick the first sector of the live taxonomy (avoids a 404 on mock-only ids)
  const sector = params.get('sector') ?? tree.data?.[0]?.id ?? ''
  const sub = params.get('sub')
  const detail = useSectorDetail(sector, sub, region)

  const select = (s: string, ss: string | null) => {
    const p = new URLSearchParams()
    p.set('sector', s)
    if (ss) p.set('sub', ss)
    setParams(p, { replace: true })
  }

  return (
    <>
      <PageHeader title="Sector Explorer" subtitle="Drill sector → subsector; the KPI set adapts to the business model" />
      <div className="grid grid-cols-[240px_minmax(0,1fr)] gap-3">
        <Panel title="Taxonomy" flush className="self-start">
          <QueryView query={tree} skeleton={<SkeletonRows className="p-3" rows={14} />}>
            {(nodes) => <SectorTree nodes={nodes} sector={sector} sub={sub} onSelect={select} />}
          </QueryView>
        </Panel>
        <div className="min-w-0">
          <QueryView query={detail} skeleton={<DetailSkeleton />}>
            {(d) => <SectorBody key={`${d.sector}:${d.subsector}`} d={d} sectorId={sector} subId={sub} region={region} />}
          </QueryView>
        </div>
      </div>
    </>
  )
}

function SectorTree({ nodes, sector, sub, onSelect }: { nodes: SectorNode[]; sector: string; sub: string | null; onSelect: (s: string, ss: string | null) => void }) {
  return (
    <ul className="p-1.5" aria-label="Sectors">
      {nodes.map((n) => {
        const open = n.id === sector
        return (
          <li key={n.id}>
            <button
              type="button"
              aria-expanded={open}
              onClick={() => onSelect(n.id, null)}
              className={cn(
                'flex h-7 w-full items-center gap-1.5 rounded-sm px-1.5 text-left text-sm focus-visible:outline-2 focus-visible:outline-focus',
                open && !sub ? 'bg-surface-3 text-fg' : 'text-fg hover:bg-surface-2',
              )}
            >
              <ChevronRight className={cn('size-3.5 shrink-0 text-faint transition-transform', open && 'rotate-90')} aria-hidden />
              <span className="min-w-0 flex-1 truncate font-medium">{n.name}</span>
              <span className="num text-xs text-faint">{Number.isFinite(n.companyCount) ? n.companyCount : ''}</span>
            </button>
            {open && (
              <ul className="mb-1 ml-5 border-l border-border pl-1">
                {n.subsectors.map((s) => (
                  <li key={s.id}>
                    <button
                      type="button"
                      aria-current={sub === s.id ? 'true' : undefined}
                      onClick={() => onSelect(n.id, s.id)}
                      className={cn(
                        'flex h-6 w-full items-center gap-1.5 rounded-sm px-1.5 text-left text-xs focus-visible:outline-2 focus-visible:outline-focus',
                        sub === s.id ? 'bg-accent-soft font-semibold text-accent-text' : 'text-muted hover:bg-surface-2 hover:text-fg',
                      )}
                    >
                      <span className="min-w-0 flex-1 truncate">{s.name}</span>
                      <span className="num text-faint">{Number.isFinite(s.companyCount) ? s.companyCount : ''}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        )
      })}
    </ul>
  )
}

function DetailSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-16" />
      <div className="grid grid-cols-4 gap-3">
        {Array.from({ length: 8 }, (_, i) => (
          <Skeleton key={i} className="h-44" />
        ))}
      </div>
      <Skeleton className="h-64" />
    </div>
  )
}

const RANK_COLS: ColumnDef<ScreenerRow>[] = [
  { id: 'rank', header: '#', size: 30, enableSorting: false, cell: (c) => <span className="num text-faint">{c.row.index + 1}</span>, meta: { align: 'right' } },
  { id: 'sec', header: 'Company', size: 196, accessorFn: (r) => r.ticker, cell: (c) => <SecurityCell id={c.row.original.id} ticker={c.row.original.ticker} name={c.row.original.name} country={c.row.original.country} /> },
  { id: 'mcap', header: 'Mkt cap', size: 80, accessorFn: (r) => r.marketCapUsd, cell: (c) => <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span>, meta: { align: 'right' } },
  { id: 'score', header: 'Score', size: 70, accessorFn: (r) => r.compositeScore, cell: (c) => <ScorePill value={c.getValue<number>()} />, meta: { align: 'right' } },
  { id: 'r1m', header: '1M', size: 70, accessorFn: (r) => r.return1m, cell: (c) => <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 1, true)} />, meta: { align: 'right' } },
]

function SectorBody({ d, sectorId, subId, region }: { d: SectorDetail; sectorId: string; subId: string | null; region: string }) {
  const navigate = useNavigate()
  const options = useMemo(() => {
    const seen = new Map<string, string>()
    for (const r of [...d.leaders, ...d.laggards]) seen.set(r.id, `${r.ticker} — ${r.name}`)
    for (const v of d.distributions[0]?.values ?? []) if (!seen.has(v.companyId)) seen.set(v.companyId, v.ticker)
    return [{ value: '', label: 'No highlight' }, ...[...seen.entries()].map(([value, label]) => ({ value, label }))]
  }, [d])
  const [hl, setHl] = useState<string>(d.leaders[0]?.id ?? '')
  const cols = RANK_COLS

  return (
    <div className="space-y-3">
      <Panel>
        <div className="flex flex-wrap items-center gap-x-8 gap-y-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="text-lg font-semibold">{d.subsector ?? d.sector}</h2>
              <Tip content="KPI definitions switch with the business model (banks use NIM/CET1, software uses NRR/Rule of 40, REITs use FFO/LTV …)">
                <span tabIndex={0}>
                  <Badge tone="accent">{PROFILE_LABEL[d.kpiProfile]}</Badge>
                </span>
              </Tip>
            </div>
            <div className="text-xs text-muted">{d.subsector ? d.sector : 'Sector view · companies on the dominant KPI profile; pick a subsector to switch KPI set'}</div>
          </div>
          <Stat label="Companies" value={fmtInt(d.stats.companyCount)} />
          <Stat label="Median score" value={fmtScore(d.stats.medianScore)} />
          <Stat label="Total mkt cap" value={fmtCompactCurrency(d.stats.totalMarketCapUsd)} />
          <Stat label="1M return (cap-weighted)" value={<Delta value={d.stats.medianReturn1m} format={(v) => fmtPct(v, 1, true)} />} />
          <div className="ml-auto flex items-center gap-2">
            <span className="label-caps">Highlight</span>
            <Select label="Highlight company in distributions" value={hl} onChange={setHl} options={options} className="w-56" />
          </div>
        </div>
      </Panel>

      {d.stats.companyCount === 0 ? (
        <Panel>
          <div className="py-10 text-center text-sm text-muted">No companies in this selection for the current region filter.</div>
        </Panel>
      ) : (
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
          {d.distributions.map((dist) => (
            <Panel key={dist.kpi.id} title={dist.kpi.label} subtitle={`${kpiDirectionLabel(dist.kpi)} · n=${dist.values.length}`} className="min-w-0" bodyClassName="p-1.5">
              <div title={dist.kpi.description}>
                <DistributionChart dist={dist} highlightId={hl || null} />
              </div>
            </Panel>
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
        <Panel title="Leaders" subtitle="Top composite scores in selection (descriptive ranking)" actions={<CompositeLabel />} className="min-w-0" flush>
          <DataTable data={d.leaders} columns={cols} ariaLabel="Sector leaders" manualSorting getRowId={(r) => r.id} onRowClick={(r) => navigate(`/company/${encodeURIComponent(r.id)}`)} />
        </Panel>
        <Panel title="Laggards" subtitle="Lowest composite scores in selection (descriptive ranking)" actions={<CompositeLabel />} className="min-w-0" flush>
          <DataTable data={d.laggards} columns={cols} ariaLabel="Sector laggards" manualSorting getRowId={(r) => r.id} onRowClick={(r) => navigate(`/company/${encodeURIComponent(r.id)}`)} />
        </Panel>
      </div>
      {!USE_MOCK && sectorId && (
        <PeerGroupPanel
          query={{ sectorId: subId ? undefined : sectorId, subsectorId: subId ?? undefined, region: ['US', 'EU', 'UK', 'JP', 'KR', 'LATAM'].includes(region) ? region : undefined, limit: 120 }}
          title="Peer-group joint anomaly in this selection"
        />
      )}
    </div>
  )
}
