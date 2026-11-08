/**
 * Studio v2 S3 (contract v1.50) mapping helpers: guided builder, pre-run lint, out-of-fold compare,
 * Model Registry and monitoring. Pure (no I/O) — unit-tested in __tests__/s3.test.ts.
 */
import type { components } from '../generated/schema'
import type {
  ExpandingPointView,
  GuideSuggestionView,
  LintIssueView,
  LintSeverity,
  LintView,
  ModelLastMonitorView,
  ModelMetricView,
  ModelStage,
  MonitorSchedule,
  PsiStatus,
  StudioCompareOosView,
  StudioModelMonitorView,
  StudioModelView,
} from './types'

type S = components['schemas']
type Obj = Record<string, unknown>

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const obj = (v: unknown): Obj | null => (v && typeof v === 'object' && !Array.isArray(v) ? (v as Obj) : null)
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])

/* ───────────── lint & guide ───────────── */

const SEVERITY_RANK: Record<LintSeverity, number> = { error: 0, warning: 1, info: 2 }

export function adaptLint(l: S['StudioLintResult']): LintView {
  const issues: LintIssueView[] = (l.issues ?? [])
    .map((i) => ({
      code: i.code,
      severity: (['error', 'warning', 'info'].includes(i.severity) ? i.severity : 'info') as LintSeverity,
      message: i.message,
      fix: i.fix ?? '',
      columns: i.columns ?? [],
    }))
    .sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity])
  return { ok: l.ok && !issues.some((i) => i.severity === 'error'), issues, estimatedRows: num(l.estimated_rows), nEntities: num(l.n_entities) }
}

export const lintErrors = (l: LintView | null | undefined): LintIssueView[] => (l?.issues ?? []).filter((i) => i.severity === 'error')

export function adaptGuide(g: S['StudioGuideSuggestion']): GuideSuggestionView {
  return {
    study: g.study,
    method: g.method,
    rationale: g.rationale ?? [],
    alternatives: (g.alternatives ?? []).map((a) => ({ method: a.method, why: a.why })),
    lint: adaptLint(g.lint),
  }
}

/* ───────────── out-of-fold compare ───────────── */

export function adaptCompareOos(o: S['StudioCompareOos'] | null | undefined): StudioCompareOosView | null {
  if (!o) return null
  return {
    benchmarkRunId: o.benchmark_run_id ?? null,
    tests: (o.tests ?? []).map((t) => ({
      runId: t.run_id,
      benchmarkRunId: t.benchmark_run_id,
      nRows: t.n_rows,
      nDates: num(t.n_dates),
      mseModel: num(t.mse_model),
      mseBenchmark: num(t.mse_benchmark),
      dmStat: num(t.dm_stat),
      dmP: num(t.dm_p),
      nested: t.nested === true,
      cwStat: num(t.cw_stat),
      cwP: num(t.cw_p),
      verdict: t.verdict ?? null,
      note: t.note ?? null,
    })),
    equity: (o.equity ?? []).map((e) => {
      // drop points without a date; keep cum_return aligned with the remaining dates
      const keep = e.dates.map((d, i) => [d, i] as const).filter(([d]) => typeof d === 'string')
      return {
        runId: e.run_id,
        kind: e.kind,
        dates: keep.map(([d]) => d as string),
        cumReturn: keep.map(([, i]) => num(e.cum_return[i])),
        sharpe: num(e.sharpe),
        meanIc: num(e.mean_ic),
        nPeriods: num(e.n_periods),
      }
    }),
    skipped: (o.skipped ?? []).map((s) => ({ runId: s.run_id ?? '', reason: s.reason ?? '' })),
  }
}

/* ───────────── Model Registry ───────────── */

const STAGES: ModelStage[] = ['candidate', 'production', 'archived']
const SCHEDULES: MonitorSchedule[] = ['none', 'weekly', 'monthly']

function adaptMetric(v: unknown): ModelMetricView | null {
  const m = obj(v)
  if (!m) return null
  return { kind: str(m.kind), value: num(m.value), baseline: num(m.baseline) }
}

function adaptLastMonitor(v: unknown): ModelLastMonitorView | null {
  const m = obj(v)
  if (!m) return null
  return { drift: typeof m.drift === 'boolean' ? m.drift : null, breaches: strs(m.breaches), metric: adaptMetric(m.metric), predictionPsi: num(m.prediction_psi) }
}

/** Detail responses keep the whole stored monitor result (list rows are trimmed: no feature_psi). */
function fullMonitor(m: S['StudioModel']): StudioModelMonitorView | null {
  const lm = obj(m.last_monitor)
  if (!lm || !Array.isArray(lm.feature_psi)) return null
  return adaptMonitor({ ...lm, model_id: m.model_id, run_id: str(lm.run_id) ?? m.run_id } as unknown as S['StudioModelMonitor'])
}

export function adaptModel(m: S['StudioModel']): StudioModelView {
  const metrics = obj(m.metrics) ?? {}
  return {
    modelId: m.model_id,
    name: m.name,
    version: m.version,
    runId: m.run_id,
    method: m.method,
    spec: obj(m.spec),
    specHash: m.spec_hash ?? '',
    snapshotId: m.snapshot_id ?? null,
    datasetKey: m.dataset_key ?? null,
    owner: m.owner ?? '',
    description: m.description ?? null,
    stage: STAGES.includes(m.stage) ? m.stage : 'candidate',
    metrics: Object.fromEntries(Object.entries(metrics).map(([k, v]) => [k, num(v)])),
    monitor: SCHEDULES.includes(m.monitor) ? m.monitor : 'none',
    psiThreshold: num(m.psi_threshold) ?? 0.25,
    metricFloor: num(m.metric_floor),
    alertId: m.alert_id ?? null,
    lastMonitor: adaptLastMonitor(m.last_monitor),
    lastMonitorDetail: fullMonitor(m),
    lastMonitoredAt: m.last_monitored_at ?? null,
    createdAt: m.created_at ?? null,
    updatedAt: m.updated_at ?? null,
    versions: (m.versions ?? []).map((v) => adaptModel(v as unknown as S['StudioModel'])),
  }
}

/** Headline metric of a registered model: the OOS metric first, then the usual fit statistics. */
export function modelHeadline(m: StudioModelView): { key: string; value: number } | null {
  for (const k of ['oos_rank_ic', 'mean_ic', 'rank_ic', 'oos_r2', 'r2_oos', 'oos_auc', 'auc', 'crps', 'oos_relative_rmse', 'adj_r2', 'r2', 'pseudo_r2', 'oos_rmse', 'rmse', 'silhouette']) {
    const v = m.metrics[k]
    if (typeof v === 'number') return { key: k, value: v }
  }
  const first = Object.entries(m.metrics).find(([k, v]) => typeof v === 'number' && k !== 'n_obs')
  return first ? { key: first[0], value: first[1] as number } : null
}

/** PSI convention: < 0.1 stable, 0.1–0.25 watch, ≥ 0.25 drift (the server's status wins when present). */
export function psiStatus(psi: number | null, status?: string | null): PsiStatus | null {
  if (status === 'stable' || status === 'watch' || status === 'drift') return status
  if (psi === null) return null
  return psi >= 0.25 ? 'drift' : psi >= 0.1 ? 'watch' : 'stable'
}

export function adaptMonitor(m: S['StudioModelMonitor']): StudioModelMonitorView {
  const w = obj(m.current_window)
  return {
    modelId: m.model_id,
    runId: m.run_id,
    currentWindow: w ? { start: str(w.start), end: str(w.end), afterBaselineEnd: typeof w.after_baseline_end === 'boolean' ? w.after_baseline_end : null } : null,
    featurePsi: (m.feature_psi ?? [])
      .map((f) => ({ feature: f.feature, psi: num(f.psi), status: psiStatus(num(f.psi), f.status), nCurrent: num(f.n_current) }))
      .sort((a, b) => (b.psi ?? -1) - (a.psi ?? -1)),
    predictionPsi: num(m.prediction_psi),
    metric: adaptMetric(m.metric),
    expanding: (m.expanding ?? [])
      .map((p): ExpandingPointView | null => {
        const r = obj(p)
        const date = r ? str(r.date) : null
        if (!r || !date) return null
        return { date, ic: num(r.ic), expandingIc: num(r.expanding_ic), expandingIcT: num(r.expanding_ic_t), expandingR2Oos: num(r.expanding_r2_oos), expandingRmse: num(r.expanding_rmse) }
      })
      .filter((p): p is ExpandingPointView => p !== null),
    breaches: m.breaches ?? [],
    drift: m.drift === true,
    psiThreshold: num(m.psi_threshold),
    metricFloor: num(m.metric_floor),
  }
}
