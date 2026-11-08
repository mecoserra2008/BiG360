import type { ColumnDef } from '@tanstack/react-table'
import { RotateCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import { CELL_CLASS, GAP_CLASS_LABEL, alertTone, campaignProgressText, cellTone, datasetAlerting, datasetReasonText, datasetReasonTone, datasetTone, gapPeriod, gapStatusTone, lastRefreshSource } from '@/api/ops/adapters'
import { useDataHealth } from '@/api/ops/hooks'
import { pauseText, type PauseFlag } from '@/api/ops/pause'
import type { DataGap, DataHealth, DataHealthCampaign, DataHealthDataset, DataHealthSource } from '@/api/ops/types'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtDate, fmtDateTime, fmtNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ConformalCoveragePanel } from '@/components/conformal/CoveragePanel'
import { LiveFeedsPanel } from '@/components/live/LiveFeedsPanel'
import { LiveNewsPanel } from '@/components/live/LiveNewsPanel'
import { QaPanel } from './QaPanel'
import { SyncPanel } from './SyncPanel'
import { OpsAlertBanner } from './SchedulerPanel'

/** /system/data-health: coverage registry status, freshness heatmap, gap queue, backfill campaigns, filler, sources. */
export default function DataHealthPage() {
  const q = useDataHealth()
  return (
    <>
      <PageHeader
        title="Data health"
        subtitle="What data the app should have, what is missing or stale, and how the background filler is repairing it"
        actions={
          <Button size="sm" variant="outline" onClick={() => void q.refetch()} disabled={q.isFetching}>
            <RotateCw className={q.isFetching ? 'animate-spin' : undefined} /> Refresh
          </Button>
        }
      />
      <QueryView query={q} skeleton={<Panel><SkeletonRows rows={14} /></Panel>}>
        {(d) => <Body d={d} />}
      </QueryView>
    </>
  )
}

function Body({ d }: { d: DataHealth }) {
  const fresh = d.datasets.filter((x) => x.status === 'fresh').length
  // v1.43 (alert hygiene 2026-10-30): one verdict per dataset - the sync's reason decides, not the detector's SLA rule
  const stale = d.datasets.filter(datasetAlerting).length
  const waiting = d.datasets.filter((x) => !datasetAlerting(x) && x.reason && x.reason !== 'fresh').length
  const fr = (d.freshness ?? null) as { source?: string; sync_scanned_at?: string | null } | null
  const open = (d.gaps.by_status.open ?? 0) + (d.gaps.by_status.failed ?? 0) + (d.gaps.by_status.queued ?? 0)
  const f = d.filler as { alive?: boolean; state?: string | null; reasons?: string[] }
  return (
    <div className="grid grid-cols-12 gap-3">
      {d.alerts.length > 0 && (
        <div className="col-span-12">
          <OpsAlertBanner alerts={d.alerts} />
        </div>
      )}
      <Panel className="col-span-12">
        <div className="flex flex-wrap gap-x-10 gap-y-2">
          <Stat label="Datasets fresh" value={`${fresh}/${d.datasets.length}`} sub={`profile ${String((d.profile as { active?: string }).active ?? '—')}`} />
          <Stat label="Stale beyond SLA" value={stale} sub={waiting ? `${waiting} waiting with a reason (not published yet, key, no source …)` : undefined} />
          <Stat label="Last refresh" value={d.last_refresh ? fmtDateTime(d.last_refresh.at) : '—'} sub={lastRefreshSource(d.last_refresh) || undefined} />
          <Stat label="Open gaps" value={open} sub={`${d.gaps.by_status.filling ?? 0} filling · ${d.gaps.by_status.unfillable ?? 0} unfillable · ${d.gaps.by_status.manual ?? 0} manual`} />
          <Stat label="Gap filler" value={<Badge tone={!f.alive ? 'neutral' : f.state === 'running' ? 'up' : 'warn'}>{f.alive ? f.state ?? 'running' : 'not running'}</Badge>} sub={(f.reasons ?? []).join('; ') || undefined} />
          <Stat label="Disk free" value={d.disk_free_gb != null ? `${fmtNum(d.disk_free_gb, 0)} GB` : '—'} />
          <Stat label="Last detection" value={fmtDateTime(String((d.detector as { generated_at?: string }).generated_at ?? '')) || '—'} sub={fr?.source ? `verdicts from the ${fr.source === 'sync' ? 'startup sync' : 'gap detector (no recent sync)'}` : undefined} />
        </div>
      </Panel>
      <SyncPanel className="col-span-12" />
      <QaPanel qa={d.qa} className="col-span-12" />
      <LiveFeedsPanel className="col-span-12" />
      <LiveNewsPanel className="col-span-12" />
      <ConformalCoveragePanel className="col-span-12" />
      <HeatmapPanel d={d} className="col-span-12" />
      <CampaignsPanel items={d.campaigns} className="col-span-12 xl:col-span-7" />
      <FillerPanel d={d} className="col-span-12 xl:col-span-5" />
      <GapQueuePanel d={d} className="col-span-12" />
      <SourcesPanel items={d.sources} className="col-span-12 xl:col-span-7" />
      <FailuresPanel d={d} className="col-span-12 xl:col-span-5" />
    </div>
  )
}

const LEGEND: { tone: ReturnType<typeof cellTone>; label: string }[] = [
  { tone: 'full', label: '≥ 95% of expected' },
  { tone: 'ok', label: '80–95%' },
  { tone: 'partial', label: '5–80% (partial)' },
  { tone: 'missing', label: '< 5% (missing)' },
  { tone: 'pending', label: 'not ingested yet' },
  { tone: 'none', label: 'no session' },
]

/** Dataset × day coverage heatmap (status colours always paired with the legend labels + per-cell tooltip). */
function HeatmapPanel({ d, className }: { d: DataHealth; className?: string }) {
  const byId = new Map(d.datasets.map((x) => [x.dataset, x]))
  const days = d.heatmap.days
  return (
    <Panel title="Freshness heatmap" subtitle="Coverage per day (actual / expected entities) and freshness vs SLA, last 21 days" className={className}>
      <div className="mb-2 flex flex-wrap gap-3 text-2xs text-muted" aria-label="Heatmap legend">
        {LEGEND.map((l) => (
          <span key={l.tone} className="inline-flex items-center gap-1">
            <span className={cn('inline-block size-3 rounded-[2px]', CELL_CLASS[l.tone])} aria-hidden /> {l.label}
          </span>
        ))}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-[900px] border-separate border-spacing-[2px] text-xs" aria-label="Dataset coverage by day">
          <thead>
            <tr>
              <th scope="col" className="w-44 text-left font-semibold text-muted">Dataset</th>
              <th scope="col" className="w-24 text-left font-semibold text-muted">Status</th>
              <th scope="col" className="w-24 text-left font-semibold text-muted">Data as of</th>
              {days.map((x) => (
                <th key={x} scope="col" className="num w-6 text-center text-2xs font-normal text-faint">{x.slice(8)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {d.heatmap.rows.map((r) => {
              const ds = byId.get(r.dataset)
              return (
                <tr key={r.dataset}>
                  <th scope="row" className="truncate text-left font-medium" title={ds?.detail ?? undefined}>{r.label}</th>
                  <td>{ds && <Badge tone={datasetTone(ds.status)}>{ds.status}</Badge>}</td>
                  <td className="num text-muted">{ds?.data_as_of ? fmtDate(ds.data_as_of) : '—'}</td>
                  {r.cells.map((v, i) => (
                    <td key={days[i]} className="p-0">
                      <Tip content={`${r.label} · ${days[i]}: ${v === null || v === undefined ? 'no session' : `${Math.round(v * 100)}% of expected`}${ds?.data_as_of && days[i] > ds.data_as_of ? ' (after the data as-of: not ingested yet)' : ''}`}>
                        <span tabIndex={0} className={cn('block h-4 w-6 rounded-[2px]', CELL_CLASS[cellTone(v, Boolean(ds?.data_as_of && days[i] > ds.data_as_of))])} aria-label={`${r.label} ${days[i]} ${v == null ? 'no session' : `${Math.round(v * 100)}%`}`} />
                      </Tip>
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <DatasetTable items={d.datasets} />
    </Panel>
  )
}

function DatasetTable({ items }: { items: DataHealthDataset[] }) {
  const cols = useMemo<ColumnDef<DataHealthDataset>[]>(
    () => [
      { id: 'label', header: 'Dataset', size: 190, accessorFn: (r) => r.label, cell: (c) => <span className="font-medium text-fg" title={c.row.original.dataset}>{c.getValue<string>()}</span> },
      { id: 'group', header: 'Group', size: 90, accessorFn: (r) => r.group, cell: (c) => <span className="text-muted capitalize">{c.getValue<string>()}</span> },
      { id: 'status', header: 'Status', size: 90, accessorFn: (r) => r.status, cell: (c) => <Badge tone={datasetTone(c.row.original.status)}>{c.row.original.status}</Badge> },
      {
        id: 'reason',
        header: 'Reason',
        size: 230,
        accessorFn: (r) => `${datasetAlerting(r) ? 0 : 1}${r.reason ?? ''}`,
        cell: (c) => {
          const r = c.row.original
          if (!r.reason) return <span className="text-faint">—</span>
          return (
            <span title={r.reason_detail ?? undefined}>
              <Badge tone={datasetReasonTone(r)}>{datasetReasonText(r)}</Badge>
            </span>
          )
        },
        meta: { title: 'Startup sync reason (the gap detector when no sync ran in the last 6 h); only failed, due / source unavailable past the SLA, or stale alert' },
      },
      { id: 'asof', header: 'Data as of', size: 100, accessorFn: (r) => r.data_as_of ?? '', cell: (c) => <span className="num">{c.row.original.data_as_of ?? '—'}</span> },
      { id: 'exp', header: 'Expected', size: 100, accessorFn: (r) => r.expected_as_of ?? '', cell: (c) => <span className="num text-muted">{c.row.original.expected_as_of ?? '—'}</span> },
      { id: 'lag', header: 'Lag / SLA', size: 90, accessorFn: (r) => r.lag ?? -1, cell: (c) => <span className="num">{c.row.original.lag ?? '—'} / {c.row.original.sla ?? '—'}</span>, meta: { align: 'right', title: 'Sessions (or days) behind the expected date vs the profile-adjusted SLA' } },
      { id: 'gaps', header: 'Open gaps', size: 80, accessorFn: (r) => r.open_gaps, cell: (c) => <span className="num">{c.getValue<number>()}</span>, meta: { align: 'right' } },
      { id: 'job', header: 'Live job', size: 160, accessorFn: (r) => r.live_job ?? '', cell: (c) => <span className="num text-muted">{c.row.original.live_job ?? '—'}{c.row.original.last_job_status ? ` · ${c.row.original.last_job_status}` : ''}</span> },
      { id: 'detail', header: 'Detail', size: 320, accessorFn: (r) => r.detail ?? '', enableSorting: false, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
    ],
    [],
  )
  return (
    <div className="mt-3 -mx-3 -mb-3 border-t border-border">
      <DataTable data={items} columns={cols} ariaLabel="Datasets" height={360} initialSorting={[{ id: 'status', desc: true }]} getRowId={(r) => r.dataset} />
    </div>
  )
}

function CampaignsPanel({ items, className }: { items: DataHealthCampaign[]; className?: string }) {
  return (
    <Panel title="Backfill campaigns" subtitle="Long resumable backfills run by the gap filler (progress from their own state files)" className={className}>
      <ul className="space-y-3">
        {items.map((c) => {
          const pct = Math.max(0, Math.min(100, c.progress_pct ?? 0))
          return (
            <li key={c.id}>
              <div className="flex flex-wrap items-baseline justify-between gap-2 text-sm">
                <span className="font-medium">{c.label}</span>
                <span className="flex items-center gap-2">
                  <span className="num text-xs">{campaignProgressText(c)}</span>
                  <Badge tone={c.status === 'running' ? 'info' : c.status === 'done' ? 'up' : c.status === 'backoff' ? 'warn' : 'neutral'}>{c.status}</Badge>
                </span>
              </div>
              <div
                className="mt-1 h-2 w-full overflow-hidden rounded-full bg-surface-2"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(pct)}
                aria-label={`${c.label} progress`}
              >
                <div className={cn('h-full rounded-full', c.status === 'done' ? 'bg-up' : 'bg-accent')} style={{ width: `${pct}%` }} />
              </div>
              <div className="mt-0.5 text-2xs text-muted">
                {c.done_units != null && c.total_units != null ? `${fmtNum(c.done_units, 0)} / ${fmtNum(c.total_units, 0)} ${c.unit ?? ''} · ` : ''}
                {c.rate_per_h ? `${fmtNum(c.rate_per_h, 1)} ${c.unit ?? 'units'}/h · ` : ''}
                {c.detail}
                {c.last_error ? <span className="text-down"> · {c.last_error}</span> : null}
              </div>
            </li>
          )
        })}
      </ul>
    </Panel>
  )
}

interface FillerView {
  alive?: boolean
  pid?: number | null
  heartbeat_at?: string | null
  state?: string | null
  reasons?: string[]
  sample?: Record<string, unknown>
  config?: Record<string, unknown>
  running?: { task_id: string; label?: string; pid?: number; rss_gb?: number; adopted?: boolean; started_at?: string; stopping?: boolean }[]
  waiting?: { task_id: string; reason: string }[]
  recent?: { task_id: string; label?: string; outcome?: string; finished_at?: string; error?: string | null }[]
  keep_awake?: string[]
  /** coverage v3 A4: the filler.PAUSE flag read live (reason, owner, expiry; null without a file) */
  pause?: PauseFlag | null
}

function FillerPanel({ d, className }: { d: DataHealth; className?: string }) {
  const f = d.filler as FillerView
  const sm = f.sample ?? {}
  const cfg = f.config ?? {}
  const pz = pauseText(f.pause)
  return (
    <Panel title="Gap filler & resource governor" subtitle={f.alive ? `pid ${f.pid} · heartbeat ${fmtDateTime(f.heartbeat_at ?? '')}` : 'Not running (start it with the supervisor)'} className={className}>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap gap-1.5">
          <Badge tone={f.state === 'running' ? 'up' : f.alive ? 'warn' : 'neutral'}>{f.alive ? f.state ?? '—' : 'off'}</Badge>
          {(f.reasons ?? []).map((r) => <Badge key={r} tone="warn">{r}</Badge>)}
          {(f.keep_awake ?? []).length > 0 && <Badge tone="info">keeping PC awake</Badge>}
        </div>
        {pz && (
          <p className={cn('text-xs', pz.tone === 'warn' ? 'text-warn' : 'text-muted')} title={f.pause?.detail ?? undefined}>
            {pz.text}
          </p>
        )}
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted">Free RAM</dt>
          <dd className="num">{sm.free_ram_gb != null ? `${fmtNum(Number(sm.free_ram_gb), 1)} GB` : '—'} <span className="text-muted">(pause below {String(cfg.min_free_ram_gb ?? 3)} GB)</span></dd>
          <dt className="text-muted">Filler RAM</dt>
          <dd className="num">{sm.filler_rss_gb != null ? `${fmtNum(Number(sm.filler_rss_gb), 2)} GB` : '—'} <span className="text-muted">/ cap {String(cfg.ram_cap_gb ?? 4)} GB</span></dd>
          <dt className="text-muted">Power</dt>
          <dd>{sm.on_ac === false ? 'battery' : sm.on_ac ? 'AC' : '—'}{sm.battery_pct != null ? ` · ${String(sm.battery_pct)}%` : ''}</dd>
          <dt className="text-muted">Network</dt>
          <dd>{sm.metered ? 'metered' : String(sm.network ?? '—')}</dd>
          <dt className="text-muted">Disk free</dt>
          <dd className="num">{sm.disk_free_gb != null ? `${fmtNum(Number(sm.disk_free_gb), 0)} GB` : '—'}</dd>
          <dt className="text-muted">Priority / max tasks</dt>
          <dd>{String(cfg.priority ?? 'below_normal')} · {String(cfg.max_concurrency ?? 2)}</dd>
        </dl>
        <div>
          <div className="label-caps mb-1">Running</div>
          {(f.running ?? []).length === 0 ? (
            <div className="text-xs text-muted">nothing</div>
          ) : (
            <ul className="space-y-0.5 text-xs">
              {(f.running ?? []).map((t) => (
                <li key={t.task_id}>
                  <span className="font-medium">{t.label ?? t.task_id}</span>{' '}
                  <span className="num text-muted">pid {t.pid} · {fmtNum(t.rss_gb ?? 0, 2)} GB{t.adopted ? ' · adopted' : ''}{t.stopping ? ' · stopping' : ''}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {(f.waiting ?? []).length > 0 && (
          <div>
            <div className="label-caps mb-1">Waiting</div>
            <ul className="max-h-28 space-y-0.5 overflow-y-auto text-xs">
              {(f.waiting ?? []).slice(0, 12).map((w) => (
                <li key={w.task_id}>
                  <span className="num">{w.task_id}</span> <span className="text-muted">— {w.reason}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {(f.recent ?? []).length > 0 && (
          <div>
            <div className="label-caps mb-1">Recent tasks</div>
            <ul className="max-h-36 space-y-0.5 overflow-y-auto text-xs">
              {(f.recent ?? []).slice(0, 12).map((r, i) => (
                <li key={`${r.task_id}-${i}`}>
                  <Badge tone={r.outcome === 'ok' ? 'up' : r.outcome === 'failed' ? 'down' : 'neutral'}>{r.outcome}</Badge>{' '}
                  <span>{r.label ?? r.task_id}</span> <span className="num text-muted">{fmtDateTime(r.finished_at ?? '')}</span>
                  {r.error ? <span className="text-down"> · {r.error.slice(0, 120)}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Panel>
  )
}

type GapFilter = 'active' | 'unfillable' | 'manual' | 'all'

function GapQueuePanel({ d, className }: { d: DataHealth; className?: string }) {
  const [flt, setFlt] = useState<GapFilter>('active')
  const items = d.gaps.items.filter((g) =>
    flt === 'all' ? true : flt === 'active' ? ['open', 'queued', 'filling', 'failed'].includes(g.status) : g.status === flt,
  )
  const cols = useMemo<ColumnDef<DataGap>[]>(
    () => [
      { id: 'prio', header: 'Prio', size: 56, accessorFn: (r) => r.priority ?? 99, cell: (c) => <span className="num">{c.getValue<number>()}</span>, meta: { align: 'right' } },
      { id: 'dataset', header: 'Dataset', size: 130, accessorFn: (r) => r.dataset, cell: (c) => <span className="num">{c.getValue<string>()}</span> },
      { id: 'entity', header: 'Entity', size: 120, accessorFn: (r) => r.entity, cell: (c) => <span className="num">{c.getValue<string>()}</span> },
      { id: 'class', header: 'Class', size: 90, accessorFn: (r) => r.gap_class, cell: (c) => <Badge tone={c.row.original.gap_class === 'unfillable' ? 'down' : c.row.original.gap_class === 'suspicious' ? 'warn' : 'neutral'}>{GAP_CLASS_LABEL[c.row.original.gap_class]}</Badge> },
      { id: 'status', header: 'Status', size: 90, accessorFn: (r) => r.status, cell: (c) => <Badge tone={gapStatusTone(c.row.original.status)}>{c.row.original.status}</Badge> },
      { id: 'period', header: 'Period', size: 170, accessorFn: (r) => r.period_start ?? '', cell: (c) => <span className="num">{gapPeriod(c.row.original)}</span> },
      { id: 'cov', header: 'Actual / exp.', size: 100, accessorFn: (r) => r.actual ?? -1, cell: (c) => <span className="num">{c.row.original.actual ?? '—'} / {c.row.original.expected ?? '—'}</span>, meta: { align: 'right' } },
      { id: 'att', header: 'Tries', size: 56, accessorFn: (r) => r.attempts ?? 0, cell: (c) => <span className="num">{c.getValue<number>()}</span>, meta: { align: 'right' } },
      { id: 'next', header: 'Next try', size: 130, accessorFn: (r) => r.next_attempt_at ?? '', cell: (c) => <span className="num text-muted">{c.row.original.next_attempt_at ? fmtDateTime(c.row.original.next_attempt_at) : '—'}</span> },
      { id: 'note', header: 'Note / last error', size: 420, accessorFn: (r) => r.note ?? '', enableSorting: false, cell: (c) => <span className={c.row.original.last_error ? 'text-down' : 'text-muted'} title={c.row.original.last_error ?? undefined}>{c.row.original.last_error ? `${c.row.original.last_error.slice(0, 160)} · ` : ''}{c.row.original.note}</span> },
    ],
    [],
  )
  return (
    <Panel
      title="Gap queue"
      subtitle={`${d.gaps.total} gaps tracked · ${Object.entries(d.gaps.by_class).map(([k, v]) => `${k} ${v}`).join(' · ') || 'none open'}`}
      className={className}
      flush
      actions={<Segmented label="Gap filter" value={flt} onChange={setFlt} options={[{ value: 'active', label: 'Active' }, { value: 'unfillable', label: 'Unfillable' }, { value: 'manual', label: 'Manual' }, { value: 'all', label: 'All' }]} />}
    >
      <DataTable data={items} columns={cols} ariaLabel="Gap queue" height={380} initialSorting={[{ id: 'prio', desc: false }]} getRowId={(r) => r.gap_id} />
    </Panel>
  )
}

function SourcesPanel({ items, className }: { items: DataHealthSource[]; className?: string }) {
  const cols = useMemo<ColumnDef<DataHealthSource>[]>(
    () => [
      { id: 'source', header: 'Source', size: 120, accessorFn: (r) => r.source, cell: (c) => <span className="num font-medium">{c.getValue<string>()}</span> },
      { id: 'status', header: 'Status', size: 90, accessorFn: (r) => r.status, cell: (c) => <Badge tone={c.row.original.status === 'ok' ? 'up' : c.row.original.status === 'failing' ? 'down' : c.row.original.status === 'degraded' ? 'warn' : 'neutral'}>{c.row.original.status}</Badge> },
      {
        id: 'budget',
        header: 'Filler budget (1 h)',
        size: 190,
        accessorFn: (r) => r.pct ?? -1,
        cell: (c) => {
          const r = c.row.original
          if (r.per_hour == null) return <span className="text-faint">—</span>
          const pct = Math.min(100, r.pct ?? 0)
          return (
            <span className="flex items-center gap-2">
              <span className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-2" aria-hidden>
                <span className={cn('block h-full rounded-full', pct >= 90 ? 'bg-warn' : 'bg-accent')} style={{ width: `${pct}%` }} />
              </span>
              <span className="num text-xs">{fmtNum(r.used_1h ?? 0, 0)} / {fmtNum(r.per_hour, 0)} {r.unit}</span>
            </span>
          )
        },
      },
      { id: 'runs', header: 'Runs 14d', size: 80, accessorFn: (r) => r.runs_14d, cell: (c) => <span className="num">{c.row.original.runs_14d} ({c.row.original.failures_14d} ✕)</span>, meta: { align: 'right' } },
      { id: 'ok', header: 'Last OK', size: 130, accessorFn: (r) => r.last_ok_at ?? '', cell: (c) => <span className="num text-muted">{c.row.original.last_ok_at ? fmtDateTime(c.row.original.last_ok_at) : '—'}</span> },
      { id: 'err', header: 'Last error', size: 300, accessorFn: (r) => r.last_error ?? '', enableSorting: false, cell: (c) => <span className="text-down" title={c.row.original.note ?? undefined}>{c.getValue<string>()}</span> },
    ],
    [],
  )
  return (
    <Panel title="Sources" subtitle="Live-job health per source (14 days) and filler rate-budget use" className={className} flush>
      <DataTable data={items} columns={cols} ariaLabel="Sources" height={340} initialSorting={[{ id: 'status', desc: false }]} getRowId={(r) => r.source} />
    </Panel>
  )
}

interface FailureRow {
  run_id: string
  job_id: string
  started_at?: string
  error_type?: string | null
  error_message?: string | null
}

function FailuresPanel({ d, className }: { d: DataHealth; className?: string }) {
  const rows = (d.failures ?? []) as unknown as FailureRow[]
  const br = (d.sla_breaches ?? []) as unknown as { dataset: string; label: string; lag?: number | null; sla?: number | null; data_as_of?: string | null; reason?: string | null }[]
  const rf = d.refresh_failures ?? []
  return (
    <Panel title="SLA breaches & recent failures" subtitle="Last 7 days of failed job runs" className={className}>
      <div className="space-y-3 text-xs">
        <div>
          <div className="label-caps mb-1">SLA breaches</div>
          {br.length === 0 ? (
            <span className="text-up">none</span>
          ) : (
            <ul className="space-y-0.5">
              {br.map((b) => (
                <li key={b.dataset}>
                  <Badge tone="warn">{b.label}</Badge> <span className="num text-muted">{b.reason ? `${b.reason.replace(/_/g, ' ')} · ` : ''}as of {b.data_as_of ?? '—'} · lag {b.lag ?? '—'} / SLA {b.sla ?? '—'}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <div className="label-caps mb-1">Refresh jobs failing (latest attempt)</div>
          {rf.length === 0 ? (
            <span className="text-up">none</span>
          ) : (
            <ul className="space-y-0.5">
              {rf.map((f) => (
                <li key={f.job_id}>
                  <span className="num font-medium">{f.job_id}</span> <span className="num text-muted">{f.dag ?? '—'} · since {fmtDateTime(f.since ?? '')}</span>
                  {f.error ? <div className="text-down">{f.error}</div> : null}
                </li>
              ))}
            </ul>
          )}
        </div>
        <div>
          <div className="label-caps mb-1">Failed runs</div>
          {rows.length === 0 ? (
            <span className="text-up">none</span>
          ) : (
            <ul className="max-h-72 space-y-1 overflow-y-auto">
              {rows.map((r) => (
                <li key={r.run_id}>
                  <span className="num font-medium">{r.job_id}</span> <span className="num text-muted">{fmtDateTime(r.started_at ?? '')}</span>
                  <div className="text-down">{r.error_type ? `${r.error_type}: ` : ''}{(r.error_message ?? '').slice(0, 200)}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="flex flex-wrap gap-1">
          {d.alerts.filter((a) => a.severity === 'info').map((a) => <Badge key={a.id} tone={alertTone(a.severity)} title={a.detail ?? undefined}>{a.title}</Badge>)}
        </div>
      </div>
    </Panel>
  )
}
