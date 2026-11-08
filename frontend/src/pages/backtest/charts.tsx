import { useMemo } from 'react'
import { fin, histogram } from '@/api/backtest/adapters'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'

/** Vertical bars over categories (decile returns, factor contributions). Positive = --up, negative = --down unless `color`. */
export function BarChart({
  labels,
  values,
  format,
  height = 220,
  ariaLabel,
  color,
  horizontal = false,
}: {
  labels: string[]
  values: (number | null)[]
  format: (v: number) => string
  height?: number
  ariaLabel: string
  color?: string
  horizontal?: boolean
}) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const cat = { type: 'category' as const, data: labels, axisLabel: { color: t.muted, fontSize: 10, interval: 0, hideOverlap: true }, axisTick: { show: false } }
    const val = { type: 'value' as const, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => format(v) }, splitLine: { lineStyle: { color: t.grid } } }
    return {
      animation: false,
      grid: { left: horizontal ? 8 : 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (raw: unknown) => {
          const p = (raw as { name: string; value: number | null; color: string }[])[0]
          return p ? ttTitle(p.name) + ttRow(p.color, 'Value', fin(p.value) ? format(p.value) : '–') : ''
        },
      },
      xAxis: horizontal ? val : cat,
      yAxis: horizontal ? { ...cat, inverse: true } : val,
      series: [
        {
          type: 'bar',
          barMaxWidth: 28,
          data: values.map((v) => ({ value: v, itemStyle: { color: color ?? (fin(v) && v < 0 ? t.down : t.up), borderRadius: 2 } })),
        },
      ],
    } as EChartsOption
  }, [labels, values, format, t, color, horizontal])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Histogram of a distribution with an optional marker (e.g. permutation Sharpes vs the actual Sharpe). */
export function Histogram({
  values,
  marker,
  markerLabel,
  format,
  bins = 20,
  height = 200,
  ariaLabel,
  zeroLine = false,
}: {
  values: number[]
  marker?: number | null
  markerLabel?: string
  format: (v: number) => string
  bins?: number
  height?: number
  ariaLabel: string
  zeroLine?: boolean
}) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const vals = fin(marker) ? [...values, marker] : values
    const h = histogram(vals, bins).map((b) => ({ ...b, n: 0 }))
    const lo = h.length ? h[0].x0 : 0
    const w = h.length ? h[0].x1 - h[0].x0 : 1
    for (const v of values.filter(fin)) h[Math.min(h.length - 1, Math.max(0, Math.floor((v - lo) / w)))].n += 1
    const lines: { xAxis: number; name: string; lineStyle: object }[] = []
    if (fin(marker)) lines.push({ xAxis: marker, name: markerLabel ?? 'actual', lineStyle: { color: t.accent, width: 2, type: 'solid' } })
    if (zeroLine) lines.push({ xAxis: 0, name: '0', lineStyle: { color: t.borderStrong, type: 'dashed' } })
    return {
      animation: false,
      grid: { left: 8, right: 16, top: 18, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (raw: unknown) => {
          const p = raw as { data: { value: [number, number]; x0: number; x1: number } }
          return ttTitle(`${format(p.data.x0)} … ${format(p.data.x1)}`) + ttRow(t.ref, 'Count', String(p.data.value[1]))
        },
      },
      xAxis: { type: 'value', scale: true, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => format(v) }, splitLine: { show: false } },
      yAxis: { type: 'value', minInterval: 1, axisLabel: { color: t.muted, fontSize: 10 }, splitLine: { lineStyle: { color: t.grid } } },
      series: [
        {
          type: 'bar',
          barWidth: '92%',
          data: h.map((b) => ({ value: [(b.x0 + b.x1) / 2, b.n], x0: b.x0, x1: b.x1, itemStyle: { color: alpha(t.ref, 0.75), borderRadius: 1 } })),
          markLine: lines.length ? { silent: true, symbol: 'none', label: { color: t.muted, fontSize: 9, formatter: '{b}' }, data: lines } : undefined,
        },
      ],
    } as EChartsOption
  }, [values, marker, markerLabel, format, bins, t, zeroLine])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Mean path with a 95% band over event days (x = trading days since entry). */
export function PathChart({ days, mean, lo, hi, excess, format, height = 240, ariaLabel }: { days: number[]; mean: number[]; lo: number[]; hi: number[]; excess?: number[]; format: (v: number) => string; height?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const c = t.series[0]
    const series: object[] = [
      { name: 'band-lo', type: 'line', data: lo, stack: 'band', lineStyle: { opacity: 0 }, symbol: 'none', silent: true, tooltip: { show: false } },
      { name: '95% band', type: 'line', data: hi.map((h, i) => h - lo[i]), stack: 'band', lineStyle: { opacity: 0 }, symbol: 'none', areaStyle: { color: alpha(c, 0.14) }, silent: true },
      { name: 'Mean (direction-adjusted)', type: 'line', data: mean, symbol: 'none', lineStyle: { color: c, width: 2 }, itemStyle: { color: c } },
    ]
    if (excess?.length) series.push({ name: 'Mean minus benchmark', type: 'line', data: excess, symbol: 'none', lineStyle: { color: t.series[1], width: 1.5, type: 'dashed' }, itemStyle: { color: t.series[1] } })
    return {
      animation: false,
      grid: { left: 8, right: 16, top: 26, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0, data: ['Mean (direction-adjusted)', ...(excess?.length ? ['Mean minus benchmark'] : [])], textStyle: { color: t.muted, fontSize: 11 } },
      tooltip: {
        trigger: 'axis',
        formatter: (raw: unknown) => {
          const arr = raw as { seriesName: string; value: number; color: string; dataIndex: number }[]
          const i = arr[0]?.dataIndex ?? 0
          return ttTitle(`Day ${days[i]}`) + ttRow(c, 'Mean', format(mean[i])) + ttRow(t.ref, '95% band', `${format(lo[i])} … ${format(hi[i])}`) + (excess?.length ? ttRow(t.series[1], 'vs benchmark', format(excess[i])) : '')
        },
      },
      xAxis: { type: 'category', data: days.map(String), name: 'trading days after entry', nameLocation: 'middle', nameGap: 22, axisLabel: { color: t.muted, fontSize: 10 } },
      yAxis: { type: 'value', axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => format(v) }, splitLine: { lineStyle: { color: t.grid } } },
      series,
    } as EChartsOption
  }, [days, mean, lo, hi, excess, format, t])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Multi-line chart over a category x-axis (cumulative quantile returns per rebalance). */
export function LinesChart({ x, series, format, height = 240, ariaLabel }: { x: string[]; series: { name: string; data: (number | null)[]; color?: string; dashed?: boolean }[]; format: (v: number) => string; height?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    return {
      animation: false,
      grid: { left: 8, right: 16, top: 26, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0, type: 'scroll', textStyle: { color: t.muted, fontSize: 11 } },
      tooltip: {
        trigger: 'axis',
        formatter: (raw: unknown) => {
          const arr = raw as { seriesName: string; value: number | null; color: string; axisValueLabel: string }[]
          return ttTitle(arr[0]?.axisValueLabel ?? '') + arr.filter((p) => fin(p.value)).map((p) => ttRow(p.color, p.seriesName, format(p.value as number))).join('')
        },
      },
      xAxis: { type: 'category', data: x, axisLabel: { color: t.muted, fontSize: 10, hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => format(v) }, splitLine: { lineStyle: { color: t.grid } } },
      series: series.map((s, i) => {
        const c = s.color ?? t.series[i % t.series.length]
        return { name: s.name, type: 'line', data: s.data, symbol: 'none', lineStyle: { color: c, width: i === 0 ? 2 : 1.5, type: s.dashed ? 'dashed' : 'solid' }, itemStyle: { color: c } }
      }),
    } as EChartsOption
  }, [x, series, format, t])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}
