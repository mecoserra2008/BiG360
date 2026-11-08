import { useMemo, useState } from 'react'
import { LEVEL_STYLE, coneLines, fanAt, fanGeometry, fanStrip, fanTooltip, fmtTouch, topZones, touchAt, zoneBadges, zoneTouch } from '@/api/conformal/adapters'
import { useConformalEnvelope, useConformalTouch } from '@/api/conformal/hooks'
import type { CfLevel } from '@/api/conformal/types'
import { useTechContext } from '@/api/techcontext/hooks'
import type { CompanyTechnicals, ConfluenceZone, LevelType } from '@/api/types'
import { useCompanyTechnicals } from '@/api/hooks'
import { CalibrationPopover } from '@/components/conformal/notes'
import type { Band, ChartBadge, ChartFan, ChartMarker, ChartOverlay, ChartPriceLine } from '@/components/charts/PriceChart'
import { RangePriceChart } from '@/components/charts/RangePriceChart'
import { useChartTokens } from '@/components/charts/tokens'
import { ContextChips } from '@/components/technicals/ContextChips'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Switch } from '@/components/ui/inputs'
import { Tip } from '@/components/ui/tooltip'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtDate, fmtDateShort, fmtInt, fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/utils'
import { SetupProbabilities, TechScoreCard, ZoneHoldCell, ZoneHoldHeader } from './TechnicalsSlots'
import { BreakoutPanel, TechFactorsCard } from './TechnicalsV3Slots'

export default function TechnicalsTab({ id }: { id: string }) {
  const q = useCompanyTechnicals(id)
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-[480px]" />}>
      {(d) => <TechBody d={d} id={id} />}
    </QueryView>
  )
}

const SHORT: Record<LevelType, string> = {
  swing_pivot: 'Pivot',
  volume_profile: 'VP',
  fibonacci: 'Fib',
  round_number: 'Round',
  ma_200: 'MA200',
  ma_50: 'MA50',
  gap: 'Gap',
  anchored_vwap: 'AVWAP',
  prior_high_low: 'PHL',
}

const TOP_ZONES = 3

function sma(closes: number[], n: number, times: string[]) {
  const out: { time: string; value: number }[] = []
  let s = 0
  for (let i = 0; i < closes.length; i++) {
    s += closes[i]
    if (i >= n) s -= closes[i - n]
    if (i >= n - 1) out.push({ time: times[i], value: +(s / n).toFixed(4) })
  }
  return out
}

function TechBody({ d, id }: { d: CompanyTechnicals; id: string }) {
  const t = useChartTokens()
  const px = (v: number | null | undefined) => fmtPrice(v, d.currency)
  const [showZones, setShowZones] = useState(true)
  const [zoneSet, setZoneSet] = useState<'top' | 'all'>('top')
  const [hoverZone, setHoverZone] = useState<string | null>(null)
  const [showPivots, setShowPivots] = useState(true)
  const [showMas, setShowMas] = useState(true)
  const [showSetup, setShowSetup] = useState(true)
  const [showRange, setShowRange] = useState(true)
  const [rangeLevel, setRangeLevel] = useState<'80' | '90' | '95'>('90')
  const [touchH, setTouchH] = useState<'1' | '5' | '10' | '20'>('20')
  const env = useConformalEnvelope(id)
  const touch = useConformalTouch(id, 20)
  const ctx = useTechContext(id)
  const envOk = env.data && env.data.data_status !== 'pending' && env.data.bands.length > 0 ? env.data : undefined
  const geom = useMemo(() => fanGeometry(envOk), [envOk])
  const zt = useMemo(() => zoneTouch(touch.data), [touch.data])
  const rangeOn = showRange && !!envOk

  // zones on the chart: the strongest few by default; the rest via the "All" toggle or by hovering a table row
  const top = useMemo(() => new Set(topZones(d.zones, TOP_ZONES).map((z) => z.id)), [d.zones])
  const chartZones = useMemo<ConfluenceZone[]>(
    () => d.zones.filter((z) => z.id === hoverZone || (showZones && (zoneSet === 'all' || top.has(z.id)))),
    [d.zones, hoverZone, showZones, zoneSet, top],
  )
  const bands = useMemo<Band[]>(
    () =>
      chartZones.map((z) => ({
        low: z.low,
        high: z.high,
        color: z.kind === 'support' ? t.up : t.down,
        opacity: z.id === hoverZone ? 0.3 : 0.05 + z.strength * 0.13,
        label: `${z.kind === 'support' ? 'S' : 'R'}${z.rank}`,
      })),
    [chartZones, t, hoverZone],
  )
  const markers = useMemo<ChartMarker[]>(
    () =>
      showPivots
        ? d.pivots.map((p) => ({
            time: p.time,
            position: p.kind === 'high' ? 'aboveBar' : 'belowBar',
            color: p.label === 'HH' || p.label === 'HL' ? t.up : p.label === 'LH' || p.label === 'LL' ? t.down : t.muted,
            shape: 'circle',
            text: p.label ?? undefined,
          }))
        : [],
    [d.pivots, t, showPivots],
  )
  const overlays = useMemo<ChartOverlay[]>(() => {
    const out: ChartOverlay[] = []
    if (showMas) {
      const closes = d.bars.map((b) => b.close)
      const times = d.bars.map((b) => b.time)
      out.push({ id: 'ma50', data: sma(closes, 50, times), color: t.series[1], width: 1 }, { id: 'ma200', data: sma(closes, 200, times), color: t.series[3], width: 1 })
    }
    // older API without the fan: the 1/5/10/20 cone lines
    if (rangeOn && envOk && !geom) {
      const lvl = Number(rangeLevel) as CfLevel
      for (const l of coneLines(envOk, [lvl])) out.push({ id: l.id, data: l.data, color: t.accent, width: 2, dashed: LEVEL_STYLE[lvl].dashed, future: true })
    }
    return out
  }, [d.bars, t, showMas, rangeOn, envOk, geom, rangeLevel])
  const lines = useMemo<ChartPriceLine[]>(
    () =>
      showSetup
        ? [
            { price: d.setup.entry, color: t.accent, title: 'Entry' },
            { price: d.setup.invalidation, color: t.down, title: 'Invalid.', dashed: true },
            { price: d.setup.target1, color: t.up, title: 'T1', dashed: true },
            { price: d.setup.target2, color: t.up, title: 'T2', dashed: true },
          ].filter((l) => Number.isFinite(l.price))
        : [],
    [d.setup, t, showSetup],
  )
  // forward fan (calibrated range at every horizon 1..20 sessions), earnings marker, zone badges
  const fan = useMemo<ChartFan | null>(
    () =>
      rangeOn && geom
        ? { origin: geom.origin, points: geom.points, color: t.accent, marker: geom.earnings ? { time: geom.earnings.time, label: 'Earnings', color: t.warn } : null }
        : null,
    [rangeOn, geom, t],
  )
  const future = useMemo(() => (rangeOn && geom ? geom.points.map((p) => p.time) : undefined), [rangeOn, geom])
  const badges = useMemo<ChartBadge[]>(
    () => (fan ? zoneBadges(chartZones, touch.data, geom, Number(touchH)).map((b) => ({ price: b.price, text: b.text, color: b.kind === 'support' ? t.up : t.down })) : []),
    [fan, chartZones, touch.data, geom, touchH, t],
  )
  const futureTooltip = useMemo(
    () => (geom ? (time: string) => fanTooltip(fanAt(geom, time), (v) => fmtPrice(v, d.currency), fmtDateShort) : undefined),
    [geom, d.currency],
  )
  const strip = useMemo(() => fanStrip(geom, Number(rangeLevel) as CfLevel), [geom, rangeLevel])
  const last = d.bars[d.bars.length - 1].close
  const zonesSorted = [...d.zones].sort((a, b) => b.high - a.high)
  const maxHit = Math.max(...d.levelStats.map((s) => s.hitRate))

  return (
    <div className="grid grid-cols-12 gap-3">
      <ContextChips ctx={ctx.data} loading={ctx.isLoading} className="col-span-12" />

      <Panel
        title="Price & confluence zones"
        className="col-span-12 xl:col-span-9"
        bodyClassName="flex flex-col p-1.5"
        actions={
          <div className="flex items-center gap-2.5 text-xs text-muted">
            <label className="flex items-center gap-1.5"><Switch label="Show zones" checked={showZones} onCheckedChange={setShowZones} /> Zones</label>
            {showZones && (
              <Segmented label="Zones shown" size="xs" value={zoneSet} onChange={setZoneSet} options={[{ value: 'top', label: `Top ${TOP_ZONES}` }, { value: 'all', label: 'All' }]} />
            )}
            <label className="flex items-center gap-1.5"><Switch label="Show pivots" checked={showPivots} onCheckedChange={setShowPivots} /> Pivots</label>
            <label className="flex items-center gap-1.5"><Switch label="Show moving averages" checked={showMas} onCheckedChange={setShowMas} /> MAs</label>
            <label className="flex items-center gap-1.5"><Switch label="Show trade setup" checked={showSetup} onCheckedChange={setShowSetup} /> Setup</label>
            <label className="flex items-center gap-1.5" title={envOk ? 'Calibrated range (model-based): bands calibrated over history at every horizon 1–20 sessions' : 'Calibrated range not available for this security yet'}>
              <Switch label="Show calibrated range" checked={rangeOn} disabled={!envOk} onCheckedChange={setShowRange} /> Calibrated range
            </label>
          </div>
        }
      >
        <div className="relative min-h-[440px] flex-1">
          <div className="absolute inset-0">
            <RangePriceChart
              id={id}
              initial={d.bars}
              initialRange="6M"
              height={400}
              fill
              currency={d.currency}
              bands={bands}
              markers={markers}
              overlays={overlays}
              priceLines={lines}
              fan={fan}
              future={future}
              badges={badges}
              futureTooltip={futureTooltip}
              overlaysLabel="zones, levels, setup and the calibrated range"
              ariaLabel="Candlestick chart with confluence zones and the calibrated range fan"
            />
          </div>
        </div>
        {envOk && (
          <div className="mt-1.5 space-y-1 border-t border-border px-1.5 pt-1.5" data-testid="range-strip">
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
              <span className="label-caps">Calibrated range</span>
              <Segmented label="Calibrated range level" size="xs" value={rangeLevel} onChange={setRangeLevel} options={[{ value: '80', label: '80%' }, { value: '90', label: '90%' }, { value: '95', label: '95%' }]} />
              <span className="text-2xs text-faint">from the {fmtDateShort(envOk.as_of)} close · no direction call</span>
              <div className="ml-auto flex items-center gap-1.5">
                <span className="text-2xs text-muted">Zone badges</span>
                <Segmented label="Touch badge horizon" size="xs" value={touchH} onChange={setTouchH} options={[{ value: '1', label: '1d' }, { value: '5', label: '5d' }, { value: '10', label: '10d' }, { value: '20', label: '20d' }]} />
                <CalibrationPopover env={envOk} level={Number(rangeLevel)} fan={geom ? { source: geom.source, adjusted: geom.adjusted } : null} label="About" />
              </div>
            </div>
            <ul className="grid grid-cols-2 gap-x-3 gap-y-0.5 md:grid-cols-4" aria-label="Calibrated range by horizon">
              {(strip.length
                ? strip
                : [1, 5, 10, 20].flatMap((h) => {
                    const b = envOk.bands.find((x) => x.horizon === h && x.level === Number(rangeLevel))
                    return b ? [{ h, time: b.target_date ?? '', lo: b.lo, hi: b.hi, loPct: b.lo_pct ?? b.lo / last - 1, hiPct: b.hi_pct ?? b.hi / last - 1 }] : []
                  })
              ).map((c) => (
                <li key={c.h} className="flex min-w-0 items-baseline gap-1.5 text-xs whitespace-nowrap" title={c.time ? `${c.h} session${c.h > 1 ? 's' : ''} ahead: by ${fmtDate(c.time)}` : undefined}>
                  <span className="w-6 font-semibold text-muted">{c.h}d</span>
                  <span className="num truncate">
                    {px(c.lo)}–{px(c.hi)}
                  </span>
                  <span className="num text-2xs text-faint">±{fmtPct((c.hiPct - c.loPct) / 2, 1)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1.5 pt-1.5 text-2xs text-muted">
          <span className="inline-flex items-center gap-1"><span className="h-2 w-4 rounded-sm bg-up-soft ring-1 ring-up" /> Support zone</span>
          <span className="inline-flex items-center gap-1"><span className="h-2 w-4 rounded-sm bg-down-soft ring-1 ring-down" /> Resistance zone</span>
          {showMas && (
            <>
              <span className="inline-flex items-center gap-1"><span className="h-0.5 w-4" style={{ background: t.series[1] }} /> MA50</span>
              <span className="inline-flex items-center gap-1"><span className="h-0.5 w-4" style={{ background: t.series[3] }} /> MA200</span>
            </>
          )}
          {fan && (
            <span className="inline-flex items-center gap-1" data-testid="calibrated-range-legend">
              <span className="inline-flex h-2.5 w-5 overflow-hidden rounded-sm">
                <span className="h-full w-1/3" style={{ background: t.accent, opacity: 0.18 }} />
                <span className="h-full w-1/3" style={{ background: t.accent, opacity: 0.32 }} />
                <span className="h-full w-1/3" style={{ background: t.accent, opacity: 0.5 }} />
              </span>
              Calibrated range 95 / 90 / 80% to 20 sessions
            </span>
          )}
          {fan?.marker && (
            <span className="inline-flex items-center gap-1">
              <span className="h-2.5 w-0 border-l border-dashed" style={{ borderColor: t.warn }} /> Earnings {fmtDateShort(fan.marker.time)}
            </span>
          )}
          {showZones && zoneSet === 'top' && d.zones.length > TOP_ZONES && <span className="text-faint">Strongest {TOP_ZONES} zones shown · hover a zone row below to preview it</span>}
        </div>
      </Panel>

      <div className="col-span-12 flex flex-col gap-3 xl:col-span-3">
        <Panel
          title="Trade setup"
          subtitle="Nearest ranked zone"
          data-slot="trade-setup"
          actions={<Badge tone={d.setup.bias === 'long' ? 'up' : d.setup.bias === 'short' ? 'down' : 'neutral'}>{d.setup.bias === 'long' ? '▲' : d.setup.bias === 'short' ? '▼' : '■'} {d.setup.bias}</Badge>}
        >
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
            <Stat
              label={Number.isFinite(d.setup.entryLow ?? NaN) ? 'Entry zone' : 'Entry'}
              value={Number.isFinite(d.setup.entryLow ?? NaN) && Number.isFinite(d.setup.entryHigh ?? NaN) ? `${px(d.setup.entryLow)}–${px(d.setup.entryHigh)}` : px(d.setup.entry)}
              sub={fmtPct(d.setup.entry / last - 1, 1, true) + ' vs last'}
            />
            <Stat label="Invalidation" value={<span className="text-down">{px(d.setup.invalidation)}</span>} sub={fmtPct(d.setup.invalidation / d.setup.entry - 1, 1, true)} />
            <Stat label="Target 1" value={<span className="text-up">{px(d.setup.target1)}</span>} sub={fmtPct(d.setup.target1 / d.setup.entry - 1, 1, true)} />
            <Stat label="Target 2" value={<span className="text-up">{px(d.setup.target2)}</span>} sub={fmtPct(d.setup.target2 / d.setup.entry - 1, 1, true)} />
          </dl>
          <div className="mt-3 flex items-center justify-between rounded-md border border-border bg-surface-2 px-2 py-1.5">
            <span className="label-caps">Reward : risk</span>
            <span className={cn('num text-lg font-semibold', d.setup.riskReward >= 2 ? 'text-up' : d.setup.riskReward < 1 ? 'text-down' : 'text-fg')}>{Number.isFinite(d.setup.riskReward) ? `${fmtNum(d.setup.riskReward, 2)}×` : '—'}</span>
          </div>
          {/* SLOT T2: setup probabilities (P(win) / P(loss) / expected R) - TechnicalsSlots.tsx */}
          <SetupProbabilities securityId={id} setup={d.setup} />
          {(d.setup.trend || d.setup.structureState || d.setup.lastBreak) && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
              {d.setup.trend && <Badge tone={d.setup.trend === 'up' ? 'up' : d.setup.trend === 'down' ? 'down' : 'neutral'}>{d.setup.trend === 'up' ? '▲' : d.setup.trend === 'down' ? '▼' : '■'} trend {d.setup.trend}</Badge>}
              {d.setup.structureState && <Badge tone={d.setup.structureState === 'bullish' ? 'up' : d.setup.structureState === 'bearish' ? 'down' : 'neutral'}>structure {d.setup.structureState}</Badge>}
              {d.setup.lastBreak && (
                <span className="text-muted" title="Last break of structure (BOS) / change of character (CHoCH)">
                  last {d.setup.lastBreak.kind.toUpperCase()} {d.setup.lastBreak.direction === 'up' ? '▲' : d.setup.lastBreak.direction === 'down' ? '▼' : ''} {fmtDate(d.setup.lastBreak.date)} @ <span className="num">{px(d.setup.lastBreak.price)}</span>
                </span>
              )}
            </div>
          )}
          <p className="mt-2 line-clamp-4 text-xs text-muted" title={d.setup.note}>{d.setup.note}</p>
        </Panel>
        {/* SLOT T4: calibrated technical score card - TechnicalsSlots.tsx */}
        <TechScoreCard securityId={id} setup={d.setup} />
        {/* Technicals v3 #1 / #5: evidence-based factors + breakout state - TechnicalsV3Slots.tsx */}
        <TechFactorsCard securityId={id} />
        <BreakoutPanel securityId={id} />
        <Panel title="Indicators">
          <dl className="grid grid-cols-2 gap-x-3 gap-y-2">
            <Stat label="RSI 14" value={fmtNum(d.indicators.rsi14, 1)} sub={d.indicators.rsi14 > 70 ? 'overbought' : d.indicators.rsi14 < 30 ? 'oversold' : 'neutral'} />
            <Stat label="ATR 14" value={fmtNum(d.indicators.atr14, 2)} sub={fmtPct(d.indicators.atr14 / last, 1) + ' of price'} />
            <Stat label="SMA 50" value={px(d.indicators.ma50)} sub={fmtPct(last / d.indicators.ma50 - 1, 1, true) + ' vs last'} />
            <Stat label="SMA 200" value={px(d.indicators.ma200)} sub={fmtPct(last / d.indicators.ma200 - 1, 1, true) + ' vs last'} />
          </dl>
          <div className="mt-2 text-xs text-muted">
            Trend: <span className="font-semibold text-fg capitalize">{d.indicators.trend === 'up' ? '▲ ' : d.indicators.trend === 'down' ? '▼ ' : '■ '}{d.indicators.trend}</span>
          </div>
        </Panel>
      </div>

      <Panel
        title="Confluence zones"
        subtitle="Sorted by price (highest first; the chart shows the strongest); contributors = coinciding level types · touch = historical frequency of touching the near edge within h sessions for similar setups (model-based) · hover a row to show it on the chart"
        className="col-span-12 xl:col-span-7"
        flush
        actions={<Segmented label="Chance-of-touch horizon" size="xs" value={touchH} onChange={setTouchH} options={[{ value: '1', label: '1d' }, { value: '5', label: '5d' }, { value: '10', label: '10d' }, { value: '20', label: '20d' }]} />}
      >
        <div className="min-w-0 overflow-x-auto">
          <table className="w-full text-sm" onMouseLeave={() => setHoverZone(null)}>
            <thead>
              <tr className="text-2xs tracking-wide text-muted uppercase">
                {['Rank', 'Type', 'Range', 'Dist.', 'Strength', `Touch ${touchH}d`].map((h, i) => (
                  <th key={h} scope="col" className={cn('h-7 border-b border-border px-2 font-semibold', i === 0 ? 'pl-3 text-left' : i >= 2 ? 'text-right' : 'text-left')}>
                    {i === 5 ? (
                      <Tip content={`Historical frequency of touching the zone's near edge within ${touchH} trading day(s) for similar setups (same calibration pool, standardised distance). Calibrated within 0.2 pp over 2011–2026; model-based, not a forecast of direction.`}>
                        <span className="cursor-help underline decoration-dotted underline-offset-2">{h}</span>
                      </Tip>
                    ) : (
                      h
                    )}
                  </th>
                ))}
                {/* SLOT T3: zone hold column header - TechnicalsSlots.tsx */}
                <ZoneHoldHeader horizon={Number(touchH)} />
                <th scope="col" className="h-7 border-b border-border px-2 text-left font-semibold">Contributors</th>
                <th scope="col" className="h-7 border-b border-border px-2 pr-3 text-right font-semibold">Touches</th>
              </tr>
            </thead>
            <tbody>
              {zonesSorted.map((z) => {
                const mid = (z.low + z.high) / 2
                const lv = zt.get(Number(z.id.replace(/^z/, '')))
                const p = lv?.inside ? 1 : touchAt(lv, Number(touchH))
                return (
                  <tr
                    key={z.id}
                    className={cn('hover:bg-surface-2', d.setup.zoneId === z.id && 'bg-accent-soft', hoverZone === z.id && 'bg-surface-2')}
                    onMouseEnter={() => setHoverZone(z.id)}
                    data-testid="zone-row"
                  >
                    <td className="num h-[26px] border-b border-border pl-3 font-semibold">
                      #{z.rank}
                      {top.has(z.id) && <span className="ml-1 inline-block size-1.5 rounded-full bg-accent align-middle" title={`One of the ${TOP_ZONES} strongest zones (shown on the chart by default)`} />}
                    </td>
                    <td className="border-b border-border px-2"><Badge tone={z.kind === 'support' ? 'up' : 'down'}>{z.kind === 'support' ? '▲ Support' : '▼ Resist.'}</Badge></td>
                    <td className="num border-b border-border px-2 text-right whitespace-nowrap">{px(z.low)}–{px(z.high)}</td>
                    <td className="num border-b border-border px-2 text-right text-muted">{fmtPct(mid / last - 1, 1, true)}</td>
                    <td className="border-b border-border px-2 text-right">
                      <span className="inline-flex items-center gap-1.5">
                        <span className="num">{Math.round(z.strength * 100)}</span>
                        <span className="h-1.5 w-12 overflow-hidden rounded-full bg-surface-3"><span className="block h-full rounded-full bg-accent" style={{ width: `${z.strength * 100}%` }} /></span>
                      </span>
                    </td>
                    <td className="num border-b border-border px-2 text-right" data-testid="touch-cell">
                      {p == null ? <span className="text-faint">—</span> : <span className={cn(p >= 0.5 ? 'text-fg' : 'text-muted')}>{lv?.inside ? 'in zone' : fmtTouch(p)}</span>}
                    </td>
                    {/* SLOT T3: zone hold cell - TechnicalsSlots.tsx */}
                    <ZoneHoldCell securityId={id} zone={z} horizon={Number(touchH)} />
                    <td className="border-b border-border px-2 text-xs text-muted">{z.contributors.map((c) => SHORT[c]).join(' · ')}</td>
                    <td className="num border-b border-border px-2 pr-3 text-right">{z.touches}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Panel>

      <Panel title="Level types · backtested hit rates" subtitle="Best level key per type for this stock (shrunk hit rate, n = events)" className="col-span-12 xl:col-span-5" flush>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Level type</th>
              <th scope="col" className="border-b border-border px-2 text-left font-semibold">Hit rate</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold">Avg reaction</th>
              <th scope="col" className="border-b border-border px-3 text-right font-semibold">n</th>
            </tr>
          </thead>
          <tbody>
            {d.levelStats.map((s) => (
              <tr key={s.type} className="hover:bg-surface-2">
                <td className="h-[26px] border-b border-border px-3">{s.label} <span className="text-2xs text-faint">{SHORT[s.type]}</span></td>
                <td className="border-b border-border px-2">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="num w-10 text-right">{fmtPct(s.hitRate, 0)}</span>
                    <span className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-3"><span className="block h-full rounded-full bg-info" style={{ width: `${(s.hitRate / maxHit) * 100}%` }} /></span>
                  </span>
                </td>
                <td className="num border-b border-border px-2 text-right">{fmtPct(s.avgReactionPct, 1)}</td>
                <td className="num border-b border-border px-3 text-right text-muted">{fmtInt(s.sampleSize)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      {(d.typeReliability?.length ?? 0) > 0 && (
        <Panel
          title="Level-type reliability"
          subtitle="This stock's level keys aggregated by type (10-year backtest): bounce hit rate (shrunk) vs a random-level control"
          className="col-span-12"
          flush
        >
          <table className="w-full text-sm">
            <thead>
              <tr className="text-2xs tracking-wide text-muted uppercase">
                <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Level type</th>
                <th scope="col" className="border-b border-border px-2 text-left font-semibold">Hit rate</th>
                <th scope="col" className="border-b border-border px-2 text-right font-semibold">Edge vs random</th>
                <th scope="col" className="border-b border-border px-3 text-right font-semibold">Events</th>
              </tr>
            </thead>
            <tbody>
              {d.typeReliability!.map((r) => (
                <tr key={r.type} className="hover:bg-surface-2">
                  <td className="h-[26px] border-b border-border px-3">{r.label}</td>
                  <td className="border-b border-border px-2">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="num w-10 text-right">{fmtPct(r.hitRate, 1)}</span>
                      <span className="h-1.5 w-24 overflow-hidden rounded-full bg-surface-3"><span className="block h-full rounded-full bg-info" style={{ width: `${Math.max(0, Math.min(100, r.hitRate * 100))}%` }} /></span>
                    </span>
                  </td>
                  <td className={cn('num border-b border-border px-2 text-right', r.edge > 0 ? 'text-up' : r.edge < 0 ? 'text-down' : 'text-muted')}>
                    {Number.isFinite(r.edge) ? `${r.edge > 0 ? '▲ +' : r.edge < 0 ? '▼ ' : ''}${fmtNum(r.edge * 100, 1)} pp` : '—'}
                  </td>
                  <td className="num border-b border-border px-3 text-right text-muted">{fmtInt(r.nEvents)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="border-t border-border px-3 py-2 text-xs text-muted">
            Out-of-sample: top-ranked level types beat random levels by ~1.5pp bounce rate — use as an entry filter, not a standalone strategy.
          </p>
        </Panel>
      )}
    </div>
  )
}
