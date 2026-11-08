import { useQuery } from '@tanstack/react-query'
import { Plus, Save, Trash2, X } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { useNavigate, useParams, useSearchParams } from 'react-router'
import type { components } from '@/api/generated/schema'
import { http, USE_MOCK } from '@/api/http'
import { useScenarios } from '@/api/macro/hooks'
import {
  DIRECTION_META,
  draftFromParams,
  draftFromThesis,
  draftToCreate,
  draftToUpdate,
  HORIZONS,
  newId,
  SUBJECT_HINT,
  SUBJECT_LABEL,
  validateDraft,
  type Draft,
} from '@/api/theses/adapters'
import { useCreateThesis, useSubjectDefaults, useThesis, useThesisMutations } from '@/api/theses/hooks'
import type { SubjectType, ThesisCatalyst, ThesisInvalidation, ThesisKpi } from '@/api/theses/types'
import { BandHintButtons } from '@/components/conformal/ThesisConformal'
import { Button } from '@/components/ui/button'
import { ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { fmtDate, fmtPrice } from '@/lib/format'
import { textareaCls } from './shared'

type ScreenerField = components['schemas']['ScreenerField']

export function ThesisNew() {
  const [sp] = useSearchParams()
  return <EditorForm initial={draftFromParams(sp)} mode="create" />
}

export function ThesisEdit() {
  const { id = '' } = useParams()
  const q = useThesis(id)
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  if (!q.data) return <SkeletonRows rows={12} />
  return <EditorForm initial={draftFromThesis(q.data)} mode="edit" id={id} closed={q.data.status === 'closed'} />
}

function Field({ label, children, hint, className }: { label: string; children: ReactNode; hint?: ReactNode; className?: string }) {
  return (
    <label className={`flex min-w-0 flex-col gap-1 text-xs text-muted ${className ?? ''}`}>
      <span className="label-caps">{label}</span>
      {children}
      {hint && <span className="text-2xs text-faint">{hint}</span>}
    </label>
  )
}

function useDebounced<T>(v: T, ms = 400): T {
  const [d, setD] = useState(v)
  useEffect(() => {
    const h = setTimeout(() => setD(v), ms)
    return () => clearTimeout(h)
  }, [v, ms])
  return d
}

function EditorForm({ initial, mode, id, closed }: { initial: Draft; mode: 'create' | 'edit'; id?: string; closed?: boolean }) {
  const nav = useNavigate()
  const [d, setD] = useState<Draft>(initial)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setD((x) => ({ ...x, [k]: v }))
  const subj = useDebounced(d.subjectId.trim())
  const dfl = useSubjectDefaults(d.subjectType, subj)
  const scen = useScenarios()
  const fields = useQuery({ queryKey: ['screener', 'fields'], queryFn: () => http.get<ScreenerField[]>('/screener/fields'), staleTime: 3600_000, enabled: !USE_MOCK })
  const create = useCreateThesis()
  const muts = useThesisMutations(id ?? '')
  // default title from the resolved subject (create only, while the title is empty)
  const label = dfl.data?.subject_label
  const autoTitle = mode === 'create' && label ? `${d.direction === 'short' ? 'Short' : d.direction === 'neutral' ? 'Watch' : 'Long'} ${label}` : ''
  const eff: Draft = d.title.trim() ? d : { ...d, title: autoTitle }
  const errors = validateDraft(eff)
  const [tried, setTried] = useState(false)
  const busy = create.isPending || muts.update.isPending

  const save = () => {
    setTried(true)
    if (errors.length) return
    const onError = (e: unknown) => toast({ tone: 'error', title: 'Could not save the thesis', message: (e as Error).message })
    if (mode === 'create')
      create.mutate(draftToCreate(eff), {
        onSuccess: (t) => {
          toast({ tone: 'success', title: 'Thesis created', message: `${t.alerts.length} linked alert rules generated.` })
          nav(`/theses/${encodeURIComponent(t.thesis_id)}`)
        },
        onError,
      })
    else
      muts.update.mutate(draftToUpdate(eff), {
        onSuccess: () => {
          toast({ tone: 'success', title: 'Thesis saved', message: 'Linked alert rules regenerated and status re-evaluated.' })
          nav(`/theses/${encodeURIComponent(id as string)}`)
        },
        onError,
      })
  }

  const numericFields = (fields.data ?? []).filter((f) => f.type === 'number')
  const isSec = d.subjectType === 'security'
  return (
    <div className="space-y-3">
      <PageHeader title={mode === 'create' ? 'New thesis' : 'Edit thesis'} subtitle="Subject, plan, KPIs and invalidation. Saving regenerates the linked alert rules and re-evaluates the automatic status.">
        <Button variant="ghost" onClick={() => nav(-1)}>
          <X /> Cancel
        </Button>
        <Button variant="primary" onClick={save} disabled={busy}>
          <Save /> {mode === 'create' ? 'Create thesis' : 'Save'}
        </Button>
      </PageHeader>
      {tried && errors.length > 0 && (
        <div role="alert" className="rounded-md border border-down/40 bg-down-soft px-3 py-2 text-xs text-down">
          {errors.map((e) => (
            <div key={e}>{e}</div>
          ))}
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="space-y-3">
          <Panel title="Subject & view">
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Subject type">
                <Select label="Subject type" value={d.subjectType} onChange={(v) => set('subjectType', v as SubjectType)} options={Object.entries(SUBJECT_LABEL).map(([value, label]) => ({ value, label }))} disabled={mode === 'edit'} />
              </Field>
              <Field label="Subject" hint={SUBJECT_HINT[d.subjectType]} className="lg:col-span-3">
                <Input aria-label="Subject id" value={d.subjectId} onChange={(e) => set('subjectId', e.target.value)} placeholder={SUBJECT_HINT[d.subjectType]} disabled={mode === 'edit'} />
              </Field>
            </div>
            <div className="mt-2 min-h-5 text-xs" aria-live="polite">
              {dfl.isError ? (
                <span className="text-down">Unknown subject - check the id.</span>
              ) : dfl.data ? (
                <span className="text-muted">
                  <span className="font-semibold text-fg">{dfl.data.subject_label}</span>
                  {dfl.data.price_id ? (
                    <>
                      {' '}
                      · price instrument <span className="num">{dfl.data.price_id}</span> {dfl.data.last_price != null && <>last {fmtPrice(dfl.data.last_price)} {dfl.data.price_currency} ({fmtDate(dfl.data.price_date)})</>}
                    </>
                  ) : (
                    ' · no price instrument (set a proxy below for bands and performance)'
                  )}
                  {' '}· benchmark <span className="num">{dfl.data.benchmark_id ?? '—'}</span>
                  {dfl.data.sector_benchmark_id && (
                    <>
                      {' '}· sector <span className="num">{dfl.data.sector_benchmark_id}</span>
                    </>
                  )}
                </span>
              ) : null}
            </div>
            <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Title" className="sm:col-span-2 lg:col-span-4">
                <Input aria-label="Title" value={d.title} onChange={(e) => set('title', e.target.value)} placeholder={autoTitle || 'One line: what you believe and why'} maxLength={160} />
              </Field>
              <Field label="Direction">
                <Segmented label="Direction" value={d.direction} onChange={(v) => set('direction', v)} options={Object.entries(DIRECTION_META).map(([value, m]) => ({ value: value as Draft['direction'], label: m.label }))} />
              </Field>
              <Field label="Horizon">
                <Select label="Horizon" value={d.horizon} onChange={(v) => set('horizon', v)} options={HORIZONS.map((h) => ({ value: h, label: h }))} />
              </Field>
              <Field label={`Conviction (${d.conviction}/5)`}>
                <Segmented label="Conviction" value={String(d.conviction)} onChange={(v) => set('conviction', Number(v))} options={[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: String(n) }))} />
              </Field>
              {mode === 'create' && (
                <Field label="Start as">
                  <Segmented label="Initial status" value={d.status === 'active' ? 'active' : 'idea'} onChange={(v) => set('status', v)} options={[{ value: 'idea', label: 'Idea' }, { value: 'active', label: 'Active' }]} />
                </Field>
              )}
            </div>
          </Panel>
          <Panel title="Thesis" subtitle="Markdown: # headings, - bullets, **bold**, *italic*, `code`, [links](https://...)">
            <textarea aria-label="Thesis text (Markdown)" className={`${textareaCls} min-h-40 font-mono`} value={d.thesisMd} onChange={(e) => set('thesisMd', e.target.value)} placeholder={'## Why now\n- ...\n\n## Variant perception\n...'} />
            <Field label="Key drivers (one per line)" className="mt-3">
              <textarea aria-label="Key drivers" className={`${textareaCls} min-h-20`} value={d.drivers} onChange={(e) => set('drivers', e.target.value)} placeholder={'Pricing power\nMargin expansion'} />
            </Field>
          </Panel>
          <KpiEditor kpis={d.kpis} onChange={(v) => set('kpis', v)} fields={numericFields} subjectType={d.subjectType} />
          <CatalystEditor items={d.catalysts} onChange={(v) => set('catalysts', v)} suggestions={dfl.data?.suggested_catalysts ?? []} />
          <InvalidationEditor items={d.invalidation} onChange={(v) => set('invalidation', v)} kpis={d.kpis} />
        </div>
        <div className="space-y-3">
          <Panel title="Price plan" subtitle={`On the price instrument (${dfl.data?.price_currency ?? 'trading currency'}); bands become price alerts`}>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Entry low">
                <Input aria-label="Entry low" inputMode="decimal" value={d.entryLo} onChange={(e) => set('entryLo', e.target.value)} />
              </Field>
              <Field label="Entry high">
                <Input aria-label="Entry high" inputMode="decimal" value={d.entryHi} onChange={(e) => set('entryHi', e.target.value)} />
              </Field>
              <Field label="Target">
                <Input aria-label="Target" inputMode="decimal" value={d.target} onChange={(e) => set('target', e.target.value)} />
              </Field>
              <Field label="Stop">
                <Input aria-label="Stop" inputMode="decimal" value={d.stop} onChange={(e) => set('stop', e.target.value)} />
              </Field>
            </div>
            {dfl.data?.last_price != null && (
              <div className="mt-2 flex flex-wrap gap-1">
                {[
                  ['Entry ±3%', () => setD((x) => ({ ...x, entryLo: (dfl.data!.last_price! * 0.97).toFixed(2), entryHi: (dfl.data!.last_price! * 1.03).toFixed(2) }))],
                  ['Target +25%', () => set('target', (dfl.data!.last_price! * (d.direction === 'short' ? 0.75 : 1.25)).toFixed(2))],
                  ['Stop -12%', () => set('stop', (dfl.data!.last_price! * (d.direction === 'short' ? 1.12 : 0.88)).toFixed(2))],
                ].map(([l, fn]) => (
                  <Button key={l as string} size="xs" variant="outline" onClick={fn as () => void}>
                    {l as string}
                  </Button>
                ))}
              </div>
            )}
            {d.subjectType === 'security' && (dfl.data?.price_id ?? d.subjectId) && (
              <BandHintButtons
                securityId={dfl.data?.price_id ?? d.subjectId}
                direction={d.direction}
                onApply={(h) => setD((x) => ({ ...x, ...Object.fromEntries(Object.entries(h).filter(([, v]) => v !== undefined)) }))}
              />
            )}
          </Panel>
          <Panel title="Benchmarks & instrument" subtitle="Leave empty for the subject defaults">
            <div className="grid gap-3">
              <Field label="Price instrument" hint={dfl.data?.price_id ? `default ${dfl.data.price_id}` : 'e.g. ETF:TIP for an inflation theme'}>
                <Input aria-label="Price instrument" value={d.priceId} onChange={(e) => set('priceId', e.target.value)} placeholder={dfl.data?.price_id ?? ''} />
              </Field>
              <Field label="Benchmark" hint={dfl.data?.benchmark_id ? `default ${dfl.data.benchmark_id}` : undefined}>
                <Input aria-label="Benchmark" value={d.benchmarkId} onChange={(e) => set('benchmarkId', e.target.value)} placeholder={dfl.data?.benchmark_id ?? 'IDX:^GSPC'} />
              </Field>
              <Field label="Sector benchmark" hint={isSec ? (dfl.data?.sector_benchmark_id ? `default ${dfl.data.sector_benchmark_id}` : 'no US sector ETF for this sector') : undefined}>
                <Input aria-label="Sector benchmark" value={d.sectorBenchmarkId} onChange={(e) => set('sectorBenchmarkId', e.target.value)} placeholder={dfl.data?.sector_benchmark_id ?? ''} />
              </Field>
            </div>
          </Panel>
          <Panel title="Linked analysis" subtitle="W4 scenario, Model Studio and Backtest Lab ids">
            <div className="grid gap-3">
              <Field label="Macro scenario (W4)">
                <Select
                  label="Macro scenario"
                  value={d.scenarioId}
                  onChange={(v) => set('scenarioId', v)}
                  options={[{ value: '', label: 'None' }, ...(scen.data?.presets ?? []).map((s) => ({ value: s.scenario_id, label: `Preset: ${s.name}` })), ...(scen.data?.saved ?? []).map((s) => ({ value: s.scenario_id, label: s.name })), ...(d.scenarioId && !scen.data ? [{ value: d.scenarioId, label: d.scenarioId }] : [])]}
                />
              </Field>
              <div className="grid grid-cols-2 gap-3">
                <Field label="Studio study">
                  <Input aria-label="Studio study id" value={d.studyId} onChange={(e) => set('studyId', e.target.value)} placeholder="ST…" />
                </Field>
                <Field label="Studio run">
                  <Input aria-label="Studio run id" value={d.studioRunId} onChange={(e) => set('studioRunId', e.target.value)} placeholder="run id" />
                </Field>
                <Field label="Backtest strategy">
                  <Input aria-label="Backtest strategy id" value={d.backtestStrategyId} onChange={(e) => set('backtestStrategyId', e.target.value)} placeholder="BS…" />
                </Field>
                <Field label="Backtest run">
                  <Input aria-label="Backtest run id" value={d.backtestRunId} onChange={(e) => set('backtestRunId', e.target.value)} placeholder="run id" />
                </Field>
              </div>
            </div>
          </Panel>
          <Panel title="Housekeeping">
            <div className="grid gap-3">
              <Field label="Review every (days)" hint="Review reminders fire as alerts; empty = none">
                <Input aria-label="Review cadence in days" inputMode="numeric" value={d.reviewEveryDays} onChange={(e) => set('reviewEveryDays', e.target.value)} />
              </Field>
              <Field label="Tags (comma separated)">
                <Input aria-label="Tags" value={d.tags} onChange={(e) => set('tags', e.target.value)} placeholder="quality, US" />
              </Field>
              {closed && <p className="text-2xs text-muted">Closed thesis: the close reason, outcome and post-mortem are edited on the thesis page.</p>}
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}

const KPI_KINDS = [
  { value: 'metric', label: 'Metric' },
  { value: 'expr', label: 'Formula' },
  { value: 'xa', label: 'Cross-asset' },
  { value: 'macro', label: 'Macro cell' },
] as const
const OPS = [
  { value: 'gte', label: '≥' },
  { value: 'gt', label: '>' },
  { value: 'lte', label: '≤' },
  { value: 'lt', label: '<' },
  { value: 'between', label: 'between' },
] as const
const ROLES = [
  { value: 'confirm', label: 'Confirms (breach → at risk)' },
  { value: 'invalidate', label: 'Invalidates (breach → invalidated)' },
  { value: 'watch', label: 'Watch only' },
] as const

function KpiEditor({ kpis, onChange, fields, subjectType }: { kpis: ThesisKpi[]; onChange: (v: ThesisKpi[]) => void; fields: ScreenerField[]; subjectType: SubjectType }) {
  const upd = (i: number, p: Partial<ThesisKpi>) => onChange(kpis.map((k, j) => (j === i ? { ...k, ...p } : k)))
  const def: ThesisKpi = { kpi_id: newId('k'), label: '', kind: subjectType === 'cross_asset' ? 'xa' : subjectType === 'macro_theme' ? 'macro' : 'metric', ref: '', expr: null, op: 'gte', threshold: 0, threshold_hi: null, role: 'confirm', unit: null }
  return (
    <Panel
      title="KPIs to watch"
      subtitle="The threshold is the healthy condition. Metrics are screener field ids; formulas use the formula engine over them (sector / country subjects: median of members); cross-asset '<asset>:<signal>'; macro '<CC>:<indicator>'"
      actions={
        <Button size="xs" variant="outline" onClick={() => onChange([...kpis, def])}>
          <Plus /> KPI
        </Button>
      }
    >
      <datalist id="thesis-metric-fields">
        {fields.map((f) => (
          <option key={f.id} value={f.id}>
            {f.label}
          </option>
        ))}
      </datalist>
      {!kpis.length && <p className="text-xs text-muted">No KPIs yet. Each KPI becomes a linked alert and feeds the automatic status.</p>}
      <div className="space-y-2">
        {kpis.map((k, i) => (
          <div key={k.kpi_id ?? i} className="grid grid-cols-2 items-end gap-2 rounded-md border border-border p-2 md:grid-cols-[1.2fr_0.8fr_1.6fr_0.6fr_0.8fr_1.2fr_auto]">
            <Field label="Label">
              <Input aria-label={`KPI ${i + 1} label`} value={k.label} onChange={(e) => upd(i, { label: e.target.value })} placeholder="ROIC" />
            </Field>
            <Field label="Kind">
              <Select label={`KPI ${i + 1} kind`} value={k.kind} onChange={(v) => upd(i, { kind: v })} options={[...KPI_KINDS]} />
            </Field>
            <Field label={k.kind === 'expr' ? 'Formula' : 'Reference'}>
              {k.kind === 'expr' ? (
                <Input aria-label={`KPI ${i + 1} formula`} value={k.expr ?? ''} onChange={(e) => upd(i, { expr: e.target.value })} placeholder="roic - 0.08" className="num" />
              ) : (
                <Input aria-label={`KPI ${i + 1} reference`} value={k.ref ?? ''} onChange={(e) => upd(i, { ref: e.target.value })} list={k.kind === 'metric' ? 'thesis-metric-fields' : undefined} placeholder={k.kind === 'xa' ? 'CL:carry' : k.kind === 'macro' ? 'US:cpi_yoy' : 'roic'} className="num" />
              )}
            </Field>
            <Field label="Op">
              <Select label={`KPI ${i + 1} operator`} value={k.op} onChange={(v) => upd(i, { op: v })} options={[...OPS]} />
            </Field>
            <Field label={k.op === 'between' ? 'Low / high' : 'Threshold'}>
              <div className="flex gap-1">
                <Input aria-label={`KPI ${i + 1} threshold`} inputMode="decimal" value={Number.isFinite(k.threshold) ? String(k.threshold) : ''} onChange={(e) => upd(i, { threshold: Number(e.target.value.replace(',', '.')) })} className="w-full" />
                {k.op === 'between' && <Input aria-label={`KPI ${i + 1} upper threshold`} inputMode="decimal" value={k.threshold_hi == null ? '' : String(k.threshold_hi)} onChange={(e) => upd(i, { threshold_hi: e.target.value === '' ? null : Number(e.target.value.replace(',', '.')) })} className="w-full" />}
              </div>
            </Field>
            <Field label="Role">
              <Select label={`KPI ${i + 1} role`} value={k.role ?? 'confirm'} onChange={(v) => upd(i, { role: v })} options={[...ROLES]} />
            </Field>
            <Button size="icon-sm" variant="ghost" aria-label={`Remove KPI ${i + 1}`} onClick={() => onChange(kpis.filter((_, j) => j !== i))}>
              <Trash2 />
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  )
}

const CAT_KINDS = ['earnings', 'macro', 'corporate', 'product', 'regulatory', 'custom'] as const

function CatalystEditor({ items, onChange, suggestions }: { items: ThesisCatalyst[]; onChange: (v: ThesisCatalyst[]) => void; suggestions: ThesisCatalyst[] }) {
  const upd = (i: number, p: Partial<ThesisCatalyst>) => onChange(items.map((c, j) => (j === i ? { ...c, ...p } : c)))
  const fresh = suggestions.filter((s) => !items.some((c) => c.date === s.date && c.kind === s.kind))
  return (
    <Panel
      title="Catalysts"
      subtitle="Dated catalysts become reminders (custom kinds) and timeline markers; earnings dates come from the earnings calendar (source labelled)"
      actions={
        <Button size="xs" variant="outline" onClick={() => onChange([...items, { catalyst_id: newId('c'), label: '', date: null, kind: 'custom', done: false }])}>
          <Plus /> Catalyst
        </Button>
      }
    >
      {fresh.length > 0 && (
        <div className="mb-2 flex flex-wrap items-center gap-1 text-xs text-muted">
          Suggested:
          {fresh.map((s) => (
            <Button key={`${s.date}${s.label}`} size="xs" variant="secondary" onClick={() => onChange([...items, { ...s, catalyst_id: newId('c'), done: false }])}>
              <Plus /> {s.label} {s.date ? `· ${fmtDate(s.date)}` : ''}
            </Button>
          ))}
        </div>
      )}
      {!items.length && <p className="text-xs text-muted">No catalysts.</p>}
      <div className="space-y-2">
        {items.map((c, i) => (
          <div key={c.catalyst_id ?? i} className="grid grid-cols-[1fr_9rem_8rem_auto] items-end gap-2">
            <Field label="Catalyst">
              <Input aria-label={`Catalyst ${i + 1}`} value={c.label} onChange={(e) => upd(i, { label: e.target.value })} />
            </Field>
            <Field label="Date">
              <Input aria-label={`Catalyst ${i + 1} date`} type="date" value={c.date ?? ''} onChange={(e) => upd(i, { date: e.target.value || null })} />
            </Field>
            <Field label="Kind">
              <Select label={`Catalyst ${i + 1} kind`} value={c.kind ?? 'custom'} onChange={(v) => upd(i, { kind: v })} options={CAT_KINDS.map((k) => ({ value: k, label: k }))} />
            </Field>
            <Button size="icon-sm" variant="ghost" aria-label={`Remove catalyst ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}>
              <Trash2 />
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  )
}

const INV_KINDS = [
  { value: 'price_below', label: 'Price below' },
  { value: 'price_above', label: 'Price above' },
  { value: 'kpi', label: 'KPI breached' },
  { value: 'date', label: 'Deadline' },
  { value: 'manual', label: 'Manual' },
] as const

function InvalidationEditor({ items, onChange, kpis }: { items: ThesisInvalidation[]; onChange: (v: ThesisInvalidation[]) => void; kpis: ThesisKpi[] }) {
  const upd = (i: number, p: Partial<ThesisInvalidation>) => onChange(items.map((c, j) => (j === i ? { ...c, ...p } : c)))
  return (
    <Panel
      title="Invalidation conditions"
      subtitle="Any condition met turns the automatic status to invalidated (deadlines passed: at risk)"
      actions={
        <Button size="xs" variant="outline" onClick={() => onChange([...items, { condition_id: newId('i'), label: '', kind: 'price_below', level: null, triggered: false }])}>
          <Plus /> Condition
        </Button>
      }
    >
      {!items.length && <p className="text-xs text-muted">No invalidation conditions (the stop still invalidates).</p>}
      <div className="space-y-2">
        {items.map((c, i) => (
          <div key={c.condition_id ?? i} className="grid grid-cols-[1fr_9rem_10rem_auto] items-end gap-2">
            <Field label="Condition">
              <Input aria-label={`Condition ${i + 1}`} value={c.label} onChange={(e) => upd(i, { label: e.target.value })} placeholder="Loses the key contract" />
            </Field>
            <Field label="Kind">
              <Select label={`Condition ${i + 1} kind`} value={c.kind ?? 'manual'} onChange={(v) => upd(i, { kind: v })} options={[...INV_KINDS]} />
            </Field>
            <Field label={c.kind === 'kpi' ? 'KPI' : c.kind === 'date' ? 'Date' : c.kind === 'manual' ? 'Triggered' : 'Level'}>
              {c.kind === 'kpi' ? (
                <Select label={`Condition ${i + 1} KPI`} value={c.kpi_id ?? ''} onChange={(v) => upd(i, { kpi_id: v || null })} options={[{ value: '', label: 'Choose…' }, ...kpis.filter((k) => k.kpi_id).map((k) => ({ value: k.kpi_id as string, label: k.label || (k.kpi_id as string) }))]} />
              ) : c.kind === 'date' ? (
                <Input aria-label={`Condition ${i + 1} date`} type="date" value={c.date ?? ''} onChange={(e) => upd(i, { date: e.target.value || null })} />
              ) : c.kind === 'manual' ? (
                <Segmented label={`Condition ${i + 1} triggered`} value={c.triggered ? 'yes' : 'no'} onChange={(v) => upd(i, { triggered: v === 'yes' })} options={[{ value: 'no', label: 'No' }, { value: 'yes', label: 'Yes' }]} />
              ) : (
                <Input aria-label={`Condition ${i + 1} level`} inputMode="decimal" value={c.level == null ? '' : String(c.level)} onChange={(e) => upd(i, { level: e.target.value === '' ? null : Number(e.target.value.replace(',', '.')) })} />
              )}
            </Field>
            <Button size="icon-sm" variant="ghost" aria-label={`Remove condition ${i + 1}`} onClick={() => onChange(items.filter((_, j) => j !== i))}>
              <Trash2 />
            </Button>
          </div>
        ))}
      </div>
    </Panel>
  )
}
