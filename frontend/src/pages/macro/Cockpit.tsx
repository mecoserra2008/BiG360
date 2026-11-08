import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { cellIndex, groupCalendar, orientedZ, recessionGrid, zBucket } from '@/api/macro/adapters'
import { useMacroCalendar, useMacroCycle, useMacroHeatmap, useMacroRecession, useMacroSurprise } from '@/api/macro/hooks'
import type { MCycle, MHeatmap, MRecession, MRecessionRow, SurpriseTheme } from '@/api/macro/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { LiveMacroTiles } from '@/components/live/LiveMacroTiles'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { METHOD_SHORT, QUADRANT_LABEL, fmtChange, fmtMacro, fmtProb, zStyle, isoDaysFromNow } from './fmt'
import { ReleaseList } from './releases'
import { CellTooltip, DirArrow, MacroNav, SeriesDrawer } from './shared'

export default function Cockpit() {
  const [series, setSeries] = useState<string | null>(null)
  return (
    <>
      <PageHeader title="Macro" subtitle="Cockpit: where each economy stands, where the cycle is heading, recession risk and what is being released" />
      <MacroNav />
      <div className="@container space-y-3">
        <LiveMacroTiles />
        <HeatmapPanel onOpen={setSeries} />
        <div className="grid gap-3 @5xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
          <CyclePanel />
          <SurprisePanel />
        </div>
        <RecessionPanel />
        <UpcomingPanel />
      </div>
      <SeriesDrawer seriesId={series} onClose={() => setSeries(null)} />
    </>
  )
}

/* ───────────── heatmap ───────────── */

function HeatmapPanel({ onOpen }: { onOpen: (id: string) => void }) {
  const q = useMacroHeatmap()
  return (
    <Panel
      title="Macro heatmap"
      subtitle="24 economies × 23 indicators · colour = z-score vs the country's own 10-year history, oriented so rose is unfavourable and teal favourable · arrow = 3-month direction"
      flush
    >
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-96" />}>
        {(h) => <Heatmap h={h} onOpen={onOpen} />}
      </QueryView>
    </Panel>
  )
}

function Heatmap({ h, onOpen }: { h: MHeatmap; onOpen: (id: string) => void }) {
  const navigate = useNavigate()
  const idx = useMemo(() => cellIndex(h), [h])
  const groups = useMemo(() => {
    const out: { group: string; n: number }[] = []
    for (const ind of h.indicators) {
      const g = ind.group ?? ''
      if (out.length && out[out.length - 1].group === g) out[out.length - 1].n++
      else out.push({ group: g, n: 1 })
    }
    return out
  }, [h.indicators])
  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full border-separate border-spacing-0 text-xs" aria-label="Macro heatmap: countries by indicators">
          <thead>
            <tr>
              <th className="sticky left-0 z-[2] bg-surface" />
              {groups.map((g) => (
                <th key={g.group} colSpan={g.n} className="border-b border-border px-1 pt-1 text-center text-2xs font-semibold tracking-wide text-muted uppercase">
                  {g.group}
                </th>
              ))}
            </tr>
            <tr>
              <th className="sticky left-0 z-[2] bg-surface px-2 text-left text-2xs text-muted">Country</th>
              {h.indicators.map((ind) => (
                <th key={ind.key} className="h-20 min-w-[52px] align-bottom" title={`${ind.label}${ind.good_direction ? (ind.good_direction > 0 ? ' · higher is better' : ' · lower is better') : ' · neutral (no good/bad direction)'}`}>
                  <span className="mx-auto block w-4 origin-bottom-left translate-x-3 -rotate-45 pb-1 text-left text-2xs font-medium whitespace-nowrap text-muted">{ind.label}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {h.countries.map((c) => (
              <tr key={c.country}>
                <th scope="row" className="sticky left-0 z-[1] bg-surface px-2 py-0.5 text-left font-medium whitespace-nowrap">
                  <button type="button" className="inline-flex items-center gap-1.5 text-fg hover:text-accent-text hover:underline" onClick={() => navigate(`/macro/country/${c.country}`)}>
                    <Flag country={c.country} />
                    <span className="num">{c.country}</span>
                    <span className="hidden max-w-24 truncate text-muted @5xl:inline">{c.name}</span>
                  </button>
                </th>
                {h.indicators.map((ind) => {
                  const cell = idx.get(`${c.country}|${ind.key}`)
                  if (!cell) return <td key={ind.key} className="border border-surface p-0 text-center text-faint" aria-label={`${c.country} ${ind.label}: no data`}>·</td>
                  const b = zBucket(orientedZ(cell))
                  return (
                    <td key={ind.key} className="border border-surface p-0">
                      <Tip content={<CellTooltip c={cell} />}>
                        <button
                          type="button"
                          onClick={() => onOpen(cell.series_id)}
                          aria-label={`${c.country} ${ind.label} ${fmtMacro(cell.value)} ${cell.units ?? ''}`}
                          className={cn('num flex h-7 w-full items-center justify-center gap-0.5 px-0.5 text-2xs text-fg focus-visible:outline-2 focus-visible:outline-focus', cell.status === 'stale' && 'opacity-60')}
                          style={zStyle(b)}
                        >
                          {fmtMacro(cell.value, 3)}
                          <DirArrow d={cell.direction} className="size-2.5 text-muted" />
                        </button>
                      </Tip>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-3 py-2 text-2xs text-muted">
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-6 rounded-sm" style={zStyle(-3)} /> unfavourable (oriented z ≤ −1.67σ)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-6 rounded-sm" style={zStyle(0)} /> normal
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-6 rounded-sm" style={zStyle(3)} /> favourable (oriented z ≥ +1.67σ)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="inline-block h-3 w-6 rounded-sm border border-border" style={zStyle(null)} /> neutral indicator (rates, money, FX)
        </span>
        <span>faded = stale · · = no official series</span>
        <span className="ml-auto num">
          snapshot {fmtDate(h.snapshot_date)} · {h.coverage.n_cells}/{h.coverage.n_possible} cells · method {h.method_version}
        </span>
      </div>
    </div>
  )
}

/* ───────────── cycle clock ───────────── */

function CyclePanel() {
  const q = useMacroCycle()
  const [trails, setTrails] = useState(true)
  return (
    <Panel
      title="Cycle clock"
      subtitle="Growth momentum × inflation momentum · 12-month trails · click a country"
      actions={
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <Switch label="Show 12-month trails" checked={trails} onCheckedChange={setTrails} /> Trails
        </label>
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
        {(c) => <CycleClock c={c} trails={trails} />}
      </QueryView>
    </Panel>
  )
}

const QUAD_INDEX: Record<string, number> = { recovery: 1, expansion: 0, slowdown: 2, contraction: 3 }

function CycleClock({ c, trails }: { c: MCycle; trails: boolean }) {
  const t = useChartTokens()
  const navigate = useNavigate()
  const placed = c.countries.filter((x) => typeof x.growth_momentum === 'number' && typeof x.inflation_momentum === 'number' && x.quadrant)
  const option = useMemo<EChartsOption>(() => {
    const all = placed.flatMap((x) => [x.growth_momentum as number, x.inflation_momentum as number, ...(trails ? x.trail.flatMap((p) => [p.growth_momentum ?? 0, p.inflation_momentum ?? 0]) : [])])
    const m = Math.max(0.5, ...all.map((v) => Math.abs(v))) * 1.1
    const quad = (x0: number, x1: number, y0: number, y1: number, label: string, col: string) => [
      { xAxis: x0, yAxis: y0, itemStyle: { color: alpha(col, 0.07) }, label: { show: true, formatter: label, position: 'insideTopLeft', color: t.muted, fontSize: 10 } },
      { xAxis: x1, yAxis: y1 },
    ]
    return {
      grid: { left: 8, right: 16, top: 12, bottom: 24, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { seriesName: string; data: { cc?: string } }
          const x = placed.find((k) => k.country === q.data?.cc)
          if (!x) return ''
          const inputs = Object.entries(x.inputs ?? {})
            .map(([k, v]) => ttRow(t.ref, k.replace(/_/g, ' '), fmtChange(v)))
            .join('')
          return `${ttTitle(`${x.name} · ${QUADRANT_LABEL[x.quadrant ?? ''] ?? '—'}`)}${ttRow(t.series[0], 'growth momentum', fmtChange(x.growth_momentum))}${ttRow(t.series[1], 'inflation momentum', fmtChange(x.inflation_momentum))}<div style="margin-top:4px;opacity:.75">inputs (z of 6-month change)</div>${inputs}<div style="margin-top:4px;opacity:.75">as of ${x.date ?? DASH}</div>`
        },
      },
      xAxis: { type: 'value', min: -m, max: m, name: 'growth momentum →', nameLocation: 'middle', nameGap: 22, axisLabel: { formatter: (v: number) => v.toFixed(1) } },
      yAxis: { type: 'value', min: -m, max: m, name: 'inflation momentum', nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' }, axisLabel: { formatter: (v: number) => v.toFixed(1) } },
      series: [
        ...(trails
          ? placed.map((x) => ({
              type: 'line',
              name: `${x.country} trail`,
              silent: true,
              showSymbol: false,
              data: [...x.trail.map((p) => [p.growth_momentum, p.inflation_momentum]), [x.growth_momentum, x.inflation_momentum]],
              lineStyle: { color: alpha(t.series[QUAD_INDEX[x.quadrant ?? 'expansion'] ?? 0], 0.35), width: 1 },
              z: 1,
            }))
          : []),
        {
          type: 'scatter',
          name: 'countries',
          symbolSize: 9,
          data: placed.map((x) => ({
            value: [x.growth_momentum, x.inflation_momentum],
            cc: x.country,
            itemStyle: { color: t.series[QUAD_INDEX[x.quadrant ?? 'expansion'] ?? 0], borderColor: t.surface, borderWidth: 1 },
          })),
          label: { show: true, position: 'right', formatter: (p: unknown) => (p as { data: { cc: string } }).data.cc, fontSize: 10, color: t.fg },
          labelLayout: { hideOverlap: true },
          markArea: {
            silent: true,
            data: [
              quad(0, m, 0, m, 'Expansion: growth ↑ inflation ↑', t.series[0]),
              quad(0, m, -m, 0, 'Recovery: growth ↑ inflation ↓', t.series[1]),
              quad(-m, 0, 0, m, 'Slowdown: growth ↓ inflation ↑', t.series[2]),
              quad(-m, 0, -m, 0, 'Contraction: growth ↓ inflation ↓', t.series[3]),
            ],
          },
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }, { yAxis: 0 }] },
          z: 3,
        },
      ],
    } as EChartsOption
  }, [placed, trails, t])
  const missing = c.countries.filter((x) => !x.quadrant).map((x) => x.country)
  return (
    <div>
      <EChart
        option={option}
        height={420}
        ariaLabel="Cycle clock: countries placed by growth and inflation momentum"
        onEvents={{
          click: (p: unknown) => {
            const cc = (p as { data?: { cc?: string } }).data?.cc
            if (cc) navigate(`/macro/country/${cc}`)
          },
        }}
      />
      <div className="mt-1 flex flex-wrap items-center gap-2 text-2xs text-muted">
        {Object.entries(c.quadrant_counts ?? {}).map(([k, v]) => (
          <span key={k}>
            {QUADRANT_LABEL[k] ?? k} <b className="num text-fg">{v}</b>
          </span>
        ))}
        {missing.length > 0 && <span>· not placed (missing growth inputs): {missing.join(', ')}</span>}
        <Tip content={<div className="max-w-sm">{c.method}</div>}>
          <span tabIndex={0} className="cursor-help underline decoration-dotted">
            method {c.method_version}
          </span>
        </Tip>
      </div>
    </div>
  )
}

/* ───────────── surprise snapshot ───────────── */

function SurprisePanel() {
  const [theme, setTheme] = useState<SurpriseTheme>('all')
  const q = useMacroSurprise(theme)
  const t = useChartTokens()
  const navigate = useNavigate()
  return (
    <Panel
      title="Surprise index"
      subtitle="Releases vs model-based expectations (not consensus) · EW index, 45-day half-life · + = better than our model expected"
      actions={
        <Segmented
          label="Surprise theme"
          size="xs"
          value={theme}
          onChange={setTheme}
          options={[
            { value: 'all', label: 'All' },
            { value: 'growth', label: 'Growth' },
            { value: 'inflation', label: 'Inflation' },
            { value: 'labor', label: 'Labor' },
          ]}
        />
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
        {(s) => {
          const rows = [...s.latest].filter((r) => typeof r.value === 'number').sort((a, b) => (b.value as number) - (a.value as number))
          const option: EChartsOption = {
            grid: { left: 8, right: 40, top: 4, bottom: 8, containLabel: true },
            tooltip: {
              trigger: 'axis',
              axisPointer: { type: 'shadow' },
              formatter: (ps: unknown) => {
                const p = (ps as { dataIndex: number }[])[0]
                const r = rows[p.dataIndex]
                return `${ttTitle(r.country)}${ttRow(t.series[0], 'surprise index', fmtMacro(r.value))}${ttRow(t.ref, 'releases in window', String(r.n_events ?? DASH))}${ttRow(t.ref, 'last release', r.last_event_date ? fmtDate(r.last_event_date) : DASH)}`
              },
            },
            xAxis: { type: 'value', axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
            yAxis: { type: 'category', inverse: true, data: rows.map((r) => r.country), axisTick: { show: false }, axisLabel: { color: t.fg, fontSize: 10 } },
            series: [
              {
                type: 'bar',
                barMaxWidth: 12,
                data: rows.map((r) => ({ value: r.value, itemStyle: { color: (r.value as number) >= 0 ? t.up : t.down } })),
                label: { show: true, position: 'right', formatter: (p: unknown) => fmtMacro((p as { value: number }).value, 2), fontSize: 9, color: t.muted },
                markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }] },
              },
            ],
          }
          return (
            <>
              <EChart
                option={option}
                height={Math.max(240, rows.length * 16 + 20)}
                ariaLabel={`Surprise index by country (${theme})`}
                onEvents={{
                  click: (p: unknown) => {
                    const cc = rows[(p as { dataIndex: number }).dataIndex]?.country
                    if (cc && cc !== 'GLOBAL') navigate(`/macro/country/${cc}`)
                  },
                }}
              />
              <p className="mt-1 text-2xs text-muted">{s.label}. Bars: teal = above our model in the direction of economic strength (for inflation: hotter than expected, not better), rose = below.</p>
            </>
          )
        }}
      </QueryView>
    </Panel>
  )
}

/* ───────────── recession panel ───────────── */


export function RecessionCell({ r }: { r: MRecessionRow | undefined }) {
  if (!r) return <span className="text-faint">{DASH}</span>
  const det = (r.details ?? {}) as Record<string, unknown>
  if (r.method === 'oecd_cli_turning_point') {
    const phase = String(det.phase ?? DASH)
    return (
      <span className="inline-flex items-center gap-1">
        <Badge tone={phase === 'expansion' ? 'up' : phase === 'downturn' || phase === 'contraction' ? 'down' : 'warn'}>{phase}</Badge>
        <span className="num text-2xs text-muted">CLI {fmtMacro(r.value, 4)}</span>
      </span>
    )
  }
  if (r.method === 'sahm_realtime')
    return (
      <span className="num inline-flex items-center gap-1">
        <span className={cn('font-semibold', r.signal ? 'text-down' : 'text-fg')}>{fmtMacro(r.value, 2)} pp</span>
        <span className="text-2xs text-muted">{r.signal ? 'triggered (≥ 0.5)' : 'below 0.5'}</span>
      </span>
    )
  const p = typeof r.probability === 'number' ? r.probability : null
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="relative h-1.5 w-14 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        <span className={cn('absolute inset-y-0 left-0 rounded-full', (p ?? 0) >= 0.5 ? 'bg-down' : (p ?? 0) >= 0.3 ? 'bg-warn' : 'bg-up')} style={{ width: `${Math.min(100, (p ?? 0) * 100)}%` }} />
      </span>
      <span className={cn('num font-semibold', (p ?? 0) >= 0.5 ? 'text-down' : 'text-fg')}>{fmtProb(p)}</span>
      {r.method === 'hmm_regime' && det.regime ? <span className="text-2xs text-muted">{String(det.regime)}</span> : null}
    </span>
  )
}

function RecessionPanel() {
  const q = useMacroRecession()
  const navigate = useNavigate()
  return (
    <Panel title="Recession risk" subtitle="Each method side by side with its label: official publications vs our model-based estimates" flush>
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-64" />}>
        {(r: MRecession) => {
          const g = recessionGrid(r)
          return (
            <div className="overflow-x-auto">
              <table className="w-full text-sm" aria-label="Recession risk by country and method">
                <thead>
                  <tr className="border-b border-border text-left">
                    <th className="px-3 py-1.5 text-2xs font-semibold tracking-wide text-muted uppercase">Country</th>
                    {g.methods.map((m) => (
                      <th key={m.method} className="px-3 py-1.5 align-bottom">
                        <Tip content={<div className="max-w-xs">{m.description ?? m.label}</div>}>
                          <span tabIndex={0} className="block cursor-help text-xs font-semibold text-fg">
                            {METHOD_SHORT[m.method] ?? m.method}
                          </span>
                        </Tip>
                        <Badge tone={m.official ? 'up' : 'info'} className="mt-0.5">
                          {m.official ? 'official' : 'model-based (ours)'}
                        </Badge>
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {g.countries.map((c) => (
                    <tr key={c} className="border-b border-border/60 hover:bg-surface-2">
                      <th scope="row" className="px-3 py-1 text-left font-medium">
                        {c.length === 2 ? (
                          <button type="button" className="inline-flex items-center gap-1.5 hover:text-accent-text hover:underline" onClick={() => navigate(`/macro/country/${c}`)}>
                            <Flag country={c} /> <span className="num">{c}</span>
                          </button>
                        ) : (
                          <span className="num">{c}</span>
                        )}
                      </th>
                      {g.methods.map((m) => (
                        <td key={m.method} className="px-3 py-1">
                          <RecessionCell r={g.get(c, m.method)} />
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-3 py-2 text-2xs text-muted">Yield-curve probabilities are for a recession within 12 months; the HMM value is the probability of being in the contraction regime now (current state), not a 12-month forecast. Sahm rule: real-time 3-month unemployment rise vs the prior 12-month low (official FRED SAHMREALTIME, US only).</p>
            </div>
          )
        }}
      </QueryView>
    </Panel>
  )
}

/* ───────────── today & upcoming ───────────── */


function UpcomingPanel() {
  const today = isoDaysFromNow(0)
  const to = isoDaysFromNow(7)
  const q = useMacroCalendar(today, to)
  const [tier, setTier] = useState('2')
  return (
    <Panel
      title="Today and the next 7 days"
      subtitle="Official release calendars · tier 1–2 · expectations are our model's"
      actions={<Select label="Maximum tier" value={tier} onChange={setTier} options={[{ value: '1', label: 'Tier 1 only' }, { value: '2', label: 'Tier 1–2' }, { value: '3', label: 'All tiers' }]} />}
      flush
    >
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-40" />}>
        {(cal) => <ReleaseList days={groupCalendar(cal.items, { maxTier: Number(tier), when: 'upcoming', today })} compact emptyText="No tier 1–2 releases in the next 7 days." />}
      </QueryView>
    </Panel>
  )
}
