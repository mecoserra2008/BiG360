import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, ChevronRight, FilePlus2, Loader2, Play, Plus, Save } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { changedParams, draftIssues, draftToSpec, draftVariableFrom, hasIvRoles, isSeriesSource, roleDrivenParams, rolesFor, switchMethod, validateParam } from '@/api/studio/adapters'
import { studioApi } from '@/api/studio/client'
import { sk, useStudioEstimate, useStudioMethods } from '@/api/studio/hooks'
import type { CStudySpec, StudioMethodView, VariableRole } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { estimateBreakdown, extrasLine } from '../runLive'
import { useStudio } from '../store'
import { DatasetSection } from './DatasetSection'
import { FAMILY_LABEL } from './families'
import { ParamForm } from './ParamForm'
import { PreviewSection } from './PreviewSection'
import { VariableCard } from './VariableCard'
import { VariablePicker } from './VariablePicker'

function Section({ n, title, sub, children, defaultOpen = true, right }: { n: number; title: string; sub?: ReactNode; children: ReactNode; defaultOpen?: boolean; right?: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="border-b border-border last:border-b-0">
      <div className="flex items-center gap-2 px-3 py-2">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-2 text-left" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? <ChevronDown className="size-3.5 text-muted" /> : <ChevronRight className="size-3.5 text-muted" />}
          <span className="num flex size-4 items-center justify-center rounded-full bg-surface-3 text-2xs text-muted">{n}</span>
          <span className="text-sm font-semibold text-fg">{title}</span>
          {sub && <span className="truncate text-xs text-muted">{sub}</span>}
        </button>
        {right}
      </div>
      {open && <div className="px-3 pb-3">{children}</div>}
    </section>
  )
}

function MethodPicker({ methods, value, onPick }: { methods: StudioMethodView[]; value: string; onPick: (m: StudioMethodView) => void }) {
  const order = Object.keys(FAMILY_LABEL)
  const fams = [...new Set(methods.map((m) => m.family))].sort((a, b) => (order.indexOf(a) + 1 || 99) - (order.indexOf(b) + 1 || 99))
  return (
    <div className="space-y-1.5">
      {fams.map((f) => (
        <div key={f}>
          <div className="mb-0.5 text-2xs font-semibold tracking-wide text-muted uppercase">{FAMILY_LABEL[f] ?? f}</div>
          <div className="flex flex-wrap gap-1" role="radiogroup" aria-label={FAMILY_LABEL[f] ?? f}>
            {methods
              .filter((m) => m.family === f)
              .map((m) => (
                <button
                  key={m.id}
                  type="button"
                  role="radio"
                  aria-checked={m.id === value}
                  title={m.description}
                  onClick={() => onPick(m)}
                  className={cn(
                    'h-6 rounded-sm border px-2 text-xs font-medium transition-colors',
                    m.id === value ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:border-border-strong hover:text-fg',
                  )}
                >
                  {m.title}
                </button>
              ))}
          </div>
        </div>
      ))}
    </div>
  )
}

export function BuilderPane({ onRun, running }: { onRun: (spec: CStudySpec, mode: 'auto' | 'async') => void; running: boolean }) {
  const qc = useQueryClient()
  const methods = useStudioMethods()
  const draft = useStudio((s) => s.draft)
  const studyId = useStudio((s) => s.studyId)
  const presetId = useStudio((s) => s.presetId)
  const patchDraft = useStudio((s) => s.patchDraft)
  const setDraft = useStudio((s) => s.setDraft)
  const patchVariable = useStudio((s) => s.patchVariable)
  const addVariable = useStudio((s) => s.addVariable)
  const removeVariable = useStudio((s) => s.removeVariable)
  const moveVariable = useStudio((s) => s.moveVariable)
  const setStudyId = useStudio((s) => s.setStudyId)
  const newStudy = useStudio((s) => s.newStudy)
  const [picking, setPicking] = useState(false)

  const method = methods.data?.find((m) => m.id === draft.method) ?? null
  const roles = rolesFor(method)
  const issues = useMemo(() => {
    const out = draftIssues(draft, method)
    if (method) for (const f of method.fields) if (f.key in draft.params && validateParam(f, draft.params[f.key])) out.push(`Parameter “${f.title}”: ${validateParam(f, draft.params[f.key])}`)
    return [...new Set(out)]
  }, [draft, method])

  const spec = (): CStudySpec => {
    const s = draftToSpec(draft)
    return method ? { ...s, params: changedParams(draft.params, method.fields) } : s
  }

  // S1.2: learned estimate of the draft (name excluded from the key: renaming does not re-estimate)
  const estSpec = useMemo(() => {
    if (issues.length || !method || draft.method === 'leaderboard') return null
    const s = draftToSpec(draft)
    return { ...s, name: undefined, params: changedParams(draft.params, method.fields) } as CStudySpec
  }, [draft, method, issues])
  const estimate = useStudioEstimate(estSpec)

  const save = useMutation({
    mutationFn: async (asNew: boolean) => {
      const name = draft.name.trim() || 'Untitled study'
      if (studyId && !asNew) return studioApi.updateStudy(studyId, { name, study: spec() })
      return studioApi.createStudy({ name, study: spec(), presetId })
    },
    onSuccess: (st) => {
      setStudyId(st.studyId)
      if (!draft.name.trim()) patchDraft({ name: st.name })
      void qc.invalidateQueries({ queryKey: sk.studies() })
      toast({ tone: 'success', title: 'Study saved', message: st.name })
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not save the study', message: e instanceof Error ? e.message : String(e) }),
  })

  const featureLabel = method?.family === 'multivariate' ? 'System variable' : 'Feature'
  const pickerRoles: { role: VariableRole; label: string }[] =
    method?.needsY === false
      ? [{ role: 'feature', label: featureLabel.toLowerCase() }]
      : [
          { role: 'y', label: 'target (y)' },
          { role: 'x', label: 'regressor (x)' },
          ...(method && hasIvRoles(method)
            ? [
                { role: 'endog' as const, label: 'endogenous regressor' },
                { role: 'instrument' as const, label: 'instrument' },
              ]
            : []),
        ]

  if (methods.isPending) return <SkeletonRows rows={14} className="p-3" />
  if (methods.isError) return <ErrorState error={methods.error} onRetry={() => void methods.refetch()} />

  const nY = draft.variables.filter((v) => v.role === 'y').length
  const nX = draft.variables.filter((v) => v.role === 'x' || v.role === 'feature').length
  return (
    <div className="flex min-w-0 flex-col">
      {/* run bar */}
      <div className="space-y-2 border-b border-border p-3">
        <div className="flex items-center gap-1.5">
          <Input className="min-w-0 flex-1 font-medium" placeholder="Study name" aria-label="Study name" value={draft.name} onChange={(e) => patchDraft({ name: e.target.value })} />
          <Button size="icon" variant="ghost" title="New empty study" aria-label="New empty study" onClick={newStudy}>
            <FilePlus2 />
          </Button>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button variant="primary" size="md" disabled={issues.length > 0 || running} onClick={() => onRun(spec(), 'auto')}>
            {running ? <Loader2 className="animate-spin" /> : <Play />} {draft.method === 'leaderboard' ? 'Run leaderboard' : 'Run'}
          </Button>
          <Button size="sm" variant="outline" disabled={issues.length > 0 || running} onClick={() => onRun(spec(), 'async')} title="Queue on the background worker even if it would be quick">
            Queue
          </Button>
          <Button size="sm" variant="secondary" disabled={draft.variables.length === 0 || save.isPending} onClick={() => save.mutate(false)}>
            <Save /> {studyId ? 'Save' : 'Save study'}
          </Button>
          {studyId && (
            <Button size="sm" variant="ghost" disabled={save.isPending} onClick={() => save.mutate(true)}>
              Save as new
            </Button>
          )}
          {studyId && <Badge tone="neutral" className="ml-auto">saved study</Badge>}
        </div>
        {estSpec && estimate.data && (
          <div className="num space-y-0.5 text-2xs text-muted" aria-live="polite">
            <div>
              {estimate.data.path === 'sync' ? 'Runs inline' : estimate.data.path === 'lane' ? 'Runs on the studio lane' : 'Queued on the worker'} · {estimateBreakdown(estimate.data)}
            </div>
            {extrasLine(estimate.data) && <div title="Heavy extras are opt-in: switch them on in the parameters below">{extrasLine(estimate.data)}</div>}
          </div>
        )}
        {issues.length > 0 && draft.variables.length > 0 && (
          <ul className="space-y-0.5 text-2xs text-warn" aria-label="Problems to fix before running">
            {issues.map((i) => (
              <li key={i}>• {i}</li>
            ))}
          </ul>
        )}
      </div>

      <Section n={1} title="Method" sub={method?.title}>
        <MethodPicker methods={methods.data} value={draft.method} onPick={(m) => setDraft(switchMethod(draft, m))} />
        {method && (
          <>
            <p className="mt-2 text-xs text-muted">{method.description}</p>
            <div className="mt-1 flex flex-wrap gap-1">
              {method.shapes.map((s) => (
                <Badge key={s} tone={s === draft.kind ? 'accent' : 'neutral'}>
                  {s.replace('_', '-')}
                </Badge>
              ))}
              {method.needsY ? <Badge tone="neutral">needs y</Badge> : <Badge tone="neutral">unsupervised</Badge>}
            </div>
          </>
        )}
      </Section>

      <Section n={2} title="Dataset" sub={`${draft.kind.replace('_', '-')} · ${draft.frequency}${draft.start ? ` · from ${draft.start}` : ''}`}>
        <DatasetSection draft={draft} patch={patchDraft} />
      </Section>

      <Section
        n={3}
        title="Variables"
        sub={method?.needsY === false ? `${nX} ${featureLabel.toLowerCase()}${nX === 1 ? '' : 's'}` : method && hasIvRoles(method) ? `y ${nY} · x ${nX} · endog ${draft.variables.filter((v) => v.role === 'endog').length} · instr. ${draft.variables.filter((v) => v.role === 'instrument').length}` : `y ${nY} · x ${nX}`}
        right={
          <Button size="xs" variant={picking ? 'secondary' : 'outline'} onClick={() => setPicking(!picking)} aria-expanded={picking}>
            <Plus /> {picking ? 'Close catalog' : 'Add variable'}
          </Button>
        }
      >
        {picking && (
          <div className="mb-2 rounded-md border border-border-strong bg-surface p-2">
            <VariablePicker
              roles={pickerRoles}
              onAdd={(v, role) => {
                const r: VariableRole = role === 'y' && draft.variables.some((x) => x.role === 'y') ? 'x' : role
                addVariable(draftVariableFrom(v, draft.variables.map((x) => x.name), r))
                toast({ tone: 'info', title: `Added ${v.field}`, message: `as ${r === 'feature' ? 'feature' : r}` })
              }}
            />
          </div>
        )}
        {draft.variables.length === 0 && !picking ? (
          <p className="py-4 text-center text-xs text-muted">No variables yet — add from the catalog or load a preset.</p>
        ) : (
          <div className="space-y-1.5">
            {draft.variables.map((v, i) => (
              <VariableCard
                key={v.uid}
                v={v}
                roles={roles}
                isFirst={i === 0}
                isLast={i === draft.variables.length - 1}
                panelLike={draft.kind !== 'timeseries' || !isSeriesSource(v.source)}
                featureLabel={featureLabel}
                onPatch={(p) => patchVariable(v.uid, p)}
                onRemove={() => removeVariable(v.uid)}
                onMove={(d) => moveVariable(v.uid, d)}
              />
            ))}
          </div>
        )}
      </Section>

      {method && (
        <Section key={method.id} n={4} title="Parameters" sub={method.fields.some((f) => f.required) ? `${method.title} · required inputs` : method.title} defaultOpen={method.fields.some((f) => f.required)}>
          <ParamForm
            fields={method.fields}
            params={draft.params}
            onChange={(params) => patchDraft({ params })}
            variables={draft.variables.map((v) => v.name)}
            hidden={roleDrivenParams(method)}
          />
          <div className="mt-3 grid grid-cols-2 gap-3 border-t border-border pt-2">
            <label className="text-xs text-fg">
              Seed
              <Input type="number" className="num mt-0.5 w-full" value={draft.seed} onChange={(e) => patchDraft({ seed: Math.trunc(Number(e.target.value) || 0) })} />
            </label>
            <label className="text-xs text-fg">
              Timeout (s)
              <Input type="number" className="num mt-0.5 w-full" value={draft.timeoutS} onChange={(e) => patchDraft({ timeoutS: Math.max(5, Math.trunc(Number(e.target.value) || 600)) })} />
            </label>
          </div>
        </Section>
      )}

      <Section n={5} title="Preview" sub="rows · summary · coverage" defaultOpen={false}>
        <PreviewSection draft={draft} disabled={draft.variables.length === 0} />
      </Section>
    </div>
  )
}
