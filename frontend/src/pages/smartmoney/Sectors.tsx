import { useMemo } from 'react'
import { useSearchParams } from 'react-router'
import { sectorName } from '@/api/earnings/adapters'
import { useSmartMoneySectors } from '@/api/smartmoney/hooks'
import type { SmSectorRow, SmSectors } from '@/api/smartmoney/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { PercentileBar } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { LagNote, ModelBasedTag, OfficialTag, Signed, SmartMoneyNav } from './shared'

export default function SmartMoneySectors() {
  const [sp, setSp] = useSearchParams()
  const region = sp.get('region') ?? ''
  const q = useSmartMoneySectors(region)
  const opts = [{ value: '', label: 'All regions' }, ...(q.data?.regions ?? []).map((r) => ({ value: r, label: r }))]
  return (
    <>
      <PageHeader title="Smart money by sector" subtitle="13F ownership and flows, short interest, squeeze flags and EU / UK short disclosures aggregated by sector">
        <Select label="Region" value={region} onChange={(v) => setSp((p) => { if (v) p.set('region', v); else p.delete('region'); return p }, { replace: true })} options={opts} />
      </PageHeader>
      <SmartMoneyNav />
      {q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <Skeleton className="h-96" /> : q.data.data_status === 'pending' ? <PendingState detail="The smart-money jobs have not produced sector aggregates yet." /> : <Body d={q.data} />}
    </>
  )
}

function Body({ d }: { d: SmSectors }) {
  const L = d.labels as Record<string, string>
  if (!d.rows.length) return <EmptyState title="No sector aggregates" />
  return (
    <div className="space-y-3">
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Institutional ownership and quarterly change" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="SEC 13F" /><LagNote lag={d.lag} /></span>}>
          <OwnershipBars rows={d.rows} />
        </Panel>
        <Panel title="Short interest" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="FINRA" /><span className="text-2xs text-muted">{L.si_float}</span></span>}>
          <ShortBars rows={d.rows} />
        </Panel>
      </div>
      <Panel title="Sector table" subtitle={<ModelBasedTag tip={`${L.crowding}. ${L.score}`}>crowding and score columns</ModelBasedTag>} flush>
        <div className="overflow-x-auto">
          <table className="w-full text-xs" aria-label="Smart money by sector">
            <thead className="text-left text-muted">
              <tr className="border-b border-border">
                {['Sector', 'Names', '13F names', 'Median inst. %', 'Median Δ q/q', 'Breadth', 'Smart buy / sell', 'Crowding', 'Median SI %', 'Avg DTC', 'Squeeze', 'EU shorted', 'Avg score'].map((h, i) => (
                  <th key={h} scope="col" className={`px-2 py-1.5 font-medium ${i ? 'text-right' : ''}`}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr key={`${r.sector_id}${r.region ?? ''}`} className="border-b border-border/60">
                  <td className="px-2 py-1 font-medium">{sectorName(r.sector_id)}</td>
                  <td className="num px-2 py-1 text-right">{fmtInt(r.n_securities)}</td>
                  <td className="num px-2 py-1 text-right">{fmtInt(r.n_13f)}</td>
                  <td className="num px-2 py-1 text-right">{fmtPct(r.median_inst_own_pct, 1)}</td>
                  <td className="px-2 py-1 text-right"><Signed v={r.median_inst_own_chg} pp /></td>
                  <td className="px-2 py-1 text-right"><Signed v={r.avg_breadth} digits={0} /></td>
                  <td className="num px-2 py-1 text-right">{fmtInt(r.smart_buyers)} / {fmtInt(r.smart_sellers)}</td>
                  <td className="px-2 py-1 text-right"><PercentileBar value={r.avg_crowding_pct != null ? r.avg_crowding_pct * 100 : null} /></td>
                  <td className="num px-2 py-1 text-right">{fmtPct(r.median_si_pct_out, 1)}</td>
                  <td className="num px-2 py-1 text-right">{fmtNum(r.avg_days_to_cover, 1)}</td>
                  <td className="num px-2 py-1 text-right">{r.n_squeeze ? fmtInt(r.n_squeeze) : DASH}</td>
                  <td className="num px-2 py-1 text-right">{r.n_eu_shorted ? `${fmtInt(r.n_eu_shorted)} (${fmtNum(r.avg_eu_short_pct, 1)}%)` : DASH}</td>
                  <td className="num px-2 py-1 text-right">{fmtNum(r.avg_score, 2, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  )
}

function OwnershipBars({ rows }: { rows: SmSectorRow[] }) {
  const t = useChartTokens()
  const data = useMemo(() => rows.filter((r) => r.median_inst_own_pct != null).sort((a, b) => (a.median_inst_own_pct ?? 0) - (b.median_inst_own_pct ?? 0)), [rows])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps: unknown) => {
        const r = data[(ps as { dataIndex: number }[])[0]?.dataIndex ?? 0]
        if (!r) return ''
        return ttTitle(sectorName(r.sector_id)) + ttRow(t.series[0], 'Median inst. ownership', fmtPct(r.median_inst_own_pct, 1)) + ttRow(t.series[1], 'Median change q/q', `${fmtNum((r.median_inst_own_chg ?? 0) * 100, 2, true)} pp`) + ttRow(t.ref, '13F names', fmtInt(r.n_13f))
      },
    },
    xAxis: [
      { type: 'value', min: 0, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    ],
    yAxis: { type: 'category', data: data.map((r) => sectorName(r.sector_id)), axisLabel: { color: t.muted }, axisTick: { show: false } },
    series: [
      { type: 'bar', name: 'Median inst. ownership', data: data.map((r) => r.median_inst_own_pct), barMaxWidth: 10, itemStyle: { color: t.series[0], borderRadius: [0, 4, 4, 0] } },
    ],
  }), [data, t])
  if (!data.length) return <EmptyState title="No 13F coverage" />
  return <EChart option={option} height={Math.max(220, data.length * 24 + 40)} ariaLabel="Median institutional ownership by sector" />
}

function ShortBars({ rows }: { rows: SmSectorRow[] }) {
  const t = useChartTokens()
  const data = useMemo(() => rows.filter((r) => r.median_si_pct_out != null).sort((a, b) => (a.median_si_pct_out ?? 0) - (b.median_si_pct_out ?? 0)), [rows])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps: unknown) => {
        const r = data[(ps as { dataIndex: number }[])[0]?.dataIndex ?? 0]
        if (!r) return ''
        return ttTitle(sectorName(r.sector_id)) + ttRow(t.down, 'Median SI % of shares', fmtPct(r.median_si_pct_out, 2)) + ttRow(t.ref, 'Avg days to cover', fmtNum(r.avg_days_to_cover, 1)) + ttRow(t.warn, 'Squeeze flags (model-based)', fmtInt(r.n_squeeze))
      },
    },
    xAxis: { type: 'value', min: 0, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    yAxis: { type: 'category', data: data.map((r) => sectorName(r.sector_id)), axisLabel: { color: t.muted }, axisTick: { show: false } },
    series: [{ type: 'bar', name: 'Median SI %', data: data.map((r) => r.median_si_pct_out), barMaxWidth: 10, itemStyle: { color: t.series[1], borderRadius: [0, 4, 4, 0] } }],
  }), [data, t])
  if (!data.length) return <EmptyState title="No short-interest coverage" />
  return <EChart option={option} height={Math.max(220, data.length * 24 + 40)} ariaLabel="Median short interest by sector" />
}
