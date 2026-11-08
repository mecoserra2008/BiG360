import { useEffect, useMemo, useState } from 'react'
import { useEmHotspots, useEvMap } from '@/api/events/hooks'
import { EV_RANGES, type EmHotspotItem, type EvMap, type EvRange, type EvTaxonomy, HOTSPOT_LABEL, HOTSPOT_LEVEL, type MapMetric, emergingLabel, metricValue } from '@/api/events/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, divergingColors, useChartTokens } from '@/components/charts/tokens'
import { countryCenter, ensureWorldMap } from '@/components/charts/worldMap'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { RangeSlider, Segmented, Switch } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate, fmtInt } from '@/lib/format'
import { ModelBadge, ThemeMultiSelect } from './shared'

export interface MapWindow {
  start?: string
  end?: string
  days?: number
  /** slider span (days) and its bucket */
  sliderDays?: number
  sliderFreq?: 'day' | 'week'
}

const METRICS: { value: MapMetric; label: string }[] = [
  { value: 'intensity', label: 'Intensity' },
  { value: 'attention_z', label: 'Attention z' },
  { value: 'tone', label: 'Tone' },
  { value: 'conflict_share', label: 'Conflict share' },
]
const HOT_MEASURE: Record<MapMetric, string> = {
  intensity: 'intensity',
  attention_z: 'attention',
  tone: 'tone',
  conflict_share: 'attention',
}
const EMERGING_SYMBOL: Record<string, string> = {
  new: 'triangle',
  consecutive: 'rect',
  intensifying: 'arrow',
  persistent: 'circle',
  diminishing: 'diamond',
  sporadic: 'pin',
  oscillating: 'roundRect',
  historical: 'emptyCircle',
}

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const fmtMetric = (m: MapMetric, v: number | null) => (v === null ? DASH : m === 'intensity' || m === 'conflict_share' ? `${(v * 100).toFixed(0)}%` : v.toFixed(2))

export function WorldMapPanel({
  tax,
  themes,
  onThemes,
  win,
  onWin,
  country,
  onCountry,
  span,
  onSpan,
}: {
  span: EvRange
  onSpan: (r: EvRange) => void
  tax: EvTaxonomy
  themes: string[]
  onThemes: (t: string[]) => void
  win: MapWindow
  onWin: (w: MapWindow) => void
  country: string | null
  onCountry: (c: string | null) => void
}) {
  const [metric, setMetric] = useState<MapMetric>('intensity')
  const [overlay, setOverlay] = useState(false)
  const q = useEvMap({ ...win, themes })
  const hotTheme = themes.length === 1 ? themes[0] : 'all'
  const hs = useEmHotspots(hotTheme, HOT_MEASURE[metric], overlay)
  return (
    <Panel
      title="World map"
      subtitle={
        <span>
          GDELT coverage by country · {q.data ? `${fmtDate(q.data.start)} → ${fmtDate(q.data.end)}` : '…'} · {themes.length ? `${themes.length} theme${themes.length > 1 ? 's' : ''}` : 'all themes'} · click a country to filter stories
        </span>
      }
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Segmented label="History range" size="xs" value={span} onChange={onSpan} options={EV_RANGES.map((r) => ({ value: r, label: r }))} />
          <ThemeMultiSelect tax={tax} value={themes} onChange={onThemes} />
          <Segmented label="Map metric" size="xs" value={metric} onChange={setMetric} options={METRICS} />
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <Switch label="Hotspot overlay" checked={overlay} onCheckedChange={setOverlay} /> Hotspots <ModelBadge text="model" />
          </label>
        </div>
      }
      bodyClassName="p-1.5"
    >
      <QueryView query={q} skeleton={<Skeleton className="h-[460px]" />}>
        {(d) => (
          <MapBody
            d={d}
            metric={metric}
            hot={overlay ? (hs.data?.items ?? null) : null}
            hotMeta={
              overlay && hs.data
                ? `${hs.data.theme === 'all' ? 'all themes' : (tax.themes.find((t) => t.id === hs.data?.theme)?.name ?? hs.data.theme)} · ${hs.data.measure} · week of ${fmtDate(hs.data.week)} · global Moran's I ${hs.data.global_moran?.moran_i?.toFixed(3) ?? '—'} (p ${hs.data.global_moran?.p_value ?? '—'}) · W ${hs.data.w_kind}`
                : null
            }
            hotLoading={overlay && hs.isLoading}
            win={win}
            onWin={onWin}
            country={country}
            onCountry={onCountry}
          />
        )}
      </QueryView>
    </Panel>
  )
}

function MapBody({
  d,
  metric,
  hot,
  hotMeta,
  hotLoading,
  win,
  onWin,
  country,
  onCountry,
}: {
  d: EvMap
  metric: MapMetric
  hot: EmHotspotItem[] | null
  hotMeta: string | null
  hotLoading: boolean
  win: MapWindow
  onWin: (w: MapWindow) => void
  country: string | null
  onCountry: (c: string | null) => void
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
  const byCode = useMemo(() => new Map(d.countries.map((c) => [c.country, c])), [d.countries])
  const hotBy = useMemo(() => new Map((hot ?? []).map((h) => [h.iso2, h])), [hot])
  const allNull = d.countries.every((c) => metricValue(c, metric) === null)

  const option = useMemo<EChartsOption>(() => {
    const vals = d.countries.map((c) => metricValue(c, metric)).filter(fin)
    const absMax = Math.max(1, ...vals.map((v) => Math.abs(v)))
    const seq = [alpha(t.accent, 0.1), alpha(t.accent, 0.45), t.accent]
    const visualMap = hot
      ? {
          type: 'piecewise',
          left: 8,
          bottom: 8,
          itemWidth: 12,
          itemHeight: 10,
          textStyle: { color: t.muted, fontSize: 10 },
          pieces: [
            { value: 3, label: HOTSPOT_LABEL.hot99, color: t.down },
            {
              value: 2,
              label: HOTSPOT_LABEL.hot95,
              color: alpha(t.down, 0.65),
            },
            {
              value: 1,
              label: HOTSPOT_LABEL.hot90,
              color: alpha(t.down, 0.35),
            },
            { value: 0, label: HOTSPOT_LABEL.ns, color: t.surface3 },
            {
              value: -1,
              label: HOTSPOT_LABEL.cold90,
              color: alpha(t.series[1], 0.35),
            },
            {
              value: -2,
              label: HOTSPOT_LABEL.cold95,
              color: alpha(t.series[1], 0.65),
            },
            { value: -3, label: HOTSPOT_LABEL.cold99, color: t.series[1] },
          ],
          seriesIndex: 0,
        }
      : metric === 'tone' || metric === 'attention_z'
        ? {
            type: 'continuous',
            min: metric === 'tone' ? -Math.min(8, absMax) : -Math.min(5, absMax),
            max: metric === 'tone' ? Math.min(8, absMax) : Math.min(5, absMax),
            left: 8,
            bottom: 8,
            itemHeight: 110,
            itemWidth: 10,
            text: metric === 'tone' ? ['+ tone', '− tone'] : ['+ z', '− z'],
            calculable: false,
            inRange: {
              color: metric === 'tone' ? divergingColors(t) : [...divergingColors(t)].reverse(),
            },
            textStyle: { color: t.muted, fontSize: 10 },
            seriesIndex: 0,
          }
        : {
            type: 'continuous',
            min: vals.length ? Math.min(...vals) : 0,
            max: vals.length ? Math.max(...vals) : 1,
            left: 8,
            bottom: 8,
            itemHeight: 110,
            itemWidth: 10,
            text: ['high', 'low'],
            calculable: false,
            inRange: { color: seq },
            textStyle: { color: t.muted, fontSize: 10 },
            seriesIndex: 0,
          }
    const emerging = new Map<string, { name: string; value: [number, number]; hot: boolean; iso: string }[]>()
    for (const h of hot ?? []) {
      if (!h.emerging_class || h.emerging_class === 'none') continue
      const c = countryCenter(h.iso2)
      if (!c) continue
      const kind = h.emerging_class.replace(/_(hot|cold)$/, '')
      const arr = emerging.get(kind) ?? []
      arr.push({
        name: h.iso2,
        value: c,
        hot: h.emerging_class.endsWith('_hot'),
        iso: h.iso2,
      })
      emerging.set(kind, arr)
    }
    return {
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const x = p as { name: string; seriesType: string }
          const c = byCode.get(x.name)
          const h = hotBy.get(x.name)
          if (!c && !h) return `${ttTitle(x.name)}<div style="color:${t.muted}">No coverage</div>`
          let s = ttTitle(c?.name ?? h?.name ?? x.name)
          if (c) {
            s += ttRow(t.accent, 'Intensity', fmtMetric('intensity', c.intensity ?? null))
            s += ttRow(t.fg, 'Attention z (max)', fin(c.attention_z) ? c.attention_z.toFixed(2) : 'n/a')
            s += ttRow(t.fg, 'Tone', fin(c.tone) ? c.tone.toFixed(2) : DASH)
            s += ttRow(t.fg, 'Conflict share', fmtMetric('conflict_share', c.conflict_share ?? null))
            s += ttRow(t.ref, 'Events / articles', `${fmtInt(c.events)} / ${fmtInt(c.articles)}`)
          }
          if (h) {
            s += ttRow(t.down, 'Gi* (model)', `${HOTSPOT_LABEL[h.hotspot] ?? h.hotspot} · z ${fin(h.gi_z) ? h.gi_z.toFixed(2) : DASH}`)
            s += ttRow(t.ref, 'LISA', `${h.lisa_quadrant} · p ${fin(h.lisa_p) ? h.lisa_p.toFixed(3) : DASH}`)
            s += ttRow(t.ref, 'Emerging', emergingLabel(h.emerging_class))
          }
          return s
        },
      },
      visualMap,
      geo: {
        map: 'world',
        roam: true,
        zoom: 1.15,
        center: [10, 18],
        scaleLimit: { min: 1, max: 8 },
        silent: true,
        itemStyle: { areaColor: 'transparent', borderColor: 'transparent' },
      },
      series: [
        {
          type: 'map',
          map: 'world',
          geoIndex: 0,
          data: hot
            ? hot.map((h) => ({
                name: h.iso2,
                value: HOTSPOT_LEVEL[h.hotspot] ?? 0,
              }))
            : d.countries.map((c) => ({
                name: c.country,
                value: metricValue(c, metric) ?? undefined,
              })),
          selectedMode: 'single',
        },
        ...[...emerging.entries()].map(([kind, pts]) => ({
          type: 'scatter' as const,
          name: emergingLabel(`${kind}_hot`).replace(' hot spot', ''),
          coordinateSystem: 'geo',
          symbol: EMERGING_SYMBOL[kind] ?? 'circle',
          itemStyle: { color: t.muted },
          symbolSize: 9,
          z: 5,
          data: pts.map((p) => ({
            name: p.name,
            value: p.value,
            itemStyle: {
              color: p.hot ? t.down : t.series[1],
              borderColor: t.surface,
              borderWidth: 1,
            },
          })),
        })),
      ],
      legend: emerging.size
        ? {
            right: 8,
            bottom: 8,
            orient: 'vertical',
            itemWidth: 10,
            itemHeight: 10,
            textStyle: { color: t.muted, fontSize: 10 },
            data: [...emerging.keys()].map((k) => emergingLabel(`${k}_hot`).replace(' hot spot', '')),
          }
        : undefined,
    } as EChartsOption
  }, [d.countries, metric, t, byCode, hot, hotBy])

  // geo + map share the geo component; area styling lives on the geo regions via the map series
  const geoStyled = useMemo<EChartsOption>(() => {
    const o = option as Record<string, unknown>
    return {
      ...o,
      geo: {
        ...(o.geo as object),
        itemStyle: {
          areaColor: t.surface3,
          borderColor: t.surface,
          borderWidth: 0.6,
        },
        emphasis: {
          label: { show: false },
          itemStyle: { areaColor: t.borderStrong },
        },
        select: {
          label: { show: false },
          itemStyle: { borderColor: t.accent, borderWidth: 1.5 },
        },
        silent: false,
      },
    } as EChartsOption
  }, [option, t])

  return (
    <div>
      {ready ? (
        <EChart
          option={geoStyled}
          height={440}
          ariaLabel={`World map of ${metric.replace('_', ' ')}${hot ? ' with hotspot classes' : ''}`}
          onEvents={{
            click: (p: unknown) => onCountry((p as { name?: string }).name ? ((p as { name: string }).name === country ? null : (p as { name: string }).name) : null),
          }}
        />
      ) : (
        <Skeleton className="h-[440px]" />
      )}
      {(allNull || hotMeta || hotLoading) && (
        <div className="flex flex-wrap items-center gap-2 px-2 pb-1 text-2xs text-muted">
          {allNull && !hot && <Badge tone="warn">no data</Badge>}
          {allNull && !hot && <span>{metric === 'attention_z' ? 'Attention z is not available yet for this window (the z baseline needs 90 days of history).' : 'No values for this metric.'}</span>}
          {hotLoading && <span>Loading hotspots…</span>}
          {hotMeta && (
            <>
              <ModelBadge /> <span>Getis-Ord Gi* classes (fill) and emerging-hotspot categories (markers): {hotMeta}</span>
            </>
          )}
        </div>
      )}
      <TimeSlider d={d} win={win} onWin={onWin} />
    </div>
  )
}

/** Daily (weekly beyond a year) totals over the selected history range with a two-thumb window selector. */
function TimeSlider({ d, win, onWin }: { d: EvMap; win: MapWindow; onWin: (w: MapWindow) => void }) {
  const t = useChartTokens()
  const dates = d.timeline.map((p) => p.date)
  const n = dates.length
  const weekly = d.timeline_freq === 'week'
  // bucket containing a date (weekly buckets are labelled by their Monday)
  const idx = (dt: string | undefined, fb: number) => {
    if (!dt) return fb
    if (n && dt < dates[0]) return 0
    let i = -1
    for (let k = 0; k < n; k++) if (dates[k] <= dt) i = k
    return i < 0 ? fb : i
  }
  const addDays = (iso: string, k: number) => new Date(Date.parse(iso) + k * 864e5).toISOString().slice(0, 10)
  const endOf = (k: number) => (k === n - 1 ? d.end : weekly ? addDays(dates[k], 6) : dates[k])
  const curEnd = idx(d.end, n - 1)
  const curStart = idx(d.start, Math.max(0, curEnd - 6))
  const [range, setRange] = useState<[number, number] | null>(null)
  const [a, b] = range ?? [curStart, curEnd]
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 4, bottom: 18, containLabel: false },
      tooltip: { trigger: 'axis', valueFormatter: (v) => fmtInt(v as number) },
      xAxis: {
        type: 'category',
        data: dates,
        axisLabel: {
          formatter: (v: string) => (weekly ? v.slice(0, 7) : v.slice(5)),
          color: t.muted,
          fontSize: 9,
        },
        axisTick: { show: false },
      },
      yAxis: { type: 'value', show: false },
      series: [
        {
          name: 'articles',
          type: 'bar',
          data: d.timeline.map((p, i) => ({
            value: p.articles,
            itemStyle: {
              color: i >= a && i <= b ? t.accent : alpha(t.ref, 0.45),
            },
          })),
          barCategoryGap: '15%',
        },
      ],
    }),
    [d.timeline, dates, a, b, t, weekly],
  )
  if (n < 2) return null
  return (
    <div className="px-2 pt-1">
      <EChart option={option} height={64} ariaLabel={`${weekly ? 'Weekly' : 'Daily'} articles (selected window highlighted)`} />
      <div className="flex items-center gap-3 px-1">
        <RangeSlider
          labels={['Window start', 'Window end']}
          min={0}
          max={n - 1}
          step={1}
          value={[a, b]}
          onValueChange={(v) => setRange([v[0], v[1]])}
          onValueCommit={(v) => {
            setRange(null)
            onWin({ ...win, start: dates[v[0]], end: endOf(v[1]), days: undefined })
          }}
        />
        <span className="num shrink-0 text-xs text-fg">
          {fmtDate(dates[a])} → {fmtDate(endOf(b))} · {Math.round((Date.parse(endOf(b)) - Date.parse(dates[a])) / 864e5) + 1}d{weekly ? ' · weekly bars' : ''}
        </span>
      </div>
    </div>
  )
}
