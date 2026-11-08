import { useMemo, useState } from 'react'
import { CORP_LABEL, expectationSeries, isOverdue, reactionSeries, sessionLabel } from '@/api/earnings/adapters'
import { useCompanyEarnings } from '@/api/earnings/hooks'
import type { ECompany, EExpectationRow } from '@/api/earnings/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtDateTime, fmtNum, fmtPct } from '@/lib/format'
import { Missing, NoDataState } from '@/components/ui/missing'
import { makeReason, type AvailabilityReason } from '@/lib/availability'
import { ModelBasedTag, Signed, SourceBadge } from '../../earnings/shared'
import { fmtEps, fmtRev } from '../../earnings/fmt'

export default function EarningsTab({ id }: { id: string }) {
  const q = useCompanyEarnings(id)
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  if (!q.data) return <SkeletonRows rows={10} className="pt-3" />
  if (q.data.data_status === 'pending') return <PendingState detail="The earnings jobs have not produced data for this snapshot yet." />
  return <Body d={q.data} />
}

function Body({ d }: { d: ECompany }) {
  const s = d.reaction_stats
  const next = d.next[0]
  return (
    <div className="space-y-3 pt-3">
      <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <Panel title="Next earnings" subtitle={next ? next.source_label : 'No upcoming date'}>
          {next ? (
            <div className="space-y-2">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="num text-2xl font-semibold">{fmtDate(next.date)}</span>
                <span className="text-sm text-muted">{sessionLabel(next.session)}</span>
                <SourceBadge source={next.date_source} window={{ lo: next.window_lo, hi: next.window_hi }} />
                {isOverdue(next, d.as_of) && <Badge tone="warn">awaiting release</Badge>}
              </div>
              {next.date_source === 'model_projection' && next.window_lo && (
                <div className="text-xs text-muted">Plausible window {fmtDate(next.window_lo)} → {fmtDate(next.window_hi)} ({next.source_label?.includes('statutory') ? 'statutory filing deadline: no release history for this company yet' : 'from the company’s reporting cadence'})</div>
              )}
              <div className="text-xs text-muted">Period {next.fiscal_period ?? ''} ending {fmtDate(next.period_end)}</div>
              <div className="grid grid-cols-2 gap-3 pt-1">
                <Stat label="EPS expectation" value={fmtEps(next.eps_expected)} sub={next.eps_lo80 != null ? `80% ${fmtEps(next.eps_lo80)} … ${fmtEps(next.eps_hi80)}` : undefined} />
                <Stat label={`Revenue expectation${next.currency ? ` (${next.currency})` : ''}`} value={fmtRev(next.rev_expected)} />
              </div>
              <ModelBasedTag />
              {d.next.length > 1 && (
                <div className="border-t border-border pt-2 text-xs text-muted">
                  Then {d.next.slice(1).map((n) => `${fmtDate(n.date)} (${n.fiscal_period ?? 'period'} · ${n.source_label})`).join(', ')}
                </div>
              )}
            </div>
          ) : (
            d.availability?.next ? <NoDataState title="No upcoming date" reason={d.availability.next} /> : <EmptyState title="No upcoming date" message="No company-announced, provider or projected date for this issuer." />
          )}
        </Panel>
        <Panel title="How this stock usually reacts" subtitle={`${s.n_reactions} releases with a measured reaction · market model vs country index`}>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Mean |CAR [−1,+1]|" value={fmtPct(s.mean_abs_car_m1_p1, 1)} sub={`median ${fmtPct(s.median_car_m1_p1, 1, true)}`} />
            <Stat label="Move vs normal" value={s.median_move_ratio != null ? `${fmtNum(s.median_move_ratio, 2)}×` : DASH} sub="median |CAR| / realised-vol baseline" />
            <Stat label="Positive reactions" value={fmtPct(s.pct_positive_car_m1_p1, 0)} />
            <Stat label="Drift [+1,+20]" value={fmtPct(s.mean_car_p1_p20, 1, true)} sub={`continues ${fmtPct(s.drift_continuation_rate, 0)}`} />
            <Stat label="Gap filled ≤ 5d" value={fmtPct(s.gap_fill_rate, 0)} />
            <Stat label="Beat model EPS" value={fmtPct(s.pct_beat_eps, 0)} sub="model-based, not consensus" />
            <Stat label="Mean |EPS surprise|" value={s.mean_abs_eps_surprise_z != null ? `${fmtNum(s.mean_abs_eps_surprise_z, 2)}σ` : DASH} />
            <Stat label="PEAD signal" value={d.pead?.pead_score != null ? fmtNum(d.pead.pead_score, 2, true) : DASH} sub={d.pead?.decile ? `decile ${d.pead.decile} · ${fmtDate(d.pead.signal_date)}` : 'model-based'} />
          </div>
          <ReactionChart d={d} />
        </Panel>
      </div>
      <ExpectationPanel rows={d.expectations} currency={d.history[0]?.currency ?? d.security.currency} reason={d.availability?.expectations} />
      <HistoryTable d={d} />
      <CorporatePanel d={d} />
    </div>
  )
}

function ReactionChart({ d }: { d: ECompany }) {
  const why = d.availability?.reactions ?? d.availability?.history
  const t = useChartTokens()
  const rows = useMemo(() => reactionSeries(d.history).filter((r) => r.car != null), [d.history])
  const option = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 12, top: 28, bottom: 8, containLabel: true },
    legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8 },
    tooltip: {
      trigger: 'axis',
      formatter: (ps: unknown) => {
        const i = (ps as { dataIndex: number }[])[0]?.dataIndex ?? 0
        const r = rows[i]
        if (!r) return ''
        return ttTitle(`${r.label} · released ${r.date}`) + ttRow(r.car && r.car > 0 ? t.up : t.down, 'CAR [−1,+1]', fmtPct(r.car, 1, true)) + ttRow(t.ref, 'Normal move (±)', fmtPct(r.baseline, 1)) + ttRow(t.series[0], 'Drift [+1,+20]', fmtPct(r.drift, 1, true))
      },
    },
    xAxis: { type: 'category', data: rows.map((r) => r.label), axisLabel: { color: t.muted, hideOverlap: true } },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%`, color: t.muted }, splitLine: { lineStyle: { color: t.grid } } },
    series: [
      { type: 'bar', name: 'CAR [−1,+1]', barMaxWidth: 14, data: rows.map((r) => ({ value: r.car, itemStyle: { color: (r.car ?? 0) >= 0 ? t.up : t.down, borderRadius: (r.car ?? 0) >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } })) },
      { type: 'line', name: 'Normal move (+)', data: rows.map((r) => r.baseline), showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: t.ref }, itemStyle: { color: t.ref } },
      { type: 'line', name: 'Normal move (−)', data: rows.map((r) => (r.baseline != null ? -r.baseline : null)), showSymbol: false, lineStyle: { width: 1.5, type: 'dashed', color: t.ref }, itemStyle: { color: t.ref }, legendHoverLink: false },
    ],
  }), [rows, t])
  if (!rows.length) return why ? <NoDataState title="No measured reactions" reason={why} className="py-6" /> : <EmptyState title="No measured reactions" className="py-6" />
  return <EChart option={option} height={230} className="mt-3" ariaLabel="Abnormal return around each earnings release vs the realised-volatility baseline" />
}

function ExpectationPanel({ rows, currency, reason }: { rows: EExpectationRow[]; currency?: string | null; reason?: AvailabilityReason | null }) {
  const [item, setItem] = useState<'eps_diluted' | 'revenue'>('eps_diluted')
  const t = useChartTokens()
  const data = useMemo(() => expectationSeries(rows, item), [rows, item])
  const fmt = item === 'revenue' ? fmtRev : fmtEps
  const option = useMemo<EChartsOption>(() => {
    const xs = data.map((r) => r.period_end)
    return {
      grid: { left: 8, right: 12, top: 28, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, textStyle: { color: t.muted }, itemWidth: 12, itemHeight: 8, data: ['Model expectation', '80% interval', 'Actual'] },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const i = (ps as { dataIndex: number }[])[0]?.dataIndex ?? 0
          const r = data[i]
          if (!r) return ''
          return ttTitle(`${r.fiscal_period ?? ''} period ending ${r.period_end}`) + ttRow(t.series[0], 'Model expectation', fmt(r.expectation)) + ttRow(alpha(t.series[0], 0.4), '80% interval', `${fmt(r.lo80)} … ${fmt(r.hi80)}`) + ttRow(t.fg, 'Actual', fmt(r.actual)) + ttRow(t.ref, 'Surprise', r.surprise_pct != null ? fmtPct(r.surprise_pct, 1, true) : 'pending') + ttRow(t.ref, 'Model', r.model)
        },
      },
      xAxis: { type: 'category', data: xs, axisLabel: { color: t.muted, hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: t.muted, formatter: (v: number) => fmt(v) }, splitLine: { lineStyle: { color: t.grid } } },
      series: [
        { type: 'line', name: '80% base', data: data.map((r) => r.lo80), stack: 'band', lineStyle: { opacity: 0 }, showSymbol: false, silent: true, tooltip: { show: false } },
        { type: 'line', name: '80% interval', data: data.map((r) => (r.hi80 != null && r.lo80 != null ? r.hi80 - r.lo80 : null)), stack: 'band', lineStyle: { opacity: 0 }, areaStyle: { color: alpha(t.series[0], 0.18) }, itemStyle: { color: alpha(t.series[0], 0.4) }, showSymbol: false, silent: true },
        { type: 'line', name: 'Model expectation', data: data.map((r) => r.expectation), lineStyle: { width: 2, color: t.series[0] }, itemStyle: { color: t.series[0] }, symbolSize: 6 },
        { type: 'scatter', name: 'Actual', data: data.map((r) => r.actual), symbolSize: 9, itemStyle: { color: t.fg, borderColor: t.surface, borderWidth: 2 } },
      ],
    }
  }, [data, t, fmt])
  return (
    <Panel
      title="Model expectation vs actual"
      subtitle={<ModelBasedTag>Foster seasonal SARIMA / seasonal naive on the company&apos;s own first-reported history; best model per period by trailing accuracy</ModelBasedTag>}
      actions={<Segmented label="Item" size="xs" value={item} onChange={setItem} options={[{ value: 'eps_diluted', label: 'EPS' }, { value: 'revenue', label: `Revenue${currency ? ` (${currency})` : ''}` }]} />}
    >
      {data.length ? <EChart option={option} height={250} ariaLabel={`${item === 'revenue' ? 'Revenue' : 'EPS'} model expectation with 80% interval and reported actuals`} /> : reason ? <NoDataState title="No expectations" reason={reason} /> : <EmptyState title="No expectations" message="Not enough quarterly / half-yearly history for this issuer." />}
    </Panel>
  )
}

function HistoryTable({ d }: { d: ECompany }) {
  if (!d.history.length) return <Panel title="Release history">{d.availability?.history ? <NoDataState title="No releases recorded" reason={d.availability.history} /> : <EmptyState />}</Panel>
  // one specific reason per missing cell (no statements / no history / not published) instead of a universe-wide note
  const expWhy = d.availability?.expectations ?? null
  const rxWhy = d.availability?.reactions ?? null
  return (
    <Panel title="Release history" subtitle="Release time from the official filing / exchange disclosure where available" flush>
      <div className="max-h-[420px] overflow-auto">
        <table className="w-full min-w-[980px] text-xs">
          <thead className="sticky top-0 bg-surface text-left text-muted">
            <tr className="border-b border-border">
              {['Period', 'Released', 'Session', 'Source', 'EPS exp. (model)', 'EPS actual', 'EPS surprise', 'Rev. surprise', 'CAR [−1,+1]', 'CAR [0,+5]', 'CAR [+1,+20]', 'Move ×', 'Gap'].map((h, i) => (
                <th key={h} scope="col" className={`px-2 py-1.5 font-medium ${i > 3 ? 'text-right' : ''}`}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.history.map((h) => (
              <tr key={h.period_end} className="border-b border-border/60 hover:bg-surface-2">
                <td className="px-2 py-1 whitespace-nowrap">
                  {h.fiscal_period ?? ''} {h.fiscal_year ?? ''} <span className="text-faint">{h.period_end}</span>
                  {h.period_inferred && <Tip content="Period inferred from the calendar / cadence (no filing matched)"><span tabIndex={0} className="ml-1 text-warn">*</span></Tip>}
                </td>
                <td className="num px-2 py-1 whitespace-nowrap">{h.release_ts ? fmtDateTime(h.release_ts) : fmtDate(h.release_date)}</td>
                <td className="px-2 py-1 whitespace-nowrap text-muted">{sessionLabel(h.session)}</td>
                <td className="px-2 py-1">
                  <Tip content={h.source_label}>
                    <span tabIndex={0} className="inline-flex"><SourceBadge source={h.date_source} /></span>
                  </Tip>
                  {h.url && <a href={h.url} target="_blank" rel="noreferrer" className="ml-1 text-2xs text-accent hover:underline">doc</a>}
                </td>
                <td className="num px-2 py-1 text-right">{h.eps_expected != null ? fmtEps(h.eps_expected) : <Missing field="earn.eps_expected" reason={expWhy ?? NO_PERIOD_MODEL} />}</td>
                <td className="num px-2 py-1 text-right">{h.eps_actual != null ? fmtEps(h.eps_actual) : <Missing field="earn.eps_actual" reason={expWhy ?? ACTUAL_PENDING} />}</td>
                <td className="px-2 py-1 text-right"><Signed v={h.eps_surprise_pct} field="earn.eps_surprise_pct" reason={expWhy ?? (h.eps_expected == null ? NO_PERIOD_MODEL : ACTUAL_PENDING)} /></td>
                <td className="px-2 py-1 text-right"><Signed v={h.rev_surprise_pct} field="earn.rev_surprise_pct" reason={expWhy ?? (h.rev_expected == null ? NO_PERIOD_MODEL : ACTUAL_PENDING)} /></td>
                <td className="px-2 py-1 text-right"><Signed v={h.car_m1_p1} field="earn.car_m1_p1" reason={carWindow(h.release_date, d.as_of, 2) ?? rxWhy} /></td>
                <td className="px-2 py-1 text-right"><Signed v={h.car_0_p5} field="earn.car_0_p5" reason={carWindow(h.release_date, d.as_of, 8) ?? rxWhy} /></td>
                <td className="px-2 py-1 text-right"><Signed v={h.car_p1_p20} field="earn.car_p1_p20" reason={carWindow(h.release_date, d.as_of, 30) ?? rxWhy} /></td>
                <td className="num px-2 py-1 text-right">{h.move_ratio != null ? `${fmtNum(h.move_ratio, 1)}×` : DASH}</td>
                <td className="px-2 py-1 text-right whitespace-nowrap">
                  <Signed v={h.gap_pct} />
                  {h.gap_filled != null && <span className="ml-1 text-2xs text-muted">{h.gap_filled ? 'filled' : 'held'}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

function CorporatePanel({ d }: { d: ECompany }) {
  const [kind, setKind] = useState<'all' | 'dividends' | 'filings'>('all')
  const isAction = (t: string) => t.includes('dividend') || t.includes('split')
  const rows = d.corporate_events.filter((e) => kind === 'all' || (kind === 'dividends' ? isAction(e.event_type) : !isAction(e.event_type)))
  return (
    <Panel
      title="Corporate events"
      subtitle="Dividends and splits (corporate actions) · 8-K items, proxies and exchange disclosures"
      actions={<Segmented label="Event kind" size="xs" value={kind} onChange={setKind} options={[{ value: 'all', label: 'All' }, { value: 'dividends', label: 'Dividends & splits' }, { value: 'filings', label: 'Filings' }]} />}
      flush
    >
      {rows.length ? (
        <ul className="max-h-[300px] divide-y divide-border/60 overflow-auto text-xs">
          {rows.slice(0, 120).map((e) => (
            <li key={e.event_id} className="flex items-center gap-2 px-3 py-1">
              <span className="num w-24 shrink-0 text-muted">{fmtDate(e.date)}</span>
              {e.upcoming && <Badge tone="accent">upcoming</Badge>}
              <span className="shrink-0 font-medium">{CORP_LABEL[e.event_type] ?? e.event_type}</span>
              <span className="min-w-0 truncate text-muted" title={e.title}>{e.title}</span>
              <span className="ml-auto shrink-0 text-2xs text-faint">{e.source_label}</span>
              {e.url && <a href={e.url} target="_blank" rel="noreferrer" className="shrink-0 text-2xs text-accent hover:underline">doc</a>}
            </li>
          ))}
        </ul>
      ) : (
        <EmptyState title="No corporate events" />
      )}
    </Panel>
  )
}

/** A reaction window that has not ended yet (release + N calendar days after the as-of date). */
function carWindow(release: string | null | undefined, asOf: string, days: number) {
  if (!release) return null
  const end = new Date(`${release.slice(0, 10)}T00:00:00Z`)
  end.setUTCDate(end.getUTCDate() + days)
  const e = end.toISOString().slice(0, 10)
  return e > asOf ? makeReason('not_yet_published', `Reaction window not complete yet (ends ~${e})`, e) : null
}

/** A released period without a model expectation although the company has expectations elsewhere: too little history. */
const NO_PERIOD_MODEL = makeReason('insufficient_data', 'No model expectation for this period: too little comparable history before this release')
/** The model expectation exists but the actual is not in the loaded statements yet (US: arrives with the 10-Q XBRL). */
const ACTUAL_PENDING = makeReason('not_yet_published', 'Actual not in the loaded statements yet (it arrives with the periodic report)')
