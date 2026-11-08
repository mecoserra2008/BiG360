/**
 * Contract (wire) types — re-exported from the file generated from contracts/api.yaml.
 * Regenerate with:  npx -y openapi-typescript@7.13.0 ../contracts/api.yaml -o src/api/generated/schema.d.ts
 * (run via npx because openapi-typescript's peer range is TypeScript ^5 and this app uses TS 6).
 */
import type { components, paths } from './generated/schema'

export type { components, paths }
type S = components['schemas']

export type CRegion = S['Region']
export type CFactorId = S['FactorId']
export type CMoatClass = S['MoatClass']
export type CEventCategory = S['EventCategory']
export type CInsiderTxnType = S['InsiderTxnType']
export type CModelType = S['ModelType']
export type CJobStatus = S['JobStatus']

export type CHealth = S['Health']
export type CSystemStatus = S['SystemStatus']
export type CFreshnessItem = S['FreshnessItem']
export type CJobRun = S['JobRun']
export type CJobRunPage = S['JobRunPage']

export type CSecurityRef = S['SecurityRef']
export type CTaxonomy = S['Taxonomy']
export type CFactorMeta = S['FactorMeta']
export type CMetricMeta = S['MetricMeta']

export type CMarketOverview = S['MarketOverview']
export type CMacroRegime = S['MacroRegime']
export type CRegimeSnapshot = S['RegimeSnapshot']
export type CMacroSeriesSummary = S['MacroSeriesSummary']
export type CSectorPerf = S['SectorPerf']

export type CSectorSummary = S['SectorSummary']
export type CSectorDetail = S['SectorDetail']
export type CKpiDistribution = S['KpiDistribution']

export type CScreenerQuery = S['ScreenerQuery']
export type CScreenerColumnId = S['ScreenerColumnId']
export type CDataStatus = S['DataStatus']
export type CTradeSetup = S['TradeSetup']
export type CCostOfCapital = S['CostOfCapital']
export type CScreenerRow = S['ScreenerRow']
export type CScreenerResult = S['ScreenerResult']

export type CCompanyOverview = S['CompanyOverview']
export type CCompanyScores = S['CompanyScores']
export type CPriceSeries = S['PriceSeries']
export type CPriceBar = S['PriceBar']
export type CFinancials = S['Financials']
export type CMetricTable = S['MetricTable']
export type CPeerComparison = S['PeerComparison']
export type CForecast = S['Forecast']
export type CMoatDetail = S['MoatDetail']
export type CInsiderTransaction = S['InsiderTransaction']
export type CCompanyInsiders = S['CompanyInsiders']
export type CCompanySentiment = S['CompanySentiment']
export type CCompanyEvents = S['CompanyEvents']
export type CGdeltEvent = S['GdeltEvent']
export type CTechnicals = S['Technicals']
export type CModelExplanation = S['ModelExplanation']

export type CInsiderFeedPage = S['InsiderFeedPage']
export type CInsiderSignalRow = S['InsiderSignalRow']
export type CClusterBuy = S['ClusterBuy']
export type CGdeltEventPage = S['GdeltEventPage']
export type CCountryEventAgg = S['CountryEventAgg']
export type CEventTimelinePoint = S['EventTimelinePoint']

export type CModelRun = S['ModelRun']
export type CModelRunDetail = S['ModelRunDetail']
export type CPredictionPage = S['PredictionPage']

export type CWatchlist = S['Watchlist']
export type CWatchlistDetail = S['WatchlistDetail']
export type CAlert = S['Alert']
export type CAlertCreate = S['AlertCreate']
export type CAlertRuleType = S['AlertRuleType']
export type CAlertEvent = S['AlertEvent']
export type CProblem = S['Problem']
