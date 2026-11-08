import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Ban, Download, FlaskConical, History, Info, Loader2, OctagonAlert, Square, Upload } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { studioRunSpec } from '@/api/backtest/adapters'
import { ApiError } from '@/api/errors'
import { useSectorTree } from '@/api/hooks'
import { isTerminal } from '@/api/studio/adapters'
import { studioApi } from '@/api/studio/client'
import { sk, useStudioMethods, useStudioRun } from '@/api/studio/hooks'
import type { StudioResultView, StudioRunStatusView, StudioRunView, StudioWarning } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import { fmtDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtSeconds, fmtStat } from '../fmt'
import { estimateBreakdown, pendingHeadline, previewLabel, progressPct, remainingS } from '../runLive'
import { useStudio } from '../store'
import { useBacktest } from '../../backtest/store'
import { ClustersTab, CoefficientsTab, DataTab, DiagnosticsTab, ForecastTab, OtherTab, PcaTab, SpecialTab, SummaryTab } from './tabs'
import { assignOutputs } from './assign'
import { CreateThesisLink } from '@/pages/theses/shared'
import { PromoteButton } from '../registry/PromoteDialog'

/* ───────────── run state chrome ───────────── */

export function RunStateBadge({ status }: { status: StudioRunStatusView['status'] }) {
  const m = {
    queued: { tone: 'neutral' as const, icon: '…' },
    running: { tone: 'info' as const, icon: '◔' },
    completed: { tone: 'up' as const, icon: '✓' },
    failed: { tone: 'down' as const, icon: '✕' },
    cancelled: { tone: 'warn' as const, icon: '■' },
  }[status]
  return (
    <Badge tone={m.tone}>
      <span aria-hidden>{m.icon}</span> {status}
    </Badge>
  )
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [active])
  return now
}

function PendingRun({ run }: { run: StudioRunStatusView }) {
  const qc = useQueryClient()
  const now = useNow(true)
  const since = Date.parse(run.startedAt ?? run.createdAt)
  const elapsed = Number.isFinite(since) ? Math.max(0, (now - since) / 1000) : null
  const cancel = useMutation({
    mutationFn: () => studioApi.cancel(run.runId),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: sk.run(run.runId) })
      void qc.invalidateQueries({ queryKey: sk.runs() })
      toast({ tone: 'info', title: 'Cancellation requested', message: run.name ?? run.runId })
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not cancel', message: e instanceof Error ? e.message : String(e) }),
  })
  const pct = progressPct(run, elapsed)
  const left = remainingS(run, elapsed) // the server's ETA, refreshed by every poll (1 s)
  const breakdown = estimateBreakdown(run.estimate)
  const compact = !!run.preview
  return (
    <div role="status" aria-live="polite" className={cn('flex flex-col items-center gap-2 px-4 text-center', compact ? 'py-3' : 'py-12 gap-3')}>
      <Loader2 className="size-6 animate-spin text-accent" aria-hidden />
      <div className="text-sm font-medium text-fg">
        {pendingHeadline(run)} — {run.name ?? run.method}
      </div>
      {run.status === 'queued' && run.queue?.blockedBy && <div className="max-w-md text-xs text-warn">Waiting for: {run.queue.blockedBy}</div>}
      <div className="num text-xs text-muted">
        {run.runId} · elapsed {fmtSeconds(elapsed)}
        {left !== null ? ` · ≈ ${fmtSeconds(left)} left` : ''}
      </div>
      <div className="h-1 w-64 overflow-hidden rounded-full bg-surface-3" aria-hidden>
        <div className={cn('h-full rounded-full bg-accent', pct === null && 'w-1/3 animate-pulse')} style={pct !== null ? { width: `${pct}%` } : undefined} />
      </div>
      {breakdown && <div className="num max-w-md text-2xs text-muted">estimate {breakdown}</div>}
      {!compact && (
        <p className="max-w-sm text-xs text-muted">
          {run.lane === 'lane' ? 'Runs on the warm studio lane: a quick preview on a sample comes first, then the full result.' : 'Runs on the background worker.'} You can keep editing, switch tabs or leave the page — you will get a notification when it finishes.
        </p>
      )}
      <Button variant="danger" size="sm" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
        <Square /> Cancel run
      </Button>
    </div>
  )
}

/* ───────────── warnings / guardrails ───────────── */

export function WarningsBlock({ warnings, datasetWarnings = [] }: { warnings: StudioWarning[]; datasetWarnings?: string[] }) {
  const [open, setOpen] = useState(false)
  const serious = warnings.filter((w) => w.severity !== 'info')
  const info = [...warnings.filter((w) => w.severity === 'info' && w.code !== 'dataset'), ...warnings.filter((w) => w.code === 'dataset')]
  const extraDs = datasetWarnings.filter((d) => !warnings.some((w) => w.message === d))
  if (!serious.length && !info.length && !extraDs.length) return null
  return (
    <div className="space-y-1.5">
      {serious.map((w, i) => (
        <div
          key={`${w.code}-${i}`}
          role="alert"
          className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-sm', w.severity === 'error' ? 'border-down/50 bg-down-soft text-down' : 'border-warn/50 bg-warn-soft text-warn')}
        >
          <OctagonAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="min-w-0">
            <span className="mr-1.5 font-semibold tracking-wide uppercase">{w.code.replace(/_/g, ' ')}</span>
            <span className="text-fg">{w.message}</span>
          </div>
        </div>
      ))}
      {(info.length > 0 || extraDs.length > 0) && (
        <div className="rounded-md border border-border bg-surface-2 px-3 py-1.5 text-xs text-muted">
          <button type="button" className="flex w-full items-center gap-1.5 text-left hover:text-fg" aria-expanded={open} onClick={() => setOpen(!open)}>
            <Info className="size-3.5 shrink-0 text-info" aria-hidden />
            <span className="shrink-0 font-medium whitespace-nowrap text-fg">{info.length + extraDs.length} note{info.length + extraDs.length === 1 ? '' : 's'}</span>
            <span className="min-w-0 truncate">{!open && (info[0]?.message ?? extraDs[0])}</span>
            <span className="ml-auto shrink-0">{open ? 'hide' : 'show'}</span>
          </button>
          {open && (
            <ul className="mt-1.5 space-y-1">
              {info.map((w, i) => (
                <li key={`${w.code}-${i}`}>
                  <span className="mr-1 font-semibold text-fg">{w.code.replace(/_/g, ' ')}:</span>
                  {w.message}
                </li>
              ))}
              {extraDs.map((d, i) => (
                <li key={`ds-${i}`}>
                  <span className="mr-1 font-semibold text-fg">dataset:</span>
                  {d}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

/** Guardrail / validation rejection from POST /studio/runs (422 urn:studio:<code>). */
export function SubmitError({ error, onDismiss }: { error: unknown; onDismiss: () => void }) {
  const code = error instanceof ApiError && error.type?.startsWith('urn:studio:') ? error.type.slice('urn:studio:'.length) : null
  const status = error instanceof ApiError ? error.status : null
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md border border-down/50 bg-down-soft px-3 py-2 text-sm">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-down" aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="font-semibold text-down">
          {code ? `Guardrail: ${code.replace(/_/g, ' ')}` : status === 422 ? 'Study rejected' : 'Run failed to start'}
          {status ? <span className="num ml-1.5 font-normal text-muted">HTTP {status}</span> : null}
        </div>
        <div className="mt-0.5 break-words text-fg">{msg}</div>
      </div>
      <Button size="xs" variant="ghost" onClick={onDismiss}>
        Dismiss
      </Button>
    </div>
  )
}

async function downloadPython(runId: string, name: string | null): Promise<void> {
  try {
    const code = await studioApi.exportPython(runId)
    const blob = new Blob([code], { type: 'text/x-python' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${(name ?? 'study').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || 'study'}_${runId}.py`
    document.body.appendChild(a)
    a.click()
    a.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    toast({ tone: 'success', title: 'Python script downloaded', message: a.download })
  } catch (e) {
    toast({ tone: 'error', title: 'Export failed', message: e instanceof Error ? e.message : String(e) })
  }
}

/* ───────────── results pane ───────────── */

export function ResultsPanel({ submitError, onDismissError, submitting }: { submitError: unknown; onDismissError: () => void; submitting: boolean }) {
  const runId = useStudio((s) => s.runId)
  const q = useStudioRun(runId)
  return (
    <div className="@container flex min-w-0 flex-col gap-3">
      {submitError ? <SubmitError error={submitError} onDismiss={onDismissError} /> : null}
      {!runId ? (
        <Panel>
          {submitting ? (
            <div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-muted">
              <Loader2 className="size-4 animate-spin" aria-hidden /> Submitting…
            </div>
          ) : (
            <EmptyState
              icon={<FlaskConical />}
              title="No results yet"
              message="Pick variables and a method on the left, then Run. Or start from a preset — one click loads a full study into the builder."
            />
          )}
        </Panel>
      ) : q.isPending ? (
        <Panel>
          <SkeletonRows rows={10} />
        </Panel>
      ) : q.isError ? (
        <Panel>
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        </Panel>
      ) : (
        <RunView run={q.data} />
      )}
    </div>
  )
}

function RunHeader({ run }: { run: StudioRunView }) {
  const s = run.status
  const loadSpec = useStudio((x) => x.loadSpec)
  const methods = useStudioMethods()
  const [busy, setBusy] = useState(false)
  const navigate = useNavigate()
  const openBacktest = useBacktest((x) => x.open)
  // panel ML runs write out-of-fold predictions (oos_predictions.parquet): backtest them as a signal
  const canBacktest = s.status === 'completed' && run.study?.dataset.kind === 'panel' && ['tree_ensemble', 'regularized', 'lightgbm', 'stacking', 'ltr', 'mlp', 'rnn'].includes(run.study?.method ?? '')
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
      <div className="min-w-0 flex-1 basis-72">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <h2 className="max-w-full min-w-0 truncate text-md font-semibold text-fg" title={s.name ?? undefined}>
            {s.name ?? run.result?.title ?? s.method}
          </h2>
          <RunStateBadge status={s.status} />
          <Badge tone="neutral">{s.mode}</Badge>
        </div>
        <div className="mt-0.5 truncate text-xs text-fg/80">{run.result?.title ?? s.title ?? s.method}</div>
        <div className="num truncate text-xs text-muted">
          {s.runId} · {fmtDateTime(s.finishedAt ?? s.createdAt)} · {fmtSeconds(s.durationS)}
          {s.nObs !== null ? ` · n = ${fmtStat(s.nObs)}` : ''}
          {s.specHash ? ` · spec ${s.specHash.slice(0, 10)}` : ''}
        </div>
      </div>
      {run.study && (
        <Button
          size="sm"
          variant="ghost"
          title="Replace the builder contents with this run's study"
          onClick={() => {
            const m = methods.data?.find((x) => x.id === run.study?.method)
            if (run.study) loadSpec(run.study, { needsY: m?.needsY, runId: s.runId, studyId: s.studyId })
            toast({ tone: 'info', title: 'Study loaded into the builder' })
          }}
        >
          <Upload /> Load into builder
        </Button>
      )}
      {canBacktest && (
        <Button
          size="sm"
          variant="outline"
          title="Backtest this signal: the run's out-of-fold predictions as a point-in-time signal (Backtest Lab). Use walk-forward CV (scheme expanding) - other schemes are flagged look-ahead."
          onClick={() => {
            const scheme = String(((run.study?.params ?? {}) as { cv?: { scheme?: string } }).cv?.scheme ?? 'auto')
            const uni = (run.study?.dataset.entities as { universe?: string | null } | undefined)?.universe ?? 'mvp_us'
            openBacktest(studioRunSpec(s.runId, s.name ?? run.result?.title ?? s.method, uni), {
              origin: { kind: 'studio', ref: s.runId, label: `from Model Studio · ${s.name ?? s.method}`, notes: [`signal = out-of-fold predictions of run ${s.runId} (CV scheme ${scheme})`, ...(scheme !== 'expanding' ? ['non walk-forward CV trains on future data: the backtest will flag LOOK-AHEAD IN FITTING'] : [])] },
            })
            navigate('/backtest')
          }}
        >
          <History /> Backtest this signal
        </Button>
      )}
      {s.status === 'completed' && <PromoteButton run={run} />}
      {s.status === 'completed' && (
        <CreateThesisLink
          link={{ subjectType: 'macro_theme', origin: 'studio', ref: s.runId, studioRunId: s.runId, studyId: s.studyId ?? undefined, title: `Thesis from ${s.name ?? s.method}` }}
          title="Create a thesis linked to this Studio run (pick the subject in the editor)"
        />
      )}
      {s.status === 'completed' && (
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => {
            setBusy(true)
            void downloadPython(s.runId, s.name).finally(() => setBusy(false))
          }}
        >
          <Download /> Export Python
        </Button>
      )}
    </div>
  )
}

function RunView({ run }: { run: StudioRunView }) {
  const s = run.status
  return (
    <Panel bodyClassName="space-y-3">
      <RunHeader run={run} />
      {!isTerminal(s.status) ? (
        <>
          <PendingRun run={s} />
          {run.preview && (
            <div className={cn('flex items-start gap-2 rounded-md border px-3 py-2 text-xs', run.preview.info.status === 'failed' ? 'border-warn/50 bg-warn-soft text-warn' : 'border-accent/40 bg-accent/5 text-fg')}>
              <Info className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
              <span>{previewLabel(run.preview.info)}</span>
            </div>
          )}
          {run.preview?.result && <CompletedRun run={run} result={run.preview.result} />}
        </>
      ) : s.status === 'failed' ? (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-down/50 bg-down-soft px-3 py-2 text-sm">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-down" aria-hidden />
          <div className="min-w-0">
            <div className="font-semibold text-down">Run failed{s.errorCode ? ` — ${s.errorCode.replace(/_/g, ' ')}` : ''}</div>
            <div className="mt-0.5 break-words whitespace-pre-wrap text-fg">{s.error ?? 'No error message was recorded.'}</div>
          </div>
        </div>
      ) : s.status === 'cancelled' ? (
        <EmptyState icon={<Ban />} title="Run cancelled" message="Re-run from the builder when ready." />
      ) : run.result ? (
        <CompletedRun run={run} result={run.result} />
      ) : (
        <EmptyState title="No result payload" message="The run completed but returned no result." />
      )}
    </Panel>
  )
}

const TAB_LABEL = { summary: 'Summary', special: 'Effects', coefficients: 'Coefficients', diagnostics: 'Diagnostics', forecast: 'Forecast', clusters: 'Clusters', pca: 'PCA', data: 'Data & reproducibility', other: 'Other output' } as const
type TabId = keyof typeof TAB_LABEL

function CompletedRun({ run, result }: { run: StudioRunView; result: StudioResultView }) {
  const a = useMemo(() => assignOutputs(result), [result])
  const sectors = useSectorTree('ALL')
  const sectorNames = useMemo(() => Object.fromEntries((sectors.data ?? []).map((x) => [x.id, x.name])), [sectors.data])
  const tabs: TabId[] = ['summary']
  if (a.special) tabs.push('special')
  if (a.coefficients) tabs.push('coefficients')
  if (a.diagnostics) tabs.push('diagnostics')
  if (a.forecast) tabs.push('forecast')
  if (a.clusters) tabs.push('clusters')
  if (a.pca) tabs.push('pca')
  if (a.otherPlots.length || a.otherTables.length) tabs.push('other')
  tabs.push('data')
  const [tab, setTab] = useState<TabId>('summary')
  const active = tabs.includes(tab) ? tab : 'summary'
  return (
    <>
      <WarningsBlock warnings={result.warnings} datasetWarnings={run.dataset?.warnings} />
      <Tabs value={active} onValueChange={(v) => setTab(v as TabId)}>
        <TabsList>
          {tabs.map((t) => (
            <TabsTrigger key={t} value={t}>
              {t === 'special' && a.special ? a.special.label : t === 'forecast' && !result.forecast ? 'Out-of-sample' : TAB_LABEL[t]}
              {t === 'diagnostics' && a.failCount > 0 && (
                <span className="num ml-1.5 rounded-sm bg-down-soft px-1 text-2xs text-down" aria-label={`${a.failCount} failed diagnostics`}>
                  {a.failCount}
                </span>
              )}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="summary">
          <SummaryTab run={run} result={result} a={a} onGoto={(t) => setTab(t as TabId)} />
        </TabsContent>
        {a.special && (
          <TabsContent value="special">
            <SpecialTab result={result} a={a} />
          </TabsContent>
        )}
        {a.coefficients && (
          <TabsContent value="coefficients">
            <CoefficientsTab result={result} a={a} />
          </TabsContent>
        )}
        {a.diagnostics && (
          <TabsContent value="diagnostics">
            <DiagnosticsTab result={result} a={a} />
          </TabsContent>
        )}
        {a.forecast && (
          <TabsContent value="forecast">
            <ForecastTab result={result} a={a} />
          </TabsContent>
        )}
        {a.clusters && (
          <TabsContent value="clusters">
            <ClustersTab run={run} result={result} a={a} sectorNames={sectorNames} />
          </TabsContent>
        )}
        {a.pca && (
          <TabsContent value="pca">
            <PcaTab result={result} a={a} />
          </TabsContent>
        )}
        {tabs.includes('other') && (
          <TabsContent value="other">
            <OtherTab result={result} a={a} />
          </TabsContent>
        )}
        <TabsContent value="data">
          <DataTab run={run} />
        </TabsContent>
      </Tabs>
    </>
  )
}
