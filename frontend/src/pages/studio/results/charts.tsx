import type { CustomSeriesRenderItemAPI, CustomSeriesRenderItemParams, CustomSeriesRenderItemReturn } from 'echarts'
import { useMemo, useState } from 'react'
import { clusterCount, cutLinkage, heightForK, linkClusters } from '@/api/studio/dendrogram'
import type { StudioCoefficient, StudioEvaluation, StudioForecast, StudioPlot, StudioSeries } from '@/api/studio/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { type ChartTokens, alpha, divergingColors, useChartTokens } from '@/components/charts/tokens'
import { Button } from '@/components/ui/button'
import { Segmented, Slider, Switch } from '@/components/ui/inputs'
import { fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtCell, fmtP, fmtStat, isDateLike } from '../fmt'

/* ───────────── shared helpers ───────────── */

const SYMBOLS = ['circle', 'rect', 'triangle', 'diamond'] as const
/** Cluster identity: 4 palette hues × 4 marker shapes (shape is the redundant, colour-blind-safe channel). */
function clusterStyle(t: ChartTokens, c: number): { color: string; symbol: string } {
  if (c < 0) return { color: t.ref, symbol: 'circle' }
  return { color: t.series[c % t.series.length], symbol: SYMBOLS[Math.floor(c / t.series.length) % SYMBOLS.length] }
}

const REF_NAMES = /^(45-degree|chance|perfect|ols|naive|zero)$/i
const isNumArr = (xs: unknown[]): boolean => xs.length > 0 && xs.every((x) => typeof x === 'number')
const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const axisFmt = (v: number): string => fmtStat(v, 3)
const tsLabel = (v: unknown): string => (typeof v === 'string' && isDateLike(v) ? fmtDate(v.slice(0, 10)) : fmtCell(v))

function grid(extra: Record<string, unknown> = {}) {
  return { left: 8, right: 16, top: 28, bottom: 8, containLabel: true, ...extra }
}

const xName = (name: string | null | undefined) => (name ? { name, nameLocation: 'middle' as const, nameGap: 22, axisLine: { onZero: false } } : { axisLine: { onZero: false } })
/** y-axis title sits above the axis (horizontal) so it never collides with tick labels */
const yName = (name: string | null | undefined) => (name ? { name, nameLocation: 'end' as const, nameGap: 8, nameTextStyle: { align: 'left' as const }, axisLine: { onZero: false } } : { axisLine: { onZero: false } })

/** Make room for axis titles (containLabel only reserves tick labels) and keep legends clear of the y title. */
function named(o: EChartsOption): EChartsOption {
  const g = o.grid as Record<string, unknown> | undefined
  const x = o.xAxis as { name?: string } | undefined
  const y = o.yAxis as { name?: string } | undefined
  if (!g || Array.isArray(o.grid) || Array.isArray(o.xAxis) || Array.isArray(o.yAxis)) return o
  const grid = { ...g }
  if (x?.name) grid.bottom = Math.max(Number(grid.bottom ?? 0), 22)
  if (y?.name) grid.top = Math.max(Number(grid.top ?? 0), o.legend ? 46 : 26)
  const legend = o.legend
  return { ...o, grid, legend } as EChartsOption
}

function seriesColor(t: ChartTokens, s: StudioSeries, i: number): { color: string; ref: boolean } {
  if (REF_NAMES.test(s.name)) return { color: t.faint, ref: true }
  if (/^actual/i.test(s.name)) return { color: t.ref, ref: false }
  return { color: t.series[i % t.series.length], ref: false }
}

/* ───────────── generic plot dispatcher ───────────── */

export function PlotView({ plot, height = 260, plotKey }: { plot: StudioPlot; height?: number; plotKey?: string }) {
  switch (plot.kind) {
    case 'line':
      return <LinePlot plot={plot} height={height} />
    case 'scatter':
      return <ScatterPlot plot={plot} height={height} />
    case 'bar':
      return plot.confidence_band?.length ? <AcfPlot plot={plot} height={height} /> : <BarPlot plot={plot} height={height} scree={plotKey === 'scree'} />
    case 'errorbar':
      return <ErrorbarPlot plot={plot} height={height} />
    case 'heatmap':
      return <HeatmapPlot plot={plot} height={height} />
    case 'scatter_points':
      return <PointsPlot plot={plot} height={height} />
    case 'arrows':
      return <ArrowsPlot plot={plot} height={height} />
    case 'dendrogram':
      return <DendrogramPlot plot={plot} linkage={null} height={height} />
    case 'ts_band':
      return <TsBandPlot plot={plot} height={height} />
    case 'event_path':
      return <EventPathPlot plot={plot} height={height} />
    case 'irf':
      return <IrfGrid plot={plot} />
    case 'fevd':
      return <FevdGrid plot={plot} />
    case 'regime_prob':
      return <RegimePlot plot={plot} height={height} />
    case 'coef_path':
      return <CoefPathPlot plot={plot} height={height} />
    case 'shap_beeswarm':
      return <ShapBeeswarm plot={plot} />
    default:
      return <div className="py-6 text-center text-xs text-muted">Unsupported plot kind “{String(plot.kind)}”.</div>
  }
}

/* ───────────── line ───────────── */

/** More than four identities: one small chart per series instead of an unreadable palette. */
function SmallMultiples({ plot }: { plot: StudioPlot }) {
  return (
    <div className="grid gap-2 @xl:grid-cols-2 @3xl:grid-cols-3">
      {(plot.series ?? []).map((s) => (
        <div key={s.name} className="rounded-sm border border-border/60 p-1">
          <div className="px-1 text-xs font-semibold text-fg">{s.name}</div>
          <LinePlot plot={{ ...plot, title: `${plot.title}: ${s.name}`, series: [s], y_label: null }} height={150} single />
        </div>
      ))}
    </div>
  )
}

function LinePlot({ plot, height, single = false }: { plot: StudioPlot; height: number; single?: boolean }) {
  const t = useChartTokens()
  const many = !single && (plot.series ?? []).filter((x) => !REF_NAMES.test(x.name) && !/^actual/i.test(x.name)).length > 4
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const allX = series.flatMap((s) => s.x)
    const time = allX.length > 0 && allX.every(isDateLike)
    const numeric = !time && isNumArr(allX)
    let pal = 0
    return named({
      grid: grid({ top: series.length > 1 ? 28 : 16 }),
      legend: series.length > 1 ? { top: 0, left: 0 } : undefined,
      tooltip: {
        trigger: 'axis',
        valueFormatter: (v) => fmtStat(v as number),
      },
      xAxis: time
        ? { type: 'time', axisLabel: { hideOverlap: true } }
        : numeric
          ? { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...xName(plot.x_label) }
          : { type: 'category', data: series[0]?.x.map(String) ?? [], ...xName(plot.x_label) },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: series.map((s) => {
        const c = seriesColor(t, s, REF_NAMES.test(s.name) || /^actual/i.test(s.name) ? 0 : pal++)
        return {
          type: 'line',
          name: s.name,
          data: time || numeric ? s.x.map((x, i) => [x, fin(s.y[i])]) : s.y.map(fin),
          showSymbol: s.x.length <= 12,
          symbolSize: 5,
          color: c.color,
          lineStyle: { width: c.ref ? 1 : /^actual/i.test(s.name) ? 1.25 : 1.5, type: c.ref ? 'dashed' : 'solid', color: c.color },
          connectNulls: false,
          z: /^actual/i.test(s.name) ? 1 : 2,
        }
      }),
    } as EChartsOption)
  }, [plot, t])
  if (many) return <SmallMultiples plot={plot} />
  return <EChart option={option} height={height} ariaLabel={plot.title} />
}

/* ───────────── scatter (residuals vs fitted, QQ, calibration) ───────────── */

function ScatterPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const residual = /resid/i.test(plot.y_label ?? '')
    let pal = 0
    return named({
      grid: grid({ top: series.length > 1 ? 28 : 16, bottom: 4 }),
      legend: series.length > 1 ? { top: 0, left: 0 } : undefined,
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { seriesName: string; data: [number, number, unknown?]; color: string }
          const lbl = q.data[2] !== undefined && q.data[2] !== null ? ttTitle(tsLabel(q.data[2])) : ''
          return `${lbl}${ttRow(q.color, plot.x_label ?? 'x', fmtStat(q.data[0]))}${ttRow(q.color, plot.y_label ?? 'y', fmtStat(q.data[1]))}`
        },
      },
      xAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...xName(plot.x_label) },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: series.map((s) => {
        const ref = REF_NAMES.test(s.name) || (s.x.length === 2 && series.length > 1)
        const c = ref ? { color: t.faint } : { color: t.series[pal++ % t.series.length] }
        const data = s.x.map((x, i) => [x, fin(s.y[i]), s.labels?.[i] ?? undefined]).filter((d) => fin(d[0]) !== null && d[1] !== null)
        if (ref)
          return { type: 'line', name: s.name, data, showSymbol: false, lineStyle: { type: 'dashed', width: 1, color: c.color }, color: c.color, silent: true, z: 1 }
        return {
          type: 'scatter',
          name: s.name,
          data,
          symbolSize: s.x.length > 1000 ? 3 : s.x.length > 200 ? 4 : 6,
          itemStyle: { color: alpha(c.color, s.x.length > 300 ? 0.55 : 0.85) },
          color: c.color,
          large: s.x.length > 3000,
          markLine: residual
            ? { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] }
            : undefined,
          z: 2,
        }
      }),
    } as EChartsOption)
  }, [plot, t])
  return <EChart option={option} height={height} ariaLabel={plot.title} />
}

/* ───────────── bars (histogram, scree) ───────────── */

function BarPlot({ plot, height, scree }: { plot: StudioPlot; height: number; scree: boolean }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const cats = (series[0]?.x ?? []).map((x) => (typeof x === 'number' ? fmtStat(x, 3) : String(x)))
    return named({
      grid: grid({ top: series.length > 1 ? 28 : 16 }),
      legend: series.length > 1 ? { top: 0, left: 0 } : undefined,
      tooltip: { trigger: 'axis', valueFormatter: (v) => (scree ? `${((v as number) * 100).toFixed(1)}%` : fmtStat(v as number)) },
      xAxis: { type: 'category', data: cats, axisLabel: { hideOverlap: true }, ...xName(plot.x_label) },
      yAxis: {
        type: 'value',
        max: scree ? 1 : undefined,
        axisLabel: { formatter: scree ? (v: number) => `${Math.round(v * 100)}%` : axisFmt },
        ...yName(plot.y_label),
      },
      series: series.map((s, i) =>
        scree && /cumul/i.test(s.name)
          ? { type: 'line', name: s.name, data: s.y.map(fin), symbol: 'circle', symbolSize: 6, color: t.ref, lineStyle: { color: t.ref, width: 1.5 } }
          : { type: 'bar', name: s.name, data: s.y.map(fin), color: t.series[i % t.series.length], barMaxWidth: 36, barCategoryGap: scree ? '35%' : '4%' },
      ),
    } as EChartsOption)
  }, [plot, t, scree])
  return <EChart option={option} height={height} ariaLabel={plot.title} />
}

/* ───────────── ACF / PACF small multiples with the ±band ───────────── */

export function AcfPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const band = plot.confidence_band ?? []
    const lo = fin(band[0]) ?? 0
    const hi = fin(band[1]) ?? Math.abs(lo)
    const n = Math.max(1, series.length)
    const gap = 6
    const top0 = 18
    const h = (100 - top0 / 3 - gap * (n - 1)) / n
    return named({
      tooltip: { trigger: 'axis', valueFormatter: (v) => fmtStat(v as number, 3) },
      title: series.map((s, i) => ({ text: s.name, left: 40, top: `${(i * (h + gap)).toFixed(1)}%`, textStyle: { fontSize: 11, color: t.muted, fontWeight: 600 } })),
      grid: series.map((_, i) => ({ left: 36, right: 12, top: `${(i * (h + gap) + 7).toFixed(1)}%`, height: `${(h - 9).toFixed(1)}%` })),
      xAxis: series.map((s, i) => ({ type: 'category', gridIndex: i, data: s.x.map(String), axisLabel: { show: i === n - 1, interval: 'auto', hideOverlap: true }, axisTick: { show: false } })),
      yAxis: series.map((_, i) => ({ type: 'value', gridIndex: i, min: -1, max: 1, splitNumber: 2, axisLabel: { formatter: (v: number) => fmtStat(v, 2) } })),
      series: series.map((s, i) => ({
        type: 'bar',
        name: s.name,
        xAxisIndex: i,
        yAxisIndex: i,
        barMaxWidth: 6,
        data: s.y.map((v) => {
          const y = fin(v)
          return { value: y, itemStyle: { color: y !== null && (y > hi || y < lo) ? t.series[0] : t.ref } }
        }),
        markArea: { silent: true, itemStyle: { color: alpha(t.series[1], 0.12) }, data: [[{ yAxis: lo }, { yAxis: hi }]] },
      })),
    } as EChartsOption)
  }, [plot, t])
  return (
    <div>
      <EChart option={option} height={height} ariaLabel={`${plot.title}: bars outside the shaded band are significant at 5%`} />
      <p className="mt-1 text-2xs text-muted">Shaded: approx. 95% band (±{fmtStat(Math.abs(fin(plot.confidence_band?.[1]) ?? 0), 2)}). Accent bars fall outside it.</p>
    </div>
  )
}

/* ───────────── error bars (coef CI, lag profiles, quantile process) ───────────── */

type RenderParams = CustomSeriesRenderItemParams
type RenderApi = CustomSeriesRenderItemAPI

function whisker(color: string, horizontal: boolean) {
  return (_p: RenderParams, api: RenderApi): CustomSeriesRenderItemReturn => {
    const a = Number(api.value(0))
    const lo = Number(api.value(1))
    const hi = Number(api.value(2))
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { type: 'group', children: [] }
    const p1 = horizontal ? api.coord([lo, a]) : api.coord([a, lo])
    const p2 = horizontal ? api.coord([hi, a]) : api.coord([a, hi])
    const w = 4
    const style = { stroke: color, lineWidth: 1.5 }
    const cap = (p: number[]) =>
      horizontal
        ? { type: 'line' as const, shape: { x1: p[0], y1: p[1] - w, x2: p[0], y2: p[1] + w }, style }
        : { type: 'line' as const, shape: { x1: p[0] - w, y1: p[1], x2: p[0] + w, y2: p[1] }, style }
    return {
      type: 'group',
      children: [{ type: 'line', shape: { x1: p1[0], y1: p1[1], x2: p2[0], y2: p2[1] }, style }, cap(p1), cap(p2)],
    }
  }
}

function ErrorbarPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  // the significance legend only makes sense when intervals can straddle zero (not for e.g. CV MSE curves)
  const spansZero = (plot.series ?? []).some((s) => (s.ci_low ?? []).some((v) => typeof v === 'number' && v < 0))
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const main = series.find((s) => s.ci_low && s.ci_high) ?? series[0]
    const refs = series.filter((s) => s !== main)
    const numeric = !!main && isNumArr(main.x) && refs.every((r) => isNumArr(r.x))
    const xs = main?.x ?? []
    const pts = xs.map((x, i) => {
      const lo = fin(main?.ci_low?.[i])
      const hi = fin(main?.ci_high?.[i])
      const y = fin(main?.y[i])
      const sig = lo !== null && hi !== null && (lo > 0 || hi < 0)
      return { x: numeric ? (x as number) : i, y, lo, hi, sig }
    })
    return named({
      grid: grid({ top: refs.length ? 28 : 16 }),
      legend: refs.length ? { top: 0, left: 0, data: [main?.name ?? 'estimate', ...refs.map((r) => r.name)] } : undefined,
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { dataIndex: number; seriesName: string; seriesType: string; data: unknown }
          if (q.seriesType !== 'scatter') return ''
          const pt = pts[q.dataIndex]
          return `${ttTitle(`${plot.x_label ?? ''} ${fmtCell(xs[q.dataIndex])}`)}${ttRow(t.series[0], 'estimate', fmtStat(pt.y))}${ttRow(t.ref, 'CI', `${fmtStat(pt.lo)} … ${fmtStat(pt.hi)}`)}`
        },
      },
      xAxis: numeric
        ? { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...xName(plot.x_label) }
        : { type: 'category', data: xs.map(String), axisLabel: { interval: 0, hideOverlap: true }, ...xName(plot.x_label) },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: [
        {
          type: 'custom',
          name: 'CI',
          renderItem: whisker(t.ref, false),
          data: pts.map((p) => [p.x, p.lo, p.hi]),
          encode: { x: 0, y: [1, 2] },
          silent: true,
          z: 1,
        },
        {
          type: 'scatter',
          name: main?.name ?? 'estimate',
          data: pts.map((p) => ({ value: [p.x, p.y], itemStyle: { color: p.sig ? t.series[0] : t.surface, borderColor: p.sig ? t.series[0] : t.muted, borderWidth: 1.5 } })),
          symbolSize: 8,
          color: t.series[0],
          z: 3,
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] },
        },
        ...refs.map((r) => ({
          type: 'line' as const,
          name: r.name,
          data: r.x.map((x, i) => [numeric ? x : i, fin(r.y[i])]),
          showSymbol: false,
          color: t.faint,
          lineStyle: { type: 'dashed' as const, color: t.faint, width: 1.25 },
          z: 2,
        })),
      ],
    } as EChartsOption)
  }, [plot, t])
  return (
    <div>
      <EChart option={option} height={height} ariaLabel={plot.title} />
      {spansZero && <p className="mt-1 text-2xs text-muted">Filled markers: interval excludes zero. Hollow: not distinguishable from zero.</p>}
    </div>
  )
}

/* ───────────── forest plot from coefficients ───────────── */

export function ForestChart({ coefs, alphaLevel = 0.05 }: { coefs: StudioCoefficient[]; alphaLevel?: number }) {
  const t = useChartTokens()
  const hasExp = coefs.some((c) => fin(c.exp_estimate) !== null)
  const [showConst, setShowConst] = useState(false)
  const [scale, setScale] = useState<'link' | 'exp'>('link')
  const rows = useMemo(() => coefs.filter((c) => showConst || !/^(const|intercept)$/i.test(c.term)).filter((c) => fin(c.estimate) !== null), [coefs, showConst])
  const exp = scale === 'exp' && hasExp
  const ref = exp ? 1 : 0
  const option = useMemo<EChartsOption>(() => {
    const ordered = [...rows].reverse()
    const pts = ordered.map((c, i) => {
      const y = exp ? fin(c.exp_estimate) : fin(c.estimate)
      const lo = exp ? fin(c.exp_ci_low) : fin(c.ci_low)
      const hi = exp ? fin(c.exp_ci_high) : fin(c.ci_high)
      const sig = lo !== null && hi !== null && (lo > ref || hi < ref)
      return { i, y, lo, hi, sig, c }
    })
    return named({
      grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { seriesType: string; dataIndex: number }
          if (q.seriesType !== 'scatter') return ''
          const pt = pts[q.dataIndex]
          return `${ttTitle(pt.c.term)}${ttRow(t.series[0], exp ? 'exp(estimate)' : 'estimate', fmtStat(pt.y))}${ttRow(t.ref, `${Math.round((1 - alphaLevel) * 100)}% CI`, `${fmtStat(pt.lo)} … ${fmtStat(pt.hi)}`)}${ttRow(t.ref, 'p-value', fmtP(pt.c.p_value))}${pt.c.p_value_adj != null ? ttRow(t.ref, 'p adj. (BH)', fmtP(pt.c.p_value_adj)) : ''}`
        },
      },
      yAxis: { type: 'category', data: ordered.map((c) => c.term), axisLabel: { color: t.fg }, axisTick: { show: false } },
      xAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, name: exp ? 'exp(estimate)' : 'estimate', nameLocation: 'middle', nameGap: 24 },
      series: [
        { type: 'custom', renderItem: whisker(t.ref, true), data: pts.map((p) => [p.i, p.lo, p.hi]), encode: { y: 0, x: [1, 2] }, silent: true, z: 1 },
        {
          type: 'scatter',
          data: pts.map((p) => ({ value: [p.y, p.i], itemStyle: { color: p.sig ? t.series[0] : t.surface, borderColor: p.sig ? t.series[0] : t.muted, borderWidth: 1.5 } })),
          symbolSize: 9,
          z: 3,
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: ref }] },
        },
      ],
    } as EChartsOption)
  }, [rows, exp, ref, t, alphaLevel])
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-3 text-xs text-muted">
        <label className="inline-flex items-center gap-1.5">
          <Switch label="Show intercept" checked={showConst} onCheckedChange={setShowConst} /> Intercept
        </label>
        {hasExp && (
          <Segmented
            label="Coefficient scale"
            size="xs"
            value={scale}
            onChange={setScale}
            options={[
              { value: 'link', label: 'Link scale' },
              { value: 'exp', label: 'exp (ratio)' },
            ]}
          />
        )}
        <span>Filled: {Math.round((1 - alphaLevel) * 100)}% interval excludes {ref}.</span>
      </div>
      <EChart option={option} height={Math.max(140, rows.length * 26 + 50)} ariaLabel="Coefficient forest plot with confidence intervals" />
    </div>
  )
}

/* ───────────── heatmap ───────────── */

function HeatmapPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const xs = (plot.x ?? []).map((v) => tsLabel(v))
    const ys = (plot.y ?? []).map((v) => String(v))
    const z = plot.z ?? []
    const data: [number, number, number | null][] = []
    let m = 0
    z.forEach((row, yi) =>
      row.forEach((v, xi) => {
        const f = fin(v)
        if (f !== null) m = Math.max(m, Math.abs(f))
        data.push([xi, yi, f])
      }),
    )
    const corr = /correl/i.test(plot.title)
    const lim = corr ? 1 : m || 1
    const labels = xs.length * ys.length <= 120 && xs.length <= 16
    return named({
      grid: { left: 8, right: 64, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        formatter: (p: unknown) => {
          const q = p as { data: [number, number, number | null] }
          return `${ttTitle(`${ys[q.data[1]]} · ${xs[q.data[0]]}`)}${ttRow(t.ref, 'value', fmtStat(q.data[2]))}`
        },
      },
      xAxis: { type: 'category', data: xs, splitArea: { show: false }, axisLabel: { rotate: xs.length > 8 ? 45 : 0, hideOverlap: true, fontSize: 10 } },
      yAxis: { type: 'category', data: ys, inverse: true, axisLabel: { fontSize: 10 } },
      visualMap: {
        min: -lim,
        max: lim,
        calculable: false,
        orient: 'vertical',
        right: 0,
        top: 'middle',
        itemHeight: Math.min(140, height - 60),
        itemWidth: 10,
        text: [fmtStat(lim, 2), fmtStat(-lim, 2)],
        textStyle: { fontSize: 10 },
        inRange: { color: divergingColors(t) },
      },
      series: [
        {
          type: 'heatmap',
          data,
          label: { show: labels, formatter: (p: unknown) => fmtStat((p as { data: [number, number, number | null] }).data[2], 2), fontSize: 10, color: t.fg },
          itemStyle: { borderColor: t.surface, borderWidth: 1 },
          emphasis: { itemStyle: { borderColor: t.fg, borderWidth: 1 } },
        },
      ],
    } as EChartsOption)
  }, [plot, t, height])
  return <EChart option={option} height={height} ariaLabel={plot.title} />
}

/* ───────────── scatter_points (PCA projection coloured by cluster) ───────────── */

export function PointsPlot({ plot, height, sectorNames, probabilities }: { plot: StudioPlot; height: number; sectorNames?: Record<string, string>; probabilities?: Record<string, number> }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const pts = (plot.points ?? []) as { label?: unknown; cluster?: unknown; x?: unknown; y?: unknown; sector_id?: unknown }[]
    const NONE = -99
    const clusters = [...new Set(pts.map((p) => (typeof p.cluster === 'number' ? p.cluster : NONE)))].sort((a, b) => a - b)
    const many = pts.length > 300
    const showLabels = pts.length <= 40
    return named({
      grid: grid({ top: clusters.length > 1 ? 28 : 12, bottom: 4 }),
      legend: clusters.length > 1 ? { top: 0, left: 0, itemWidth: 10, itemHeight: 10, icon: undefined } : undefined,
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { data: { raw: (typeof pts)[number] }; color: string }
          const r = q.data.raw
          const sec = typeof r.sector_id === 'string' ? (sectorNames?.[r.sector_id] ?? r.sector_id) : null
          const pr = probabilities?.[String(r.label)]
          return `${ttTitle(tsLabel(r.label))}${typeof r.cluster === 'number' ? ttRow(q.color, 'cluster', r.cluster === -1 ? 'noise' : String(r.cluster)) : ''}${typeof pr === 'number' ? ttRow(t.ref, 'membership prob.', `${Math.round(pr * 100)}%`) : ''}${sec ? ttRow(t.ref, 'sector', sec) : ''}${ttRow(t.ref, plot.x_label ?? 'x', fmtStat(r.x))}${ttRow(t.ref, plot.y_label ?? 'y', fmtStat(r.y))}`
        },
      },
      xAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...xName(plot.x_label) },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: clusters.map((c) => {
        const noise = c === -1
        const st = noise ? { color: t.faint, symbol: 'emptyCircle' } : clusterStyle(t, c === NONE ? -1 : c)
        return {
          type: 'scatter',
          name: c === NONE ? 'observations' : noise ? 'noise' : `cluster ${c}`,
          symbol: st.symbol,
          symbolSize: many ? 6 : 9,
          color: st.color,
          itemStyle: noise ? { color: 'transparent', borderColor: t.faint, borderWidth: 1 } : { color: alpha(st.color, many ? 0.7 : 0.9), borderColor: st.color, borderWidth: 0.5 },
          label: { show: showLabels, position: 'right', formatter: (p: unknown) => tsLabel((p as { data: { raw: (typeof pts)[number] } }).data.raw.label), fontSize: 10, color: t.muted },
          data: pts
            .filter((p) => (typeof p.cluster === 'number' ? p.cluster : NONE) === c)
            .map((p) => {
              const pr = probabilities?.[String(p.label)]
              return { value: [fin(p.x), fin(p.y)], raw: p, ...(typeof pr === 'number' && !noise ? { itemStyle: { opacity: 0.2 + 0.8 * pr } } : {}) }
            }),
        }
      }),
    } as EChartsOption)
  }, [plot, t, sectorNames, probabilities])
  return <EChart option={option} height={height} ariaLabel={`${plot.title}: points coloured and shaped by cluster`} />
}

/* ───────────── biplot arrows ───────────── */

function ArrowsPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const arrows = plot.arrows ?? []
    const m = Math.max(0.1, ...arrows.flatMap((a) => [Math.abs(a.x), Math.abs(a.y)])) * 1.2
    return named({
      grid: grid({ top: 12, bottom: 4 }),
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { seriesType: string; dataIndex: number }
          const a = arrows[q.dataIndex]
          return a ? `${ttTitle(a.feature)}${ttRow(t.series[0], 'PC1 loading', fmtStat(a.x))}${ttRow(t.series[0], 'PC2 loading', fmtStat(a.y))}` : ''
        },
      },
      xAxis: { type: 'value', min: -m, max: m, axisLabel: { formatter: (v: number) => fmtStat(v, 2) }, ...xName('PC1') },
      yAxis: { type: 'value', min: -m, max: m, axisLabel: { formatter: (v: number) => fmtStat(v, 2) }, ...yName('PC2') },
      series: [
        {
          type: 'lines',
          coordinateSystem: 'cartesian2d',
          symbol: ['none', 'arrow'],
          symbolSize: 7,
          lineStyle: { color: t.series[0], width: 1.25, opacity: 0.9 },
          data: arrows.map((a) => ({ coords: [[0, 0], [a.x, a.y]] })),
          silent: true,
        },
        {
          type: 'scatter',
          symbolSize: 4,
          color: t.series[0],
          data: arrows.map((a) => [a.x, a.y]),
          label: { show: true, position: 'right', formatter: (p: unknown) => arrows[(p as { dataIndex: number }).dataIndex]?.feature ?? '', fontSize: 10, color: t.muted },
        },
      ],
    } as EChartsOption)
  }, [plot, t])
  return <EChart option={option} height={height} ariaLabel={plot.title} />
}

/* ───────────── dendrogram with cut slider ───────────── */

export function DendrogramPlot({
  plot,
  linkage,
  height,
  onUseCut,
  memberClusters,
}: {
  plot: StudioPlot
  linkage: number[][] | null
  height: number
  onUseCut?: (h: number) => void
  /** leaf label → the model's cluster id, so colours match the scatter / members table when the partitions agree */
  memberClusters?: Record<string, number>
}) {
  const t = useChartTokens()
  const { ic, dc, leaves, leafLabels } = useMemo(() => ({ ic: plot.icoord ?? [], dc: plot.dcoord ?? [], leaves: plot.leaves ?? [], leafLabels: plot.leaf_labels ?? [] }), [plot])
  const maxH = Math.max(1e-9, ...dc.flat())
  const canCut = !!linkage && linkage.length > 0 && !plot.truncated && leaves.length === linkage.length + 1
  const [cut, setCut] = useState<number>(() => fin(plot.cut_height) ?? maxH * 0.7)
  const labels = useMemo(() => {
    if (!canCut || !linkage) return null
    const raw = cutLinkage(linkage, cut)
    if (!memberClusters) return raw
    // relabel client clusters with the model's ids when each maps one-to-one
    const map = new Map<number, Set<number>>()
    leaves.forEach((obs, pos) => {
      const srv = memberClusters[leafLabels[pos]]
      if (srv === undefined || raw[obs] === undefined) return
      if (!map.has(raw[obs])) map.set(raw[obs], new Set())
      map.get(raw[obs])?.add(srv)
    })
    const target = [...map.values()].map((v) => (v.size === 1 ? [...v][0] : -1))
    const injective = target.every((x) => x >= 0) && new Set(target).size === target.length && map.size === clusterCount(raw)
    if (!injective) return raw
    const remap = new Map([...map.entries()].map(([k, v]) => [k, [...v][0]]))
    return raw.map((r) => remap.get(r) ?? r)
  }, [canCut, linkage, cut, memberClusters, leaves, leafLabels])
  const k = labels ? clusterCount(labels) : null
  const option = useMemo<EChartsOption>(() => {
    const linkCl = labels ? linkClusters(ic, dc, leaves, labels, cut) : ic.map(() => -1)
    const rotate = leafLabels.length > 16 ? 90 : 0
    const leafCl = labels ? leaves.map((obs) => labels[obs] ?? -1) : leaves.map(() => -1)
    return named({
      grid: { left: 8, right: 16, top: 12, bottom: 8, containLabel: true },
      tooltip: { trigger: 'item', formatter: (p: unknown) => {
        const q = p as { seriesType: string; dataIndex: number }
        if (q.seriesType !== 'scatter') return ''
        return `${ttTitle(leafLabels[q.dataIndex] ?? '')}${leafCl[q.dataIndex] >= 0 ? ttRow(clusterStyle(t, leafCl[q.dataIndex]).color, 'cluster at cut', String(leafCl[q.dataIndex])) : ''}`
      } },
      xAxis: { type: 'value', min: 0, max: Math.max(10, leaves.length * 10), show: false },
      yAxis: { type: 'value', min: 0, max: maxH * 1.05, axisLabel: { formatter: axisFmt }, ...yName('distance') },
      series: [
        {
          type: 'lines',
          coordinateSystem: 'cartesian2d',
          polyline: true,
          silent: true,
          data: ic.map((xs, i) => ({
            coords: xs.map((x, j) => [x, dc[i][j]]),
            lineStyle: { color: linkCl[i] >= 0 ? clusterStyle(t, linkCl[i]).color : t.ref, width: 1.4 },
          })),
          markLine: canCut
            ? { silent: true, symbol: 'none', animation: false, label: { show: true, formatter: `cut ${fmtStat(cut, 3)}`, color: t.fg, fontSize: 10, position: 'insideEndTop' }, lineStyle: { color: t.accent, type: 'dashed', width: 1.25 }, data: [{ yAxis: cut }] }
            : undefined,
        },
        {
          type: 'scatter',
          symbolSize: 6,
          data: leaves.map((_, p) => ({ value: [10 * p + 5, 0], itemStyle: { color: leafCl[p] >= 0 ? clusterStyle(t, leafCl[p]).color : t.ref } })),
          symbol: 'circle',
          label: {
            show: leafLabels.length <= 80,
            position: rotate ? 'bottom' : 'bottom',
            rotate,
            align: rotate ? 'right' : 'center',
            verticalAlign: rotate ? 'middle' : 'top',
            distance: 6,
            formatter: (p: unknown) => leafLabels[(p as { dataIndex: number }).dataIndex] ?? '',
            fontSize: 10,
            color: t.muted,
          },
        },
      ],
    } as EChartsOption)
  }, [ic, dc, leaves, leafLabels, labels, cut, maxH, canCut, t])
  const labelPad = leafLabels.length > 16 ? Math.min(110, 7 * Math.max(...leafLabels.map((l) => l.length), 4)) : 24
  return (
    <div>
      {canCut && linkage && (
        <div className="mb-2 flex flex-wrap items-center gap-3 text-xs">
          <span className="label-caps">Cut height</span>
          <Slider label="Dendrogram cut height" className="w-56" min={0} max={maxH} step={maxH / 200} value={[cut]} onValueChange={(v) => setCut(v[0])} />
          <span className="num text-fg">{fmtStat(cut, 3)}</span>
          <span className="text-muted">
            → <span className="num font-medium text-fg">{k}</span> cluster{k === 1 ? '' : 's'}
          </span>
          <Segmented
            label="Snap cut to k clusters"
            size="xs"
            value={String(k ?? '')}
            onChange={(v) => {
              const h = heightForK(linkage, Number(v))
              if (h !== null) setCut(h)
            }}
            options={[2, 3, 4, 5, 6, 8].filter((x) => x <= linkage.length + 1).map((x) => ({ value: String(x), label: `k=${x}` }))}
          />
          {onUseCut && (
            <Button size="xs" variant="outline" onClick={() => onUseCut(cut)} title="Copy this cut into the builder (distance_threshold) — re-run to make it the model">
              Use this cut in builder
            </Button>
          )}
        </div>
      )}
      <EChart option={option} height={height + labelPad} ariaLabel={`${plot.title}${k ? `, cut into ${k} clusters` : ''}`} />
      {plot.truncated && <p className="mt-1 text-2xs text-muted">Dendrogram truncated by the server; the cut slider needs the full tree.</p>}
      {canCut && <p className="mt-1 text-2xs text-muted">Exploration only: the cut is recomputed in the browser from the linkage matrix. The model’s own cut is in the cluster tables.</p>}
    </div>
  )
}

/* ───────────── forecast fan ───────────── */

export function FanChart({ forecast, height = 300, yLabel }: { forecast: StudioForecast; height?: number; yLabel?: string | null }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const hd = forecast.history?.dates ?? []
    const hv = forecast.history?.values ?? []
    const fd = forecast.dates
    const lastH = hd.length ? ([hd[hd.length - 1], fin(hv[hv.length - 1])] as [string, number | null]) : null
    const band = (lo: (number | null)[] | null | undefined, hi: (number | null)[] | null | undefined) =>
      fd.map((d, i) => {
        const l = fin(lo?.[i])
        const h = fin(hi?.[i])
        return { base: [d, l] as [string, number | null], width: [d, l !== null && h !== null ? h - l : null] as [string, number | null] }
      })
    // GARCH sigma forecasts carry no interval (upper = mean): draw the path only
    const degenerate = fd.length > 0 && fd.every((_, i) => fin(forecast.upper?.[i]) === fin(forecast.mean[i]))
    const b95 = degenerate ? null : band(forecast.lower, forecast.upper)
    const b80 = forecast.lower_80 && forecast.upper_80 ? band(forecast.lower_80, forecast.upper_80) : null
    const pct = Math.round((1 - (forecast.alpha ?? 0.05)) * 100)
    const bandSeries = (name: string, b: ReturnType<typeof band>, stack: string, op: number) => [
      { type: 'line', name: `${name}-base`, data: b.map((x) => x.base), stack, stackStrategy: 'all', lineStyle: { opacity: 0 }, showSymbol: false, silent: true, tooltip: { show: false }, legendHoverLink: false },
      { type: 'line', name, data: b.map((x) => x.width), stack, stackStrategy: 'all', lineStyle: { opacity: 0 }, showSymbol: false, areaStyle: { color: alpha(t.series[0], op) }, color: alpha(t.series[0], op + 0.2), silent: true },
    ]
    const byDate = new Map<string, Record<string, number | null>>()
    fd.forEach((d, i) => byDate.set(d, { mean: fin(forecast.mean[i]), lo: fin(forecast.lower?.[i]), hi: fin(forecast.upper?.[i]), lo80: fin(forecast.lower_80?.[i]), hi80: fin(forecast.upper_80?.[i]) }))
    return named({
      grid: grid({ top: 28 }),
      legend: { top: 0, left: 0, data: ['history', 'forecast', ...(b95 ? [`${pct}% interval`] : []), ...(b80 ? ['80% interval'] : [])] },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { axisValue: number; seriesName: string; data: [string, number | null] }[]
          if (!arr.length) return ''
          const d = arr[0].data[0]
          const f = byDate.get(d)
          const h = arr.find((p) => p.seriesName === 'history')
          let s = ttTitle(fmtDate(String(d).slice(0, 10)))
          if (h && fin(h.data[1]) !== null) s += ttRow(t.ref, 'actual', fmtStat(h.data[1]))
          if (f) {
            s += ttRow(t.series[0], 'forecast', fmtStat(f.mean))
            if (f.lo80 !== null) s += ttRow(alpha(t.series[0], 0.5), '80%', `${fmtStat(f.lo80)} … ${fmtStat(f.hi80)}`)
            if (b95) s += ttRow(alpha(t.series[0], 0.3), `${pct}%`, `${fmtStat(f.lo)} … ${fmtStat(f.hi)}`)
          }
          return s
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(yLabel) },
      series: [
        ...(b95 ? bandSeries(`${pct}% interval`, b95, 'b95', 0.14) : []),
        ...(b80 ? bandSeries('80% interval', b80, 'b80', 0.2) : []),
        { type: 'line', name: 'history', data: hd.map((d, i) => [d, fin(hv[i])]), showSymbol: false, color: t.ref, lineStyle: { color: t.ref, width: 1.25 } },
        {
          type: 'line',
          name: 'forecast',
          data: [...(lastH ? [lastH] : []), ...fd.map((d, i) => [d, fin(forecast.mean[i])])],
          showSymbol: fd.length <= 36,
          symbolSize: 4,
          color: t.series[0],
          lineStyle: { color: t.series[0], width: 1.75 },
          z: 5,
        },
      ],
    } as EChartsOption)
  }, [forecast, t, yLabel])
  return <EChart option={option} height={height} ariaLabel={`Forecast fan chart: ${forecast.steps} steps with ${Math.round((1 - (forecast.alpha ?? 0.05)) * 100)}% and 80% intervals`} />
}

/* ───────────── out-of-sample actual vs forecast ───────────── */

export function OosChart({ evaluation, height = 220 }: { evaluation: StudioEvaluation; height?: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const cols = evaluation.columns ?? []
    const di = cols.indexOf('date')
    const ai = cols.indexOf('actual')
    const fi = cols.indexOf('forecast')
    const pts = (evaluation.points ?? []) as unknown[][]
    const rows = pts.filter((p) => isDateLike(p[di]))
    return named({
      grid: grid({ top: 28 }),
      legend: { top: 0, left: 0 },
      tooltip: { trigger: 'axis', valueFormatter: (v) => fmtStat(v as number) },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt } },
      series: [
        { type: 'line', name: 'actual', data: rows.map((p) => [p[di], fin(p[ai])]), showSymbol: rows.length <= 30, symbolSize: 4, color: t.ref, lineStyle: { color: t.ref, width: 1.25 } },
        { type: 'line', name: 'forecast (out-of-sample)', data: rows.map((p) => [p[di], fin(p[fi])]), showSymbol: rows.length <= 30, symbolSize: 4, color: t.series[0], lineStyle: { color: t.series[0], width: 1.5 } },
      ],
    } as EChartsOption)
  }, [evaluation, t])
  return <EChart option={option} height={height} ariaLabel="Out-of-sample forecasts against realised values" />
}

/* ───────────── cluster size bars ───────────── */

export function ClusterSizes({ sizes }: { sizes: number[] }) {
  const t = useChartTokens()
  const total = sizes.reduce((a, b) => a + b, 0) || 1
  return (
    <div className="space-y-1">
      {sizes.map((s, c) => {
        const st = clusterStyle(t, c)
        return (
          <div key={c} className="flex items-center gap-2 text-xs">
            <span className="w-16 shrink-0 text-muted">cluster {c}</span>
            <span className="relative h-2 flex-1 overflow-hidden rounded-full bg-surface-3">
              <span className="absolute inset-y-0 left-0 rounded-full" style={{ width: `${(100 * s) / total}%`, background: st.color }} />
            </span>
            <span className="num w-20 shrink-0 text-right text-fg">
              {s} <span className="text-muted">({Math.round((100 * s) / total)}%)</span>
            </span>
          </div>
        )
      })}
    </div>
  )
}

/** Swatch matching the chart identity of a cluster (colour + shape). */
export function ClusterSwatch({ cluster }: { cluster: number }) {
  const t = useChartTokens()
  const st = clusterStyle(t, cluster)
  const shape = st.symbol === 'rect' ? 'rounded-[1px]' : st.symbol === 'circle' ? 'rounded-full' : 'rotate-45 rounded-[1px]'
  return <span aria-hidden className={`inline-block size-2.5 shrink-0 ${shape}`} style={{ background: st.color }} />
}

/* ═════════════ v1.6 plot kinds ═════════════ */

/** Stacked-area band (invisible base + width) on a shared x axis. */
function bandPair(name: string, xs: unknown[], lower: (number | null)[], upper: (number | null)[], color: string, op: number, stack: string) {
  return [
    { type: 'line', name: `${name} base`, data: xs.map((x, i) => [x, fin(lower[i])]), stack, stackStrategy: 'all', lineStyle: { opacity: 0 }, showSymbol: false, silent: true, tooltip: { show: false } },
    {
      type: 'line',
      name,
      data: xs.map((x, i) => {
        const l = fin(lower[i])
        const u = fin(upper[i])
        return [x, l !== null && u !== null ? u - l : null]
      }),
      stack,
      stackStrategy: 'all',
      lineStyle: { opacity: 0 },
      showSymbol: false,
      areaStyle: { color: alpha(color, op) },
      color: alpha(color, Math.min(1, op + 0.25)),
      silent: true,
      tooltip: { show: false },
    },
  ]
}

const MUTED_NAMES = /^(\|?return\|?|actual|history)$/i

/** ts_band: date axis, centre lines, shaded bands, reference hlines / vlines. Many lines (placebos) become grey spaghetti. */
export function TsBandPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const series = plot.series ?? []
    const xs = (plot.x as unknown[] | undefined) ?? series[0]?.x ?? []
    const spaghetti = series.length > 6
    const bandNames = new Set((plot.bands ?? []).map((b) => b.name))
    let pal = 0
    const colorOf = new Map<string, string>()
    const lines: Record<string, unknown>[] = series.map((s, i) => {
      const muted = MUTED_NAMES.test(s.name) || (spaghetti && i > 0)
      const color = muted ? t.ref : t.series[pal++ % t.series.length]
      colorOf.set(s.name, color)
      const dashed = /synthetic/i.test(s.name)
      return {
        type: 'line',
        name: spaghetti && i > 0 ? 'placebos' : s.name,
        data: (s.x?.length ? s.x : xs).map((x, j) => [x, fin(s.y[j])]),
        showSymbol: false,
        sampling: 'lttb',
        color,
        lineStyle: { color, width: spaghetti ? (i === 0 ? 2.25 : 1) : muted ? 1 : 1.6, opacity: spaghetti && i > 0 ? 0.35 : 1, type: dashed ? 'dashed' : 'solid' },
        z: spaghetti && i > 0 ? 1 : muted ? 2 : 4,
      }
    })
    const bands = (plot.bands ?? []).flatMap((b, i) => {
      const owner = series[i]?.name
      return bandPair(b.name, xs, b.lower, b.upper, owner ? (colorOf.get(owner) ?? t.series[0]) : t.series[0], 0.16, `band${i}`)
    })
    const mark = {
      silent: true,
      symbol: 'none',
      animation: false,
      data: [
        ...(plot.hlines ?? [])
          .filter((h) => fin(h.y) !== null)
          .map((h) => ({
            yAxis: h.y as number,
            label: { show: !!h.label, formatter: h.label ?? '', position: 'insideEndTop', color: t.muted, fontSize: 10 },
            lineStyle: { color: h.label ? t.faint : t.borderStrong, type: h.label ? 'dashed' : 'solid', width: 1 },
          })),
        ...(plot.vlines ?? []).map((v) => ({
          xAxis: v.x as string,
          label: { show: !!v.label, formatter: v.label ?? '', position: 'insideEndTop', color: t.fg, fontSize: 10 },
          lineStyle: { color: t.accent, type: 'dashed', width: 1.25 },
        })),
      ],
    }
    if (lines.length) lines[0].markLine = mark
    const legendNames = [...new Set(lines.map((l) => String(l.name))), ...bandNames]
    return named({
      grid: grid({ top: legendNames.length > 1 ? 28 : 16 }),
      legend: legendNames.length > 1 ? { top: 0, left: 0, data: legendNames } : undefined,
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = (ps as { seriesName: string; data: [string, number | null]; color: string }[]).filter((p) => !/ base$/.test(p.seriesName) && !bandNames.has(p.seriesName))
          if (!arr.length) return ''
          const shown = spaghetti ? arr.slice(0, 1) : arr
          return ttTitle(tsLabel(arr[0].data[0])) + shown.map((p) => ttRow(p.color, p.seriesName, fmtStat(p.data[1]))).join('')
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label || null) },
      series: [...bands, ...lines],
    } as EChartsOption)
  }, [plot, t])
  const n = plot.series?.length ?? 0
  return (
    <div>
      <EChart option={option} height={height} ariaLabel={plot.title} />
      {n > 6 && (
        <p className="mt-1 text-2xs text-muted">
          Accent: {plot.series?.[0]?.name}. Grey: {n - 1} placebo runs (each donor treated as if it were the treated unit).
        </p>
      )}
    </div>
  )
}

/** event_path: coefficient path over relative periods with CI band, reference period and event marker. */
export function EventPathPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const xs = useMemo(() => ((plot.x as unknown[]) ?? []).map((v) => Number(v)), [plot])
  const ref = fin(plot.reference)
  const option = useMemo<EChartsOption>(() => {
    const ys = ((plot.y as unknown[]) ?? []).map(fin)
    const lo = plot.lower ?? []
    const hi = plot.upper ?? []
    const ev = fin(plot.event_x)
    return named({
      grid: grid({ top: 28 }),
      legend: { top: 0, left: 0, data: ['estimate', 'CI'] },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { dataIndex: number }[]
          if (!arr.length) return ''
          const i = arr[0].dataIndex
          return `${ttTitle(`period ${xs[i]}${xs[i] === ref ? ' (reference)' : xs[i] === ev ? ' (event)' : ''}`)}${ttRow(t.series[0], 'estimate', fmtStat(ys[i]))}${ttRow(t.ref, 'CI', `${fmtStat(lo[i])} … ${fmtStat(hi[i])}`)}`
        },
      },
      xAxis: {
        type: 'value',
        min: xs.length ? Math.min(...xs) - 0.5 : undefined,
        max: xs.length ? Math.max(...xs) + 0.5 : undefined,
        axisLabel: { formatter: (v: number) => (Number.isInteger(v) ? String(v) : '') },
        ...xName(plot.x_label),
      },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: [
        ...bandPair('CI', xs, lo, hi, t.series[0], 0.16, 'ci'),
        {
          type: 'line',
          name: 'estimate',
          data: xs.map((x, i) => {
            const l = fin(lo[i])
            const h = fin(hi[i])
            const sig = l !== null && h !== null && (l > 0 || h < 0)
            const isRef = ref !== null && x === ref
            return {
              value: [x, ys[i]],
              symbol: isRef ? 'diamond' : 'circle',
              symbolSize: isRef ? 10 : xs.length > 60 ? 3 : 6,
              itemStyle: { color: isRef || !sig ? t.surface : t.series[0], borderColor: isRef ? t.fg : t.series[0], borderWidth: 1.5 },
            }
          }),
          color: t.series[0],
          lineStyle: { color: t.series[0], width: 1.75 },
          showSymbol: true,
          z: 5,
          markLine: {
            silent: true,
            symbol: 'none',
            animation: false,
            data: [
              { yAxis: 0, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, label: { show: false } },
              ...(ev !== null
                ? [{ xAxis: ev - 0.5, lineStyle: { color: t.accent, type: 'dashed', width: 1.25 }, label: { show: true, formatter: 'event', position: 'insideEndTop', color: t.fg, fontSize: 10 } }]
                : []),
            ],
          },
        },
      ],
    } as EChartsOption)
  }, [plot, t, xs, ref])
  return (
    <div>
      <EChart option={option} height={height} ariaLabel={plot.title} />
      <p className="mt-1 text-2xs text-muted">
        Shaded: confidence interval. Filled markers: interval excludes zero.{ref !== null ? ` Diamond: reference period ${ref} (normalised to 0).` : ''} Dashed line: event / treatment start.
      </p>
    </div>
  )
}

type IrfCellData = NonNullable<StudioPlot['cells']>[number]

/** One impulse → response cell of the IRF grid. */
function IrfCell({ cell, horizons, yMin, yMax }: { cell: IrfCellData; horizons: number[]; yMin: number; yMax: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(
    () =>
      ({
        grid: { left: 4, right: 6, top: 6, bottom: 4, containLabel: true },
        tooltip: {
          trigger: 'axis',
          formatter: (ps: unknown) => {
            const i = (ps as { dataIndex: number }[])[0]?.dataIndex ?? 0
            return `${ttTitle(`${cell.impulse} shock → ${cell.response}, h=${horizons[i]}`)}${ttRow(t.series[0], 'response', fmtStat(cell.mean[i]))}${ttRow(t.ref, 'band', `${fmtStat(cell.lower[i])} … ${fmtStat(cell.upper[i])}`)}`
          },
        },
        xAxis: { type: 'value', min: 0, max: horizons[horizons.length - 1] ?? 1, axisLabel: { fontSize: 9, hideOverlap: true }, splitLine: { show: false } },
        yAxis: { type: 'value', min: yMin, max: yMax, splitNumber: 2, axisLabel: { fontSize: 9, showMinLabel: false, showMaxLabel: false, formatter: (v: number) => fmtStat(v, 2) } },
        series: [
          ...bandPair('band', horizons, cell.lower, cell.upper, t.series[0], 0.18, 'b'),
          {
            type: 'line',
            name: 'response',
            data: horizons.map((h, i) => [h, fin(cell.mean[i])]),
            showSymbol: false,
            color: t.series[0],
            lineStyle: { color: t.series[0], width: 1.5 },
            markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] },
          },
        ],
      }) as EChartsOption,
    [cell, horizons, yMin, yMax, t],
  )
  return <EChart option={option} height={120} ariaLabel={`Response of ${cell.response} to a ${cell.impulse} shock`} />
}

/** irf: k × k small multiples (rows = response, columns = impulse), shared y range per response row. */
export function IrfGrid({ plot }: { plot: StudioPlot }) {
  const { vars, cells, horizons } = useMemo(() => ({ vars: plot.variables ?? [], cells: plot.cells ?? [], horizons: plot.horizons ?? [] }), [plot])
  const ranges = useMemo(() => {
    const out: Record<string, [number, number]> = {}
    for (const r of vars) {
      const vals = cells
        .filter((c) => c.response === r)
        .flatMap((c) => [...c.lower, ...c.upper, ...c.mean])
        .map(fin)
        .filter((v): v is number => v !== null)
      const lo = Math.min(0, ...vals)
      const hi = Math.max(0, ...vals)
      const pad = (hi - lo) * 0.08 || 0.1
      out[r] = [lo - pad, hi + pad]
    }
    return out
  }, [vars, cells])
  return (
    <div>
      <div className="overflow-x-auto">
        <div className={cn('grid gap-x-1.5 gap-y-1', vars.length > 3 && 'min-w-[600px]')} style={{ gridTemplateColumns: `3.5rem repeat(${vars.length}, minmax(0, 1fr))` }}>
          <div className="self-end text-2xs leading-tight text-muted">resp. ↓ shock →</div>
          {vars.map((v) => (
            <div key={`h-${v}`} className="truncate text-center text-xs font-semibold text-fg">
              {v} shock
            </div>
          ))}
          {vars.map((r) => (
            <div key={`row-${r}`} className="contents">
              <div className="flex items-center text-xs font-semibold break-all text-fg">{r}</div>
              {vars.map((i) => {
                const c = cells.find((x) => x.impulse === i && x.response === r)
                return c ? (
                  <div key={`${i}-${r}`} className="rounded-sm border border-border/60">
                    <IrfCell cell={c} horizons={horizons} yMin={ranges[r][0]} yMax={ranges[r][1]} />
                  </div>
                ) : (
                  <div key={`${i}-${r}`} />
                )
              })}
            </div>
          ))}
        </div>
      </div>
      <p className="mt-1 text-2xs text-muted">
        {plot.orthogonalized ? 'Orthogonalized (Cholesky, ordering = variable order). ' : ''}Shaded: confidence band. Each row shares one y-axis; x = horizon in periods.
      </p>
    </div>
  )
}

/** fevd: one stacked-area chart per response (shares over impulses sum to 1). */
export function FevdGrid({ plot }: { plot: StudioPlot }) {
  const t = useChartTokens()
  const vars = useMemo(() => plot.variables ?? [], [plot])
  const horizons = useMemo(() => plot.horizons ?? [], [plot])
  return (
    <div>
      <div className="mb-1 flex flex-wrap gap-3 text-xs text-muted">
        {vars.map((v, i) => (
          <span key={v} className="inline-flex items-center gap-1">
            <span aria-hidden className="size-2.5 rounded-sm" style={{ background: t.series[i % t.series.length], opacity: i >= t.series.length ? 0.55 : 1 }} /> shock: {v}
          </span>
        ))}
      </div>
      <div className="grid gap-2 @xl:grid-cols-2 @3xl:grid-cols-3">
        {vars.map((r) => (
          <FevdChart key={r} response={r} vars={vars} horizons={horizons} plot={plot} />
        ))}
      </div>
    </div>
  )
}

function FevdChart({ response, vars, horizons, plot }: { response: string; vars: string[]; horizons: number[]; plot: StudioPlot }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const shares = (plot.shares ?? []).filter((s) => s.response === response)
    return {
      title: { text: `Variance of ${response}`, left: 0, top: 0, textStyle: { fontSize: 11, color: t.fg } },
      grid: { left: 4, right: 8, top: 22, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (v) => `${((v as number) * 100).toFixed(1)}%` },
      xAxis: { type: 'value', min: horizons[0] ?? 1, max: horizons[horizons.length - 1] ?? 1, axisLabel: { fontSize: 9 } },
      yAxis: { type: 'value', min: 0, max: 1, splitNumber: 2, axisLabel: { fontSize: 9, formatter: (v: number) => `${Math.round(v * 100)}%` } },
      series: vars.map((imp, i) => {
        const s = shares.find((x) => x.impulse === imp)
        const c = t.series[i % t.series.length]
        return {
          type: 'line',
          name: imp,
          stack: 'fevd',
          data: horizons.map((h, j) => [h, fin(s?.share[j])]),
          showSymbol: false,
          lineStyle: { width: 0.5, color: t.surface },
          areaStyle: { color: c, opacity: i >= t.series.length ? 0.5 : 0.85 },
          color: c,
        }
      }),
    } as EChartsOption
  }, [response, vars, horizons, plot, t])
  return <EChart option={option} height={160} ariaLabel={`Forecast-error variance decomposition of ${response}`} />
}

/** regime_prob: the modelled series on top, smoothed regime probabilities stacked underneath (shared x, no dual axis). */
export function RegimePlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const xs = (plot.x as unknown[]) ?? []
    const regimes = plot.regimes ?? []
    const ov = plot.overlay
    const hasOv = !!ov?.y?.length
    const yTitle = (name: string) => ({ name, nameLocation: 'end' as const, nameGap: 6, nameTextStyle: { align: 'left' as const } })
    return {
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      legend: { top: 0, right: 0, data: regimes.map((r) => r.label) },
      tooltip: { trigger: 'axis', valueFormatter: (v) => fmtStat(v as number, 3) },
      grid: hasOv
        ? [
            { left: 8, right: 16, top: 30, height: '28%', containLabel: true },
            { left: 8, right: 16, top: '50%', bottom: 8, containLabel: true },
          ]
        : [{ left: 8, right: 16, top: 30, bottom: 8, containLabel: true }],
      xAxis: hasOv
        ? [
            { type: 'time', gridIndex: 0, axisLabel: { show: false } },
            { type: 'time', gridIndex: 1, axisLabel: { hideOverlap: true } },
          ]
        : [{ type: 'time', gridIndex: 0, axisLabel: { hideOverlap: true } }],
      yAxis: hasOv
        ? [
            { type: 'value', gridIndex: 0, scale: true, axisLabel: { formatter: axisFmt }, ...yTitle(ov?.name ?? 'series') },
            { type: 'value', gridIndex: 1, min: 0, max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` }, ...yTitle('regime probability') },
          ]
        : [{ type: 'value', min: 0, max: 1, axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` }, ...yTitle('regime probability') }],
      series: [
        ...(hasOv
          ? [{ type: 'line', name: ov?.name ?? 'series', xAxisIndex: 0, yAxisIndex: 0, data: xs.map((x, i) => [x, fin(ov?.y?.[i])]), showSymbol: false, color: t.ref, lineStyle: { color: t.ref, width: 1.2 } }]
          : []),
        ...regimes.map((r, i) => {
          const c = t.series[(i + 1) % t.series.length]
          return {
            type: 'line',
            name: r.label,
            xAxisIndex: hasOv ? 1 : 0,
            yAxisIndex: hasOv ? 1 : 0,
            stack: 'p',
            data: xs.map((x, j) => [x, fin(r.prob[j])]),
            showSymbol: false,
            lineStyle: { width: 0 },
            areaStyle: { color: c, opacity: 0.8 },
            color: c,
          }
        }),
      ],
    } as EChartsOption
  }, [plot, t])
  return <EChart option={option} height={Math.max(height, 320)} ariaLabel={`${plot.title}: regime probabilities stacked under the modelled series`} />
}

/** Horizontal bars for weights (e.g. synthetic-control donor weights), largest first. */
export function WeightsBar({ items, title }: { items: { label: string; value: number }[]; title: string }) {
  const t = useChartTokens()
  const sorted = useMemo(() => [...items].sort((a, b) => b.value - a.value).slice(0, 25), [items])
  const option = useMemo<EChartsOption>(
    () =>
      ({
        grid: { left: 8, right: 52, top: 4, bottom: 4, containLabel: true },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => `${((v as number) * 100).toFixed(1)}%` },
        xAxis: { type: 'value', min: 0, max: Math.min(1, Math.max(0.05, ...sorted.map((s) => s.value)) * 1.15), axisLabel: { formatter: (v: number) => `${Math.round(v * 100)}%` } },
        yAxis: { type: 'category', inverse: true, data: sorted.map((s) => s.label), axisTick: { show: false }, axisLabel: { color: t.fg } },
        series: [
          {
            type: 'bar',
            data: sorted.map((s) => s.value),
            color: t.series[0],
            barMaxWidth: 16,
            label: { show: true, position: 'right', formatter: (p: unknown) => `${((p as { value: number }).value * 100).toFixed(1)}%`, color: t.muted, fontSize: 10 },
          },
        ],
      }) as EChartsOption,
    [sorted, t],
  )
  return <EChart option={option} height={Math.max(80, sorted.length * 24 + 20)} ariaLabel={title} />
}

/* ═════════════ v1.7 plot kinds ═════════════ */

/** coef_path: standardized coefficients over log10(alpha); markers for the selected / min-MSE / 1-SE alphas. */
export function CoefPathPlot({ plot, height }: { plot: StudioPlot; height: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const xs = ((plot.x as unknown[]) ?? []).map(Number)
    const paths = plot.paths ?? []
    const last = (p: (typeof paths)[number]) => Math.abs(fin(p.y[p.y.length - 1]) ?? 0)
    // colour the 4 largest final paths, the rest neutral (palette has 4 identities)
    const top = new Set([...paths].sort((a, b) => last(b) - last(a)).slice(0, 4).map((p) => p.term))
    let pal = 0
    const marks = [
      { x: fin(plot.selected_x), label: 'selected', color: t.accent, type: 'solid' as const, pos: 'insideEndTop' },
      { x: fin(plot.min_x), label: 'min MSE', color: t.faint, type: 'dashed' as const, pos: 'insideEndBottom' },
      { x: fin(plot.one_se_x), label: '1-SE', color: t.faint, type: 'dotted' as const, pos: 'insideStartTop' },
    ].filter((m) => m.x !== null)
    return named({
      grid: grid({ top: 28, right: 72 }),
      legend: { top: 0, left: 0, data: paths.filter((p) => top.has(p.term)).map((p) => p.term) },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { seriesName: string; data: [number, number | null]; color: string }[]
          if (!arr.length) return ''
          const rows = [...arr].filter((p) => fin(p.data[1]) !== 0).sort((a, b) => Math.abs(b.data[1] ?? 0) - Math.abs(a.data[1] ?? 0)).slice(0, 10)
          return ttTitle(`log10(alpha) = ${fmtStat(arr[0].data[0], 3)}`) + (rows.length ? rows.map((p) => ttRow(p.color, p.seriesName, fmtStat(p.data[1], 3))).join('') : '<div>all coefficients 0</div>')
        },
      },
      xAxis: { type: 'value', scale: true, inverse: xs.length > 1 && xs[0] > xs[xs.length - 1], axisLabel: { formatter: (v: number) => fmtStat(v, 2) }, ...xName(plot.x_label ?? 'log10(alpha)') },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: axisFmt }, ...yName(plot.y_label) },
      series: paths.map((p, i) => {
        const hi = top.has(p.term)
        const color = hi ? t.series[pal++ % t.series.length] : t.ref
        return {
          type: 'line',
          name: p.term,
          data: xs.map((x, j) => [x, fin(p.y[j])]),
          showSymbol: false,
          color,
          lineStyle: { color, width: hi ? 1.75 : 1, opacity: hi ? 1 : 0.5 },
          endLabel: { show: hi, formatter: p.term, color: t.muted, fontSize: 10 },
          z: hi ? 3 : 1,
          markLine:
            i === 0
              ? {
                  silent: true,
                  symbol: 'none',
                  animation: false,
                  data: [
                    { yAxis: 0, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, label: { show: false } },
                    ...marks.map((m) => ({ xAxis: m.x as number, lineStyle: { color: m.color, type: m.type, width: 1.25 }, label: { show: true, formatter: m.label, position: m.pos, color: t.fg, fontSize: 10 } })),
                  ],
                }
              : undefined,
        }
      }),
    } as EChartsOption)
  }, [plot, t])
  return (
    <div>
      <EChart option={option} height={height} ariaLabel={plot.title} />
      <p className="mt-1 text-2xs text-muted">Penalty decreases to the right. The four largest coefficients are coloured; the rest are grey.</p>
    </div>
  )
}

/** shap_beeswarm: one jittered row per feature (sorted by mean |SHAP|), colour = feature value (low → high). */
export function ShapBeeswarm({ plot }: { plot: StudioPlot }) {
  const t = useChartTokens()
  const feats = useMemo(() => (plot.features ?? []).slice(0, 15), [plot])
  const option = useMemo<EChartsOption>(() => {
    const names = feats.map((f) => f.feature)
    const data: [number, number, number, number][] = []
    feats.forEach((f, row) => {
      f.shap.forEach((v, j) => {
        const s = fin(v)
        if (s === null) return
        // deterministic jitter (no Math.random so re-renders are stable)
        const jit = (((j * 2654435761) % 1000) / 1000 - 0.5) * 0.6
        data.push([s, row + jit, fin(f.value?.[j]) ?? 0.5, row])
      })
    })
    return {
      grid: { left: 8, right: 70, top: 8, bottom: 28, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const d = (p as { data: [number, number, number, number] }).data
          return `${ttTitle(names[d[3]])}${ttRow(t.series[0], 'SHAP', fmtStat(d[0], 3))}${ttRow(t.ref, 'feature value (scaled)', fmtStat(d[2], 2))}`
        },
      },
      xAxis: { type: 'value', scale: true, name: plot.x_label ?? 'SHAP value', nameLocation: 'middle', nameGap: 22, axisLabel: { formatter: axisFmt } },
      yAxis: {
        type: 'value',
        min: -1,
        max: feats.length,
        inverse: true,
        interval: 1,
        axisLabel: { formatter: (v: number) => (Number.isInteger(v) && v >= 0 ? (names[v] ?? '') : ''), color: t.fg, showMinLabel: false, showMaxLabel: false },
        splitLine: { show: false },
      },
      visualMap: {
        dimension: 2,
        min: 0,
        max: 1,
        orient: 'vertical',
        right: 0,
        top: 'middle',
        itemHeight: 120,
        itemWidth: 10,
        text: ['high', 'low'],
        textStyle: { fontSize: 10 },
        calculable: false,
        inRange: { color: [t.series[1], t.faint, t.series[0]] },
      },
      series: [
        {
          type: 'scatter',
          data,
          symbolSize: 3.5,
          itemStyle: { opacity: 0.75 },
          large: data.length > 4000,
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }] },
        },
      ],
    } as EChartsOption
  }, [feats, plot.x_label, t])
  return (
    <div>
      <EChart option={option} height={Math.max(180, feats.length * 34 + 50)} ariaLabel={`${plot.title}: SHAP value per observation, coloured by feature value`} />
      <p className="mt-1 text-2xs text-muted">Each dot is one observation. Right of zero pushes the prediction up. Colour: feature value from low (blue) to high (orange).</p>
    </div>
  )
}

/** Feature importance: impurity vs permutation side by side (normalised to the largest of each). */
export function ImportanceBars({ rows }: { rows: { feature: string; impurity: number | null; permutation: number | null }[] }) {
  const t = useChartTokens()
  const sorted = useMemo(() => [...rows].sort((a, b) => (b.permutation ?? b.impurity ?? 0) - (a.permutation ?? a.impurity ?? 0)), [rows])
  const hasImp = sorted.some((r) => r.impurity !== null)
  // the two measures live on different scales: show each relative to its own maximum
  const maxP = Math.max(1e-12, ...sorted.map((r) => Math.abs(r.permutation ?? 0)))
  const maxI = Math.max(1e-12, ...sorted.map((r) => Math.abs(r.impurity ?? 0)))
  const rel = (v: number | null, m: number) => (v === null ? null : v / m)
  const option = useMemo<EChartsOption>(
    () =>
      ({
        grid: { left: 8, right: 16, top: 26, bottom: 26, containLabel: true },
        legend: { top: 0, left: 0 },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => fmtStat(v as number, 3) },
        xAxis: { type: 'value', name: 'relative importance (max = 1)', nameLocation: 'middle', nameGap: 22, axisLabel: { formatter: (v: number) => fmtStat(v, 2) } },
        yAxis: { type: 'category', inverse: true, data: sorted.map((r) => r.feature), axisTick: { show: false }, axisLabel: { color: t.fg } },
        series: [
          { type: 'bar', name: 'permutation (out-of-sample)', data: sorted.map((r) => rel(r.permutation, maxP)), color: t.series[0], barMaxWidth: 12 },
          ...(hasImp ? [{ type: 'bar', name: 'impurity / gain (in-sample)', data: sorted.map((r) => rel(r.impurity, maxI)), color: t.series[1], barMaxWidth: 12 }] : []),
        ],
      }) as EChartsOption,
    [sorted, hasImp, maxP, maxI, t],
  )
  return <EChart option={option} height={Math.max(140, sorted.length * (hasImp ? 30 : 22) + 50)} ariaLabel="Feature importance, permutation versus impurity" />
}
