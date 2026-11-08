import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type DeepPartial,
  type IChartApi,
  type IChartApiBase,
  type IPriceLine,
  type IPrimitivePaneRenderer,
  type IPrimitivePaneView,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type ISeriesPrimitive,
  type ChartOptions,
  type SeriesAttachedParameter,
  type SeriesMarker,
  type SeriesType,
  type Time,
  type AutoscaleInfo,
  type WhitespaceData,
} from 'lightweight-charts'
import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { PriceBar } from '@/api/types'
import { getFormatLocale } from '@/lib/format'
import { cn } from '@/lib/utils'
import { type ChartTokens, alpha, useChartTokens } from './tokens'

/* ───────────── shaded horizontal bands (confluence zones) as a series primitive ───────────── */

export interface Band {
  low: number
  high: number
  color: string
  /** 0–1 fill opacity */
  opacity: number
  label?: string
}

type DrawTarget = Parameters<IPrimitivePaneRenderer['draw']>[0]

class BandsRenderer implements IPrimitivePaneRenderer {
  private readonly src: BandsPrimitive
  constructor(src: BandsPrimitive) {
    this.src = src
  }
  drawBackground(target: DrawTarget): void {
    const series = this.src.series
    if (!series) return
    target.useBitmapCoordinateSpace((scope) => {
      const ctx = scope.context
      const vpr = scope.verticalPixelRatio
      for (const b of this.src.bands) {
        const y1 = series.priceToCoordinate(b.high)
        const y2 = series.priceToCoordinate(b.low)
        if (y1 === null || y2 === null) continue
        const top = Math.round(Math.min(y1, y2) * vpr)
        const h = Math.max(1, Math.round(Math.abs(y2 - y1) * vpr))
        ctx.fillStyle = alpha(b.color, b.opacity)
        ctx.fillRect(0, top, scope.bitmapSize.width, h)
        ctx.fillStyle = alpha(b.color, Math.min(1, b.opacity + 0.35))
        ctx.fillRect(0, top, scope.bitmapSize.width, Math.max(1, Math.round(vpr)))
        ctx.fillRect(0, top + h - Math.max(1, Math.round(vpr)), scope.bitmapSize.width, Math.max(1, Math.round(vpr)))
      }
    })
  }
  draw(target: DrawTarget): void {
    const series = this.src.series
    if (!series) return
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context
      ctx.font = `600 10px ${this.src.font}`
      ctx.textBaseline = 'middle'
      // place labels top→bottom, skipping any that would collide with the previous one
      const placed: { y: number; b: Band }[] = []
      for (const b of this.src.bands) {
        if (!b.label) continue
        const y1 = series.priceToCoordinate(b.high)
        const y2 = series.priceToCoordinate(b.low)
        if (y1 === null || y2 === null) continue
        const y = (y1 + y2) / 2
        if (y < 7 || y > scope.mediaSize.height - 7) continue
        placed.push({ y, b })
      }
      placed.sort((a, b) => a.y - b.y)
      let lastY = -Infinity
      for (const { y, b } of placed) {
        if (y - lastY < 15) continue
        lastY = y
        const label = b.label as string
        const w = ctx.measureText(label).width + 8
        ctx.fillStyle = alpha(b.color, 0.85)
        ctx.fillRect(4, y - 7, w, 14)
        ctx.fillStyle = this.src.labelFg
        ctx.fillText(label, 8, y + 0.5)
      }
    })
  }
}

class BandsPaneView implements IPrimitivePaneView {
  private readonly r: BandsRenderer
  constructor(src: BandsPrimitive) {
    this.r = new BandsRenderer(src)
  }
  zOrder(): 'bottom' {
    return 'bottom'
  }
  renderer(): IPrimitivePaneRenderer {
    return this.r
  }
}

class BandsPrimitive implements ISeriesPrimitive<Time> {
  series: ISeriesApi<SeriesType> | null = null
  bands: Band[] = []
  font = 'Inter'
  labelFg = '#000'
  private requestUpdate: (() => void) | null = null
  private readonly views: BandsPaneView[]
  constructor() {
    this.views = [new BandsPaneView(this)]
  }
  attached(p: SeriesAttachedParameter<Time>): void {
    this.series = p.series
    this.requestUpdate = p.requestUpdate
  }
  detached(): void {
    this.series = null
    this.requestUpdate = null
  }
  paneViews(): readonly IPrimitivePaneView[] {
    return this.views
  }
  set(bands: Band[], font: string, labelFg: string): void {
    this.bands = bands
    this.font = font
    this.labelFg = labelFg
    this.requestUpdate?.()
  }
}

/* ───────────── forward fan (calibrated range at every horizon) + earnings marker + right-edge badges ───────────── */

type FanLevel = 80 | 90 | 95

export interface ChartFan {
  /** origin = last close at the last bar's time */
  origin: { time: string; value: number }
  /** one point per future session (times strictly increasing, all after the origin) */
  points: { time: string; lo: Record<FanLevel, number>; hi: Record<FanLevel, number> }[]
  /** fill colour (hex); 95 / 90 / 80 drawn with increasing opacity */
  color: string
  /** vertical marker inside the window (e.g. the expected earnings date) */
  marker?: { time: string; label: string; color: string } | null
}

export interface ChartBadge {
  price: number
  text: string
  color: string
}

const FAN_ALPHA: [FanLevel, number][] = [
  [95, 0.1],
  [90, 0.16],
  [80, 0.26],
]

class FanRenderer implements IPrimitivePaneRenderer {
  private readonly src: FanPrimitive
  constructor(src: FanPrimitive) {
    this.src = src
  }
  private geom() {
    const { series, chart, fan } = this.src
    if (!series || !chart || !fan || !fan.points.length) return null
    const ts = chart.timeScale()
    const x0 = ts.timeToCoordinate(fan.origin.time as Time)
    const y0 = series.priceToCoordinate(fan.origin.value)
    if (x0 === null || y0 === null) return null
    const pts: { x: number; p: ChartFan['points'][number] }[] = []
    for (const p of fan.points) {
      const x = ts.timeToCoordinate(p.time as Time)
      if (x !== null) pts.push({ x, p })
    }
    return pts.length ? { x0, y0, pts, series, fan, ts } : null
  }
  drawBackground(target: DrawTarget): void {
    const g = this.geom()
    if (!g) return
    const { x0, y0, pts, series, fan } = g
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context
      const y = (v: number) => series.priceToCoordinate(v) ?? y0
      for (const [lvl, a] of FAN_ALPHA) {
        ctx.beginPath()
        ctx.moveTo(x0, y0)
        for (const q of pts) ctx.lineTo(q.x, y(q.p.hi[lvl]))
        for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(pts[i].x, y(pts[i].p.lo[lvl]))
        ctx.closePath()
        ctx.fillStyle = alpha(fan.color, a)
        ctx.fill()
      }
      // outer (95%) edges, hairline
      ctx.strokeStyle = alpha(fan.color, 0.55)
      ctx.lineWidth = 1
      for (const edge of ['hi', 'lo'] as const) {
        ctx.beginPath()
        ctx.moveTo(x0, y0)
        for (const q of pts) ctx.lineTo(q.x, y(q.p[edge][95]))
        ctx.stroke()
      }
      // flat dashed centre: no drift, no direction call
      ctx.setLineDash([4, 3])
      ctx.strokeStyle = alpha(fan.color, 0.95)
      ctx.beginPath()
      ctx.moveTo(x0, y0)
      ctx.lineTo(pts[pts.length - 1].x, y0)
      ctx.stroke()
      ctx.setLineDash([])
    })
  }
  draw(target: DrawTarget): void {
    const g = this.geom()
    const { series } = this.src
    if (!series) return
    target.useMediaCoordinateSpace((scope) => {
      const ctx = scope.context
      ctx.font = `600 10px ${this.src.font}`
      ctx.textBaseline = 'middle'
      // earnings (or other) vertical marker
      const m = g?.fan.marker
      if (g && m) {
        const x = g.ts.timeToCoordinate(m.time as Time)
        if (x !== null) {
          ctx.strokeStyle = alpha(m.color, 0.9)
          ctx.lineWidth = 1
          ctx.setLineDash([3, 3])
          ctx.beginPath()
          ctx.moveTo(x, 16)
          ctx.lineTo(x, scope.mediaSize.height)
          ctx.stroke()
          ctx.setLineDash([])
          const w = ctx.measureText(m.label).width + 8
          const lx = Math.min(Math.max(2, x - w / 2), scope.mediaSize.width - w - 2)
          ctx.fillStyle = alpha(m.color, 0.95)
          ctx.fillRect(lx, 2, w, 14)
          ctx.fillStyle = this.src.labelFg
          ctx.fillText(m.label, lx + 4, 9.5)
        }
      }
      // right-edge badges (e.g. zone touch probabilities), de-collided top -> bottom
      const placed: { y: number; b: ChartBadge }[] = []
      for (const b of this.src.badges) {
        const yy = series.priceToCoordinate(b.price)
        if (yy === null || yy < 20 || yy > scope.mediaSize.height - 8) continue
        placed.push({ y: yy, b })
      }
      placed.sort((a, b) => a.y - b.y)
      let lastY = -Infinity
      for (const { y, b } of placed) {
        const yy = Math.max(y, lastY + 15)
        if (yy > scope.mediaSize.height - 8) break
        lastY = yy
        const w = ctx.measureText(b.text).width + 8
        const x = scope.mediaSize.width - w - 40 // clear of the price-line titles (T1, Entry …) at the pane edge
        ctx.fillStyle = this.src.badgeBg
        ctx.fillRect(x, yy - 7, w, 14)
        ctx.strokeStyle = alpha(b.color, 0.9)
        ctx.lineWidth = 1
        ctx.strokeRect(x + 0.5, yy - 6.5, w - 1, 13)
        ctx.fillStyle = b.color
        ctx.fillText(b.text, x + 4, yy + 0.5)
      }
    })
  }
}

class FanPaneView implements IPrimitivePaneView {
  private readonly r: FanRenderer
  constructor(src: FanPrimitive) {
    this.r = new FanRenderer(src)
  }
  zOrder(): 'normal' {
    return 'normal'
  }
  renderer(): IPrimitivePaneRenderer {
    return this.r
  }
}

class FanPrimitive implements ISeriesPrimitive<Time> {
  series: ISeriesApi<SeriesType> | null = null
  chart: IChartApiBase<Time> | null = null
  fan: ChartFan | null = null
  badges: ChartBadge[] = []
  font = 'Inter'
  labelFg = '#000'
  badgeBg = '#000'
  private requestUpdate: (() => void) | null = null
  private readonly views: FanPaneView[]
  constructor() {
    this.views = [new FanPaneView(this)]
  }
  attached(p: SeriesAttachedParameter<Time>): void {
    this.series = p.series
    this.chart = p.chart
    this.requestUpdate = p.requestUpdate
  }
  detached(): void {
    this.series = null
    this.chart = null
    this.requestUpdate = null
  }
  paneViews(): readonly IPrimitivePaneView[] {
    return this.views
  }
  /** keep the whole 95% fan inside the price scale */
  autoscaleInfo(): AutoscaleInfo | null {
    const f = this.fan
    if (!f || !f.points.length) return null
    let lo = f.origin.value
    let hi = f.origin.value
    for (const p of f.points) {
      lo = Math.min(lo, p.lo[95])
      hi = Math.max(hi, p.hi[95])
    }
    return { priceRange: { minValue: lo, maxValue: hi } }
  }
  set(fan: ChartFan | null, badges: ChartBadge[], font: string, labelFg: string, badgeBg: string): void {
    this.fan = fan
    this.badges = badges
    this.font = font
    this.labelFg = labelFg
    this.badgeBg = badgeBg
    this.requestUpdate?.()
  }
}

/** lightweight-charts hands back the Time it was given (ISO string here), or a BusinessDay / UTC timestamp. */
function isoOf(t: Time | undefined): string | null {
  if (t == null) return null
  if (typeof t === 'string') return t
  if (typeof t === 'number') return new Date(t * 1000).toISOString().slice(0, 10)
  return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`
}

/* ───────────── component ───────────── */

export interface ChartMarker {
  time: string
  position: 'aboveBar' | 'belowBar' | 'inBar'
  color: string
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square'
  text?: string
}

export interface ChartPriceLine {
  price: number
  color: string
  title: string
  dashed?: boolean
}

export interface ChartOverlay {
  id: string
  data: { time: string; value: number }[]
  color: string
  width?: 1 | 2 | 3
  dashed?: boolean
  /** put on an independent left scale (e.g. sentiment index vs price) */
  separateScale?: boolean
  /** forward-looking overlay (e.g. the calibrated-range cone): points after the last candle are kept */
  future?: boolean
}

export interface PriceChartProps {
  bars: PriceBar[]
  mode?: 'candles' | 'area' | 'line'
  height?: number
  showVolume?: boolean
  bands?: Band[]
  markers?: ChartMarker[]
  priceLines?: ChartPriceLine[]
  overlays?: ChartOverlay[]
  ariaLabel: string
  className?: string
  /** colour of area/line mode (defaults to accent) */
  lineColor?: string
  /** trading currency → price precision (JPY/KRW/CLP: 0 decimals) */
  currency?: string
  /** future session dates (after the last bar) appended as whitespace so the time axis extends into the future */
  future?: string[]
  /** forward fan drawn from the last close over the future sessions */
  fan?: ChartFan | null
  /** small labels at the right edge of the pane at given prices */
  badges?: ChartBadge[]
  /** tooltip text for a future date under the crosshair (null = none) */
  futureTooltip?: (time: string) => string | null
  /** fill the parent's height (the parent must have a definite height) instead of `height` */
  fill?: boolean
  /** initial view: the last N bars plus the future sessions (default: fit everything) */
  focusBars?: number
}

function chartOptions(t: ChartTokens): DeepPartial<ChartOptions> {
  return {
    // follow the app's number locale rather than the browser's
    localization: { locale: getFormatLocale() },
    layout: { background: { type: ColorType.Solid, color: 'transparent' }, textColor: t.muted, fontFamily: t.fontSans, fontSize: 10 },
    grid: { vertLines: { color: t.grid }, horzLines: { color: t.grid } },
    rightPriceScale: { borderColor: t.border, scaleMargins: { top: 0.08, bottom: 0.08 } },
    leftPriceScale: { borderColor: t.border },
    timeScale: { borderColor: t.border, rightOffset: 4, minBarSpacing: 0.5 },
    crosshair: {
      mode: CrosshairMode.Normal,
      vertLine: { color: t.borderStrong, labelBackgroundColor: t.surface3 },
      horzLine: { color: t.borderStrong, labelBackgroundColor: t.surface3 },
    },
  }
}

const ZERO_DEC = new Set(['JPY', 'KRW', 'CLP', 'IDR', 'HUF'])

export function PriceChart({ bars, mode = 'candles', height = 320, showVolume = true, bands, markers, priceLines, overlays, ariaLabel, className, lineColor, currency, future, fan, badges, futureTooltip, fill, focusBars }: PriceChartProps) {
  const precision = currency && ZERO_DEC.has(currency) ? 0 : 2
  const t = useChartTokens()
  const el = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const mainRef = useRef<ISeriesApi<SeriesType> | null>(null)
  const volRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  const bandsRef = useRef<BandsPrimitive | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const linesRef = useRef<IPriceLine[]>([])
  const overlayRef = useRef<ISeriesApi<'Line'>[]>([])
  const fanRef = useRef<FanPrimitive | null>(null)
  const tipFn = useRef(futureTooltip)
  const lastBarRef = useRef<string | null>(null)
  const [tip, setTip] = useState<{ x: number; y: number; text: string } | null>(null)
  const tRef = useRef(t)
  // keep latest tokens for the create effect (layout effects run before passive effects)
  useLayoutEffect(() => {
    tRef.current = t
    tipFn.current = futureTooltip
  }, [t, futureTooltip])

  // create / destroy (structure changes only)
  useEffect(() => {
    if (!el.current) return
    const tk = tRef.current
    const priceFormat = { type: 'price' as const, precision, minMove: precision ? 0.01 : 1 }
    const chart = createChart(el.current, { autoSize: true, ...chartOptions(tk) })
    chartRef.current = chart
    let main: ISeriesApi<SeriesType>
    if (mode === 'candles') {
      main = chart.addSeries(CandlestickSeries, {
        upColor: tk.up,
        downColor: tk.down,
        borderUpColor: tk.up,
        borderDownColor: tk.down,
        wickUpColor: tk.up,
        wickDownColor: tk.down,
        priceLineVisible: true,
        priceFormat,
      })
    } else if (mode === 'area') {
      const c = lineColor ?? tk.accent
      main = chart.addSeries(AreaSeries, { lineColor: c, topColor: alpha(c, 0.22), bottomColor: alpha(c, 0), lineWidth: 2, priceLineVisible: false, priceFormat })
    } else {
      main = chart.addSeries(LineSeries, { color: lineColor ?? tk.accent, lineWidth: 2, priceLineVisible: false, priceFormat })
    }
    mainRef.current = main
    const prim = new BandsPrimitive()
    main.attachPrimitive(prim)
    bandsRef.current = prim
    markersRef.current = createSeriesMarkers(main, [])
    const fp = new FanPrimitive()
    main.attachPrimitive(fp)
    fanRef.current = fp
    chart.subscribeCrosshairMove((param) => {
      const iso = isoOf(param.time)
      const fn = tipFn.current
      const last = lastBarRef.current
      if (!fn || !iso || !param.point || !last || iso <= last) {
        setTip(null)
        return
      }
      const text = fn(iso)
      setTip(text ? { x: param.point.x, y: param.point.y, text } : null)
    })
    if (showVolume) {
      const vol = chart.addSeries(HistogramSeries, { priceScaleId: 'vol', priceFormat: { type: 'volume' }, lastValueVisible: false, priceLineVisible: false })
      chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } })
      volRef.current = vol
    }
    return () => {
      chart.remove()
      chartRef.current = null
      mainRef.current = null
      volRef.current = null
      bandsRef.current = null
      fanRef.current = null
      markersRef.current = null
      linesRef.current = []
      overlayRef.current = []
    }
  }, [mode, showVolume, lineColor, precision])

  // theme
  useEffect(() => {
    const chart = chartRef.current
    const main = mainRef.current
    if (!chart || !main) return
    chart.applyOptions(chartOptions(t))
    if (mode === 'candles') {
      main.applyOptions({ upColor: t.up, downColor: t.down, borderUpColor: t.up, borderDownColor: t.down, wickUpColor: t.up, wickDownColor: t.down })
    } else if (mode === 'area') {
      const c = lineColor ?? t.accent
      main.applyOptions({ lineColor: c, topColor: alpha(c, 0.22), bottomColor: alpha(c, 0) })
    } else {
      main.applyOptions({ color: lineColor ?? t.accent })
    }
  }, [t, mode, lineColor])

  // data (+ future whitespace sessions)
  const futureKey = (future ?? []).join(',')
  useEffect(() => {
    const main = mainRef.current
    if (!main) return
    const last = bars.length ? bars[bars.length - 1].time : null
    lastBarRef.current = last
    const ws: WhitespaceData<Time>[] = last ? (futureKey ? futureKey.split(',') : []).filter((d) => d > last).map((d) => ({ time: d as Time })) : []
    if (mode === 'candles') {
      main.setData([...bars.map((b) => ({ time: b.time as Time, open: b.open, high: b.high, low: b.low, close: b.close })), ...ws])
    } else {
      main.setData([...bars.map((b) => ({ time: b.time as Time, value: b.close })), ...ws])
    }
    volRef.current?.setData(
      bars.map((b) => ({ time: b.time as Time, value: b.volume, color: alpha(b.close >= b.open ? t.up : t.down, 0.35) })),
    )
    const ts = chartRef.current?.timeScale()
    // fitContent stops at the last bar: with future sessions (whitespace) the view is set explicitly to include them
    if (ts && (ws.length || (focusBars && bars.length > focusBars))) {
      const from = focusBars && bars.length > focusBars ? bars.length - focusBars : 0
      ts.setVisibleLogicalRange({ from: from - 0.5, to: bars.length - 1 + ws.length + 1.5 })
    } else ts?.fitContent()
  }, [bars, mode, t, futureKey, focusBars])

  // fan + badges
  useEffect(() => {
    fanRef.current?.set(fan ?? null, badges ?? [], t.fontSans, t.theme === 'dark' ? '#0d0f12' : '#ffffff', alpha(t.surface, 0.92))
  }, [fan, badges, t])

  // bands
  useEffect(() => {
    bandsRef.current?.set(bands ?? [], t.fontSans, t.theme === 'dark' ? '#0d0f12' : '#ffffff')
  }, [bands, t])

  // markers
  useEffect(() => {
    const m: SeriesMarker<Time>[] = (markers ?? [])
      .slice()
      .sort((a, b) => a.time.localeCompare(b.time))
      .map((x) => ({ time: x.time as Time, position: x.position, color: x.color, shape: x.shape, text: x.text }))
    markersRef.current?.setMarkers(m)
  }, [markers, mode])

  // price lines
  useEffect(() => {
    const main = mainRef.current
    if (!main) return
    for (const l of linesRef.current) main.removePriceLine(l)
    linesRef.current = (priceLines ?? []).map((p) =>
      main.createPriceLine({ price: p.price, color: p.color, lineWidth: 1, lineStyle: p.dashed ? LineStyle.Dashed : LineStyle.Solid, axisLabelVisible: true, title: p.title }),
    )
  }, [priceLines, mode])

  // overlays
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    for (const s of overlayRef.current) chart.removeSeries(s)
    overlayRef.current = (overlays ?? []).map((o) => {
      const s = chart.addSeries(LineSeries, {
        color: o.color,
        lineWidth: o.width ?? 1,
        lineStyle: o.dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        priceScaleId: o.separateScale ? 'left' : 'right',
      })
      s.setData(o.data.map((d) => ({ time: d.time as Time, value: d.value })))
      return s
    })
    if ((overlays ?? []).some((o) => o.separateScale)) chart.applyOptions({ leftPriceScale: { visible: true } })
  }, [overlays, mode, showVolume])

  return (
    <div className={cn('relative w-full min-w-0', className)} style={{ height: fill ? '100%' : height }}>
      <div ref={el} role="img" aria-label={ariaLabel} className="h-full w-full" />
      {tip && (
        <div
          role="tooltip"
          data-testid="fan-tooltip"
          className="pointer-events-none absolute z-10 max-w-[260px] rounded-md border border-border-strong bg-surface-2 px-2 py-1 text-2xs leading-snug text-fg shadow-sm"
          style={{ left: Math.max(4, tip.x - 270), top: Math.max(4, tip.y - 40) }}
        >
          {tip.text}
        </div>
      )}
    </div>
  )
}
