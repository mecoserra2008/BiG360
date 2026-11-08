import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { resolveId } from '@/api/client'
import { USE_MOCK, http } from '@/api/http'
import { type BarInterval, type CPriceSeries, type IntervalChoice, type PriceRange, RANGES, effectiveInterval, hasWeeklyPart, rangeStart, seriesView } from '@/api/prices/range'
import type { PriceBar } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { fmtDate } from '@/lib/format'
import { type PriceChartProps, PriceChart } from './PriceChart'

type Overlays = Pick<PriceChartProps, 'bands' | 'markers' | 'priceLines' | 'overlays' | 'fan' | 'future' | 'badges' | 'futureTooltip' | 'focusBars'>

export function usePriceSeries(kind: 'company' | 'instrument', id: string, range: PriceRange, interval: BarInterval, enabled = true) {
  return useQuery({
    queryKey: ['prices', kind, id, range, interval],
    queryFn: async () => {
      const sid = kind === 'company' ? await resolveId(id) : id
      const base = kind === 'company' ? `/companies/${encodeURIComponent(sid)}/prices` : `/instruments/${encodeURIComponent(sid)}/prices`
      const from = rangeStart(range) ?? '1900-01-01'
      return seriesView(await http.get<CPriceSeries>(base, { from, interval }))
    },
    enabled: enabled && !USE_MOCK,
    staleTime: 10 * 60_000,
    placeholderData: keepPreviousData,
  })
}

/**
 * PriceChart with a range selector (1M … Max) and an interval control (auto: D ≤ 2Y, W ≤ 10Y, M for Max).
 * `initial` bars (already loaded by the page) are shown for `initialRange` with the daily interval, so the default view
 * costs no extra request; overlays (zones, levels, markers, the forward fan / future sessions) only apply to the daily view.
 * `fill`: the chart area takes the remaining height of a parent with a definite height (min `height`).
 */
export function RangePriceChart({
  kind = 'company',
  id,
  initial,
  initialRange = '1Y',
  overlaysLabel,
  ...rest
}: Omit<PriceChartProps, 'bars' | keyof Overlays> &
  Overlays & { kind?: 'company' | 'instrument'; id: string; initial?: PriceBar[]; initialRange?: PriceRange; overlaysLabel?: string }) {
  const [range, setRange] = useState<PriceRange>(initialRange)
  const [choice, setChoice] = useState<IntervalChoice>('auto')
  const interval = effectiveInterval(range, choice)
  const useInitial = !!initial && range === initialRange && interval === 'D'
  const q = usePriceSeries(kind, id, range, interval, !useInitial)
  const startIso = rangeStart(range)
  const bars = useInitial ? (initial ?? []).filter((x) => !startIso || x.time >= startIso) : (q.data?.bars ?? [])
  const served = useInitial ? 'D' : (q.data?.interval ?? interval)
  const daily = served === 'D'
  const { bands, markers, priceLines, overlays, fan, future, badges, futureTooltip, focusBars, ...chart } = rest
  const hasOverlays = !!(bands?.length || markers?.length || priceLines?.length || overlays?.length || fan)
  const weeklyPart = !useInitial && q.data ? hasWeeklyPart(served, rangeStart(range), q.data.dailyFrom) : false
  const fill = !!chart.fill
  const body =
    !useInitial && q.isLoading ? (
      <Skeleton style={{ height: fill ? '100%' : (chart.height ?? 320) }} />
    ) : !useInitial && q.isError ? (
      <p className="py-8 text-center text-xs text-muted">Could not load prices for this range.</p>
    ) : (
      <PriceChart
        {...chart}
        bars={bars}
        {...(daily ? { bands, markers: clipMarkers(markers, bars), priceLines, overlays: clipOverlays(overlays, bars), fan, future, badges, futureTooltip, focusBars } : {})}
      />
    )
  return (
    <div className={fill ? 'flex h-full min-h-0 flex-col' : undefined}>
      <div className="mb-1.5 flex flex-wrap items-center gap-2 text-xs">
        <Segmented label="Price range" size="xs" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r }))} />
        <Segmented
          label="Bar interval"
          size="xs"
          value={choice}
          onChange={setChoice}
          options={[
            { value: 'auto', label: `Auto (${effectiveInterval(range, 'auto')})` },
            { value: 'D', label: 'D' },
            { value: 'W', label: 'W' },
            { value: 'M', label: 'M' },
          ]}
        />
        {served !== interval && !useInitial && <Badge tone="warn">{served === 'W' ? 'weekly (daily capped at 10y)' : served}</Badge>}
        <span className="ml-auto text-2xs text-muted">
          {q.data?.firstDate ? `History from ${fmtDate(q.data.firstDate)}` : ''}
          {weeklyPart && q.data?.dailyFrom ? ` · weekly bars before ${fmtDate(q.data.dailyFrom)}` : ''}
          {hasOverlays && !daily ? ` · ${overlaysLabel ?? 'overlays'} shown on the daily view only` : ''}
        </span>
      </div>
      {fill ? (
        <div className="relative min-h-0 flex-1" style={{ minHeight: chart.height ?? 320 }}>
          <div className="absolute inset-0">{body}</div>
        </div>
      ) : (
        body
      )}
    </div>
  )
}

/** Overlay lines / markers computed on a longer series (e.g. MAs on ~2y of bars) are clipped to the bars on screen, so
 * the time axis is not stretched beyond the candles with orphan line segments. */
function clipOverlays(overlays: Overlays['overlays'], bars: PriceBar[]): Overlays['overlays'] {
  if (!overlays || !bars.length) return overlays
  const lo = bars[0].time
  const hi = bars[bars.length - 1].time
  return overlays.map((o) => ({ ...o, data: o.data.filter((p) => p.time >= lo && (o.future || p.time <= hi)) }))
}

function clipMarkers(markers: Overlays['markers'], bars: PriceBar[]): Overlays['markers'] {
  if (!markers || !bars.length) return markers
  const lo = bars[0].time
  const hi = bars[bars.length - 1].time
  return markers.filter((m) => m.time >= lo && m.time <= hi)
}
