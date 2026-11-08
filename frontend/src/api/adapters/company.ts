import type { AvailabilityReason } from '@/lib/availability'
import type {
  CCompanyEvents,
  CCompanyInsiders,
  CCompanyOverview,
  CCompanySentiment,
  CCostOfCapital,
  CFinancials,
  CForecast,
  CGdeltEvent,
  CInsiderTransaction,
  CMetricTable,
  CModelExplanation,
  CModelType,
  CMoatDetail,
  CPeerComparison,
  CPriceSeries,
  CTechnicals,
} from '../contract'
import type { components } from '../contract'
import type {
  AlertSeverity,
  BeneishDetail,
  CompanyAlert,
  CompanyFinancials,
  CompanyForecast,
  CompanyInsiders,
  CompanyModel,
  CompanyMoat,
  CompanyOverview,
  CompanyPeers,
  CompanySentiment,
  CompanyTechnicals,
  CostOfCapital,
  ConfluenceZone,
  EventCategory,
  EventItem,
  FactorScoreDetail,
  FinancialLineItem,
  FinancialStatement,
  ForecastSeries,
  InsiderTransaction,
  InsiderTxnCode,
  KeyRatio,
  LevelType,
  ModelKind,
  MoatSource,
  Periodicity,
  PriceBar,
  ProjectedStatementRow,
  Scenario,
  ShapContribution,
  StatementType,
  SwingPivot,
  TradeSetup,
  ValueUnit,
} from '../types'
import { type AdapterCtx, barsFromContract, dqFromScore, factorFromContract, factorMap, moatFromContract, num, snakeToCamel, summaryFromSecurity, unitFromContract } from './common'

const pct100 = (v: number | null | undefined): number => {
  const x = num(v)
  return Number.isFinite(x) && Math.abs(x) <= 1 ? x * 100 : x
}

/* ───────────────────────── overview ───────────────────────── */

const BADGE: Record<string, { severity: AlertSeverity; category: CompanyAlert['category']; message: string }> = {
  cluster_buy: { severity: 'warning', category: 'insider', message: 'Insider cluster buy detected' },
  distress_zone: { severity: 'critical', category: 'fundamental', message: 'Altman Z-score in the distress zone' },
  // v1.29: the model-based (recalibrated) screen — a screen, not an accounting finding → warning, never critical
  beneish_flag: { severity: 'warning', category: 'fundamental', message: 'Beneish screen (model-based): growth-neutral M-score above −1.78 with corroborating red flags' },
  beneish_watch: { severity: 'info', category: 'fundamental', message: 'Beneish watch (model-based): textbook M-score above −1.78, growth-driven or uncorroborated' },
  wide_moat: { severity: 'info', category: 'fundamental', message: 'Classified as wide moat' },
  piotroski_high: { severity: 'info', category: 'fundamental', message: 'Piotroski F-score high (≥ 8)' },
  piotroski_low: { severity: 'warning', category: 'fundamental', message: 'Piotroski F-score low (≤ 2)' },
  grey_zone: { severity: 'warning', category: 'fundamental', message: 'Altman Z-score in the grey zone' },
  insider_selling: { severity: 'warning', category: 'insider', message: 'Heavy insider selling (90 days)' },
  ml_top_decile: { severity: 'info', category: 'model', message: 'Ranking model: top decile (3M)' },
  ml_bottom_decile: { severity: 'warning', category: 'model', message: 'Ranking model: bottom decile (3M)' },
  narrow_moat: { severity: 'info', category: 'fundamental', message: 'Classified as narrow moat' },
  stale_fundamentals: { severity: 'warning', category: 'data', message: 'Fundamentals are stale (no recent filing ingested)' },
}

const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

/** GET /companies/{id}/overview (+ /prices 1y, + /ratios for 5y history) → UI CompanyOverview. */
export function adaptCompanyOverview(ov: CCompanyOverview, prices: CPriceSeries | null, ratios: CMetricTable | null, ctx: AdapterCtx): CompanyOverview {
  const q = ov.quote ?? null
  const bars = barsFromContract(prices?.bars).slice(-260)
  const closes = bars.map((b) => b.close)
  const factorScores = factorMap(Object.fromEntries(ov.factors.map((f) => [f.factor_id, f.score ?? null])))
  const base = summaryFromSecurity(ov.security, ctx, {
    isin: ov.issuer.identifiers?.isin ?? '',
    price: num(q?.close),
    change1dPct: num(q?.chg_1d),
    compositeScore: num(ov.composite.score),
    factorScores,
    moat: moatFromContract(ov.moat?.moat_class),
    dataQuality: dqFromScore(ov.composite.dq_score),
  })
  const mmdd = ov.issuer.fiscal_year_end_mmdd ?? ''
  const month = Number(mmdd.slice(0, 2))
  const wsum = Object.values(ctx.weights).reduce((a, b) => a + b, 0) || 1

  const factors: FactorScoreDetail[] = ov.factors
    .map((f) => {
      const family = factorFromContract(f.factor_id)
      if (!family) return null
      const score = num(f.score)
      const weight = ctx.weights[family] / wsum
      return {
        family,
        score,
        peerMedian: num(f.peer_median),
        weight,
        contribution: Number.isFinite(score) ? (score - 50) * weight : NaN,
        subFactors: ov.key_metrics.filter((m) => m.factor_id === f.factor_id).map((m) => ({ name: m.label, score: num(m.metric_score ?? m.peer_percentile) })),
      }
    })
    .filter((x): x is FactorScoreDetail => x !== null)

  const hist = new Map((ratios?.metrics ?? []).map((m) => [m.metric_id, m.values.filter((v): v is number => typeof v === 'number')]))
  const keyRatios: KeyRatio[] = ov.key_metrics.map((m) => ({
    id: m.metric_id,
    label: m.label,
    unit: unitFromContract(m.unit),
    value: m.raw_value ?? null,
    peerMedian: m.peer_median ?? null,
    peerPercentile: m.peer_percentile === null || m.peer_percentile === undefined ? null : pct100(m.peer_percentile),
    selfZScore5y: m.self_z ?? null,
    higherIsBetter: m.higher_is_better ?? true,
    history: hist.get(m.metric_id) ?? [],
  }))

  const alerts: CompanyAlert[] = ov.badges.map((b, i) => ({
    id: `${ov.security.security_id}-badge-${i}`,
    date: ov.as_of,
    code: b,
    modelBased: b.startsWith('beneish') || b.startsWith('ml_'),
    ...(BADGE[b] ?? { severity: 'info' as const, category: 'data' as const, message: b.replace(/_/g, ' ') }),
  }))

  return {
    dataStatus: ov.data_status,
    profile: {
      ...base,
      currency: q?.currency ?? base.currency,
      marketCapUsd: num(q?.market_cap_usd ?? ov.security.market_cap_usd),
      description: ov.issuer.description ?? '',
      website: ov.issuer.website ?? '',
      employees: num(ov.issuer.employees),
      fiscalYearEnd: month >= 1 && month <= 12 ? MONTHS[month - 1] : '—',
      reportingCurrency: ov.issuer.reporting_currency ?? base.currency,
      fxToUsd: num(ov.fx_to_usd),
      high52w: num(q?.high_52w ?? (closes.length ? Math.max(...closes) : null)),
      low52w: num(q?.low_52w ?? (closes.length ? Math.min(...closes) : null)),
      beta: num(ov.beta_1y),
      sharesOutstanding: num(ov.shares_outstanding),
      enterpriseValueUsd: num(q?.enterprise_value_usd),
      avgDailyValueUsd: num(q?.avg_dollar_volume_20d_usd),
      lastFilingDate: ov.last_filing_date ?? '',
      dataQualityNotes: ov.badges.filter((b) => b.startsWith('stale') || b.includes('dq')).map((b) => b.replace(/_/g, ' ')),
      scoreScope: (ov.composite.scope as 'mvp' | 'extended' | null | undefined) ?? null,
      compositeReason: (ov.composite.reason as AvailabilityReason | null | undefined) ?? null,
      compositeValidated: ov.composite.validated === true && ov.composite.label === 'validated',
      rankSectorFull: ov.composite.rank_sector_full ?? null,
      nSectorFull: ov.composite.n_sector_full ?? null,
      mlScope: (ov.ml?.scope as 'mvp' | 'extrapolated' | null | undefined) ?? null,
    },
    factors,
    keyRatios,
    alerts,
    priceHistory: bars,
  }
}

/* ───────────────────────── Beneish (v1.29, model-based) ───────────────────────── */

type CBeneish = components['schemas']['BeneishDetail'] & { corroboration?: string[] }
type CAccountingFlags = NonNullable<components['schemas']['CompanyScores']['accounting_flags']>

const BENEISH_LABEL: Record<string, string> = {
  dsri: 'Days sales in receivables (DSRI)',
  gmi: 'Gross margin index (GMI)',
  aqi: 'Asset quality index (AQI)',
  sgi: 'Sales growth index (SGI)',
  depi: 'Depreciation index (DEPI)',
  sgai: 'SG&A index (SGAI)',
  tata: 'Total accruals to assets (TATA)',
  lvgi: 'Leverage index (LVGI)',
}
const BENEISH_ORDER = ['dsri', 'gmi', 'aqi', 'sgi', 'depi', 'sgai', 'tata', 'lvgi']

/** /companies/{id}/scores accounting_flags.beneish → UI breakdown; null when the API has no v1.29 detail. */
export function adaptBeneish(flags: CAccountingFlags | null | undefined): BeneishDetail | null {
  const b = flags?.beneish as CBeneish | undefined
  if (!b) return null
  const comps = b.components ?? {}
  const contr = b.contributions ?? {}
  const means = b.manipulator_means ?? {}
  const red = new Set(b.red_flags ?? [])
  const ids = [...BENEISH_ORDER.filter((k) => k in comps || k in contr), ...Object.keys(comps).filter((k) => !BENEISH_ORDER.includes(k))]
  return {
    level: b.level ?? flags?.beneish_level ?? 'none',
    mScore: numOrNullC(b.m_score ?? flags?.beneish_m),
    mScoreGrowthNeutral: numOrNullC(b.m_score_growth_adj),
    threshold: typeof b.threshold === 'number' ? b.threshold : -1.78,
    textbookFlag: b.textbook_flag ?? flags?.beneish_flag ?? null,
    growthDriven: b.growth_driven ?? null,
    applicable: b.applicable !== false,
    components: ids.map((id) => ({
      id,
      label: BENEISH_LABEL[id] ?? id.toUpperCase(),
      value: numOrNullC(comps[id]),
      contribution: numOrNullC(contr[id]),
      manipulatorMean: numOrNullC(means[id]),
      redFlag: red.has(id),
    })),
    redFlags: [...red],
    corroboration: Array.isArray(b.corroboration) ? b.corroboration.map(String) : [],
    method: b.method ?? null,
    periodEnd: b.period?.period_end ?? null,
  }
}
const numOrNullC = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/* ───────────────────────── financials ───────────────────────── */

const TOTAL_RE = /^(revenue|gross_profit|operating_income|ebit|ebitda|pretax_income|net_income|total_.*|cfo|cfi|cff|fcf)$/

/** GET /companies/{id}/financials?currency=LOCAL (+ USD for per-period FX) → UI CompanyFinancials. */
export function adaptFinancials(local: CFinancials, usd: CFinancials | null, periodicity: Periodicity): CompanyFinancials {
  const periods = local.periods.map((p) => (p.period_kind === 'FY' ? `FY${p.fiscal_year}` : `${p.fiscal_period}'${String(p.fiscal_year).slice(-2)}`))
  const periodEnds = local.periods.map((p) => p.period_end)
  // per-period FX implied by the USD view of the same line (currency lines only)
  const fx = local.periods.map((_, i) => {
    for (const l of local.lines) {
      if (l.unit && l.unit !== 'currency') continue
      const u = usd?.lines.find((x) => x.item_id === l.item_id)
      const a = l.values[i]
      const b = u?.values[i]
      if (typeof a === 'number' && typeof b === 'number' && a !== 0) return b / a
    }
    return local.currency === 'USD' ? 1 : NaN
  })
  const kindOf = (u: string | undefined): FinancialLineItem['kind'] => (u === 'per_share' ? 'perShare' : u === 'shares' ? 'shares' : 'amount')
  const scale = (u: string | undefined) => (u === 'currency' || u === 'shares' || u === undefined ? 1e-6 : 1)
  const byType: Record<StatementType, FinancialLineItem[]> = { income: [], balance: [], cashflow: [] }
  const T: Record<string, StatementType | undefined> = { IS: 'income', BS: 'balance', CF: 'cashflow' }
  for (const l of local.lines) {
    const t = T[l.statement]
    if (!t) continue
    const k = scale(l.unit)
    byType[t].push({
      id: snakeToCamel(l.item_id),
      label: l.label,
      level: Math.min(2, l.indent ?? 0),
      isTotal: TOTAL_RE.test(l.item_id),
      kind: kindOf(l.unit),
      values: l.values.map((v) => (typeof v === 'number' ? v * k : null)),
    })
  }
  const statements: FinancialStatement[] = (['income', 'balance', 'cashflow'] as StatementType[]).map((type) => ({
    type,
    periodicity,
    currency: local.currency,
    periods,
    periodEnds,
    fxToUsd: fx,
    items: byType[type],
  }))
  return { dataStatus: local.data_status, companyId: local.security_id, reportingCurrency: local.currency, periodicity, statements }
}


/* ───────────────────────── forecast ───────────────────────── */

const METRIC_ITEMS: { metric: ForecastSeries['metric']; label: string; ids: string[]; unit: ValueUnit }[] = [
  // first candidate with projected values wins (banks: excess-return model projects NI / book value, not revenue)
  { metric: 'revenue', label: 'Revenue', ids: ['revenue', 'total_revenue', 'net_income'], unit: 'currency' },
  { metric: 'ebit', label: 'EBIT', ids: ['ebit', 'operating_income', 'pretax_income', 'residual_income'], unit: 'currency' },
  { metric: 'fcf', label: 'Free cash flow', ids: ['fcf', 'free_cash_flow', 'total_equity_parent', 'total_equity'], unit: 'currency' },
  { metric: 'eps', label: 'Diluted EPS', ids: ['eps_diluted', 'eps'], unit: 'number' },
]
const SERIES_LABEL: Record<string, string> = { net_income: 'Net income', pretax_income: 'Pre-tax income', residual_income: 'Residual income', total_equity_parent: 'Book value', total_equity: 'Book value', operating_income: 'Operating income' }

function lineUnit(id: string): ValueUnit {
  if (/^drv_|margin|growth|rate|pct|ratio|cagr|_share$/.test(id)) return 'pct'
  if (/eps|per_share|dps|bvps/.test(id)) return 'number'
  if (/^shares/.test(id)) return 'number'
  return 'currency'
}
const prettyDriver = (id: string, label: string) => (label && label !== id ? label : id.replace(/^drv_/, '').replace(/_/g, ' '))

function adaptCoC(c: CCostOfCapital | null | undefined): CostOfCapital | null {
  if (!c) return null
  return {
    asOf: c.as_of ?? null,
    currency: c.currency ?? null,
    wacc: num(c.wacc),
    costOfEquity: num(c.cost_of_equity),
    beta: num(c.beta),
    betaRaw: num(c.beta_raw),
    betaSource: c.beta_source ?? null,
    rf: num(c.rf),
    rfSource: c.rf_source ?? null,
    erp: num(c.erp),
    crp: num(c.crp),
    kdPost: num(c.kd_post),
    taxRate: num(c.tax_rate),
    weightEquity: num(c.weight_equity),
    terminalGrowth: num(c.terminal_growth),
    isFinancial: !!c.is_financial,
    flags: c.flags ?? [],
  }
}

/** GET /companies/{id}/forecast → UI CompanyForecast (currency amounts in millions). */
export function adaptForecast(f: CForecast): CompanyForecast {
  const sc = (s: Scenario) => f.scenarios.find((x) => x.scenario === s) ?? f.scenarios.find((x) => x.scenario === 'base') ?? f.scenarios[0]
  const actual = new Set(f.actual_years)
  const fcYears = f.years.filter((y) => !actual.has(y))
  const idx = (y: number) => f.years.indexOf(y)
  const projectionPeriods = fcYears.map((y) => `FY${y}E`)
  const scaleOf = (u: ValueUnit) => (u === 'currency' ? 1e-6 : 1)
  const projectedAny = (l: { values: (number | null)[] }) => fcYears.some((y) => typeof l.values[idx(y)] === 'number')
  const line = (s: Scenario, ids: string[]) => {
    const ls = sc(s)?.lines ?? []
    for (const id of ids) {
      const l = ls.find((x) => x.item_id === id)
      if (l && projectedAny(l)) return l
    }
    return undefined
  }

  const series: ForecastSeries[] = []
  const used = new Set<string>()
  for (const m of METRIC_ITEMS) {
    const base = line('base', m.ids)
    if (!base || used.has(base.item_id)) continue
    used.add(base.item_id)
    const k = scaleOf(m.unit)
    const pick = (s: Scenario) => fcYears.map((y) => ({ period: `FY${y}E`, value: num((sc(s)?.lines.find((l) => l.item_id === base.item_id) ?? base).values[idx(y)]) * k }))
    series.push({
      metric: m.metric,
      label: SERIES_LABEL[base.item_id] ?? m.label,
      unit: m.unit,
      historical: f.actual_years.map((y) => ({ period: `FY${y}`, value: num(base.values[idx(y)]) * k })),
      projections: { bear: pick('bear'), base: pick('base'), bull: pick('bull') },
    })
  }

  const mean = (xs: (number | null | undefined)[]) => {
    const v = xs.filter((x): x is number => typeof x === 'number')
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : NaN
  }
  const drivers = (sc('base')?.lines ?? []).filter((l) => l.is_driver)
  const assumptions = drivers.map((d) => {
    const u = lineUnit(d.item_id)
    const avg = (s: Scenario) => mean(fcYears.map((y) => sc(s)?.lines.find((l) => l.item_id === d.item_id)?.values[idx(y)]))
    const h = mean(f.actual_years.map((y) => d.values[idx(y)]))
    return { id: d.item_id, label: prettyDriver(d.item_id, d.label), unit: u, bear: avg('bear'), base: avg('base'), bull: avg('bull'), historical5y: Number.isFinite(h) ? h : null }
  })

  const projected = {} as Record<Scenario, ProjectedStatementRow[]>
  for (const s of ['bear', 'base', 'bull'] as Scenario[]) {
    projected[s] = (sc(s)?.lines ?? [])
      .filter((l) => !l.is_driver && projectedAny(l))
      .map((l) => {
        const u = lineUnit(l.item_id)
        return { id: l.item_id, label: l.label, isTotal: /^(revenue|ebit|net_income|fcf|eps_diluted)$/.test(l.item_id), unit: u, values: fcYears.map((y) => num(l.values[idx(y)]) * scaleOf(u)) }
      })
  }

  const v = (s: Scenario) => sc(s)?.valuation
  const rd = f.reverse_dcf
  // API grid (base row, WACC × g) → flat list; rows = wacc, cols = terminal growth
  const g = f.sensitivity_grid
  const sensitivity: CompanyForecast['reverseDcf']['sensitivity'] = []
  if (g)
    g.wacc.forEach((w, i) =>
      g.terminal_growth.forEach((tg, j) => {
        const fv = g.fair_value[i]?.[j]
        if (typeof fv === 'number') sensitivity.push({ wacc: w, terminalGrowth: tg, fairValue: fv })
      }),
    )
  const price = num(f.price)
  const blended = num(f.weighted_fair_value ?? v('base')?.fair_value_per_share)
  return {
    dataStatus: f.data_status,
    currency: f.currency,
    projectionPeriods,
    series,
    assumptions,
    projected,
    fairValueDetail: {
      model: f.model ?? null,
      blended,
      upsidePct: Number.isFinite(blended) && price > 0 ? blended / price - 1 : NaN,
      dcf: num(f.fair_value_dcf),
      multiples: num(f.fair_value_multiples),
      low: num(f.fair_value_low),
      high: num(f.fair_value_high),
      target12m: num(f.target_price_12m),
      confidence: num(f.valuation_confidence),
      confidenceReasons: f.confidence_reasons ?? [],
      tvShare: num(f.tv_share),
      flags: f.flags ?? [],
    },
    costOfCapital: adaptCoC(f.cost_of_capital),
    reverseDcf: {
      currentPrice: price,
      impliedGrowth: num(rd?.implied_revenue_cagr_5y),
      impliedYears: 5,
      wacc: num(f.cost_of_capital?.wacc ?? v('base')?.wacc),
      terminalGrowth: num(v('base')?.terminal_growth),
      historicalGrowth5y: num(rd?.hist_revenue_cagr_5y),
      consensusGrowth: f.consensus_growth ?? null,
      fairValue: { bear: num(v('bear')?.fair_value_per_share), base: num(v('base')?.fair_value_per_share), bull: num(v('bull')?.fair_value_per_share) },
      sensitivity,
    },
  }
}

/* ───────────────────────── peers ───────────────────────── */

/** GET /companies/{id}/peers → UI CompanyPeers. */
export function adaptPeers(p: CPeerComparison, subjectId: string, ctx: AdapterCtx): CompanyPeers {
  const metrics = p.metrics.map((m) => ({ id: m.id, label: m.label, unit: unitFromContract(m.unit), higherIsBetter: m.higher_is_better ?? true, description: m.formula ?? m.label }))
  const hasSelf = p.rows.some((r) => r.is_self)
  const peers = p.rows.map((r) => ({
    companyId: r.security.security_id,
    ticker: r.security.ticker,
    name: r.security.name,
    country: r.security.country,
    marketCapUsd: num(r.security.market_cap_usd),
    compositeScore: num(r.composite),
    isSubject: hasSelf ? r.is_self : r.security.security_id === subjectId,
    metrics: Object.fromEntries(metrics.map((m) => [m.id, typeof r.values[m.id] === 'number' && Number.isFinite(r.values[m.id]) ? (r.values[m.id] as number) : null])),
  }))
  const subject = peers.find((x) => x.isSubject)
  const rank: CompanyPeers['subjectRankByMetric'] = {}
  for (const m of metrics) {
    const sv = subject?.metrics[m.id]
    const vals = peers.map((x) => x.metrics[m.id]).filter((x): x is number => x !== null)
    if (typeof sv === 'number') rank[m.id] = { rank: vals.filter((x) => (m.higherIsBetter ? x > sv : x < sv)).length + 1, of: vals.length }
  }
  const x = metrics.find((m) => m.unit === 'multiple') ?? metrics[0]
  const y = metrics.find((m) => m.id !== x?.id && m.unit === 'pct' && m.higherIsBetter) ?? metrics.find((m) => m.id !== x?.id) ?? x
  const subjSub = p.rows.find((r) => r.is_self)?.security.subsector_id
  return {
    dataStatus: p.data_status,
    peerCount: p.peer_count ?? undefined,
    level: p.level,
    peerGroupKey: p.peer_group_key,
    kpiProfile: ctx.kpiProfileOfSubsector(subjSub),
    metrics,
    peers,
    scatter: { xMetric: x?.id ?? '', yMetric: y?.id ?? '' },
    subjectRankByMetric: rank,
  }
}

/* ───────────────────────── moat ───────────────────────── */

const SOURCES: MoatSource[] = ['Intangibles', 'Switching costs', 'Network effects', 'Cost advantage', 'Efficient scale']
function moatSource(s: string): MoatSource | null {
  const t = s.toLowerCase()
  if (/intang|brand|patent|licen|regulat/.test(t)) return 'Intangibles'
  if (/switch|lock/.test(t)) return 'Switching costs'
  if (/network/.test(t)) return 'Network effects'
  if (/cost/.test(t)) return 'Cost advantage'
  if (/scale/.test(t)) return 'Efficient scale'
  return null
}

/** GET /companies/{id}/moat → UI CompanyMoat. */
export function adaptMoat(m: CMoatDetail, ticker = 'The company'): CompanyMoat {
  const typed = (m.moat_sources ?? []).map((x) => ({ src: moatSource(x.type), strength: typeof x.strength === 'number' ? (x.strength > 1 ? x.strength / 100 : x.strength) : 1 }))
  const legacy = (m.sources_of_moat ?? []).map((x) => ({ src: moatSource(x), strength: 1 }))
  const strengthOf = new Map<MoatSource, number>()
  for (const t of typed.length ? typed : legacy) if (t.src) strengthOf.set(t.src, Math.max(strengthOf.get(t.src) ?? 0, t.strength))
  const h = (m.history ?? []).filter((x) => typeof x.moat_score === 'number')
  const d = h.length >= 2 ? (h[h.length - 1].moat_score as number) - (h[Math.max(0, h.length - 5)].moat_score as number) : 0
  const rating = moatFromContract(m.moat_class)
  return {
    dataStatus: m.data_status,
    modelVariant: m.model_variant ?? null,
    yearsEvaluated: m.years_evaluated ?? null,
    rating,
    score: num(m.moat_score),
    confidence: m.confidence,
    trend: d > 3 ? 'widening' : d < -3 ? 'narrowing' : 'stable',
    sources: SOURCES.map((source) => ({ source, strength: strengthOf.get(source) ?? 0 })),
    evidence: m.components.map((c) => {
      const score = num(c.score)
      const threshold = num(c.threshold ?? 50)
      const hist = (c.history ?? [])
        .map((p) => ({ date: p.fiscal_year ? `${p.fiscal_year}-12-31` : p.date, value: num(p.value ?? p.score) }))
        .filter((p) => Number.isFinite(p.value))
      return {
        id: c.id,
        label: c.label,
        supplementary: !!c.supplementary,
        score,
        // threshold / pass-fail apply to the 0–100 component score; history carries the raw metric
        unit: 'score' as const,
        value: score,
        threshold,
        passes: typeof c.passed === 'boolean' ? c.passed : Number.isFinite(score) && score >= threshold,
        description: `Weight ${Math.round(c.weight * 100)}%${c.supplementary ? ' (supplementary)' : ''}${typeof c.raw === 'number' ? ` · raw ${Number(c.raw.toFixed(3))}` : ''}`,
        history: hist,
      }
    }),
    roicVsWacc: m.roic_vs_wacc.map((r) => ({ date: `${r.fiscal_year}-12-31`, roic: num(r.roic), wacc: num(r.wacc), hurdle: num(r.hurdle ?? r.wacc) })),
    marketShare: (m.market_share_series ?? []).filter((x) => typeof x.share === 'number').map((x) => ({ date: `${x.fiscal_year}-12-31`, value: x.share as number })),
    summary:
      m.moat_class === 'insufficient_data'
        ? `${ticker}: insufficient history to classify a moat.`
        : `${ticker} is classified as ${rating === 'none' ? 'no' : rating} moat with ${Math.round(m.confidence * 100)}% confidence${strengthOf.size ? ` (sources: ${[...strengthOf.keys()].join(', ').toLowerCase()})` : ''}${m.model_variant === 'financial' ? '; financial-company model variant.' : '.'}`,
  }
}

/* ───────────────────────── insiders ───────────────────────── */

const TXN_CODE: Record<string, InsiderTxnCode> = { buy: 'P', sell: 'S', grant: 'A', option_exercise: 'M', tax_withholding: 'F', gift: 'G', conversion: 'M', other: 'A' }
const NEGATIVE = new Set<InsiderTxnCode>(['S', 'F', 'G'])
const roleLabel = (r: string) => (/^(ceo|cfo|coo)$/.test(r) ? r.toUpperCase() : r.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase()))

export function adaptInsiderTxn(t: CInsiderTransaction, sec: { id: string; ticker: string }, source: string, clusterWindow?: { from: string; to: string } | null): InsiderTransaction {
  const raw = (t.txn_code ?? '').toUpperCase()
  const code: InsiderTxnCode = (['P', 'S', 'A', 'M', 'F', 'G'] as const).includes(raw as InsiderTxnCode) ? (raw as InsiderTxnCode) : (TXN_CODE[t.txn_type] ?? 'A')
  const sign = NEGATIVE.has(code) ? -1 : 1
  const value = num(t.value_usd)
  const inWindow = !!clusterWindow && code === 'P' && t.transaction_date >= clusterWindow.from && t.transaction_date <= clusterWindow.to
  return {
    id: t.txn_id,
    companyId: sec.id,
    ticker: sec.ticker,
    filedAt: t.knowledge_date,
    tradeDate: t.transaction_date,
    insiderName: t.insider_name,
    role: t.officer_title ?? (t.role_norm ? roleLabel(t.role_norm) : t.is_director ? 'Director' : t.is_officer ? 'Officer' : '—'),
    code,
    shares: sign * Math.abs(t.shares),
    price: num(t.price),
    valueUsd: sign * Math.abs(value),
    sharesOwnedAfter: num(t.shares_owned_after),
    ownershipChangePct: num(t.holding_change_pct),
    anomalyScore: num(t.anomaly_score),
    isClusterBuy: code === 'P' && (!!t.cluster_id || inWindow),
    clusterId: t.cluster_id ?? null,
    features: (t.features as Record<string, unknown> | null | undefined) ?? null,
    source,
  }
}

/** GET /companies/{id}/insiders (+ /prices) → UI CompanyInsiders. */
export function adaptCompanyInsiders(ci: CCompanyInsiders, prices: CPriceSeries | null, sec: { id: string; ticker: string }): CompanyInsiders {
  const sig = ci.signal ?? null
  const asOf = sig?.as_of_date ?? ''
  const hasClusterIds = ci.transactions.some((t) => !!t.cluster_id)
  const window = !hasClusterIds && sig?.cluster_buy && asOf ? { from: shiftDays(asOf, -90), to: asOf } : null
  const source = ci.coverage === 'full' ? 'Form 4' : ci.coverage === 'partial' ? 'National filing' : 'Filing'
  const tx = ci.transactions.map((t) => adaptInsiderTxn(t, sec, source, window)).sort((a, b) => b.tradeDate.localeCompare(a.tradeDate))
  const clusters = new Map<string, InsiderTransaction[]>()
  for (const t of tx) if (t.clusterId && t.code === 'P') clusters.set(t.clusterId, [...(clusters.get(t.clusterId) ?? []), t])
  const clusterBuys = clusters.size
    ? [...clusters.values()].map((ts) => ({ date: ts[0].tradeDate, insiders: new Set(ts.map((t) => t.insiderName)).size, valueUsd: ts.reduce((a, t) => a + (Number.isFinite(t.valueUsd) ? t.valueUsd : 0), 0) }))
    : sig?.cluster_buy
      ? [{ date: asOf, insiders: num(sig.n_buyers_90d), valueUsd: num(sig.net_buy_value_usd_90d) }]
      : []
  const scores = tx.map((t) => t.anomalyScore).filter((x) => Number.isFinite(x))
  return {
    dataStatus: ci.data_status,
    coverage: ci.coverage,
    anomalyScore: num(sig?.anomaly_score ?? (scores.length ? Math.max(...scores) : null)),
    netBuyValue90dUsd: num(sig?.net_buy_value_usd_90d),
    buyers90d: num(sig?.n_buyers_90d),
    sellers90d: num(sig?.n_sellers_90d),
    transactions: tx,
    clusterBuys,
    priceHistory: barsFromContract(prices?.bars),
  }
}

export function shiftDays(iso: string, d: number): string {
  const x = new Date(`${iso}T00:00:00Z`)
  x.setUTCDate(x.getUTCDate() + d)
  return x.toISOString().slice(0, 10)
}

/* ───────────────────────── sentiment & events ───────────────────────── */

const CAT: Record<string, EventCategory> = {
  conflict: 'Conflict',
  protest: 'Protest',
  sanctions_trade: 'Policy',
  diplomacy: 'Diplomacy',
  economy: 'Economic',
  disaster: 'Disaster',
  legal_regulatory: 'Legal',
  health: 'Disaster',
  other: 'Corporate',
}

export function adaptGdeltEvent(e: CGdeltEvent): EventItem {
  const category = CAT[e.event_category ?? 'other'] ?? 'Corporate'
  const actors = [e.actor1_name, e.actor2_name].filter(Boolean).join(' → ')
  return {
    id: String(e.global_event_id),
    date: e.event_date,
    category,
    headline: (e.headline ? decodeEntities(e.headline) : '') || (actors ? `${category}: ${actors}` : `${category} event (CAMEO ${e.event_code ?? e.event_root_code})`),
    countries: e.action_country ? [e.action_country] : [],
    tone: num(e.avg_tone),
    intensity: Math.min(1, Math.log10(e.num_articles + 1) / 3),
    articleCount: e.num_articles,
  }
}

/** Some publishers' titles arrive HTML-escaped (e.g. CJK as &#x7F8E;) → decode for display. */
export function decodeEntities(s: string): string {
  if (!s.includes('&')) return s
  return s
    .replace(/&#x([0-9a-f]+);/gi, (_, h: string) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d: string) => String.fromCodePoint(Number(d)))
    .replace(/&quot;/g, '"')
    .replace(/&#39;|&apos;/g, "'")
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&')
}

/** GET /companies/{id}/sentiment (+ /events, + /prices) → UI CompanySentiment. */
export function adaptCompanySentiment(cs: CCompanySentiment, ev: CCompanyEvents | null, prices: CPriceSeries | null): CompanySentiment {
  const bars = barsFromContract(prices?.bars)
  const closeOn = new Map(bars.map((b) => [b.time, b.close]))
  let lastClose = NaN
  const daily = [...cs.daily].sort((a, b) => a.date.localeCompare(b.date))
  const index = daily.map((d) => {
    lastClose = closeOn.get(d.date) ?? lastClose
    return { date: d.date, sentiment: num(d.sentiment_ewm_7d ?? d.sentiment_mean), newsVolume: d.news_count, close: lastClose }
  })
  const valid = index.filter((p) => Number.isFinite(p.sentiment))
  const last = valid[valid.length - 1]?.sentiment ?? NaN
  const prior = valid[Math.max(0, valid.length - 22)]?.sentiment ?? NaN
  const tones = (ev?.daily ?? []).slice(-30).map((d) => d.avg_tone).filter((x): x is number => typeof x === 'number')
  const lp = [...(cs.lazy_prices ?? [])].sort((a, b) => b.knowledge_date.localeCompare(a.knowledge_date))[0]
  return {
    dataStatus: cs.data_status,
    // −1…+1 index = latest 7-day EWM of item polarity; `sentiment_score` is the 0–100 cross-sectional percentile blend
    currentIndex: last,
    score: cs.sentiment_score ?? null,
    change30d: last - prior,
    toneAvg30d: tones.length ? tones.reduce((a, b) => a + b, 0) / tones.length : NaN,
    lazyPricesSimilarity: lp?.cosine_tfidf ?? null,
    index,
    news: cs.items.map((n) => ({
      id: n.item_id,
      publishedAt: n.published_at,
      source: n.publisher ?? n.source_id,
      headline: decodeEntities(n.title),
      url: n.url,
      language: n.language ?? 'en',
      sentiment: num(n.sentiment_score ?? (n.sentiment_label === 'positive' ? 0.5 : n.sentiment_label === 'negative' ? -0.5 : 0)),
      relevance: num(n.relevance),
    })),
    events: [...new Map((ev?.events ?? []).map(adaptGdeltEvent).map((e) => [`${e.headline.toLowerCase()}|${e.date}`, e] as const)).values()].sort((a, b) => b.date.localeCompare(a.date)),
  }
}

/* ───────────────────────── technicals ───────────────────────── */

const FRONTEND_TYPES: LevelType[] = ['swing_pivot', 'volume_profile', 'fibonacci', 'round_number', 'ma_200', 'ma_50', 'gap', 'anchored_vwap', 'prior_high_low']
const LEVEL_LABEL: Record<LevelType, string> = {
  swing_pivot: 'Swing pivots',
  volume_profile: 'Volume profile',
  fibonacci: 'Fibonacci',
  round_number: 'Round numbers',
  ma_200: '200-day MA',
  ma_50: '50-day MA',
  gap: 'Gaps',
  anchored_vwap: 'Anchored VWAP',
  prior_high_low: 'Prior period H/L',
}

function levelTypeOf(key: string): LevelType {
  if ((FRONTEND_TYPES as string[]).includes(key)) return key as LevelType
  const k = key.toLowerCase()
  if (/vwap/.test(k)) return 'anchored_vwap'
  if (/vp|volume|hvn|poc/.test(k)) return 'volume_profile'
  if (/fib/.test(k)) return 'fibonacci'
  if (/round/.test(k)) return 'round_number'
  if (/200/.test(k)) return 'ma_200'
  if (/50/.test(k)) return 'ma_50'
  if (/gap/.test(k)) return 'gap'
  if (/prior|prev|pdh|pdl|pwh|pwl|pmh|pml|high_low|cur_open|floor|cam/.test(k)) return 'prior_high_low'
  return 'swing_pivot'
}
const levelLabel = (t: string) => LEVEL_LABEL[t as LevelType] ?? t.replace(/_/g, ' ')

function sma(xs: number[], n: number): number {
  if (xs.length < n) return NaN
  return xs.slice(-n).reduce((a, b) => a + b, 0) / n
}
function rsi(xs: number[], n = 14): number {
  if (xs.length <= n) return NaN
  let g = 0
  let l = 0
  for (let i = xs.length - n; i < xs.length; i++) {
    const d = xs[i] - xs[i - 1]
    if (d > 0) g += d
    else l -= d
  }
  return l === 0 ? 100 : 100 - 100 / (1 + g / l)
}
const COMPONENT_LABEL: Record<string, string> = { trend: 'Trend', structure: 'Structure', location: 'Location', rr: 'Reward:risk', momentum: 'Momentum' }

function adaptSetup(t: CTechnicals): TradeSetup {
  const s = t.setup ?? null
  const plan = t.trade_plan ?? null
  const weights = (s?.score_weights ?? {}) as Record<string, number | null>
  const comps = Object.entries((s?.score_components ?? {}) as Record<string, number | null>)
    .filter(([, v]) => typeof v === 'number')
    .map(([key, v]) => ({ key: COMPONENT_LABEL[key] ?? key, score: v as number, weight: num(weights[key]) }))
  if (s) {
    const lb = s.last_break
    return {
      bias: s.bias ?? 'neutral',
      entry: num(s.entry),
      entryLow: num(s.entry_low),
      entryHigh: num(s.entry_high),
      invalidation: num(s.invalidation),
      target1: num(s.target1),
      target2: num(s.target2),
      riskReward: num(s.reward_risk),
      zoneId: typeof s.support_zone_id === 'number' ? `z${s.support_zone_id}` : null,
      note: s.note ?? '',
      technicalScore: num(s.technical_score),
      scoreComponents: comps,
      trend: s.trend ?? null,
      structureState: s.structure_state ?? null,
      lastBreak: lb && lb.kind ? { kind: lb.kind, direction: lb.direction ?? null, date: lb.date ?? null, price: num(lb.price) } : null,
    }
  }
  return {
    bias: plan ? (plan.side === 'support' ? 'long' : 'short') : 'neutral',
    entry: num(plan?.entry),
    invalidation: num(plan?.invalidation),
    target1: num(plan?.targets?.[0] ?? plan?.target),
    target2: num(plan?.targets?.[1] ?? plan?.target),
    riskReward: num(plan?.reward_risk),
    zoneId: plan ? `z${plan.zone_id}` : null,
    note: plan ? `Rank-${plan.rank} ${plan.side} zone ${plan.zone_low.toFixed(2)}–${plan.zone_high.toFixed(2)}.` : 'No zone with reward:risk ≥ 2 near price.',
  }
}

/** GET /companies/{id}/technicals (+ /prices) → UI CompanyTechnicals. */
export function adaptTechnicals(t: CTechnicals, prices: CPriceSeries | null): CompanyTechnicals {
  let bars: PriceBar[] = barsFromContract(prices?.bars).slice(-520)
  if (!bars.length) bars = [{ time: t.as_of, open: t.last_close, high: t.last_close, low: t.last_close, close: t.last_close, volume: 0 }]
  const closes = bars.map((b) => b.close)
  const levelByKey = new Map(t.levels.map((l) => [l.level_key, l]))
  const norm = (s: number) => (s > 1 ? s / 100 : s)
  const zones: ConfluenceZone[] = t.zones.map((z) => ({
    id: `z${z.zone_id}`,
    low: z.zone_low,
    high: z.zone_high,
    kind: z.side,
    strength: norm(z.strength),
    rank: z.rank,
    contributors: [...new Set(z.member_level_keys.map((k) => levelTypeOf(levelByKey.get(k)?.frontend_type ?? levelByKey.get(k)?.level_type ?? k)))],
    touches: z.member_level_keys.reduce((a, k) => a + (levelByKey.get(k)?.touches ?? 0), 0),
  }))
  // pivots: one per (date, price) — the API lists the same swing at several lookbacks (k2/k3/k5)
  const pivotSrc: SwingPivot[] = t.pivots?.length
    ? t.pivots
        .filter((p) => p.date)
        .map((p) => ({ time: p.date as string, price: p.price, kind: /high|resist/.test(`${p.kind} ${p.side ?? ''}`) ? ('high' as const) : ('low' as const), label: p.label ?? null }))
    : t.levels
        .filter((l) => /swing|pivot/i.test(l.level_type) && l.anchor_date)
        .map((l) => ({ time: l.anchor_date as string, price: l.price, kind: l.side === 'resistance' ? ('high' as const) : ('low' as const), label: l.structure_label ?? null }))
  const seen = new Set<string>()
  const pivots: SwingPivot[] = []
  for (const p of [...pivotSrc].sort((a, b) => (b.label ? 1 : 0) - (a.label ? 1 : 0))) {
    const k = `${p.time}|${p.price.toFixed(4)}`
    if (seen.has(k)) continue
    seen.add(k)
    pivots.push(p)
  }
  pivots.sort((a, b) => a.time.localeCompare(b.time))

  // per-level-key stats → one row per frontend type (best-ranked key wins)
  const byType = new Map<string, CTechnicals['level_stats'][number]>()
  for (const s of t.level_stats) {
    const type = s.frontend_type ?? levelTypeOf(s.level_key)
    const cur = byType.get(type)
    if (!cur || (s.hit_rate_shrunk ?? 0) > (cur.hit_rate_shrunk ?? 0)) byType.set(type, s)
  }
  const ind = t.indicators ?? null
  const ma50 = num(ind?.sma50 ?? sma(closes, 50))
  const ma200 = num(ind?.sma200 ?? sma(closes, 200))
  const last = t.last_close
  const trendRaw = ind?.trend ?? t.setup?.trend ?? null
  const trend = trendRaw === 'up' || trendRaw === 'down' || trendRaw === 'sideways' ? trendRaw : last > ma50 && ma50 > ma200 ? 'up' : last < ma50 && ma50 < ma200 ? 'down' : 'sideways'
  return {
    dataStatus: t.data_status,
    currency: prices?.currency,
    bars,
    zones,
    pivots,
    levelStats: [...byType.entries()]
      .map(([type, s]) => ({
        type: levelTypeOf(type),
        label: `${levelLabel(type)} · ${s.level_key}`,
        hitRate: num(s.hit_rate_shrunk ?? s.hit_rate),
        sampleSize: s.n_events,
        avgReactionPct: num(s.mean_reaction_atr) * (t.atr14 / t.last_close),
      }))
      .sort((a, b) => (b.hitRate || 0) - (a.hitRate || 0)),
    typeReliability: (t.type_reliability ?? [])
      .map((r) => ({ type: r.frontend_type, label: levelLabel(r.frontend_type), nEvents: r.n_events, hitRate: num(r.hit_rate_shrunk), edge: num(r.edge) }))
      .sort((a, b) => (Number.isFinite(b.edge) ? b.edge : -1) - (Number.isFinite(a.edge) ? a.edge : -1)),
    setup: adaptSetup(t),
    indicators: { rsi14: num(ind?.rsi14 ?? rsi(closes)), atr14: num(ind?.atr14 ?? t.atr14), ma50, ma200, trend },
  }
}

/* ───────────────────────── model ───────────────────────── */

export function modelKindOf(t: CModelType): ModelKind {
  return t === 'ft_transformer' || t === 'cs_attention' ? 'transformer' : 'xgboost'
}
const HORIZON_DAYS: Record<string, number> = { '1M': 21, '3M': 63, '6M': 126 }

/** SHAP families are model-internal (e.g. "factor", "adv"); map feature names onto factor families where obvious. */
function familyOfFeature(f: string): ShapContribution['family'] {
  const m = f.match(/f_factor_(\w+)/)
  const fam = m ? factorFromContract(m[1]) : null
  if (fam) return fam
  if (/mom|ret|trend/.test(f)) return 'momentum'
  if (/insider/.test(f)) return 'insider'
  if (/sent|news|tone/.test(f)) return 'sentiment'
  if (/macro|beta|rate/.test(f)) return 'macro'
  return 'quality'
}

/** GET /companies/{id}/model → UI CompanyModel (one prediction per model in `models[]`). */
export function adaptCompanyModel(m: CModelExplanation): CompanyModel {
  const kind = modelKindOf(m.model_type)
  const list = m.models?.length
    ? m.models
    : [{ model_type: m.model_type, model_run_id: m.model_run_id, horizon: m.horizon, date: m.date, rank_pct: m.rank_pct, decile: m.decile, expected_excess_return: m.expected_excess_return, interval: m.interval, confidence: m.confidence, trained_at: m.trained_at, feature_set_version: m.feature_set_version }]
  return {
    dataStatus: m.data_status,
    shapUnit: m.shap_unit ?? null,
    asOf: m.date,
    featureSetVersion: m.feature_set_version ?? list[0]?.feature_set_version ?? '—',
    predictions: list.map((p) => ({
      model: modelKindOf(p.model_type),
      modelType: p.model_type,
      modelVersion: p.model_run_id,
      horizonDays: HORIZON_DAYS[p.horizon] ?? 63,
      predictedRankPct: p.rank_pct * 100,
      expectedExcessReturn: num(p.expected_excess_return),
      intervalLow: num(p.interval?.low),
      intervalHigh: num(p.interval?.high),
      confidence: num(p.confidence),
      trainedAt: p.trained_at ?? p.date ?? m.date,
    })),
    baseValue: 0,
    shap: m.shap_top
      .map((s) => ({ feature: s.label ?? s.feature, family: factorFromContract(s.family) ?? familyOfFeature(s.feature), featureValue: typeof s.value === 'number' ? s.value.toFixed(2) : '—', contribution: s.contribution }))
      .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution)),
    rankHistory: (m.history ?? []).map((h) => ({ date: h.date, xgboost: kind === 'xgboost' ? h.rank_pct * 100 : NaN, transformer: kind === 'transformer' ? h.rank_pct * 100 : NaN })),
    scope: (m.scope as 'mvp' | 'extrapolated' | null | undefined) ?? null,
    scopeNote: m.scope_note ?? null,
    reason: (m.reason as AvailabilityReason | null | undefined) ?? null,
  }
}
