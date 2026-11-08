import { ArrowDownRight, ArrowRight, ArrowUpRight, ExternalLink, FlaskConical, Info, Search, X } from 'lucide-react'
import { Dialog } from 'radix-ui'
import { type ReactNode, useDeferredValue, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router'
import { orientedZ, zBucket } from '@/api/macro/adapters'
import { useMacroSeries } from '@/api/macro/hooks'
import { EXPECTATION_LABEL, type MCell, type SeriesTransform } from '@/api/macro/types'
import { useStudioVariables } from '@/api/studio/hooks'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented } from '@/components/ui/inputs'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtDateTime } from '@/lib/format'
import { macroCellReason, reasonText } from '@/lib/availability'
import { cn } from '@/lib/utils'
import { MacroLines } from './charts'
import { QUADRANT_LABEL, fmtChange, fmtMacro, isoDaysFromNow, zStyle } from './fmt'
import { useOpenInStudio } from './useOpenInStudio'

/* ───────────── section sub-navigation ───────────── */

const LINKS = [
  { to: '/macro', label: 'Cockpit', end: true },
  { to: '/macro/calendar', label: 'Calendar' },
  { to: '/macro/nowcast', label: 'Nowcast' },
  { to: '/macro/theme/growth', label: 'Themes', match: '/macro/theme' },
  { to: '/macro/country/US', label: 'Countries', match: '/macro/country' },
  { to: '/macro/series', label: 'Series explorer', match: '/macro/series' },
  { to: '/macro/cb', label: 'Central banks', match: '/macro/cb' },
  { to: '/macro/curves', label: 'Curves', match: '/macro/curves' },
  { to: '/macro/credit', label: 'Credit', match: '/macro/credit' },
  { to: '/macro/markets', label: 'Markets', match: '/macro/markets' },
  { to: '/macro/markets?tab=instruments', label: 'Instruments', match: '/instruments' },
  { to: '/macro/cross-asset', label: 'Cross-asset', match: '/macro/cross-asset' },
  { to: '/macro/positioning', label: 'Positioning (CFTC)', match: '/macro/positioning' },
]

export function MacroNav() {
  const { pathname, search } = useLocation()
  const instr = pathname.startsWith('/instruments') || (pathname === '/macro/markets' && search.includes('tab=instruments'))
  return (
    <nav aria-label="Macro sections" className="mb-3 flex flex-wrap items-center gap-x-1 gap-y-2 border-b border-border">
      {LINKS.map((l) => (
        <NavLink
          key={l.to}
          to={l.to}
          end={l.end}
          className={({ isActive }) =>
            cn(
              '-mb-px h-8 border-b-2 px-3 text-sm leading-8 font-medium whitespace-nowrap transition-colors',
              (l.label === 'Instruments' ? instr : l.label === 'Markets' ? !instr && (isActive || pathname.startsWith('/macro/markets')) : isActive || (l.match && pathname.startsWith(l.match)))
                ? 'border-accent text-fg'
                : 'border-transparent text-muted hover:text-fg',
            )
          }
        >
          {l.label}
        </NavLink>
      ))}
      <div className="ml-auto pb-1">
        <MacroSearch />
      </div>
    </nav>
  )
}

/** Global search over the macro catalog (all official series in the lake). */
export function MacroSearch({ className, autoFocus }: { className?: string; autoFocus?: boolean }) {
  const [q, setQ] = useState('')
  const dq = useDeferredValue(q.trim())
  const res = useStudioVariables({ q: dq || undefined, source: 'macro', limit: 12 }, dq.length > 1)
  const navigate = useNavigate()
  const items = dq.length > 1 ? (res.data?.items ?? []) : []
  return (
    <div className={cn('relative w-72', className)}>
      <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
      <Input autoFocus={autoFocus} className="w-full pl-7" placeholder="Search macro series (e.g. HICP, payrolls, 10Y)" aria-label="Search macro series" value={q}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => e.key === 'Escape' && setQ('')}
        onBlur={() => setTimeout(() => setQ(''), 200)}
      />
      {items.length > 0 && (
        <ul className="absolute top-full right-0 left-0 z-30 mt-0.5 max-h-80 overflow-y-auto rounded-md border border-border-strong bg-surface py-0.5 text-sm" role="listbox" aria-label="Matching macro series">
          {items.map((v) => (
            <li key={v.key}>
              <button
                type="button"
                className="flex w-full items-start gap-2 px-2 py-1 text-left hover:bg-surface-2"
                onClick={() => {
                  setQ('')
                  navigate(`/macro/series/${encodeURIComponent(v.field)}`)
                }}
              >
                {v.country && v.country.length === 2 ? <Flag country={v.country} className="mt-0.5" /> : <span className="w-[18px]" />}
                <span className="min-w-0">
                  <span className="block truncate text-fg">{v.title}</span>
                  <span className="num block truncate text-2xs text-muted">
                    {v.field} · {v.frequency} · to {v.lastDate ? fmtDate(v.lastDate) : DASH}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/* ───────────── labels & badges ───────────── */

/** Mandatory wording next to every model expectation (user rule). */
export function ModelBasedTag({ className, short = false }: { className?: string; short?: boolean }) {
  return (
    <Tip content="Our own forecast (ARIMA/SARIMA with order search, seasonal-naive fallback, nowcast-informed where available), snapshotted before the release. It is not a market consensus.">
      <span tabIndex={0} className={cn('inline-flex items-center gap-1 rounded-sm border border-info/40 bg-info-soft px-1.5 text-2xs font-semibold text-info', className)}>
        <Info className="size-3" aria-hidden />
        {short ? 'Model-based (not consensus)' : EXPECTATION_LABEL}
      </span>
    </Tip>
  )
}

export function OfficialTag({ official, label }: { official: boolean; label?: string }) {
  return official ? <Badge tone="up">{label ?? 'official'}</Badge> : <Badge tone="info">{label ?? 'model-based (ours)'}</Badge>
}

export function QuadrantBadge({ q }: { q: string | null | undefined }) {
  if (!q) return <Badge tone="neutral">not placed</Badge>
  const tone = q === 'expansion' ? 'up' : q === 'contraction' ? 'down' : q === 'slowdown' ? 'warn' : 'info'
  return <Badge tone={tone}>{QUADRANT_LABEL[q] ?? q}</Badge>
}

/** Source line for a cell / series: publisher id + series id (+ official page link when known). */
export function SourceLine({ sourceId, seriesId, href, className }: { sourceId?: string | null; seriesId: string; href?: string | null; className?: string }) {
  return (
    <span className={cn('num inline-flex flex-wrap items-center gap-1 text-2xs text-muted', className)}>
      source <b className="text-fg">{sourceId ?? seriesId.split(':')[0]}</b> · {seriesId}
      {href && (
        <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
          official page <ExternalLink className="size-3" />
        </a>
      )}
    </span>
  )
}

/* ───────────── z-coloured cell ───────────── */

export function DirArrow({ d, className }: { d: string | null | undefined; className?: string }) {
  const Icon = d === 'up' ? ArrowUpRight : d === 'down' ? ArrowDownRight : ArrowRight
  return <Icon className={cn('size-3 shrink-0', className)} aria-label={d ? `3-month direction ${d}` : undefined} />
}

export function CellTooltip({ c }: { c: MCell }) {
  const oz = orientedZ(c)
  return (
    <div className="min-w-56 space-y-0.5 text-xs">
      <div className="font-semibold text-fg">
        {c.country} · {c.label ?? c.indicator}
      </div>
      <div className="text-muted">{c.title}</div>
      <div className="num">
        <b className="text-fg">{fmtMacro(c.value)}</b> {c.units} · {c.period_label ?? c.period}
      </div>
      <div className="num text-muted">
        released {c.release_date ? fmtDate(c.release_date) : DASH}
        {c.release_date_estimated ? ' (estimated)' : ''} · {c.status}
      </div>
      {(c.value == null || c.status === 'stale' || c.status === 'discontinued') && (
        <div className="text-warn" data-testid="missing-reason">{reasonText(macroCellReason(c))}</div>
      )}
      <div className="num text-muted">
        3m {fmtChange(c.change_3m)} · 6m {fmtChange(c.change_6m)} · 12m {fmtChange(c.change_12m)}
      </div>
      <div className="num text-muted">
        z (10y) {fmtChange(c.z_10y)} · percentile {typeof c.percentile_10y === 'number' ? `${Math.round(c.percentile_10y * 100)}` : DASH}
        {oz === null ? ' · neutral indicator' : oz > 0.3 ? ' · favourable' : oz < -0.3 ? ' · unfavourable' : ''}
      </div>
      {c.has_vintages && (
        <div className="num text-muted">
          first release {fmtMacro(c.first_release_value)} → revised {fmtMacro(c.value)} ({fmtChange(c.revision)})
        </div>
      )}
      <div className="num flex flex-wrap items-center gap-1 text-muted">
        source {c.source_id} · {c.series_id}
        {c.official !== false && <span className="rounded-sm bg-up-soft px-1 text-2xs font-semibold text-up">official</span>}
      </div>
      {c.source_url && (
        <a href={c.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
          official source page <ExternalLink className="size-3" />
        </a>
      )}
    </div>
  )
}

/** Compact tile for a headline / section cell. */
export function CellTile({ c, onOpen, spark }: { c: MCell; onOpen?: (seriesId: string) => void; spark?: ReactNode }) {
  const b = zBucket(orientedZ(c))
  return (
    <Tip content={<CellTooltip c={c} />}>
      <button
        type="button"
        onClick={() => onOpen?.(c.series_id)}
        className="flex min-w-0 flex-col rounded-md border border-border px-2 py-1.5 text-left hover:border-border-strong focus-visible:outline-2 focus-visible:outline-focus"
        style={zStyle(b)}
      >
        <span className="truncate text-2xs font-semibold tracking-wide text-muted uppercase">{c.label ?? c.indicator}</span>
        <span className="flex items-center gap-1">
          {c.value == null ? (
            <span className="num text-md font-semibold text-faint underline decoration-dotted underline-offset-2" data-reason={macroCellReason(c).code} aria-label={`Not available. ${reasonText(macroCellReason(c))}`}>{DASH}</span>
          ) : (
            <span className="num text-md font-semibold text-fg">{fmtMacro(c.value)}</span>
          )}
          <DirArrow d={c.direction} className="text-muted" />
        </span>
        <span className="num truncate text-2xs text-muted">
          {c.period_label} · z {fmtChange(c.z_10y, 1)}
          {c.status === 'stale' ? ' · stale' : ''}
        </span>
        {spark}
      </button>
    </Tip>
  )
}

/* ───────────── series drawer ───────────── */

export function SeriesDrawer({ seriesId, onClose }: { seriesId: string | null; onClose: () => void }) {
  return (
    <Dialog.Root open={!!seriesId} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="@container fixed top-0 right-0 z-50 flex h-full w-[min(640px,100vw)] flex-col border-l border-border-strong bg-surface shadow-none focus-visible:outline-none" aria-describedby={undefined}>
          {seriesId && <SeriesDrawerBody seriesId={seriesId} onClose={onClose} />}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function SeriesDrawerBody({ seriesId, onClose }: { seriesId: string; onClose: () => void }) {
  const [tf, setTf] = useState<SeriesTransform>('canonical')
  const q = useMacroSeries(seriesId, tf === 'canonical' ? 'level' : tf, false)
  const open = useOpenInStudio()
  const d = q.data
  const from = isoDaysFromNow(-3652)
  const points = d ? (tf === 'canonical' && d.canonical[0] ? d.canonical[0].points : d.points).filter((p) => p.date >= from) : []
  const units = d ? (tf === 'canonical' && d.canonical[0] ? d.canonical[0].units : d.units) : null
  return (
    <>
      <header className="flex items-start gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <Dialog.Title className="truncate text-md font-semibold text-fg">{d?.meta.title ?? seriesId}</Dialog.Title>
          <div className="num truncate text-xs text-muted">
            {seriesId}
            {d?.meta.country ? ` · ${d.meta.country}` : ''} · {d?.meta.frequency}
          </div>
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
          <X />
        </Button>
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        <Segmented
          label="Transform"
          size="xs"
          value={tf}
          onChange={setTf}
          options={[
            { value: 'canonical', label: 'Canonical' },
            { value: 'level', label: 'Level' },
            { value: 'yoy', label: 'y/y' },
            { value: 'mom', label: 'm/m' },
            { value: 'z', label: 'z-score' },
          ]}
        />
        {q.isPending ? (
          <SkeletonRows rows={6} />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : d ? (
          <>
            <MacroLines lines={[{ name: d.meta.title ?? seriesId, points }]} units={units} height={260} ariaLabel={`${seriesId} history`} shadeUsRecessions={d.meta.country === 'US'} />
            <Provenance d={d} />
            <div className="flex flex-wrap gap-2">
              <Link to={`/macro/series/${encodeURIComponent(seriesId)}`} className="inline-flex h-7 items-center rounded-md border border-border px-2.5 text-sm text-fg hover:bg-surface-2" onClick={onClose}>
                Open in series explorer
              </Link>
              <Button size="sm" variant="outline" onClick={() => open(d)}>
                <FlaskConical /> Open in Model Studio
              </Button>
            </div>
          </>
        ) : null}
      </div>
    </>
  )
}

export function Provenance({ d }: { d: NonNullable<ReturnType<typeof useMacroSeries>['data']> }) {
  const p = d.provenance
  return (
    <section className="rounded-md border border-border bg-surface-2 p-3 text-sm">
      <div className="mb-1 flex items-center gap-2">
        <h3 className="font-semibold text-fg">Provenance</h3>
        <OfficialTag official={p.official} label={p.official ? 'official source' : 'derived'} />
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-xs">
        <dt className="text-muted">Publisher / source</dt>
        <dd className="num text-fg">{p.source_id}</dd>
        <dt className="text-muted">Native code</dt>
        <dd className="num text-fg">{p.native_code}</dd>
        <dt className="text-muted">Units</dt>
        <dd className="text-fg">{d.meta.units ?? DASH}</dd>
        <dt className="text-muted">Seasonal adjustment</dt>
        <dd className="text-fg">{p.seasonal_adjustment ?? DASH}</dd>
        <dt className="text-muted">Typical release lag</dt>
        <dd className="num text-fg">{p.release_lag_days != null ? `${p.release_lag_days} days` : DASH}</dd>
        <dt className="text-muted">Last observation</dt>
        <dd className="num text-fg">{d.meta.last_observation_date ? fmtDate(d.meta.last_observation_date) : DASH}</dd>
        <dt className="text-muted">Updated in the lake</dt>
        <dd className="num text-fg">{p.updated_at ? fmtDateTime(p.updated_at) : DASH}</dd>
        {p.source_page && (
          <>
            <dt className="text-muted">Official page</dt>
            <dd className="truncate">
              <a href={p.source_page} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent-text hover:underline">
                {p.source_page.replace(/^https?:\/\//, '').slice(0, 60)} <ExternalLink className="size-3" />
              </a>
            </dd>
          </>
        )}
        {p.source_endpoint && (
          <>
            <dt className="text-muted">API endpoint</dt>
            <dd className="num truncate text-muted">{p.source_endpoint}</dd>
          </>
        )}
      </dl>
    </section>
  )
}

export function QuadrantText({ g, i }: { g: number | null | undefined; i: number | null | undefined }) {
  return (
    <span className="num text-xs text-muted">
      growth {fmtChange(g)} · inflation {fmtChange(i)}
    </span>
  )
}
