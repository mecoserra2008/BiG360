import { useEffect, useMemo, useState } from 'react'
import { HOTSPOT_TEXT } from '@/api/supplychain/adapters'
import type { MapOverlay, ScOverlay } from '@/api/supplychain/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { ensureWorldMap } from '@/components/charts/worldMap'
import { Skeleton } from '@/components/ui/feedback'
import { DASH, fmtNum, fmtPct } from '@/lib/format'
import { countryName } from '@/lib/regions'

export interface MapRow {
  iso2: string
  share: number | null
  overlay?: ScOverlay | null
}

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/**
 * World choropleth of revenue exposure or a country overlay (the bundled Natural Earth map of Global Events). Countries
 * without exposure are drawn neutral; the tooltip always shows exposure + every overlay component.
 */
export function ExposureMap({
  rows,
  data,
  overlay,
  height = 380,
  ariaLabel,
  onCountry,
}: {
  rows: MapRow[]
  data: { name: string; value: number }[]
  overlay: MapOverlay
  height?: number
  ariaLabel: string
  onCountry?: (iso2: string) => void
}) {
  const t = useChartTokens()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    let alive = true
    void ensureWorldMap().then(() => alive && setReady(true))
    return () => {
      alive = false
    }
  }, [])
  const byIso = useMemo(() => new Map(rows.map((r) => [r.iso2, r])), [rows])
  const option = useMemo<EChartsOption>(() => {
    const vals = data.map((d) => d.value).filter(fin)
    const max = overlay === 'risk_score' ? 100 : overlay === 'recession_prob' || overlay === 'p_escalation_30d' ? 1 : Math.max(1e-6, ...vals)
    const min = overlay === 'event_pressure' ? Math.min(0, ...vals) : 0
    const ramp = overlay === 'exposure' ? [alpha(t.accent, 0.12), alpha(t.accent, 0.5), t.accent] : [alpha(t.warn, 0.15), alpha(t.down, 0.55), t.down]
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const x = p as { name: string }
          const r = byIso.get(x.name)
          let s = ttTitle(`${countryName(x.name)} (${x.name})`)
          s += ttRow(t.accent, 'Revenue exposure', r && fin(r.share) ? fmtPct(r.share, 1) : DASH)
          const o = r?.overlay
          if (o) {
            s += ttRow(t.down, 'Country risk (model)', fin(o.risk_score) ? fmtNum(o.risk_score, 0) : DASH)
            s += ttRow(t.fg, 'Event pressure', fin(o.event_pressure) ? fmtNum(o.event_pressure, 2) : DASH)
            s += ttRow(t.fg, 'Escalation 30d', fin(o.p_escalation_30d) ? fmtPct(o.p_escalation_30d, 0) : DASH)
            s += ttRow(t.fg, 'Hotspot', o.hotspot ? (HOTSPOT_TEXT[o.hotspot] ?? o.hotspot) : DASH)
            s += ttRow(t.fg, 'Recession prob.', fin(o.recession_prob) ? `${fmtPct(o.recession_prob, 0)}${o.recession_official ? ' (official)' : ''}` : DASH)
            s += ttRow(t.fg, 'Macro surprise', fin(o.surprise_index) ? fmtNum(o.surprise_index, 0) : DASH)
            s += ttRow(t.ref, 'Cycle', o.cycle_quadrant ?? DASH)
          } else if (!r) {
            s += `<div style="color:${t.muted}">No exposure</div>`
          }
          return s
        },
      },
      visualMap: {
        type: 'continuous',
        min,
        max: Math.max(max, min + 1e-6),
        left: 8,
        bottom: 8,
        itemHeight: 100,
        itemWidth: 10,
        text: ['high', 'low'],
        calculable: false,
        inRange: { color: ramp },
        textStyle: { color: t.muted, fontSize: 10 },
        seriesIndex: 0,
      },
      geo: {
        map: 'world',
        roam: true,
        zoom: 1.15,
        center: [10, 18],
        scaleLimit: { min: 1, max: 8 },
        itemStyle: { areaColor: t.surface3, borderColor: t.surface, borderWidth: 0.6 },
        emphasis: { label: { show: false }, itemStyle: { areaColor: t.borderStrong } },
        select: { disabled: true },
      },
      series: [{ type: 'map', map: 'world', geoIndex: 0, data }],
    } as EChartsOption
  }, [data, overlay, t, byIso])
  if (!ready) return <Skeleton style={{ height }} />
  return (
    <EChart
      option={option}
      height={height}
      ariaLabel={ariaLabel}
      onEvents={onCountry ? { click: (p: unknown) => (p as { name?: string }).name && onCountry((p as { name: string }).name) } : undefined}
    />
  )
}
