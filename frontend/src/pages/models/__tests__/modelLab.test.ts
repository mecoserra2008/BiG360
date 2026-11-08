import { describe, expect, it } from 'vitest'
import { adaptModelLab, adaptModelRun } from '@/api/adapters/crossSection'
import * as fx from '@/api/adapters/__tests__/fixtures'
import { foldTestMonths, monthsBetween } from '../folds'

describe('Model Lab labels match the code (guide 10)', () => {
  it('derives the walk-forward test-fold length from the folds (24 months, not 12)', () => {
    const folds = [
      { testStart: '2010-01-15', testEnd: '2012-01-15' },
      { testStart: '2012-01-15', testEnd: '2014-01-15' },
      { testStart: '2026-04-15', testEnd: '2026-10-09' }, // partial last fold
    ]
    expect(foldTestMonths(folds)).toBe(24)
    expect(monthsBetween('2026-04-15', '2026-10-09')).toBe(6)
    expect(foldTestMonths([])).toBeNull()
  })

  it('flags the Transformer and ensemble runs as demoted (failed leakage gate)', () => {
    const base = fx.modelRuns[0]
    const ft = adaptModelRun({ ...base, model_type: 'ft_transformer', leakage_gate: 'failed', gate_note: 'DEMOTED: x' })
    expect(ft).toMatchObject({ model: 'transformer', leakageGate: 'failed', gateNote: 'DEMOTED: x' })
    // older API without the field: non-XGBoost ranking models default to failed, XGBoost to passed
    expect(adaptModelRun({ ...base, model_type: 'ensemble' }).leakageGate).toBe('failed')
    expect(adaptModelRun({ ...base, model_type: 'xgboost_rank' }).leakageGate).toBe('passed')
    const lab = adaptModelLab(fx.modelRuns, fx.modelDetails)
    expect(lab.xgboostRun?.leakageGate).toBe('passed')
  })
})
