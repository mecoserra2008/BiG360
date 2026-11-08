import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { ColumnDef } from '@tanstack/react-table'
import { BellRing, CalendarClock, Copy, FolderOpen, GitCompareArrows, Pencil, Play, Trash2, X } from 'lucide-react'
import { Link } from 'react-router'
import { useMemo, useState } from 'react'
import { studioApi } from '@/api/studio/client'
import { sk, useStudioCompare, useStudioMethods, useStudioPeerSets, useStudioRuns, useStudioStudies } from '@/api/studio/hooks'
import type { CStudySpec, StudioCompareView, StudioRunStatusView, StudioStudyView } from '@/api/studio/types'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox, Input, Select } from '@/components/ui/inputs'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { DASH, fmtDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { OosCompare } from './compare/OosCompare'
import { fmtP, fmtSeconds, fmtStat, fmtWhen, labelOf } from './fmt'
import { RunStateBadge } from './results/ResultsPanel'
import { useStudio } from './store'

const nObs = (r: StudioRunStatusView): number | null => r.nObs ?? (typeof r.metrics.n_obs === 'number' ? r.metrics.n_obs : null)

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))

/** The run's headline metric for the list (first of r2 / auc / silhouette / pc1_share / aic). */
function headline(r: StudioRunStatusView): string {
  for (const k of ['adj_r2', 'r2', 'pseudo_r2', 'auc', 'silhouette', 'pc1_share', 'oos_relative_rmse', 'aic']) {
    const v = r.metrics[k]
    if (typeof v === 'number') return `${labelOf(k)} ${fmtStat(v, 3)}`
  }
  return DASH
}

function StudyRow({ s, onRun }: { s: StudioStudyView; onRun: (spec: CStudySpec, studyId: string) => void }) {
  const qc = useQueryClient()
  const methods = useStudioMethods()
  const loadSpec = useStudio((x) => x.loadSpec)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [confirm, setConfirm] = useState(false)
  const done = () => void qc.invalidateQueries({ queryKey: sk.studies() })
  const rename = useMutation({ mutationFn: (name: string) => studioApi.updateStudy(s.studyId, { name }), onSuccess: () => (done(), setRenaming(null)), onError: (e) => toast({ tone: 'error', title: 'Rename failed', message: errMsg(e) }) })
  const dup = useMutation({
    mutationFn: () => studioApi.createStudy({ name: `${s.name} (copy)`, description: s.description, study: { ...s.study, name: `${s.name} (copy)` }, tags: s.tags, presetId: s.presetId }),
    onSuccess: (n) => (done(), toast({ tone: 'success', title: 'Study duplicated', message: n.name })),
    onError: (e) => toast({ tone: 'error', title: 'Duplicate failed', message: errMsg(e) }),
  })
  const del = useMutation({
    mutationFn: () => studioApi.deleteStudy(s.studyId),
    onSuccess: () => {
      done()
      if (useStudio.getState().studyId === s.studyId) useStudio.getState().setStudyId(null)
      toast({ tone: 'info', title: 'Study deleted', message: s.name })
    },
    onError: (e) => toast({ tone: 'error', title: 'Delete failed', message: errMsg(e) }),
  })
  const open = () => {
    const m = methods.data?.find((x) => x.id === s.method)
    loadSpec({ ...s.study, name: s.name }, { needsY: m?.needsY, studyId: s.studyId, presetId: s.presetId, runId: s.lastRunId })
  }
  return (
    <li className="flex items-center gap-2 px-3 py-2 hover:bg-surface-2">
      <div className="min-w-0 flex-1">
        {renaming !== null ? (
          <form
            className="flex items-center gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              if (renaming.trim()) rename.mutate(renaming.trim())
            }}
          >
            <Input autoFocus aria-label="New study name" className="h-6 flex-1" value={renaming} onChange={(e) => setRenaming(e.target.value)} />
            <Button size="xs" type="submit" variant="primary" disabled={rename.isPending}>
              Save
            </Button>
            <Button size="icon-sm" variant="ghost" aria-label="Cancel rename" onClick={() => setRenaming(null)}>
              <X />
            </Button>
          </form>
        ) : (
          <button type="button" className="block max-w-full truncate text-left text-sm font-medium text-fg hover:text-accent-text" onClick={open}>
            {s.name}
          </button>
        )}
        <div className="num mt-0.5 flex flex-wrap gap-x-2 text-2xs text-muted">
          <span>{methods.data?.find((m) => m.id === s.method)?.title ?? s.method}</span>
          <span>updated {fmtDateTime(s.updatedAt)}</span>
          {s.presetId && <span>from preset</span>}
          {s.lastRunId && <span>last run {s.lastRunId}</span>}
        </div>
        <ScheduleRow s={s} />
      </div>
      <div className="flex shrink-0 items-center">
        <Button size="icon-sm" variant="ghost" title="Open in builder" aria-label={`Open ${s.name}`} onClick={open}>
          <FolderOpen />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          title="Run now"
          aria-label={`Run ${s.name}`}
          onClick={() => {
            open()
            onRun(s.study, s.studyId)
          }}
        >
          <Play />
        </Button>
        <Button size="icon-sm" variant="ghost" title="Rename" aria-label={`Rename ${s.name}`} onClick={() => setRenaming(s.name)}>
          <Pencil />
        </Button>
        <Button size="icon-sm" variant="ghost" title="Duplicate" aria-label={`Duplicate ${s.name}`} disabled={dup.isPending} onClick={() => dup.mutate()}>
          <Copy />
        </Button>
        {confirm ? (
          <Button size="xs" variant="danger" onClick={() => del.mutate()} onBlur={() => setConfirm(false)} disabled={del.isPending}>
            Delete?
          </Button>
        ) : (
          <Button size="icon-sm" variant="ghost" title="Delete" aria-label={`Delete ${s.name}`} onClick={() => setConfirm(true)}>
            <Trash2 />
          </Button>
        )}
      </div>
    </li>
  )
}

/** Scheduled re-runs (studio_scheduled job) + drift alert threshold; PATCH /studio/studies/{id}. */
function ScheduleRow({ s }: { s: StudioStudyView }) {
  const qc = useQueryClient()
  const [thr, setThr] = useState<string | null>(null)
  const m = useMutation({
    mutationFn: (patch: { schedule?: 'none' | 'weekly' | 'monthly'; drift_threshold?: number }) => studioApi.updateStudy(s.studyId, patch),
    onSuccess: (st, patch) => {
      void qc.invalidateQueries({ queryKey: sk.studies() })
      setThr(null)
      if (patch.schedule) toast({ tone: 'success', title: patch.schedule === 'none' ? 'Schedule removed' : `Scheduled ${patch.schedule}`, message: st.alertId ? `${st.name} · drift alert ${st.alertId}` : st.name })
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not update the schedule', message: errMsg(e) }),
  })
  const pct = s.driftThreshold !== null ? String(Math.round(s.driftThreshold * 100)) : '20'
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-muted">
      <CalendarClock className="size-3.5 text-faint" aria-hidden />
      <Select
        label={`Schedule for ${s.name}`}
        className="[&_select]:h-6 [&_select]:text-2xs"
        value={s.schedule}
        disabled={m.isPending}
        onChange={(v) => m.mutate({ schedule: v })}
        options={[
          { value: 'none', label: 'Not scheduled' },
          { value: 'weekly', label: 'Re-run weekly' },
          { value: 'monthly', label: 'Re-run monthly' },
        ]}
      />
      {s.schedule !== 'none' && (
        <label className="inline-flex items-center gap-1">
          drift alert at
          <Input
            type="number"
            aria-label={`Drift threshold for ${s.name} (%)`}
            className="num h-6 w-14 px-1 text-2xs"
            value={thr ?? pct}
            onChange={(e) => setThr(e.target.value)}
            onBlur={() => {
              const v = Number(thr)
              if (thr !== null && Number.isFinite(v) && v > 0 && v !== Number(pct)) m.mutate({ drift_threshold: v / 100 })
              else setThr(null)
            }}
          />
          % worse
        </label>
      )}
      {s.lastScheduledAt && <span>last scheduled run {fmtDateTime(s.lastScheduledAt)}</span>}
      {s.alertId && (
        <Link to="/watchlist" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
          <BellRing className="size-3" /> drift alert
        </Link>
      )}
    </div>
  )
}

function PeerSetsPanel() {
  const qc = useQueryClient()
  const q = useStudioPeerSets()
  const setRun = useStudio((x) => x.setRun)
  const setTab = useStudio((x) => x.setTab)
  const del = useMutation({
    mutationFn: (id: string) => studioApi.deletePeerSet(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sk.peerSets() })
      toast({ tone: 'info', title: 'Peer set deleted' })
    },
    onError: (e) => toast({ tone: 'error', title: 'Delete failed', message: errMsg(e) }),
  })
  return (
    <Panel title="Peer sets" subtitle="Saved from clustering runs · selectable in Company 360 → Peers" flush>
      {q.isPending ? (
        <SkeletonRows rows={3} className="p-3" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.length === 0 ? (
        <EmptyState title="No peer sets" message="Run a security-level clustering (k-means, GMM, HDBSCAN…) and use “Save as peer set”." />
      ) : (
        <ul className="divide-y divide-border">
          {q.data.map((p) => (
            <li key={p.peer_set_id} className="flex items-center gap-2 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-fg">{p.name}</div>
                <div className="num text-2xs text-muted">
                  {p.method} · {p.n_members} companies · {p.n_clusters} groups · {fmtDateTime(p.created_at)}
                </div>
              </div>
              <Button
                size="xs"
                variant="ghost"
                title="Open the source run"
                onClick={() => {
                  setRun(p.source_run_id)
                  setTab('builder')
                }}
              >
                run
              </Button>
              <Button size="icon-sm" variant="ghost" aria-label={`Delete peer set ${p.name}`} disabled={del.isPending} onClick={() => del.mutate(p.peer_set_id)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function CompareView({ ids, onClose }: { ids: string[]; onClose: () => void }) {
  // v1.50: benchmark for the out-of-fold DM / Clark-West tests (null = the server default, the first run)
  const [benchmark, setBenchmark] = useState<string | null>(null)
  const q = useStudioCompare(ids, benchmark)
  const names = (q.data?.runs ?? []).reduce<Record<string, string>>((m, r) => ({ ...m, [r.runId]: r.name ?? r.method }), {})
  return (
    <Panel
      title="Compare runs"
      subtitle="Metrics aligned by name; coefficients by term (estimate, p-value); out-of-fold tests vs the benchmark"
      actions={
        <>
          <Select
            label="Benchmark run"
            className="w-56"
            value={benchmark ?? q.data?.oos?.benchmarkRunId ?? ids[0]}
            onChange={(v) => setBenchmark(v)}
            options={ids.map((id) => ({ value: id, label: `Benchmark: ${names[id] ?? id}` }))}
          />
          <Button size="icon-sm" variant="ghost" aria-label="Close comparison" onClick={onClose}>
            <X />
          </Button>
        </>
      }
    >
      <QueryView query={q}>
        {(c) => (
          <div className={cn('space-y-4', q.isFetching && 'opacity-70')}>
            <CompareTables c={c} />
            {c.oos ? (
              <div className="border-t border-border pt-3">
                <OosCompare oos={c.oos} runs={c.runIds.map((id) => c.runs.find((r) => r.runId === id))} />
              </div>
            ) : null}
          </div>
        )}
      </QueryView>
    </Panel>
  )
}

function CompareTables({ c }: { c: StudioCompareView }) {
  const runs = c.runIds.map((id) => c.runs.find((r) => r.runId === id))
  const head = (
    <tr className="border-b border-border">
      <th className="sticky left-0 bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide text-muted uppercase">Metric</th>
      {runs.map((r, i) => (
        <th key={c.runIds[i]} className="bg-surface-2 px-2 py-1 text-right text-xs font-semibold whitespace-nowrap text-fg">
          <div className="max-w-44 truncate">{r?.name ?? c.runIds[i]}</div>
          <div className="num text-2xs font-normal text-muted">
            {r?.method ?? ''} · {c.runIds[i]}
          </div>
        </th>
      ))}
    </tr>
  )
  const lowerBetter = /aic|bic|rmse|mae|mape|brier|loss|deviance|inertia|davies/i
  // counts and settings, not quality measures
  const neutral = /^(n_obs|k|n_params|df_resid|n_clusters|n_components)$/i
  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label="Metric comparison">
          <thead>{head}</thead>
          <tbody>
            {c.metrics.map((m) => {
              const vals = c.runIds.map((id) => m.values[id] ?? null)
              const nums = vals.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
              const best = nums.length > 1 && !neutral.test(m.metric) ? (lowerBetter.test(m.metric) ? Math.min(...nums) : Math.max(...nums)) : null
              return (
                <tr key={m.metric} className="border-b border-border/60">
                  <td className="sticky left-0 bg-surface px-2 py-1 text-muted">{labelOf(m.metric)}</td>
                  {vals.map((v, i) => (
                    <td key={c.runIds[i]} className={cn('num px-2 py-1 text-right', v !== null && v === best ? 'font-semibold text-up' : 'text-fg')}>
                      {fmtStat(v)}
                      {v !== null && v === best ? <span className="sr-only"> (best)</span> : null}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="mt-1 text-2xs text-muted">Green = best across the selected runs (lower is better for information criteria and errors). Only meaningful when runs share the same target and sample.</p>
      </div>
      {c.coefficients.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" aria-label="Coefficient comparison">
            <thead>
              <tr className="border-b border-border">
                <th className="sticky left-0 bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide text-muted uppercase">Term</th>
                {c.runIds.map((id, i) => (
                  <th key={id} className="bg-surface-2 px-2 py-1 text-right text-xs font-semibold whitespace-nowrap text-fg">
                    {runs[i]?.name ?? id}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {c.coefficients.map((row) => (
                <tr key={row.term} className="border-b border-border/60">
                  <td className="sticky left-0 bg-surface px-2 py-1 font-medium text-fg">{row.term}</td>
                  {c.runIds.map((id) => {
                    const v = row.values[id]
                    const sig = typeof v?.pValue === 'number' && v.pValue < 0.05
                    return (
                      <td key={id} className="num px-2 py-1 text-right whitespace-nowrap">
                        {v ? (
                          <>
                            <span className={sig ? 'font-semibold text-fg' : 'text-fg'}>{fmtStat(v.estimate)}</span>
                            <span className="ml-1 text-2xs text-muted">p {fmtP(v.pValue)}</span>
                          </>
                        ) : (
                          <span className="text-faint">{DASH}</span>
                        )}
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export function StudiesTab({ onRun }: { onRun: (spec: CStudySpec, studyId: string | null) => void }) {
  const studies = useStudioStudies()
  const runs = useStudioRuns()
  const compare = useStudio((s) => s.compare)
  const toggleCompare = useStudio((s) => s.toggleCompare)
  const clearCompare = useStudio((s) => s.clearCompare)
  const setRun = useStudio((s) => s.setRun)
  const setTab = useStudio((s) => s.setTab)
  const [comparing, setComparing] = useState<string[] | null>(null)
  const [filter, setFilter] = useState('')

  const cols = useMemo<ColumnDef<StudioRunStatusView>[]>(
    () => [
      {
        id: 'sel',
        header: '',
        size: 32,
        enableSorting: false,
        cell: (c) => (
          <span onClick={(e) => e.stopPropagation()} className="inline-flex">
            <Checkbox label={`Select ${c.row.original.runId} for comparison`} checked={compare.includes(c.row.original.runId)} onCheckedChange={() => toggleCompare(c.row.original.runId)} disabled={c.row.original.status !== 'completed'} />
          </span>
        ),
      },
      { id: 'name', header: 'Run', size: 200, accessorFn: (r) => r.name ?? r.runId, cell: (c) => <span className="truncate font-medium text-fg">{c.getValue<string>()}</span> },
      { id: 'method', header: 'Method', size: 100, accessorFn: (r) => r.method, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
      { id: 'status', header: 'Status', size: 112, accessorFn: (r) => r.status, cell: (c) => <RunStateBadge status={c.row.original.status} /> },
      { id: 'created', header: 'Created', size: 104, accessorFn: (r) => r.createdAt, cell: (c) => <span className="num text-muted">{fmtWhen(c.getValue<string>())}</span> },
      { id: 'dur', header: 'Time', size: 66, accessorFn: (r) => r.durationS ?? -1, cell: (c) => <span className="num">{fmtSeconds(c.row.original.durationS)}</span>, meta: { align: 'right' } },
      { id: 'n', header: 'n', size: 60, accessorFn: (r) => nObs(r) ?? -1, cell: (c) => <span className="num text-muted">{fmtStat(nObs(c.row.original))}</span>, meta: { align: 'right' } },
      { id: 'metric', header: 'Headline', size: 150, accessorFn: (r) => headline(r), cell: (c) => <span className="num text-muted">{c.getValue<string>()}</span> },
    ],
    [compare, toggleCompare],
  )
  const runRows = (runs.data ?? []).filter((r) => !filter.trim() || `${r.name ?? ''} ${r.method} ${r.runId}`.toLowerCase().includes(filter.trim().toLowerCase()))

  return (
    <div className="space-y-3">
      <div className="grid items-start gap-3 xl:grid-cols-[minmax(320px,420px)_minmax(0,1fr)]">
        <Panel title="Saved studies" subtitle="Open, run, rename, duplicate or delete" flush>
          {studies.isPending ? (
            <SkeletonRows rows={6} className="p-3" />
          ) : studies.isError ? (
            <ErrorState error={studies.error} onRetry={() => void studies.refetch()} />
          ) : studies.data.length === 0 ? (
            <EmptyState title="No saved studies" message="Use “Save study” in the builder to keep a study here." />
          ) : (
            <ul className="max-h-[560px] divide-y divide-border overflow-y-auto">
              {studies.data.map((s) => (
                <StudyRow key={s.studyId} s={s} onRun={onRun} />
              ))}
            </ul>
          )}
        </Panel>
        <Panel
          title="Run history"
          subtitle="Click a run to open its results · tick completed runs to compare"
          flush
          actions={
            <>
              <Input placeholder="Filter runs" aria-label="Filter runs" className="w-36" value={filter} onChange={(e) => setFilter(e.target.value)} />
              {compare.length > 0 && (
                <Button size="sm" variant="ghost" onClick={clearCompare}>
                  Clear ({compare.length})
                </Button>
              )}
              <Button size="sm" variant="primary" disabled={compare.length < 2} onClick={() => setComparing([...compare])}>
                <GitCompareArrows /> Compare {compare.length >= 2 ? compare.length : ''}
              </Button>
            </>
          }
        >
          {runs.isPending ? (
            <SkeletonRows rows={8} className="p-3" />
          ) : runs.isError ? (
            <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
          ) : (
            <DataTable
              data={runRows}
              columns={cols}
              ariaLabel="Run history"
              height={Math.min(520, 34 + runRows.length * 28)}
              rowHeight={28}
              getRowId={(r) => r.runId}
              onRowClick={(r) => {
                setRun(r.runId)
                setTab('builder')
              }}
              rowClassName={(r) => (compare.includes(r.runId) ? 'bg-accent-soft/40' : undefined)}
              emptyMessage="No runs yet."
            />
          )}
        </Panel>
      </div>
      {comparing && comparing.length >= 2 && <CompareView ids={comparing} onClose={() => setComparing(null)} />}
      <PeerSetsPanel />
      {runs.data && runs.data.length > 0 && <p className="text-2xs text-muted">{runs.data.length} most recent runs. <Badge tone="neutral">{runs.data.filter((r) => r.status === 'completed').length} completed</Badge></p>}
    </div>
  )
}
