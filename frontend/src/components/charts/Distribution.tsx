import { useMemo } from 'react'
import type { KpiDistribution } from '@/api/types'
import { fmtValue } from '@/lib/format'
import { EmptyState } from '../ui/feedback'
import { EChart, type EChartsOption, ttRow, ttTitle } from './EChart'
import { alpha, useChartTokens } from './tokens'

/**
 * Histogram of a KPI across a peer set, IQR shaded, median marked,
 * and the highlighted company's bin + value line in the brand colour.
 */
export function DistributionChart({ dist, highlightId, height = 150 }: { dist: KpiDistribution; highlightId?: string | null; height?: number }) {
  // the live API publishes quantiles only (no per-company values) → quantile strip instead of a histogram
  if (!dist.values.length) return <QuantileStrip dist={dist} height={height} />
  return <Histogram dist={dist} highlightId={highlightId} height={height} />
}

function QuantileStrip({ dist, height }: { dist: KpiDistribution; height: number }) {
  const q = [dist.p10, dist.p25, dist.median, dist.p75, dist.p90]
  if (!q.every((x) => Number.isFinite(x))) return <EmptyState title="No distribution" message="Not enough companies reported this KPI." className="py-6" />
  const lo = dist.p10
  const span = dist.p90 - dist.p10 || 1
  const x = (v: number) => `${4 + ((v - lo) / span) * 92}%`
  const f = (v: number) => fmtValue(v, dist.kpi.unit)
  return (
    <div className="flex flex-col justify-center px-2" style={{ height }} role="img" aria-label={`${dist.kpi.label}: p10 ${f(dist.p10)}, median ${f(dist.median)}, p90 ${f(dist.p90)}`}>
      <div className="relative h-8">
        <span className="absolute top-1/2 h-px bg-border-strong" style={{ left: x(dist.p10), right: `calc(100% - ${x(dist.p90)})` }} />
        <span className="absolute top-1 bottom-1 rounded-sm bg-surface-3 ring-1 ring-border-strong" style={{ left: x(dist.p25), width: `calc(${x(dist.p75)} - ${x(dist.p25)})` }} />
        <span className="absolute top-0 bottom-0 w-0.5 bg-fg" style={{ left: x(dist.median) }} />
        {[dist.p10, dist.p90].map((v, i) => (
          <span key={i} className="absolute top-2 bottom-2 w-px bg-muted" style={{ left: x(v) }} />
        ))}
      </div>
      <div className="relative mt-1 h-4 text-2xs text-muted">
        {(['p10', 'p25', 'p50', 'p75', 'p90'] as const).map((k, i) => (
          <span key={k} className="num absolute -translate-x-1/2 whitespace-nowrap" style={{ left: x(q[i]) }}>
            {i % 2 === 0 ? f(q[i]) : ''}
          </span>
        ))}
      </div>
      <div className="mt-1 text-center text-2xs text-faint">p10 · IQR · median · p90 (quantiles from API)</div>
    </div>
  )
}

function Histogram({ dist, highlightId, height }: { dist: KpiDistribution; highlightId?: string | null; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    // trim to the 2nd–98th percentile so a few extreme outliers don't flatten the histogram;
    // values beyond are counted in the edge bins
    const sorted = dist.values.map((v) => v.value).sort((x, y) => x - y)
    const n = sorted.length
    // heavy-tailed KPIs (EV/EBITDA, growth): clamp the axis to p10/p90 ± 60% of their spread
    const q = (p: number) => sorted[Math.min(n - 1, Math.max(0, Math.round(p * (n - 1))))]
    const p10 = Number.isFinite(dist.p10) ? dist.p10 : q(0.1)
    const p90 = Number.isFinite(dist.p90) ? dist.p90 : q(0.9)
    const spread = p90 - p10 || Math.abs(p90) || 1
    const lo = Math.max(sorted[0], p10 - 0.6 * spread)
    const hi = Math.min(sorted[n - 1], p90 + 0.6 * spread)
    const vals = sorted
    const nb = Math.min(18, Math.max(6, Math.round(Math.sqrt(vals.length) * 1.6)))
    const w = (hi - lo) / nb || 1
    const bins = Array.from({ length: nb }, (_, i) => ({ x0: lo + i * w, x1: lo + (i + 1) * w, n: 0, tickers: [] as string[] }))
    for (const v of dist.values) {
      const i = Math.min(nb - 1, Math.max(0, Math.floor((v.value - lo) / w)))
      bins[i].n++
      if (bins[i].tickers.length < 6) bins[i].tickers.push(v.ticker)
    }
    const hl = highlightId ? dist.values.find((v) => v.companyId === highlightId) : undefined
    const hlBin = hl ? Math.min(nb - 1, Math.max(0, Math.floor((hl.value - lo) / w))) : -1
    const idx = (x: number) => Math.min(nb - 0.5, Math.max(-0.5, (x - lo) / w - 0.5))
    const f = (x: number) => fmtValue(x, dist.kpi.unit)
    return {
      grid: { left: 4, right: 8, top: 8, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const i = (p as { dataIndex: number }).dataIndex
          const b = bins[i]
          return ttTitle(`${f(b.x0)} – ${f(b.x1)}`) + ttRow(t.ref, 'Companies', String(b.n)) + (b.tickers.length ? `<div style="color:${t.muted};margin-top:2px">${b.tickers.join(', ')}${b.n > b.tickers.length ? '…' : ''}</div>` : '')
        },
      },
      xAxis: {
        type: 'category',
        data: bins.map((b) => f((b.x0 + b.x1) / 2)),
        axisLabel: { interval: Math.ceil(nb / 4) - 1, fontSize: 9 },
        axisTick: { show: false },
      },
      yAxis: { type: 'value', minInterval: 1, splitNumber: 2, axisLabel: { fontSize: 9 } },
      series: [
        {
          type: 'bar',
          barCategoryGap: '8%',
          data: bins.map((b, i) => ({ value: b.n, itemStyle: { color: i === hlBin ? t.accent : alpha(t.ref, 0.55), borderRadius: [2, 2, 0, 0] } })),
          markArea: { silent: true, itemStyle: { color: alpha(t.ref, 0.1) }, data: [[{ xAxis: idx(dist.p25) }, { xAxis: idx(dist.p75) }]] },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            data: [
              { name: 'Median', xAxis: idx(dist.median), lineStyle: { color: t.muted, type: 'dashed', width: 1 } },
              ...(hl ? [{ name: hl.ticker, xAxis: idx(hl.value), lineStyle: { color: t.accent, width: 1.5, type: 'solid' as const } }] : []),
            ],
          },
        },
      ],
    }
  }, [dist, highlightId, t])
  const hl = highlightId ? dist.values.find((v) => v.companyId === highlightId) : undefined
  return (
    <div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 px-1.5 text-2xs text-muted">
        <span className="inline-flex items-center gap-1">
          <span className="h-0 w-3 border-t border-dashed border-muted" aria-hidden /> Median <span className="num text-fg">{fmtValue(dist.median, dist.kpi.unit)}</span>
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-3 rounded-sm bg-surface-3" aria-hidden /> IQR <span className="num">{fmtValue(dist.p25, dist.kpi.unit)}–{fmtValue(dist.p75, dist.kpi.unit)}</span>
        </span>
        {hl && (
          <span className="ml-auto inline-flex items-center gap-1 font-semibold text-accent-text">
            <span className="h-2.5 w-0.5 bg-accent" aria-hidden /> {hl.ticker} <span className="num">{fmtValue(hl.value, dist.kpi.unit)}</span>
          </span>
        )}
      </div>
      <EChart option={option} height={height} ariaLabel={`${dist.kpi.label} distribution`} />
    </div>
  )
}
