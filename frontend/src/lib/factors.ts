import type { FactorFamily, FactorWeights, MoatRating } from '@/api/types'

export const FACTOR_FAMILIES: FactorFamily[] = [
  'quality',
  'profitability',
  'growth',
  'value',
  'balanceSheet',
  'momentum',
  'insider',
  'sentiment',
  'macro',
  'moat',
]

export const FACTOR_LABEL: Record<FactorFamily, string> = {
  quality: 'Quality',
  profitability: 'Profitability',
  growth: 'Growth',
  value: 'Value',
  balanceSheet: 'Balance Sheet',
  momentum: 'Momentum',
  insider: 'Insider',
  sentiment: 'Sentiment',
  macro: 'Macro',
  moat: 'Moat',
}

export const FACTOR_SHORT: Record<FactorFamily, string> = {
  quality: 'QUAL',
  profitability: 'PROF',
  growth: 'GROW',
  value: 'VAL',
  balanceSheet: 'BS',
  momentum: 'MOM',
  insider: 'INS',
  sentiment: 'SENT',
  macro: 'MAC',
  moat: 'MOAT',
}

/** UI-side default weights = the stored balanced profile (contracts/scoring.yaml composite.profiles.balanced), so the
 *  Screener's default composite equals the stored composite shown everywhere else (guide 04). */
export const DEFAULT_WEIGHTS: FactorWeights = {
  quality: 18,
  profitability: 12,
  growth: 12,
  value: 15,
  balanceSheet: 8,
  momentum: 12,
  insider: 5,
  sentiment: 5,
  macro: 5,
  moat: 8,
}

/** Defaults of earlier builds (12/12/11/13/9/11/7/7/8/10): stored weights still equal to them are migrated. */
export const LEGACY_DEFAULT_WEIGHTS: FactorWeights = { quality: 12, profitability: 12, growth: 11, value: 13, balanceSheet: 9, momentum: 11, insider: 7, sentiment: 7, macro: 8, moat: 10 }

/** Weighted mean of the factor percentiles over the factors that have a score, weights renormalised - the same
 *  rule as the server (serving.composite_sql / scoring.yaml composite.custom_weights): NaN when the factors with a
 *  score carry less than half of the requested weight. (A single missing factor used to make the whole composite
 *  NaN and the row disappear: guide 04.) */
export function weightedComposite(scores: Record<FactorFamily, number>, weights: FactorWeights, minFrac = 0.5): number {
  let s = 0
  let w = 0
  let tot = 0
  for (const f of FACTOR_FAMILIES) {
    if (!(weights[f] > 0)) continue
    tot += weights[f]
    const v = scores[f]
    if (!Number.isFinite(v)) continue
    s += v * weights[f]
    w += weights[f]
  }
  return w > 0 && w >= minFrac * tot ? s / w : Number.NaN
}

/** Factor families without a score (shown as an "incomplete" flag in the Screener). */
export function missingFactors(scores: Record<FactorFamily, number>): FactorFamily[] {
  return FACTOR_FAMILIES.filter((f) => !Number.isFinite(scores[f]))
}

/** Live ranking under the given weights: composite (renormalised over available factors), live rank (rows with no
 *  composite at all last) and the missing factors per row. */
export function rankByWeights<T extends { factorScores: Record<FactorFamily, number> }>(rows: T[], weights: FactorWeights): (T & { score: number; liveRank: number; missing: FactorFamily[] })[] {
  const scored = rows.map((r) => ({ ...r, score: weightedComposite(r.factorScores, weights), liveRank: 0, missing: missingFactors(r.factorScores) }))
  scored.sort((a, b) => (Number.isFinite(b.score) ? b.score : -1) - (Number.isFinite(a.score) ? a.score : -1))
  scored.forEach((r, i) => (r.liveRank = i + 1))
  return scored
}

export const MOAT_LABEL: Record<MoatRating, string> = { wide: 'Wide moat', narrow: 'Narrow moat', none: 'No moat', insufficient: 'Insufficient data' }
