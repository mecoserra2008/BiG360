import { Pause, Pin, Play, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { useMacroTheme } from '@/api/macro/hooks'
import { type MCompareItem, type MCurve, type MThemePanel, type MThemeSeries, type MacroTheme, THEMES } from '@/api/macro/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select, Slider } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroLines } from './charts'
import { fmtMacro } from './fmt'
import { MacroNav, SeriesDrawer, SourceLine } from './shared'

const isTheme = (t: string | undefined): t is MacroTheme => !!t && THEMES.some((x) => x.id === t)

export default function Theme() {
  const { theme } = useParams()
  const navigate = useNavigate()
  const th: MacroTheme = isTheme(theme) ? theme : 'growth'
  const [years, setYears] = useState('10')
  const q = useMacroTheme(th, Number(years))
  const [series, setSeries] = useState<string | null>(null)
  return (
    <>
      <PageHeader title="Macro themes" subtitle="Official series by theme · compare countries · curves · derived liquidity" />
      <MacroNav />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Segmented label="Theme" value={th} onChange={(v) => navigate(`/macro/theme/${v}`)} options={THEMES.map((x) => ({ value: x.id, label: x.label }))} />
        <Select label="History" value={years} onChange={setYears} options={[{ value: '3', label: '3 years' }, { value: '5', label: '5 years' }, { value: '10', label: '10 years' }, { value: '20', label: '20 years' }]} />
      </div>
      <QueryView query={q} skeleton={<Skeleton className="h-[600px]" />}>
        {(d) => (
          <div className="@container grid gap-3 @5xl:grid-cols-2">
            {d.panels.map((p) => (
              <PanelView key={p.id} p={p} onOpen={setSeries} />
            ))}
          </div>
        )}
      </QueryView>
      <SeriesDrawer seriesId={series} onClose={() => setSeries(null)} />
    </>
  )
}

function PanelView({ p, onOpen }: { p: MThemePanel; onOpen: (id: string) => void }) {
  if (p.kind === 'curve') return <CurvePanel p={p} />
  if (p.kind === 'compare') return <ComparePanel p={p} onOpen={onOpen} />
  return <SeriesPanel p={p} onOpen={onOpen} />
}

function SeriesPanel({ p, onOpen }: { p: MThemePanel; onOpen: (id: string) => void }) {
  const ser = ((p.series ?? []) as MThemeSeries[]).filter((s) => s.available !== false)
  const units = [...new Set(ser.map((s) => s.units))]
  const usOnly = ser.length > 0 && ser.every((s) => /^(fred|nyfed|derived:us)/.test(s.series_id))
  const many = ser.length > 4
  return (
    <Panel
      title={p.title}
      subtitle={p.kind === 'derived' ? <span className="num">{(p.series?.[0] as MThemeSeries | undefined)?.formula ?? 'derived from official series'}</span> : `${ser.length} official series${units.length === 1 && units[0] ? ` · ${units[0]}` : ''}`}
      className={cn(many && '@5xl:col-span-2')}
    >
      {ser.length === 0 ? (
        <p className="py-8 text-center text-xs text-muted">No series available.</p>
      ) : (
        <>
          <MacroLines
            lines={ser.map((s) => ({ name: s.label ?? s.title ?? s.series_id, points: s.points }))}
            units={units.length === 1 ? units[0] : null}
            height={260}
            ariaLabel={p.title}
            shadeUsRecessions={usOnly}
          />
          <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
            {ser.map((s, k) => (
              <li key={`${s.series_id}-${k}`}>
                <button type="button" className="text-left hover:underline" onClick={() => !s.series_id.startsWith('derived:') && onOpen(s.series_id)}>
                  <SourceLine sourceId={s.source_id} seriesId={s.series_id} />
                </button>
              </li>
            ))}
          </ul>
          {p.kind === 'derived' && <Badge tone="info" className="mt-1">derived from official inputs</Badge>}
        </>
      )}
    </Panel>
  )
}

const DEFAULT_PICK = ['US', 'EA', 'GB', 'JP']

function ComparePanel({ p, onOpen }: { p: MThemePanel; onOpen: (id: string) => void }) {
  const items = ((p.series ?? []) as MCompareItem[]).filter((s) => s.available !== false && s.points.length)
  const [pick, setPick] = useState<string[]>(() => {
    const d = DEFAULT_PICK.filter((c) => items.some((s) => s.country === c))
    return d.length ? d : items.slice(0, 4).map((s) => s.country)
  })
  const t = useChartTokens()
  const latest = useMemo(() => [...items].filter((s) => typeof s.latest?.value === 'number').sort((a, b) => (b.latest?.value as number) - (a.latest?.value as number)), [items])
  const bar = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 36, top: 4, bottom: 4, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const s = latest[(ps as { dataIndex: number }[])[0].dataIndex]
          return `${ttTitle(`${s.name ?? s.country} · ${s.latest?.period_label ?? ''}`)}${ttRow(t.series[0], 'latest', `${fmtMacro(s.latest?.value)} ${s.units ?? ''}`)}${ttRow(t.ref, 'source', `${s.latest?.source_id ?? ''} ${s.series_id}`)}`
        },
      },
      xAxis: { type: 'value', axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
      yAxis: { type: 'category', inverse: true, data: latest.map((s) => s.country), axisTick: { show: false }, axisLabel: { fontSize: 10, color: t.fg } },
      series: [
        {
          type: 'bar',
          barMaxWidth: 10,
          data: latest.map((s) => ({ value: s.latest?.value, itemStyle: { color: pick.includes(s.country) ? t.series[pick.indexOf(s.country) % t.series.length] : t.ref } })),
          label: { show: true, position: 'right', fontSize: 9, color: t.muted, formatter: (x: unknown) => fmtMacro((x as { value: number }).value, 3) },
        },
      ],
    }) as EChartsOption,
    [latest, pick, t],
  )
  const sel = items.filter((s) => pick.includes(s.country))
  return (
    <Panel title={p.title} subtitle={`${items.length} countries · latest print (grey = not charted) · up to 4 countries on the chart`} className="@5xl:col-span-2">
      <div className="grid gap-3 @3xl:grid-cols-[minmax(200px,1fr)_minmax(0,2fr)]">
        <EChart option={bar} height={Math.max(160, latest.length * 16 + 10)} ariaLabel={`${p.title}: latest values by country`} />
        <div className="min-w-0">
          <div className="mb-1 flex flex-wrap gap-1" role="group" aria-label="Countries on the chart">
            {items.map((s) => {
              const on = pick.includes(s.country)
              return (
                <button
                  key={s.country}
                  type="button"
                  aria-pressed={on}
                  onClick={() => setPick(on ? pick.filter((x) => x !== s.country) : [...pick, s.country].slice(-4))}
                  className={cn('num h-5 rounded-sm border px-1.5 text-2xs', on ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:text-fg')}
                >
                  {s.country}
                </button>
              )
            })}
          </div>
          <MacroLines lines={sel.map((s) => ({ name: s.country, points: s.points }))} units={sel[0]?.units} height={240} ariaLabel={`${p.title}: history`} />
          <div className="mt-1 flex flex-wrap gap-x-3">
            {sel.map((s) => (
              <button key={s.country} type="button" className="hover:underline" onClick={() => s.series_id && onOpen(s.series_id)}>
                <SourceLine sourceId={s.latest?.source_id} seriesId={`${s.country}: ${s.series_id ?? DASH}`} />
              </button>
            ))}
          </div>
        </div>
      </div>
    </Panel>
  )
}

/* ───────────── animated yield curve ───────────── */

function CurvePanel({ p }: { p: MThemePanel }) {
  const curves = p.curves ?? []
  const [cid, setCid] = useState(curves[0]?.curve_id ?? '')
  const c = curves.find((x) => x.curve_id === cid) ?? curves[0]
  return (
    <Panel
      title={p.title}
      subtitle="Month-end curves from official yields · play or scrub through history, pin dates to compare"
      className="@5xl:col-span-2"
      actions={<Select label="Curve" value={c?.curve_id ?? ''} onChange={setCid} options={curves.map((x) => ({ value: x.curve_id, label: x.label }))} />}
    >
      {c ? <CurveAnimator key={c.curve_id} c={c} /> : <p className="py-8 text-center text-xs text-muted">No curve data.</p>}
    </Panel>
  )
}

function CurveAnimator({ c }: { c: MCurve }) {
  const t = useChartTokens()
  const last = c.dates.length - 1
  const [i, setI] = useState(last)
  const [playing, setPlaying] = useState(false)
  const yearAgo = Math.max(0, last - 12)
  const [pins, setPins] = useState<number[]>(() => [yearAgo])
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => setI((k) => (k >= last ? (setPlaying(false), last) : k + 1)), 260)
    return () => clearInterval(id)
  }, [playing, last])
  const all = c.matrix.flat().filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
  const lo = Math.min(...all)
  const hi = Math.max(...all)
  const option = useMemo<EChartsOption>(() => {
    const row = (k: number) => c.tenors.map((_, j) => c.matrix[k]?.[j] ?? null)
    const lines = [
      { name: fmtDate(c.dates[i]), data: row(i), color: t.series[0], width: 2.2, dashed: false },
      ...pins.filter((k) => k !== i).map((k, n) => ({ name: fmtDate(c.dates[k]), data: row(k), color: [t.ref, t.series[1], t.series[2]][n % 3], width: 1.3, dashed: true })),
    ]
    return {
      grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0 },
      animationDurationUpdate: 200,
      tooltip: { trigger: 'axis', valueFormatter: (v) => `${fmtMacro(v as number, 3)}%` },
      xAxis: { type: 'category', data: c.tenors.map((x) => x.tenor), boundaryGap: false },
      yAxis: { type: 'value', min: Math.floor(lo - 0.25), max: Math.ceil(hi + 0.25), name: 'yield %', nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' }, axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
      series: lines.map((l) => ({ type: 'line', name: l.name, data: l.data, color: l.color, symbolSize: 5, lineStyle: { color: l.color, width: l.width, type: l.dashed ? 'dashed' : 'solid' }, connectNulls: true })),
    } as EChartsOption
  }, [c, i, pins, lo, hi, t])
  return (
    <div>
      <EChart option={option} height={300} ariaLabel={`${c.label} yield curve on ${c.dates[i]}`} />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" onClick={() => (i >= last && !playing ? (setI(0), setPlaying(true)) : setPlaying(!playing))} aria-label={playing ? 'Pause' : 'Play'}>
          {playing ? <Pause /> : <Play />} {playing ? 'Pause' : 'Play'}
        </Button>
        <Slider label="Curve date" className="min-w-48 flex-1" min={0} max={last} step={1} value={[i]} onValueChange={(v) => setI(v[0])} />
        <span className="num w-24 text-sm text-fg">{fmtDate(c.dates[i])}</span>
        <Button size="sm" variant="ghost" onClick={() => setPins([...new Set([...pins, i])].slice(-3))} title="Pin this date for comparison">
          <Pin /> Pin date
        </Button>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-1 text-2xs text-muted">
        compare:
        {pins.map((k) => (
          <span key={k} className="num inline-flex h-5 items-center gap-0.5 rounded-sm border border-border bg-surface-2 pl-1.5">
            {fmtDate(c.dates[k])}
            <button type="button" aria-label={`Unpin ${c.dates[k]}`} className="px-0.5 text-faint hover:text-down" onClick={() => setPins(pins.filter((x) => x !== k))}>
              <X className="size-3" />
            </button>
          </span>
        ))}
        {pins.length === 0 && DASH}
        <span className="ml-2">tenors from {c.tenors.map((x) => x.series_id.split(':')[0]).filter((v, k, a) => a.indexOf(v) === k).join(', ')} (official)</span>
      </div>
    </div>
  )
}
