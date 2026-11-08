import { FlaskConical } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router'
import { revisionOverlay } from '@/api/macro/adapters'
import { useMacroCompare, useMacroCountries, useMacroSeries } from '@/api/macro/hooks'
import type { MRevisionStats, MSeriesData, SeriesTransform } from '@/api/macro/types'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroLines, type MLine } from './charts'
import { fmtMacro, isoDaysFromNow } from './fmt'
import { CellTile, MacroNav, MacroSearch, Provenance, SourceLine } from './shared'
import { useOpenInStudio } from './useOpenInStudio'

const SUGGESTED = [
  { id: 'fred:CPIAUCSL', label: 'US CPI (BLS via FRED)' },
  { id: 'fred:PAYEMS', label: 'US nonfarm payrolls' },
  { id: 'fred:GDPC1', label: 'US real GDP' },
  { id: 'fred:DGS10', label: 'US 10Y Treasury' },
  { id: 'fred:UNRATE', label: 'US unemployment rate' },
  { id: 'fred:INDPRO', label: 'US industrial production' },
]

export default function SeriesExplorer() {
  const { id } = useParams()
  return (
    <>
      <PageHeader title="Series explorer" subtitle="Any official macro series: transforms, first release vs revised, multi-country compare, provenance" />
      <MacroNav />
      {id ? (
        <SeriesView id={decodeURIComponent(id)} />
      ) : (
        <Panel title="Find a series" subtitle="Search the macro catalog (official sources only)">
          <MacroSearch className="w-full max-w-xl" autoFocus />
          <div className="mt-3 flex flex-wrap gap-2 text-sm">
            {SUGGESTED.map((s) => (
              <Link key={s.id} to={`/macro/series/${encodeURIComponent(s.id)}`} className="rounded-md border border-border px-2 py-1 text-fg hover:bg-surface-2">
                {s.label} <span className="num text-2xs text-muted">{s.id}</span>
              </Link>
            ))}
          </div>
        </Panel>
      )}
    </>
  )
}

const RANGES: Record<string, number | null> = { '5': 5, '10': 10, '20': 20, all: null }

function SeriesView({ id }: { id: string }) {
  const [tf, setTf] = useState<SeriesTransform>('canonical')
  const [vint, setVint] = useState(false)
  const [range, setRange] = useState('10')
  const q = useMacroSeries(id, tf === 'canonical' ? 'level' : tf, vint)
  const open = useOpenInStudio()
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-[500px]" />}>
      {(d) => {
        const yrs = RANGES[range]
        const from = yrs ? isoDaysFromNow(-365 * yrs) : undefined
        return (
          <div className="@container space-y-3">
            <Panel
              title={d.meta.title ?? id}
              subtitle={
                <span className="num">
                  {id} · {d.meta.country ?? DASH} · {d.meta.frequency} · {d.meta.category ?? DASH} · last {d.meta.last_observation_date ? fmtDate(d.meta.last_observation_date) : DASH}
                </span>
              }
              actions={
                <Button size="sm" variant="outline" onClick={() => open(d)} title="New Model Studio study with this series as the target">
                  <FlaskConical /> Open in Model Studio
                </Button>
              }
            >
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Segmented
                  label="Transform"
                  size="xs"
                  value={tf}
                  onChange={setTf}
                  options={[
                    { value: 'canonical', label: 'Canonical' },
                    { value: 'level', label: 'Level' },
                    { value: 'yoy', label: 'y/y %' },
                    { value: 'mom', label: 'm/m %' },
                    { value: 'z', label: 'z-score' },
                  ]}
                />
                <Select label="Range" value={range} onChange={setRange} options={[{ value: '5', label: '5 years' }, { value: '10', label: '10 years' }, { value: '20', label: '20 years' }, { value: 'all', label: 'All' }]} />
                <label className="flex items-center gap-1.5 text-xs text-muted">
                  <Switch label="First release vs latest" checked={vint} onCheckedChange={setVint} /> First release vs latest
                </label>
              </div>
              <MainChart d={d} tf={tf} vint={vint} from={from} />
            </Panel>
            <div className="grid gap-3 @4xl:grid-cols-2">
              <Provenance d={d} />
              <div className="space-y-3">
                {d.cells.length > 0 && (
                  <Panel title="Heatmap cells using this series">
                    <div className="grid grid-cols-2 gap-1.5">
                      {d.cells.map((c) => (
                        <CellTile key={`${c.country}-${c.indicator}`} c={c} />
                      ))}
                    </div>
                  </Panel>
                )}
                {vint && d.vintages && <RevisionStats d={d} />}
              </div>
            </div>
            {d.meta.indicator && <CompareCountries indicator={d.meta.indicator} home={d.meta.country ?? 'US'} />}
            {d.releases && d.releases.length > 0 && <ReleasesTable d={d} />}
          </div>
        )
      }}
    </QueryView>
  )
}

function MainChart({ d, tf, vint, from }: { d: MSeriesData; tf: SeriesTransform; vint: boolean; from?: string }) {
  const canon = d.canonical[0]
  const base = (tf === 'canonical' && canon ? canon.points : d.points).filter((p) => !from || p.date >= from)
  const units = tf === 'canonical' && canon ? canon.units : d.units
  const lines = useMemo<MLine[]>(() => {
    const out: MLine[] = [{ name: tf === 'canonical' && canon ? `${canon.measure} (latest)` : `${d.meta.series_id} (latest)`, points: base }]
    if (vint && d.vintages) {
      const ov = revisionOverlay(d.vintages, from)
      // the vintage pair is on the native level or the canonical transform: show it when it matches what is plotted
      const comparable = ov && ((ov.transformed && (tf === 'canonical' || tf === d.vintages.transform)) || (!ov.transformed && tf === 'level'))
      if (ov && comparable) out.push({ name: 'as first released', points: ov.dates.map((dt, i) => ({ date: dt, value: ov.first[i] })), dashed: true })
    }
    return out
  }, [base, vint, d, tf, from, canon])
  if (!base.length) return <EmptyState title="No observations" message="No data in this range for this transform." />
  const vintNote = vint && !d.vintages ? 'No vintage history (ALFRED / first-seen) for this series.' : vint && lines.length < 2 ? `First-release values are published on the ${d.vintages?.transform ?? 'native'} basis: switch the transform to compare.` : null
  return (
    <>
      <MacroLines lines={lines} units={units} height={320} ariaLabel={`${d.meta.series_id} history`} shadeUsRecessions={d.meta.country === 'US'} />
      {vintNote && <p className="mt-1 text-xs text-warn">{vintNote}</p>}
    </>
  )
}

function RevisionStats({ d }: { d: MSeriesData }) {
  const rs = d.vintages?.revision_stats
  if (!rs) return null
  const rows = Object.entries(rs) as [string, MRevisionStats][]
  const basis = rows.find(([, b]) => b.basis)?.[1].basis ?? 'canonical'
  const rebased = Math.max(0, ...rows.map(([, b]) => b.n_rebased_excluded ?? 0))
  const estimated = Math.max(0, ...rows.map(([, b]) => b.n_estimated_excluded ?? 0))
  return (
    <Panel
      title="Revision statistics"
      subtitle={`latest − first release · primary basis: ${basis} · ${d.vintages?.kind === 'alfred' ? 'ALFRED vintages (St. Louis Fed)' : 'first-seen values'}`}
      flush
    >
      <table className="w-full text-xs" aria-label="Revision statistics">
        <thead>
          <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
            <th className="px-3 py-1">Window</th>
            <th className="px-2 py-1">Basis</th>
            <th className="px-2 py-1 text-right">n</th>
            <th className="px-2 py-1 text-right">mean</th>
            <th className="px-2 py-1 text-right">mean |rev|</th>
            <th className="px-2 py-1 text-right">std</th>
            <th className="px-2 py-1 text-right">share up</th>
            <th className="px-3 py-1 text-right">max |rev|</th>
          </tr>
        </thead>
        <tbody>
          {rows.flatMap(([w, b]) =>
            ([b.basis === 'native' ? 'native' : 'canonical', b.basis === 'native' ? 'canonical' : 'native'] as const)
              .filter((k) => b[k])
              .map((k) => {
                const v = b[k] as Record<string, number | null>
                const primary = k === (b.basis ?? 'canonical')
                return (
                  <tr key={`${w}-${k}`} className={cn('border-b border-border/60', primary && 'bg-accent-soft/30')}>
                    <td className="num px-3 py-0.5 text-fg">{w}</td>
                    <td className={cn('px-2 py-0.5', primary ? 'font-medium text-fg' : 'text-muted')}>
                      {k}
                      {primary ? ' (primary)' : ''}
                    </td>
                    <td className="num px-2 py-0.5 text-right">{fmtMacro(v.n)}</td>
                    <td className="num px-2 py-0.5 text-right">{fmtMacro(v.mean, 3)}</td>
                    <td className="num px-2 py-0.5 text-right">{fmtMacro(v.mean_abs, 3)}</td>
                    <td className="num px-2 py-0.5 text-right">{fmtMacro(v.std, 3)}</td>
                    <td className="num px-2 py-0.5 text-right">{typeof v.share_up === 'number' ? `${Math.round(v.share_up * 100)}%` : DASH}</td>
                    <td className="num px-3 py-0.5 text-right">{fmtMacro(v.max_abs, 3)}</td>
                  </tr>
                )
              }),
          )}
        </tbody>
      </table>
      <p className="px-3 py-1.5 text-2xs text-muted">
        Excluded from the statistics: {rebased} periods with an index rebase between first release and latest, {estimated} with an estimated first-release date.
      </p>
    </Panel>
  )
}

function CompareCountries({ indicator, home }: { indicator: string; home: string }) {
  const countries = useMacroCountries()
  const [pick, setPick] = useState<string[]>(() => [...new Set([home, 'US', 'EA', 'GB', 'JP'])].slice(0, 4))
  const q = useMacroCompare(indicator, pick)
  return (
    <Panel title="Compare countries" subtitle={`Same indicator (${indicator}) across countries · up to 4`}>
      <div className="mb-2 flex flex-wrap gap-1" role="group" aria-label="Countries to compare">
        {(countries.data?.countries ?? []).map((c) => {
          const on = pick.includes(c.country)
          return (
            <button
              key={c.country}
              type="button"
              aria-pressed={on}
              onClick={() => setPick(on ? pick.filter((x) => x !== c.country) : [...pick, c.country].slice(-4))}
              className={cn('num h-5 rounded-sm border px-1.5 text-2xs', on ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:text-fg')}
            >
              {c.country}
            </button>
          )
        })}
      </div>
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(c) => (
          <>
            <MacroLines lines={c.countries.filter((x) => x.available).map((x) => ({ name: x.country, points: x.points }))} units={c.countries[0]?.units} height={280} ariaLabel={`${c.label} by country`} />
            <div className="mt-1 flex flex-wrap gap-x-3">
              {c.countries.map((x) => (
                <SourceLine key={x.country} sourceId={x.latest?.source_id} seriesId={`${x.country}: ${x.series_id ?? 'not available'}`} />
              ))}
            </div>
          </>
        )}
      </QueryView>
    </Panel>
  )
}

function ReleasesTable({ d }: { d: MSeriesData }) {
  return (
    <Panel title="Release history" subtitle="When each period was first published and last revised" flush>
      <div className="max-h-72 overflow-y-auto">
        <table className="w-full text-xs" aria-label="Release history">
          <thead>
            <tr className="sticky top-0 border-b border-border bg-surface text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1">Period</th>
              <th className="px-2 py-1">First released</th>
              <th className="px-2 py-1">Latest vintage</th>
              <th className="px-3 py-1 text-right">Vintages</th>
            </tr>
          </thead>
          <tbody>
            {[...(d.releases ?? [])].reverse().map((r) => (
              <tr key={r.date} className="border-b border-border/60">
                <td className="num px-3 py-0.5 text-fg">{fmtDate(r.date)}</td>
                <td className="num px-2 py-0.5">{r.first_release_date ? fmtDate(r.first_release_date) : DASH}</td>
                <td className="num px-2 py-0.5">{r.latest_vintage_date ? fmtDate(r.latest_vintage_date) : DASH}</td>
                <td className="num px-3 py-0.5 text-right">{fmtMacro(r.n_vintages)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}
