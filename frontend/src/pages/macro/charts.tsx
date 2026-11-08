import { useMemo } from 'react'
import { recessionBands } from '@/api/macro/adapters'
import { useUsRecessions } from '@/api/macro/hooks'
import type { MPoint } from '@/api/macro/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtMacro } from './fmt'

export interface MLine {
  name: string
  points: MPoint[]
  /** shaded interval around the line */
  band?: { lo: (number | null)[]; hi: (number | null)[]; label: string }
  dashed?: boolean
  muted?: boolean
  color?: string
}

const fin = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** NBER recession bands (official, FRED USREC) inside the visible range. */
function useShading(on: boolean, from: string | null) {
  const q = useUsRecessions()
  return useMemo(() => {
    if (!on || !q.data) return []
    return recessionBands(q.data.points).filter((b) => !from || b.end >= from)
  }, [on, q.data, from])
}

/**
 * Single-axis time-series chart. More than four identities → small multiples (one chart per line),
 * so the palette never carries more than four colours. Optional NBER recession shading.
 */
export function MacroLines({
  lines,
  height = 240,
  units,
  ariaLabel,
  shadeUsRecessions = false,
  zeroLine = false,
  className,
}: {
  lines: MLine[]
  height?: number
  units?: string | null
  ariaLabel: string
  shadeUsRecessions?: boolean
  zeroLine?: boolean
  className?: string
}) {
  const active = lines.filter((l) => l.points.length)
  if (active.filter((l) => !l.muted).length > 4)
    return (
      <div className={cn('grid gap-2 @xl:grid-cols-2 @3xl:grid-cols-3', className)}>
        {active.map((l) => (
          <div key={l.name} className="min-w-0 rounded-sm border border-border/60 p-1">
            <div className="truncate px-1 text-xs font-semibold text-fg">{l.name}</div>
            <MacroLines lines={[l]} height={140} units={units} ariaLabel={`${ariaLabel}: ${l.name}`} shadeUsRecessions={shadeUsRecessions} zeroLine={zeroLine} />
          </div>
        ))}
      </div>
    )
  return <LinesChart lines={active} height={height} units={units} ariaLabel={ariaLabel} shade={shadeUsRecessions} zeroLine={zeroLine} className={className} />
}

function LinesChart({ lines, height, units, ariaLabel, shade, zeroLine, className }: { lines: MLine[]; height: number; units?: string | null; ariaLabel: string; shade: boolean; zeroLine: boolean; className?: string }) {
  const t = useChartTokens()
  const from = lines.reduce<string | null>((m, l) => (l.points[0] && (!m || l.points[0].date < m) ? l.points[0].date : m), null)
  const bands = useShading(shade, from)
  const option = useMemo<EChartsOption>(() => {
    let pal = 0
    const series: Record<string, unknown>[] = []
    lines.forEach((l, i) => {
      const color = l.color ?? (l.muted ? t.ref : t.series[pal++ % t.series.length])
      if (l.band) {
        const xs = l.points.map((p) => p.date)
        series.push(
          { type: 'line', name: `${l.band.label} base`, data: xs.map((x, j) => [x, fin(l.band?.lo[j])]), stack: `b${i}`, stackStrategy: 'all', lineStyle: { opacity: 0 }, showSymbol: false, silent: true, tooltip: { show: false } },
          {
            type: 'line',
            name: l.band.label,
            data: xs.map((x, j) => {
              const lo = fin(l.band?.lo[j])
              const hi = fin(l.band?.hi[j])
              return [x, lo !== null && hi !== null ? hi - lo : null]
            }),
            stack: `b${i}`,
            stackStrategy: 'all',
            lineStyle: { opacity: 0 },
            showSymbol: false,
            areaStyle: { color: alpha(color, 0.16) },
            color: alpha(color, 0.4),
            silent: true,
            tooltip: { show: false },
          },
        )
      }
      series.push({
        type: 'line',
        name: l.name,
        data: l.points.map((p) => [p.date, fin(p.value)]),
        showSymbol: l.points.length <= 24,
        symbolSize: 4,
        sampling: 'lttb',
        color,
        lineStyle: { color, width: l.muted ? 1.2 : 1.6, type: l.dashed ? 'dashed' : 'solid' },
        z: l.muted ? 1 : 3,
        ...(i === 0
          ? {
              markArea: bands.length
                ? { silent: true, itemStyle: { color: alpha(t.ref, 0.12) }, data: bands.map((b) => [{ xAxis: b.start }, { xAxis: b.end }]) }
                : undefined,
              markLine: zeroLine ? { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ yAxis: 0 }] } : undefined,
            }
          : {}),
      })
    })
    const names = lines.map((l) => l.name)
    return {
      grid: { left: 8, right: 16, top: names.length > 1 || units ? 30 : 12, bottom: 8, containLabel: true },
      legend: names.length > 1 ? { top: 0, left: 0, data: [...names, ...lines.filter((l) => l.band).map((l) => l.band?.label as string)] } : undefined,
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = (ps as { seriesName: string; data: [string, number | null]; color: string }[]).filter((p) => names.includes(p.seriesName))
          if (!arr.length) return ''
          return ttTitle(fmtDate(String(arr[0].data[0]).slice(0, 10))) + arr.map((p) => ttRow(p.color, p.seriesName, fmtMacro(p.data[1]))).join('') + (units ? `<div style="opacity:.7;margin-top:2px">${units}</div>` : '')
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => fmtMacro(v, 3) }, ...(units && names.length <= 1 ? { name: units.length > 28 ? `${units.slice(0, 27)}…` : units, nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' } } : {}) },
      series,
    } as EChartsOption
  }, [lines, bands, units, zeroLine, t])
  return (
    <div className={className}>
      <EChart option={option} height={height} ariaLabel={ariaLabel} />
      {bands.length > 0 && <p className="mt-0.5 text-2xs text-muted">Shaded: NBER US recessions (official, FRED USREC).</p>}
    </div>
  )
}
