import { useMutation, useQueryClient } from '@tanstack/react-query'
import { PanelLeftClose, PanelLeftOpen } from 'lucide-react'
import { useState } from 'react'
import { isTerminal } from '@/api/studio/adapters'
import { studioApi } from '@/api/studio/client'
import { sk, useStudioCatalog, useStudioLane } from '@/api/studio/hooks'
import type { CLeaderboardRequest, CStudySpec, StudioRunView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import ModelLab from '../models/ModelLab'
import { BuilderPane } from './builder/BuilderPane'
import { fmtSeconds } from './fmt'
import { GuideTab } from './guide/GuideTab'
import { LintPanel } from './lint/LintIssues'
import { useLintGate } from './lint/useLintGate'
import { PresetsTab } from './PresetsTab'
import { RegistryTab } from './registry/RegistryTab'
import { ResultsPanel } from './results/ResultsPanel'
import { type StudioTab, useStudio } from './store'
import { StudiesTab } from './StudiesTab'

interface SubmitArgs {
  spec: CStudySpec
  mode: 'auto' | 'async'
  studyId?: string | null
}

/** Submit a study: quick runs come back finished; long ones are queued on the worker and polled. */
function useSubmit() {
  const qc = useQueryClient()
  const setRun = useStudio((s) => s.setRun)
  const watch = useStudio((s) => s.watch)
  const setTab = useStudio((s) => s.setTab)
  const [error, setError] = useState<unknown>(null)
  const m = useMutation({
    mutationFn: (a: SubmitArgs) => {
      // leaderboard mode: candidates are compared on identical CV splits via POST /studio/leaderboard
      if (a.spec.method === 'leaderboard') {
        const p = (a.spec.params ?? {}) as Record<string, unknown>
        return studioApi.leaderboard({
          name: a.spec.name ?? null,
          dataset: a.spec.dataset,
          y: a.spec.y ?? '',
          x: a.spec.x ?? [],
          candidates: (p.candidates ?? []) as CLeaderboardRequest['candidates'],
          task: (p.task as CLeaderboardRequest['task']) ?? 'auto',
          metric: (p.metric as CLeaderboardRequest['metric']) ?? null,
          cv: (p.cv as Record<string, unknown>) ?? {},
          mode: a.mode,
          study_id: a.studyId ?? null,
          seed: a.spec.seed ?? 42,
        })
      }
      return studioApi.submit(a.spec, { mode: a.mode, studyId: a.studyId ?? null })
    },
    onMutate: () => {
      setError(null)
      setTab('builder')
    },
    onSuccess: (run: StudioRunView) => {
      const s = run.status
      qc.setQueryData(sk.run(s.runId), run)
      setRun(s.runId)
      void qc.invalidateQueries({ queryKey: sk.runs() })
      void qc.invalidateQueries({ queryKey: sk.studies() })
      if (!isTerminal(s.status)) {
        watch(s.runId)
        toast({ tone: 'info', title: s.lane === 'lane' ? 'Run started on the studio lane' : 'Run queued on the worker', message: `${s.name ?? s.method}${s.estimateS ? ` · estimate ≈ ${fmtSeconds(s.estimateS)}` : ''}` })
      } else if (s.status === 'completed') {
        toast({ tone: 'success', title: 'Run complete', message: `${run.result?.title ?? s.method} · ${fmtSeconds(s.durationS)}` })
      } else if (s.status === 'failed') {
        toast({ tone: 'error', title: 'Run failed', message: s.error ?? undefined })
      }
    },
    onError: (e) => {
      setError(e)
      toast({ tone: 'error', title: 'Run rejected', message: e instanceof Error ? e.message : String(e) })
    },
  })
  return { submit: m.mutate, pending: m.isPending, error, clearError: () => setError(null) }
}

export default function Studio() {
  const tab = useStudio((s) => s.tab)
  const setTab = useStudio((s) => s.setTab)
  const studyId = useStudio((s) => s.studyId)
  const cat = useStudioCatalog()
  const { submit, pending, error, clearError } = useSubmit()
  const [builderOpen, setBuilderOpen] = useState(true)
  // v1.50: pre-run lint in front of the builder's Run (errors wait for "Run anyway"; lint failures never block)
  const gate = useLintGate((spec, mode) => submit({ spec, mode, studyId }))
  return (
    <>
      <PageHeader
        title="Model Studio"
        subtitle="Econometrics & ML workbench — build a dataset from any table, pick a method, inspect diagnostics, save and compare studies"
        actions={
          <div className="flex items-center gap-1.5">
            <LaneBadge />
            {cat.data ? <Badge tone="neutral" title="Data snapshot every run is pinned to">snapshot {cat.data.snapshotId}</Badge> : null}
          </div>
        }
      />
      <Tabs value={tab} onValueChange={(v) => setTab(v as StudioTab)}>
        <TabsList>
          <TabsTrigger value="builder">Builder</TabsTrigger>
          <TabsTrigger value="guide">Guide</TabsTrigger>
          <TabsTrigger value="studies">Studies & runs</TabsTrigger>
          <TabsTrigger value="presets">Presets</TabsTrigger>
          <TabsTrigger value="registry">Registry</TabsTrigger>
          <TabsTrigger value="monitor">ML monitor</TabsTrigger>
        </TabsList>
        <TabsContent value="builder">
          <div className={cn('grid items-start gap-3', builderOpen ? 'grid-cols-[340px_minmax(0,1fr)] xl:grid-cols-[380px_minmax(0,1fr)] 2xl:grid-cols-[420px_minmax(0,1fr)]' : 'grid-cols-1')}>
            {builderOpen && (
              <Panel
                flush
                title="Study builder"
                className="sticky top-0 max-h-[calc(100vh-7.5rem)]"
                bodyClassName="overflow-y-auto"
                actions={
                  <Button size="icon-sm" variant="ghost" aria-label="Hide builder" title="Hide builder (full-width results)" onClick={() => setBuilderOpen(false)}>
                    <PanelLeftClose />
                  </Button>
                }
              >
                <BuilderPane running={pending || gate.checking} onRun={gate.run} />
              </Panel>
            )}
            <div className="min-w-0">
              {!builderOpen && (
                <Button size="sm" variant="outline" className="mb-2" onClick={() => setBuilderOpen(true)}>
                  <PanelLeftOpen /> Show builder
                </Button>
              )}
              {gate.state && (
                <div className="mb-3">
                  <LintPanel lint={gate.state.lint} blocked={gate.state.blocked} running={pending} onRunAnyway={gate.runAnyway} onDismiss={gate.dismiss} />
                </div>
              )}
              <ResultsPanel submitError={error} onDismissError={clearError} submitting={pending} />
            </div>
          </div>
        </TabsContent>
        <TabsContent value="guide">
          <GuideTab onRun={(spec) => submit({ spec, mode: 'auto' })} />
        </TabsContent>
        <TabsContent value="studies">
          <StudiesTab onRun={(spec, sid) => submit({ spec, mode: 'auto', studyId: sid })} />
        </TabsContent>
        <TabsContent value="presets">
          <PresetsTab onRun={(spec) => submit({ spec, mode: 'auto' })} />
        </TabsContent>
        <TabsContent value="registry">
          <RegistryTab />
        </TabsContent>
        <TabsContent value="monitor">
          <ModelLab embedded />
        </TabsContent>
      </Tabs>
    </>
  )
}

/** S1: warm studio lane state + estimate accuracy (observed / estimated end-to-end ratio of recent runs). */
function LaneBadge() {
  const lane = useStudioLane()
  const l = lane.data
  if (!l) return null
  const a = l.accuracy.studio
  const acc = a.n && a.medianRatio !== null ? ` · estimates: observed/estimated ${a.medianRatio.toFixed(2)} (median of ${a.n} runs${a.within30 !== null ? `, ${Math.round(a.within30 * 100)} % within ±30 %` : ''})` : ''
  const title = l.alive
    ? `Studio lane: warm, ${l.pool ?? '?'} worker(s), ${l.running} running, ${l.queued} queued, priority ${l.priority ?? 'normal'}${l.heavyJobs.length ? ` (lowered while ${l.heavyJobs.join(', ')} runs)` : ''}${l.blockedBy ? ` · ${l.blockedBy}` : ''}${acc}`
    : `Studio lane ${l.enabled ? 'not running' : 'disabled (BIG360_STUDIO_LANE=0)'}: long runs use the background worker${acc}`
  return (
    <Badge tone={l.alive ? 'up' : 'neutral'} title={title}>
      {l.alive ? 'lane warm' : 'lane off'}
      {a.n && a.medianRatio !== null ? ` · est ×${a.medianRatio.toFixed(2)}` : ''}
    </Badge>
  )
}
