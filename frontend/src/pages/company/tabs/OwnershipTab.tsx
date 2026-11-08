import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { HOLDER_STATUS, euChange, euShortSeries, holderMix, siSeries, trendSeries } from '@/api/smartmoney/adapters'
import { useCompanyOwnership } from '@/api/smartmoney/hooks'
import type { CoOwnership } from '@/api/smartmoney/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { PercentileBar } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtCompact, fmtDate, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { Avail, NoDataState, useMissingReason } from '@/components/ui/missing'
import { EventBadge, LagNote, ModelBasedTag, OfficialTag, Signed, SourcesFooter } from '../../smartmoney/shared'

export default function OwnershipTab({ id }: { id: string }) {
  const q = useCompanyOwnership(id)
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  if (!q.data) return <SkeletonRows rows={10} className="pt-3" />
  if (q.data.data_status === 'pending') return <PendingState detail="The smart-money jobs have not produced ownership data for this snapshot yet." />
  return <Body d={q.data} />
}

function Body({ d }: { d: CoOwnership }) {
  const s = d.summary
  const L = d.labels as Record<string, string>
  const mix = holderMix(d.holders)
  // completeness C: empty panels / stats say why (13F / FINRA cover US listings only, shares outstanding missing ...)
  const r13f = useMissingReason('own.n_holders')
  const rSi = useMissingReason('si.days')
  const rScore = useMissingReason('sm.score')
  return (
    <div className="space-y-3 pt-3">
      <Panel title="Ownership summary" subtitle={<span className="inline-flex flex-wrap items-center gap-3"><LagNote lag={d.lag} /><Link to="/smart-money" className="text-2xs text-accent-text hover:underline">Smart money overview →</Link></span>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
          <Stat label="Institutional (13F)" value={<Avail value={s.inst_own_pct} field="own.inst_own_pct">{fmtPct(s.inst_own_pct, 1)}</Avail>} sub={<Signed v={s.inst_own_chg} pp />} />
          <Stat label="13F holders" value={fmtInt(s.n_holders)} sub={`${fmtInt(s.n_hf_holders)} hedge-fund-like*`} />
          <Stat label="Smart cohort*" value={fmtInt(s.n_smart_holders)} sub="holders (model-based)" />
          <Stat label="Crowding*" value={s.crowding_pct != null ? <PercentileBar value={s.crowding_pct * 100} /> : DASH} sub="HF top-20 overlap" />
          <Stat label="Top-10 holders" value={fmtPct(s.top10_share, 0)} sub={`HHI ${fmtNum(s.hhi, 3)}`} />
          <Stat label="Short interest" value={<Avail value={s.si_pct_out} field="si.si_pct_out">{fmtPct(s.si_pct_out, 1)}</Avail>} sub={`${fmtNum(s.days_to_cover, 1)} days to cover${s.squeeze_flag ? ' · squeeze risk*' : ''}`} />
          <Stat label="EU / UK shorts" value={s.eu_short_pct ? `${fmtNum(s.eu_short_pct, 2)}%` : DASH} sub="disclosed net short" />
          <Stat label="Smart-money score*" value={s.score_pct != null ? <PercentileBar value={s.score_pct} /> : DASH} sub={s.score_date ? `as of ${fmtDate(s.score_date)}` : undefined} />
        </div>
        <p className="mt-2 text-2xs text-faint">* {L.model_based}. Shares outstanding: {fmtCompact(s.shares_out)} ({s.shares_out_source ?? 'unknown source'}).</p>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Institutional ownership trend" subtitle={<OfficialTag label="SEC 13F - point-in-time on the filing cutoff of each quarter" />}>
          {d.ownership_trend.length ? <TrendChart d={d} /> : <NoDataState reason={r13f} title="No 13F history" />}
        </Panel>
        <Panel title="Short interest trend" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="FINRA (settlement dates)" /><span className="text-2xs text-muted">{L.si_float}</span></span>}>
          {d.short_interest.length ? <SiChart d={d} /> : <NoDataState reason={rSi} title="No short-interest data" />}
        </Panel>
      </div>
      <HoldersPanel d={d} mix={mix} />
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Insider timeline" subtitle={<OfficialTag label="Form 4 / national regulator filings (open-market buys and sells, 2 years)" />} flush>
          {d.insider_timeline.length ? (
            <div className="max-h-80 overflow-auto">
              <table className="w-full text-xs" aria-label="Insider transactions">
                <thead className="sticky top-0 bg-surface text-left text-muted">
                  <tr className="border-b border-border">{['Date', 'Insider', 'Role', 'Type', 'Value'].map((h, i) => <th key={h} className={`px-2 py-1.5 font-medium ${i === 4 ? 'text-right' : ''}`}>{h}</th>)}</tr>
                </thead>
                <tbody>
                  {d.insider_timeline.map((t, i) => (
                    <tr key={i} className="border-b border-border/60">
                      <td className="num px-2 py-1 text-muted">{fmtDate(t.transaction_date)}</td>
                      <td className="max-w-[180px] truncate px-2 py-1" title={t.insider_name ?? ''}>{t.insider_name ?? DASH}</td>
                      <td className="px-2 py-1 text-muted">{t.role ?? DASH}</td>
                      <td className="px-2 py-1"><Badge tone={t.txn_type === 'buy' ? 'up' : 'down'}>{t.txn_type}</Badge>{t.cluster_id ? <Badge tone="accent" className="ml-1">cluster</Badge> : null}</td>
                      <td className="num px-2 py-1 text-right">{fmtCompact(t.value_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No insider transactions" className="py-6" />
          )}
        </Panel>
        <EuPanel d={d} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="13D / 13G filings" subtitle={<OfficialTag label="SEC" />} flush>
          {d.activism.length ? (
            <ul className="divide-y divide-border/60 text-xs">
              {d.activism.slice(0, 20).map((e) => (
                <li key={e.filing_id} className="flex items-center gap-2 px-2 py-1">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(e.event_date)}</span>
                  <EventBadge type={e.event_type} />
                  <span className="min-w-0 flex-1 truncate font-medium" title={e.filer_name ?? ''}>{e.filer_name ?? 'Unknown filer'}</span>
                  <a href={e.url} target="_blank" rel="noreferrer" className="shrink-0 text-2xs text-accent-text hover:underline">EDGAR</a>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No 13D / 13G filings" className="py-6" />
          )}
        </Panel>
        <Panel title="Smart-money score history" subtitle={<ModelBasedTag tip={L.score}>percentile within the universe</ModelBasedTag>}>
          {d.score_history.length ? <ScoreChart d={d} /> : <NoDataState reason={rScore} title="Not scored" />}
        </Panel>
      </div>
      <SourcesFooter sources={d.sources} />
    </div>
  )
}

function HoldersPanel({ d, mix }: { d: CoOwnership; mix: ReturnType<typeof holderMix> }) {
  const [view, setView] = useState<'holders' | 'exited'>('holders')
  const L = d.labels as Record<string, string>
  return (
    <Panel
      title="13F holders"
      subtitle={<span className="inline-flex flex-wrap items-center gap-2"><LagNote lag={d.lag} /><span className="text-2xs text-muted">{mix.newOrAdded} new / added · {mix.trimmed} trimmed · {mix.hf} hedge-fund-like* · {mix.smart} smart cohort*</span></span>}
      actions={<Segmented<'holders' | 'exited'> label="Holder view" size="xs" value={view} onChange={setView} options={[{ value: 'holders', label: `Top holders (${d.holders.length})` }, { value: 'exited', label: `Exited (${d.exited.length})` }]} />}
      flush
    >
      {view === 'holders' ? (
        d.holders.length ? (
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-xs" aria-label="13F holders">
              <thead className="sticky top-0 bg-surface text-left text-muted">
                <tr className="border-b border-border">
                  {['Filer', 'Shares', 'Value', '% out', 'Change', 'Status', 'Weight in book', 'Filed'].map((h, i) => <th key={h} className={`px-2 py-1.5 font-medium ${i ? 'text-right' : ''}`}>{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {d.holders.map((h) => {
                  const st = HOLDER_STATUS[h.status]
                  return (
                    <tr key={h.filer_cik} className="border-b border-border/60">
                      <td className="max-w-[260px] px-2 py-1">
                        <span className="truncate" title={h.filer_name ?? h.filer_cik}>{h.filer_name ?? h.filer_cik}</span>
                        {h.hedge_fund_like ? <Badge tone="info" className="ml-1" title={L.hedge_fund_like}>HF-like*</Badge> : null}
                        {h.in_smart_cohort ? <Badge tone="accent" className="ml-1" title={L.smart_cohort}>smart*</Badge> : null}
                      </td>
                      <td className="num px-2 py-1 text-right">{fmtCompact(h.shares)}</td>
                      <td className="num px-2 py-1 text-right">{fmtCompact(h.value_usd)}</td>
                      <td className="num px-2 py-1 text-right">{fmtPct(h.pct_out, 2)}</td>
                      <td className="px-2 py-1 text-right"><Signed v={h.change_pct} digits={0} /></td>
                      <td className="px-2 py-1 text-right"><Badge tone={st.tone}>{st.label}</Badge></td>
                      <td className="num px-2 py-1 text-right">{fmtPct(h.weight_in_portfolio, 2)}</td>
                      <td className="num px-2 py-1 text-right text-muted">{fmtDate(h.filed_date)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No 13F holders (not a 13F-reportable security or not mapped)" className="py-6" />
        )
      ) : d.exited.length ? (
        <ul className="divide-y divide-border/60 text-xs">
          {d.exited.map((h) => (
            <li key={h.filer_cik} className="flex items-center gap-2 px-2 py-1">
              <span className="min-w-0 flex-1 truncate">{h.filer_name ?? h.filer_cik}</span>
              {h.hedge_fund_like ? <Badge tone="info">HF-like*</Badge> : null}
              <span className="num text-muted">{fmtCompact(h.shares_prev)} shares last quarter</span>
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No exits among last quarter's top holders" className="py-6" />
      )}
    </Panel>
  )
}

function EuPanel({ d }: { d: CoOwnership }) {
  const t = useChartTokens()
  const ser = useMemo(() => euShortSeries(d.eu_shorts.notifications), [d])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${fmtNum(v as number, 2)}%` },
    xAxis: { type: 'category', data: ser.x, axisLabel: { color: t.muted } },
    yAxis: { type: 'value', min: 0, axisLabel: { formatter: (v: number) => `${v}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    series: [{ type: 'line', step: 'end', name: 'Disclosed net short', data: ser.y, showSymbol: false, lineStyle: { width: 2, color: t.down }, areaStyle: { color: t.downSoft } }],
  }), [ser, t])
  return (
    <Panel title="EU / UK net short positions" subtitle={<OfficialTag label="regulator registers (>= 0.5% of capital)" />} flush>
      {d.eu_shorts.current.length || d.eu_shorts.notifications.length ? (
        <div className="space-y-2 p-2">
          {ser.x.length > 1 && <EChart option={option} height={160} ariaLabel="Disclosed net short position over time" />}
          <table className="w-full text-xs" aria-label="Current net short positions">
            <thead className="text-left text-muted">
              <tr className="border-b border-border">{['Holder', 'Regulator', 'Net short', 'Change', 'Date'].map((h, i) => <th key={h} className={`px-2 py-1 font-medium ${i >= 2 ? 'text-right' : ''}`}>{h}</th>)}</tr>
            </thead>
            <tbody>
              {d.eu_shorts.current.map((r, i) => (
                <tr key={i} className="border-b border-border/60">
                  <td className="max-w-[200px] truncate px-2 py-1">{r.position_type === 'aggregate' ? <Badge tone="info">aggregate</Badge> : r.holder_name}</td>
                  <td className="px-2 py-1 text-muted">{r.regulator_label}</td>
                  <td className="num px-2 py-1 text-right">{fmtNum(r.position_pct, 2)}%</td>
                  <td className="px-2 py-1 text-right">{euChange(r) == null ? <Badge tone="info">new</Badge> : <Signed v={(euChange(r) ?? 0) / 100} invert digits={2} pp />}</td>
                  <td className="num px-2 py-1 text-right text-muted">{fmtDate(r.position_date)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!d.eu_shorts.current.length && <p className="text-2xs text-muted">No position currently at or above 0.5%; {d.eu_shorts.notifications.length} past notifications.</p>}
        </div>
      ) : (
        <EmptyState title="No EU / UK short disclosures" message="Only issuers supervised by FCA, AMF, CNMV, AFM, CBI, FSMA, FI or Finanstilsynet have published positions" className="py-6" />
      )}
    </Panel>
  )
}

function TrendChart({ d }: { d: CoOwnership }) {
  const t = useChartTokens()
  const s = useMemo(() => trendSeries(d.ownership_trend), [d])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 8, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8 },
    tooltip: { trigger: 'axis' },
    xAxis: { type: 'category', data: s.x, axisLabel: { color: t.muted } },
    yAxis: [
      { type: 'value', min: 0, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
      { type: 'value', min: 0, axisLabel: { color: t.muted }, splitLine: { show: false } },
    ],
    series: [
      { type: 'line', name: 'Institutional % of shares', data: s.pct, lineStyle: { width: 2, color: t.series[0] }, itemStyle: { color: t.series[0] }, symbolSize: 6, tooltip: { valueFormatter: (v: unknown) => fmtPct(v as number, 1) } },
      { type: 'bar', name: '13F holders', yAxisIndex: 1, data: s.holders, barMaxWidth: 14, itemStyle: { color: t.ref, opacity: 0.5 } },
    ],
  }), [s, t])
  return <EChart option={option} height={240} ariaLabel="Institutional ownership and number of 13F holders by quarter" />
}

function SiChart({ d }: { d: CoOwnership }) {
  const t = useChartTokens()
  const s = useMemo(() => siSeries(d.short_interest), [d])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 8, top: 12, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => (s.unit === 'pct' ? fmtPct(v as number, 2) : fmtCompact(v as number)) },
    xAxis: { type: 'category', data: s.x, axisLabel: { color: t.muted } },
    yAxis: { type: 'value', min: 0, axisLabel: { formatter: (v: number) => (s.unit === 'pct' ? `${(v * 100).toFixed(1)}%` : fmtCompact(v)), color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    series: [{ type: 'line', name: s.unit === 'pct' ? 'Short interest % of shares outstanding' : 'Shares short', data: s.y, showSymbol: false, lineStyle: { width: 2, color: t.down }, areaStyle: { color: t.downSoft } }],
  }), [s, t])
  return <EChart option={option} height={240} ariaLabel="Short interest by settlement date" />
}

function ScoreChart({ d }: { d: CoOwnership }) {
  const t = useChartTokens()
  const rows = d.score_history
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 8, top: 12, bottom: 8, containLabel: true },
    tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => fmtNum(v as number, 0) },
    xAxis: { type: 'category', data: rows.map((r) => r.as_of_date), axisLabel: { color: t.muted } },
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    series: [{ type: 'line', name: 'Score percentile (model-based)', data: rows.map((r) => r.score_pct), showSymbol: false, lineStyle: { width: 2, color: t.accent } }],
  }), [rows, t])
  return <EChart option={option} height={200} ariaLabel="Smart-money score percentile history" />
}
