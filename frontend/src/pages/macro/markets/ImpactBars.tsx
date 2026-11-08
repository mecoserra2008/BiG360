import { useMemo } from 'react'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { fmtChange } from '../fmt'

export interface ImpactRow {
  label: string
  value: number | null | undefined
  lo?: number | null
  hi?: number | null
  /** Optional comparison value (e.g. the sector) drawn as a marker. */
  ref?: number | null
  /** Optional second marker (e.g. the sector model value). */
  ref2?: number | null
}

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pctFmt = (v: number) => `${fmtChange(v * 100, 1)}%`
const pctAxis = (v: number) => `${(v * 100).toFixed(0)}%`

/** Horizontal bars of fractional impacts with 90% whiskers (lo/hi) and an optional reference marker. */
export function ImpactBars({ rows, height, ariaLabel, refLabel = 'sector', ref2Label = 'reference', valueLabel = 'impact', onClick, fmt = pctFmt, axisFmt = pctAxis }: { rows: ImpactRow[]; height?: number; ariaLabel: string; refLabel?: string; ref2Label?: string; valueLabel?: string; onClick?: (i: number) => void; fmt?: (v: number) => string; axisFmt?: (v: number) => string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const pc = (v: unknown) => (fin(v) ? fmt(v) : '—')
    const hasRef = rows.some((r) => fin(r.ref))
    const hasRef2 = rows.some((r) => fin(r.ref2))
    return {
      grid: { left: 8, right: 56, top: hasRef ? 22 : 6, bottom: 22, containLabel: true },
      legend: hasRef ? { top: 0, right: 0, itemWidth: 10, itemHeight: 8, textStyle: { color: t.muted, fontSize: 10 }, data: [valueLabel, refLabel, ...(hasRef2 ? [ref2Label] : [])] } : undefined,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const r = rows[(ps as { dataIndex: number }[])[0].dataIndex]
          return `${ttTitle(r.label)}${ttRow(fin(r.value) && r.value >= 0 ? t.up : t.down, valueLabel, pc(r.value))}${fin(r.lo) && fin(r.hi) ? ttRow(t.ref, '90% band', `${pc(r.lo)} to ${pc(r.hi)}`) : ''}${hasRef ? ttRow(t.series[1], refLabel, pc(r.ref)) : ''}${hasRef2 ? ttRow(t.series[2], ref2Label, pc(r.ref2)) : ''}`
        },
      },
      xAxis: { type: 'value', axisLabel: { formatter: axisFmt }, splitLine: { lineStyle: { color: t.grid } } },
      yAxis: { type: 'category', inverse: true, data: rows.map((r) => r.label), axisTick: { show: false }, axisLabel: { fontSize: 10, color: t.fg, width: 150, overflow: 'truncate' } },
      series: [
        {
          name: valueLabel,
          type: 'bar',
          barMaxWidth: 12,
          itemStyle: { color: t.up },
          data: rows.map((r) => ({ value: fin(r.value) ? r.value : null, itemStyle: { color: fin(r.value) && r.value >= 0 ? t.up : t.down } })),
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }] },
        },
        {
          name: '90% band',
          type: 'custom',
          silent: true,
          z: 5,
          data: rows.map((r, i) => [i, fin(r.lo) ? r.lo : NaN, fin(r.hi) ? r.hi : NaN]),
          encode: { x: [1, 2], y: 0 },
          renderItem: (_p: unknown, api: unknown) => {
            const a = api as { value: (i: number) => number; coord: (v: [number, number]) => number[]; size: (v: [number, number]) => number[] }
            const lo = a.value(1)
            const hi = a.value(2)
            if (!Number.isFinite(lo) || !Number.isFinite(hi)) return { type: 'group', children: [] }
            const s = a.coord([lo, a.value(0)])
            const e = a.coord([hi, a.value(0)])
            const h = Math.min(8, a.size([0, 1])[1] * 0.4)
            const style = { stroke: t.fg, lineWidth: 1, opacity: 0.7 }
            return {
              type: 'group',
              children: [
                { type: 'line', shape: { x1: s[0], y1: s[1], x2: e[0], y2: e[1] }, style },
                { type: 'line', shape: { x1: s[0], y1: s[1] - h / 2, x2: s[0], y2: s[1] + h / 2 }, style },
                { type: 'line', shape: { x1: e[0], y1: e[1] - h / 2, x2: e[0], y2: e[1] + h / 2 }, style },
              ],
            }
          },
        },
        ...(hasRef
          ? [
              {
                name: refLabel,
                type: 'scatter' as const,
                symbol: 'diamond',
                symbolSize: 9,
                z: 6,
                itemStyle: { color: t.series[1] },
                data: rows.map((r, i) => (fin(r.ref) ? [r.ref, i] : [NaN, i])),
              },
            ]
          : []),
        ...(hasRef2
          ? [
              {
                name: ref2Label,
                type: 'scatter' as const,
                symbol: 'circle',
                symbolSize: 7,
                z: 6,
                itemStyle: { color: t.series[2], borderColor: t.surface, borderWidth: 1 },
                data: rows.map((r, i) => (fin(r.ref2) ? [r.ref2, i] : [NaN, i])),
              },
            ]
          : []),
      ],
    } as EChartsOption
  }, [rows, t, refLabel, ref2Label, valueLabel, fmt, axisFmt])
  return (
    <EChart
      option={option}
      height={height ?? Math.max(160, rows.length * 22 + 50)}
      ariaLabel={ariaLabel}
      onEvents={onClick ? { click: (p: unknown) => onClick((p as { dataIndex: number }).dataIndex) } : undefined}
    />
  )
}
