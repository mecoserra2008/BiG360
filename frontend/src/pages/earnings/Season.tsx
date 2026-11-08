import { useMemo } from 'react'
import { useSearchParams } from 'react-router'
import { sectorName, seasonLabel } from '@/api/earnings/adapters'
import { useEarningsSeason } from '@/api/earnings/hooks'
import type { ESeason, ESeasonRow } from '@/api/earnings/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Flag } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { CompanyLink, EarningsNav, ModelBasedTag, Signed, SourceBadge } from './shared'

const REGION_OPTS = ['', 'US', 'EU', 'UK', 'JP', 'KR', 'CN', 'HK', 'LATAM', 'APAC'].map((r) => ({ value: r, label: r || 'All regions' }))
const pct = (v: number | null | undefined) => fmtPct(v, 0)

export default function EarningsSeason() {
  const [sp, setSp] = useSearchParams()
  const season = sp.get('season') ?? ''
  const region = sp.get('region') ?? ''
  const q = useEarningsSeason(season, region)
  const set = (k: string, v: string) => setSp((p) => { if (v) p.set(k, v); else p.delete(k); return p }, { replace: true })
  return (
    <>
      <PageHeader title="Earnings season" subtitle="Who has reported, how results compare with our model-based expectations (not consensus), and how prices reacted">
        <div className="flex flex-wrap items-center gap-2">
          <Select
            label="Season"
            value={q.data?.season ?? season}
            onChange={(v) => set('season', v)}
            options={(q.data?.seasons ?? (season ? [season] : [])).map((s) => ({ value: s, label: seasonLabel(s) }))}
          />
          <Select label="Region" value={region} onChange={(v) => set('region', v)} options={REGION_OPTS} />
        </div>
      </PageHeader>
      <EarningsNav />
      {q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <Skeleton className="h-96" /> : q.data.data_status === 'pending' || !q.data.total ? <PendingState detail="The earnings jobs have not produced season aggregates yet." /> : <SeasonBody d={q.data} />}
    </>
  )
}

function SeasonBody({ d }: { d: ESeason }) {
  const t = d.total as ESeasonRow
  return (
    <div className="space-y-3">
      <Panel title={seasonLabel(d.season)} subtitle={<span className="inline-flex flex-wrap items-center gap-2">As of {fmtDate(d.as_of)} · {d.region === 'ALL' ? 'all regions' : d.region} <ModelBasedTag /></span>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-7">
          <Stat label="Reported" value={`${fmtInt(t.n_reported)} / ${fmtInt(t.n_expected)}`} sub={pct(t.share_reported)} />
          <Stat label="Next 7 days" value={fmtInt(t.n_upcoming_7d ?? 0)} sub="expected reporters" />
          <Stat label="Beat EPS (model)" value={pct(t.pct_beat_eps)} sub={`${fmtInt(t.n_eps_surprise ?? 0)} with a surprise`} />
          <Stat label="Median EPS surprise" value={t.median_eps_surprise_z != null ? `${fmtNum(t.median_eps_surprise_z, 2, true)}σ` : DASH} sub="vs model interval" />
          <Stat label="Beat revenue (model)" value={pct(t.pct_beat_rev)} sub={`median ${fmtPct(t.median_rev_surprise_pct, 1, true)}`} />
          <Stat label="Reaction breadth" value={pct(t.reaction_breadth)} sub="CAR[−1,+1] > 0" />
          <Stat label="Median reaction" value={fmtPct(t.median_car_m1_p1, 1, true)} sub={`|move| ${fmtNum(t.median_abs_move_ratio, 2)}× normal`} />
        </div>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="By sector" subtitle="Share beating the model-based EPS expectation vs share with a positive price reaction">
          {d.by_sector.length ? <SectorBars rows={d.by_sector} /> : <EmptyState />}
        </Panel>
        <Panel title="Season history" subtitle="Same measures across seasons (share of reporters)">
          <HistoryLines rows={d.history} />
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="By region" flush>
          <RegionTable rows={d.by_region} />
        </Panel>
        <Panel title="Biggest model surprises" subtitle="|EPS surprise z| weighted by size" flush>
          <TopTable d={d} />
        </Panel>
      </div>
      <Panel title="Reporting in the next 7 days" flush>
        {d.upcoming.length ? (
          <ul className="grid gap-x-4 gap-y-0.5 p-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {d.upcoming.map((u, i) => (
              <li key={i} className="flex min-w-0 items-center gap-2">
                <span className="num w-20 shrink-0 text-muted">{fmtDate(u.date)}</span>
                <CompanyLink ticker={u.security.ticker} name={u.security.name} id={u.security.security_id} className="truncate" />
                <span className="ml-auto shrink-0"><SourceBadge source={u.date_source} window={{ lo: u.window_lo, hi: u.window_hi }} /></span>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyState title="No reporters expected" />
        )}
      </Panel>
    </div>
  )
}

function SectorBars({ rows }: { rows: ESeasonRow[] }) {
  const t = useChartTokens()
  const data = useMemo(() => [...rows].filter((r) => r.n_reported > 0).sort((a, b) => (a.pct_beat_eps ?? 0) - (b.pct_beat_eps ?? 0)), [rows])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8 },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (ps: unknown) => {
        const arr = ps as { dataIndex: number }[]
        const r = data[arr[0]?.dataIndex ?? 0]
        if (!r) return ''
        return ttTitle(sectorName(r.sector_id)) + ttRow(t.series[0], 'Beat EPS (model)', pct(r.pct_beat_eps)) + ttRow(t.series[1], 'Positive reaction', pct(r.reaction_breadth)) + ttRow(t.ref, 'Reported', `${r.n_reported} / ${r.n_expected}`)
      },
    },
    xAxis: { type: 'value', min: 0, max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    yAxis: { type: 'category', data: data.map((r) => sectorName(r.sector_id)), axisLabel: { color: t.muted }, axisTick: { show: false } },
    series: [
      { type: 'bar', name: 'Beat EPS (model)', data: data.map((r) => r.pct_beat_eps), barMaxWidth: 8, itemStyle: { color: t.series[0], borderRadius: [0, 4, 4, 0] } },
      { type: 'bar', name: 'Positive reaction', data: data.map((r) => r.reaction_breadth), barMaxWidth: 8, itemStyle: { color: t.series[1], borderRadius: [0, 4, 4, 0] } },
    ],
  }), [data, t])
  return <EChart option={option} height={Math.max(220, data.length * 26 + 40)} ariaLabel="Share beating model EPS expectation and share with positive reaction by sector" />
}

function HistoryLines({ rows }: { rows: ESeasonRow[] }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8 },
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => pct(v as number) },
    xAxis: { type: 'category', data: rows.map((r) => r.season), axisLabel: { color: t.muted } },
    yAxis: { type: 'value', min: 0, max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    series: [
      { type: 'line', name: 'Beat EPS (model)', data: rows.map((r) => r.pct_beat_eps), lineStyle: { width: 2, color: t.series[0] }, itemStyle: { color: t.series[0] }, symbolSize: 8 },
      { type: 'line', name: 'Positive reaction', data: rows.map((r) => r.reaction_breadth), lineStyle: { width: 2, color: t.series[1] }, itemStyle: { color: t.series[1] }, symbolSize: 8 },
      { type: 'line', name: 'Share reported', data: rows.map((r) => r.share_reported), lineStyle: { width: 2, color: t.ref, type: 'dashed' }, itemStyle: { color: t.ref }, symbolSize: 8 },
    ],
  }), [rows, t])
  return <EChart option={option} height={260} ariaLabel="Season history of beat rate, reaction breadth and share reported" />
}

function RegionTable({ rows }: { rows: ESeasonRow[] }) {
  if (!rows.length) return <EmptyState />
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-left text-muted">
          <tr className="border-b border-border">
            {['Region', 'Reported', 'Beat EPS', 'Median EPS z', 'Beat rev.', 'Breadth', 'Median CAR'].map((h, i) => (
              <th key={h} scope="col" className={`px-2 py-1.5 font-medium ${i ? 'text-right' : ''}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.region} className="border-b border-border/60">
              <td className="px-2 py-1 font-medium">{r.region}</td>
              <td className="num px-2 py-1 text-right">{r.n_reported} / {r.n_expected} <span className="text-muted">({pct(r.share_reported)})</span></td>
              <td className="num px-2 py-1 text-right">{pct(r.pct_beat_eps)} <span className="text-faint">({r.n_eps_surprise ?? 0})</span></td>
              <td className="px-2 py-1 text-right"><Signed v={r.median_eps_surprise_z} kind="z" /></td>
              <td className="num px-2 py-1 text-right">{pct(r.pct_beat_rev)}</td>
              <td className="num px-2 py-1 text-right">{pct(r.reaction_breadth)}</td>
              <td className="px-2 py-1 text-right"><Signed v={r.median_car_m1_p1} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function TopTable({ d }: { d: ESeason }) {
  if (!d.top_surprises.length) return <EmptyState title="No surprises yet" />
  return (
    <div className="max-h-[360px] overflow-auto">
      <table className="w-full text-xs">
        <thead className="sticky top-0 bg-surface text-left text-muted">
          <tr className="border-b border-border">
            {['Company', 'Released', 'EPS z', 'EPS vs model', 'Rev. vs model', 'CAR [−1,+1]'].map((h, i) => (
              <th key={h} scope="col" className={`px-2 py-1.5 font-medium ${i > 1 ? 'text-right' : ''}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {d.top_surprises.map((x, i) => (
            <tr key={i} className="border-b border-border/60">
              <td className="max-w-[220px] px-2 py-1">
                <div className="flex items-center gap-1.5">
                  <Flag country={x.security.country} className="h-2.5 w-[15px] shrink-0" />
                  <CompanyLink ticker={x.security.ticker} name={x.security.name} id={x.security.security_id} className="truncate" />
                </div>
              </td>
              <td className="num px-2 py-1 text-muted">{fmtDate(x.release_date)}</td>
              <td className="px-2 py-1 text-right"><Signed v={x.eps_surprise_z} kind="z" /></td>
              <td className="px-2 py-1 text-right"><Signed v={x.eps_surprise_pct} /></td>
              <td className="px-2 py-1 text-right"><Signed v={x.rev_surprise_pct} /></td>
              <td className="px-2 py-1 text-right"><Signed v={x.car_m1_p1} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
