/**
 * Pure mapping helpers for the Model Studio API (contract v1.5) and the builder draft ↔ StudySpec round trip.
 * No I/O here — everything is unit-tested in __tests__/studio.test.ts.
 */
import type { components } from '../generated/schema'
import { adaptCompareOos } from './adaptersS3'
import type {
  CStudySpec,
  CTransform,
  CVariableSpec,
  DraftVariable,
  LaneAccuracy,
  ParamField,
  RunEstimateView,
  RunPreviewInfo,
  RunState,
  StudioCatalogView,
  StudioCompareView,
  StudioLaneView,
  StudioMethodView,
  StudioPresetView,
  StudioPreviewView,
  StudioResultView,
  StudioRunStatusView,
  StudioRunView,
  StudioStudyView,
  StudioTestView,
  StudioVariableView,
  StudyDraft,
  TestVerdict,
  VariableRole,
} from './types'

type S = components['schemas']

/* ───────────── catalog ───────────── */

export function adaptCatalog(c: S['StudioSourceCatalog']): StudioCatalogView {
  return {
    snapshotId: c.snapshot_id,
    sources: c.items.map((s) => ({
      source: s.source,
      table: s.table,
      title: s.title,
      entityType: s.entity_type,
      nativeFrequency: s.native_frequency,
      rows: s.rows,
      nEntities: s.n_entities,
      nVariables: s.n_variables ?? null,
      firstDate: s.first_date ?? null,
      lastDate: s.last_date ?? null,
    })),
    transforms: c.transforms,
    universes: c.universes,
  }
}

export function adaptVariable(v: S['StudioVariable']): StudioVariableView {
  return {
    key: `${v.source}:${v.field}`,
    source: v.source,
    field: v.field,
    table: v.table,
    title: v.title,
    country: v.country ?? null,
    frequency: v.frequency,
    units: v.units ?? null,
    entityType: v.entity_type,
    nObs: v.n_obs,
    nEntities: v.n_entities ?? null,
    firstDate: v.first_date ?? null,
    lastDate: v.last_date ?? null,
    pit: v.pit,
  }
}

/* ───────────── methods: params JSON schema → form fields ───────────── */

type JsonSchema = {
  type?: string | string[]
  enum?: unknown[]
  anyOf?: JsonSchema[]
  items?: JsonSchema
  minItems?: number
  maxItems?: number
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  additionalProperties?: JsonSchema | boolean
  properties?: Record<string, JsonSchema>
  default?: unknown
  title?: string
  description?: string
  format?: string
  $ref?: string
}

const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** Pydantic-style params schema → ordered list of form controls. Unknown shapes fall back to a JSON text field. */
export function paramFields(schema: Record<string, unknown> | null | undefined, inheritedDefs?: Record<string, JsonSchema>): ParamField[] {
  const root = (schema ?? {}) as { properties?: Record<string, JsonSchema>; $defs?: Record<string, JsonSchema> }
  const defs = { ...(inheritedDefs ?? {}), ...(root.$defs ?? {}) }
  const deref = (x: JsonSchema): JsonSchema => {
    if (!x.$ref) return x
    const name = x.$ref.split('/').pop() ?? ''
    return { ...(defs[name] ?? {}), title: x.title ?? defs[name]?.title, description: x.description ?? defs[name]?.description }
  }
  return Object.entries(root.properties ?? {}).map(([key, raw0]) => {
    const raw = deref(raw0)
    const branches = (raw.anyOf ?? [raw]).map(deref)
    const nullable = branches.some((b) => b.type === 'null') || (Array.isArray(raw.type) && raw.type.includes('null'))
    const s: JsonSchema = { ...(branches.find((b) => b.type !== 'null') ?? raw) }
    const t = Array.isArray(s.type) ? s.type.find((x) => x !== 'null') : (s.type ?? (s.properties ? 'object' : undefined))
    const items = s.items ? deref(s.items) : undefined
    let kind: ParamField['kind'] = 'json'
    let length: number | null = null
    if (s.enum) kind = 'enum'
    else if (t === 'boolean') kind = 'boolean'
    else if (t === 'integer') kind = 'integer'
    else if (t === 'number') kind = 'number'
    else if (t === 'string') kind = s.format === 'date' ? 'date' : 'string'
    else if (t === 'object' && s.properties?.mode && s.properties?.events) kind = 'treatment'
    else if (t === 'array' && items?.properties?.name && items?.properties?.method) kind = 'candidates'
    else if (t === 'object' && s.properties && Object.keys(s.properties).length) kind = 'object'
    else if (t === 'array' && items?.properties?.entity && items?.properties?.date) kind = 'events'
    else if (t === 'array' && items?.type === 'integer' && s.minItems !== undefined && s.minItems === s.maxItems) {
      kind = 'int_tuple'
      length = s.minItems
    } else if (t === 'array' && (items?.type === 'number' || items?.type === 'integer')) kind = 'number_list'
    else if (t === 'array' && items?.type === 'string') kind = 'string_list'
    else if (t === 'object' && typeof s.additionalProperties === 'object' && s.additionalProperties.type === 'integer') kind = 'int_map'
    const required = ((schema as { required?: string[] } | null)?.required ?? []).includes(key)
    return {
      key,
      title: raw0.title ?? raw.title ?? s.title ?? key,
      description: raw0.description ?? raw.description ?? s.description ?? null,
      kind,
      options: (s.enum ?? []).map(String),
      enumValues: s.enum ?? [],
      min: numOrNull(s.minimum),
      max: numOrNull(s.maximum),
      exclusiveMin: numOrNull(s.exclusiveMinimum),
      exclusiveMax: numOrNull(s.exclusiveMaximum),
      nullable,
      defaultValue: raw0.default ?? raw.default ?? s.default ?? (required ? null : kind === 'string_list' || kind === 'events' ? [] : null),
      length,
      required,
      children: kind === 'object' ? paramFields(s as Record<string, unknown>, defs) : [],
    }
  })
}

export function adaptMethod(m: S['StudioMethod']): StudioMethodView {
  return {
    id: m.id,
    family: m.family,
    title: m.title,
    description: m.description,
    shapes: m.shapes,
    needsY: m.needs_y,
    minX: m.min_x,
    maxX: m.max_x ?? null,
    usesWeights: m.uses_weights,
    outputs: m.outputs,
    diagnostics: m.diagnostics,
    fields: paramFields(m.params_schema),
  }
}

export function defaultParams(fields: ParamField[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((f) => [f.key, f.defaultValue]))
}

/** Only send params that differ from the schema default (keeps exported specs and spec hashes minimal). */
export function changedParams(params: Record<string, unknown>, fields: ParamField[]): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  const known = new Set(fields.map((f) => f.key))
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined) continue
    const f = fields.find((x) => x.key === k)
    if (!known.has(k) || JSON.stringify(v) !== JSON.stringify(f?.defaultValue ?? null)) out[k] = v
  }
  return out
}

/** Validate one param value against its field; returns an error message or null. */
export function validateParam(f: ParamField, v: unknown): string | null {
  if (v === null || v === undefined || v === '') return f.nullable || (f.kind === 'json' && !f.required) ? null : 'required'
  if (f.required && Array.isArray(v) && v.length === 0 && f.kind !== 'int_tuple') return 'required'
  if (f.kind === 'integer' || f.kind === 'number') {
    if (typeof v !== 'number' || !Number.isFinite(v)) return 'not a number'
    if (f.kind === 'integer' && !Number.isInteger(v)) return 'must be an integer'
    if (f.min !== null && v < f.min) return `≥ ${f.min}`
    if (f.max !== null && v > f.max) return `≤ ${f.max}`
    if (f.exclusiveMin !== null && v <= f.exclusiveMin) return `> ${f.exclusiveMin}`
    if (f.exclusiveMax !== null && v >= f.exclusiveMax) return `< ${f.exclusiveMax}`
  }
  if (f.kind === 'int_tuple' && (!Array.isArray(v) || v.length !== f.length || v.some((x) => !Number.isInteger(x)))) return `${f.length} integers`
  return null
}

export function adaptPreset(p: S['StudioPreset']): StudioPresetView {
  return {
    id: p.id,
    title: p.title,
    description: p.description,
    method: p.method,
    tags: p.tags,
    study: p.study,
    question: p.question ?? null,
    methodNote: p.method_note ?? null,
    expectedOutputs: p.expected_outputs ?? [],
    runtimeS: numOrNull(p.runtime_s),
    validated: p.validated ?? null,
  }
}

/* ───────────── preview ───────────── */

const adaptCoverage = (c: S['StudioCoverage']) => ({
  column: c.column,
  nNonNull: c.n_non_null,
  pctNonNull: c.pct_non_null,
  firstDate: c.first_date ?? null,
  lastDate: c.last_date ?? null,
  nEntities: c.n_entities ?? null,
  usesFuture: c.uses_future,
})

export function adaptPreview(p: S['StudioDatasetPreview']): StudioPreviewView {
  return {
    datasetKey: p.dataset_key,
    snapshotId: p.snapshot_id,
    cached: p.cached,
    kind: p.kind,
    frequency: p.frequency,
    start: p.start,
    end: p.end,
    nRows: p.n_rows,
    nEntities: p.n_entities,
    nPeriods: p.n_periods,
    columns: p.columns,
    rows: p.rows,
    stats: p.stats,
    coverage: p.coverage.map(adaptCoverage),
    warnings: p.warnings,
    buildSeconds: p.build_seconds ?? null,
  }
}

/* ───────────── runs & results ───────────── */

const metricValue = (v: unknown): number | string | null => (typeof v === 'number' ? (Number.isFinite(v) ? v : null) : typeof v === 'string' ? v : null)

export function adaptRunStatus(r: S['StudioRunStatus']): StudioRunStatusView {
  return {
    runId: r.run_id,
    studyId: r.study_id ?? null,
    name: r.name ?? null,
    title: r.title ?? null,
    method: r.method,
    status: r.status,
    mode: r.mode,
    createdAt: r.created_at,
    startedAt: r.started_at ?? null,
    finishedAt: r.finished_at ?? null,
    durationS: r.duration_s ?? null,
    estimateS: r.estimate_s ?? null,
    error: r.error ?? null,
    errorCode: r.error_code ?? null,
    specHash: r.spec_hash ?? null,
    nObs: r.n_obs ?? null,
    warningsCount: r.warnings_count ?? null,
    metrics: Object.fromEntries(Object.entries(r.metrics ?? {}).map(([k, v]) => [k, metricValue(v)])),
    ...adaptLive(r),
  }
}

type LiveFields = Pick<S['StudioRunStatus'], 'lane' | 'stage' | 'eta_s' | 'queue' | 'estimate' | 'preview'>

/** S1 live fields shared by Studio and backtest run statuses. */
export function adaptLive(r: LiveFields): Pick<StudioRunStatusView, 'lane' | 'stage' | 'etaS' | 'queue' | 'estimate' | 'preview'> {
  return {
    lane: r.lane ?? null,
    stage: r.stage ?? null,
    etaS: r.eta_s ?? null,
    queue: r.queue ? { position: r.queue.position ?? null, ahead: r.queue.ahead ?? 0, blockedBy: r.queue.blocked_by ?? null, queueS: r.queue.queue_s ?? null } : null,
    estimate: r.estimate ? adaptEstimate(r.estimate) : null,
    preview: r.preview ? adaptPreviewInfo(r.preview) : null,
  }
}

export function adaptEstimate(e: S['StudioRunEstimate']): RunEstimateView {
  return {
    totalS: e.total_s,
    runS: e.run_s,
    queueS: e.queue_s,
    overheadS: e.overhead_s,
    buildS: e.build_s,
    fitS: e.fit_s,
    path: e.path,
    model: e.model,
    nHistory: e.n_history ?? 0,
    extras: (e.extras ?? []).map((x) => ({ name: x.name, label: x.label ?? x.name, enabled: x.enabled, costS: x.cost_s })),
  }
}

export function adaptPreviewInfo(p: S['StudioRunPreviewInfo']): RunPreviewInfo {
  return {
    status: p.status,
    fraction: p.fraction ?? null,
    by: p.by ?? null,
    nRows: p.n_rows ?? null,
    nRowsFull: p.n_rows_full ?? null,
    durationS: p.duration_s ?? null,
    error: p.error ?? null,
    skipped: p.skipped ?? [],
  }
}

export function adaptLane(l: S['StudioLaneStatus']): StudioLaneView {
  const acc = (k: string): LaneAccuracy => {
    const a = (l.accuracy ?? {})[k] ?? {}
    return { n: a.n ?? 0, medianRatio: a.median_ratio ?? null, within30: a.within_30pct ?? null }
  }
  return {
    enabled: l.enabled,
    alive: l.alive,
    pool: l.pool ?? null,
    priority: l.priority ?? null,
    heavyJobs: l.heavy_jobs ?? [],
    running: (l.running ?? []).length,
    queued: (l.queue ?? []).length,
    blockedBy: l.blocked_by ?? null,
    accuracy: { studio: acc('studio'), backtest: acc('backtest') },
  }
}

export const isTerminal = (s: RunState): boolean => s === 'completed' || s === 'failed' || s === 'cancelled'

/**
 * Test colouring. v1.6: the server says whether the model passes a check (`passed`); tests without a pass/fail
 * reading (Granger, Hausman, cointegration, inference…) are shown as reject / do not reject at 5%.
 */
export function testVerdict(t: S['StudioTest']): TestVerdict {
  if (t.passed === true) return 'pass'
  if (t.passed === false) return 'fail'
  const c = (t.conclusion ?? '').toLowerCase()
  const p = numOrNull(t.p_value)
  const rejected = /reject/.test(c) ? !/do not reject|not reject|fail to reject/.test(c) : p !== null ? p < 0.05 : null
  if (rejected === null) return 'info'
  return rejected ? 'significant' : 'not_significant'
}

export function adaptResult(r: S['StudioResult']): StudioResultView {
  return {
    method: r.method,
    family: r.family,
    title: r.title,
    summary: r.summary ?? {},
    fitStats: r.fit_stats ?? {},
    coefficients: r.coefficients ?? [],
    tests: (r.tests ?? []).map((t) => ({
      ...t,
      statistic: numOrNull(t.statistic),
      pValue: numOrNull(t.p_value),
      df: Array.isArray(t.df) ? t.df : numOrNull(t.df),
      verdict: testVerdict(t),
    })) as StudioTestView[],
    tables: r.tables ?? [],
    forecast: r.forecast ?? null,
    clusters: r.clusters ?? null,
    evaluation: r.evaluation ?? null,
    plots: r.plots ?? {},
    warnings: r.warnings ?? [],
    metrics: r.metrics ?? {},
  }
}

export function adaptRun(r: S['StudioRun']): StudioRunView {
  const env = r.result
  return {
    status: adaptRunStatus(r.run),
    study: env?.study ?? null,
    dataset: env?.dataset ? { ...env.dataset, coverage: env.dataset.coverage.map(adaptCoverage) } : null,
    result: env?.result ? adaptResult(env.result) : null,
    reproducibility: env?.reproducibility ?? null,
    preview: r.preview ? { info: adaptPreviewInfo(r.preview.preview), result: r.preview.result ? adaptResult(r.preview.result as unknown as S['StudioResult']) : null } : null,
  }
}

export function adaptStudy(s: S['StudioStudy']): StudioStudyView {
  return {
    studyId: s.study_id,
    name: s.name,
    description: s.description ?? null,
    method: s.method,
    tags: s.tags,
    presetId: s.preset_id ?? null,
    lastRunId: s.last_run_id ?? null,
    schedule: s.schedule ?? 'none',
    driftThreshold: numOrNull(s.drift_threshold),
    lastScheduledAt: s.last_scheduled_at ?? null,
    alertId: s.alert_id ?? null,
    study: s.study,
    createdAt: s.created_at,
    updatedAt: s.updated_at,
  }
}

export function adaptCompare(c: S['StudioCompare']): StudioCompareView {
  return {
    runIds: c.run_ids,
    runs: c.runs.map(adaptRunStatus),
    metrics: c.metrics,
    coefficients: c.coefficients.map((row) => ({
      term: row.term,
      values: Object.fromEntries(
        Object.entries(row.values).map(([k, v]) => [k, v ? { estimate: numOrNull(v.estimate), stdError: numOrNull(v.std_error), pValue: numOrNull(v.p_value) } : null]),
      ),
    })),
    oos: adaptCompareOos(c.oos),
  }
}

/* ───────────── builder draft ↔ StudySpec ───────────── */

let uidSeq = 0
/** Time-series sources without entities (macro series, CFTC positioning, cross-asset signals). */
export const isSeriesSource = (s: string): boolean => s === 'macro' || s === 'cftc' || s === 'cross_asset'

export const newUid = (): string => `v${Date.now().toString(36)}${(uidSeq++).toString(36)}`

const VAR_KEYS = new Set(['name', 'source', 'field', 'agg', 'entity_agg', 'entities', 'pre_transforms', 'transforms'])
const DATASET_KEYS = new Set(['kind', 'frequency', 'start', 'end', 'entities', 'entity_level', 'macro_alignment', 'variables'])

export function specToDraft(spec: CStudySpec, opts: { needsY?: boolean } = {}): StudyDraft {
  const ds = spec.dataset
  const xs = new Set(spec.x ?? [])
  const strs = (v: unknown): Set<string> => new Set(Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
  const endog = strs(spec.params?.endog)
  const instr = strs(spec.params?.instruments)
  const role = (name: string): VariableRole => {
    if (spec.y && name === spec.y) return 'y'
    if (spec.weights && name === spec.weights) return 'weights'
    if (endog.has(name)) return 'endog'
    if (instr.has(name)) return 'instrument'
    if (xs.has(name)) return opts.needsY === false ? 'feature' : 'x'
    return 'none'
  }
  return {
    name: spec.name ?? '',
    kind: ds.kind,
    frequency: ds.frequency,
    start: ds.start ?? null,
    end: ds.end ?? null,
    entities: ds.entities ?? { primary_only: true },
    entityLevel: ds.entity_level ?? 'security',
    macroAlignment: ds.macro_alignment ?? 'auto',
    variables: ds.variables.map((v) => ({
      uid: newUid(),
      name: v.name,
      source: v.source,
      field: v.field,
      title: null,
      role: role(v.name),
      agg: v.agg ?? null,
      entityAgg: v.entity_agg ?? null,
      entities: v.entities ?? null,
      preTransforms: v.pre_transforms ?? [],
      transforms: v.transforms ?? [],
      extra: Object.fromEntries(Object.entries(v).filter(([k]) => !VAR_KEYS.has(k))),
    })),
    method: spec.method,
    params: { ...(spec.params ?? {}) },
    seed: spec.seed ?? 42,
    timeoutS: spec.timeout_s ?? 600,
    datasetExtra: Object.fromEntries(Object.entries(ds).filter(([k]) => !DATASET_KEYS.has(k))),
  }
}

/** Builder draft → StudySpec body (y / x / weights derived from variable roles; null fields omitted). */
export function draftToSpec(d: StudyDraft): CStudySpec {
  const vars = d.variables.map((v) => {
    const out: Record<string, unknown> = { ...v.extra, name: v.name, source: v.source, field: v.field }
    if (v.agg) out.agg = v.agg
    if (v.entityAgg) out.entity_agg = v.entityAgg
    if (v.entities) out.entities = v.entities
    if (v.preTransforms.length) out.pre_transforms = v.preTransforms
    out.transforms = v.transforms
    return out as unknown as CVariableSpec
  })
  const y = d.variables.find((v) => v.role === 'y')?.name ?? null
  const w = d.variables.find((v) => v.role === 'weights')?.name ?? null
  const x = d.variables.filter((v) => v.role === 'x' || v.role === 'feature').map((v) => v.name)
  const dataset: Record<string, unknown> = {
    ...d.datasetExtra,
    kind: d.kind,
    frequency: d.frequency,
    entity_level: d.entityLevel,
    macro_alignment: d.macroAlignment,
    variables: vars,
  }
  if (d.start) dataset.start = d.start
  if (d.end) dataset.end = d.end
  const ent = Object.fromEntries(Object.entries(d.entities).filter(([, v]) => v !== null && v !== undefined && !(Array.isArray(v) && v.length === 0)))
  if (Object.keys(ent).some((k) => k !== 'primary_only')) dataset.entities = ent
  const params: Record<string, unknown> = { ...d.params }
  const endog = d.variables.filter((v) => v.role === 'endog').map((v) => v.name)
  const instruments = d.variables.filter((v) => v.role === 'instrument').map((v) => v.name)
  if (endog.length || instruments.length) {
    params.endog = endog
    params.instruments = instruments
  }
  const spec: Record<string, unknown> = { dataset, method: d.method, x, params, seed: d.seed, timeout_s: d.timeoutS }
  if (d.name) spec.name = d.name
  if (y) spec.y = y
  if (w) spec.weights = w
  return spec as unknown as CStudySpec
}

/** Lowercase identifier from a field id (e.g. "fred:DGS10" → "dgs10"), unique within `taken`. */
export function variableName(field: string, taken: Iterable<string>): string {
  const base = (field.split(':').pop() ?? field).toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 24) || 'var'
  const used = new Set(taken)
  let name = /^[a-z]/.test(base) ? base : `v_${base}`
  for (let i = 2; used.has(name); i++) name = `${base}_${i}`
  return name
}

export function draftVariableFrom(v: StudioVariableView, taken: Iterable<string>, role: VariableRole): DraftVariable {
  return {
    uid: newUid(),
    name: variableName(v.field, taken),
    source: v.source,
    field: v.field,
    title: v.title,
    role,
    agg: null,
    entityAgg: null,
    entities: null,
    preTransforms: [],
    transforms: [],
    extra: {},
  }
}

/** Net lag implied by a transform chain: lag k → +k, lead k → −k (shown on the lag/lead spinner). */
export function netLag(ts: CTransform[]): number {
  return ts.reduce((a, t) => a + (t.op === 'lag' ? (t.k ?? 1) : t.op === 'lead' ? -(t.k ?? 1) : 0), 0)
}

/** Set the lag/lead spinner: replaces every lag/lead step with a single one at the end of the chain (0 removes it). */
export function withNetLag(ts: CTransform[], k: number): CTransform[] {
  const rest = ts.filter((t) => t.op !== 'lag' && t.op !== 'lead')
  if (k === 0) return rest
  return [...rest, k > 0 ? { op: 'lag', k } : { op: 'lead', k: -k }]
}

/** Default parameters for a transform op when added from the chain editor. */
export function defaultTransform(op: CTransform['op']): CTransform {
  if (op === 'lag' || op === 'lead' || op === 'diff' || op === 'pct_change' || op === 'fwd_sum' || op === 'fwd_compound') return { op, k: 1 }
  if (op.startsWith('rolling_') || op === 'drawdown') return { op, window: 12 }
  if (op === 'winsorize') return { op, lower: 0.01, upper: 0.99 }
  if (op === 'clip') return { op, lower: -1, upper: 1 }
  if (op === 'binarize') return { op, threshold: 0 }
  if (op === 'scale') return { op, factor: 100 }
  return { op }
}

export function describeTransform(t: CTransform): string {
  const p = [t.k != null ? `${t.k}` : null, t.window != null ? `w=${t.window}` : null, t.lower != null || t.upper != null ? `${t.lower ?? ''}…${t.upper ?? ''}` : null, t.threshold != null ? `>${t.threshold}` : null, t.factor != null ? `×${t.factor}` : null]
    .filter(Boolean)
    .join(', ')
  return p ? `${t.op}(${p})` : t.op
}

/** Builder checks before submitting (mirror of the server guardrails that can be checked client-side). */
export function draftIssues(d: StudyDraft, m: StudioMethodView | null): string[] {
  const out: string[] = []
  if (!m) return ['Pick a method']
  if (!d.variables.length) out.push('Add at least one variable')
  const names = d.variables.map((v) => v.name)
  if (new Set(names).size !== names.length) out.push('Variable names must be unique')
  if (names.some((n) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(n))) out.push('Variable names: letters, digits and _ only')
  const ys = d.variables.filter((v) => v.role === 'y').length
  const xs = d.variables.filter((v) => v.role === 'x' || v.role === 'feature').length
  if (m.needsY && ys !== 1) out.push(`${m.title} needs exactly one target (y)`)
  if (xs < m.minX) out.push(`${m.title} needs at least ${m.minX} regressor${m.minX === 1 ? '' : 's'}/feature${m.minX === 1 ? '' : 's'}`)
  if (m.maxX !== null && xs > m.maxX) out.push(`${m.title} accepts at most ${m.maxX} regressors`)
  if (!m.shapes.includes(d.kind)) out.push(`${m.title} does not support ${d.kind.replace('_', '-')} data (${m.shapes.join(', ')})`)
  if (hasIvRoles(m)) {
    const ne = d.variables.filter((v) => v.role === 'endog').length
    const ni = d.variables.filter((v) => v.role === 'instrument').length
    if (ne < 1) out.push('IV needs at least one endogenous regressor (role “endog”)')
    if (ni < ne) out.push('IV needs at least as many instruments as endogenous regressors')
  }
  for (const f of m.fields) if (f.required && !roleDrivenParams(m).has(f.key) && validateParam(f, d.params[f.key]) === 'required') out.push(`Set “${f.title}” in Parameters`)
  if (hasIvRoles(m)) {
    const ne = d.variables.filter((v) => v.role === 'endog').length
    const ni = d.variables.filter((v) => v.role === 'instrument').length
    if (ne < 1) out.push('IV needs at least one endogenous regressor (role “endog”)')
    if (ni < ne) out.push('IV needs at least as many instruments as endogenous regressors')
  }
  const y = d.variables.find((v) => v.role === 'y')
  for (const v of d.variables) {
    if (v.role === 'x' && v.transforms.some((t) => t.op === 'lead' || t.op === 'fwd_sum' || t.op === 'fwd_compound') && v !== y)
      out.push(`${v.name}: a regressor uses future data (lead / forward transform) — look-ahead`)
  }
  return out
}

/** Methods whose params take endogenous regressors + instruments as dataset columns (IV). */
export const hasIvRoles = (m: StudioMethodView): boolean => m.fields.some((f) => f.key === 'endog') && m.fields.some((f) => f.key === 'instruments')

/** Params the builder derives from variable roles (hidden from the generated form). */
export function roleDrivenParams(m: StudioMethodView | null): Set<string> {
  return new Set(m && hasIvRoles(m) ? ['endog', 'instruments'] : [])
}

/** Roles a variable can take under a method. */
export function rolesFor(m: StudioMethodView | null): VariableRole[] {
  if (!m) return ['y', 'x', 'none']
  if (!m.needsY) return ['feature', 'none']
  const iv = hasIvRoles(m)
  return ['y', 'x', ...(m.usesWeights ? (['weights'] as const) : []), ...(iv ? (['endog', 'instrument'] as const) : []), 'none']
}

/** Switching method: map roles onto the new method and drop params it does not know. */
export function switchMethod(d: StudyDraft, m: StudioMethodView): StudyDraft {
  const known = new Set(m.fields.map((f) => f.key))
  const variables = d.variables.map((v): DraftVariable => {
    if (!m.needsY) return { ...v, role: ['y', 'x', 'feature', 'endog', 'instrument'].includes(v.role) ? 'feature' : 'none' }
    if (v.role === 'feature') return { ...v, role: 'x' }
    if (v.role === 'weights' && !m.usesWeights) return { ...v, role: 'none' }
    if ((v.role === 'endog' || v.role === 'instrument') && !hasIvRoles(m)) return { ...v, role: 'x' }
    return v
  })
  if (m.needsY && !variables.some((v) => v.role === 'y')) {
    const first = variables.findIndex((v) => v.role === 'x')
    if (first >= 0 && variables.filter((v) => v.role === 'x').length > m.minX) variables[first] = { ...variables[first], role: 'y' }
  }
  const kind = m.shapes.includes(d.kind) ? d.kind : m.shapes[0]
  return { ...d, method: m.id, kind, variables, params: Object.fromEntries(Object.entries(d.params).filter(([k]) => known.has(k))) }
}
