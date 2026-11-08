// ESM build of the core wrapper (the /lib CJS build breaks default-import interop under Vite)
import ReactEChartsCore from 'echarts-for-react/esm/core'
import {
  BarChart,
  BoxplotChart,
  CustomChart,
  GaugeChart,
  GraphChart,
  HeatmapChart,
  LineChart,
  LinesChart,
  MapChart,
  RadarChart,
  ScatterChart,
  TreemapChart,
} from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
  GeoComponent,
} from 'echarts/components'
import * as echarts from 'echarts/core'
import { LegacyGridContainLabel } from 'echarts/features'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'
import { cn } from '@/lib/utils'
import { type ChartTokens, useChartTokens } from './tokens'

echarts.use([
  BarChart,
  BoxplotChart,
  CustomChart,
  GaugeChart,
  GraphChart,
  HeatmapChart,
  LineChart,
  LinesChart,
  MapChart,
  RadarChart,
  ScatterChart,
  TreemapChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  MarkLineComponent,
  MarkPointComponent,
  RadarComponent,
  TitleComponent,
  TooltipComponent,
  VisualMapComponent,
  GeoComponent,
  CanvasRenderer,
  // grid.containLabel (used throughout) moved to an opt-in feature in ECharts 6
  LegacyGridContainLabel,
])

export { echarts }
export type { EChartsOption }

/** ECharts theme derived from the CSS design tokens; re-registered whenever the theme toggles. */
function buildTheme(t: ChartTokens): object {
  const axis = {
    axisLine: { show: true, lineStyle: { color: t.borderStrong } },
    axisTick: { show: false, lineStyle: { color: t.borderStrong } },
    axisLabel: { color: t.muted, fontSize: 10, fontFamily: t.fontSans },
    splitLine: { show: true, lineStyle: { color: t.grid, width: 1 } },
    splitArea: { show: false },
    nameTextStyle: { color: t.muted, fontSize: 10 },
  }
  return {
    color: t.series,
    backgroundColor: 'transparent',
    textStyle: { fontFamily: t.fontSans, color: t.muted, fontSize: 11 },
    title: { textStyle: { color: t.fg, fontSize: 12, fontWeight: 600 }, subtextStyle: { color: t.muted } },
    line: { symbol: 'none', smooth: false, lineStyle: { width: 1.5 } },
    bar: { itemStyle: { borderRadius: 0 } },
    categoryAxis: { ...axis, splitLine: { show: false } },
    valueAxis: { ...axis, axisLine: { show: false } },
    timeAxis: { ...axis, splitLine: { show: false } },
    logAxis: axis,
    legend: { textStyle: { color: t.muted, fontSize: 11 }, itemWidth: 12, itemHeight: 3, icon: 'roundRect', inactiveColor: t.faint },
    tooltip: {
      backgroundColor: t.tooltipBg,
      borderColor: t.borderStrong,
      borderWidth: 1,
      padding: [6, 8],
      textStyle: { color: t.fg, fontSize: 11, fontFamily: t.fontSans },
      axisPointer: { lineStyle: { color: t.borderStrong }, crossStyle: { color: t.borderStrong }, label: { backgroundColor: t.surface3, color: t.fg } },
      extraCssText: 'box-shadow:none;border-radius:4px;',
    },
    radar: { axisName: { color: t.muted }, splitLine: { lineStyle: { color: t.grid } }, splitArea: { show: false }, axisLine: { lineStyle: { color: t.grid } } },
    visualMap: { textStyle: { color: t.muted } },
    dataZoom: {
      backgroundColor: 'transparent',
      borderColor: t.border,
      fillerColor: t.accentSoft,
      handleStyle: { color: t.surface3, borderColor: t.borderStrong },
      textStyle: { color: t.muted },
      dataBackground: { lineStyle: { color: t.faint }, areaStyle: { color: t.surface3 } },
    },
    gauge: { title: { color: t.muted }, detail: { color: t.fg } },
    markLine: { lineStyle: { color: t.faint } },
  }
}

/** Token objects are memoised per theme, so registration is idempotent per object. */
const registered = new WeakSet<ChartTokens>()
function registerTheme(name: string, tokens: ChartTokens): void {
  if (registered.has(tokens)) return
  echarts.registerTheme(name, buildTheme(tokens))
  registered.add(tokens)
}

export function useEChartsTheme(): { name: string; tokens: ChartTokens } {
  const tokens = useChartTokens()
  const name = `big-${tokens.theme}`
  registerTheme(name, tokens)
  return { name, tokens }
}

export interface EChartProps {
  option: EChartsOption
  height?: number | string
  className?: string
  ariaLabel: string
  onEvents?: Record<string, (params: unknown) => void>
  notMerge?: boolean
}

/** Themed ECharts wrapper (tree-shaken core build, canvas renderer, auto-resize). */
export function EChart({ option, height = 260, className, ariaLabel, onEvents, notMerge = true }: EChartProps) {
  const { name } = useEChartsTheme()
  return (
    <div role="img" aria-label={ariaLabel} className={cn('w-full min-w-0', className)} style={{ height }}>
      <ReactEChartsCore
        key={name}
        echarts={echarts}
        option={option}
        theme={name}
        notMerge={notMerge}
        lazyUpdate
        style={{ height: '100%', width: '100%' }}
        opts={{ renderer: 'canvas' }}
        onEvents={onEvents}
      />
    </div>
  )
}

/** Shared tooltip row helper (HTML string for ECharts formatters). */
export function ttRow(color: string, label: string, value: string): string {
  return `<div style="display:flex;align-items:center;gap:6px;justify-content:space-between;min-width:140px"><span style="display:inline-flex;align-items:center;gap:6px"><span style="width:8px;height:3px;border-radius:2px;background:${color}"></span>${label}</span><span style="font-family:'IBM Plex Mono',monospace">${value}</span></div>`
}

export function ttTitle(s: string): string {
  return `<div style="font-weight:600;margin-bottom:4px">${s}</div>`
}
