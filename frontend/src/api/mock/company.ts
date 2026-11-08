import type {
  CompanyAlert,
  CompanyFinancials,
  CompanyForecast,
  CompanyInsiders,
  CompanyModel,
  CompanyMoat,
  CompanyOverview,
  CompanyPeers,
  CompanyProfile,
  CompanySentiment,
  CompanyTechnicals,
  ConfluenceZone,
  EventCategory,
  EventItem,
  FactorScoreDetail,
  FinancialLineItem,
  FinancialStatement,
  ForecastAssumption,
  ForecastSeries,
  InsiderTransaction,
  InsiderTxnCode,
  KeyRatio,
  LevelType,
  LevelTypeStat,
  MoatEvidence,
  MoatSource,
  NewsItem,
  Periodicity,
  PriceBar,
  ProjectedStatementRow,
  Scenario,
  SentimentPoint,
  ShapContribution,
  SwingPivot,
} from '../types'
import { FX_TO_USD } from '@/lib/regions'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, KPI_SETS, SUBFACTORS } from './taxonomy'
import { BY_ID, UNIVERSE, toSummary, type UniverseEntry } from './universe'
import { priceBars } from './prices'
import { MOCK_AS_OF, Rng, addDays, round } from './rng'

export class NotFoundError extends Error {
  status = 404
}

export function entry(id: string): UniverseEntry {
  const e = BY_ID.get(id)
  if (!e) throw new NotFoundError(`Unknown security id: ${id}`)
  return e
}

const fxR = (e: UniverseEntry) => FX_TO_USD[e.reportingCurrency] ?? 1

/* ───────────── profile & overview ───────────── */

const DESCR: Record<string, string> = {
  Software: 'develops and licenses enterprise and consumer software, with a growing share of recurring subscription revenue',
  Semiconductors: 'designs and manufactures semiconductors used across data-centre, automotive, industrial and consumer end-markets',
  Banks: 'provides retail, corporate and investment banking services, funded primarily by a stable customer deposit base',
  Insurance: 'underwrites property & casualty and life insurance, investing float in a diversified fixed-income portfolio',
  REITs: 'owns and operates a diversified portfolio of income-producing real estate under long-dated leases',
  Pharmaceuticals: 'discovers, develops and commercialises prescription medicines across several therapeutic areas',
}

export function profile(id: string): CompanyProfile {
  const e = entry(id)
  const r = new Rng(`prof:${id}`)
  const bars = priceBars(id, 260)
  const closes = bars.map((b) => b.close)
  const notes: string[] = []
  if (e.dataQuality !== 'high') notes.push('Segment data partially inferred from filings text')
  if (e.region !== 'US') notes.push(`Fundamentals sourced from national filings; ${e.reportingCurrency} reporting`)
  if (e.dataQuality === 'low') notes.push('Quarterly cash-flow statement not reported; interpolated from semi-annual')
  return {
    ...toSummary(e),
    description: `${e.name} ${DESCR[e.subsector] ?? `operates in ${e.subsector.toLowerCase()}, serving customers across its home market and international regions`}. Mock profile for UI development.`,
    website: `https://www.${e.name.toLowerCase().replace(/[^a-z]+/g, '').slice(0, 14)}.com`,
    employees: Math.round(e.revenueUsdBn * r.range(1500, 6000)),
    fiscalYearEnd: e.country === 'JP' ? 'March' : 'December',
    reportingCurrency: e.reportingCurrency,
    fxToUsd: fxR(e),
    high52w: Math.max(...closes),
    low52w: Math.min(...closes),
    beta: e.beta,
    sharesOutstanding: Math.round(e.marketCapUsd / e.fxToUsd / e.price),
    enterpriseValueUsd: e.marketCapUsd * (1 + (e.kpis.netDebtEbitda ?? 1) * 0.08),
    avgDailyValueUsd: e.marketCapUsd * r.range(0.002, 0.008),
    lastFilingDate: addDays(MOCK_AS_OF, -r.int(5, 70)),
    dataQualityNotes: notes,
  }
}

function factorDetails(e: UniverseEntry): FactorScoreDetail[] {
  const r = new Rng(`fd:${e.id}`)
  const wsum = FACTOR_FAMILIES.reduce((a, f) => a + DEFAULT_WEIGHTS[f], 0)
  return FACTOR_FAMILIES.map((f) => {
    const score = e.factorScores[f]
    return {
      family: f,
      score,
      peerMedian: round(Math.min(95, Math.max(5, r.normal(50, 9))), 1),
      weight: round(DEFAULT_WEIGHTS[f] / wsum, 4),
      contribution: round(((score - 50) * DEFAULT_WEIGHTS[f]) / wsum, 2),
      subFactors: SUBFACTORS[f].map((name) => ({ name, score: round(Math.min(99, Math.max(1, score + r.normal(0, 14))), 0) })),
    }
  })
}

function peersOf(e: UniverseEntry, n = 12): UniverseEntry[] {
  let pool = UNIVERSE.filter((x) => x.subsector === e.subsector && x.id !== e.id)
  if (pool.length < 6) pool = pool.concat(UNIVERSE.filter((x) => x.sector === e.sector && x.subsector !== e.subsector))
  pool.sort(
    (a, b) =>
      Math.abs(Math.log(a.marketCapUsd / e.marketCapUsd)) - Math.abs(Math.log(b.marketCapUsd / e.marketCapUsd)) +
      (a.synthetic ? 0.6 : 0) - (b.synthetic ? 0.6 : 0),
  )
  return pool.slice(0, n)
}

function keyRatios(e: UniverseEntry): KeyRatio[] {
  const peers = peersOf(e, 25)
  const r = new Rng(`kr:${e.id}`)
  return KPI_SETS[e.kpiProfile].map((spec) => {
    const v = e.kpis[spec.id]
    const pv = peers.map((p) => p.kpis[spec.id]).filter((x): x is number => typeof x === 'number').sort((a, b) => a - b)
    const median = pv.length ? pv[Math.floor(pv.length / 2)] : null
    let pct = pv.length ? (pv.filter((x) => x < v).length / pv.length) * 100 : null
    if (pct !== null && !spec.higherIsBetter) pct = 100 - pct
    const history: number[] = []
    let h = v
    for (let i = 0; i < 20; i++) {
      history.unshift(round(h, 4))
      h = h - spec.sd * r.normal(0, 0.18)
    }
    const m = history.reduce((a, b) => a + b, 0) / history.length
    const sd = Math.sqrt(history.reduce((a, b) => a + (b - m) ** 2, 0) / (history.length - 1)) || 1
    return {
      id: spec.id,
      label: spec.label,
      unit: spec.unit,
      value: v,
      peerMedian: median,
      peerPercentile: pct === null ? null : round(pct, 0),
      selfZScore5y: round((v - m) / sd, 2),
      higherIsBetter: spec.higherIsBetter !== false,
      history,
    }
  })
}

function alerts(e: UniverseEntry): CompanyAlert[] {
  const r = new Rng(`al:${e.id}`)
  const pool: Omit<CompanyAlert, 'id' | 'date'>[] = [
    { severity: 'warning', category: 'insider', message: 'Cluster of 3 insider purchases within 6 trading days (anomaly score 0.87)' },
    { severity: 'info', category: 'technical', message: 'Price entered rank-1 support confluence zone' },
    { severity: 'critical', category: 'fundamental', message: 'Beneish M-score crossed −1.78 threshold (possible earnings manipulation flag)' },
    { severity: 'info', category: 'model', message: 'XGBoost predicted rank moved into top decile' },
    { severity: 'warning', category: 'sentiment', message: 'News sentiment fell 2.1σ below 90-day mean' },
    { severity: 'info', category: 'fundamental', message: 'EPS revisions breadth turned positive (+0.31)' },
    { severity: 'warning', category: 'data', message: 'Latest 10-Q restated prior-quarter revenue; point-in-time history preserved' },
    { severity: 'warning', category: 'fundamental', message: 'Interest coverage below 3.0x for the first time in 5 years' },
  ]
  return r
    .shuffle(pool)
    .slice(0, r.int(2, 5))
    .map((a, i) => ({ ...a, id: `${e.id}-al-${i}`, date: addDays(MOCK_AS_OF, -r.int(0, 40)) }))
    .sort((a, b) => b.date.localeCompare(a.date))
}

export function overview(id: string): CompanyOverview {
  const e = entry(id)
  return { profile: profile(id), factors: factorDetails(e), keyRatios: keyRatios(e), alerts: alerts(e), priceHistory: priceBars(id, 260) }
}

/* ───────────── financial statements ───────────── */

interface YearCore {
  rev: number
  gm: number
  ebitM: number
  daPct: number
  rdPct: number
  netDebt: number
  shares: number
}

function statementCore(e: UniverseEntry, n: number, quarterly: boolean): { periods: string[]; ends: string[]; cores: YearCore[] } {
  const r = new Rng(`fin:${e.id}:${quarterly ? 'q' : 'a'}`)
  const revLast = (e.revenueUsdBn * 1e3) / fxR(e) / (quarterly ? 4 : 1)
  const g = quarterly ? e.growthBase / 4 : e.growthBase
  const fyEndMonth = e.country === 'JP' ? 3 : 12
  const lastFy = 2025
  const periods: string[] = []
  const ends: string[] = []
  for (let i = n - 1; i >= 0; i--) {
    if (quarterly) {
      // last reported quarter: Q2 2026 (Jun) for Dec FY
      const qIdx = 4 * 2026 + 1 - i // 0-based quarter index
      const y = Math.floor(qIdx / 4)
      const q = (qIdx % 4) + 1
      periods.push(`Q${q}'${String(y).slice(2)}`)
      ends.push(new Date(Date.UTC(y, q * 3, 0)).toISOString().slice(0, 10))
    } else {
      const y = lastFy - i
      periods.push(`FY${y}`)
      ends.push(new Date(Date.UTC(fyEndMonth === 12 ? y : y + 1, fyEndMonth, 0)).toISOString().slice(0, 10))
    }
  }
  const cores: YearCore[] = []
  let rev = revLast
  const tech = e.kpiProfile === 'software' || e.kpiProfile === 'semis' || e.kpiProfile === 'pharma'
  for (let i = 0; i < n; i++) {
    const season = quarterly ? 1 + 0.04 * Math.sin(((n - i) / 4) * 2 * Math.PI) : 1
    cores.unshift({
      rev: rev * season,
      gm: Math.min(0.92, Math.max(0.08, (e.kpis.grossMargin ?? 0.38 + e.marginBase * 0.6) + r.normal(0, 0.012))),
      ebitM: Math.max(-0.15, e.marginBase + r.normal(0, 0.015) - i * 0.002),
      daPct: 0.04 + r.next() * 0.03,
      rdPct: tech ? (e.kpis.rdPctRev ?? 0.15) + r.normal(0, 0.008) : 0,
      netDebt: revLast * (quarterly ? 4 : 1) * 0.3 * ((e.kpis.netDebtEbitda ?? 1.2) / 1.5) * (1 + r.normal(0, 0.08)),
      shares: (e.marketCapUsd / e.fxToUsd / e.price / 1e6) * (1 + i * (quarterly ? 0.003 : 0.012)),
    })
    rev = rev / (1 + g + r.normal(0, quarterly ? 0.012 : 0.03))
  }
  return { periods, ends, cores }
}

const li = (id: string, label: string, values: (number | null)[], level = 0, isTotal = false, kind: FinancialLineItem['kind'] = 'amount'): FinancialLineItem => ({
  id,
  label,
  level,
  isTotal,
  kind,
  values: values.map((v) => (v === null ? null : kind === 'perShare' ? round(v, 2) : round(v, 1))),
})

function generalStatements(e: UniverseEntry, periodicity: Periodicity): FinancialStatement[] {
  const quarterly = periodicity === 'quarterly'
  const n = quarterly ? 12 : 6
  const { periods, ends, cores } = statementCore(e, n, quarterly)
  const r = new Rng(`fin2:${e.id}:${periodicity}`)
  const fx = ends.map((_, i) => round(fxR(e) * (1 + 0.04 * Math.sin(i * 0.9) + r.normal(0, 0.01)), 6))
  const rev = cores.map((c) => c.rev)
  const cogs = cores.map((c) => -c.rev * (1 - c.gm))
  const gp = cores.map((_, i) => rev[i] + cogs[i])
  const ebit = cores.map((c) => c.rev * c.ebitM)
  const da = cores.map((c) => c.rev * c.daPct)
  const rd = cores.map((c) => (c.rdPct ? -c.rev * c.rdPct : null))
  const sga = cores.map((_, i) => -(gp[i] - ebit[i]) - (rd[i] ?? 0))
  const interest = cores.map((c) => -Math.max(0, c.netDebt) * 0.045 / (quarterly ? 4 : 1))
  const pretax = ebit.map((v, i) => v + interest[i])
  const tax = pretax.map((v) => -Math.max(0, v) * 0.22)
  const ni = pretax.map((v, i) => v + tax[i])
  const shares = cores.map((c) => c.shares)
  const eps = ni.map((v, i) => v / shares[i])
  const income: FinancialStatement = {
    type: 'income',
    periodicity,
    currency: e.reportingCurrency,
    periods,
    periodEnds: ends,
    fxToUsd: fx,
    items: [
      li('revenue', 'Revenue', rev, 0, true),
      li('cogs', 'Cost of revenue', cogs, 1),
      li('grossProfit', 'Gross profit', gp, 0, true),
      li('rd', 'Research & development', rd, 1),
      li('sga', 'Selling, general & admin.', sga, 1),
      li('ebitda', 'EBITDA', ebit.map((v, i) => v + da[i]), 0, true),
      li('da', 'Depreciation & amortisation', da.map((v) => -v), 1),
      li('ebit', 'Operating income (EBIT)', ebit, 0, true),
      li('interest', 'Net interest expense', interest, 1),
      li('pretax', 'Pre-tax income', pretax, 0, true),
      li('tax', 'Income tax', tax, 1),
      li('netIncome', 'Net income', ni, 0, true),
      li('eps', 'Diluted EPS', eps, 0, false, 'perShare'),
      li('shares', 'Diluted shares (m)', shares, 1, false, 'shares'),
    ],
  }
  const annualRev = rev.map((v) => v * (quarterly ? 4 : 1))
  const cash = annualRev.map((v) => v * (0.1 + r.next() * 0.06))
  const recv = annualRev.map((v) => v * 0.13)
  const inv = annualRev.map((v) => (e.kpiProfile === 'software' ? null : v * 0.1))
  const tca = cash.map((v, i) => v + recv[i] + (inv[i] ?? 0) + annualRev[i] * 0.03)
  const ppe = annualRev.map((v) => v * (0.35 + (e.kpiProfile === 'utility' || e.kpiProfile === 'energy' ? 1.6 : 0)))
  const gw = annualRev.map((v) => v * 0.25)
  const ta = tca.map((v, i) => v + ppe[i] + gw[i] + annualRev[i] * 0.06)
  const pay = annualRev.map((v) => v * 0.09)
  const std = cores.map((c) => Math.max(0, c.netDebt) * 0.15)
  const tcl = pay.map((v, i) => v + std[i] + annualRev[i] * 0.07)
  const ltd = cores.map((c, i) => Math.max(0, c.netDebt) * 0.85 + cash[i])
  const tl = tcl.map((v, i) => v + ltd[i] + annualRev[i] * 0.05)
  const eq = ta.map((v, i) => v - tl[i])
  const balance: FinancialStatement = {
    ...income,
    type: 'balance',
    items: [
      li('cash', 'Cash & equivalents', cash, 1),
      li('receivables', 'Receivables', recv, 1),
      li('inventory', 'Inventories', inv, 1),
      li('currentAssets', 'Total current assets', tca, 0, true),
      li('ppe', 'PP&E, net', ppe, 1),
      li('goodwill', 'Goodwill & intangibles', gw, 1),
      li('totalAssets', 'Total assets', ta, 0, true),
      li('payables', 'Payables', pay, 1),
      li('shortDebt', 'Short-term debt', std, 1),
      li('currentLiabilities', 'Total current liabilities', tcl, 0, true),
      li('longDebt', 'Long-term debt', ltd, 1),
      li('totalLiabilities', 'Total liabilities', tl, 0, true),
      li('equity', "Shareholders' equity", eq, 0, true),
    ],
  }
  const cfo = ni.map((v, i) => v + da[i] + rev[i] * r.normal(0, 0.015))
  const capex = rev.map((v, i) => -v * (e.kpis.capexPctRev ?? 0.045 + cores[i].daPct * 0.4))
  const fcf = cfo.map((v, i) => v + capex[i])
  const acq = rev.map((v) => (r.chance(0.3) ? -v * r.range(0.01, 0.08) : 0))
  const cfi = capex.map((v, i) => v + acq[i])
  const div = ni.map((v) => -Math.max(0, v) * (e.kpis.dividendYield ?? 0.02) * 12)
  const buy = ni.map((v) => -Math.max(0, v) * r.range(0, 0.35))
  const debt = rev.map((v) => v * r.normal(0, 0.02))
  const cff = div.map((v, i) => v + buy[i] + debt[i])
  const cashflow: FinancialStatement = {
    ...income,
    type: 'cashflow',
    items: [
      li('netIncomeCf', 'Net income', ni, 1),
      li('daCf', 'Depreciation & amortisation', da, 1),
      li('cfo', 'Cash from operations', cfo, 0, true),
      li('capex', 'Capital expenditure', capex, 1),
      li('acquisitions', 'Acquisitions', acq, 1),
      li('cfi', 'Cash from investing', cfi, 0, true),
      li('dividends', 'Dividends paid', div, 1),
      li('buybacks', 'Share repurchases', buy, 1),
      li('netDebt', 'Net debt issuance', debt, 1),
      li('cff', 'Cash from financing', cff, 0, true),
      li('fcf', 'Free cash flow', fcf, 0, true),
    ],
  }
  return [income, balance, cashflow]
}

function bankStatements(e: UniverseEntry, periodicity: Periodicity): FinancialStatement[] {
  const quarterly = periodicity === 'quarterly'
  const n = quarterly ? 12 : 6
  const { periods, ends, cores } = statementCore(e, n, quarterly)
  const r = new Rng(`bank:${e.id}:${periodicity}`)
  const fx = ends.map((_, i) => round(fxR(e) * (1 + 0.04 * Math.sin(i * 0.9)), 6))
  const tor = cores.map((c) => c.rev)
  const nii = tor.map((v) => v * (0.62 + r.normal(0, 0.02)))
  const fees = tor.map((v) => v * 0.27)
  const trading = tor.map((v, i) => v - nii[i] - fees[i])
  const opex = tor.map((v) => -v * (e.kpis.costIncome ?? 0.55))
  const ppp = tor.map((v, i) => v + opex[i])
  const prov = tor.map((v) => -v * Math.max(0.02, (e.kpis.nplRatio ?? 0.02) * 4 + r.normal(0, 0.01)))
  const pbt = ppp.map((v, i) => v + prov[i])
  const tax = pbt.map((v) => -Math.max(0, v) * 0.26)
  const ni = pbt.map((v, i) => v + tax[i])
  const shares = cores.map((c) => c.shares)
  const income: FinancialStatement = {
    type: 'income',
    periodicity,
    currency: e.reportingCurrency,
    periods,
    periodEnds: ends,
    fxToUsd: fx,
    items: [
      li('nii', 'Net interest income', nii, 1),
      li('fees', 'Net fee & commission income', fees, 1),
      li('trading', 'Trading & other income', trading, 1),
      li('revenue', 'Total operating income', tor, 0, true),
      li('opex', 'Operating expenses', opex, 1),
      li('ppp', 'Pre-provision profit', ppp, 0, true),
      li('provisions', 'Loan-loss provisions', prov, 1),
      li('pretax', 'Profit before tax', pbt, 0, true),
      li('tax', 'Income tax', tax, 1),
      li('netIncome', 'Net income', ni, 0, true),
      li('eps', 'Diluted EPS', ni.map((v, i) => v / shares[i]), 0, false, 'perShare'),
    ],
  }
  const scale = quarterly ? 4 : 1
  const loans = tor.map((v) => (v * scale) / 0.045)
  const secs = loans.map((v) => v * 0.35)
  const cashCb = loans.map((v) => v * 0.12)
  const ta = loans.map((v, i) => v + secs[i] + cashCb[i] + v * 0.08)
  const dep = loans.map((v) => v / (e.kpis.loanDeposit ?? 0.85))
  const debt = ta.map((v, i) => v * 0.9 - dep[i])
  const tl = dep.map((v, i) => v + Math.max(0, debt[i]) + ta[i] * 0.02)
  const eq = ta.map((v, i) => v - tl[i])
  const balance: FinancialStatement = {
    ...income,
    type: 'balance',
    items: [
      li('cashCb', 'Cash & central-bank balances', cashCb, 1),
      li('loans', 'Loans to customers, net', loans, 1),
      li('securities', 'Investment securities', secs, 1),
      li('totalAssets', 'Total assets', ta, 0, true),
      li('deposits', 'Customer deposits', dep, 1),
      li('debtIssued', 'Debt securities issued', debt.map((v) => Math.max(0, v)), 1),
      li('totalLiabilities', 'Total liabilities', tl, 0, true),
      li('equity', "Shareholders' equity", eq, 0, true),
      li('cet1Capital', 'CET1 capital', eq.map((v) => v * 0.82), 1),
    ],
  }
  const cfo = ni.map((v) => v * (1 + r.normal(0, 0.6)))
  const cashflow: FinancialStatement = {
    ...income,
    type: 'cashflow',
    items: [
      li('cfo', 'Cash from operations', cfo, 0, true),
      li('cfi', 'Cash from investing', cfo.map((v) => -v * r.range(0.2, 0.8)), 0, true),
      li('dividends', 'Dividends paid', ni.map((v) => -v * 0.45), 1),
      li('buybacks', 'Share repurchases', ni.map((v) => -v * r.range(0.05, 0.3)), 1),
      li('cff', 'Cash from financing', ni.map((v) => -v * r.range(0.4, 0.9)), 0, true),
    ],
  }
  return [income, balance, cashflow]
}

export function financials(id: string, periodicity: Periodicity): CompanyFinancials {
  const e = entry(id)
  const statements = e.kpiProfile === 'bank' || e.kpiProfile === 'insurance' ? bankStatements(e, periodicity) : generalStatements(e, periodicity)
  return { companyId: id, reportingCurrency: e.reportingCurrency, periodicity, statements }
}

/* ───────────── forecast ───────────── */

export function forecast(id: string): CompanyForecast {
  const e = entry(id)
  const r = new Rng(`fc:${id}`)
  const fin = financials(id, 'annual')
  const inc = fin.statements[0]
  const get = (k: string) => inc.items.find((x) => x.id === k)?.values.map((v) => v ?? 0) ?? []
  const rev = get('revenue')
  const ebit = inc.items.some((x) => x.id === 'ebit') ? get('ebit') : get('pretax')
  const ni = get('netIncome')
  const eps = get('eps')
  const cf = fin.statements[2]
  const fcfHist = cf.items.find((x) => x.id === 'fcf')?.values.map((v) => v ?? 0) ?? ni.map((v) => v * 0.9)
  const projectionPeriods = ['FY2026E', 'FY2027E', 'FY2028E', 'FY2029E', 'FY2030E']
  const g = Math.max(-0.05, e.growthBase)
  const assumptions: ForecastAssumption[] = [
    { id: 'revGrowth', label: 'Revenue CAGR', unit: 'pct', bear: round(g - 0.045, 3), base: round(g, 3), bull: round(g + 0.045, 3), historical5y: round(Math.pow(rev[rev.length - 1] / rev[0], 1 / 5) - 1, 3) },
    { id: 'ebitMargin', label: 'Terminal EBIT margin', unit: 'pct', bear: round(e.marginBase - 0.03, 3), base: round(e.marginBase + 0.005, 3), bull: round(e.marginBase + 0.03, 3), historical5y: round(e.marginBase, 3) },
    { id: 'taxRate', label: 'Effective tax rate', unit: 'pct', bear: 0.25, base: 0.22, bull: 0.2, historical5y: 0.22 },
    { id: 'capex', label: 'Capex % revenue', unit: 'pct', bear: 0.07, base: 0.055, bull: 0.045, historical5y: 0.052 },
    { id: 'nwc', label: 'Net working capital % rev', unit: 'pct', bear: 0.14, base: 0.11, bull: 0.09, historical5y: 0.115 },
    { id: 'buyback', label: 'Net share count change p.a.', unit: 'pct', bear: 0.005, base: -0.012, bull: -0.025, historical5y: -0.01 },
  ]
  const shift: Record<Scenario, number> = { bear: -1, base: 0, bull: 1 }
  const project = (last: number, growth: number, marginDrift: number) =>
    projectionPeriods.map((period, i) => ({ period, value: round(last * Math.pow(1 + growth, i + 1) * (1 + marginDrift * (i + 1)), 1) }))
  const scen = (fn: (s: Scenario) => { period: string; value: number }[]) => ({ bear: fn('bear'), base: fn('base'), bull: fn('bull') })
  const hist = (vals: number[]) => inc.periods.map((period, i) => ({ period, value: round(vals[i], 2) }))
  const series: ForecastSeries[] = [
    { metric: 'revenue', label: 'Revenue', unit: 'currency', historical: hist(rev), projections: scen((s) => project(rev[rev.length - 1], g + 0.045 * shift[s], 0)) },
    { metric: 'ebit', label: 'EBIT', unit: 'currency', historical: hist(ebit), projections: scen((s) => project(ebit[ebit.length - 1], g + 0.045 * shift[s], 0.012 * shift[s])) },
    { metric: 'fcf', label: 'Free cash flow', unit: 'currency', historical: hist(fcfHist), projections: scen((s) => project(fcfHist[fcfHist.length - 1], g + 0.05 * shift[s], 0.015 * shift[s])) },
    { metric: 'eps', label: 'Diluted EPS', unit: 'number', historical: hist(eps), projections: scen((s) => project(eps[eps.length - 1], g + 0.012 + 0.05 * shift[s], 0.012 * shift[s]).map((p) => ({ ...p, value: round(p.value, 2) }))) },
  ]
  const projected = {} as Record<Scenario, ProjectedStatementRow[]>
  for (const s of ['bear', 'base', 'bull'] as Scenario[]) {
    const rv = series[0].projections[s].map((p) => p.value)
    const eb = series[1].projections[s].map((p) => p.value)
    const fc = series[2].projections[s].map((p) => p.value)
    const ep = series[3].projections[s].map((p) => p.value)
    projected[s] = [
      { id: 'revenue', label: 'Revenue', isTotal: true, unit: 'currency', values: rv },
      { id: 'growth', label: 'Growth y/y', isTotal: false, unit: 'pct', values: rv.map((v, i) => round(v / (i ? rv[i - 1] : rev[rev.length - 1]) - 1, 4)) },
      { id: 'ebit', label: 'EBIT', isTotal: true, unit: 'currency', values: eb },
      { id: 'ebitMargin', label: 'EBIT margin', isTotal: false, unit: 'pct', values: eb.map((v, i) => round(v / rv[i], 4)) },
      { id: 'netIncome', label: 'Net income', isTotal: true, unit: 'currency', values: eb.map((v) => round(v * 0.74, 1)) },
      { id: 'fcf', label: 'Free cash flow', isTotal: true, unit: 'currency', values: fc },
      { id: 'fcfConv', label: 'FCF conversion', isTotal: false, unit: 'pct', values: fc.map((v, i) => round(v / (eb[i] * 0.74), 4)) },
      { id: 'eps', label: 'Diluted EPS', isTotal: true, unit: 'number', values: ep },
    ]
  }
  const wacc = round(0.075 + e.beta * 0.012 + (e.region === 'LATAM' ? 0.03 : 0), 4)
  const price = e.price
  const fair: Record<Scenario, number> = {
    bear: round(price * (0.72 + r.normal(0, 0.06)), 2),
    base: round(price * (1.04 + r.normal(0, 0.12)), 2),
    bull: round(price * (1.38 + r.normal(0, 0.12)), 2),
  }
  const sensitivity: CompanyForecast['reverseDcf']['sensitivity'] = []
  for (const w of [-0.01, -0.005, 0, 0.005, 0.01]) {
    for (const tg of [0.015, 0.02, 0.025, 0.03, 0.035]) {
      const fv = fair.base * (1 - w * 9) * (1 + (tg - 0.025) * 12)
      sensitivity.push({ wacc: round(wacc + w, 4), terminalGrowth: tg, fairValue: round(fv, 2) })
    }
  }
  return {
    currency: e.reportingCurrency,
    projectionPeriods,
    series,
    assumptions,
    projected,
    reverseDcf: {
      currentPrice: price,
      impliedGrowth: round(g + r.normal(0.01, 0.03), 4),
      impliedYears: 10,
      wacc,
      terminalGrowth: 0.025,
      historicalGrowth5y: assumptions[0].historical5y ?? 0,
      consensusGrowth: e.synthetic ? null : round(g + r.normal(0, 0.015), 4),
      fairValue: fair,
      sensitivity,
    },
  }
}

/* ───────────── peers ───────────── */

const SCATTER: Record<string, [string, string]> = {
  general: ['evEbit', 'roic'],
  bank: ['ptbv', 'rote'],
  insurance: ['pb', 'roe'],
  software: ['evSales', 'ruleOf40'],
  semis: ['evEbit', 'roic'],
  reit: ['priceToNav', 'ffoYield'],
  energy: ['evEbitda', 'roace'],
  pharma: ['peNtm', 'ebitMargin'],
  retail: ['evEbitda', 'roic'],
  utility: ['peNtm', 'rateBaseGrowth'],
}

export function peers(id: string): CompanyPeers {
  const e = entry(id)
  const set = KPI_SETS[e.kpiProfile]
  const list = [e, ...peersOf(e, 14)]
  const rows = list.map((p) => ({
    companyId: p.id,
    ticker: p.ticker,
    name: p.name,
    country: p.country,
    marketCapUsd: p.marketCapUsd,
    compositeScore: p.compositeScore,
    isSubject: p.id === e.id,
    metrics: Object.fromEntries(set.map((s) => [s.id, p.kpis[s.id] ?? null])),
  }))
  const subjectRankByMetric: CompanyPeers['subjectRankByMetric'] = {}
  for (const s of set) {
    const vals = rows.map((x) => x.metrics[s.id]).filter((v): v is number => v !== null)
    const sv = e.kpis[s.id]
    const better = vals.filter((v) => (s.higherIsBetter ? v > sv : v < sv)).length
    subjectRankByMetric[s.id] = { rank: better + 1, of: vals.length }
  }
  const [x, y] = SCATTER[e.kpiProfile]
  return {
    kpiProfile: e.kpiProfile,
    metrics: set.map((s) => ({ id: s.id, label: s.label, unit: s.unit, higherIsBetter: s.higherIsBetter, description: s.description })),
    peers: rows,
    scatter: { xMetric: x, yMetric: y },
    subjectRankByMetric,
  }
}

/* ───────────── moat ───────────── */

export function moat(id: string): CompanyMoat {
  const e = entry(id)
  const r = new Rng(`moat:${id}`)
  const years = Array.from({ length: 10 }, (_, i) => `${2016 + i}-12-31`)
  const wacc0 = 0.075 + e.beta * 0.01
  const roicBase = (e.kpis.roic ?? e.kpis.rote ?? e.kpis.roe ?? 0.12) as number
  const roicVsWacc = years.map((date, i) => ({ date, roic: round(roicBase + r.normal(0, 0.015) - (9 - i) * 0.002, 4), wacc: round(wacc0 + r.normal(0, 0.003) - (i > 6 ? 0 : 0.005), 4) }))
  const spread = roicVsWacc.map((p) => p.roic - p.wacc)
  const gm = years.map(() => (e.kpis.grossMargin ?? 0.4) + r.normal(0, e.moat === 'wide' ? 0.008 : 0.025))
  const gmMean = gm.reduce((a, b) => a + b, 0) / gm.length
  const gmSd = Math.sqrt(gm.reduce((a, b) => a + (b - gmMean) ** 2, 0) / (gm.length - 1))
  const premium = years.map((_, i) => e.marginBase - 0.12 + r.normal(0, 0.01) + i * 0.001)
  let share = 0.04 + r.next() * 0.2
  const marketShare = years.map((date) => {
    share = Math.max(0.005, share * (1 + r.normal(e.moat === 'none' ? -0.01 : 0.015, 0.03)))
    return { date, value: round(share, 4) }
  })
  const avgSpread = spread.reduce((a, b) => a + b, 0) / spread.length
  const evidence: MoatEvidence[] = [
    { id: 'roicWacc', label: 'ROIC − WACC (10y avg.)', unit: 'pct', value: round(avgSpread, 4), threshold: 0.03, passes: avgSpread > 0.03, description: 'Sustained excess return over cost of capital is the core economic signature of a moat.', history: years.map((date, i) => ({ date, value: round(spread[i], 4) })) },
    { id: 'gmStability', label: 'Gross-margin volatility (σ, 10y)', unit: 'pct', value: round(gmSd, 4), threshold: 0.02, passes: gmSd < 0.02, description: 'Low dispersion of gross margin through the cycle indicates pricing power.', history: years.map((date, i) => ({ date, value: round(gm[i], 4) })) },
    { id: 'marginPremium', label: 'EBIT-margin premium vs subsector', unit: 'pct', value: round(premium[premium.length - 1], 4), threshold: 0.03, passes: premium[premium.length - 1] > 0.03, description: 'Operating margin above the subsector median, adjusted for mix.', history: years.map((date, i) => ({ date, value: round(premium[i], 4) })) },
    { id: 'marketShare', label: 'Market-share change (5y)', unit: 'pct', value: round(marketShare[9].value - marketShare[4].value, 4), threshold: 0, passes: marketShare[9].value > marketShare[4].value, description: 'Share gains while sustaining returns argue for durable competitive advantage.', history: marketShare },
    { id: 'reinvestment', label: 'Incremental ROIC (5y)', unit: 'pct', value: round(roicBase + r.normal(0.01, 0.04), 4), threshold: 0.1, passes: roicBase > 0.1, description: 'Return on capital invested over the last five years.', history: years.map((date) => ({ date, value: round(roicBase + r.normal(0, 0.04), 4) })) },
  ]
  const srcs: MoatSource[] = ['Intangibles', 'Switching costs', 'Network effects', 'Cost advantage', 'Efficient scale']
  const passed = evidence.filter((x) => x.passes).length
  return {
    rating: e.moat,
    score: round(e.factorScores.moat, 0),
    confidence: round(0.45 + passed * 0.09 + (e.dataQuality === 'high' ? 0.05 : 0), 2),
    trend: spread[9] > spread[5] + 0.01 ? 'widening' : spread[9] < spread[5] - 0.01 ? 'narrowing' : 'stable',
    sources: srcs.map((source) => ({ source, strength: round(Math.min(1, Math.max(0, r.next() * (e.moat === 'wide' ? 1 : e.moat === 'narrow' ? 0.7 : 0.4))), 2) })),
    evidence,
    roicVsWacc,
    marketShare,
    summary:
      e.moat === 'wide'
        ? `${e.ticker} has earned returns well above its cost of capital for a decade with stable gross margins — consistent with a wide moat.`
        : e.moat === 'narrow'
          ? `${e.ticker} shows a positive but less durable excess-return profile; evidence supports a narrow moat.`
          : `${e.ticker} does not consistently earn its cost of capital; no durable moat is evident.`,
  }
}

/* ───────────── insiders ───────────── */

const FIRST: Record<string, string[]> = {
  default: ['James', 'Mary', 'Robert', 'Patricia', 'Michael', 'Linda', 'David', 'Susan', 'Richard', 'Karen', 'Thomas', 'Laura', 'Daniel', 'Anne'],
  JP: ['Hiroshi', 'Takashi', 'Kenji', 'Yuko', 'Akira', 'Naoko', 'Satoshi', 'Keiko'],
  KR: ['Min-jun', 'Seo-yeon', 'Ji-hoon', 'Hye-jin', 'Dong-hyun', 'Soo-ah'],
  CNHK: ['Wei', 'Li', 'Jun', 'Mei', 'Hao', 'Xin', 'Chun'],
  LATAM: ['João', 'Maria', 'Carlos', 'Ana', 'Luis', 'Fernanda', 'Pedro'],
  EU: ['Pierre', 'Sophie', 'Klaus', 'Elena', 'Marco', 'Inês', 'Lars', 'Ana'],
}
const LAST: Record<string, string[]> = {
  default: ['Smith', 'Johnson', 'Miller', 'Davis', 'Wilson', 'Anderson', 'Taylor', 'Clark', 'Hall', 'Walker'],
  JP: ['Sato', 'Suzuki', 'Takahashi', 'Tanaka', 'Watanabe', 'Ito'],
  KR: ['Kim', 'Lee', 'Park', 'Choi', 'Jung'],
  CNHK: ['Wang', 'Zhang', 'Chen', 'Liu', 'Huang', 'Wong'],
  LATAM: ['Silva', 'Santos', 'Oliveira', 'Hernández', 'García', 'Costa'],
  EU: ['Dubois', 'Müller', 'Rossi', 'Fernandes', 'Schmidt', 'Lindqvist', 'Moreau'],
}
const ROLES = ['CEO', 'CFO', 'COO', 'Director', 'Director', 'EVP', 'Chair', 'General Counsel', '10% Owner', 'CTO']

export function insiderTransactions(e: UniverseEntry, count?: number): InsiderTransaction[] {
  const r = new Rng(`ins:${e.id}`)
  const nameKey = e.region === 'US' || e.region === 'UK' ? 'default' : e.region
  const people = Array.from({ length: r.int(5, 9) }, () => ({
    name: `${r.pick(FIRST[nameKey] ?? FIRST.default)} ${r.pick(LAST[nameKey] ?? LAST.default)}`,
    role: r.pick(ROLES),
  }))
  const n = count ?? r.int(18, 36)
  const bars = priceBars(e.id)
  const src = e.region === 'US' ? 'SEC Form 4' : e.region === 'UK' ? 'RNS PDMR' : e.region === 'EU' ? 'MAR Art. 19' : e.region === 'JP' ? 'EDINET' : e.region === 'KR' ? 'OpenDART' : e.region === 'CNHK' ? 'HKEX DI' : 'CVM'
  const out: InsiderTransaction[] = []
  // force a cluster buy in ~40% of names
  const clusterAt = r.chance(0.2) ? r.int(20, 200) : -1
  for (let i = 0; i < n; i++) {
    const p = r.pick(people)
    let back = r.int(1, 480)
    let code: InsiderTxnCode = r.pick(['S', 'S', 'S', 'P', 'A', 'M', 'F', 'P', 'G'] as const)
    if (clusterAt > 0 && i < 4) {
      back = clusterAt - i * r.int(1, 2)
      code = 'P'
    }
    const bar = bars[Math.max(0, bars.length - 1 - back)]
    const shares = Math.round(Math.exp(r.normal(Math.log(8000), 1.1)))
    const price = bar.close
    const valueUsd = Math.round(shares * price * e.fxToUsd)
    const owned = Math.round(shares * r.range(2, 40))
    const isCluster = clusterAt > 0 && i < 4
    out.push({
      id: `${e.id}-ins-${i}`,
      companyId: e.id,
      ticker: e.ticker,
      filedAt: `${addDays(bar.time, r.int(1, 3))}T${String(r.int(10, 21)).padStart(2, '0')}:${String(r.int(0, 59)).padStart(2, '0')}:00Z`,
      tradeDate: bar.time,
      insiderName: p.name,
      role: p.role,
      code,
      shares: code === 'S' || code === 'F' || code === 'G' ? -shares : shares,
      price,
      valueUsd: code === 'S' || code === 'F' || code === 'G' ? -valueUsd : valueUsd,
      sharesOwnedAfter: owned,
      ownershipChangePct: round((code === 'S' || code === 'F' || code === 'G' ? -1 : 1) * (shares / (owned + shares)), 4),
      anomalyScore: round(
        Math.min(
          0.99,
          isCluster ? 0.75 + r.next() * 0.22 : code === 'P' ? 0.3 + r.next() * 0.55 : code === 'S' && r.chance(0.18) ? 0.55 + r.next() * 0.38 : r.chance(0.06) ? 0.6 + r.next() * 0.3 : r.next() * 0.45,
        ),
        2,
      ),
      isClusterBuy: isCluster,
      source: src,
    })
  }
  return out.sort((a, b) => b.tradeDate.localeCompare(a.tradeDate))
}

export function insiders(id: string): CompanyInsiders {
  const e = entry(id)
  const transactions = insiderTransactions(e)
  const cutoff = addDays(MOCK_AS_OF, -90)
  const recent = transactions.filter((t) => t.tradeDate >= cutoff)
  const clusters = transactions.filter((t) => t.isClusterBuy)
  const clusterBuys = clusters.length
    ? [{ date: clusters[clusters.length - 1].tradeDate, insiders: new Set(clusters.map((c) => c.insiderName)).size, valueUsd: clusters.reduce((a, c) => a + c.valueUsd, 0) }]
    : []
  return {
    anomalyScore: round(Math.max(...transactions.slice(0, 10).map((t) => t.anomalyScore)), 2),
    netBuyValue90dUsd: recent.filter((t) => t.code === 'P' || t.code === 'S').reduce((a, t) => a + t.valueUsd, 0),
    buyers90d: new Set(recent.filter((t) => t.code === 'P').map((t) => t.insiderName)).size,
    sellers90d: new Set(recent.filter((t) => t.code === 'S').map((t) => t.insiderName)).size,
    transactions,
    clusterBuys,
    priceHistory: priceBars(id, 500),
  }
}

/* ───────────── sentiment & events ───────────── */

const SOURCES = ['Reuters', 'Bloomberg', 'Financial Times', 'WSJ', 'Nikkei Asia', 'Yonhap', 'Caixin', 'Valor Econômico', 'Les Echos', 'Handelsblatt', 'Expansión', 'Jornal de Negócios', 'MarketWatch', 'Barron’s']
const POS = ['beats estimates as {sub} demand accelerates', 'raises full-year guidance on margin expansion', 'wins multi-year contract worth up to $1.2bn', 'announces $5bn buyback programme', 'upgraded to Buy on improving free-cash-flow outlook', 'gains share in core market, survey shows']
const NEG = ['misses revenue estimates amid softer {sub} demand', 'cuts outlook citing FX headwinds', 'faces regulatory probe over pricing practices', 'downgraded on valuation after strong run', 'CFO departs unexpectedly', 'recalls products after quality issue']
const NEU = ['to present at investor day next month', 'completes previously announced bond issue', 'names new independent director', 'reports in line with consensus', 'reaffirms dividend policy']

export function sentiment(id: string): CompanySentiment {
  const e = entry(id)
  const r = new Rng(`sent:${id}`)
  const bars = priceBars(id, 260)
  let s = r.normal(0.05, 0.15)
  const index: SentimentPoint[] = bars.map((b, i) => {
    const ret = i ? b.close / bars[i - 1].close - 1 : 0
    s = 0.93 * s + 0.07 * (ret * 25 + r.normal(0.02, 0.25))
    return { date: b.time, sentiment: round(Math.max(-1, Math.min(1, s)), 3), newsVolume: Math.max(0, Math.round(r.normal(6, 3) + Math.abs(ret) * 300)), close: b.close }
  })
  const news: NewsItem[] = Array.from({ length: 28 }, (_, i) => {
    const tone = r.next()
    const tmpl = tone > 0.6 ? r.pick(POS) : tone < 0.3 ? r.pick(NEG) : r.pick(NEU)
    const sentimentVal = tone > 0.6 ? r.range(0.25, 0.9) : tone < 0.3 ? -r.range(0.25, 0.9) : r.range(-0.15, 0.15)
    const d = addDays(MOCK_AS_OF, -Math.floor(i * 2.5 + r.next() * 2))
    return {
      id: `${id}-n-${i}`,
      publishedAt: `${d}T${String(r.int(6, 22)).padStart(2, '0')}:${String(r.int(0, 59)).padStart(2, '0')}:00Z`,
      source: r.pick(SOURCES),
      headline: `${e.name.replace(/ (Inc\.|Corp\.|plc|SA|AG|SE|N\.V\.|Ltd\.|Co\.,? Ltd\.|Co\.)$/, '')} ${tmpl.replace('{sub}', e.subsector.toLowerCase())}`,
      url: '#',
      language: e.region === 'JP' ? r.pick(['ja', 'en']) : e.region === 'LATAM' ? r.pick(['pt', 'es', 'en']) : 'en',
      sentiment: round(sentimentVal, 2),
      relevance: round(r.range(0.4, 1), 2),
    }
  })
  const cats: EventCategory[] = ['Policy', 'Economic', 'Legal', 'Corporate', 'Protest', 'Diplomacy', 'Disaster', 'Conflict']
  const events: EventItem[] = Array.from({ length: 12 }, (_, i) => {
    const cat = r.pick(cats)
    return {
      id: `${id}-ev-${i}`,
      date: addDays(MOCK_AS_OF, -r.int(0, 200)),
      category: cat,
      headline: `${cat} event in ${e.country === 'US' ? 'United States' : e.country} relevant to ${e.subsector.toLowerCase()}`,
      countries: [e.country],
      tone: round(r.normal(-1.2, 2.4), 2),
      intensity: round(r.next(), 2),
      articleCount: r.int(4, 400),
    }
  }).sort((a, b) => b.date.localeCompare(a.date))
  const last = index[index.length - 1].sentiment
  return {
    currentIndex: last,
    change30d: round(last - index[index.length - 22].sentiment, 3),
    toneAvg30d: round(r.normal(-0.4, 1.2), 2),
    lazyPricesSimilarity: e.region === 'US' ? round(r.range(0.82, 0.99), 3) : null,
    index,
    news,
    events,
  }
}

/* ───────────── technicals ───────────── */

function sma(xs: number[], n: number): number {
  const s = xs.slice(-n)
  return s.reduce((a, b) => a + b, 0) / s.length
}

function pivotsOf(bars: PriceBar[], w = 8): SwingPivot[] {
  const out: SwingPivot[] = []
  for (let i = w; i < bars.length - w; i++) {
    let hi = true
    let lo = true
    for (let j = i - w; j <= i + w; j++) {
      if (bars[j].high > bars[i].high) hi = false
      if (bars[j].low < bars[i].low) lo = false
    }
    if (hi) out.push({ time: bars[i].time, price: bars[i].high, kind: 'high' })
    if (lo) out.push({ time: bars[i].time, price: bars[i].low, kind: 'low' })
  }
  return out
}

const LEVEL_LABEL: Record<LevelType, string> = {
  swing_pivot: 'Swing pivots',
  volume_profile: 'Volume-profile HVN',
  fibonacci: 'Fibonacci retracement',
  round_number: 'Round numbers',
  ma_200: '200-day MA',
  ma_50: '50-day MA',
  gap: 'Unfilled gaps',
  anchored_vwap: 'Anchored VWAP',
  prior_high_low: 'Prior period high/low',
}

export function technicals(id: string): CompanyTechnicals {
  const r = new Rng(`ta:${id}`)
  const bars = priceBars(id, 520)
  const closes = bars.map((b) => b.close)
  const last = closes[closes.length - 1]
  const trs = bars.slice(-15).map((b, i, a) => (i ? Math.max(b.high - b.low, Math.abs(b.high - a[i - 1].close), Math.abs(b.low - a[i - 1].close)) : b.high - b.low))
  const atr = trs.slice(1).reduce((a, b) => a + b, 0) / 14
  let gain = 0
  let loss = 0
  for (let i = closes.length - 14; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1]
    if (d > 0) gain += d
    else loss -= d
  }
  const rsi = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
  const ma50 = sma(closes, 50)
  const ma200 = sma(closes, 200)
  const pivots = pivotsOf(bars).slice(-40)
  // zones from pivot clusters around price
  const types: LevelType[] = ['swing_pivot', 'volume_profile', 'fibonacci', 'round_number', 'ma_200', 'ma_50', 'gap', 'anchored_vwap', 'prior_high_low']
  const levelStats: LevelTypeStat[] = types
    .map((type) => ({ type, label: LEVEL_LABEL[type], hitRate: round(r.range(0.48, 0.79), 3), sampleSize: r.int(180, 2400), avgReactionPct: round(r.range(0.008, 0.035), 4) }))
    .sort((a, b) => b.hitRate - a.hitRate)
  const candidates = pivots.map((p) => p.price).concat([ma50, ma200])
  const zones: ConfluenceZone[] = []
  const used = new Set<number>()
  candidates
    .map((c) => ({ c, d: Math.abs(c / last - 1) }))
    .filter((x) => x.d > 0.012 && x.d < 0.35)
    .sort((a, b) => a.d - b.d)
    .forEach(({ c }) => {
      if (zones.length >= 7) return
      // enforce spacing so zones are distinct (≥ 3.5 ATR apart)
      if (zones.some((z) => Math.abs((z.low + z.high) / 2 - c) < atr * 3.5)) return
      const key = Math.round(c / atr)
      if (used.has(key)) return
      used.add(key)
      const width = atr * r.range(0.4, 0.9)
      const contributors = r.shuffle(types).slice(0, r.int(2, 5))
      const strength = Math.min(1, contributors.reduce((a, t) => a + (levelStats.find((s) => s.type === t)?.hitRate ?? 0.5), 0) / 3.2 + r.normal(0, 0.05))
      zones.push({ id: `z${zones.length}`, low: round(c - width / 2, 2), high: round(c + width / 2, 2), kind: c < last ? 'support' : 'resistance', strength: round(Math.max(0.15, strength), 2), rank: 0, contributors, touches: r.int(2, 9) })
    })
  zones.sort((a, b) => b.strength - a.strength).forEach((z, i) => (z.rank = i + 1))
  const sup = zones.filter((z) => z.kind === 'support').sort((a, b) => b.high - a.high)
  const res = zones.filter((z) => z.kind === 'resistance').sort((a, b) => a.low - b.low)
  const trend = last > ma50 && ma50 > ma200 ? 'up' : last < ma50 && ma50 < ma200 ? 'down' : 'sideways'
  const entryZone = sup[0]
  const entryPx = entryZone ? (entryZone.low + entryZone.high) / 2 : last * 0.97
  const inval = entryZone ? entryZone.low - atr * 0.5 : last * 0.92
  const risk = Math.max(0.01, entryPx - inval)
  // first resistance offering at least 1.5R, else a 2R measured move
  const tgts = res.filter((z) => z.low > entryPx + 1.5 * risk)
  const t1 = tgts[0] ? tgts[0].low : entryPx + 2 * risk
  const t2 = tgts[1] ? tgts[1].low : Math.max(t1 + risk, entryPx + 3 * risk)
  const rr = (t1 - entryPx) / risk
  return {
    bars,
    zones,
    pivots,
    levelStats,
    setup: {
      bias: trend === 'down' ? 'neutral' : 'long',
      entry: round(entryPx, 2),
      invalidation: round(inval, 2),
      target1: round(t1, 2),
      target2: round(t2, 2),
      riskReward: round(rr, 2),
      zoneId: entryZone?.id ?? null,
      note: entryZone ? `Entry inside rank-${entryZone.rank} support confluence; invalidate on daily close below zone − 0.5×ATR.` : 'No qualifying support zone within 30%.',
    },
    indicators: { rsi14: round(rsi, 1), atr14: round(atr, 2), ma50: round(ma50, 2), ma200: round(ma200, 2), trend },
  }
}

/* ───────────── model ───────────── */

const FEATURES: [string, ShapContribution['family']][] = [
  ['12-1m momentum', 'momentum'],
  ['EPS revision breadth 3m', 'growth'],
  ['EV/EBIT vs sector', 'value'],
  ['ROIC − WACC', 'moat'],
  ['Gross profitability', 'profitability'],
  ['Insider net buying 90d', 'insider'],
  ['News sentiment Δ30d', 'sentiment'],
  ['Accruals ratio', 'quality'],
  ['Net debt/EBITDA', 'balanceSheet'],
  ['Regime × beta', 'macro'],
  ['FCF yield', 'value'],
  ['Short-interest days to cover', 'sentiment'],
  ['Revenue CAGR 3y', 'growth'],
  ['Piotroski F-score', 'quality'],
  ['Realised vol 60d', 'momentum'],
  ['Rate sensitivity (duration)', 'macro'],
]

export function model(id: string): CompanyModel {
  const e = entry(id)
  const r = new Rng(`ml:${id}`)
  const tilt = (e.compositeScore - 50) / 50
  const xgbRank = Math.min(99, Math.max(1, 50 + tilt * 35 + r.normal(0, 10)))
  const trRank = Math.min(99, Math.max(1, xgbRank + r.normal(0, 9)))
  const er = (p: number) => round(((p - 50) / 50) * 0.055, 4)
  const shap: ShapContribution[] = r
    .shuffle(FEATURES)
    .slice(0, 12)
    .map(([feature, family]) => ({ feature, family, featureValue: r.chance(0.5) ? `${round(r.normal(0, 1.2), 2)}σ` : `p${r.int(5, 95)}`, contribution: round(((e.factorScores[family] - 50) / 50) * 0.012 + r.normal(0, 0.004), 4) }))
    .sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution))
  const baseValue = 0.0
  const rankHistory: CompanyModel['rankHistory'] = []
  let xa = xgbRank
  let ta = trRank
  for (let i = 51; i >= 0; i--) {
    rankHistory.push({ date: addDays(MOCK_AS_OF, -i * 7), xgboost: round(xa, 1), transformer: round(ta, 1) })
    xa = Math.min(99, Math.max(1, xa + r.normal(0, 4)))
    ta = Math.min(99, Math.max(1, ta + r.normal(0, 5)))
  }
  rankHistory[rankHistory.length - 1] = { date: MOCK_AS_OF, xgboost: round(xgbRank, 1), transformer: round(trRank, 1) }
  return {
    asOf: MOCK_AS_OF,
    featureSetVersion: 'fs-2026.09.2',
    predictions: [
      { model: 'xgboost', modelVersion: 'xgb-wf-2026.10.01', horizonDays: 63, predictedRankPct: round(xgbRank, 1), expectedExcessReturn: er(xgbRank), intervalLow: er(xgbRank) - 0.045, intervalHigh: er(xgbRank) + 0.045, confidence: round(0.55 + r.next() * 0.3, 2), trainedAt: '2026-10-01T03:12:00Z' },
      { model: 'transformer', modelVersion: 'mft-2026.09.28', horizonDays: 63, predictedRankPct: round(trRank, 1), expectedExcessReturn: er(trRank), intervalLow: er(trRank) - 0.055, intervalHigh: er(trRank) + 0.055, confidence: round(0.45 + r.next() * 0.35, 2), trainedAt: '2026-09-28T04:40:00Z' },
    ],
    baseValue,
    shap,
    rankHistory,
  }
}
