import { useMemo, useState } from 'react'
import { useEmEscalation, useEmGpr, useEvTaxonomy } from '@/api/events/hooks'
import { EV_RANGES, type EmGpr, type EvRange, isExplosive, pearson, rangeDays } from '@/api/events/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ModelBadge } from './shared'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pct = (v: unknown) => (fin(v) ? `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%` : DASH)

function ProbBar({ p }: { p: number | null | undefined }) {
  const v = fin(p) ? Math.max(0, Math.min(1, p)) : null
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="relative h-1.5 w-16 rounded-full bg-surface-3" aria-hidden>
        {v !== null && <span className={cn('absolute top-0 left-0 h-1.5 rounded-full', v >= 0.5 ? 'bg-down' : v >= 0.2 ? 'bg-warn' : 'bg-faint')} style={{ width: `${v * 100}%` }} />}
      </span>
      <span className="num w-10 text-right">{pct(p)}</span>
    </span>
  )
}

export function EscalationPanel() {
  const [group, setGroup] = useState('all')
  const [sort, setSort] = useState('p_escalation_7d')
  const q = useEmEscalation(group, sort)
  return (
    <Panel
      title="Escalation probabilities"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <ModelBadge /> Bivariate Hawkes fits per country × theme group (own + neighbour excitation): probability that event counts exceed the escalation threshold within 7 / 30 days
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <Select label="Theme group" value={group} onChange={setGroup} options={[{ value: 'all', label: 'All theme groups' }, ...(q.data?.theme_groups ?? []).filter((g) => g !== 'all').map((g) => ({ value: g, label: g }))]} />
          <Select
            label="Sort"
            value={sort}
            onChange={setSort}
            options={[
              { value: 'p_escalation_7d', label: 'P(7 days)' },
              { value: 'p_escalation_30d', label: 'P(30 days)' },
              { value: 'branching_ratio', label: 'Branching ratio' },
              { value: 'expected_7d', label: 'Expected 7d' },
            ]}
          />
        </div>
      }
      flush
    >
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-80" />}>
        {(d) => {
          const nExpl = d.items.filter(isExplosive).length
          return (
            <>
              <div className="max-h-[520px] overflow-auto">
                <table className="w-full text-xs" aria-label="Escalation probabilities">
                  <thead>
                    <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                      <th className="sticky top-0 bg-surface px-3 py-1">Country</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">Theme group</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">P(escalation) 7d</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">30d</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">Branching ratio</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">Expected 7d / threshold</th>
                      <th className="sticky top-0 bg-surface px-3 py-1 text-right">Events</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.items.map((e) => {
                      const expl = isExplosive(e)
                      return (
                        <tr key={`${e.iso2}-${e.theme_group}`} className={cn('border-b border-border/60', expl && 'bg-warn-soft/40')}>
                          <td className="px-3 py-1">
                            <span className="inline-flex items-center gap-1.5">
                              {e.iso2.length === 2 && <Flag country={e.iso2} />}
                              <span className="font-medium text-fg">{e.name ?? e.iso2}</span>
                            </span>
                          </td>
                          <td className="px-2 py-1 text-muted">{e.theme_group}</td>
                          <td className="px-2 py-1">
                            <ProbBar p={e.p_escalation_7d} />
                          </td>
                          <td className="px-2 py-1">
                            <ProbBar p={e.p_escalation_30d} />
                          </td>
                          <td className="num px-2 py-1 text-right">
                            {fin(e.branching_ratio) ? e.branching_ratio.toFixed(3) : DASH}{' '}
                            {expl && (
                              <Tip content="Raw branching ratio > 1: the self-exciting fit is explosive (non-stationary), so the probabilities are not reliable.">
                                <span tabIndex={0}>
                                  <Badge tone="warn">explosive</Badge>
                                </span>
                              </Tip>
                            )}
                            {!e.converged && <Badge tone="neutral">not converged</Badge>}
                          </td>
                          <td className="num px-2 py-1 text-right text-muted">
                            {fin(e.expected_7d) ? fmtInt(e.expected_7d) : DASH} / {fin(e.threshold_7d) ? fmtInt(e.threshold_7d) : DASH}
                          </td>
                          <td className="num px-3 py-1 text-right text-muted">{fmtInt(e.n_events)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <p className="border-t border-border px-3 py-2 text-2xs text-muted">
                As of {d.as_of ? fmtDate(d.as_of) : DASH} · {d.n_days} days of history · {nExpl} of {d.items.length} fits explosive (branching ratio &gt; 1, highlighted). Model-based; probabilities assume the fitted process persists.
              </p>
            </>
          )
        }}
      </QueryView>
    </Panel>
  )
}

export function GprPanel() {
  const [iso, setIso] = useState<string | null>(null)
  const tax = useEvTaxonomy()
  const [range, setRange] = useState<EvRange>('1Y')
  const q = useEmGpr(iso, rangeDays(range, tax.data?.coverage))
  return (
    <Panel
      title="Geopolitical risk index"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <ModelBadge /> Ours (share of geopolitical-theme coverage, base = 100) vs the published Caldara-Iacoviello GPR (global only)
        </span>
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Segmented label="GPR history range" size="xs" value={range} onChange={setRange} options={EV_RANGES.filter((r) => r !== '7D').map((r) => ({ value: r, label: r }))} />
          <Select
            label="Country"
            value={iso ?? ''}
            onChange={(v) => setIso(v || null)}
            options={[
              { value: '', label: 'Global' },
              ...(q.data?.latest ?? []).map((l) => ({
                value: l.iso2,
                label: l.name ?? l.iso2,
              })),
            ]}
          />
        </div>
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-80" />}>
        {(d) => <GprBody d={d} onPick={setIso} />}
      </QueryView>
    </Panel>
  )
}

function GprBody({ d, onPick }: { d: EmGpr; onPick: (iso: string) => void }) {
  const t = useChartTokens()
  const s = d.series[0]
  const pts = useMemo(() => s?.points ?? [], [s])
  const corr = useMemo(
    () =>
      pearson(
        pts.map((p) => p.gpr),
        pts.map((p) => p.reference_gpr),
      ),
    [pts],
  )
  const corr7 = useMemo(
    () =>
      pearson(
        pts.map((p) => p.gpr_ma7),
        pts.map((p) => p.reference_gpr),
      ),
    [pts],
  )
  const hasRef = pts.some((p) => fin(p.reference_gpr))
  const val = (d.validation as Record<string, unknown> | null)?.validation as Record<string, unknown> | undefined
  const dg = val?.daily_global as Record<string, { pearson?: number; n?: number }> | undefined
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 30, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, textStyle: { color: t.muted, fontSize: 10 } },
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v) => (fin(v) ? (v as number).toFixed(1) : DASH),
      },
      xAxis: {
        type: 'category',
        data: pts.map((p) => p.date),
        axisLabel: { formatter: (v: string) => (pts.length > 120 ? v.slice(0, 7) : v.slice(5)) },
      },
      yAxis: [
        {
          type: 'value',
          scale: true,
          name: 'ours',
          nameTextStyle: { color: t.muted },
        },
        ...(hasRef
          ? [
              {
                type: 'value' as const,
                scale: true,
                name: 'published',
                nameTextStyle: { color: t.muted },
                splitLine: { show: false },
              },
            ]
          : []),
      ],
      series: [
        {
          name: 'Ours (daily)',
          type: 'line',
          data: pts.map((p) => p.gpr),
          showSymbol: false,
          lineStyle: { width: 1, color: t.ref },
          itemStyle: { color: t.ref },
        },
        {
          name: 'Ours (7-day avg)',
          type: 'line',
          data: pts.map((p) => p.gpr_ma7),
          showSymbol: false,
          lineStyle: { width: 2, color: t.series[0] },
          itemStyle: { color: t.series[0] },
        },
        ...(hasRef
          ? [
              {
                name: 'Caldara-Iacoviello (published)',
                type: 'line' as const,
                yAxisIndex: 1,
                data: pts.map((p) => p.reference_gpr),
                showSymbol: false,
                lineStyle: { width: 1.5, color: t.series[1] },
                itemStyle: { color: t.series[1] },
              },
            ]
          : []),
      ],
    }),
    [pts, t, hasRef],
  )
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,2fr)_minmax(260px,1fr)]">
      <div>
        <EChart option={option} height={300} ariaLabel={`Geopolitical risk index, ${s?.name ?? ''}`} />
        <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-2xs text-muted">
          {hasRef ? (
            <>
              <span>
                Correlation with published GPR: daily <span className="num font-semibold text-fg">{corr.r === null ? DASH : corr.r.toFixed(2)}</span> (n {corr.n}) · 7-day avg{' '}
                <span className="num font-semibold text-fg">{corr7.r === null ? DASH : corr7.r.toFixed(2)}</span> (n {corr7.n})
              </span>
              {dg?.ma7 && (
                <span>
                  Validation report (ma7): r {fin(dg.ma7.pearson) ? dg.ma7.pearson.toFixed(2) : DASH} · DOW-adjusted log: r {fin(dg.dow_adjusted_log?.pearson) ? dg.dow_adjusted_log.pearson.toFixed(2) : DASH}
                </span>
              )}
            </>
          ) : (
            <span>The published index is global only; country lines have no reference.</span>
          )}
          <span>
            Base {d.base_start ? fmtDate(d.base_start) : DASH} → {d.base_end ? fmtDate(d.base_end) : DASH} (short history: correlations are indicative)
          </span>
        </div>
        <p className="mt-1 text-2xs text-muted">{d.label}</p>
      </div>
      <div className="max-h-[340px] overflow-y-auto">
        <table className="w-full text-xs" aria-label="Highest GPR countries">
          <thead>
            <tr className="border-b border-border text-left text-2xs text-muted uppercase">
              <th className="px-2 py-1">Country</th>
              <th className="px-2 py-1 text-right">GPR 7d</th>
              <th className="px-2 py-1 text-right">Δ 7d</th>
            </tr>
          </thead>
          <tbody>
            {d.latest.map((l) => (
              <tr key={l.iso2} className="cursor-pointer border-b border-border/60 hover:bg-surface-2" onClick={() => onPick(l.iso2)}>
                <td className="px-2 py-0.5">
                  <span className="inline-flex items-center gap-1.5">
                    {l.iso2.length === 2 && <Flag country={l.iso2} />}
                    {l.name ?? l.iso2}
                  </span>
                </td>
                <td className="num px-2 py-0.5 text-right">{fin(l.gpr_ma7) ? l.gpr_ma7.toFixed(0) : DASH}</td>
                <td className={cn('num px-2 py-0.5 text-right', fin(l.change_7d) && (l.change_7d > 0 ? 'text-down' : 'text-up'))}>{fin(l.change_7d) ? `${l.change_7d > 0 ? '+' : '−'}${Math.abs(l.change_7d).toFixed(0)}` : DASH}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
