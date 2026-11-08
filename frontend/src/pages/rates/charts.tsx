import { useMemo } from 'react'
import { fmtRate, tenorLabel } from '@/api/rates/adapters'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'

export interface CurveLine {
  name: string
  points: [number, number][]
  color?: string
  dashed?: boolean
  width?: number
  /** faint reference (e.g. the latest curve behind an animation frame) */
  ghost?: boolean
}

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Yield curves: x = maturity in years (square-root scale so the short end is readable), y = percent. */
export function YieldCurveChart({ lines, height = 300, ariaLabel, yExtent }: { lines: CurveLine[]; height?: number; ariaLabel: string; yExtent?: [number, number] | null }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const maxX = Math.max(1, ...lines.flatMap((l) => l.points.map((p) => p[0])))
    const ticks = [0.25, 1, 2, 5, 10, 20, 30, 40].filter((x) => x <= maxX + 1e-9)
    const sq = (x: number) => Math.sqrt(x)
    return {
      animation: false,
      grid: { left: 52, right: 16, top: lines.length > 1 ? 30 : 12, bottom: 34 },
      legend: lines.length > 1 ? { top: 0, left: 48, textStyle: { color: t.muted, fontSize: 11 }, itemHeight: 3, itemWidth: 14 } : undefined,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line' },
        formatter: (raw: unknown) => {
          const arr = raw as { seriesName: string; color: string; value: [number, number, number] }[]
          if (!arr.length) return ''
          const yrs = arr[0].value[2]
          return ttTitle(`${tenorLabel(Math.round(yrs * 12))} maturity`) + arr.map((p) => ttRow(p.color, p.seriesName, fmtRate(p.value[1]))).join('')
        },
      },
      xAxis: {
        type: 'value',
        min: 0,
        max: sq(maxX),
        name: 'Maturity (years, square-root scale)',
        nameLocation: 'middle',
        nameGap: 22,
        interval: undefined,
        axisLabel: { color: t.muted, fontSize: 10, customValues: ticks.map(sq), formatter: (v: number) => `${+(v * v).toFixed(2)}y` },
        axisTick: { show: true, customValues: ticks.map(sq) },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        scale: !yExtent,
        min: yExtent?.[0],
        max: yExtent?.[1],
        axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => `${v.toFixed(1)}%` },
        splitLine: { lineStyle: { color: t.grid } },
      },
      series: lines.map((l, i) => {
        const c = l.color ?? (l.ghost ? t.ref : t.series[i % t.series.length])
        return {
          name: l.name,
          type: 'line' as const,
          data: l.points.filter((p) => fin(p[1])).map((p) => [sq(p[0]), p[1], p[0]]),
          showSymbol: !l.ghost && l.points.length <= 25,
          symbol: 'circle',
          symbolSize: 4,
          smooth: false,
          lineStyle: { width: l.width ?? (l.ghost ? 1 : i === 0 ? 2.2 : 1.6), color: c, type: l.dashed || l.ghost ? ('dashed' as const) : ('solid' as const), opacity: l.ghost ? 0.6 : 1 },
          itemStyle: { color: c },
          emphasis: { disabled: true },
        }
      }),
    } as EChartsOption
  }, [lines, t, yExtent])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

export interface BarSeries {
  name: string
  data: (number | null)[]
  color?: string
}

/** Vertical bars over categories (one unit per chart; stacked optional). */
export function Bars({ categories, series, height = 220, ariaLabel, format, stacked = false, rotate = 0 }: {
  categories: string[]
  series: BarSeries[]
  height?: number
  ariaLabel: string
  format: (v: number) => string
  stacked?: boolean
  rotate?: number
}) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(
    () => ({
      animation: false,
      grid: { left: 8, right: 12, top: series.length > 1 ? 26 : 10, bottom: 8, containLabel: true },
      legend: series.length > 1 ? { top: 0, right: 0 } : undefined,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (raw: unknown) => {
          const arr = raw as { axisValueLabel: string; seriesName: string; color: string; value: number | null }[]
          return ttTitle(arr[0]?.axisValueLabel ?? '') + arr.filter((p) => fin(p.value)).map((p) => ttRow(p.color, p.seriesName, format(p.value as number))).join('')
        },
      },
      xAxis: { type: 'category', data: categories, axisLabel: { color: t.muted, fontSize: 10, rotate, hideOverlap: true }, axisTick: { show: false } },
      yAxis: { type: 'value', axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => format(v) }, splitLine: { lineStyle: { color: t.grid } } },
      series: series.map((s, i) => ({
        name: s.name,
        type: 'bar' as const,
        stack: stacked ? 'total' : undefined,
        data: s.data,
        barMaxWidth: 28,
        itemStyle: { color: s.color ?? t.series[i % t.series.length] },
        emphasis: { disabled: true },
      })),
    }) as EChartsOption,
    [categories, series, t, format, stacked, rotate],
  )
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}
