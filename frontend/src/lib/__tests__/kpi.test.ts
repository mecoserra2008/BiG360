import { describe, expect, it } from 'vitest'
import { adaptKpiDistribution } from '@/api/adapters/screener'
import { kpiDirectionLabel } from '../kpi'

describe('KPI direction labels (guide 03)', () => {
  it('labels band metrics by their target, not "Higher is better"', () => {
    const d = adaptKpiDistribution({ metric_id: 'loans_to_deposits', label: 'Loans / deposits', unit: 'pct', higher_is_better: null, target: 0.85, n: 44 })
    expect(d.kpi.higherIsBetter).toBeNull()
    expect(kpiDirectionLabel(d.kpi)).toBe('Closest to 85% is best')
    expect(kpiDirectionLabel({ higherIsBetter: true, unit: 'pct' })).toBe('Higher is better')
    expect(kpiDirectionLabel({ higherIsBetter: false, unit: 'pct' })).toBe('Lower is better')
    // older payloads without target keep the previous default
    expect(adaptKpiDistribution({ metric_id: 'roe', label: 'ROE', unit: 'pct', n: 3 }).kpi.higherIsBetter).toBe(true)
  })
})
