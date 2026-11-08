import { describe, expect, it } from 'vitest'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, missingFactors, rankByWeights, weightedComposite } from '@/lib/factors'
import type { FactorFamily } from '@/api/types'
import { qk } from '@/api/hooks'

const full = (v: number) => Object.fromEntries(FACTOR_FAMILIES.map((f) => [f, v])) as Record<FactorFamily, number>

describe('Screener composite with missing factors (guide 04: 752 of 1,000 rows shown)', () => {
  it('renormalises the weights over the factors that have a score, like the server', () => {
    const s = { ...full(60), growth: Number.NaN, moat: Number.NaN }
    const w = DEFAULT_WEIGHTS
    const exp = FACTOR_FAMILIES.filter((f) => f !== 'growth' && f !== 'moat').reduce((a, f) => a + 60 * w[f], 0) /
      FACTOR_FAMILIES.filter((f) => f !== 'growth' && f !== 'moat').reduce((a, f) => a + w[f], 0)
    expect(weightedComposite(s, w)).toBeCloseTo(exp, 10)
    expect(weightedComposite(s, w)).toBeCloseTo(60, 10)
    expect(missingFactors(s)).toEqual(['growth', 'moat'])
    expect(Number.isNaN(weightedComposite(full(Number.NaN), w))).toBe(true)
    // fewer than half of the requested weight available: no composite (server: composite_sql min_frac 0.5)
    const sparse = { ...full(Number.NaN), quality: 80, value: 70 }
    expect(Number.isNaN(weightedComposite(sparse, w))).toBe(true)
  })

  it('keeps incomplete rows in the ranking (flagged), rows without any score last', () => {
    const rows = [
      { id: 'a', factorScores: full(50) },
      { id: 'b', factorScores: { ...full(90), growth: Number.NaN } },
      { id: 'c', factorScores: full(Number.NaN) },
      { id: 'd', factorScores: full(70) },
    ]
    const r = rankByWeights(rows, DEFAULT_WEIGHTS)
    expect(r.map((x) => x.id)).toEqual(['b', 'd', 'a', 'c'])
    expect(r.map((x) => x.liveRank)).toEqual([1, 2, 3, 4])
    expect(r[0].missing).toEqual(['growth'])
    expect(r[0].score).toBeCloseTo(90, 10)
    expect(r.filter((x) => x.missing.length > 0)).toHaveLength(2)
  })

  it('re-ranks when the weights change', () => {
    const rows = [
      { id: 'q', factorScores: { ...full(10), quality: 100 } },
      { id: 'v', factorScores: { ...full(10), value: 100 } },
    ]
    expect(rankByWeights(rows, { ...DEFAULT_WEIGHTS, quality: 30, value: 0 })[0].id).toBe('q')
    expect(rankByWeights(rows, { ...DEFAULT_WEIGHTS, quality: 0, value: 30 })[0].id).toBe('v')
  })
})

describe('Screener refetch on weight change (guide 04)', () => {
  it('keys the screener query on the weights so a change refetches the server top 1,000', () => {
    const a = qk.screener('ALL', JSON.stringify(DEFAULT_WEIGHTS))
    const b = qk.screener('ALL', JSON.stringify({ ...DEFAULT_WEIGHTS, value: 30 }))
    expect(a).not.toEqual(b)
    expect(qk.screener('ALL')).toEqual(['screener', 'ALL'])
  })
})
