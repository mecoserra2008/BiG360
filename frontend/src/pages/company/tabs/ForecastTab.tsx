import { useMemo, useState } from 'react'
import type { CompanyForecast, ForecastSeries, Scenario } from '@/api/types'
import { useCompanyForecast } from '@/api/hooks'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { type ChartTokens, alpha, divergingColors, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { PendingState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtCompact, fmtMillions, fmtNum, fmtPct, fmtPrice, fmtValue } from '@/lib/format'
import { cn } from '@/lib/utils'
import { CostOfCapitalPanel, ValuationPanel } from './ValuationPanels'

const SC_LABEL: Record<Scenario, string> = { bear: 'Bear', base: 'Base', bull: 'Bull' }

export default function ForecastTab({ id }: { id: string }) {
  const q = useCompanyForecast(id)
  return (
    <QueryView query={q} skeleton={<div className="grid grid-cols-3 gap-3"><Skeleton className="col-span-2 h-80" /><Skeleton className="h-80" /></div>}>
      {(d) => (d.series.length ? <ForecastBody d={d} /> : <PendingState detail="No forecast scenarios published for this company yet." />)}
    </QueryView>
  )
}

function fanOption(s: ForecastSeries, t: ChartTokens, ccy: string): EChartsOption {
  const hist = s.historical
  const periods = [...hist.map((h) => h.period), ...s.projections.base.map((p) => p.period)]
  const n = hist.length
  const last = hist[n - 1].value
  const pad = (vals: number[]) => [...Array(n - 1).fill(null), last, ...vals]
  const bear = s.projections.bear.map((p) => p.value)
  const base = s.projections.base.map((p) => p.value)
  const bull = s.projections.bull.map((p) => p.value)
  const fmt = (v: number) => (s.unit === 'currency' ? fmtCompact(v * 1e6, 2) : fmtNum(v, 2))
  return {
    grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
    legend: { top: 0, right: 0, data: ['Historical', 'Bull', 'Base', 'Bear'] },
    tooltip: {
      trigger: 'axis',
      formatter: (ps: unknown) => {
        const arr = ps as { axisValue: string; seriesName: string; value: number | null; color: string }[]
        const rows = arr.filter((p) => p.value !== null && p.value !== undefined && !p.seriesName.startsWith('_'))
        return ttTitle(arr[0]?.axisValue ?? '') + rows.map((p) => ttRow(p.color, p.seriesName, fmt(p.value as number))).join('')
      },
    },
    xAxis: { type: 'category', data: periods, boundaryGap: false },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => (s.unit === 'currency' ? fmtCompact(v * 1e6, 1) : fmtNum(v, 1)) }, name: s.unit === 'currency' ? ccy : '', nameTextStyle: { align: 'right' } },
    series: [
      // band between bear and bull (stack trick, hidden from legend/tooltip)
      { name: '_lo', type: 'line', data: pad(bear), stack: 'band', lineStyle: { opacity: 0 }, symbol: 'none', silent: true },
      { name: '_band', type: 'line', data: pad(bull.map((v, i) => v - bear[i])).map((v, i) => (i === n - 1 ? 0 : v)), stack: 'band', lineStyle: { opacity: 0 }, areaStyle: { color: alpha(t.accent, 0.12) }, symbol: 'none', silent: true },
      { name: 'Historical', type: 'line', data: [...hist.map((h) => h.value), ...Array(base.length).fill(null)], lineStyle: { color: t.fg, width: 2 }, itemStyle: { color: t.fg }, symbol: 'circle', symbolSize: 4 },
      { name: 'Bull', type: 'line', data: pad(bull), lineStyle: { color: t.up, width: 1.5, type: 'dashed' }, itemStyle: { color: t.up }, symbol: 'none' },
      { name: 'Base', type: 'line', data: pad(base), lineStyle: { color: t.accent, width: 2 }, itemStyle: { color: t.accent }, symbol: 'circle', symbolSize: 4 },
      { name: 'Bear', type: 'line', data: pad(bear), lineStyle: { color: t.down, width: 1.5, type: 'dashed' }, itemStyle: { color: t.down }, symbol: 'none' },
    ],
  }
}

function ForecastBody({ d }: { d: CompanyForecast }) {
  const t = useChartTokens()
  const [metric, setMetric] = useState<ForecastSeries['metric']>('revenue')
  const [scenario, setScenario] = useState<Scenario>('base')
  const series = d.series.find((s) => s.metric === metric) ?? d.series[0]
  const option = useMemo(() => fanOption(series, t, d.currency), [series, t, d.currency])
  const rd = d.reverseDcf
  const upside = (rd.fairValue.base / rd.currentPrice) - 1

  const sensOption = useMemo<EChartsOption>(() => {
    const waccs = [...new Set(rd.sensitivity.map((s) => s.wacc))]
    const gs = [...new Set(rd.sensitivity.map((s) => s.terminalGrowth))]
    const data = rd.sensitivity.map((s) => [gs.indexOf(s.terminalGrowth), waccs.indexOf(s.wacc), +((s.fairValue / rd.currentPrice - 1) * 100).toFixed(1), s.fairValue])
    const m = Math.max(10, ...data.map((x) => Math.abs(x[2])))
    return {
      grid: { left: 4, right: 4, top: 4, bottom: 4, containLabel: true },
      tooltip: { formatter: (p: unknown) => { const v = (p as { value: number[] }).value; return ttTitle(`WACC ${fmtPct(waccs[v[1]], 1)} · g ${fmtPct(gs[v[0]], 1)}`) + ttRow(t.fg, 'Fair value', fmtPrice(v[3])) + ttRow(t.fg, 'vs price', `${v[2] > 0 ? '+' : ''}${v[2]}%`) } },
      xAxis: { type: 'category', data: gs.map((g) => fmtPct(g, 1)), name: 'g', axisLine: { show: false }, position: 'top' },
      yAxis: { type: 'category', data: waccs.map((w) => fmtPct(w, 1)), axisLine: { show: false }, inverse: true },
      visualMap: { show: false, min: -m, max: m, dimension: 2, inRange: { color: divergingColors(t) } },
      series: [{ type: 'heatmap', data, label: { show: true, color: t.fg, fontSize: 10, fontFamily: t.fontMono, formatter: (p: unknown) => fmtPrice((p as { value: number[] }).value[3]) }, itemStyle: { borderColor: t.surface, borderWidth: 2 } }],
    }
  }, [rd, t])

  const projected = d.projected[scenario]

  return (
    <div className="grid grid-cols-12 gap-3">
      <ValuationPanel d={d} className="col-span-12 xl:col-span-8" />
      <CostOfCapitalPanel c={d.costOfCapital} className="col-span-12 xl:col-span-4" />
      <Panel
        title="Scenario fan chart"
        subtitle="Historical (as reported) and 5-year bear / base / bull projections"
        className="col-span-12 xl:col-span-8"
        actions={<Segmented label="Metric" value={metric} onChange={setMetric} options={d.series.map((s) => ({ value: s.metric, label: s.label }))} />}
      >
        <EChart option={option} height={300} ariaLabel={`${series.label} historical and projected scenarios`} />
      </Panel>

      <Panel title="Reverse DCF" subtitle="Growth the current price implies" className="col-span-12 xl:col-span-4">
        <div className="grid grid-cols-3 gap-3">
          <Stat label="Implied growth" value={fmtPct(rd.impliedGrowth, 1)} sub={`${rd.impliedYears}y, then ${fmtPct(rd.terminalGrowth, 1)}`} />
          <Stat label="Hist. 5y CAGR" value={fmtPct(rd.historicalGrowth5y, 1)} />
          <Stat label="Consensus" value={fmtPct(rd.consensusGrowth, 1)} sub={rd.consensusGrowth === null ? 'n/a' : undefined} />
        </div>
        <div className="mt-3 rounded-md border border-border bg-surface-2 p-2 text-xs text-muted">
          Market implies <span className="num font-semibold text-fg">{fmtPct(rd.impliedGrowth, 1)}</span> p.a. vs a 5-year track record of{' '}
          <span className="num font-semibold text-fg">{fmtPct(rd.historicalGrowth5y, 1)}</span> —{' '}
          {rd.impliedGrowth > rd.historicalGrowth5y + 0.02 ? <span className="text-down">expectations look demanding.</span> : rd.impliedGrowth < rd.historicalGrowth5y - 0.02 ? <span className="text-up">expectations look undemanding.</span> : 'expectations are in line with history.'}
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2">
          {(['bear', 'base', 'bull'] as Scenario[]).map((s) => (
            <div key={s} className={cn('rounded-md border px-2 py-1.5', s === 'base' ? 'border-accent/50 bg-accent-soft' : 'border-border')}>
              <div className="label-caps">{SC_LABEL[s]} value</div>
              <div className="num text-md font-medium">{fmtPrice(rd.fairValue[s])}</div>
              <Delta value={rd.fairValue[s] / rd.currentPrice - 1} format={(v) => fmtPct(v, 0, true)} className="text-2xs" />
            </div>
          ))}
        </div>
        <div className="mt-3 flex items-center justify-between text-xs">
          <span className="text-muted">WACC {fmtPct(rd.wacc, 1)} · price {fmtPrice(rd.currentPrice)}</span>
          <Badge tone={upside > 0.1 ? 'up' : upside < -0.1 ? 'down' : 'neutral'}>{upside > 0 ? '▲' : '▼'} Base {fmtPct(upside, 0, true)}</Badge>
        </div>
      </Panel>

      <Panel
        title="Projected statements"
        subtitle={`${SC_LABEL[scenario]} case · ${d.currency} millions`}
        className="col-span-12 xl:col-span-7 xl:row-span-2"
        flush
        actions={<Segmented label="Scenario" value={scenario} onChange={setScenario} options={[{ value: 'bear', label: 'Bear' }, { value: 'base', label: 'Base' }, { value: 'bull', label: 'Bull' }]} />}
      >
        <table className="w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Line</th>
              {d.projectionPeriods.map((p) => (
                <th key={p} scope="col" className="num border-b border-border px-2 text-right font-semibold">{p}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {projected.map((r) => (
              <tr key={r.id} className="hover:bg-surface-2">
                <th scope="row" className={cn('h-[26px] border-b border-border px-3 text-left font-normal', r.isTotal ? 'font-semibold text-fg' : 'pl-6 text-muted')}>{r.label}</th>
                {r.values.map((v, i) => (
                  <td key={i} className={cn('num border-b border-border px-2 text-right', !r.isTotal && 'text-muted')}>
                    {r.unit === 'pct' ? fmtPct(v, 1) : r.unit === 'number' ? fmtNum(v, 2) : fmtMillions(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="Assumptions" subtitle="Driver inputs by scenario vs 5-year history" className="col-span-12 xl:col-span-5" flush>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Driver</th>
              <th scope="col" className="border-b border-border px-1.5 text-right font-semibold">Bear</th>
              <th scope="col" className="border-b border-border px-1.5 text-right font-semibold text-fg">Base</th>
              <th scope="col" className="border-b border-border px-1.5 text-right font-semibold">Bull</th>
              <th scope="col" className="border-b border-border px-3 text-right font-semibold">5y</th>
            </tr>
          </thead>
          <tbody>
            {d.assumptions.map((a) => (
              <tr key={a.id}>
                <th scope="row" className="h-[26px] border-b border-border px-3 text-left font-normal whitespace-nowrap text-muted">{a.label}</th>
                <td className="num border-b border-border px-1.5 text-right">{fmtValue(a.bear, a.unit)}</td>
                <td className="num border-b border-border px-1.5 text-right font-semibold text-fg">{fmtValue(a.base, a.unit)}</td>
                <td className="num border-b border-border px-1.5 text-right">{fmtValue(a.bull, a.unit)}</td>
                <td className="num border-b border-border px-3 text-right text-faint">{fmtValue(a.historical5y, a.unit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel title="DCF sensitivity" subtitle="Fair value per share · WACC × terminal growth (API grid, base scenario)" className="col-span-12 xl:col-span-5 xl:col-start-8" bodyClassName="p-1.5">
        <EChart option={sensOption} height={220} ariaLabel="DCF sensitivity grid" />
      </Panel>
    </div>
  )
}
