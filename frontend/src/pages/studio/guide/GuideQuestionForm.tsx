import { Loader2, Plus, RotateCcw, Sparkles, X } from 'lucide-react'
import { type ReactNode, useState } from 'react'
import { useSectorTree } from '@/api/hooks'
import { useStudioCatalog } from '@/api/studio/hooks'
import type { CGuideVar, GuideGoal, GuideOutcomeKind, StudioVariableView } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Checkbox, Input, Select, Switch } from '@/components/ui/inputs'
import { Toggles } from '../builder/DatasetSection'
import { VariablePicker } from '../builder/VariablePicker'
import { type GuideForm, useGuide } from './guideStore'

const GOALS: { value: GuideGoal; label: string; hint: string }[] = [
  { value: 'effect', label: 'Effect', hint: 'Does the driver move the outcome, holding the controls fixed? (regression / causal estimate)' },
  { value: 'predict', label: 'Predict', hint: 'Forecast the outcome out of sample (purged walk-forward CV, OOS R² / IC).' },
  { value: 'rank', label: 'Rank', hint: 'Rank entities cross-sectionally each period (rank IC, top-minus-bottom spread).' },
  { value: 'uncertainty', label: 'Uncertainty', hint: 'Predictive distribution of the outcome: intervals, quantiles, calibration (probabilistic models).' },
  { value: 'heterogeneity', label: 'Heterogeneity', hint: 'Does the effect differ across groups (sectors, countries)? (partial pooling / interactions)' },
  { value: 'regime', label: 'Regime', hint: 'Does the relationship change across regimes or over time? (switching / time-varying models)' },
]

const OUTCOME_KINDS: { value: GuideOutcomeKind; label: string }[] = [
  { value: 'return', label: 'Return (forward % change)' },
  { value: 'level', label: 'Level' },
  { value: 'change', label: 'Change (difference)' },
  { value: 'binary', label: 'Binary (0 / 1 event)' },
]

const asVar = (v: StudioVariableView): CGuideVar => ({ source: v.source, field: v.field, name: null, binary: false })
const varLabel = (v: CGuideVar) => `${v.source}.${v.field}`

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-0.5">
      <div className="text-xs font-medium text-fg">{label}</div>
      {children}
      {hint && <div className="text-2xs text-muted">{hint}</div>}
    </div>
  )
}

function VarChip({ v, onRemove, label }: { v: CGuideVar; onRemove: () => void; label: string }) {
  return (
    <span className="num inline-flex h-6 items-center gap-1 rounded-sm border border-border bg-surface-2 pr-0.5 pl-1.5 text-xs text-fg">
      {varLabel(v)}
      <button type="button" aria-label={`Remove ${label} ${varLabel(v)}`} className="rounded-sm p-0.5 text-muted hover:text-fg" onClick={onRemove}>
        <X className="size-3" />
      </button>
    </span>
  )
}

/** Structured question: goal, outcome, driver, controls, horizon, frequency, universe, sectors, window. */
export function GuideQuestionForm({ onSuggest, pending }: { onSuggest: () => void; pending: boolean }) {
  const f = useGuide((s) => s.form)
  const patch = useGuide((s) => s.patch)
  const reset = useGuide((s) => s.reset)
  const cat = useStudioCatalog()
  const sectors = useSectorTree('ALL')
  const [picking, setPicking] = useState(!f.outcome)
  const goal = GOALS.find((g) => g.value === f.goal) ?? GOALS[0]
  const sectorIds = (sectors.data ?? []).map((s) => s.id)
  const sectorNames = Object.fromEntries((sectors.data ?? []).map((s) => [s.id, s.name]))
  const set =
    <K extends keyof GuideForm>(k: K) =>
    (v: GuideForm[K]) =>
      patch({ [k]: v } as Partial<GuideForm>)

  return (
    <form
      className="space-y-3"
      aria-label="Guided study question"
      onSubmit={(e) => {
        e.preventDefault()
        onSuggest()
      }}
    >
      <Field label="Goal" hint={goal.hint}>
        <Select label="Goal" className="w-full" value={f.goal} onChange={set('goal')} options={GOALS.map((g) => ({ value: g.value, label: g.label }))} />
      </Field>

      <div className="space-y-1.5 rounded-md border border-border p-2">
        <div className="flex items-center gap-2">
          <span className="text-xs font-medium text-fg">Variables</span>
          <Button size="xs" variant={picking ? 'secondary' : 'outline'} className="ml-auto" aria-expanded={picking} onClick={() => setPicking(!picking)}>
            <Plus /> {picking ? 'Close catalog' : 'Pick from catalog'}
          </Button>
        </div>
        <Field label="Outcome">
          {f.outcome ? (
            <VarChip v={f.outcome} label="outcome" onRemove={() => patch({ outcome: null })} />
          ) : (
            <div className="text-2xs text-warn">Required — pick it from the catalog (“as outcome”).</div>
          )}
        </Field>
        <Field label="Driver (optional)">
          {f.driver ? (
            <div className="flex flex-wrap items-center gap-2">
              <VarChip v={f.driver} label="driver" onRemove={() => patch({ driver: null })} />
              <Checkbox label="Driver is binary (0 / 1 treatment)" checked={f.driver.binary === true} onCheckedChange={(c) => f.driver && patch({ driver: { ...f.driver, binary: c === true } })} />
              <span className="text-2xs text-muted">binary (treatment)</span>
            </div>
          ) : (
            <div className="text-2xs text-muted">None: the guide studies the outcome with the controls / suggested predictors.</div>
          )}
        </Field>
        <Field label="Controls (optional)">
          <div className="flex flex-wrap gap-1">
            {f.controls.map((c, i) => (
              <VarChip key={`${varLabel(c)}-${i}`} v={c} label="control" onRemove={() => patch({ controls: f.controls.filter((_, j) => j !== i) })} />
            ))}
            {!f.controls.length && <span className="text-2xs text-muted">None picked.</span>}
          </div>
          <div className="mt-1 flex items-center gap-2">
            <Switch label="Suggest standard controls" checked={f.suggestControls} onCheckedChange={(c) => patch({ suggestControls: c })} />
            <span className="text-2xs text-muted">suggest standard controls (size, value, momentum, sector …)</span>
          </div>
        </Field>
        {picking && (
          <div className="rounded-md border border-border-strong bg-surface p-2">
            <VariablePicker
              roles={[
                { role: 'y', label: 'outcome' },
                { role: 'x', label: 'driver' },
                { role: 'feature', label: 'control' },
              ]}
              onAdd={(v, role) => {
                if (role === 'y') patch({ outcome: asVar(v) })
                else if (role === 'x') patch({ driver: asVar(v) })
                else if (!f.controls.some((c) => c.source === v.source && c.field === v.field)) patch({ controls: [...f.controls, asVar(v)] })
              }}
            />
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Field label="Outcome kind">
          <Select label="Outcome kind" className="w-full" value={f.outcomeKind} onChange={set('outcomeKind')} options={OUTCOME_KINDS} />
        </Field>
        <Field label="Horizon (periods ahead)">
          <Input
            type="number"
            min={0}
            max={60}
            aria-label="Horizon in periods"
            className="num w-full"
            value={f.horizon}
            onChange={(e) => patch({ horizon: Math.max(0, Math.min(60, Math.trunc(Number(e.target.value) || 0))) })}
          />
        </Field>
        <Field label="Frequency">
          <Select
            label="Frequency"
            className="w-full"
            value={f.frequency}
            onChange={set('frequency')}
            options={[
              { value: 'D', label: 'Daily' },
              { value: 'W', label: 'Weekly' },
              { value: 'M', label: 'Monthly' },
              { value: 'Q', label: 'Quarterly' },
            ]}
          />
        </Field>
        <Field label="Dataset shape">
          <Select
            label="Dataset shape"
            className="w-full"
            value={f.kind}
            onChange={set('kind')}
            options={[
              { value: 'auto', label: 'Auto' },
              { value: 'panel', label: 'Panel' },
              { value: 'timeseries', label: 'Time series' },
              { value: 'cross_section', label: 'Cross-section' },
            ]}
          />
        </Field>
        <Field label="Universe">
          <Select
            label="Universe"
            className="w-full"
            value={f.universe}
            onChange={set('universe')}
            options={[{ value: '', label: 'None — macro time series' }, ...(cat.data?.universes ?? ['mvp_us']).map((u) => ({ value: u, label: u }))]}
          />
        </Field>
        <div className="grid grid-cols-2 gap-1">
          <Field label="Start">
            <Input type="date" aria-label="Start date" className="num w-full" value={f.start} onChange={(e) => patch({ start: e.target.value })} />
          </Field>
          <Field label="End">
            <Input type="date" aria-label="End date" className="num w-full" value={f.end} onChange={(e) => patch({ end: e.target.value })} />
          </Field>
        </div>
      </div>

      {f.universe && sectorIds.length > 0 && (
        <Field label="Sectors (optional)" hint={f.sectors.length ? `${f.sectors.length} selected` : 'All sectors'}>
          <Toggles label="Sectors" all={sectorIds} selected={f.sectors} onChange={(s) => patch({ sectors: s })} names={sectorNames} />
        </Field>
      )}

      <div className="flex items-center gap-1.5 border-t border-border pt-2">
        <Button type="submit" variant="primary" size="md" disabled={!f.outcome || pending}>
          {pending ? <Loader2 className="animate-spin" /> : <Sparkles />} Suggest
        </Button>
        <Button variant="ghost" size="sm" onClick={reset}>
          <RotateCcw /> Reset
        </Button>
      </div>
    </form>
  )
}
