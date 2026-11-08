import { CalendarClock, ChevronLeft, ChevronRight, Coins, FileText, Scissors } from 'lucide-react'
import { useMemo } from 'react'
import { useSearchParams } from 'react-router'
import {
  CORP_LABEL,
  SECTOR_NAME,
  bucketByDay,
  eventGroup,
  filterGroups,
  isOverdue,
  monthGrid,
  rangeFor,
  rangeTitle,
  sessionLabel,
  shiftAnchor,
  sortDay,
  weekDays,
} from '@/api/earnings/adapters'
import { useEarningsCalendar } from '@/api/earnings/hooks'
import type { CalendarView, ECalendar, ECalendarItem, EventGroup } from '@/api/earnings/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox, Segmented, Select } from '@/components/ui/inputs'
import { ErrorState, EmptyState, Skeleton } from '@/components/ui/feedback'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { Flag } from '@/components/ui/data'
import { fmtCompactCurrency, fmtDateTime, fmtNum, isNum } from '@/lib/format'
import { Missing } from '@/components/ui/missing'
import { releaseReason } from '@/lib/availability'
import { cn } from '@/lib/utils'
import { useWatchlist } from '@/store'
import { CompanyLink, EarningsNav, ModelBasedTag, Signed, SourceBadge } from './shared'
import { fmtEps, fmtRev } from './fmt'

const todayIso = () => new Date().toISOString().slice(0, 10)
const GROUPS: { id: EventGroup; label: string }[] = [
  { id: 'earnings', label: 'Earnings' },
  { id: 'dividend', label: 'Dividends' },
  { id: 'split', label: 'Splits' },
  { id: 'corporate', label: 'Corporate events' },
]
const UNIVERSES = [
  { value: 'mvp_us', label: 'US top 500' },
  { value: 'mvp_global', label: 'Global MVP' },
  { value: 'eu_broad', label: 'Europe broad' },
  { value: 'full', label: 'Full universe' },
  { value: 'watch', label: 'My watchlist' },
]
const REGION_OPTS = ['', 'US', 'EU', 'UK', 'JP', 'KR', 'CN', 'HK', 'LATAM', 'APAC', 'CA'].map((r) => ({ value: r, label: r || 'All regions' }))
const SECTOR_OPTS = [{ value: '', label: 'All sectors' }, ...Object.entries(SECTOR_NAME).filter(([k]) => !['ALL', 'UNKNOWN'].includes(k)).map(([value, label]) => ({ value, label }))]
const SOURCE_OPTS = [
  { value: '', label: 'All date sources' },
  { value: 'official_release,official_filing', label: 'Official (released)' },
  { value: 'company_announced', label: 'Company-announced' },
  { value: 'provider,provider_estimate', label: 'Provider (Yahoo)' },
  { value: 'model_projection', label: 'Model-based projection' },
]

function GroupIcon({ it }: { it: ECalendarItem }) {
  const g = eventGroup(it)
  const C = g === 'earnings' ? CalendarClock : g === 'dividend' ? Coins : g === 'split' ? Scissors : FileText
  return <C className={cn('size-3 shrink-0', g === 'earnings' ? 'text-accent' : 'text-muted')} aria-label={g} />
}

function itemLabel(it: ECalendarItem): string {
  if (it.kind === 'earnings') return `Earnings${it.fiscal_period ? ` ${it.fiscal_period}` : ''}`
  return it.title || CORP_LABEL[it.event_type] || it.event_type
}

export default function EarningsCalendar() {
  const [sp, setSp] = useSearchParams()
  const view = (['day', 'week', 'month'].includes(sp.get('view') ?? '') ? sp.get('view') : 'week') as CalendarView
  const anchor = /^\d{4}-\d{2}-\d{2}$/.test(sp.get('date') ?? '') ? (sp.get('date') as string) : todayIso()
  const universe = sp.get('universe') ?? 'mvp_us'
  const region = sp.get('region') ?? ''
  const sector = sp.get('sector') ?? ''
  const src = sp.get('src') ?? ''
  const groups = (sp.get('types')?.split(',').filter(Boolean) as EventGroup[] | undefined) ?? ['earnings', 'dividend', 'split']
  const watchIds = useWatchlist((s) => s.ids)
  const set = (k: string, v: string) =>
    setSp(
      (p) => {
        if (v) p.set(k, v)
        else p.delete(k)
        return p
      },
      { replace: true },
    )
  const range = rangeFor(view, anchor)
  const q = useEarningsCalendar({
    from: range.from,
    to: range.to,
    universe: universe === 'watch' ? undefined : universe,
    watchlistIds: universe === 'watch' ? watchIds : undefined,
    region,
    sector,
    types: groups,
    dateSources: src ? src.split(',') : undefined,
  }, universe !== 'watch' || watchIds.length > 0)
  const toggleGroup = (g: EventGroup, on: boolean) => {
    const next = on ? [...new Set([...groups, g])] : groups.filter((x) => x !== g)
    set('types', next.length ? next.join(',') : 'earnings')
  }

  return (
    <>
      <PageHeader
        title="Earnings & corporate events"
        subtitle="Official release times · upcoming dates labelled by source · reactions and model-based surprises (not consensus)"
      />
      <EarningsNav />
      <Panel
        title={
          <span className="inline-flex items-center gap-2">
            <Button size="icon-sm" variant="ghost" aria-label="Previous" onClick={() => set('date', shiftAnchor(view, anchor, -1))}>
              <ChevronLeft />
            </Button>
            <span>{rangeTitle(view, anchor)}</span>
            <Button size="icon-sm" variant="ghost" aria-label="Next" onClick={() => set('date', shiftAnchor(view, anchor, 1))}>
              <ChevronRight />
            </Button>
            <Button size="xs" variant="outline" onClick={() => set('date', '')}>
              Today
            </Button>
          </span>
        }
        actions={
          <Segmented
            label="Calendar view"
            size="xs"
            value={view}
            onChange={(v) => set('view', v)}
            options={[
              { value: 'day', label: 'Day' },
              { value: 'week', label: 'Week' },
              { value: 'month', label: 'Month' },
            ]}
          />
        }
        flush
      >
        <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
          <Select label="Universe" value={universe} onChange={(v) => set('universe', v)} options={UNIVERSES} />
          <Select label="Region" value={region} onChange={(v) => set('region', v)} options={REGION_OPTS} />
          <Select label="Sector" value={sector} onChange={(v) => set('sector', v)} options={SECTOR_OPTS} />
          <Select label="Date source" value={src} onChange={(v) => set('src', v)} options={SOURCE_OPTS} />
          <div className="flex flex-wrap items-center gap-3 pl-1" role="group" aria-label="Event types">
            {GROUPS.map((g) => (
              <label key={g.id} className="flex items-center gap-1.5 text-xs text-muted">
                <Checkbox label={g.label} checked={groups.includes(g.id)} onCheckedChange={(c) => toggleGroup(g.id, c === true)} />
                {g.label}
              </label>
            ))}
          </div>
        </div>
        <Legend />
        {universe === 'watch' && watchIds.length === 0 ? (
          <EmptyState title="Your watchlist is empty" message="Add companies with the star on Company 360." />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : !q.data ? (
          <Skeleton className="m-3 h-96" />
        ) : (
          <CalendarBody cal={q.data} view={view} anchor={anchor} groups={groups} onPickDay={(d) => setSp((p) => { p.set('view', 'day'); p.set('date', d); return p })} />
        )}
      </Panel>
    </>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-3 py-1.5 text-2xs text-muted">
      <span className="inline-flex items-center gap-1"><SourceBadge source="official_release" /> filing / exchange time</span>
      <span className="inline-flex items-center gap-1"><SourceBadge source="company_announced" /> exchange schedule</span>
      <span className="inline-flex items-center gap-1"><SourceBadge source="provider_estimate" /> Yahoo</span>
      <span className="inline-flex items-center gap-1"><SourceBadge source="model_projection" /> our cadence model, ± window</span>
      <ModelBasedTag />
    </div>
  )
}

function CalendarBody({ cal, view, anchor, groups, onPickDay }: { cal: ECalendar; view: CalendarView; anchor: string; groups: EventGroup[]; onPickDay: (d: string) => void }) {
  const items = useMemo(() => filterGroups(cal.items, groups), [cal.items, groups])
  const byDay = useMemo(() => bucketByDay(items), [items])
  return (
    <div>
      {cal.truncated && <div className="border-b border-border bg-warn-soft px-3 py-1 text-xs text-warn">Showing the first {cal.items.length} of {cal.total} events — narrow the filters.</div>}
      {view === 'day' ? (
        <DayTable items={sortDay(byDay.get(anchor) ?? [])} asOf={cal.as_of} />
      ) : view === 'week' ? (
        <WeekGrid days={weekDays(anchor)} byDay={byDay} asOf={cal.as_of} onPickDay={onPickDay} />
      ) : (
        <MonthGrid anchor={anchor} byDay={byDay} onPickDay={onPickDay} />
      )}
    </div>
  )
}

function Chip({ it, asOf }: { it: ECalendarItem; asOf: string }) {
  const s = it.security
  const tip = (
    <div className="space-y-0.5">
      <div className="font-semibold">{s.name}</div>
      <div>{itemLabel(it)} · {sessionLabel(it.session)}</div>
      {it.kind === 'earnings' && <div>{it.source_label}{it.window_lo ? ` (window ${it.window_lo} → ${it.window_hi})` : ''}</div>}
      {it.eps_expected != null && <div>EPS model-based expectation {fmtEps(it.eps_expected)} (80% {fmtEps(it.eps_lo80)}…{fmtEps(it.eps_hi80)})</div>}
      {it.eps_surprise_pct != null && <div>EPS surprise vs model {fmtNum(it.eps_surprise_pct * 100, 1, true)}%</div>}
      {it.amount != null && <div>{fmtNum(it.amount, 4)} {it.currency}</div>}
      {isOverdue(it, asOf) && <div className="text-warn">Projected date passed - release not in the data yet</div>}
    </div>
  )
  return (
    <Tip content={tip}>
      <li className="flex min-w-0 items-center gap-1 rounded-sm px-1 py-0.5 text-xs hover:bg-surface-2">
        <GroupIcon it={it} />
        <CompanyLink ticker={s.ticker} id={s.security_id} className="truncate" />
        {it.kind === 'earnings' && it.status === 'upcoming' && it.date_source !== 'company_announced' && (
          <span className={cn('ml-auto shrink-0 text-2xs', it.date_source === 'model_projection' ? 'text-warn' : 'text-info')} aria-label={it.source_label ?? ''}>
            {it.date_source === 'model_projection' ? 'proj.' : 'prov.'}
          </span>
        )}
        {it.kind === 'earnings' && it.status === 'released' && <span className="ml-auto shrink-0 text-2xs"><Signed v={it.eps_surprise_pct} digits={0} /></span>}
      </li>
    </Tip>
  )
}

function WeekGrid({ days, byDay, asOf, onPickDay }: { days: string[]; byDay: Map<string, ECalendarItem[]>; asOf: string; onPickDay: (d: string) => void }) {
  const CAP = 30
  return (
    <div className="grid grid-cols-1 divide-y divide-border sm:grid-cols-7 sm:divide-x sm:divide-y-0">
      {days.map((d) => {
        const its = sortDay(byDay.get(d) ?? [])
        const wknd = [5, 6].includes(days.indexOf(d))
        return (
          <section key={d} className={cn('min-w-0 p-1.5', wknd && 'bg-surface-2/40')} aria-label={d}>
            <button type="button" onClick={() => onPickDay(d)} className={cn('mb-1 flex w-full items-baseline justify-between rounded-sm px-1 text-left text-xs font-semibold hover:bg-surface-2', d === asOf && 'text-accent')}>
              <span>{new Date(`${d}T00:00:00Z`).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' })}</span>
              <span className="num text-2xs font-normal text-muted">{its.length || ''}</span>
            </button>
            <ul className="space-y-px">
              {its.slice(0, CAP).map((it, i) => (
                <Chip key={`${it.security.security_id}-${it.event_type}-${i}`} it={it} asOf={asOf} />
              ))}
            </ul>
            {its.length > CAP && (
              <button type="button" className="mt-1 px-1 text-2xs text-accent hover:underline" onClick={() => onPickDay(d)}>
                +{its.length - CAP} more
              </button>
            )}
          </section>
        )
      })}
    </div>
  )
}

function MonthGrid({ anchor, byDay, onPickDay }: { anchor: string; byDay: Map<string, ECalendarItem[]>; onPickDay: (d: string) => void }) {
  const weeks = monthGrid(anchor)
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] table-fixed border-collapse text-xs">
        <thead>
          <tr className="text-muted">
            {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d) => (
              <th key={d} scope="col" className="border-b border-border px-1.5 py-1 text-left font-medium">{d}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {weeks.map((w) => (
            <tr key={w[0].date}>
              {w.map((c) => {
                const its = sortDay(byDay.get(c.date) ?? [])
                const ne = its.filter((x) => x.kind === 'earnings').length
                const nd = its.filter((x) => eventGroup(x) === 'dividend').length
                return (
                  <td key={c.date} className={cn('h-24 border border-border/60 p-1 align-top', !c.inMonth && 'bg-surface-2/50 text-faint')}>
                    <button type="button" onClick={() => onPickDay(c.date)} className="w-full text-left hover:bg-surface-2" aria-label={`${c.date}: ${ne} earnings, ${nd} dividends`}>
                      <div className="flex items-baseline justify-between">
                        <span className="num font-semibold">{Number(c.date.slice(8))}</span>
                        <span className="text-2xs text-muted">
                          {ne > 0 && <span className="mr-1">E {ne}</span>}
                          {nd > 0 && <span>D {nd}</span>}
                        </span>
                      </div>
                      <ul className="mt-0.5 space-y-px">
                        {its.slice(0, 4).map((it, i) => (
                          <li key={i} className="flex items-center gap-1 truncate">
                            <GroupIcon it={it} />
                            <span className="num truncate">{it.security.ticker}</span>
                          </li>
                        ))}
                      </ul>
                    </button>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DayTable({ items, asOf }: { items: ECalendarItem[]; asOf: string }) {
  if (!items.length) return <EmptyState title="Nothing scheduled" message="No events for this day with the current filters." />
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[900px] text-xs">
        <thead className="text-left text-muted">
          <tr className="border-b border-border">
            <th scope="col" className="px-2 py-1.5 font-medium">Company</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Event</th>
            <th scope="col" className="px-2 py-1.5 font-medium">When</th>
            <th scope="col" className="px-2 py-1.5 font-medium">Date source</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">
              <Tip content="Model-based expectation (not consensus), 80% interval"><span tabIndex={0} className="cursor-help underline decoration-dotted">EPS exp.</span></Tip>
            </th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Revenue exp.</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">EPS actual</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Surprise vs model</th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">
              <Tip content="Cumulative abnormal return over days -1..+1 (market model vs country index)"><span tabIndex={0} className="cursor-help underline decoration-dotted">CAR [−1,+1]</span></Tip>
            </th>
            <th scope="col" className="px-2 py-1.5 text-right font-medium">Mkt cap</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it, i) => (
            <tr key={`${it.security.security_id}-${i}`} className="border-b border-border/60 hover:bg-surface-2">
              <td className="max-w-[260px] px-2 py-1">
                <div className="flex items-center gap-1.5">
                  <Flag country={it.security.country} className="h-2.5 w-[15px] shrink-0" />
                  <CompanyLink ticker={it.security.ticker} name={it.security.name} id={it.security.security_id} className="truncate" />
                </div>
              </td>
              <td className="px-2 py-1">
                <span className="inline-flex items-center gap-1 whitespace-nowrap">
                  <GroupIcon it={it} />
                  <span className="max-w-[260px] truncate" title={itemLabel(it)}>{itemLabel(it)}</span>
                  {it.period_end && <span className="text-faint">· {it.period_end}</span>}
                </span>
              </td>
              <td className="px-2 py-1 whitespace-nowrap">{it.ts ? fmtDateTime(it.ts) : sessionLabel(it.session)}</td>
              <td className="px-2 py-1">
                {it.kind === 'earnings' ? (
                  <span className="inline-flex items-center gap-1">
                    <SourceBadge source={it.date_source} window={{ lo: it.window_lo, hi: it.window_hi }} />
                    {isOverdue(it, asOf) && <Badge tone="warn">awaiting</Badge>}
                  </span>
                ) : (
                  <span className="text-muted">{it.source_label}</span>
                )}
              </td>
              <td className="num px-2 py-1 text-right">
                {it.eps_expected != null ? (
                  <Tip content={`80% interval ${fmtEps(it.eps_lo80)} … ${fmtEps(it.eps_hi80)}`}>
                    <span tabIndex={0}>{fmtEps(it.eps_expected)}</span>
                  </Tip>
                ) : it.amount != null ? (
                  <span className="text-muted">{fmtNum(it.amount, 4)} {it.currency}</span>
                ) : (
                  <Missing field="earn.eps_expected" region={it.security.region} />
                )}
              </td>
              <td className="num px-2 py-1 text-right">{isNum(it.rev_expected) ? fmtRev(it.rev_expected) : <Missing field="earn.rev_expected" region={it.security.region} />}</td>
              <td className="num px-2 py-1 text-right">{isNum(it.eps_actual) ? fmtEps(it.eps_actual) : <Missing field="earn.eps_actual" region={it.security.region} reason={releaseReason(it, asOf)} />}</td>
              <td className="px-2 py-1 text-right">{isNum(it.eps_surprise_pct) ? <Signed v={it.eps_surprise_pct} /> : <Missing field="eps_surprise" region={it.security.region} reason={releaseReason(it, asOf)} />}</td>
              <td className="px-2 py-1 text-right">{isNum(it.car_m1_p1) ? <Signed v={it.car_m1_p1} /> : <Missing field="earn.car_m1_p1" region={it.security.region} reason={releaseReason(it, asOf, 'Reaction measured after')} />}</td>
              <td className="num px-2 py-1 text-right text-muted">{isNum(it.security.market_cap_usd) ? fmtCompactCurrency(it.security.market_cap_usd) : <Missing field="mkt_cap_usd" region={it.security.region} />}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
