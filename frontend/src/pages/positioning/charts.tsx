import { useMemo } from 'react'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { type ChartTokens, alpha, useChartTokens } from '@/components/charts/tokens'

export interface PanelSeries {
  name: string
  data: (number | null)[]
  kind?: 'line' | 'bar' | 'signed-bar'
  /** index into the categorical palette (fixed order) or an explicit colour */
  color?: number | string
}

export interface StackPanel {
  title: string
  series: PanelSeries[]
  fmt?: (v: number) => string
  /** fixed y range (e.g. COT index 0..1) */
  min?: number
  max?: number
  /** shaded y bands, e.g. extreme zones */
  bands?: { from: number; to: number; tone: 'hi' | 'lo' }[]
}

const colorOf = (t: ChartTokens, c: number | string | undefined, i: number) => (typeof c === 'string' ? c : t.series[(c ?? i) % t.series.length])

/**
 * Vertically stacked panels sharing one x-axis (dates): one y-scale per panel, never a dual axis. Crosshair, tooltip
 * and zoom are linked across panels.
 */
export function StackedPanels({ dates, panels, panelHeight = 120, ariaLabel }: { dates: string[]; panels: StackPanel[]; panelHeight?: number; ariaLabel: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const top0 = 22
    const gap = 30
    const n = panels.length
    const grids = panels.map((_, i) => ({ left: 64, right: 16, top: top0 + i * (panelHeight + gap), height: panelHeight }))
    const xIdx = panels.map((_, i) => i)
    const fmts = panels.map((p) => p.fmt ?? ((v: number) => v.toLocaleString()))
    const series = panels.flatMap((p, i) =>
      p.series.map((s, j) => {
        const col = colorOf(t, s.color, j)
        const base = { name: s.name, xAxisIndex: i, yAxisIndex: i, data: s.data, emphasis: { disabled: true } }
        if (s.kind === 'signed-bar') {
          return {
            ...base,
            type: 'bar' as const,
            barCategoryGap: '20%',
            itemStyle: { color: (pp: { value: unknown }) => ((pp.value as number) >= 0 ? t.series[1] : t.series[0]) },
            large: true,
          }
        }
        if (s.kind === 'bar') return { ...base, type: 'bar' as const, itemStyle: { color: col }, large: true }
        return {
          ...base,
          type: 'line' as const,
          showSymbol: false,
          connectNulls: false,
          lineStyle: { width: 1.5, color: col },
          itemStyle: { color: col },
          ...(j === 0 && p.bands?.length
            ? {
                markArea: {
                  silent: true,
                  itemStyle: { opacity: 1 },
                  data: p.bands.map((b) => [
                    { yAxis: b.from, itemStyle: { color: alpha(b.tone === 'hi' ? t.series[1] : t.series[0], 0.12) } },
                    { yAxis: b.to },
                  ]),
                },
              }
            : {}),
        }
      }),
    )
    return {
      animation: false,
      title: panels.map((p, i) => ({ text: p.title, left: 64, top: top0 + i * (panelHeight + gap) - 18, textStyle: { fontSize: 11, fontWeight: 600, color: t.muted } })),
      grid: grids,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line' },
        formatter: (raw: unknown) => {
          const ps = (Array.isArray(raw) ? raw : [raw]) as { axisValue: string; seriesName: string; value: unknown; color: string; axisIndex?: number; seriesIndex: number }[]
          if (!ps.length) return ''
          let si = 0
          const owner: number[] = panels.flatMap((p, i) => p.series.map(() => i))
          const rows = ps
            .filter((p) => typeof p.value === 'number' && Number.isFinite(p.value))
            .map((p) => {
              si = owner[p.seriesIndex] ?? 0
              const c = typeof p.color === 'string' ? p.color : t.fg
              return ttRow(c, p.seriesName, fmts[si](p.value as number))
            })
          return ttTitle(ps[0].axisValue) + rows.join('')
        },
      },
      dataZoom: [{ type: 'inside', xAxisIndex: xIdx, filterMode: 'none' }],
      xAxis: panels.map((_, i) => ({
        type: 'category' as const,
        gridIndex: i,
        data: dates,
        boundaryGap: true,
        axisLabel: { show: i === n - 1, color: t.muted, formatter: (v: string) => v.slice(0, 7) },
        axisTick: { show: false },
        axisLine: { lineStyle: { color: t.border } },
      })),
      yAxis: panels.map((p, i) => ({
        type: 'value' as const,
        gridIndex: i,
        min: p.min,
        max: p.max,
        scale: p.min === undefined,
        splitNumber: 3,
        axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => fmts[i](v) },
        splitLine: { lineStyle: { color: t.grid } },
      })),
      series,
    } as EChartsOption
  }, [dates, panels, panelHeight, t])
  const height = 22 + panels.length * (panelHeight + 30) + 8
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}

/** Simple single-panel multi-line chart (<= 4 series, legend shown when >= 2). */
export function Lines({ dates, series, fmt, height = 200, ariaLabel, min, max }: { dates: string[]; series: PanelSeries[]; fmt?: (v: number) => string; height?: number; ariaLabel: string; min?: number; max?: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const f = fmt ?? ((v: number) => v.toLocaleString())
    return {
      animation: false,
      grid: { left: 56, right: 12, top: series.length > 1 ? 26 : 10, bottom: 22 },
      legend: series.length > 1 ? { top: 0, left: 50, textStyle: { color: t.muted, fontSize: 11 }, itemHeight: 3, itemWidth: 12 } : undefined,
      tooltip: { trigger: 'axis', valueFormatter: (v) => (typeof v === 'number' ? f(v) : '—') },
      xAxis: { type: 'category', data: dates, axisLabel: { color: t.muted, formatter: (v: string) => v.slice(0, 7) }, axisTick: { show: false } },
      yAxis: { type: 'value', min, max, scale: min === undefined, splitNumber: 3, axisLabel: { color: t.muted, fontSize: 10, formatter: (v: number) => f(v) }, splitLine: { lineStyle: { color: t.grid } } },
      dataZoom: [{ type: 'inside', filterMode: 'none' }],
      series: series.map((s, j) => {
        const col = colorOf(t, s.color, j)
        return { name: s.name, type: 'line', data: s.data, showSymbol: false, lineStyle: { width: 1.5, color: col }, itemStyle: { color: col } }
      }),
    } as EChartsOption
  }, [dates, series, fmt, t, min, max])
  return <EChart option={option} height={height} ariaLabel={ariaLabel} />
}
