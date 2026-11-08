import type { AvailabilityReason } from '@/lib/availability'
import type { CScreenerColumnId, CScreenerQuery, CScreenerResult, CScreenerRow, CSecurityRef, CSectorDetail, CSectorSummary, CTaxonomy } from '../contract'
import type { FactorWeights, KpiDistribution, RegionFilter, ScreenerPayload, ScreenerRow, SearchResult, SectorDetail, SectorNode } from '../types'
import { type AdapterCtx, KPI_SET_TO_PROFILE, dqFromScore, factorMap, moatFromContract, num, numOrNull, regionsToContract, summaryFromSecurity, unitFromContract, weightsToContract } from './common'

/** Model-based macro screener fields (macro-w4). */
export const MACRO_COLUMNS = ['macro_beta_rates', 'macro_beta_usd', 'macro_beta_oil', 'macro_beta_credit', 'macro_beta_growth_surprise', 'macro_impact_rates_down_100bp'] as const satisfies readonly CScreenerColumnId[]

/**
 * Extra screener field ids requested via ScreenerQuery.columns and read back from row.values.
 * The contract does not pin field ids (they come from GET /screener/fields) — these are the ids the UI expects.
 */
export const SCREENER_COLUMNS = ['isin', 'ret_12m', 'pe_ntm', 'ev_ebit', 'fcf_yield', 'roic', 'revenue_growth', 'net_debt_ebitda', 'dividend_yield', ...MACRO_COLUMNS] as const satisfies readonly CScreenerColumnId[]

export interface ScreenerOptions {
  weights?: FactorWeights
  sectorIds?: string[]
  countries?: string[]
  minScore?: number
  limit?: number
}

/** UI screener request → contract ScreenerQuery body (POST /screener/query). */
export function buildScreenerQuery(region: RegionFilter, opts: ScreenerOptions = {}): CScreenerQuery {
  const q: CScreenerQuery = {
    universe_id: 'full',
    columns: [...SCREENER_COLUMNS],
    sort_by: 'composite',
    sort_dir: 'desc',
    limit: opts.limit ?? 1000,
    offset: 0,
  }
  const regions = regionsToContract(region)
  if (regions.length) q.regions = regions
  if (opts.weights) q.factor_weights = weightsToContract(opts.weights)
  if (opts.sectorIds?.length) q.sector_ids = opts.sectorIds
  if (opts.countries?.length) q.countries = opts.countries
  if (typeof opts.minScore === 'number' && opts.minScore > 0) q.filters = [{ field: 'composite', op: 'gte', value: opts.minScore }]
  return q
}

export function adaptScreenerRow(r: CScreenerRow, ctx: AdapterCtx, fallbackRank: number): ScreenerRow {
  const v = (k: string) => numOrNull(r.values?.[k])
  const isin = typeof r.values?.isin === 'string' ? (r.values.isin as string) : ''
  const base = summaryFromSecurity(r.security, ctx, {
    isin,
    price: num(r.price),
    change1dPct: num(r.chg_1d),
    compositeScore: num(r.composite),
    factorScores: factorMap(r.factor_scores),
    moat: moatFromContract(r.moat_class),
    dataQuality: dqFromScore(r.dq_score),
  })
  return {
    ...base,
    rank: r.rank ?? fallbackRank,
    grade: r.grade ?? null,
    return1m: num(r.chg_1m),
    return12m: num(v('ret_12m')),
    peNtm: v('pe_ntm'),
    evEbit: v('ev_ebit'),
    fcfYield: v('fcf_yield'),
    roic: v('roic'),
    revenueGrowth: v('revenue_growth'),
    netDebtEbitda: v('net_debt_ebitda'),
    dividendYield: v('dividend_yield'),
    modelExpectedExcess: num(r.ml_expected_excess_3m ?? v('ml_expected_excess_3m')),
    mxBetaRates: v('macro_beta_rates'),
    mxBetaUsd: v('macro_beta_usd'),
    mxBetaOil: v('macro_beta_oil'),
    mxBetaCredit: v('macro_beta_credit'),
    mxBetaGrowthSurprise: v('macro_beta_growth_surprise'),
    mxImpactRatesDown100: v('macro_impact_rates_down_100bp'),
    scoreScope: (r.score_scope as 'mvp' | 'extended' | null | undefined) ?? null,
    scoreReason: (r.score_reason as AvailabilityReason | null | undefined) ?? null,
  }
}

/** POST /screener/query → UI ScreenerPayload. */
export function adaptScreener(res: CScreenerResult, ctx: AdapterCtx): ScreenerPayload {
  const w = factorMap(res.weights_used, 0)
  const hasWeights = Object.values(w).some((x) => x > 0)
  return {
    dataStatus: res.data_status,
    asOf: res.as_of,
    rows: res.rows.map((r, i) => adaptScreenerRow(r, ctx, res.offset + i + 1)),
    // weights_used are fractions; UI sliders work in integer points
    defaultWeights: hasWeights ? (Object.fromEntries(Object.entries(w).map(([k, x]) => [k, Math.round(x * 100)])) as FactorWeights) : ctx.weights,
    compositeValidated: res.composite_label === 'validated',
  }
}

/** GET /search → UI SearchResult[]. */
export function adaptSearch(rows: CSecurityRef[], ctx: AdapterCtx): SearchResult[] {
  return rows.map((s) => {
    const sum = summaryFromSecurity(s, ctx)
    return { id: sum.id, ticker: sum.ticker, name: sum.name, isin: '', exchange: sum.exchange, country: sum.country, sector: sum.sector, sectorId: s.sector_id ?? null }
  })
}

/* ───────────── sectors ───────────── */

/** GET /meta/taxonomy (+ GET /sectors for counts) → UI SectorNode[]. */
export function adaptSectorTree(tax: CTaxonomy, summaries: CSectorSummary[] | null): SectorNode[] {
  const count = new Map<string, number>()
  for (const s of summaries ?? []) {
    count.set(s.sector_id, s.n_securities)
    for (const sub of s.subsectors ?? []) if (sub.subsector_id) count.set(`${s.sector_id}/${sub.subsector_id}`, sub.n_securities)
  }
  return tax.sectors.map((s) => ({
    id: s.id,
    name: s.name,
    companyCount: summaries ? (count.get(s.id) ?? 0) : NaN,
    subsectors: s.subsectors.map((sub) => ({
      id: sub.id,
      name: sub.name,
      kpiProfile: KPI_SET_TO_PROFILE[sub.kpi_set_id ?? ''] ?? 'general',
      companyCount: summaries ? (count.get(`${s.id}/${sub.id}`) ?? 0) : NaN,
    })),
  }))
}

export function adaptKpiDistribution(d: CSectorDetail['kpi_distributions'][number]): KpiDistribution {
  return {
    kpi: { id: d.metric_id, label: d.label, unit: unitFromContract(d.unit), higherIsBetter: d.higher_is_better ?? (typeof d.target === 'number' ? null : true), target: d.target ?? null, description: `${d.label} · n=${d.n}` },
    // v1.2 publishes per-company values; older payloads → quantile strip
    values: (d.values ?? []).filter((x) => Number.isFinite(x.value)).map((x) => ({ companyId: x.security_id, ticker: x.ticker, value: x.value })),
    p10: num(d.p10),
    p25: num(d.p25),
    median: num(d.p50),
    p75: num(d.p75),
    p90: num(d.p90),
  }
}

/** GET /sectors/{sector_id} → UI SectorDetail. */
export function adaptSectorDetail(d: CSectorDetail, ctx: AdapterCtx, subsectorId: string | null, asOf: string): SectorDetail {
  const s = d.summary
  const profile = subsectorId ? ctx.kpiProfileOfSubsector(subsectorId) : (KPI_SET_TO_PROFILE[d.kpi_set_ids?.[0] ?? ''] ?? 'general')
  const distributions = d.kpi_distributions.map(adaptKpiDistribution)
  return {
    dataStatus: d.data_status,
    asOf,
    sector: ctx.sectorName(s.sector_id),
    subsector: subsectorId ? ctx.subsectorName(subsectorId) : null,
    kpiProfile: profile,
    kpis: distributions.map((x) => x.kpi),
    distributions,
    stats: {
      companyCount: s.n_securities,
      medianScore: num(s.median_composite ?? s.median_factor_scores?.composite),
      totalMarketCapUsd: num(s.market_cap_usd),
      medianReturn1m: num(s.ret_1m),
    },
    leaders: d.leaders.map((r, i) => adaptScreenerRow(r, ctx, i + 1)),
    laggards: d.laggards.map((r, i) => adaptScreenerRow(r, ctx, i + 1)),
  }
}
