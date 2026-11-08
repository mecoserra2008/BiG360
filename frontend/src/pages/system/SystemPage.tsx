import type { ColumnDef } from '@tanstack/react-table'
import { RotateCw } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { JobRun, SourceFreshness, SystemStatus } from '@/api/types'
import { API_BASE_URL, API_PREFIX, USE_MOCK } from '@/api/client'
import { useFactorModel, useHealth, useSystemStatus } from '@/api/hooks'
import { statusCacheAlert } from '@/api/ops/adapters'
import type { FactorFamily } from '@/api/types'
import { FACTOR_FAMILIES, FACTOR_LABEL } from '@/lib/factors'
import { LiveHealthPanel } from '@/components/live/LiveHealthPanel'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { Button } from '@/components/ui/button'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { StatusBadge } from '@/components/ui/status'
import { fmtCompact, fmtDateTime, fmtDuration, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { REGION_LABEL } from '@/lib/regions'
import { useUi } from '@/store'
import { OpsAlertBanner, ScheduleSettingsPanel, SchedulerPanel } from './SchedulerPanel'
import { type SourceGroup, groupSources } from './sourceGroups'

const regionName = (r: string) => (r === 'GLOBAL' ? 'Global' : (REGION_LABEL[r as keyof typeof REGION_LABEL] ?? r))

export default function SystemPage() {
  const q = useSystemStatus()
  return (
    <>
      <PageHeader
        title="System"
        subtitle="Scheduler, schedule profile, ingestion jobs and data freshness per source"
        actions={
          <Button size="sm" variant="outline" onClick={() => void q.refetch()} disabled={q.isFetching}>
            <RotateCw className={q.isFetching ? 'animate-spin' : undefined} /> Refresh
          </Button>
        }
      />
      <QueryView query={q} skeleton={<Panel><SkeletonRows rows={16} /></Panel>}>
        {(d) => <SystemBody d={d} />}
      </QueryView>
    </>
  )
}

function SystemBody({ d }: { d: SystemStatus }) {
  const [jobFilter, setJobFilter] = useState<'all' | 'failed' | 'active'>('all')
  const [srcView, setSrcView] = useState<'dataset' | 'region'>('dataset')
  const groups = useMemo(() => groupSources(d.sources), [d.sources])
  const groupCols = useMemo<ColumnDef<SourceGroup>[]>(
    () => [
      { id: 'source', header: 'Source', size: 150, accessorFn: (r) => r.source, sortDescFirst: false, cell: (c) => <span className="font-medium text-fg">{c.getValue<string>()}</span> },
      { id: 'desc', header: 'Dataset', size: 230, accessorFn: (r) => r.description, enableSorting: false, cell: (c) => <span className="text-muted" title={c.row.original.jobId ? `job ${c.row.original.jobId}` : undefined}>{c.getValue<string>()}</span> },
      {
        id: 'regions',
        header: 'Regions',
        size: 96,
        accessorFn: (r) => r.regions.length,
        cell: (c) => {
          const g = c.row.original
          const tip = `${g.regions.map(regionName).join(', ')}${g.best && g.worst && g.nRows > 1 ? ` · freshest ${regionName(g.best.region)} (${fmtNum(g.best.lagHours, 0)}h) · stalest ${regionName(g.worst.region)} (${fmtNum(g.worst.lagHours, 0)}h)` : ''}`
          return g.regions.length === 1 ? <Badge title={tip}>{regionName(g.regions[0])}</Badge> : <span className="num text-muted" title={tip}>{g.regions.length} regions</span>
        },
        meta: { title: 'Hover for the freshest / stalest region' },
      },
      { id: 'cadence', header: 'Cadence', size: 84, accessorFn: (r) => r.cadence, cell: (c) => <span className="text-muted capitalize">{c.getValue<string>()}</span> },
      { id: 'last', header: 'Last success', size: 140, accessorFn: (r) => r.lastSuccess, cell: (c) => <span className="num">{fmtDateTime(c.getValue<string>())}</span> },
      { id: 'lag', header: 'Lag (worst)', size: 84, accessorFn: (r) => r.lagHours, cell: (c) => <span className="num">{fmtNum(c.getValue<number>(), 1)}h</span>, meta: { align: 'right' } },
      { id: 'coverage', header: 'Coverage', size: 84, accessorFn: (r) => r.coverage, cell: (c) => <span className="num">{fmtCompact(c.getValue<number>(), 1)}</span>, meta: { align: 'right', title: 'Entities / series covered (all regions)' } },
      { id: 'dq', header: 'DQ', size: 76, accessorFn: (r) => r.dqStatus ?? '', cell: (c) => {
        const v = c.row.original.dqStatus
        return v ? <Badge tone={v === 'ok' ? 'up' : v === 'warn' ? 'warn' : v === 'error' ? 'down' : 'neutral'}>{v === 'ok' ? '✓' : v === 'warn' ? '!' : v === 'error' ? '✕' : '·'} {v.replace('_', ' ')}</Badge> : <span className="text-faint">—</span>
      }, meta: { title: 'Worst data-quality result across regions' } },
      {
        id: 'status',
        header: 'Status',
        size: 120,
        accessorFn: (r) => ({ fresh: 0, stale: 1, error: 2 })[r.status],
        cell: (c) => {
          const g = c.row.original
          return (
            <span className="inline-flex items-center gap-1.5">
              <StatusBadge status={g.status} />
              {g.nRows > 1 && <span className="num text-2xs text-muted" title="fresh / stale / error rows">{g.counts.fresh}/{g.counts.stale}/{g.counts.error}</span>}
            </span>
          )
        },
      },
    ],
    [],
  )
  const locale = useUi((s) => s.locale)
  const setLocale = useUi((s) => s.setLocale)

  const sourceCols = useMemo<ColumnDef<SourceFreshness>[]>(
    () => [
      { id: 'source', header: 'Source', size: 150, accessorFn: (r) => r.source, sortDescFirst: false, cell: (c) => <span className="font-medium text-fg">{c.getValue<string>()}</span> },
      { id: 'desc', header: 'Dataset', size: 230, accessorFn: (r) => r.description, enableSorting: false, cell: (c) => <span className="text-muted" title={c.row.original.jobId ? `job ${c.row.original.jobId}` : undefined}>{c.getValue<string>()}</span> },
      { id: 'region', header: 'Region', size: 72, accessorFn: (r) => r.region, cell: (c) => <Badge>{c.row.original.region === 'GLOBAL' ? 'Global' : REGION_LABEL[c.row.original.region]}</Badge> },
      { id: 'cadence', header: 'Cadence', size: 84, accessorFn: (r) => r.cadence, cell: (c) => <span className="text-muted capitalize">{c.getValue<string>()}</span> },
      { id: 'last', header: 'Last success', size: 140, accessorFn: (r) => r.lastSuccess, cell: (c) => <span className="num">{fmtDateTime(c.getValue<string>())}</span> },
      { id: 'lag', header: 'Lag', size: 72, accessorFn: (r) => r.lagHours, cell: (c) => <span className="num">{fmtNum(c.getValue<number>(), 1)}h</span>, meta: { align: 'right' } },
      { id: 'coverage', header: 'Coverage', size: 84, accessorFn: (r) => r.coverage, cell: (c) => <span className="num">{fmtCompact(c.getValue<number>(), 1)}</span>, meta: { align: 'right', title: 'Entities / series covered' } },
      { id: 'dq', header: 'DQ', size: 76, accessorFn: (r) => r.dqStatus ?? '', cell: (c) => {
        const v = c.row.original.dqStatus
        return v ? <Badge tone={v === 'ok' ? 'up' : v === 'warn' ? 'warn' : v === 'error' ? 'down' : 'neutral'}>{v === 'ok' ? '✓' : v === 'warn' ? '!' : v === 'error' ? '✕' : '·'} {v.replace('_', ' ')}</Badge> : <span className="text-faint">—</span>
      }, meta: { title: 'Data-quality checks on the latest load' } },
      { id: 'status', header: 'Status', size: 90, accessorFn: (r) => ({ fresh: 0, stale: 1, error: 2 })[r.status], cell: (c) => <StatusBadge status={c.row.original.status} /> },
    ],
    [],
  )

  const jobCols = useMemo<ColumnDef<JobRun>[]>(
    () => [
      { id: 'id', header: 'Run', size: 84, accessorFn: (r) => r.id, cell: (c) => <span className="num text-muted">{c.getValue<string>()}</span> },
      { id: 'job', header: 'Job', size: 220, accessorFn: (r) => r.job, cell: (c) => <span className="num">{c.getValue<string>()}</span> },
      { id: 'status', header: 'Status', size: 96, accessorFn: (r) => r.status, cell: (c) => <StatusBadge status={c.row.original.status} /> },
      { id: 'started', header: 'Started', size: 140, accessorFn: (r) => r.startedAt, cell: (c) => <span className="num">{fmtDateTime(c.getValue<string>())}</span> },
      { id: 'dur', header: 'Duration', size: 84, accessorFn: (r) => r.durationSec, cell: (c) => <span className="num">{c.row.original.status === 'queued' ? '—' : fmtDuration(c.getValue<number>())}</span>, meta: { align: 'right' } },
      { id: 'rows', header: 'Rows', size: 84, accessorFn: (r) => r.rowsWritten, cell: (c) => <span className="num">{fmtInt(c.getValue<number>())}</span>, meta: { align: 'right' } },
      { id: 'msg', header: 'Message', size: 360, accessorFn: (r) => r.message, enableSorting: false, cell: (c) => <span className={c.row.original.status === 'failed' ? 'text-down' : 'text-muted'}>{c.getValue<string>()}</span> },
    ],
    [],
  )

  const jobs = d.jobs.filter((j) => jobFilter === 'all' || (jobFilter === 'failed' ? j.status === 'failed' || j.status === 'partial' : j.status === 'running' || j.status === 'queued'))
  const fresh = d.sources.filter((s) => s.status === 'fresh').length
  const done = d.jobs.filter((j) => j.status === 'success' || j.status === 'failed')
  const successRate = done.length ? done.filter((j) => j.status === 'success').length / done.length : 0
  // coverage v3 A7: a status payload with failed sections (last good value <= ~10 min, or nothing) says so up front
  const cacheAlert = statusCacheAlert(d.statusCache)
  const alerts = [...(cacheAlert ? [cacheAlert] : []), ...(d.opsAlerts ?? [])]

  return (
    <div className="grid grid-cols-12 gap-3">
      {alerts.length > 0 && (
        <div className="col-span-12">
          <OpsAlertBanner alerts={alerts} />
        </div>
      )}
      <SchedulerPanel d={d} className="col-span-12 xl:col-span-7" />
      <ScheduleSettingsPanel className="col-span-12 xl:col-span-5" />
      <BackendPanel className="col-span-12 xl:col-span-5" />
      <FactorModelPanel className="col-span-12 xl:col-span-7" />
      <LiveHealthPanel className="col-span-12" />
      <Panel className="col-span-12 xl:col-span-8">
        <div className="flex flex-wrap gap-x-10 gap-y-2">
          <Stat label="Sources fresh" value={`${fresh}/${d.sources.length}`} />
          <Stat label="Stale" value={d.sources.filter((s) => s.status === 'stale').length} />
          <Stat label="Errors" value={d.sources.filter((s) => s.status === 'error').length} />
          <Stat label="Job success rate" value={fmtPct(successRate, 0)} sub={`last ${done.length} completed`} />
          <Stat label="Status as of" value={fmtDateTime(d.asOf)} />
        </div>
      </Panel>
      <Panel title="Preferences & connection" className="col-span-12 xl:col-span-4">
        <dl className="grid grid-cols-[auto_1fr] items-center gap-x-4 gap-y-2 text-sm">
          <dt className="text-muted">Data mode</dt>
          <dd>{USE_MOCK ? <Badge tone="warn">Mock (VITE_USE_MOCK=true)</Badge> : <Badge tone="up">Live API</Badge>}</dd>
          <dt className="text-muted">API</dt>
          <dd className="num truncate text-xs">{API_BASE_URL}{API_PREFIX}</dd>
          <dt className="text-muted">Number locale</dt>
          <dd>
            <Select
              label="Number and date locale"
              value={locale}
              onChange={setLocale}
              className="w-44"
              options={[
                { value: 'en-US', label: 'English (US)' },
                { value: 'en-GB', label: 'English (UK)' },
                { value: 'pt-PT', label: 'Português (PT)' },
                { value: 'de-DE', label: 'Deutsch (DE)' },
                { value: 'fr-FR', label: 'Français (FR)' },
                { value: 'ja-JP', label: '日本語 (JP)' },
              ]}
            />
          </dd>
        </dl>
        <p className="mt-3 text-2xs text-faint">
          Charts: <a className="underline hover:text-fg" href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView Lightweight Charts™</a> · Apache ECharts. Map: Natural Earth via world-atlas. Events: GDELT Project.
        </p>
      </Panel>
      <Panel
        title="Data sources"
        subtitle={srcView === 'dataset' ? `Freshness vs expected cadence · ${groups.length} datasets (${d.sources.length} region rows) · prices, FX and macro first` : 'Freshness vs expected cadence · one row per source × region'}
        className="col-span-12"
        flush
        actions={<Segmented label="Source rows" value={srcView} onChange={setSrcView} options={[{ value: 'dataset', label: 'Per dataset' }, { value: 'region', label: 'Per region' }]} />}
      >
        {srcView === 'dataset' ? (
          <DataTable data={groups} columns={groupCols} ariaLabel="Data source freshness per dataset" getRowId={(r) => r.key} />
        ) : (
          <DataTable data={d.sources} columns={sourceCols} ariaLabel="Data source freshness" initialSorting={[{ id: 'status', desc: true }]} getRowId={(r, i) => `${r.source}-${r.region}-${i}`} />
        )}
      </Panel>
      <Panel
        title="Job runs"
        subtitle="APScheduler ingestion and analytics jobs"
        className="col-span-12"
        flush
        actions={<Segmented label="Job filter" value={jobFilter} onChange={setJobFilter} options={[{ value: 'all', label: 'All' }, { value: 'failed', label: 'Failed / partial' }, { value: 'active', label: 'Running / queued' }]} />}
      >
        <DataTable data={jobs} columns={jobCols} ariaLabel="Job runs" height={420} initialSorting={[{ id: "started", desc: true }]} getRowId={(r, i) => `${r.id}-${i}`} />
      </Panel>
    </div>
  )
}

/** GET /health (+ keys_present / universe counts from /system/status). */
function BackendPanel({ className }: { className?: string }) {
  const h = useHealth()
  return (
    <Panel title="Backend" subtitle={USE_MOCK ? 'Mock mode — no backend contacted' : `${API_BASE_URL} · /health`} className={className}>
      <QueryView query={h} skeleton={<SkeletonRows rows={5} />}>
        {(d) => (
          <div className="space-y-3">
            <div className="flex flex-wrap gap-x-8 gap-y-2">
              <Stat label="Status" value={<Badge tone={d.status === 'ok' ? 'up' : 'warn'}>{d.status === 'ok' ? '✓ ok' : '! degraded'}</Badge>} sub={d.status === 'degraded' ? 'no serving snapshot yet' : undefined} />
              <Stat label="API version" value={d.version} />
              <Stat label="Serving snapshot" value={d.servingRunId ?? '—'} sub={d.servingBuiltAt ? fmtDateTime(d.servingBuiltAt) : 'not built'} />
              <Stat label="Server time" value={fmtDateTime(d.time)} />
            </div>
            <div>
              <div className="label-caps mb-1">API keys configured</div>
              <div className="flex flex-wrap gap-1.5">
                {Object.keys(d.keysPresent).length === 0 && <span className="text-xs text-muted">—</span>}
                {Object.entries(d.keysPresent).map(([k, on]) => (
                  <Badge key={k} tone={on ? 'up' : 'neutral'}>
                    {on ? '✓' : '✕'} {k}
                  </Badge>
                ))}
              </div>
            </div>
            {Object.keys(d.universeCounts).length > 0 && (
              <div className="text-xs text-muted">
                Universes: {Object.entries(d.universeCounts).map(([k, v]) => `${k} ${fmtInt(v)}`).join(' · ')}
              </div>
            )}
          </div>
        )}
      </QueryView>
    </Panel>
  )
}

/** GET /meta/factors — factor families and weight profiles used by the composite score. */
function FactorModelPanel({ className }: { className?: string }) {
  const f = useFactorModel()
  return (
    <Panel title="Factor model" subtitle="Families and composite weight profiles · /meta/factors" className={className} flush>
      <QueryView query={f} skeleton={<SkeletonRows rows={8} className="p-3" />}>
        {(d) => {
          const profiles = Object.keys(d.profiles)
          const desc = new Map(d.factors.map((x) => [x.family, x]))
          return (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-sm">
                <thead>
                  <tr className="text-2xs tracking-wide text-muted uppercase">
                    <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Factor</th>
                    {profiles.map((p) => (
                      <th key={p} scope="col" className={`border-b border-border px-2 text-right font-semibold ${p === d.defaultProfile ? 'text-accent-text' : ''}`}>
                        {p.replace(/_/g, ' ')}
                        {p === d.defaultProfile ? ' ★' : ''}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {FACTOR_FAMILIES.map((fam: FactorFamily) => (
                    <tr key={fam} className="hover:bg-surface-2">
                      <th scope="row" className="h-[26px] border-b border-border px-3 text-left font-normal" title={desc.get(fam)?.description}>
                        {desc.get(fam)?.name ?? FACTOR_LABEL[fam]}
                      </th>
                      {profiles.map((p) => {
                        const w = d.profiles[p][fam]
                        return (
                          <td key={p} className="num border-b border-border px-2 text-right">
                            {w ? fmtPct(w, 0) : <span className="text-faint">—</span>}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="flex items-start gap-2 border-t border-border px-3 py-2 text-xs text-muted" data-testid="composite-validation-note">
                <CompositeLabel profileLevel />
                <span>
                  {d.compositeValidation.note}
                  {d.compositeValidation.signalWording ? ' A pass applies only to the composites of the population it was tested on; every other composite stays descriptive.' : ''}
                  {d.compositeValidation.evaluatedAt ? ` Gate recorded ${fmtDateTime(d.compositeValidation.evaluatedAt)}.` : ' No out-of-sample gate recorded yet (job composite_validation).'}
                </span>
              </p>
            </div>
          )
        }}
      </QueryView>
    </Panel>
  )
}
