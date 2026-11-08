import { useMemo } from 'react'
import { chartBands, placeMarkers } from '@/api/theses/adapters'
import type { ThesisPerformance, ThesisTimeline } from '@/api/theses/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { fmtNum } from '@/lib/format'

const KIND_LABEL = { journal: 'Journal', alert: 'Triggered alert', catalyst: 'Catalyst', review: 'Review' } as const
const SYMBOL = { journal: 'circle', alert: 'triangle', catalyst: 'diamond', review: 'rect' } as const

const esc = (s: string) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)

/** Price of the thesis instrument with entry / target / stop bands and timeline markers (journal, triggered alerts,
 * catalysts, reviews). Close prices are unadjusted - the unit of the bands. */
export function ThesisPriceChart({ tl, height = 320 }: { tl: ThesisTimeline; height?: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const bands = chartBands(tl.bands)
    const color = { target: t.up, stop: t.down, entry_hi: t.accent, entry_lo: t.accent, inception: t.ref }
    const placed = placeMarkers(tl.markers, tl.bars)
    const kc = { journal: t.info, alert: t.warn, catalyst: t.accent, review: t.series[3] ?? t.ref }
    const lo = tl.bands.entry_lo
    const hi = tl.bands.entry_hi
    // keep every band on screen (a target / stop far from the traded range would otherwise be clipped)
    const ys = [...tl.bars.map((b) => b.close).filter((v): v is number => v != null), ...bands.map((b) => b.value)]
    const lo0 = ys.length ? Math.min(...ys) : 0
    const hi0 = ys.length ? Math.max(...ys) : 1
    const pad = (hi0 - lo0) * 0.04 || Math.abs(hi0) * 0.02 || 1
    const vlines: { xAxis: string; name: string }[] = []
    if (tl.inception_date) vlines.push({ xAxis: tl.inception_date, name: 'Inception' })
    if (tl.closed_date) vlines.push({ xAxis: tl.closed_date, name: 'Closed' })
    return {
      grid: { left: 8, right: 70, top: 28, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0, data: ['Close', ...(['journal', 'alert', 'catalyst', 'review'] as const).filter((k) => placed.some((p) => p.kind === k)).map((k) => KIND_LABEL[k])] },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const x = p as { seriesName: string; value: [string, number]; color: string; data: { labels?: string[] } }
          if (x.data?.labels) return ttTitle(x.value[0]) + x.data.labels.map((l) => ttRow(x.color, x.seriesName, esc(l))).join('')
          return ttTitle(x.value[0]) + ttRow(x.color, 'Close', fmtNum(x.value[1], 2))
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', min: Number((lo0 - pad).toPrecision(4)), max: Number((hi0 + pad).toPrecision(4)), axisLabel: { formatter: (v: number) => fmtNum(v, 2) } },
      series: [
        {
          name: 'Close',
          type: 'line',
          data: tl.bars.map((b) => [b.date, b.close]),
          showSymbol: false,
          lineStyle: { color: t.fg, width: 1.5 },
          itemStyle: { color: t.fg },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { color: t.muted, fontSize: 9, formatter: '{b}', position: 'end' },
            data: [
              ...bands.map((b) => ({ yAxis: b.value, name: `${b.label} ${fmtNum(b.value, 2)}`, lineStyle: { color: color[b.key], type: b.key === 'inception' ? ('dotted' as const) : ('dashed' as const), width: 1.2 } })),
              ...vlines.map((v) => ({ xAxis: v.xAxis, name: v.name, lineStyle: { color: t.borderStrong, type: 'dotted' as const }, label: { position: 'insideEndTop' as const, color: t.muted, fontSize: 9, formatter: '{b}' } })),
            ],
          },
          markArea: lo != null && hi != null ? { silent: true, itemStyle: { color: alpha(t.accent, 0.1) }, data: [[{ yAxis: lo }, { yAxis: hi }]] } : undefined,
        },
        ...(['journal', 'alert', 'catalyst', 'review'] as const)
          .filter((k) => placed.some((p) => p.kind === k))
          .map((k) => ({
            name: KIND_LABEL[k],
            type: 'scatter' as const,
            symbol: SYMBOL[k],
            symbolSize: 9,
            itemStyle: { color: kc[k], borderColor: t.surface, borderWidth: 1 },
            data: placed.filter((p) => p.kind === k).map((p) => ({ value: [p.date, p.value], labels: p.labels })),
            z: 5,
          })),
      ],
    }
  }, [tl, t])
  return <EChart option={option} height={height} ariaLabel="Thesis price chart with entry, target and stop bands and timeline markers" />
}

/** Rebased (100 = inception) subject vs benchmark vs sector benchmark. */
export function ThesisPerfChart({ p, height = 220 }: { p: ThesisPerformance; height?: number }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const leg = (k: 'subject' | 'benchmark' | 'sector', name: string, color: string, dashed = false) => ({
      name,
      type: 'line' as const,
      data: p.series.map((x) => [x.date, x[k] ?? null]),
      showSymbol: false,
      connectNulls: true,
      lineStyle: { color, width: k === 'subject' ? 2 : 1.4, type: dashed ? ('dashed' as const) : ('solid' as const) },
      itemStyle: { color },
    })
    const series = [leg('subject', p.subject?.id ?? 'Subject', t.series[0])]
    if (p.benchmark?.return != null) series.push(leg('benchmark', `Benchmark ${p.benchmark.id}`, t.ref, true))
    if (p.sector?.return != null) series.push(leg('sector', `Sector ${p.sector.id}`, t.series[1]))
    return {
      grid: { left: 8, right: 14, top: 26, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0 },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { axisValueLabel: string; seriesName: string; value: [string, number | null]; color: string }[]
          return ttTitle(arr[0]?.axisValueLabel ?? '') + arr.filter((x) => x.value[1] != null).map((x) => ttRow(x.color, x.seriesName, fmtNum(x.value[1] as number, 1))).join('')
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => fmtNum(v, 0) } },
      series: series.map((s, i) => (i === 0 ? { ...s, markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong }, data: [{ yAxis: 100 }] } } : s)),
    }
  }, [p, t])
  return <EChart option={option} height={height} ariaLabel="Performance since inception rebased to 100: subject, benchmark and sector" />
}
