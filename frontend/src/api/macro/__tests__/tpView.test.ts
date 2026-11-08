import { describe, expect, it } from 'vitest'
import { inferStep, pathView, pricedText, sepView } from '../v34'

// Figure 2A.31 numbers: policy 3.875, raw 12m 4.495 (+62bp), TP-adjusted 12m 4.095 (+22bp)
const pts = [
  { point_type: 'meeting', implied_rate: 3.95, implied_rate_tp_adj: 3.9, change_bp: 7.5, moves_priced: 0.3, incremental_bp: 7.5 },
  { point_type: 'meeting', implied_rate: 4.1, implied_rate_tp_adj: 3.95, change_bp: 22.5, moves_priced: 0.9, incremental_bp: 15 },
  { point_type: 'horizon', implied_rate: 4.495, implied_rate_tp_adj: 4.095, change_bp: 62, moves_priced: 2.48, incremental_bp: null },
]

describe('Fed term-premium switch drives every derived chip (guide 02a)', () => {
  it('off: the raw values pass through', () => {
    const v = pathView(pts, 3.875, 25, false)
    expect(v.map((x) => x.change_bp)).toEqual([7.5, 22.5, 62])
    expect(v.every((x) => !x.adjusted)).toBe(true)
  })
  it('on: implied, vs today, this step and moves priced follow the adjusted path', () => {
    const v = pathView(pts, 3.875, 25, true)
    expect(v[2].implied).toBe(4.095)
    expect(v[2].change_bp).toBeCloseTo(22, 6)
    expect(v[2].moves_priced).toBeCloseTo(0.88, 6)
    expect(v[0].incremental_bp).toBeCloseTo(2.5, 6)   // 3.90 - 3.875
    expect(v[1].incremental_bp).toBeCloseTo(5, 6)     // 3.95 - 3.90
    expect(v[2].incremental_bp).toBeNull()            // horizons have no step
    expect(pricedText({ change_bp: v[2].change_bp as number, moves_priced: v[2].moves_priced as number })).toBe('+22bp · 0.9 hikes of 25bp')
  })
  it('falls back to raw when a point has no adjusted rate', () => {
    const v = pathView([{ point_type: 'meeting', implied_rate: 4, implied_rate_tp_adj: null, change_bp: 12.5, moves_priced: 0.5 }], 3.875, 25, true)
    expect(v[0]).toMatchObject({ implied: 4, change_bp: 12.5, adjusted: false })
  })
  it('market minus official uses the adjusted market rate', () => {
    const s = { sep_median: 4.1, market_implied: 4.22, market_implied_tp_adj: 4.139, market_minus_sep_bp: 12 }
    expect(sepView(s, false)).toEqual({ market: 4.22, minus_bp: 12 })
    expect(sepView(s, true).market).toBe(4.139)
    expect(sepView(s, true).minus_bp).toBeCloseTo(3.9, 6)
  })
})

describe('inferStep', () => {
  it('recovers the bank step from the points', () => {
    expect(inferStep(pts)).toBe(25)
    expect(inferStep([{ change_bp: -10, moves_priced: -1 }])).toBe(10)
    expect(inferStep([])).toBe(25)
  })
})
