/**
 * Macro v3 (central banks) and v4 (macro → markets) contract aliases and pure helpers
 * (unit-tested in __tests__/v34.test.ts).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type CbSummary = S['MacroV3CentralBanks']['banks'][number]
export type CbOverview = S['MacroV3CentralBanks']
export type CbDetail = S['MacroV3CentralBank']
export type CbImplied = S['MacroV3ImpliedPaths']
export type CbImpliedBank = S['MacroV3ImpliedBank']
export type CbPriced = S['MacroV3PricedBrief']
export type CbPathPoint = S['MacroV3PathPoint']
export type CbDecision = S['MacroV3Decision']
export type MxFactors = S['MacroV4Factors']
export type MxExposures = S['MacroV4Exposures']
export type MxExposureItem = S['MacroV4ExposureItem']
export type MxFactorMeta = S['MacroV4FactorMeta']
export type MxBeta = S['MacroV4Beta']
export type MxRegimes = S['MacroV4RegimePerformance']
export type MxRegimeItem = S['MacroV4RegimeItem']
export type MxRegimeScopes = S['MacroV4RegimeScopes']
export type MxRegimeScope = MxRegimeScopes['regime_types'][number]['scopes'][number]
export type CbToneTrend = NonNullable<CbSummary['tone_trend']>
export type MxScenario = S['MacroV4Scenario']
export type MxScenarioList = S['MacroV4ScenarioList']
export type MxShocks = S['MacroV4Shocks']
export type MxRunRequest = S['MacroV4ScenarioRunRequest']
export type MxScenarioResult = S['MacroV4ScenarioResult']
export type MxImpact = S['MacroV4Impact']
export type MxEpisodes = S['MacroV4Episodes']
export type MxEpisode = S['MacroV4EpisodeDetail']
export type MxReplayRow = S['MacroV4ReplayRow']
export type CompanyMacroV4 = S['CompanyMacroV4']
export type CompanyMacro = S['CompanyMacro']

export type ExposureEntity = 'sector' | 'subsector' | 'region' | 'factor' | 'etf' | 'index' | 'future'
export type ExposureModel = 'joint' | 'univariate' | 'surprise'
export type RegimeType = 'cycle_quadrant' | 'hmm_regime' | 'rate_cycle' | 'inflation_regime'

/** Required wording for implied paths (user rule). */
export const IMPLIED_LABEL = 'Approximation from official curves (not futures/OIS-implied probabilities)'

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** Priced change → colour bucket: −2 big cuts … +2 big hikes (|moves| ≥ 1.5 = strong). */
export function pricedBucket(movesPriced: number | null | undefined): number {
  const m = num(movesPriced)
  if (m === null || Math.abs(m) < 0.25) return 0
  return m > 0 ? (m >= 1.5 ? 2 : 1) : m <= -1.5 ? -2 : -1
}

/** "+58bp · +2.3 hikes" style text for a priced change. */
/** Implied-path point as displayed: with `tp` on, the term-premium-adjusted rate and every change derived from it
 *  (vs today, this step, moves priced) - not only the Implied column (guide 02a, Fed page). */
export interface PathPointView {
  implied: number | null
  change_bp: number | null
  moves_priced: number | null
  incremental_bp: number | null
  adjusted: boolean
}

interface PathPointIn {
  point_type: string
  implied_rate?: number | null
  implied_rate_tp_adj?: number | null
  change_bp?: number | null
  moves_priced?: number | null
  incremental_bp?: number | null
}

/** Rate step of the bank (25bp; PBoC 10bp) recovered from change_bp / moves_priced of its points. */
export function inferStep(pts: { change_bp?: number | null; moves_priced?: number | null }[] | null | undefined, fallback = 25): number {
  for (const p of pts ?? []) {
    if (typeof p.change_bp === 'number' && typeof p.moves_priced === 'number' && Math.abs(p.moves_priced) > 0.05) {
      const st = Math.abs(p.change_bp / p.moves_priced)
      if (Number.isFinite(st) && st > 0) return Math.round(st * 2) / 2
    }
  }
  return fallback
}

export function pathView(pts: PathPointIn[], policyRate: number | null | undefined, step: number, tp: boolean): PathPointView[] {
  const pr = typeof policyRate === 'number' && Number.isFinite(policyRate) ? policyRate : null
  let prevMeeting = pr
  return pts.map((p) => {
    const adj = tp && typeof p.implied_rate_tp_adj === 'number' && Number.isFinite(p.implied_rate_tp_adj)
    if (!adj) {
      if (p.point_type === 'meeting' && typeof p.implied_rate === 'number') prevMeeting = p.implied_rate
      return { implied: p.implied_rate ?? null, change_bp: p.change_bp ?? null, moves_priced: p.moves_priced ?? null, incremental_bp: p.incremental_bp ?? null, adjusted: false }
    }
    const v = p.implied_rate_tp_adj as number
    const ch = pr === null ? null : (v - pr) * 100
    const inc = p.point_type === 'meeting' && prevMeeting !== null ? (v - prevMeeting) * 100 : null
    if (p.point_type === 'meeting') prevMeeting = v
    return { implied: v, change_bp: ch, moves_priced: ch === null ? null : ch / step, incremental_bp: inc, adjusted: true }
  })
}

/** Official projection vs market: market - official recomputed on the adjusted market rate when tp is on. */
export function sepView(s: { sep_median?: number | null; market_implied?: number | null; market_implied_tp_adj?: number | null; market_minus_sep_bp?: number | null }, tp: boolean): { market: number | null; minus_bp: number | null } {
  if (tp && typeof s.market_implied_tp_adj === 'number') {
    return { market: s.market_implied_tp_adj, minus_bp: typeof s.sep_median === 'number' ? (s.market_implied_tp_adj - s.sep_median) * 100 : null }
  }
  return { market: s.market_implied ?? null, minus_bp: s.market_minus_sep_bp ?? null }
}

export function pricedText(p: Pick<CbPriced, 'change_bp' | 'moves_priced'> | null | undefined, step = 25): string {
  if (!p) return '—'
  const bp = num(p.change_bp)
  const mv = num(p.moves_priced)
  if (bp === null) return '—'
  const r = Math.round(bp)
  const sign = r > 0 ? '+' : r < 0 ? '−' : ''
  const moves = mv === null ? '' : ` · ${Math.abs(mv).toFixed(1)} ${Math.abs(mv) < 0.05 ? 'moves' : mv > 0 ? 'hikes' : 'cuts'} of ${step}bp`
  return `${sign}${Math.abs(r)}bp${moves}`
}

/** Exposure heatmap matrix: rows × factor ids → beta / t-stat / significance. */
export function exposureMatrix(items: MxExposureItem[], factors: MxFactorMeta[]): { id: string; label: string; weighting: string; r2: number | null; cells: { factor: string; beta: number | null; t: number | null; sig: 0 | 1 | 2 }[] }[] {
  return items.map((it) => ({
    id: it.entity_id,
    label: it.name || it.ticker || it.entity_id,
    weighting: it.weighting,
    r2: num(it.r2),
    cells: factors.map((f) => {
      const b = it.betas[f.factor_id]
      const t = num(b?.t_stat)
      return { factor: f.factor_id, beta: num(b?.beta), t, sig: t === null ? 0 : Math.abs(t) >= 2.58 ? 2 : Math.abs(t) >= 1.96 ? 1 : 0 }
    }),
  }))
}

/** Remove zero / empty shocks (the API treats missing as 0; keeps saved scenarios minimal). */
export function cleanShocks(s: Record<string, number | null | undefined>): MxShocks {
  return Object.fromEntries(Object.entries(s).filter(([, v]) => typeof v === 'number' && Number.isFinite(v) && v !== 0)) as MxShocks
}

/** Model vs realized points with a known realized value (episode replay scatter). */
export function replayPoints(rows: MxReplayRow[]): { label: string; model: number; realized: number }[] {
  return rows
    .filter((r) => num(r.model_implied) !== null && num(r.realized) !== null)
    .map((r) => ({ label: r.ticker || r.name || r.entity_id, model: r.model_implied as number, realized: r.realized as number }))
}

/** Normal-approximation interval for a beta (CI for the company-vs-sector chart). */
export function betaInterval(beta: number | null | undefined, se: number | null | undefined, z = 1.645): [number, number] | null {
  const b = num(beta)
  const s = num(se)
  return b === null || s === null ? null : [b - z * s, b + z * s]
}

/** Regime table rows sorted by the current regime's mean (best first). */
export function regimeRows(items: MxRegimeItem[], current: string | null | undefined): MxRegimeItem[] {
  if (!current) return items
  return [...items].sort((a, b) => (num(b.cells[current]?.mean) ?? -Infinity) - (num(a.cells[current]?.mean) ?? -Infinity))
}

export const REGIME_LABEL: Record<string, string> = {
  expansion: 'Expansion',
  recovery: 'Recovery',
  slowdown: 'Slowdown',
  contraction: 'Contraction',
  hiking: 'Hiking',
  pausing: 'Pausing',
  cutting: 'Cutting',
  below_2: 'CPI < 2%',
  '2_to_3': 'CPI 2–3%',
  '3_to_5': 'CPI 3–5%',
  above_5: 'CPI > 5%',
}

/** Regime picker options from GET /macro/v4/regime-scopes (scopes with performance first, then alphabetical). */
export function scopeOptions(scopes: MxRegimeScope[]): { value: string; label: string; hasPerformance: boolean }[] {
  return [...scopes]
    .sort((a, b) => Number(b.has_performance) - Number(a.has_performance) || a.scope.localeCompare(b.scope))
    .map((s) => ({ value: s.scope, label: `${s.scope.toUpperCase()}${s.current_state ? ` · now ${REGIME_LABEL[s.current_state] ?? s.current_state}` : ''}${s.has_performance ? '' : ' (no performance data)'}`, hasPerformance: s.has_performance }))
}

/** Tone-trend sparkline geometry: points on a fixed −1…+1 scale plus decision markers (hike top, cut bottom). */
export function toneSpark(trend: CbToneTrend, width: number, height: number, pad = 3): { line: string; marks: { x: number; action: 'hike' | 'cut' | 'hold' }[]; zeroY: number } {
  const n = trend.length
  const x = (i: number) => (n <= 1 ? width / 2 : pad + (i * (width - 2 * pad)) / (n - 1))
  const y = (v: number) => pad + ((1 - Math.max(-1, Math.min(1, v))) / 2) * (height - 2 * pad)
  const pts = trend.map((p, i) => (typeof p.tone_model === 'number' && Number.isFinite(p.tone_model) ? `${x(i).toFixed(1)},${y(p.tone_model).toFixed(1)}` : null)).filter((p): p is string => p !== null)
  const marks = trend.flatMap((p, i) => (p.action === 'hike' || p.action === 'cut' || p.action === 'hold' ? [{ x: Number(x(i).toFixed(1)), action: p.action as 'hike' | 'cut' | 'hold' }] : []))
  return { line: pts.join(' '), marks, zeroY: Number(y(0).toFixed(1)) }
}
