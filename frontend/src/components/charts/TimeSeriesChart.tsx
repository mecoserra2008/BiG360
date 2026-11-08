import { useMemo } from 'react'
import { EChart, type EChartsOption, ttRow, ttTitle } from './EChart'
import { alpha, useChartTokens } from './tokens'

export interface TsSeries {
  name: string
  data: { date: string; value: number | null }[]
  /** CSS colour; defaults to the categorical palette in fixed order */
  color?: string
  dashed?: boolean
  area?: boolean
  width?: number
  step?: boolean
}

/**
 * Generic single-axis time-series line chart (one unit per chart — never dual-axis).
 * Uses a time x-axis so series with different sampling align.
 */
export function TimeSeriesChart({
  series,
  height = 220,
  format,
  axisFormat,
  threshold,
  zeroLine = false,
  ariaLabel,
  legend = series.length > 1,
  yMin,
  yMax,
}: {
  series: TsSeries[]
  height?: number
  format: (v: number) => string
  axisFormat?: (v: number) => string
  threshold?: { value: number; label: string }
  zeroLine?: boolean
  ariaLabel: string
  legend?: boolean
  yMin?: number
  yMax?: number
}) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const marks: { yAxis: number; name: string; lineStyle?: object }[] = []
    if (threshold) marks.push({ yAxis: threshold.value, name: threshold.label, lineStyle: { color: t.warn, type: 'dashed' } })
    if (zeroLine) marks.push({ yAxis: 0, name: '', lineStyle: { color: t.borderStrong, type: 'solid' } })
    // sparse annual/multi-year series: label the x-axis by year with ≥1y tick spacing
    const all = series.flatMap((s) => s.data.map((p) => Date.parse(p.date)))
    const spanYears = all.length ? (Math.max(...all) - Math.min(...all)) / 3.15e10 : 0
    const n = Math.max(...series.map((s) => s.data.length), 0)
    const yearly = spanYears >= 3 && n / spanYears < 6
    return {
      grid: { left: 8, right: 14, top: legend ? 26 : 12, bottom: 8, containLabel: true },
      legend: legend ? { top: 0, right: 0 } : undefined,
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { axisValueLabel: string; seriesName: string; value: [string, number | null]; color: string }[]
          return ttTitle(arr[0]?.axisValueLabel ?? '') + arr.filter((p) => p.value[1] !== null).map((p) => ttRow(p.color, p.seriesName, format(p.value[1] as number))).join('')
        },
      },
      xAxis: yearly
        ? { type: 'time', minInterval: 3.15e10, axisLabel: { hideOverlap: true, formatter: (v: number) => (new Date(v).getUTCMonth() === 0 ? String(new Date(v).getUTCFullYear()) : '') } }
        : { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: yMin === undefined, min: yMin, max: yMax, axisLabel: { formatter: (v: number) => (axisFormat ?? format)(v) } },
      series: series.map((s, i) => {
        const color = s.color ?? t.series[i % t.series.length]
        return {
          name: s.name,
          type: 'line' as const,
          step: s.step ? ('end' as const) : undefined,
          data: s.data.map((p) => [p.date, p.value]),
          showSymbol: false,
          lineStyle: { color, width: s.width ?? (i === 0 ? 2 : 1.5), type: s.dashed ? ('dashed' as const) : ('solid' as const) },
          itemStyle: { color },
          areaStyle: s.area ? { color: alpha(color, 0.12) } : undefined,
          markLine: i === 0 && marks.length ? { silent: true, symbol: 'none', label: { color: t.muted, fontSize: 9, formatter: '{b}', position: 'insideEndTop' }, data: marks } : undefined,
        }
      }),
    }
  }, [series, t, format, axisFormat, threshold, zeroLine, legend, yMin, yMax])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}
