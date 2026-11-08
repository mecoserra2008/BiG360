/** Studio v2 S2 methods: params schemas map to real form controls (no JSON fallbacks) and outputs route to their tab. */
import { describe, expect, it } from 'vitest'
import { assignOutputs } from '@/pages/studio/results/assign'
import type { StudioResultView } from '@/api/studio/types'
import { adaptMethod, paramFields } from '../adapters'
import s2 from './s2_methods.json'

const methods = s2 as unknown as Record<string, { params_schema: Record<string, unknown>; family: string }>

describe('S2 params schemas → form fields', () => {
  it('every S2 method adapts and no field falls back to raw JSON', () => {
    for (const [id, m] of Object.entries(methods)) {
      const fields = paramFields(m.params_schema)
      expect(fields.length, id).toBeGreaterThan(0)
      expect(fields.filter((f) => f.kind === 'json').map((f) => `${id}.${f.key}`)).toEqual([])
      expect(adaptMethod(m as never).id).toBe(id)
    }
  })
  it('maps treatment columns, nested CV, base lists and layer sizes', () => {
    const f = (id: string) => Object.fromEntries(paramFields(methods[id].params_schema).map((x) => [x.key, x]))
    expect(f('dml').treatment).toMatchObject({ kind: 'string', required: true })
    expect(f('dml').crossfit.kind).toBe('object')
    expect(f('dml').learner_y).toMatchObject({ kind: 'enum', options: ['lasso', 'rf', 'xgb', 'lgbm'] })
    expect(f('stacking').bases.kind).toBe('string_list')
    expect(f('mlp').hidden.kind).toBe('number_list')
    expect(f('rdd').h).toMatchObject({ kind: 'number', nullable: true })
    expect(methods.mlp.family).toBe('neural')
  })
})

describe('S2 result tabs', () => {
  it('routes method outputs to the special tab', () => {
    const r = {
      method: 'rdd',
      plots: { rd_plot: { kind: 'scatter', title: 'RD', series: [] }, bandwidth_sensitivity: { kind: 'errorbar', title: 'h', series: [] } },
      tables: [{ id: 'placebo', title: 'p', columns: [], rows: [] }],
      coefficients: [],
      tests: [],
      clusters: null,
      forecast: null,
      evaluation: null,
    } as unknown as StudioResultView
    const a = assignOutputs(r)
    expect(a.special?.label).toBe('Discontinuity')
    expect(a.special?.plots.map(([k]) => k).sort()).toEqual(['bandwidth_sensitivity', 'rd_plot'])
    expect(a.special?.tables.map((t) => t.id)).toEqual(['placebo'])
  })
})
