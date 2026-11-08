import { useMemo } from 'react'
import { curvePoints, gaugePos } from '@/api/crossasset/adapters'
import { SNAPSHOT_LABEL, type XaCurveSnapshot, type XaRiskComponent, type XaSnapshotLabel } from '@/api/crossasset/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { getFormatLocale } from '@/lib/format'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const fmtPx = (v: number) => v.toLocaleString(getFormatLocale(), { maximumSignificantDigits: 6 })

/** Term structure now vs ~1m vs ~1y ago, x = months to expiry (so the shapes are comparable across dates). */
export function CurveChart({ snapshots, height = 280, ariaLabel }: { snapshots: XaCurveSnapshot[]; height?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const color: Record<string, string> = { latest: t.series[0], '1m': t.series[1], '1y': t.ref }
    const series = snapshots
      .filter((s) => s.points.length)
      .map((s) => {
        const pts = curvePoints(s)
        const c = color[s.label] ?? t.fg
        return {
          name: `${SNAPSHOT_LABEL[s.label as XaSnapshotLabel] ?? s.label} · ${s.date ?? ''}`,
          type: 'line' as const,
          data: pts.map((p) => ({ value: [p.x, p.y], contract: p.contract, expiry: p.expiry })),
          showSymbol: true,
          symbol: 'circle',
          symbolSize: s.label === 'latest' ? 6 : 5,
          lineStyle: { width: s.label === 'latest' ? 2 : 1.5, color: c, type: s.label === '1y' ? ('dashed' as const) : ('solid' as const) },
          itemStyle: { color: c },
          emphasis: { disabled: true },
        }
      })
    return {
      animation: false,
      grid: { left: 64, right: 16, top: 30, bottom: 34 },
      legend: { top: 0, left: 60, textStyle: { color: t.muted, fontSize: 11 }, itemHeight: 3, itemWidth: 14 },
      tooltip: {
        trigger: 'item',
        formatter: (raw: unknown) => {
          const p = raw as { seriesName: string; color: string; data: { value: [number, number]; contract: string; expiry: string } }
          return ttTitle(`${p.data.contract} · expiry ≈ ${p.data.expiry}`) + ttRow(p.color, p.seriesName, fmtPx(p.data.value[1])) + ttRow(t.faint, 'Months to expiry', p.data.value[0].toFixed(1))
        },
      },
      xAxis: { type: 'value', name: 'Months to expiry (approx.)', nameLocation: 'middle', nameGap: 22, min: 0, axisLabel: { color: t.muted, fontSize: 10 }, splitLine: { show: false } },
      yAxis: { type: 'value', scale: true, splitNumber: 4, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => fmtPx(v) }, splitLine: { lineStyle: { color: t.grid } } },
      series,
    } as EChartsOption
  }, [snapshots, t])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Correlation matrix heatmap: --c2 (blue) for positive, --c1 (orange) for negative, neutral surface at 0. */
export function CorrMatrix({ names, cells, height = 360, ariaLabel }: { names: string[]; cells: [number, number, number | null][]; height?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const n = names.length
    return {
      animation: false,
      grid: { left: 64, right: 12, top: 8, bottom: 60 },
      tooltip: {
        formatter: (raw: unknown) => {
          const p = raw as { value: [number, number, number | null] }
          const [x, y, v] = p.value
          return ttTitle(`${names[n - 1 - y]} × ${names[x]}`) + ttRow(t.fg, 'Correlation', fin(v) ? v.toFixed(2) : '—')
        },
      },
      xAxis: { type: 'category', data: names, axisLabel: { color: t.muted, fontSize: 10, rotate: 45, interval: 0 }, axisTick: { show: false }, splitArea: { show: false } },
      yAxis: { type: 'category', data: [...names].reverse(), axisLabel: { color: t.muted, fontSize: 10, interval: 0 }, axisTick: { show: false } },
      visualMap: { min: -1, max: 1, show: false, inRange: { color: [alpha(t.series[0], 0.85), t.surface2, alpha(t.series[1], 0.85)] } },
      series: [
        {
          type: 'heatmap',
          data: cells.map((c) => (fin(c[2]) ? c : [c[0], c[1], '-'])),
          label: { show: n <= 14, fontSize: 9, color: t.fg, formatter: (p: { value: [number, number, number] }) => (fin(p.value[2]) ? p.value[2].toFixed(2).replace(/^(-?)0\./, '$1.') : '') },
          itemStyle: { borderColor: t.surface, borderWidth: 1 },
          emphasis: { disabled: true },
        },
      ],
    } as EChartsOption
  }, [names, cells, t])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Semicircular risk gauge in SVG: −2 (risk-off, orange) … +2 (risk-on, blue); neutral band ±0.5. */
export function RiskGauge({ value, size = 200, label = 'Risk composite (model-based)' }: { value: number | null | undefined; size?: number; label?: string }) {
  const pos = gaugePos(value)
  const r = size / 2 - 12
  const cx = size / 2
  const cy = size / 2 + 4
  const pt = (f: number, rr = r) => {
    const a = Math.PI * (1 - f)
    return [cx + rr * Math.cos(a), cy - rr * Math.sin(a)]
  }
  const arc = (f0: number, f1: number) => {
    const [x0, y0] = pt(f0)
    const [x1, y1] = pt(f1)
    return `M${x0.toFixed(2)},${y0.toFixed(2)} A${r},${r} 0 0 1 ${x1.toFixed(2)},${y1.toFixed(2)}`
  }
  const lo = (2 - 0.5) / 4
  const hi = (2 + 0.5) / 4
  const [nx, ny] = pos === null ? [cx, cy] : pt(pos, r - 14)
  return (
    <div className="relative inline-flex flex-col items-center" style={{ width: size }} role="meter" aria-valuemin={-2} aria-valuemax={2} aria-valuenow={fin(value) ? Math.round(value * 100) / 100 : undefined} aria-label={label}>
      <svg width={size} height={size / 2 + 24} aria-hidden>
        <path d={arc(0, lo)} stroke="color-mix(in srgb, var(--c1) 70%, var(--surface))" strokeWidth={10} fill="none" />
        <path d={arc(lo, hi)} stroke="var(--surface-3)" strokeWidth={10} fill="none" />
        <path d={arc(hi, 1)} stroke="color-mix(in srgb, var(--c2) 70%, var(--surface))" strokeWidth={10} fill="none" />
        {pos !== null && (
          <>
            <line x1={cx} y1={cy} x2={nx} y2={ny} stroke="var(--fg)" strokeWidth={2.5} strokeLinecap="round" />
            <circle cx={cx} cy={cy} r={4.5} fill="var(--fg)" />
          </>
        )}
        <text x={pt(0)[0]} y={cy + 14} fontSize={10} fill="var(--muted)" textAnchor="start">
          risk-off
        </text>
        <text x={pt(1)[0]} y={cy + 14} fontSize={10} fill="var(--muted)" textAnchor="end">
          risk-on
        </text>
      </svg>
    </div>
  )
}

/** Horizontal bars of the risk components' z-scores (signed so that + = risk-on). */
export function ComponentBars({ components, height, ariaLabel }: { components: XaRiskComponent[]; height?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const rows = useMemo(() => [...components].reverse(), [components])
  const option = useMemo<EChartsOption>(
    () =>
      ({
        animation: false,
        grid: { left: 128, right: 36, top: 6, bottom: 20 },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: unknown) => (fin(v) ? `${v > 0 ? '+' : ''}${v.toFixed(2)}σ` : 'n/a') },
        xAxis: { type: 'value', min: -3, max: 3, splitNumber: 6, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => `${v > 0 ? '+' : ''}${v}` }, splitLine: { lineStyle: { color: t.grid } } },
        yAxis: { type: 'category', data: rows.map((c) => c.label), axisLabel: { color: t.muted, fontSize: 10 }, axisTick: { show: false } },
        series: [
          {
            type: 'bar',
            name: 'z (+ = risk-on)',
            data: rows.map((c) => (fin(c.z) ? { value: c.z, itemStyle: { color: c.z >= 0 ? t.series[1] : t.series[0] } } : { value: null })),
            barWidth: 10,
            label: { show: true, position: 'right', fontSize: 10, color: t.muted, formatter: (p: { value: unknown }) => (fin(p.value) ? (p.value as number).toFixed(1) : 'n/a') },
          },
        ],
      }) as EChartsOption,
    [rows, t],
  )
  return <EChart option={option} height={height ?? 22 * rows.length + 30} ariaLabel={ariaLabel} />
}
