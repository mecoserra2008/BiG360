import { Pause, Play, Plus, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import {
  curveXY,
  defaultSpreads,
  fmtBp,
  fmtRate,
  frameIndexAt,
  frameXY,
  framesExtent,
  groupCurves,
  seriesId,
  slopeSpreads,
  tenorLabel,
} from '@/api/rates/adapters'
import { useRatesCurve, useRatesCurves, useRatesMonitor, useRatesSeries, useRatesSovereign } from '@/api/rates/hooks'
import { KIND_LABEL, SNAP_LABEL, TYPE_LABEL, type CurveType, type RatesCurve, type RatesMonitor, type RatesMonitorCountry } from '@/api/rates/types'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { EmptyState, QueryView, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Segmented, Select, Slider } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtCompact, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroNav } from '../macro/shared'
import { Bars, type CurveLine, YieldCurveChart } from './charts'

const TENORS = [24, 60, 120, 360] as const
const COMPARE = ['1w', '1m', '1y', '5y'] as const
const RANGE_YEARS = { '1Y': 1, '5Y': 5, '10Y': 10, '20Y': 20, Max: 80 } as const
type Range = keyof typeof RANGE_YEARS
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pct = (v: number) => fmtRate(v)
const bp = (v: number) => fmtBp(v, false)
const isoYearsAgo = (y: number) => {
  const d = new Date()
  d.setFullYear(d.getFullYear() - y)
  return d.toISOString().slice(0, 10)
}

/** Macro → Curves: multi-country official yield curves, slopes, spreads vs Bund / UST, real vs nominal, history slider + animation. */
export default function CurveMonitor() {
  const [params, setParams] = useSearchParams()
  const set = (k: string, v: string | null) =>
    setParams(
      (p) => {
        if (v === null) p.delete(k)
        else p.set(k, v)
        return p
      },
      { replace: true },
    )
  const tenor = Number(params.get('tenor') ?? 120)
  const mon = useRatesMonitor(TENORS.includes(tenor as (typeof TENORS)[number]) ? tenor : 120)
  const curve = params.get('curve') ?? 'US_TSY'
  const overlays = (params.get('vs') ?? 'DE_BUND,GB_GILT,JP_JGB').split(',').filter((x) => x && x !== curve).slice(0, 3)
  return (
    <>
      <PageHeader
        title="Curve monitor"
        subtitle="Official government curves (Treasury, Fed H.15, ECB, Bundesbank, BoE, MoF, Banco de España, Banco de Portugal / IGCP, SNB, BoC, RBA, Riksbank, Norges Bank) — slopes, spreads vs Bund and UST, real vs nominal, money markets"
      />
      <MacroNav />
      <div className="space-y-3">
        <LiveStrip q={mon.data} />
        <Panel
          title="Benchmark yields by country"
          subtitle="Latest official curve date per country · click a row to chart its curve · spreads and changes at the selected tenor"
          actions={<Segmented label="Tenor" size="xs" value={String(tenor)} onChange={(v) => set('tenor', v)} options={TENORS.map((t) => ({ value: String(t), label: tenorLabel(t) }))} />}
          flush
        >
          <QueryView query={mon} skeleton={<SkeletonRows rows={10} className="p-3" />}>
            {(d) => <CountryTable d={d} selected={curve} onSelect={(c) => set('curve', c)} />}
          </QueryView>
        </Panel>
        <CurvePanel curve={curve} overlays={overlays} onOverlays={(v) => set('vs', v.join(','))} onCurve={(c) => set('curve', c)} />
        <SpreadPanel curve={curve} mon={mon.data} />
        <div className="grid gap-3 xl:grid-cols-2">
          <RealPanel q={mon} />
          <MoneyPanel q={mon} />
        </div>
        <Lt10Panel q={mon} />
        <SovereignPanel />
        <SourcesNote />
      </div>
    </>
  )
}

/* ───────────── live strip ───────────── */

function LiveStrip({ q }: { q: RatesMonitor | undefined }) {
  const live = q?.live
  return (
    <Panel bodyClassName="py-2">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-xs">
        <span className="label-caps">Delayed live yields</span>
        {live?.items.length ? (
          live.items.map((it) => (
            <span key={it.id} className="num">
              <span className="text-muted">{it.label}</span> <span className="text-fg">{fmtRate(it.value, 3)}</span>{' '}
              <span className={cn(fin(it.change_bp) && it.change_bp > 0 ? 'text-down' : fin(it.change_bp) && it.change_bp < 0 ? 'text-up' : 'text-muted')}>{fmtBp(it.change_bp)}</span>
              {it.stale ? <Badge tone="warn" className="ml-1">stale</Badge> : null}
            </span>
          ))
        ) : (
          <span className="text-muted">{live?.reason ?? 'Delayed quotes not available (quotes service off or not polled yet)'}</span>
        )}
        <span className="ml-auto text-2xs text-faint">{live?.label}</span>
      </div>
    </Panel>
  )
}

/* ───────────── country table ───────────── */

function CountryTable({ d, selected, onSelect }: { d: RatesMonitor; selected: string; onSelect: (c: string) => void }) {
  if (!d.countries.length) return <EmptyState className="m-3" title="No curves yet" message="The official curves have not been loaded yet (job rates_curves / campaign rates_curves_history)." />
  const t = d.tenor_months
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead className="text-2xs text-muted">
          <tr className="border-b border-border">
            <th className="px-3 py-1.5 text-left font-medium">Country / curve</th>
            <th className="px-2 text-left font-medium">Date</th>
            {['2Y', '5Y', '10Y', '30Y'].map((h) => (
              <th key={h} className="px-2 text-right font-medium">{h}</th>
            ))}
            <th className="px-2 text-right font-medium"><Tip content="10y minus 2y"><span>2s10s</span></Tip></th>
            <th className="px-2 text-right font-medium"><Tip content="30y minus 5y"><span>5s30s</span></Tip></th>
            <th className="px-2 text-right font-medium">{tenorLabel(t)} vs Bund</th>
            <th className="px-2 text-right font-medium">{tenorLabel(t)} vs UST</th>
            <th className="px-2 text-right font-medium">1d</th>
            <th className="px-2 text-right font-medium">1w</th>
            <th className="px-2 text-right font-medium">1m</th>
            <th className="px-3 text-right font-medium">YTD</th>
          </tr>
        </thead>
        <tbody>
          {d.countries.map((c) => (
            <tr
              key={c.curve_id}
              onClick={() => onSelect(c.curve_id)}
              className={cn('cursor-pointer border-b border-border/60 hover:bg-surface-2', selected === c.curve_id && 'bg-surface-3')}
            >
              <td className="px-3 py-1.5">
                <span className="inline-flex items-center gap-1.5">
                  <Flag country={c.country} />
                  <span className="font-medium text-fg">{c.label}</span>
                  <span className="text-faint">{c.type}</span>
                </span>
              </td>
              <td className="px-2 text-muted">{fmtDate(c.date)}</td>
              {[c.y2, c.y5, c.y10, c.y30].map((v, i) => (
                <td key={i} className="num px-2 text-right">{fmtRate(v)}</td>
              ))}
              <td className={cn('num px-2 text-right', fin(c.s2s10_bp) && c.s2s10_bp < 0 && 'text-down')}>{fmtBp(c.s2s10_bp, false)}</td>
              <td className={cn('num px-2 text-right', fin(c.s5s30_bp) && c.s5s30_bp < 0 && 'text-down')}>{fmtBp(c.s5s30_bp, false)}</td>
              <td className="num px-2 text-right">{fmtBp(c.spread_vs_bund_bp, false)}</td>
              <td className="num px-2 text-right">{fmtBp(c.spread_vs_ust_bp, false)}</td>
              {[c.chg_1d_bp, c.chg_1w_bp, c.chg_1m_bp, c.chg_ytd_bp].map((v, i) => (
                <td key={i} className={cn('num px-2 text-right', i === 3 && 'pr-3', fin(v) && v > 0 ? 'text-down' : fin(v) && v < 0 ? 'text-up' : 'text-muted')}>{fmtBp(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-1.5 text-2xs text-faint">Yield changes: rising yields in red, falling in green (bond-price perspective). Spain / Portugal are average market yields by residual maturity, not fitted curves.</p>
    </div>
  )
}

/* ───────────── curve chart + history slider / animation ───────────── */

function CurvePanel({ curve, overlays, onOverlays, onCurve }: { curve: string; overlays: string[]; onOverlays: (v: string[]) => void; onCurve: (c: string) => void }) {
  const cat = useRatesCurves()
  const meta = cat.data?.items.find((c) => c.curve_id === curve)
  const [type, setType] = useState<CurveType | null>(null)
  const typ = type && meta?.types.includes(type) ? type : ((meta?.monitor_type as CurveType | undefined) ?? undefined)
  const [cmp, setCmp] = useState<string[]>(['1m', '1y'])
  const main = useRatesCurve(curve, { type: typ, compare: cmp.join(','), frames: 'M' })
  const o1 = useRatesCurve(overlays[0] ?? null, { frames: 'none', compare: '' })
  const o2 = useRatesCurve(overlays[1] ?? null, { frames: 'none', compare: '' })
  const o3 = useRatesCurve(overlays[2] ?? null, { frames: 'none', compare: '' })
  const others = [o1.data, o2.data, o3.data].filter((x): x is RatesCurve => !!x)
  const groups = useMemo(() => groupCurves(cat.data?.items ?? []), [cat.data])
  const options = groups.flatMap((g) => g.items.map((c) => ({ value: c.curve_id, label: `${g.label.startsWith('Country') ? '' : '· '}${c.label}` })))
  const [addSel, setAddSel] = useState('')
  return (
    <Panel
      title={meta ? `${meta.label}` : 'Curve'}
      subtitle={meta ? `${KIND_LABEL[meta.kind] ?? meta.kind} · ${meta.source} · history from ${fmtDate(meta.first_date)} · published ${meta.publish}` : undefined}
      actions={
        <>
          {options.length ? <Select label="Curve" value={curve} onChange={onCurve} options={options} className="w-64" /> : null}
          {meta && meta.types.length > 1 ? (
            <Segmented label="Measure" size="xs" value={(typ ?? meta.types[0]) as CurveType} onChange={(v) => setType(v)} options={meta.types.map((t) => ({ value: t as CurveType, label: TYPE_LABEL[t as CurveType] ?? t }))} />
          ) : null}
        </>
      }
    >
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Compare with</span>
        {COMPARE.map((c) => (
          <button
            key={c}
            type="button"
            aria-pressed={cmp.includes(c)}
            onClick={() => setCmp((x) => (x.includes(c) ? x.filter((y) => y !== c) : [...x, c]))}
            className={cn('h-6 rounded-md border px-2', cmp.includes(c) ? 'border-accent bg-surface-3 text-fg' : 'border-border text-muted hover:text-fg')}
          >
            {SNAP_LABEL[c]}
          </button>
        ))}
        <span className="ml-3 text-muted">Overlay countries</span>
        {overlays.map((o) => (
          <Badge key={o} tone="neutral" className="normal-case">
            {o}
            <button type="button" aria-label={`Remove ${o}`} onClick={() => onOverlays(overlays.filter((x) => x !== o))}>
              <X />
            </button>
          </Badge>
        ))}
        {overlays.length < 3 && options.length ? (
          <span className="inline-flex items-center gap-1">
            <Select label="Add overlay" value={addSel} onChange={setAddSel} options={[{ value: '', label: 'Add a curve…' }, ...options.filter((o) => o.value !== curve && !overlays.includes(o.value))]} className="w-52" />
            <Button size="sm" variant="ghost" disabled={!addSel} onClick={() => { onOverlays([...overlays, addSel]); setAddSel('') }} aria-label="Add overlay">
              <Plus className="size-3.5" />
            </Button>
          </span>
        ) : null}
      </div>
      <QueryView query={main} skeleton={<Skeleton className="h-[340px]" />}>
        {(d) => <CurveBody d={d} others={others} />}
      </QueryView>
    </Panel>
  )
}

function CurveBody({ d, others }: { d: RatesCurve; others: RatesCurve[] }) {
  const t = useChartTokens()
  const frames = d.frames ?? null
  const n = frames?.dates.length ?? 0
  const [idx, setIdx] = useState<number | null>(null)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<'1' | '3' | '8'>('3')
  useEffect(() => {
    if (!playing || !n) return
    const h = setInterval(() => setIdx((i) => {
      const next = (i ?? 0) + Number(speed)
      if (next >= n - 1) {
        setPlaying(false)
        return n - 1
      }
      return next
    }), 120)
    return () => clearInterval(h)
  }, [playing, n, speed])
  const extent = useMemo(() => (frames ? framesExtent(frames) : null), [frames])
  const lines: CurveLine[] = []
  const latest = d.snapshots[0]
  if (idx !== null && frames && idx < n) {
    lines.push({ name: `${d.curve.label} · ${frames.dates[idx]}`, points: frameXY(frames, idx), color: t.accent, width: 2.4 })
    if (latest) lines.push({ name: `Latest · ${latest.date}`, points: curveXY(latest), ghost: true })
  } else {
    d.snapshots.forEach((s, i) => lines.push({ name: `${SNAP_LABEL[s.label] ?? s.label} · ${s.date}`, points: curveXY(s), color: i === 0 ? t.series[0] : undefined, dashed: i > 0, width: i === 0 ? 2.2 : 1.2 }))
    others.forEach((o, i) => o.snapshots[0] && lines.push({ name: `${o.curve.label} · ${o.snapshots[0].date}`, points: curveXY(o.snapshots[0]), color: t.series[(i + 1) % t.series.length] }))
  }
  return (
    <div className="space-y-2">
      <YieldCurveChart lines={lines} height={340} ariaLabel={`${d.curve.label} yield curve`} yExtent={idx !== null ? extent : null} />
      {frames && n > 1 ? (
        <div className="flex flex-wrap items-center gap-3 text-xs">
          <Button size="sm" variant="secondary" onClick={() => { if (idx === null || idx >= n - 1) setIdx(0); setPlaying((p) => !p) }} aria-label={playing ? 'Pause animation' : 'Play the curve history'}>
            {playing ? <Pause className="size-3.5" /> : <Play className="size-3.5" />}
            {playing ? 'Pause' : 'Animate'}
          </Button>
          <Segmented label="Animation speed" size="xs" value={speed} onChange={setSpeed} options={[{ value: '1', label: '1×' }, { value: '3', label: '3×' }, { value: '8', label: '8×' }]} />
          <div className="min-w-64 flex-1">
            <Slider label="Curve history date" min={0} max={n - 1} step={1} value={[idx ?? n - 1]} onValueChange={(v) => { setPlaying(false); setIdx(v[0]) }} />
          </div>
          <span className="num w-24 text-right text-fg">{idx !== null ? frames.dates[idx] : frames.dates[n - 1]}</span>
          <input
            type="date"
            aria-label="Jump to date"
            className="h-6 rounded-md border border-border bg-surface-2 px-1 text-xs"
            min={frames.dates[0]}
            max={frames.dates[n - 1]}
            onChange={(e) => e.target.value && setIdx(frameIndexAt(frames, e.target.value))}
          />
          {idx !== null ? (
            <Button size="sm" variant="ghost" onClick={() => { setPlaying(false); setIdx(null) }}>Back to latest</Button>
          ) : null}
          <span className="text-2xs text-faint">{n} {frames.frequency === 'M' ? 'month-end' : 'week-end'} curves since {frames.dates[0]}</span>
        </div>
      ) : null}
      {d.params ? (
        <p className="text-2xs text-muted">
          Svensson parameters ({d.params.source}, {d.params.date}): β0 {d.params.beta0} · β1 {d.params.beta1} · β2 {d.params.beta2} · β3 {d.params.beta3} · τ1 {d.params.tau1} · τ2 {d.params.tau2} — full curve reconstructed; spot rates continuously compounded.
        </p>
      ) : null}
      {d.curve.notes ? <p className="text-2xs text-faint">{d.curve.notes}</p> : null}
    </div>
  )
}

/* ───────────── spreads / slopes ───────────── */

function SpreadPanel({ curve, mon }: { curve: string; mon: RatesMonitor | undefined }) {
  const [range, setRange] = useState<Range>('5Y')
  const row = mon?.countries.find((c) => c.curve_id === curve)
  const bund = mon?.countries.find((c) => c.country === 'DE')
  const ust = mon?.countries.find((c) => c.country === 'US')
  const type = row?.type ?? 'par'
  const from = isoYearsAgo(RANGE_YEARS[range])
  const slopes = useRatesSeries([], row ? slopeSpreads(curve, type) : [], from)
  const spreads = useRatesSeries([], row ? defaultSpreads(row, bund, ust) : [], from)
  const levels = useRatesSeries(row ? [seriesId(curve, 24, type), seriesId(curve, 120, type)] : [], [], from)
  return (
    <Panel
      title="Slopes, spreads and levels"
      subtitle={row ? `${row.label}: 2s10s and 5s30s, 10y spread vs the Bund and the UST, 2y / 10y levels (weekly beyond 3 years)` : 'Pick a country benchmark curve in the table'}
      actions={<Segmented label="Range" size="xs" value={range} onChange={setRange} options={Object.keys(RANGE_YEARS).map((k) => ({ value: k as Range, label: k }))} />}
    >
      {!row ? (
        <EmptyState title="Not a country benchmark" message="Spreads are shown for the country benchmark curves of the table." />
      ) : (
        <div className="grid gap-3 xl:grid-cols-3">
          <div>
            <div className="label-caps mb-1">Curve slopes (bp)</div>
            <QueryView query={slopes} skeleton={<Skeleton className="h-[220px]" />}>
              {(d) => <TimeSeriesChart series={d.series.map((s, i) => ({ name: i === 0 ? '2s10s' : '5s30s', data: s.points }))} format={bp} zeroLine ariaLabel="Curve slopes" />}
            </QueryView>
          </div>
          <div>
            <div className="label-caps mb-1">10y spreads (bp)</div>
            <QueryView query={spreads} skeleton={<Skeleton className="h-[220px]" />} isEmpty={(d) => !d.series.length}>
              {(d) => <TimeSeriesChart series={d.series.map((s) => ({ name: s.id.includes('DE_BUND') ? 'vs Bund' : 'vs UST', data: s.points }))} format={bp} zeroLine ariaLabel="10y spreads" />}
            </QueryView>
          </div>
          <div>
            <div className="label-caps mb-1">Levels (%)</div>
            <QueryView query={levels} skeleton={<Skeleton className="h-[220px]" />}>
              {(d) => <TimeSeriesChart series={d.series.map((s, i) => ({ name: i === 0 ? '2y' : '10y', data: s.points }))} format={pct} ariaLabel="2y and 10y yields" />}
            </QueryView>
          </div>
        </div>
      )}
    </Panel>
  )
}

/* ───────────── real vs nominal, money markets, LT10 ───────────── */

function RealPanel({ q }: { q: ReturnType<typeof useRatesMonitor> }) {
  const [range] = useState<Range>('10Y')
  const be = useRatesSeries([], ['US_TSY:120:par-US_TSY_REAL:120:par', 'GB_GILT:120:spot-GB_GILT_REAL:120:spot'], isoYearsAgo(RANGE_YEARS[range]))
  return (
    <Panel title="Real vs nominal" subtitle="Inflation-linked curves (US TIPS par, UK index-linked spot) and the breakeven = nominal − real">
      <QueryView query={q} skeleton={<SkeletonRows rows={6} />}>
        {(d) =>
          d.real_vs_nominal.length ? (
            <div className="space-y-3">
              <table className="w-full text-xs">
                <thead className="text-2xs text-muted">
                  <tr className="border-b border-border">
                    <th className="py-1 text-left font-medium">Country</th>
                    <th className="text-left font-medium">Tenor</th>
                    <th className="text-right font-medium">Nominal</th>
                    <th className="text-right font-medium">Real</th>
                    <th className="text-right font-medium">Breakeven</th>
                  </tr>
                </thead>
                <tbody>
                  {d.real_vs_nominal.map((r) => (
                    <tr key={`${r.country}${r.tenor_months}`} className="border-b border-border/60">
                      <td className="py-1"><Flag country={r.country} /> {r.country}</td>
                      <td>{tenorLabel(r.tenor_months)}</td>
                      <td className="num text-right">{fmtRate(r.nominal)}</td>
                      <td className="num text-right">{fmtRate(r.real)}</td>
                      <td className="num text-right">{fmtBp(r.breakeven_bp, false)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <QueryView query={be} skeleton={<Skeleton className="h-[200px]" />}>
                {(s) => <TimeSeriesChart height={200} series={s.series.map((x, i) => ({ name: i === 0 ? 'US 10y breakeven' : 'UK 10y breakeven (RPI)', data: x.points }))} format={bp} ariaLabel="10y breakevens" />}
              </QueryView>
            </div>
          ) : (
            <EmptyState title="No real curves yet" />
          )
        }
      </QueryView>
    </Panel>
  )
}

function MoneyPanel({ q }: { q: ReturnType<typeof useRatesMonitor> }) {
  return (
    <Panel title="Money markets" subtitle="Official overnight benchmarks (NY Fed, ECB, BoE, BoJ, BoC) and policy rates; EURIBOR via Banco de España" flush>
      <QueryView query={q} skeleton={<SkeletonRows rows={8} className="p-3" />}>
        {(d) =>
          d.money_market.length ? (
            <table className="w-full text-xs">
              <thead className="text-2xs text-muted">
                <tr className="border-b border-border">
                  <th className="px-3 py-1 text-left font-medium">Rate</th>
                  <th className="text-left font-medium">Date</th>
                  <th className="text-right font-medium">Value</th>
                  <th className="text-right font-medium">1d</th>
                  <th className="px-3 text-right font-medium">1m</th>
                </tr>
              </thead>
              <tbody>
                {d.money_market.map((m) => (
                  <tr key={m.series_id} className="border-b border-border/60">
                    <td className="px-3 py-1"><Flag country={m.country === 'EA' ? 'EU' : m.country} /> {m.label}</td>
                    <td className="text-muted">{fmtDate(m.date)}</td>
                    <td className="num text-right">{fmtRate(m.value, 3)}</td>
                    <td className="num text-right">{fmtBp(m.chg_1d_bp)}</td>
                    <td className="num px-3 text-right">{fmtBp(m.chg_1m_bp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState className="m-3" title="No fixings yet" message="Money-market series arrive with the macro jobs (macro_national / macro_eu)." />
          )
        }
      </QueryView>
    </Panel>
  )
}

function Lt10Panel({ q }: { q: ReturnType<typeof useRatesMonitor> }) {
  return (
    <Panel title="10-year yields, rest of the world (monthly)" subtitle="ECB convergence yields for the EU27 (incl. France and Italy), OECD long-term rates otherwise · spread vs the latest Bund 10y">
      <QueryView query={q} skeleton={<Skeleton className="h-[200px]" />}>
        {(d) =>
          d.lt10.length ? (
            <Bars
              categories={d.lt10.map((r) => r.country)}
              series={[{ name: '10y yield', data: d.lt10.map((r: RatesMonitor['lt10'][number]) => r.y10 ?? null) }]}
              format={(v) => `${v.toFixed(1)}%`}
              ariaLabel="Monthly 10-year government yields by country"
              height={210}
            />
          ) : (
            <EmptyState title="No monthly 10y data yet" />
          )
        }
      </QueryView>
    </Panel>
  )
}

/* ───────────── sovereign debt ───────────── */

function SovereignPanel() {
  const q = useRatesSovereign()
  const [debtM, setDebtM] = useState<'debt_gdp' | 'deficit_gdp'>('debt_gdp')
  return (
    <Panel title="Sovereign debt" subtitle="Auction calendar and results (US Treasury Fiscal Data, UK DMO), debt / GDP and deficits (Eurostat, IMF WEO), TARGET balances (ECB)">
      <QueryView query={q} skeleton={<SkeletonRows rows={10} />}>
        {(d) => (
          <div className="grid gap-3 xl:grid-cols-2">
            <div className="min-w-0 space-y-2">
              <div className="label-caps">Upcoming auctions</div>
              <AuctionTable rows={d.auctions_upcoming.slice(0, 12)} upcoming />
              <div className="label-caps pt-2">Recent results</div>
              <AuctionTable rows={d.auctions_recent.slice(0, 12)} />
            </div>
            <div className="min-w-0 space-y-3">
              {d.us_10y_auctions.length ? (
                <div>
                  <div className="label-caps mb-1">US 10-year note auctions: stop-out yield</div>
                  <TimeSeriesChart height={170} series={[{ name: 'High yield', data: d.us_10y_auctions.map((a) => ({ date: a.date, value: a.high_yield ?? null })) }]} format={pct} ariaLabel="US 10-year auction yields" />
                  <div className="label-caps mt-2 mb-1">Bid-to-cover</div>
                  <TimeSeriesChart height={140} series={[{ name: 'Bid-to-cover', data: d.us_10y_auctions.map((a) => ({ date: a.date, value: a.bid_to_cover ?? null })) }]} format={(v) => `${v.toFixed(2)}×`} ariaLabel="US 10-year bid-to-cover" />
                </div>
              ) : null}
              <div>
                <div className="mb-1 flex items-center gap-2">
                  <span className="label-caps">General government</span>
                  <Segmented label="Measure" size="xs" value={debtM} onChange={setDebtM} options={[{ value: 'debt_gdp', label: 'Debt / GDP' }, { value: 'deficit_gdp', label: 'Balance / GDP' }]} />
                </div>
                <DebtBars rows={d.debt.filter((x) => x.measure === debtM)} />
              </div>
              {d.target2.length ? (
                <div>
                  <div className="label-caps mb-1">TARGET balances (EUR bn)</div>
                  <TimeSeriesChart
                    height={180}
                    series={Object.entries(groupBy(d.target2, (x) => x.country)).slice(0, 4).map(([cc, rows]) => ({ name: cc, data: rows.map((r) => ({ date: r.date, value: fin(r.value_eur_mn) ? r.value_eur_mn / 1000 : null })) }))}
                    format={(v) => `${v.toFixed(0)}`}
                    zeroLine
                    ariaLabel="TARGET balances"
                  />
                </div>
              ) : null}
            </div>
          </div>
        )}
      </QueryView>
    </Panel>
  )
}

function groupBy<T>(rows: T[], key: (r: T) => string): Record<string, T[]> {
  const out: Record<string, T[]> = {}
  for (const r of rows) (out[key(r)] ??= []).push(r)
  return out
}

function DebtBars({ rows }: { rows: { country: string; source: string; date: string; value: number | null }[] }) {
  const latest = Object.values(groupBy(rows, (r) => r.country)).map((rs) => rs.sort((a, b) => (a.source === 'eurostat' ? -1 : 1) - (b.source === 'eurostat' ? -1 : 1))[0])
  latest.sort((a, b) => (b.value ?? 0) - (a.value ?? 0))
  if (!latest.length) return <EmptyState title="No debt data yet" />
  return <Bars categories={latest.map((r) => r.country)} series={[{ name: '% of GDP', data: latest.map((r) => r.value) }]} format={(v) => `${v.toFixed(0)}%`} ariaLabel="Government debt by country" height={200} />
}

function AuctionTable({ rows, upcoming = false }: { rows: RatesSovereignAuction[]; upcoming?: boolean }) {
  if (!rows.length) return <p className="text-xs text-muted">{upcoming ? 'No announced auction in the calendar.' : 'No result in the last 45 days.'}</p>
  return (
    <table className="w-full text-xs">
      <thead className="text-2xs text-muted">
        <tr className="border-b border-border">
          <th className="py-1 text-left font-medium">Date</th>
          <th className="text-left font-medium">Issuer</th>
          <th className="text-left font-medium">Security</th>
          <th className="text-right font-medium">{upcoming ? 'Offering' : 'Accepted'}</th>
          {!upcoming && <th className="text-right font-medium">Yield</th>}
          {!upcoming && <th className="text-right font-medium">Cover</th>}
        </tr>
      </thead>
      <tbody>
        {rows.map((a) => (
          <tr key={`${a.agency}${a.auction_date}${a.security_id ?? a.security_term}`} className="border-b border-border/60">
            <td className="py-1 text-muted">{fmtDate(a.auction_date)}</td>
            <td><Flag country={a.country} /> {a.agency === 'us_treasury' ? 'UST' : 'DMO'}</td>
            <td className="truncate">{a.security_type} {a.security_term ?? ''}</td>
            <td className="num text-right">{fmtCompact(upcoming ? a.offering_amount : a.accepted_amount)}</td>
            {!upcoming && <td className="num text-right">{fmtRate(a.high_yield, 3)}</td>}
            {!upcoming && <td className="num text-right">{fin(a.bid_to_cover) ? `${a.bid_to_cover.toFixed(2)}×` : DASH}</td>}
          </tr>
        ))}
      </tbody>
    </table>
  )
}
type RatesSovereignAuction = import('@/api/rates/types').RatesAuction

function SourcesNote() {
  const cat = useRatesCurves()
  return (
    <p className="text-2xs text-muted">
      Sources: official only — US Treasury (daily par / real curves), Federal Reserve H.15 (via FRED), ECB Data Portal (euro area AAA / all-issuer Svensson parameters, IRS convergence yields, EUR STR, TARGET balances),
      Deutsche Bundesbank (Svensson parameters), Bank of England yield-curve archive, Ministry of Finance Japan, Banco de España, Banco de Portugal BPstat (IGCP OT yields), SNB, Bank of Canada, RBA, Riksbank,
      Norges Bank, NY Fed Markets API, BIS, OECD, Eurostat, IMF, US Treasury Fiscal Data, UK DMO. Stored as integer basis points × 100. Delayed live yields are Yahoo market data (personal use).
      {cat.data?.rejected_sources.length ? ` Not used: ${cat.data.rejected_sources.map((r) => r.source).join('; ')} (reasons on the System page / docs/it/DATA_LICENSING.md).` : ''}
    </p>
  )
}

export type { RatesMonitorCountry }
