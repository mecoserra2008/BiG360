import { Compass, Play, Upload } from 'lucide-react'
import { switchMethod } from '@/api/studio/adapters'
import { lintErrors } from '@/api/studio/adaptersS3'
import { useGuideSuggest, useStudioMethods } from '@/api/studio/hooks'
import type { CStudySpec, GuideSuggestionView, StudioMethodView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { FAMILY_LABEL } from '../builder/families'
import { LintIssues, LintSize } from '../lint/LintIssues'
import { useStudio } from '../store'
import { GuideQuestionForm } from './GuideQuestionForm'
import { toQuestion, useGuide } from './guideStore'

function describeStudy(s: CStudySpec): string {
  const d = s.dataset
  const parts = [d.kind.replace('_', '-'), d.frequency]
  if (d.start) parts.push(`from ${d.start}`)
  if (d.end) parts.push(`to ${d.end}`)
  const uni = (d.entities as { universe?: string | null } | undefined)?.universe
  if (uni) parts.push(`universe ${uni}`)
  parts.push(`${d.variables.length} variables`)
  return parts.join(' · ')
}

function Suggestion({ g, methods, onRun }: { g: GuideSuggestionView; methods: StudioMethodView[]; onRun: (spec: CStudySpec) => void }) {
  const loadSpec = useStudio((s) => s.loadSpec)
  const setDraft = useStudio((s) => s.setDraft)
  const m = methods.find((x) => x.id === g.method)
  const errors = lintErrors(g.lint)
  const open = () => loadSpec(g.study, { needsY: m?.needsY, studyId: null, presetId: null })
  const openWith = (alt: StudioMethodView) => {
    open()
    setDraft(switchMethod(useStudio.getState().draft, alt))
    toast({ tone: 'info', title: `Opened in the builder with ${alt.title}`, message: 'Check the variable roles and parameters before running.' })
  }
  return (
    <div className="space-y-3">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-md font-semibold text-fg">{m?.title ?? g.method}</h3>
          {m && <Badge tone="accent">{FAMILY_LABEL[m.family] ?? m.family}</Badge>}
          {g.study.y && (
            <Badge tone="neutral" className="normal-case">
              y: {g.study.y}
            </Badge>
          )}
          <Badge tone="neutral" className="normal-case">
            {g.study.x?.length ?? 0} regressors
          </Badge>
        </div>
        <div className="num mt-0.5 text-2xs text-muted">{describeStudy(g.study)}</div>
      </div>

      {g.rationale.length > 0 && (
        <section aria-label="Why this method">
          <h4 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Why</h4>
          <ul className="list-disc space-y-0.5 pl-4 text-xs text-fg">
            {g.rationale.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </section>
      )}

      {g.alternatives.length > 0 && (
        <section aria-label="Alternative methods">
          <h4 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Alternatives</h4>
          <ul className="space-y-1">
            {g.alternatives.map((a) => {
              const am = methods.find((x) => x.id === a.method)
              return (
                <li key={a.method} className="flex items-start gap-2 text-xs">
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!am}
                    title={am ? `Open the suggested study in the builder with ${am.title}` : 'Method not available'}
                    onClick={() => am && openWith(am)}
                  >
                    {am?.title ?? a.method}
                  </Button>
                  <span className="pt-0.5 text-muted">{a.why}</span>
                </li>
              )
            })}
          </ul>
        </section>
      )}

      <section aria-label="Pre-run check">
        <div className="mb-1 flex items-center gap-2">
          <h4 className="text-2xs font-semibold tracking-wide text-muted uppercase">Pre-run check</h4>
          <LintSize lint={g.lint} />
        </div>
        <LintIssues issues={g.lint.issues} />
      </section>

      <div className="flex flex-wrap items-center gap-1.5 border-t border-border pt-2">
        <Button
          variant="primary"
          size="md"
          disabled={!g.lint.ok}
          title={g.lint.ok ? 'Load into the builder and run' : 'Fix the blocking errors first (open in the builder)'}
          onClick={() => {
            open()
            onRun(g.study)
          }}
        >
          <Play /> Run
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            open()
            toast({ tone: 'info', title: 'Suggested study loaded into the builder', message: m?.title ?? g.method })
          }}
        >
          <Upload /> Open in builder
        </Button>
        {!g.lint.ok && (
          <span role="alert" className="text-xs text-down">
            Run disabled: {errors.length} blocking error{errors.length === 1 ? '' : 's'} ({errors.map((e) => e.code.replace(/_/g, ' ')).join(', ')})
          </span>
        )}
      </div>
    </div>
  )
}

/** v1.50 guided builder: a structured question → suggested study, method rationale, alternatives and the lint. */
export function GuideTab({ onRun }: { onRun: (spec: CStudySpec) => void }) {
  const form = useGuide((s) => s.form)
  const suggestion = useGuide((s) => s.suggestion)
  const setSuggestion = useGuide((s) => s.setSuggestion)
  const methods = useStudioMethods()
  const suggest = useGuideSuggest()
  const ask = () => {
    const q = toQuestion(form)
    if (!q) return
    suggest.mutate(q, { onSuccess: (g) => setSuggestion(g) })
  }
  return (
    <div className="grid items-start gap-3 xl:grid-cols-[minmax(360px,460px)_minmax(0,1fr)]">
      <Panel title="Ask a question" subtitle="Does the driver move the outcome over the horizon, controlling for …, in the universe?">
        <GuideQuestionForm onSuggest={ask} pending={suggest.isPending} />
      </Panel>
      <Panel title="Suggested study" subtitle="Rule-based (no LLM): method, controls and purged CV picked from the question">
        {suggest.isError ? (
          <ErrorState error={suggest.error} onRetry={ask} />
        ) : suggestion ? (
          <Suggestion g={suggestion} methods={methods.data ?? []} onRun={onRun} />
        ) : (
          <EmptyState icon={<Compass />} title="No suggestion yet" message="Pick an outcome (and optionally a driver and controls), then Suggest." />
        )}
      </Panel>
    </div>
  )
}
