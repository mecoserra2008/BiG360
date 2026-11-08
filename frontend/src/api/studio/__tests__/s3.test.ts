/** Studio v2 S3 (contract v1.50): guide, lint, out-of-fold compare, Model Registry, recipes, probabilistic family. */
import { describe, expect, it } from 'vitest'
import { GUIDE_DEFAULT, toQuestion } from '@/pages/studio/guide/guideStore'
import { gateDecision } from '@/pages/studio/lint/useLintGate'
import { filterPresets } from '@/pages/studio/presetFilter'
import { assignOutputs } from '@/pages/studio/results/assign'
import { FAMILY_LABEL } from '@/pages/studio/builder/families'
import type { components } from '../../generated/schema'
import { adaptCompare, adaptPreset, paramFields } from '../adapters'
import { adaptGuide, adaptLint, adaptModel, adaptMonitor, lintErrors, modelHeadline, psiStatus } from '../adaptersS3'
import type { CStudySpec, StudioResultView } from '../types'

type S = components['schemas']

const study = { method: 'ols', y: 'ret', x: ['size'], dataset: { kind: 'panel', frequency: 'M', variables: [] } } as unknown as CStudySpec

const lintPayload: S['StudioLintResult'] = {
  ok: false,
  issues: [
    { code: 'tiny_sample', severity: 'info', message: 'small', fix: 'widen' },
    { code: 'lookahead_regressor', severity: 'error', message: 'x uses t+1', fix: 'lag x', columns: ['x'] },
    { code: 'shuffled_cv', severity: 'warning', message: 'kfold', fix: 'use expanding' },
  ],
  estimated_rows: 1200,
  n_entities: null,
}

describe('lint & guide', () => {
  it('sorts issues by severity and keeps ok / sizes', () => {
    const l = adaptLint(lintPayload)
    expect(l.issues.map((i) => i.severity)).toEqual(['error', 'warning', 'info'])
    expect(l.issues[0].columns).toEqual(['x'])
    expect(l.issues[1].columns).toEqual([])
    expect(l).toMatchObject({ ok: false, estimatedRows: 1200, nEntities: null })
    expect(lintErrors(l).map((i) => i.code)).toEqual(['lookahead_regressor'])
  })
  it('treats an error issue as not ok even if the flag says ok', () => {
    expect(adaptLint({ ...lintPayload, ok: true }).ok).toBe(false)
  })
  it('adapts a guide suggestion', () => {
    const g = adaptGuide({ study, method: 'panel_fe', rationale: ['panel → FE'], alternatives: [{ method: 'fama_macbeth', why: 'premia' }], lint: { ok: true, issues: [] } })
    expect(g).toMatchObject({ method: 'panel_fe', rationale: ['panel → FE'], alternatives: [{ method: 'fama_macbeth', why: 'premia' }], lint: { ok: true, issues: [], estimatedRows: null } })
  })
  it('pre-run gate: lint failure never blocks, errors block, warnings are notes', () => {
    expect(gateDecision(null)).toBe('submit')
    expect(gateDecision(adaptLint(lintPayload))).toBe('block')
    expect(gateDecision(adaptLint({ ok: true, issues: [{ code: 'shuffled_cv', severity: 'warning', message: 'm', fix: 'f' }] }))).toBe('notes')
    expect(gateDecision(adaptLint({ ok: true, issues: [] }))).toBe('submit')
  })
  it('guide form → question', () => {
    expect(toQuestion(GUIDE_DEFAULT)).toBeNull()
    const q = toQuestion({ ...GUIDE_DEFAULT, outcome: { source: 'prices', field: 'ret_1m', binary: false }, universe: '', horizon: 99, sectors: [], start: '2010-01-01' })
    expect(q).toMatchObject({ universe: null, horizon: 60, sectors: null, start: '2010-01-01', end: null, goal: 'effect', suggest_controls: true })
  })
})

describe('out-of-fold compare', () => {
  const base: S['StudioCompare'] = { run_ids: ['A', 'B'], runs: [], metrics: [], coefficients: [] }
  it('is null on an old backend', () => {
    expect(adaptCompare(base).oos).toBeNull()
  })
  it('maps tests, equity (dropping undated points) and skipped runs', () => {
    const c = adaptCompare({
      ...base,
      oos: {
        benchmark_run_id: 'A',
        tests: [{ run_id: 'B', benchmark_run_id: 'A', n_rows: 500, n_dates: 40, mse_model: 1.1, mse_benchmark: 1.2, dm_stat: 2.1, dm_p: 0.03, nested: true, cw_stat: 2.5, cw_p: 0.006, verdict: 'beats the benchmark', note: null }],
        equity: [{ run_id: 'B', kind: 'top_minus_bottom_quintile', dates: ['2020-01-31', null, '2020-03-31'], cum_return: [0.01, 0.5, 0.03], sharpe: 0.8, mean_ic: 0.04, n_periods: 3 }],
        skipped: [{ run_id: 'C', reason: 'no oos_eval.parquet' }],
      },
    })
    expect(c.oos?.benchmarkRunId).toBe('A')
    expect(c.oos?.tests[0]).toMatchObject({ runId: 'B', nRows: 500, dmStat: 2.1, nested: true, cwP: 0.006, verdict: 'beats the benchmark', note: null })
    expect(c.oos?.equity[0]).toMatchObject({ dates: ['2020-01-31', '2020-03-31'], cumReturn: [0.01, 0.03], sharpe: 0.8, meanIc: 0.04 })
    expect(c.oos?.skipped).toEqual([{ runId: 'C', reason: 'no oos_eval.parquet' }])
  })
})

describe('Model Registry', () => {
  const row = {
    model_id: 'MR1',
    name: 'value tilt',
    version: 2,
    run_id: 'R2',
    method: 'lightgbm',
    spec_hash: 'abcdef1234567890',
    owner: 'me',
    stage: 'production',
    monitor: 'weekly',
    psi_threshold: 0.25,
    created_at: '2026-11-01T10:00:00Z',
    metrics: { n_obs: 1000, oos_rank_ic: 0.05, adj_r2: 0.1 },
    last_monitor: { drift: true, breaches: ['feature x PSI 0.31 > 0.25'], metric: { kind: 'rank_ic', value: 0.02, baseline: 0.05 }, prediction_psi: 0.12 },
  } as unknown as S['StudioModel']

  it('adapts a list row (trimmed last_monitor)', () => {
    const m = adaptModel(row)
    expect(m).toMatchObject({ modelId: 'MR1', version: 2, stage: 'production', monitor: 'weekly', psiThreshold: 0.25, metricFloor: null, spec: null, versions: [], lastMonitorDetail: null })
    expect(m.lastMonitor).toEqual({ drift: true, breaches: ['feature x PSI 0.31 > 0.25'], metric: { kind: 'rank_ic', value: 0.02, baseline: 0.05 }, predictionPsi: 0.12 })
    expect(modelHeadline(m)).toEqual({ key: 'oos_rank_ic', value: 0.05 })
  })
  it('adapts a detail with versions and the full stored monitor result', () => {
    const m = adaptModel({
      ...row,
      spec: { method: 'lightgbm' },
      versions: [row, { ...row, model_id: 'MR0', version: 1, stage: 'candidate', last_monitor: null }],
      last_monitor: { run_id: 'R2', feature_psi: [{ feature: 'x', psi: 0.31, n_current: 50 }], prediction_psi: 0.05, expanding: [], breaches: ['b'], drift: true, metric: { kind: 'rank_ic', value: 0.02 } },
    } as unknown as S['StudioModel'])
    expect(m.versions.map((v) => v.version)).toEqual([2, 1])
    expect(m.versions[1].lastMonitor).toBeNull()
    expect(m.lastMonitorDetail).toMatchObject({ modelId: 'MR1', runId: 'R2', drift: true, featurePsi: [{ feature: 'x', psi: 0.31, status: 'drift', nCurrent: 50 }] })
  })
  it('PSI status thresholds (server status wins)', () => {
    expect([psiStatus(0.05), psiStatus(0.1), psiStatus(0.24), psiStatus(0.25), psiStatus(null)]).toEqual(['stable', 'watch', 'watch', 'drift', null])
    expect(psiStatus(0.5, 'stable')).toBe('stable')
  })
  it('adapts a monitor result (expanding IC / R2, PSI sorted desc)', () => {
    const r = adaptMonitor({
      model_id: 'MR1',
      run_id: 'R2',
      current_window: { start: '2025-01-31', end: '2026-10-31', after_baseline_end: true },
      feature_psi: [
        { feature: 'a', psi: 0.02, status: 'stable', n_current: 10 },
        { feature: 'b', psi: 0.18, status: 'watch', n_current: 10 },
        { feature: 'c', psi: null, status: null },
      ],
      prediction_psi: 0.3,
      metric: { kind: 'r2_oos', value: -0.01, baseline: 0.02 },
      expanding: [{ date: '2025-06-30', expanding_r2_oos: 0.01, expanding_rmse: 0.2 }, { nodate: 1 }],
      breaches: ['prediction PSI 0.30 > 0.25'],
      drift: true,
      psi_threshold: 0.25,
      metric_floor: 0,
    } as unknown as S['StudioModelMonitor'])
    expect(r.featurePsi.map((f) => f.feature)).toEqual(['b', 'a', 'c'])
    expect(r.expanding).toEqual([{ date: '2025-06-30', ic: null, expandingIc: null, expandingIcT: null, expandingR2Oos: 0.01, expandingRmse: 0.2 }])
    expect(r).toMatchObject({ drift: true, predictionPsi: 0.3, metricFloor: 0, currentWindow: { start: '2025-01-31', afterBaselineEnd: true }, metric: { kind: 'r2_oos', value: -0.01 } })
  })
})

describe('recipes (presets v1.50)', () => {
  const p = (id: string, extra: Partial<S['StudioPreset']> = {}) => adaptPreset({ id, title: `T ${id}`, description: 'd', method: 'ols', tags: [], study, ...extra })
  it('maps the recipe fields with safe defaults', () => {
    expect(p('a')).toMatchObject({ question: null, methodNote: null, expectedOutputs: [], runtimeS: null, validated: null })
    expect(p('b', { question: 'factors', method_note: 'FM with NW', expected_outputs: ['premia'], runtime_s: 12.3, validated: '2026-10' })).toMatchObject({
      question: 'factors',
      methodNote: 'FM with NW',
      expectedOutputs: ['premia'],
      runtimeS: 12.3,
    })
  })
  it('filters by question group and searches the method note', () => {
    const all = [p('a', { question: 'factors', method_note: 'Newey-West premia' }), p('b', { question: 'macro' }), p('c')]
    expect(filterPresets(all, 'factors', '').map((x) => x.id)).toEqual(['a'])
    expect(filterPresets(all, 'other', '').map((x) => x.id)).toEqual(['c'])
    expect(filterPresets(all, '', 'newey').map((x) => x.id)).toEqual(['a'])
    expect(filterPresets(all, '', '').length).toBe(3)
  })
})

describe('probabilistic family', () => {
  it('has a label', () => {
    expect(FAMILY_LABEL.probabilistic).toMatch(/probabilistic/i)
  })
  it('routes PIT / calibration / fan / forest / trace and its tables to the method tab', () => {
    const r = {
      method: 'hier_pool',
      plots: {
        pit: { kind: 'bar', title: 'PIT', series: [] },
        calibration: { kind: 'scatter', title: 'cal', series: [] },
        forest: { kind: 'errorbar', title: 'f', series: [] },
        trace: { kind: 'line', title: 't', series: [] },
        slice_tech: { kind: 'ts_band', title: 's', series: [] },
      },
      tables: ['pooling', 'convergence', 'prob_scores'].map((id) => ({ id, title: id, columns: [], rows: [] })),
      coefficients: [],
      tests: [],
      clusters: null,
      forecast: null,
      evaluation: null,
    } as unknown as StudioResultView
    const a = assignOutputs(r)
    expect(a.special?.label).toBe('Partial pooling')
    expect(a.special?.plots.map(([k]) => k).sort()).toEqual(['calibration', 'forest', 'pit', 'slice_tech', 'trace'])
    expect(a.special?.tables.map((t) => t.id)).toEqual(['pooling', 'convergence', 'prob_scores'])
    expect(a.diagPlots).toEqual([])
  })
  it('renders a nullable conformal_alpha nested in params.cv as a nullable number', () => {
    const fields = paramFields({
      properties: {
        cv: { $ref: '#/$defs/CV' },
      },
      $defs: {
        CV: {
          type: 'object',
          properties: {
            folds: { type: 'integer', default: 5, minimum: 2 },
            conformal_alpha: { anyOf: [{ type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 }, { type: 'null' }], default: null, title: 'Conformal Alpha' },
          },
        },
      },
    })
    const cv = fields.find((f) => f.key === 'cv')
    expect(cv?.kind).toBe('object')
    expect(cv?.children.find((c) => c.key === 'conformal_alpha')).toMatchObject({ kind: 'number', nullable: true, defaultValue: null, exclusiveMin: 0, exclusiveMax: 1 })
  })
})
