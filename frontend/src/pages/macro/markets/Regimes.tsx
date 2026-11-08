import { useMemo, useState } from 'react'
import { useRegimePerformance, useRegimeScopes } from '@/api/macro/hooks'
import { type MxRegimeScopes, type MxRegimes, REGIME_LABEL, type RegimeType, regimeRows, scopeOptions } from '@/api/macro/v34'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtChange, fmtMacro, zStyle } from '../fmt'

const hitPct = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? `${Math.round(v * 100)}%` : DASH)
const pct = (v: unknown, d = 1) => (typeof v === 'number' && Number.isFinite(v) ? `${fmtChange(v * 100, d)}%` : DASH)

export function Regimes() {
  const sc = useRegimeScopes()
  return (
    <QueryView query={sc} skeleton={<Skeleton className="h-96" />}>
      {(d) => <RegimesInner scopes={d} />}
    </QueryView>
  )
}

function RegimesInner({ scopes }: { scopes: MxRegimeScopes }) {
  const types = scopes.regime_types
  const [rt, setRt] = useState<RegimeType>(types[0]?.regime_type ?? 'cycle_quadrant')
  const typ = types.find((x) => x.regime_type === rt) ?? types[0]
  const opts = useMemo(() => scopeOptions(typ?.scopes ?? []), [typ])
  const [scopeSel, setScope] = useState<string | null>(null)
  const scope = scopeSel && opts.some((o) => o.value === scopeSel) ? scopeSel : (typ?.default_scope ?? opts[0]?.value ?? '')
  const meta = typ?.scopes.find((x) => x.scope === scope)
  const [et, setEt] = useState('sector')
  const [timing, setTiming] = useState<'contemporaneous' | 'next_month'>('contemporaneous')
  const q = useRegimePerformance(rt, scope, et, timing, !!meta?.has_performance)
  return (
    <Panel
      title="Performance by macro regime"
      subtitle="Historical monthly returns relative to the benchmark, grouped by the regime in force · current regime highlighted"
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Select
            label="Regime type"
            value={rt}
            onChange={(v) => {
              setRt(v)
              setScope(null)
            }}
            options={types.map((x) => ({ value: x.regime_type, label: x.label }))}
          />
          <Select label="Scope" value={scope} onChange={setScope} options={opts.map((o) => ({ value: o.value, label: o.label }))} />
          <Select label="Entities" value={et} onChange={setEt} options={['sector', 'subsector', 'region', 'factor', 'etf', 'index'].map((x) => ({ value: x, label: x[0].toUpperCase() + x.slice(1) }))} />
          <Segmented
            label="Timing"
            size="xs"
            value={timing}
            onChange={setTiming}
            options={[
              { value: 'contemporaneous', label: 'Same month' },
              { value: 'next_month', label: 'Next month' },
            ]}
          />
        </div>
      }
      flush
    >
      {meta && (
        <p className="border-b border-border px-3 py-1.5 text-2xs text-muted">
          {typ?.label} · {scope.toUpperCase()} · states {meta.states.map((x) => REGIME_LABEL[x] ?? x).join(', ')}
          {meta.first_date ? ` · history ${fmtDate(meta.first_date)} → ${meta.last_date ? fmtDate(meta.last_date) : DASH}` : ''}
          {meta.source ? ` · source ${meta.source}` : ''}
        </p>
      )}
      {!meta?.has_performance ? (
        <p className="py-10 text-center text-xs text-muted">
          No performance statistics for {scope.toUpperCase()} yet{meta?.current_state ? ` (current state: ${REGIME_LABEL[meta.current_state] ?? meta.current_state})` : ''}: the regime history is too short or not covered. Pick a scope without the “no performance data” note.
        </p>
      ) : (
        <QueryView query={q} skeleton={<Skeleton className="m-3 h-96" />}>
          {(d) => <RegimeBody key={`${rt}-${scope}`} d={d} states={meta.states} current={meta.current_state ?? null} modelBased={meta.model_based} />}
        </QueryView>
      )}
    </Panel>
  )
}

function RegimeBody({ d, states, current, modelBased }: { d: MxRegimes; states: string[]; current: string | null; modelBased: boolean }) {
  const t = useChartTokens()
  const cur = current ?? d.current_regime ?? null
  const order = useMemo(() => [...states.filter((x) => d.regimes.includes(x)), ...d.regimes.filter((x) => !states.includes(x))], [states, d.regimes])
  const rows = useMemo(() => regimeRows(d.items, cur).slice(0, 60), [d, cur])
  const [focus, setFocus] = useState<string>(cur ?? order[0] ?? '')
  const option = useMemo<EChartsOption>(() => {
    const r = rows.slice(0, 25)
    return {
      grid: { left: 8, right: 48, top: 8, bottom: 24, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const it = r[(ps as { dataIndex: number }[])[0].dataIndex]
          const c = it.cells[focus]
          return `${ttTitle(`${it.name} · ${REGIME_LABEL[focus] ?? focus}`)}${ttRow(t.series[0], 'annualized', pct(c?.ann_mean))}${ttRow(t.ref, 'hit rate', hitPct(c?.hit_rate))}${ttRow(t.ref, 't-stat', fmtChange(c?.t_stat))}${ttRow(t.ref, 'months', String(c?.n ?? DASH))}`
        },
      },
      xAxis: { type: 'value', name: 'annualized, % vs benchmark', nameLocation: 'middle', nameGap: 22, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}` } },
      yAxis: { type: 'category', inverse: true, data: r.map((x) => x.ticker || x.name), axisTick: { show: false }, axisLabel: { fontSize: 10, color: t.fg } },
      series: [
        {
          type: 'bar',
          barMaxWidth: 12,
          data: r.map((x) => {
            const c = x.cells[focus]
            const v = typeof c?.ann_mean === 'number' ? c.ann_mean : null
            const sig = typeof c?.t_stat === 'number' && Math.abs(c.t_stat) >= 1.96
            return { value: v, itemStyle: { color: (v ?? 0) >= 0 ? t.up : t.down, opacity: sig ? 1 : 0.45 } }
          }),
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }] },
        },
      ],
    } as EChartsOption
  }, [rows, focus, t])
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 text-xs">
        <span className="text-muted">Current regime:</span>
        <Badge tone="accent">{REGIME_LABEL[cur ?? ''] ?? cur ?? DASH}</Badge>
        {modelBased || d.model_based ? <Badge tone="info">model-based regimes</Badge> : <Badge tone="up">rule-based on official data</Badge>}
        <span className="ml-auto text-muted">Bars show:</span>
        <Segmented label="Regime for the bar chart" size="xs" value={focus} onChange={setFocus} options={order.map((r) => ({ value: r, label: `${REGIME_LABEL[r] ?? r}${r === cur ? ' (now)' : ''}` }))} />
      </div>
      <div className="grid gap-3 p-3 xl:grid-cols-[minmax(0,2fr)_minmax(320px,1fr)]">
        <div className="max-h-[560px] overflow-auto">
          <table className="w-full text-xs" aria-label="Relative returns by regime">
            <thead>
              <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                <th className="sticky top-0 bg-surface px-2 py-1">Entity</th>
                {order.map((r) => (
                  <th key={r} className={cn('sticky top-0 bg-surface px-2 py-1 text-right', r === cur && 'text-accent-text')}>
                    {REGIME_LABEL[r] ?? r}
                    {r === cur ? ' (now)' : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((x) => (
                <tr key={`${x.entity_id}-${x.weighting}`} className="border-b border-border/60">
                  <td className="max-w-[200px] truncate px-2 py-1 font-medium text-fg" title={x.name}>
                    {x.name}
                  </td>
                  {order.map((r) => {
                    const c = x.cells[r]
                    const sig = typeof c?.t_stat === 'number' && Math.abs(c.t_stat) >= 1.96
                    const b = typeof c?.ann_mean === 'number' ? Math.max(-3, Math.min(3, Math.round(c.ann_mean * 20))) : null
                    return (
                      <td key={r} className={cn('px-1 py-0.5 text-right', r === cur && 'outline outline-1 outline-accent/50 -outline-offset-1')}>
                        {c ? (
                          <span className="num block rounded-sm px-1" style={zStyle(b)} title={`hit ${hitPct(c.hit_rate)} · t ${fmtChange(c.t_stat)} · n ${c.n}`}>
                            <span className={sig ? 'font-semibold text-fg' : 'text-muted'}>{pct(c.ann_mean)}</span>
                            <span className="block text-2xs whitespace-nowrap text-muted">
                              hit {hitPct(c.hit_rate)} · t {fmtMacro(c.t_stat, 2)} · n {c.n}
                            </span>
                          </span>
                        ) : (
                          <span className="text-faint">{DASH}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <EChart option={option} height={Math.max(260, Math.min(rows.length, 25) * 20 + 50)} ariaLabel={`Annualized relative return in the ${focus} regime`} />
      </div>
      <p className="px-3 pb-2 text-2xs text-muted">
        {String((d.method as Record<string, unknown>).label ?? '')}. {String((d.method as Record<string, unknown>)[d.metric ?? 'relative'] ?? '')}. Timing: {d.timing === 'next_month' ? 'regime known at month-end, return of the following month (tradable)' : 'regime and return of the same month'}. Bold = |t| ≥ 1.96; faded bars = not significant.
      </p>
    </div>
  )
}
