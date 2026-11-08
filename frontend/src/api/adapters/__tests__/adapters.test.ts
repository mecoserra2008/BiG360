import { describe, expect, it } from 'vitest'
import { buildCtx, factorMap, regionFromContract, regionsToContract, unitFromContract, dqFromScore, weightsToContract } from '../common'
import {
  adaptCompanyInsiders,
  adaptCompanyModel,
  adaptCompanyOverview,
  adaptCompanySentiment,
  adaptFinancials,
  adaptForecast,
  adaptGdeltEvent,
  decodeEntities,
  adaptMoat,
  adaptPeers,
  adaptTechnicals,
} from '../company'
import { adaptFreshness, adaptGlobalEvents, adaptInsiderRadar, adaptModelLab, adaptSystemStatus, alertFromContract, alertToContract, watchlistIds } from '../crossSection'
import { adaptMarketOverview } from '../market'
import { adaptScreener, adaptSearch, adaptSectorDetail, adaptSectorTree, buildScreenerQuery } from '../screener'
import * as fx from './fixtures'

const ctx = buildCtx(fx.taxonomy, fx.factorMeta)

describe('common', () => {
  it('maps regions both ways', () => {
    expect(regionFromContract('HK')).toBe('CNHK')
    expect(regionFromContract('CA')).toBe('OTHER')
    expect(regionsToContract('CNHK')).toEqual(['CN', 'HK'])
    expect(regionsToContract('ALL')).toEqual([])
  })
  it('maps factor ids and weights (balance_sheet ↔ balanceSheet)', () => {
    const f = factorMap({ balance_sheet: 70, unknown: 1 })
    expect(f.balanceSheet).toBe(70)
    expect(Number.isNaN(f.quality)).toBe(true)
    expect(weightsToContract({ ...f, quality: 1, balanceSheet: 2, profitability: 0, growth: 0, value: 0, momentum: 0, insider: 0, sentiment: 0, macro: 0, moat: 0 })).toMatchObject({ balance_sheet: 2, quality: 1 })
  })
  it('builds context from taxonomy + factor meta', () => {
    expect(ctx.sectorName('TECH')).toBe('Technology')
    expect(ctx.subsectorName('software_application')).toBe('Application Software')
    expect(ctx.kpiProfileOfSubsector('semiconductors')).toBe('semis')
    expect(ctx.kpiProfileOfSubsector('banks_global')).toBe('bank')
    expect(ctx.weights.balanceSheet).toBeCloseTo(0.08)
    expect(ctx.sectorName('NEW_SECTOR')).toBe('New Sector')
  })
  it('normalises units and data-quality scores', () => {
    expect(unitFromContract('pct')).toBe('pct')
    expect(unitFromContract('x')).toBe('multiple')
    expect(unitFromContract('USD')).toBe('currency')
    expect(dqFromScore(95)).toBe('high')
    expect(dqFromScore(0.6)).toBe('medium')
    expect(dqFromScore(null)).toBe('medium')
  })
})

describe('market', () => {
  it('maps overview, regime, macro and credit series', () => {
    const m = adaptMarketOverview(fx.marketOverview, fx.macroRegime, [{ region: 'US', ov: fx.marketOverview }])
    expect(m.asOf).toBe('2026-10-09')
    expect(m.regime.current).toBe('Expansion')
    expect(m.regime.probability).toBeCloseTo(0.6)
    expect(m.regime.history).toHaveLength(2)
    expect(m.regime.drivers[0]).toMatchObject({ label: 'ISM PMI', direction: 'up' })
    expect(m.yieldCurves).toEqual([])
    expect(m.indicators).toHaveLength(1)
    expect(m.indicators[0].value).toBeCloseTo(0.043) // "Percent" → fraction
    expect(m.indicators[0].history).toHaveLength(3)
    expect(m.creditSpreads[0].valueBps).toBeCloseTo(84)
    // pre-1.29 key_macro credit: no calendar 1M change / percentile is fabricated (F09)
    expect(m.creditSpreads[0].change1dBps).toBeCloseTo(-2)
    expect(Number.isNaN(m.creditSpreads[0].change1mBps)).toBe(true)
    expect(Number.isNaN(m.creditSpreads[0].percentile)).toBe(true)
    expect(m.indices[0]).toMatchObject({ id: 'SPX', last: 6400, change1d: 0.004 })
    expect(m.breadth).toMatchObject({ pctAbove50d: 0.55, advancers: 300 })
    expect(m.sectorPerformance[0]).toMatchObject({ sector: 'Technology', region: 'US', return1m: 0.03 })
  })
  it('keeps stagflation as its own state and survives a missing regime', () => {
    const m = adaptMarketOverview({ ...fx.marketOverview, regime: null, key_macro: [] }, null, [])
    expect(m.regime.history).toEqual([])
    expect(Number.isNaN(m.regime.probability)).toBe(true)
    const m2 = adaptMarketOverview(fx.marketOverview, null, [])
    expect(m2.regime.probabilities.Slowdown).toBeCloseTo(0.25)
    expect(m2.regime.probabilities.Stagflation).toBeCloseTo(0.05)
  })
})

describe('screener & sectors', () => {
  it('builds the POST /screener/query body', () => {
    const q = buildScreenerQuery('CNHK', { weights: { quality: 10, profitability: 0, growth: 0, value: 0, balanceSheet: 5, momentum: 0, insider: 0, sentiment: 0, macro: 0, moat: 0 }, minScore: 60 })
    expect(q.regions).toEqual(['CN', 'HK'])
    expect(q.factor_weights).toMatchObject({ quality: 10, balance_sheet: 5 })
    expect(q.filters).toEqual([{ field: 'composite', op: 'gte', value: 60 }])
    expect(q.sort_by).toBe('composite')
    expect(buildScreenerQuery('ALL').regions).toBeUndefined()
  })
  it('maps screener rows', () => {
    const p = adaptScreener(fx.screenerResult, ctx)
    const r = p.rows[0]
    expect(r).toMatchObject({ id: 'S000123', ticker: 'MSFT', exchange: 'NASDAQ', isin: 'US5949181045', sector: 'Technology', subsector: 'Application Software', kpiProfile: 'software', moat: 'wide', dataQuality: 'high', rank: 14, return12m: 0.21, peNtm: 31.5, fcfYield: null })
    expect(r.factorScores.balanceSheet).toBe(66)
    expect(Number.isNaN(r.factorScores.momentum)).toBe(true)
    expect(r.marketCapBucket).toBe('mega')
    // coverage v3 A8: the page-level label only from the API's composite_label
    expect(p.compositeValidated).toBe(false)
    expect(adaptScreener({ ...fx.screenerResult, composite_label: 'validated' }, ctx).compositeValidated).toBe(true)
    expect(p.defaultWeights.balanceSheet).toBe(50)
  })
  it('maps search results', () => {
    expect(adaptSearch([fx.secMsft], ctx)[0]).toMatchObject({ id: 'S000123', exchange: 'NASDAQ', sector: 'Technology' })
  })
  it('maps the taxonomy tree with counts', () => {
    const t = adaptSectorTree(fx.taxonomy, fx.sectorSummaries)
    expect(t[0]).toMatchObject({ id: 'TECH', companyCount: 120 })
    expect(t[0].subsectors[0]).toMatchObject({ id: 'semiconductors', kpiProfile: 'semis', companyCount: 40 })
    expect(Number.isNaN(adaptSectorTree(fx.taxonomy, null)[0].companyCount)).toBe(true) // counts unknown → hidden
  })
  it('maps sector detail (quantiles only)', () => {
    const d = adaptSectorDetail(fx.sectorDetail, ctx, 'semiconductors', '2026-10-09')
    expect(d.subsector).toBe('Semiconductors')
    expect(d.kpiProfile).toBe('semis')
    expect(d.distributions[0]).toMatchObject({ median: 0.55, p10: 0.32, values: [] })
    expect(d.stats).toMatchObject({ companyCount: 40, medianScore: 58 })
    expect(d.leaders).toHaveLength(1)
  })
})

describe('company 360', () => {
  it('maps overview, ratios history and badges', () => {
    const o = adaptCompanyOverview(fx.companyOverview, fx.priceSeries, fx.ratios, ctx)
    expect(o.profile).toMatchObject({ id: 'S000123', isin: 'US5949181045', price: 515.2, compositeScore: 72.5, moat: 'wide', dataQuality: 'high', fiscalYearEnd: 'June', high52w: 555 })
    expect(o.factors.find((f) => f.family === 'balanceSheet')?.score).toBe(40)
    expect(o.factors.find((f) => f.family === 'quality')?.subFactors[0]).toEqual({ name: 'ROIC', score: 90 })
    expect(o.keyRatios[0]).toMatchObject({ id: 'roic', unit: 'pct', peerPercentile: 92, history: [0.25, 0.28, 0.31] })
    expect(o.alerts.map((a) => a.category)).toEqual(['fundamental', 'data', 'data'])
    expect(o.priceHistory).toHaveLength(260)
    // coverage v3 A8: per-company confirmation that the composite lies inside a passed gate's population
    expect(o.profile.compositeValidated).toBe(false)
    const ov = { ...fx.companyOverview, composite: { ...fx.companyOverview.composite, label: 'validated' as const, validated: true } }
    expect(adaptCompanyOverview(ov, fx.priceSeries, fx.ratios, ctx).profile.compositeValidated).toBe(true)
    const bad = { ...fx.companyOverview, composite: { ...fx.companyOverview.composite, label: 'descriptive' as const, validated: true } }
    expect(adaptCompanyOverview(bad, fx.priceSeries, fx.ratios, ctx).profile.compositeValidated).toBe(false)
    expect(o.priceHistory[0].time < o.priceHistory[1].time).toBe(true)
  })
  it('maps financial statements with per-period FX and millions', () => {
    const f = adaptFinancials(fx.financialsLocal, fx.financialsUsd, 'annual')
    expect(f.statements.map((s) => s.type)).toEqual(['income', 'balance', 'cashflow'])
    const is = f.statements[0]
    expect(is.periods).toEqual(['FY2024', 'FY2025'])
    expect(is.fxToUsd[0]).toBeCloseTo(1.08)
    expect(is.items.find((i) => i.id === 'revenue')).toMatchObject({ isTotal: true, kind: 'amount', values: [100, 120] })
    expect(is.items.find((i) => i.id === 'epsDiluted')).toMatchObject({ kind: 'perShare', values: [1.5, 1.8] })
    expect(f.statements[1].items[0].id).toBe('totalAssets')
    expect(f.statements[2].items[0].values).toEqual([30, null])
  })
  it('maps forecast scenarios', () => {
    const f = adaptForecast(fx.forecast)
    expect(f.projectionPeriods).toEqual(['FY2026E', 'FY2027E'])
    const rev = f.series.find((s) => s.metric === 'revenue')!
    expect(rev.historical.map((h) => h.value)).toEqual([100, 110])
    expect(rev.projections.bull[1].value).toBe(140)
    expect(f.series.find((s) => s.metric === 'eps')?.unit).toBe('number')
    expect(f.assumptions[0]).toMatchObject({ id: 'revenue_growth', unit: 'pct', historical5y: 0.1 })
    expect(f.reverseDcf).toMatchObject({ currentPrice: 500, impliedGrowth: 0.12, wacc: 0.085, fairValue: { bear: 400, base: 500, bull: 600 } })
  })
  it('maps peers, ranks and scatter axes', () => {
    const p = adaptPeers(fx.peers, 'S000123', ctx)
    expect(p.scatter).toEqual({ xMetric: 'ev_ebit', yMetric: 'roic' })
    expect(p.peers.find((r) => r.isSubject)?.ticker).toBe('MSFT')
    expect(p.subjectRankByMetric.roic).toEqual({ rank: 1, of: 2 })
    expect(p.subjectRankByMetric.ev_ebit).toEqual({ rank: 2, of: 3 })
  })
  it('maps moat detail', () => {
    const m = adaptMoat(fx.moat, 'MSFT')
    expect(m.rating).toBe('wide')
    expect(m.trend).toBe('widening')
    expect(m.sources.filter((s) => s.strength > 0).map((s) => s.source)).toEqual(['Switching costs', 'Network effects'])
    expect(m.evidence[0]).toMatchObject({ value: 91, passes: true })
    expect(m.roicVsWacc[0]).toEqual({ date: '2024-12-31', roic: 0.3, wacc: 0.085, hurdle: 0.085 })
  })
  it('maps insiders with signed values and cluster window', () => {
    const i = adaptCompanyInsiders(fx.companyInsiders, fx.priceSeries, { id: 'S000123', ticker: 'MSFT' })
    expect(i.anomalyScore).toBe(0.83)
    expect(i.clusterBuys[0]).toMatchObject({ date: '2026-10-01', insiders: 3 })
    const [buy, sell, grant] = i.transactions
    expect(buy).toMatchObject({ code: 'P', shares: 1000, valueUsd: 100000, isClusterBuy: true, role: 'CFO' })
    expect(sell).toMatchObject({ code: 'S', shares: -1000, valueUsd: -50000, isClusterBuy: false })
    expect(grant.code).toBe('A')
  })
  it('maps sentiment with price join and latest lazy-prices row', () => {
    const s = adaptCompanySentiment(fx.sentiment, fx.companyEvents, fx.priceSeries)
    expect(s.index[0]).toMatchObject({ sentiment: 0.12, newsVolume: 3, close: 400.4 })
    expect(s.index[1].sentiment).toBe(0.3)
    expect(s.news[0]).toMatchObject({ source: 'Reuters', sentiment: -0.5 })
    expect(s.lazyPricesSimilarity).toBe(0.95)
    expect(s.toneAvg30d).toBe(-2)
    expect(s.events[0].category).toBe('Conflict')
  })
  it('maps GDELT events', () => {
    expect(adaptGdeltEvent(fx.gdeltEvent)).toMatchObject({ id: '1234', category: 'Conflict', headline: 'Conflict: RUSSIA → UKRAINE', countries: ['UA'] })
  })
  it('maps technicals and computes indicators from bars', () => {
    const t = adaptTechnicals(fx.technicals, fx.priceSeries)
    expect(t.zones[0]).toMatchObject({ id: 'z1', kind: 'support', contributors: ['swing_pivot', 'ma_200'], touches: 4 })
    expect(t.setup).toMatchObject({ bias: 'long', entry: 482, invalidation: 474, target1: 530, riskReward: 6, zoneId: 'z1' })
    expect(t.pivots[0]).toEqual({ time: '2026-08-01', price: 480, kind: 'low', label: null })
    expect(t.levelStats[0]).toMatchObject({ type: 'ma_200', hitRate: 0.6, sampleSize: 900 })
    expect(Number.isFinite(t.indicators.ma200)).toBe(true)
    // no prices → one synthetic bar so the chart never sees an empty series
    expect(adaptTechnicals(fx.technicals, null).bars).toHaveLength(1)
  })
  it('maps model explanation', () => {
    const m = adaptCompanyModel(fx.modelExplanation)
    expect(m.predictions[0]).toMatchObject({ model: 'transformer', predictedRankPct: 81, horizonDays: 63 })
    expect(m.shap[0]).toMatchObject({ feature: 'accruals', family: 'quality', featureValue: '—' })
    expect(m.rankHistory[0].transformer).toBeCloseTo(70)
  })
})

describe('cross-sectional pages', () => {
  it('merges insider feed, top-signals and clusters', () => {
    const r = adaptInsiderRadar(fx.insiderFeed, fx.insiderSignals, fx.insiderClusters, ctx)
    expect(r).toHaveLength(3)
    expect(r[0]).toMatchObject({ ticker: 'MSFT', anomalyScore: 0.87, isClusterBuy: true })
    expect(r[0].reason).toMatch(/^Cluster buy: 3 insiders/)
    const tsmc = r.find((x) => x.companyId === 'S000777')!
    expect(tsmc).toMatchObject({ anomalyScore: 0.66, region: 'CNHK', code: 'S' })
    // the failed-gate insider XGBoost probability is no longer a "why flagged" reason (guide 07)
    expect(tsmc.reason).not.toMatch(/outperform/)
    expect(r.find((x) => x.companyId === 'S9')).toMatchObject({ source: 'Cluster', insiderName: 'D, E', isClusterBuy: true })
  })
  it('combines event map, timeline and feed with region filter', () => {
    const g = adaptGlobalEvents(fx.eventMap, fx.eventTimeline, fx.eventFeed, ctx, 'ALL', '2026-10-09')
    expect(g.countries[0]).toMatchObject({ country: 'UA', intensity: 1, eventCount: 400 })
    expect(g.countries[1].intensity).toBeCloseTo(0.5)
    expect(g.timeline.map((t) => t.date)).toEqual(['2026-10-01', '2026-10-02'])
    expect(g.events[0].affectedSectors).toEqual(['Financials'])
    expect(adaptGlobalEvents(fx.eventMap, [], fx.eventFeed, ctx, 'US', '2026-10-09').events.map((e) => e.countries[0])).toEqual(['US'])
  })
  it('builds Model Lab from runs + run detail', () => {
    const l = adaptModelLab(fx.modelRuns, fx.modelDetails)
    expect(l.runs).toHaveLength(2) // insider model excluded
    expect(l.runs[0]).toMatchObject({ model: 'xgboost', status: 'success', durationSec: 1200, nFeatures: 148, promoted: true })
    expect(l.icSeries).toHaveLength(2)
    expect(l.icSeries[0]).toMatchObject({ date: '2026-08-31', xgboostRankIc: 0.035 })
    expect(Number.isNaN(l.icSeries[0].transformerRankIc)).toBe(true)
    expect(l.deciles).toEqual([
      { decile: 1, xgboost: -0.02, transformer: NaN },
      { decile: 10, xgboost: 0.05, transformer: 0.06 },
    ])
    expect(l.featureImportance[0]).toMatchObject({ feature: 'bs', family: 'balanceSheet' })
    expect(l.folds[0]).toMatchObject({ fold: 1, testEnd: '2020-12-31', rankIc: 0.04 })
  })
  it('maps system status and derives freshness', () => {
    const s = adaptSystemStatus(fx.systemStatus, fx.jobRuns)
    expect(s.sources[1]).toMatchObject({ source: 'insider_transactions', status: 'stale', lagHours: 192 })
    expect(s.jobs[0]).toMatchObject({ job: 'dag:nightly', status: 'partial' })
    expect(s.jobs[1]).toMatchObject({ job: 'ingest_prices', status: 'failed', message: 'HTTP 429' })
    expect(adaptFreshness(fx.systemStatus)).toMatchObject({ status: 'stale', staleSources: ['insider_transactions'], asOf: '2026-10-09' })
    // v1.28: only a missing CRITICAL table is an error; optional realtime tables (live_*) never degrade the badge (F13)
    expect(adaptFreshness({ ...fx.systemStatus, data_freshness: [{ table: 'prices_daily', status: 'missing' }] }).status).toBe('error')
    expect(adaptFreshness({ ...fx.systemStatus, data_freshness: [{ table: 'x', status: 'missing' }] }).status).toBe('stale')
    expect(adaptFreshness({ ...fx.systemStatus, data_freshness: [{ table: 'prices_daily', status: 'fresh' }, { table: 'live_macro', status: 'missing' }] }).status).toBe('fresh')
    expect(adaptFreshness(fx.emptyStatus).status).toBe('stale')
  })
  it('round-trips watchlists and alert rules', () => {
    expect(watchlistIds(fx.watchlistDetail)).toEqual(['S000123'])
    const body = alertToContract({ companyId: 'S000123', ticker: 'MSFT', metric: 'price', operator: '<', threshold: 480, enabled: true })
    expect(body).toMatchObject({ security_id: 'S000123', rule_type: 'price_cross', params: { level: 480, direction: 'below', ui_metric: 'price' } })
    expect(alertFromContract(fx.alert)).toMatchObject({ id: 'a1', metric: 'price', operator: '<', threshold: 480, enabled: true })
    expect(alertFromContract({ ...fx.alert, params: body.params as Record<string, unknown> }).ticker).toBe('MSFT')
    // v1.7 Model Studio drift alerts carry the study, not a security
    const drift = alertFromContract({ ...fx.alert, rule_type: 'studio_drift', security_id: null, params: { study_id: 'ST1', study_name: 'Tech ENet' } })
    expect(drift).toMatchObject({ metric: 'studioDrift', ticker: 'Tech ENet', studyId: 'ST1', threshold: 20 })
  })
})

describe('contract v1.2 additions', () => {
  it('maps yield curves (percent → fraction, 2s10s) and spark_points', () => {
    const m = adaptMarketOverview(fx.overviewV12, null, [{ region: 'US', ov: fx.overviewV12 }])
    expect(m.yieldCurves[0]).toMatchObject({ country: 'US', spread2s10sBps: 47 })
    expect(m.yieldCurves[0].current[0]).toMatchObject({ tenor: '3M', years: 0.25 })
    expect(m.yieldCurves[0].policyRate).toBeNull()
    expect(m.yieldCurves[0].current[1].yield).toBeCloseTo(0.0475)
    expect(m.yieldCurves[0].oneYearAgo[1].yield).toBeCloseTo(0.0358)
    expect(m.indicators[0].history).toEqual([{ date: '2026-08-31', value: 1.05 }, { date: '2026-09-30', value: 1.1 }])
    expect(m.sectorPerformance[0].marketCapUsd).toBe(4.8e13)
    expect(m.dataStatus).toBe('ok')
  })
  it('maps model-based macro screener columns (macro-w4) and requests them', () => {
    expect(buildScreenerQuery('ALL').columns).toEqual(expect.arrayContaining(['macro_beta_rates', 'macro_beta_usd', 'macro_beta_oil', 'macro_beta_credit', 'macro_beta_growth_surprise', 'macro_impact_rates_down_100bp']))
    const values = { macro_beta_rates: -0.09, macro_beta_usd: -1.02, macro_beta_oil: 0.04, macro_beta_credit: -0.065, macro_beta_growth_surprise: 0.001, macro_impact_rates_down_100bp: 0.211 }
    const r = adaptScreener({ ...fx.screenerResult, rows: [{ ...fx.screenerRow, values }] }, ctx).rows[0]
    expect(r).toMatchObject({ mxBetaRates: -0.09, mxBetaUsd: -1.02, mxBetaOil: 0.04, mxBetaCredit: -0.065, mxBetaGrowthSurprise: 0.001, mxImpactRatesDown100: 0.211 })
    const empty = adaptScreener({ ...fx.screenerResult, rows: [{ ...fx.screenerRow, values: { macro_beta_rates: null } }] }, ctx).rows[0]
    expect(empty.mxBetaRates).toBeNull()
    expect(empty.mxImpactRatesDown100).toBeNull()
  })
  it('uses isin / exchange_name from SecurityRef', () => {
    const r = adaptScreener({ ...fx.screenerResult, rows: [{ ...fx.screenerRow, values: {}, security: { ...fx.secMsft, isin: 'US0378331005', exchange_name: 'Nasdaq GS' }, ml_expected_excess_3m: 0.012 }] }, ctx).rows[0]
    expect(r).toMatchObject({ isin: 'US0378331005', exchange: 'Nasdaq GS', modelExpectedExcess: 0.012 })
  })
  it('maps forecast fair-value blend, cost of capital and API sensitivity grid', () => {
    const f = adaptForecast(fx.forecastV12)
    expect(f.fairValueDetail).toMatchObject({ model: 'dcf', blended: 267, dcf: 185, multiples: 260, low: 185, high: 260, target12m: 265, confidence: 76.3, tvShare: 0.9, flags: ['high_tv_share'] })
    expect(f.fairValueDetail?.upsidePct).toBeCloseTo(267 / 336 - 1)
    expect(f.costOfCapital).toMatchObject({ wacc: 0.112, rfSource: 'fred:DGS10', beta: 1.2, betaRaw: 1.06 })
    expect(f.reverseDcf.sensitivity).toHaveLength(4)
    expect(f.reverseDcf.sensitivity[3]).toEqual({ wacc: 0.07, terminalGrowth: 0.03, fairValue: 240 })
    expect(f.assumptions.find((a) => a.id === 'drv_ebit_margin')?.label).toBe('ebit margin')
  })
  it('maps v1.2 moat components, typed sources and hurdle', () => {
    const m = adaptMoat(fx.moatV12, 'AAPL')
    expect(m.modelVariant).toBe('financial')
    expect(m.sources.find((x) => x.source === 'Efficient scale')?.strength).toBe(0.8)
    expect(m.evidence[1]).toMatchObject({ supplementary: true, score: 40, threshold: 60, passes: false })
    expect(m.evidence[0].history).toEqual([{ date: '2024-12-31', value: 0.17 }])
    expect(m.roicVsWacc[0].hurdle).toBe(0.09)
    expect(m.marketShare).toEqual([{ date: '2025-12-31', value: 0.3 }])
  })
  it('maps per-transaction anomaly scores, cluster ids and features', () => {
    const i = adaptCompanyInsiders(fx.insidersV12, null, { id: 'S000123', ticker: 'MSFT' })
    expect(i.transactions[0]).toMatchObject({ anomalyScore: 0.93, clusterId: 'S1:20260901', isClusterBuy: true, features: { role_weight: 1.5 } })
    expect(i.clusterBuys).toEqual([{ date: '2026-09-02', insiders: 1, valueUsd: 100000 }])
  })
  it('maps v1.2 technicals: setup, indicators, labelled pivots, reliability', () => {
    const t = adaptTechnicals(fx.technicalsV12, fx.priceSeries)
    expect(t.setup).toMatchObject({ bias: 'long', entryLow: 327.7, entryHigh: 336.6, target2: 363.6, technicalScore: 81.8, trend: 'up', structureState: 'bullish', lastBreak: { kind: 'bos', direction: 'up', date: '2026-07-15', price: 317.1 } })
    expect(t.setup.scoreComponents).toEqual([{ key: 'Trend', score: 100, weight: 0.25 }])
    expect(t.indicators).toMatchObject({ rsi14: 56.1, ma50: 322.4, ma200: 290.4, trend: 'up' })
    expect(t.pivots).toHaveLength(2) // duplicate k2/k3 swing collapsed
    expect(t.pivots.find((p) => p.label)).toMatchObject({ label: 'HH', kind: 'high' })
    expect(t.typeReliability?.[0]).toMatchObject({ type: 'gap', label: 'Gaps', edge: 0.0272 })
    expect(t.currency).toBe('USD')
  })
  it('maps models[] and SHAP unit', () => {
    const m = adaptCompanyModel({ ...fx.modelExplanation, shap_unit: 'raw score', models: [{ model_type: 'xgboost_rank', model_run_id: 'a', horizon: '3M', rank_pct: 0.2, decile: 2, expected_excess_return: -0.008, interval: { low: -0.03, high: 0.02 }, confidence: 0.68 }, { model_type: 'ft_transformer', model_run_id: 'b', horizon: '3M', rank_pct: 0.6, decile: 6 }] })
    expect(m.shapUnit).toBe('raw score')
    expect(m.predictions.map((p) => p.model)).toEqual(['xgboost', 'transformer'])
    expect(m.predictions[0]).toMatchObject({ expectedExcessReturn: -0.008, intervalLow: -0.03, confidence: 0.68 })
  })
  it('links events via securities[] and passes change_7d through as a fraction', () => {
    const g = adaptGlobalEvents([{ ...fx.eventMap[0], change_7d: 1.622 }, { ...fx.eventMap[1], change_7d: 0.12 }], [], { ...fx.eventFeed, items: [{ ...fx.gdeltEvent, headline: 'Real headline', securities: [{ security_id: 'S000123', ticker: 'MSFT' }] }] }, ctx, 'ALL', '2026-10-09')
    expect(g.countries[0].change7d).toBeCloseTo(1.622)
    expect(g.countries[1].change7d).toBeCloseTo(0.12)
    expect(g.events[0]).toMatchObject({ headline: 'Real headline', affectedCompanies: [{ id: 'S000123', ticker: 'MSFT' }] })
  })
  it('decodes HTML-escaped titles and keeps the 0–100 sentiment_score apart from the −1…+1 index', () => {
    expect(decodeEntities('&#x7F8E;&#x56FD; &amp; Co &#39;26')).toBe("美国 & Co '26")
    const s = adaptCompanySentiment({ ...fx.sentiment, sentiment_score: 25.7 }, null, null)
    expect(s.currentIndex).toBe(0.3) // latest daily value, not (25.7 − 50) / 50
    expect(s.score).toBe(25.7)
  })
  it('never shows reused freshness rows as fresh (coverage v3 A7 review)', () => {
    const st = {
      ...fx.systemStatus,
      data_freshness: [{ table: 'fx_rates', status: 'fresh' as const, last_date: '2026-11-06' }],
      cache: { state: 'partial' as const, ttl_s: 10, section_errors: ['data_freshness: ValueError: jobs.yaml invalid'], section_as_of: { data_freshness: '2026-11-07T09:55:00+00:00' } },
    }
    const s = adaptSystemStatus(st, null)
    expect(s.sources[0].status).toBe('stale')
    expect(s.sources[0].description).toContain('status from 2026-11-07T09:55:00+00:00')
    expect(s.statusCache?.state).toBe('partial')
    expect(adaptSystemStatus({ ...st, cache: { state: 'fresh', ttl_s: 10, section_errors: [] } }, null).sources[0].status).toBe('fresh')
  })
  it('maps freshness cadence / region / dq status', () => {
    const s = adaptSystemStatus({ ...fx.systemStatus, data_freshness: [{ table: 'prices_daily', status: 'fresh', expected_cadence: 'weekly', region: 'JP', dq_status: 'error', job_id: 'prices' }] }, null)
    expect(s.sources[0]).toMatchObject({ cadence: 'weekly', region: 'JP', dqStatus: 'error', status: 'error', jobId: 'prices' })
  })
})
