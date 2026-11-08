import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { groupCalendar } from '@/api/macro/adapters'
import { useMacroCalendar, useMacroCountries, useMacroCountry, useMacroNowcast, useMacroRecession, useMacroSurprise } from '@/api/macro/hooks'
import { type MCountryPage, type MSection, NOWCAST_COUNTRIES } from '@/api/macro/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { Sparkline } from '@/components/charts/Sparkline'
import { useChartTokens } from '@/components/charts/tokens'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { MacroLines } from './charts'
import { RecessionCell } from './Cockpit'
import { METHOD_SHORT, fmtChange, fmtMacro, fmtProb, isoDaysFromNow } from './fmt'
import { LabelEventsPanel } from '../events/v2/LabelPanel'
import { ReleaseList } from './releases'
import { CellTile, MacroNav, ModelBasedTag, QuadrantBadge, QuadrantText, SeriesDrawer } from './shared'

export default function Country() {
  const { cc = 'US' } = useParams()
  const code = cc.toUpperCase()
  const q = useMacroCountry(code)
  const countries = useMacroCountries()
  const navigate = useNavigate()
  const [series, setSeries] = useState<string | null>(null)
  const name = countries.data?.countries.find((c) => c.country === code)?.name ?? code
  return (
    <>
      <PageHeader
        title={
          <span className="inline-flex items-center gap-2">
            <Flag country={code} className="h-4 w-6" /> {name}
          </span>
        }
        subtitle="Country macro page · official sources · cells z-scored against the country's own 10-year history"
        actions={
          <Select
            label="Country"
            value={code}
            onChange={(v) => navigate(`/macro/country/${v}`)}
            options={(countries.data?.countries ?? [{ country: code, name: code }]).map((c) => ({ value: c.country, label: `${c.country} · ${c.name}` }))}
          />
        }
      />
      <MacroNav />
      <QueryView query={q} skeleton={<Skeleton className="h-[600px]" />}>
        {(d) => <CountryBody d={d} onOpen={setSeries} />}
      </QueryView>
      <SeriesDrawer seriesId={series} onClose={() => setSeries(null)} />
    </>
  )
}

function CountryBody({ d, onOpen }: { d: MCountryPage; onOpen: (id: string) => void }) {
  const cc = d.country
  const headline = d.sections.map((s) => s.cells[0]).filter(Boolean)
  return (
    <div className="@container space-y-3">
      <Panel>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <div>
            <div className="label-caps">Cycle quadrant</div>
            <div className="mt-0.5 flex items-center gap-2">
              <QuadrantBadge q={d.cycle.latest?.quadrant} />
              <QuadrantText g={d.cycle.latest?.growth_momentum} i={d.cycle.latest?.inflation_momentum} />
            </div>
            <div className="num text-2xs text-muted">as of {d.cycle.latest?.date ? fmtDate(d.cycle.latest.date) : DASH}</div>
          </div>
          <div className="grid min-w-0 flex-1 grid-cols-2 gap-2 @2xl:grid-cols-5">
            {headline.slice(0, 5).map((c) => (
              <CellTile key={c.indicator} c={c} onOpen={onOpen} />
            ))}
          </div>
        </div>
      </Panel>
      <div className="grid gap-3 @4xl:grid-cols-2">
        {d.sections.map((s) => (
          <SectionCard key={s.key} s={s} onOpen={onOpen} />
        ))}
      </div>
      <div className="grid gap-3 @4xl:grid-cols-2">
        <EquityVsMacro d={d} />
        <CountrySurprise cc={cc} />
      </div>
      <div className="grid gap-3 @4xl:grid-cols-2">
        <CountryRecession cc={cc} />
        {(NOWCAST_COUNTRIES as readonly string[]).includes(cc) ? <NowcastSummary cc={cc} /> : <CountryReleases cc={cc} />}
      </div>
      {(NOWCAST_COUNTRIES as readonly string[]).includes(cc) && <CountryReleases cc={cc} />}
      <LabelEventsPanel type="country" id={cc} title="Events" />
    </div>
  )
}

function SectionCard({ s, onOpen }: { s: MSection; onOpen: (id: string) => void }) {
  const [all, setAll] = useState(false)
  const series = useMemo(() => [...s.series].sort((a, b) => Number(b.is_primary) - Number(a.is_primary)), [s.series])
  const shown = all ? series : series.slice(0, 6)
  return (
    <Panel title={s.title} subtitle={`${s.cells.length} headline cells · ${s.series.length} official series`}>
      {s.cells.length > 0 && (
        <div className="grid grid-cols-2 gap-1.5 @xl:grid-cols-3">
          {s.cells.map((c) => (
            <CellTile key={c.indicator} c={c} onOpen={onOpen} />
          ))}
        </div>
      )}
      <ul className="mt-2 divide-y divide-border/60">
        {shown.map((x) => (
          <li key={`${x.series_id}-${x.measure}`}>
            <button type="button" className="flex w-full items-center gap-2 py-1 text-left hover:bg-surface-2" onClick={() => onOpen(x.series_id)}>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-xs text-fg">{x.title}</span>
                <span className="num block truncate text-2xs text-muted">
                  {x.series_id} · {x.units} · {x.last_date ? fmtDate(x.last_date) : DASH}
                </span>
              </span>
              <Sparkline data={(x.spark ?? []).map((p) => p.value)} width={80} height={22} tone="neutral" ariaLabel={`${x.title} 5-year sparkline`} />
              <span className="num w-20 shrink-0 text-right text-xs font-medium text-fg">{fmtMacro(x.last_value)}</span>
            </button>
          </li>
        ))}
      </ul>
      {series.length > 6 && (
        <Button size="xs" variant="ghost" className="mt-1" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : `Show all ${series.length} series`}
        </Button>
      )}
    </Panel>
  )
}

/** Equity index y/y on top, macro composite (growth / inflation momentum) underneath: shared x, no dual axis. */
function EquityVsMacro({ d }: { d: MCountryPage }) {
  const t = useChartTokens()
  const e = d.equity_vs_macro
  const option = useMemo<EChartsOption | null>(() => {
    if (!e || !e.points.length) return null
    const xs = e.points.map((p) => p.date)
    const f = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
    return {
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      legend: { top: 0, left: 0, data: [`${e.index_name ?? e.equity_index} y/y`, 'growth momentum', 'inflation momentum'] },
      tooltip: { trigger: 'axis', valueFormatter: (v) => fmtMacro(v as number, 3) },
      grid: [
        { left: 8, right: 12, top: 30, height: '36%', containLabel: true },
        { left: 8, right: 12, top: '60%', bottom: 8, containLabel: true },
      ],
      xAxis: [
        { type: 'time', gridIndex: 0, axisLabel: { show: false } },
        { type: 'time', gridIndex: 1, axisLabel: { hideOverlap: true } },
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, scale: true, name: '% y/y', nameLocation: 'end', nameGap: 6, nameTextStyle: { align: 'left' }, axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
        { type: 'value', gridIndex: 1, scale: true, name: 'z (momentum)', nameLocation: 'end', nameGap: 6, nameTextStyle: { align: 'left' }, axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
      ],
      series: [
        { type: 'line', name: `${e.index_name ?? e.equity_index} y/y`, data: xs.map((x, i) => [x, f(e.points[i].index_yoy)]), showSymbol: false, color: t.ref, lineStyle: { color: t.ref, width: 1.4 } },
        { type: 'line', name: 'growth momentum', xAxisIndex: 1, yAxisIndex: 1, data: xs.map((x, i) => [x, f(e.points[i].growth_momentum)]), showSymbol: false, color: t.series[0], lineStyle: { color: t.series[0], width: 1.6 } },
        { type: 'line', name: 'inflation momentum', xAxisIndex: 1, yAxisIndex: 1, data: xs.map((x, i) => [x, f(e.points[i].inflation_momentum)]), showSymbol: false, color: t.series[1], lineStyle: { color: t.series[1], width: 1.6 } },
      ],
    } as EChartsOption
  }, [e, t])
  return (
    <Panel title="Equities vs the macro composite" subtitle={e ? `${e.index_name ?? e.equity_index} · correlation with growth momentum ${fmtChange(e.corr_index_yoy_growth, 2)} over ${e.n_overlap ?? DASH} months` : 'no official equity index series for this country'}>
      {option ? <EChart option={option} height={300} ariaLabel="Equity index y/y above, growth and inflation momentum below" /> : <p className="py-8 text-center text-xs text-muted">No equity index in the lake for this country.</p>}
      {e?.note && <p className="mt-1 text-2xs text-muted">{e.note}</p>}
    </Panel>
  )
}

function CountrySurprise({ cc }: { cc: string }) {
  const q = useMacroSurprise('all', [cc])
  return (
    <Panel title="Surprise index" subtitle={<ModelBasedTag short />}>
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(s) => {
          const ser = s.series.find((x) => x.country === cc)
          const latest = s.latest.find((x) => x.country === cc)
          if (!ser || !ser.points.length) return <p className="py-8 text-center text-xs text-muted">No scored releases for {cc} yet.</p>
          return (
            <>
              <MacroLines lines={[{ name: `${cc} surprise index (all)`, points: ser.points }]} height={240} zeroLine ariaLabel={`${cc} surprise index history`} />
              <p className="text-2xs text-muted">
                Latest {fmtMacro(latest?.value, 3)} · {latest?.n_events ?? DASH} releases in the 365-day window. + = releases better than our model expected (signed by economic direction).
              </p>
            </>
          )
        }}
      </QueryView>
    </Panel>
  )
}

function CountryRecession({ cc }: { cc: string }) {
  const q = useMacroRecession()
  return (
    <Panel title="Recession risk" subtitle="Official vs model-based methods, each labelled">
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(r) => {
          const c = r.countries.find((x) => x.country === cc)
          if (!c) return <p className="py-8 text-center text-xs text-muted">No recession indicators for {cc}.</p>
          const probLines = c.rows
            .filter((row) => ['nyfed_yield_curve_official', 'yield_curve_probit', 'hmm_regime'].includes(row.method) && (row.history?.length ?? 0) > 1)
            .map((row) => ({ name: `${METHOD_SHORT[row.method] ?? row.method}${row.official ? ' (official)' : ' (ours)'}`, points: row.history ?? [] }))
          return (
            <>
              <ul className="mb-2 space-y-1">
                {c.rows.map((row) => (
                  <li key={row.method} className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="w-36 shrink-0 font-medium text-fg">{METHOD_SHORT[row.method] ?? row.method}</span>
                    <RecessionCell r={row} />
                    <span className="min-w-0 flex-1 truncate text-2xs text-muted" title={row.method_label}>
                      {row.method_label}
                    </span>
                  </li>
                ))}
              </ul>
              {probLines.length > 0 && <MacroLines lines={probLines} height={200} ariaLabel={`${cc} recession probability history`} shadeUsRecessions={cc === 'US'} />}
              <p className="text-2xs text-muted">Probability histories (0–1). {fmtProb(0.5)} is the conventional signal level.</p>
            </>
          )
        }}
      </QueryView>
    </Panel>
  )
}

function NowcastSummary({ cc }: { cc: string }) {
  const q = useMacroNowcast(cc)
  return (
    <Panel title="GDP nowcast" subtitle="Model-based nowcast (ours) with official benchmarks where published" actions={<Link to={`/macro/nowcast?cc=${cc}`} className="text-xs text-accent-text hover:underline">Open nowcast →</Link>}>
      <QueryView query={q} skeleton={<Skeleton className="h-40" />}>
        {(n) => (
          <ul className="space-y-1.5">
            {n.targets.slice(0, 2).map((t) => (
              <li key={t.target_label} className="rounded-sm border border-border px-2 py-1.5 text-sm">
                <div className="font-semibold text-fg">
                  {t.target_label} <span className="text-xs font-normal text-muted">{n.units}</span>
                </div>
                {t.models.map((m) => (
                  <div key={m.model_id} className="num flex items-center gap-2 text-xs">
                    <span className="w-56 truncate text-muted" title={m.label}>
                      {m.label}
                    </span>
                    <span className="font-medium text-fg">{fmtMacro(m.latest?.nowcast)}</span>
                    {m.latest && typeof m.latest.lo === 'number' && (
                      <span className="text-2xs text-muted">
                        [{fmtMacro(m.latest.lo, 3)}, {fmtMacro(m.latest.hi, 3)}]
                      </span>
                    )}
                  </div>
                ))}
                {typeof t.actual === 'number' && <div className="num text-xs text-muted">actual (first print) {fmtMacro(t.actual)}</div>}
              </li>
            ))}
          </ul>
        )}
      </QueryView>
    </Panel>
  )
}


function CountryReleases({ cc }: { cc: string }) {
  const today = isoDaysFromNow(0)
  const q = useMacroCalendar(today, isoDaysFromNow(30))
  return (
    <Panel title="Upcoming releases" subtitle="Next 30 days · official calendars" flush>
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-40" />}>
        {(cal) => <ReleaseList days={groupCalendar(cal.items, { countries: [cc], maxTier: 3, when: 'upcoming', today })} compact emptyText={`No scheduled ${cc} releases in the next 30 days.`} />}
      </QueryView>
    </Panel>
  )
}
