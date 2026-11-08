import { Activity, ExternalLink, Loader2, Save, Trash2, X } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { modelHeadline } from '@/api/studio/adaptersS3'
import { useDeleteModel, useMonitorModel, useStudioModel, useUpdateModel } from '@/api/studio/hooks'
import type { CModelUpdate, StudioModelMonitorView, StudioModelView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Input, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { DASH, fmtDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtStat, fmtWhen, labelOf } from '../fmt'
import { useStudio } from '../store'
import { MonitorView } from './MonitorView'
import { MONITOR_OPTIONS, STAGE_OPTIONS, STAGE_TONE, numOrNull } from './options'

const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs font-semibold tracking-wide text-muted uppercase">{label}</dt>
      <dd className="num truncate text-xs text-fg">{children}</dd>
    </div>
  )
}

/** Stage / monitoring / thresholds / owner / description editor (PATCH). */
function ModelSettings({ m }: { m: StudioModelView }) {
  const update = useUpdateModel()
  const [owner, setOwner] = useState(m.owner)
  const [description, setDescription] = useState(m.description ?? '')
  const [psi, setPsi] = useState(String(m.psiThreshold))
  const [floor, setFloor] = useState(m.metricFloor === null ? '' : String(m.metricFloor))
  const patch = (p: CModelUpdate, msg: string) =>
    update.mutate(
      { modelId: m.modelId, patch: p },
      { onSuccess: () => toast({ tone: 'success', title: msg, message: `${m.name} v${m.version}` }), onError: (e) => toast({ tone: 'error', title: 'Update failed', message: errMsg(e) }) },
    )
  const psiV = numOrNull(psi)
  const dirty = owner !== m.owner || description !== (m.description ?? '') || psiV !== m.psiThreshold || numOrNull(floor) !== m.metricFloor
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2">
        <div className="text-xs text-fg">
          Stage
          <Select label="Stage" className="mt-0.5 w-full" value={m.stage} disabled={update.isPending} onChange={(v) => patch({ stage: v }, `Stage set to ${v}`)} options={STAGE_OPTIONS} />
        </div>
        <div className="text-xs text-fg">
          Monitoring
          <Select
            label="Monitoring schedule"
            className="mt-0.5 w-full"
            value={m.monitor}
            disabled={update.isPending}
            onChange={(v) => patch({ monitor: v }, `Monitoring ${v === 'none' ? 'off' : v}`)}
            options={MONITOR_OPTIONS}
          />
        </div>
        <label className="text-xs text-fg">
          Owner
          <Input className="mt-0.5 w-full" value={owner} onChange={(e) => setOwner(e.target.value)} />
        </label>
        <div className="grid grid-cols-2 gap-1">
          <label className="text-xs text-fg" title="PSI above which a feature / the predictions count as drifted">
            PSI threshold
            <Input type="number" step="0.01" min="0" className="num mt-0.5 w-full" value={psi} aria-invalid={psiV === null || psiV <= 0} onChange={(e) => setPsi(e.target.value)} />
          </label>
          <label className="text-xs text-fg" title="Alert when the expanding OOS rank IC / R² OOS falls below">
            Metric floor
            <Input type="number" step="0.01" className="num mt-0.5 w-full" placeholder="none" value={floor} onChange={(e) => setFloor(e.target.value)} />
          </label>
        </div>
      </div>
      <label className="block text-xs text-fg">
        Description
        <Input className="mt-0.5 w-full" value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <Button
        size="sm"
        variant="secondary"
        disabled={!dirty || update.isPending || psiV === null || psiV <= 0}
        onClick={() => patch({ owner: owner.trim() || null, description: description.trim() || null, psi_threshold: psiV, metric_floor: numOrNull(floor) }, 'Model updated')}
      >
        {update.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save changes
      </Button>
    </div>
  )
}

function Detail({ m }: { m: StudioModelView }) {
  const setModelId = useStudio((s) => s.setModelId)
  const setRun = useStudio((s) => s.setRun)
  const setTab = useStudio((s) => s.setTab)
  const monitor = useMonitorModel()
  const del = useDeleteModel()
  const [confirm, setConfirm] = useState(false)
  const [mon, setMon] = useState<StudioModelMonitorView | null>(null)
  const shown = mon ?? m.lastMonitorDetail
  const metrics = Object.entries(m.metrics)
    .filter(([, v]) => v !== null)
    .slice(0, 12)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={STAGE_TONE[m.stage]}>{m.stage}</Badge>
        <span className="text-xs text-muted">{m.method}</span>
        <div className="ml-auto flex flex-wrap gap-1.5">
          <Button
            size="sm"
            variant="ghost"
            title="Open the promoted run in the builder's results"
            onClick={() => {
              setRun(m.runId)
              setTab('builder')
            }}
          >
            <ExternalLink /> Open run
          </Button>
          <Button
            size="sm"
            variant="primary"
            disabled={monitor.isPending}
            onClick={() =>
              monitor.mutate(
                { modelId: m.modelId },
                {
                  onSuccess: (r) => {
                    setMon(r)
                    toast({ tone: r.drift ? 'error' : 'success', title: r.drift ? 'Drift detected' : 'No drift', message: r.breaches.slice(0, 2).join('; ') || `${m.name} v${m.version}` })
                  },
                  onError: (e) => toast({ tone: 'error', title: 'Monitor failed', message: errMsg(e) }),
                },
              )
            }
          >
            {monitor.isPending ? <Loader2 className="animate-spin" /> : <Activity />} Run monitor now
          </Button>
          {confirm ? (
            <Button
              size="sm"
              variant="danger"
              disabled={del.isPending}
              onBlur={() => setConfirm(false)}
              onClick={() =>
                del.mutate(m.modelId, {
                  onSuccess: () => {
                    setModelId(null)
                    toast({ tone: 'success', title: 'Model deleted', message: `${m.name} v${m.version}` })
                  },
                  onError: (e) => toast({ tone: 'error', title: 'Delete failed', message: errMsg(e) }),
                })
              }
            >
              Delete v{m.version}?
            </Button>
          ) : (
            <Button size="icon-sm" variant="ghost" aria-label={`Delete ${m.name} v${m.version}`} title="Delete this version" onClick={() => setConfirm(true)}>
              <Trash2 />
            </Button>
          )}
        </div>
      </div>

      {m.description && <p className="text-xs text-fg/80">{m.description}</p>}

      <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5 @lg:grid-cols-3">
        <Meta label="Run">{m.runId}</Meta>
        <Meta label="Owner">{m.owner || DASH}</Meta>
        <Meta label="Spec hash">
          <span title={m.specHash}>{m.specHash.slice(0, 12) || DASH}</span>
        </Meta>
        <Meta label="Snapshot">{m.snapshotId ?? DASH}</Meta>
        <Meta label="Dataset key">
          <span title={m.datasetKey ?? undefined}>{m.datasetKey?.slice(0, 16) ?? DASH}</span>
        </Meta>
        <Meta label="Alert">{m.alertId ?? 'none'}</Meta>
        <Meta label="Created">{m.createdAt ? fmtDateTime(m.createdAt) : DASH}</Meta>
        <Meta label="Updated">{m.updatedAt ? fmtDateTime(m.updatedAt) : DASH}</Meta>
        <Meta label="Last monitored">{m.lastMonitoredAt ? fmtWhen(m.lastMonitoredAt) : 'never'}</Meta>
      </dl>

      {metrics.length > 0 && (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs" aria-label="Promotion metrics">
          {metrics.map(([k, v]) => (
            <span key={k} className="text-muted">
              {labelOf(k)} <span className="num text-fg">{fmtStat(v, 4)}</span>
            </span>
          ))}
        </div>
      )}

      <section aria-label="Settings" className="border-t border-border pt-3">
        <h3 className="mb-1.5 text-2xs font-semibold tracking-wide text-muted uppercase">Stage & monitoring</h3>
        <ModelSettings key={`${m.modelId}-${m.updatedAt ?? ''}`} m={m} />
      </section>

      {m.versions.length > 0 && (
        <section aria-label="Versions" className="border-t border-border pt-3">
          <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Versions of {m.name}</h3>
          <ul className="divide-y divide-border/60 text-xs">
            {[...m.versions]
              .sort((a, b) => b.version - a.version)
              .map((v) => {
                const h = modelHeadline(v)
                return (
                  <li key={v.modelId}>
                    <button
                      type="button"
                      aria-current={v.modelId === m.modelId}
                      onClick={() => setModelId(v.modelId)}
                      className={cn('flex w-full items-center gap-2 px-1 py-1 text-left hover:bg-surface-2', v.modelId === m.modelId && 'bg-accent-soft/40')}
                    >
                      <span className="num w-8 font-semibold text-fg">v{v.version}</span>
                      <Badge tone={STAGE_TONE[v.stage]}>{v.stage}</Badge>
                      <span className="num min-w-0 flex-1 truncate text-muted">{v.runId}</span>
                      <span className="num text-muted">{h ? `${labelOf(h.key)} ${fmtStat(h.value, 3)}` : DASH}</span>
                      <span className="num w-20 text-right text-muted">{fmtWhen(v.createdAt)}</span>
                    </button>
                  </li>
                )
              })}
          </ul>
        </section>
      )}

      <section aria-label="Monitoring" className="border-t border-border pt-3">
        <h3 className="mb-1.5 text-2xs font-semibold tracking-wide text-muted uppercase">{mon ? 'Monitor result' : 'Last monitor result'}</h3>
        {shown ? <MonitorView mon={shown} /> : <p className="text-xs text-muted">Not monitored yet — “Run monitor now” compares the latest data with the promotion baseline.</p>}
      </section>
    </div>
  )
}

/** Registry entry detail: metadata, versions, stage / monitoring controls, monitor result, delete. */
export function ModelDetail({ modelId }: { modelId: string }) {
  const q = useStudioModel(modelId)
  const setModelId = useStudio((s) => s.setModelId)
  return (
    <Panel
      title={q.data ? `${q.data.name} · v${q.data.version}` : 'Model'}
      subtitle="Versioned run: spec hash, data snapshot, owner, stage and drift monitoring"
      className="@container"
      actions={
        <Button size="icon-sm" variant="ghost" aria-label="Close model" onClick={() => setModelId(null)}>
          <X />
        </Button>
      }
    >
      <QueryView query={q} skeleton={<SkeletonRows rows={10} />}>
        {(m) => <Detail key={m.modelId} m={m} />}
      </QueryView>
    </Panel>
  )
}
