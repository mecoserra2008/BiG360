import { describe, expect, it } from 'vitest'
import {
  adaptCompare,
  adaptMethod,
  adaptRun,
  changedParams,
  defaultParams,
  draftIssues,
  draftToSpec,
  netLag,
  paramFields,
  roleDrivenParams,
  rolesFor,
  specToDraft,
  switchMethod,
  testVerdict,
  validateParam,
  variableName,
  withNetLag,
} from '../adapters'
import { clusterCount, cutLinkage, heightForK, linkClusters } from '../dendrogram'
import * as fx from './fixtures'

const dl = adaptMethod(fx.dlMethod)
const km = adaptMethod(fx.kmeansMethod)

describe('params schema → form fields', () => {
  const f = Object.fromEntries(paramFields(fx.dlMethod.params_schema).map((x) => [x.key, x]))
  it('maps primitive, enum, nullable and tuple shapes', () => {
    expect(f.add_constant.kind).toBe('boolean')
    expect(f.cov_type).toMatchObject({ kind: 'enum', options: ['nonrobust', 'HC1', 'HAC', 'cluster'], defaultValue: 'HAC' })
    expect(f.hac_maxlags).toMatchObject({ kind: 'integer', nullable: true, min: 0, max: 60, defaultValue: null })
    expect(f.alpha).toMatchObject({ kind: 'number', exclusiveMin: 0, exclusiveMax: 0.5 })
    expect(f.order).toMatchObject({ kind: 'int_tuple', length: 3, defaultValue: [1, 0, 0] })
    expect(f.quantiles.kind).toBe('number_list')
    expect(f.lag_overrides.kind).toBe('int_map')
    expect(f.trend).toMatchObject({ kind: 'enum', nullable: true, options: ['n', 'c', 't', 'ct'] })
    expect(f.exog_values.kind).toBe('json')
  })
  it('validates against bounds', () => {
    expect(validateParam(f.lags, 3)).toBeNull()
    expect(validateParam(f.lags, 40)).toBe('≤ 36')
    expect(validateParam(f.lags, 2.5)).toBe('must be an integer')
    expect(validateParam(f.alpha, 0.5)).toBe('< 0.5')
    expect(validateParam(f.hac_maxlags, null)).toBeNull()
    expect(validateParam(f.lags, null)).toBe('required')
    expect(validateParam(f.order, [1, 2])).toBe('3 integers')
  })
  it('sends only non-default params', () => {
    const d = defaultParams(dl.fields)
    expect(changedParams({ ...d, lags: 6 }, dl.fields)).toEqual({ lags: 6 })
    expect(changedParams({ order: [1, 0, 0], unknown_key: 1 }, dl.fields)).toEqual({ unknown_key: 1 })
  })
})

describe('test verdicts', () => {
  const run = adaptRun(fx.runPayload)
  const v = Object.fromEntries((run.result?.tests ?? []).map((t) => [t.name, t.verdict]))
  it('uses the server pass/fail reading (v1.6 passed)', () => {
    expect(v['Breusch-Pagan']).toBe('pass')
    expect(v.White).toBe('fail')
    expect(v['Durbin-Watson']).toBe('pass')
    expect(v['Pearson dispersion (chi2/df)']).toBe('fail')
  })
  it('keeps [df1, df2] for F tests', () => {
    const r = adaptRun(fx.runPayload)
    expect(r.result?.tests.find((t) => t.group === 'lag_blocks')?.df).toEqual([4, 249])
  })
  it('tests without a pass/fail reading are significance tests', () => {
    expect(v['d_10y lags (joint F)']).toBe('not_significant')
    expect(v['ADF ip (diff 0)']).toBe('significant')
    expect(testVerdict({ name: 'x', group: 'other', p_value: 0.01 })).toBe('significant')
    expect(testVerdict({ name: 'x', group: 'other' })).toBe('info')
  })
})

describe('run / compare adapters', () => {
  it('adapts a run envelope and drops non-finite metrics', () => {
    const r = adaptRun(fx.runPayload)
    expect(r.status).toMatchObject({ runId: 'SR1', status: 'completed', nObs: 258, durationS: 0.085 })
    expect(r.status.metrics).toEqual({ r2: 0.05, aic: -735.3, bad: null, label: 'x' })
    expect(r.dataset?.coverage[0]).toMatchObject({ column: 'fins_ret', pctNonNull: 1, usesFuture: false, firstDate: null })
    expect(r.result?.tests).toHaveLength(6)
    expect(r.result?.forecast).toBeNull()
    expect(r.reproducibility?.spec_hash).toBe('abc')
  })
  it('adapts compare rows', () => {
    const c = adaptCompare(fx.comparePayload)
    expect(c.runs.map((r) => r.runId)).toEqual(['SR1', 'SR2'])
    expect(c.coefficients[0].values.SR1).toEqual({ estimate: 0.06, stdError: 0.04, pValue: 0.17 })
    expect(c.coefficients[0].values.SR2).toBeNull()
  })
})

describe('builder draft ↔ StudySpec', () => {
  it('round-trips a preset (roles, transforms, untouched keys)', () => {
    const d = specToDraft(fx.dlPreset.study, { needsY: true })
    expect(d.variables.map((v) => [v.name, v.role])).toEqual([
      ['fins_ret', 'y'],
      ['d_10y', 'x'],
      ['d_slope', 'x'],
    ])
    expect(d.variables[2].extra).toEqual({ max_stale_days: 10 })
    expect(d.datasetExtra).toMatchObject({ lookback: 0, missing: { ffill_limit: 0, fill: 'none', drop: 'any' } })
    const s = draftToSpec(d)
    expect(s.y).toBe('fins_ret')
    expect(s.x).toEqual(['d_10y', 'd_slope'])
    expect(s.params).toEqual({ lags: 3, cov_type: 'HAC' })
    expect(s.dataset.variables[0].entities).toEqual({ universe: 'mvp_us', sectors: ['FINS'], primary_only: true })
    expect(s.dataset.variables[2]).toMatchObject({ max_stale_days: 10, transforms: [{ op: 'diff' }] })
    expect(s.dataset.entities).toBeUndefined()
    expect(s.dataset.start).toBe('2005-01-01')
  })
  it('maps x to features for unsupervised methods and keeps the universe filter', () => {
    const d = specToDraft(fx.kmeansSpec, { needsY: false })
    expect(d.variables.every((v) => v.role === 'feature')).toBe(true)
    const s = draftToSpec(d)
    expect(s.x).toEqual(['roic', 'op_margin'])
    expect(s.y).toBeUndefined()
    expect(s.dataset.entities).toEqual({ universe: 'mvp_us', primary_only: true })
  })
  it('switching method remaps roles, shape and params', () => {
    const d = specToDraft(fx.dlPreset.study, { needsY: true })
    const k = switchMethod(d, km)
    expect(k.variables.map((v) => v.role)).toEqual(['feature', 'feature', 'feature'])
    expect(k.params).toEqual({})
    const back = switchMethod(k, dl)
    expect(back.variables.map((v) => v.role)).toEqual(['y', 'x', 'x'])
    expect(rolesFor(km)).toEqual(['feature', 'none'])
    expect(rolesFor(dl)).toEqual(['y', 'x', 'none'])
  })
  it('lag/lead spinner keeps a single net step', () => {
    const t = [{ op: 'diff' as const }, { op: 'lag' as const, k: 2 }, { op: 'lead' as const, k: 1 }]
    expect(netLag(t)).toBe(1)
    expect(withNetLag(t, 3)).toEqual([{ op: 'diff' }, { op: 'lag', k: 3 }])
    expect(withNetLag(t, -2)).toEqual([{ op: 'diff' }, { op: 'lead', k: 2 }])
    expect(withNetLag(t, 0)).toEqual([{ op: 'diff' }])
  })
  it('names variables uniquely', () => {
    expect(variableName('fred:DGS10', [])).toBe('dgs10')
    expect(variableName('fred:DGS10', ['dgs10'])).toBe('dgs10_2')
    expect(variableName('ecb:HICP.M.U2', [])).toBe('hicp_m_u2')
    expect(variableName('10y', [])).toBe('v_10y')
  })
  it('flags builder problems before submit', () => {
    const d = specToDraft(fx.dlPreset.study, { needsY: true })
    expect(draftIssues(d, dl)).toEqual([])
    expect(draftIssues(d, null)).toEqual(['Pick a method'])
    const noY = { ...d, variables: d.variables.map((v) => ({ ...v, role: v.role === 'y' ? ('x' as const) : v.role })) }
    expect(draftIssues(noY, dl)).toContain('Distributed lag needs exactly one target (y)')
    const leak = { ...d, variables: d.variables.map((v) => (v.name === 'd_10y' ? { ...v, transforms: [{ op: 'lead' as const, k: 1 }] } : v)) }
    expect(draftIssues(leak, dl).some((i) => i.includes('look-ahead'))).toBe(true)
    expect(draftIssues({ ...d, kind: 'cross_section' }, dl).some((i) => i.includes('does not support'))).toBe(true)
  })
})

describe('v1.6 params schemas and IV roles', () => {
  const f = Object.fromEntries(paramFields(fx.didSchema).map((x) => [x.key, x]))
  it('resolves $ref / $defs, dates, string lists and integer enums', () => {
    expect(f.treatment).toMatchObject({ kind: 'treatment', required: true, title: 'Treatment' })
    expect(f.events).toMatchObject({ kind: 'events', defaultValue: [] })
    expect(f.treatment_date.kind).toBe('date')
    expect(f.donors).toMatchObject({ kind: 'string_list', defaultValue: [] })
    expect(f.det_order).toMatchObject({ kind: 'enum', options: ['-1', '0', '1'], enumValues: [-1, 0, 1] })
    expect(validateParam(f.treatment, null)).toBe('required')
    expect(validateParam(f.donors, [])).toBeNull()
  })
  it('maps endog / instrument roles to params and back', () => {
    const iv = adaptMethod(fx.ivMethod)
    expect(rolesFor(iv)).toEqual(['y', 'x', 'endog', 'instrument', 'none'])
    expect(roleDrivenParams(iv)).toEqual(new Set(['endog', 'instruments']))
    const spec = { ...fx.dlPreset.study, method: 'iv2sls', x: [], params: { endog: ['d_10y'], instruments: ['d_slope'] } }
    const d = specToDraft(spec, { needsY: true })
    expect(d.variables.map((v) => v.role)).toEqual(['y', 'endog', 'instrument'])
    const back = draftToSpec(d)
    expect(back.x).toEqual([])
    expect(back.params).toEqual({ endog: ['d_10y'], instruments: ['d_slope'] })
    expect(draftIssues(d, iv)).toEqual([])
    const noInstr = { ...d, variables: d.variables.map((v) => (v.role === 'instrument' ? { ...v, role: 'none' as const } : v)) }
    expect(draftIssues(noInstr, iv)).toContain('IV needs at least as many instruments as endogenous regressors')
    expect(switchMethod(d, dl).variables.map((v) => v.role)).toEqual(['y', 'x', 'x'])
  })
})

describe('v1.7 schemas: nested cv object and leaderboard candidates', () => {
  const schema = {
    type: 'object',
    $defs: {
      CVParams: { type: 'object', properties: { folds: { default: 5, minimum: 2, type: 'integer' }, scheme: { default: 'auto', enum: ['auto', 'expanding', 'purged_kfold', 'kfold'], type: 'string' } } },
      Candidate: { type: 'object', properties: { name: { type: 'string' }, method: { enum: ['mean', 'ols'], type: 'string' } }, required: ['name', 'method'] },
    },
    properties: {
      cv: { $ref: '#/$defs/CVParams' },
      candidates: { items: { $ref: '#/$defs/Candidate' }, minItems: 2, type: 'array', title: 'Candidates' },
    },
  }
  const f = Object.fromEntries(paramFields(schema).map((x) => [x.key, x]))
  it('expands $ref objects into child fields', () => {
    expect(f.cv.kind).toBe('object')
    expect(f.cv.children.map((c) => [c.key, c.kind, c.defaultValue])).toEqual([
      ['folds', 'integer', 5],
      ['scheme', 'enum', 'auto'],
    ])
  })
  it('recognises leaderboard candidates', () => {
    expect(f.candidates.kind).toBe('candidates')
  })
})

describe('dendrogram cut', () => {
  // 4 points: (0,1) merge at 1, (2,3) at 2, then all at 5
  const Z = [
    [0, 1, 1, 2],
    [2, 3, 2, 2],
    [4, 5, 5, 4],
  ]
  it('cuts like fcluster(criterion="distance")', () => {
    expect(clusterCount(cutLinkage(Z, 0.5))).toBe(4)
    expect(cutLinkage(Z, 1.5)).toEqual([0, 0, 1, 2])
    expect(clusterCount(cutLinkage(Z, 3))).toBe(2)
    expect(clusterCount(cutLinkage(Z, 6))).toBe(1)
  })
  it('finds a height for k clusters', () => {
    expect(clusterCount(cutLinkage(Z, heightForK(Z, 2) as number))).toBe(2)
    expect(clusterCount(cutLinkage(Z, heightForK(Z, 3) as number))).toBe(3)
    expect(heightForK(Z, 9)).toBeNull()
  })
  it('colours links below the cut by cluster', () => {
    const labels = cutLinkage(Z, 3)
    // leaves drawn in order 0,1,2,3 at x = 5, 15, 25, 35
    const ic = [
      [5, 5, 15, 15],
      [25, 25, 35, 35],
      [10, 10, 30, 30],
    ]
    const dc = [
      [0, 1, 1, 0],
      [0, 2, 2, 0],
      [1, 5, 5, 2],
    ]
    expect(linkClusters(ic, dc, [0, 1, 2, 3], labels, 3)).toEqual([0, 1, -1])
  })
})
