import type {
  CClusterBuy,
  CCompanyEvents,
  CCompanyInsiders,
  CCompanyOverview,
  CCompanyScores,
  CCompanySentiment,
  CCountryEventAgg,
  CEventTimelinePoint,
  CFactorMeta,
  CHealth,
  CFinancials,
  CForecast,
  CGdeltEventPage,
  CInsiderFeedPage,
  CInsiderSignalRow,
  CJobRunPage,
  CMacroRegime,
  CMarketOverview,
  CMetricTable,
  CModelExplanation,
  CModelRun,
  CModelRunDetail,
  CMoatDetail,
  CPeerComparison,
  CPriceSeries,
  CRegion,
  CScreenerResult,
  CSectorDetail,
  CSectorSummary,
  CSecurityRef,
  CSystemStatus,
  CTaxonomy,
  CTechnicals,
} from './contract'
import type {
  BackendHealth,
  CompanyFinancials,
  CompanyForecast,
  CompanyInsiders,
  CompanyModel,
  CompanyMoat,
  CompanyOverview,
  CompanyPeers,
  CompanySentiment,
  CompanyTechnicals,
  FactorModelMeta,
  FactorWeights,
  FreshnessSummary,
  GlobalEventsPayload,
  InsiderRadarItem,
  MarketOverview,
  ModelLabPayload,
  Periodicity,
  RegionCode,
  RegionFilter,
  ScreenerPayload,
  SearchResult,
  SectorDetail,
  SectorNode,
  SystemStatus,
} from './types'
import { type AdapterCtx, EMPTY_CTX, buildCtx, daysAgo, regionsToContract } from './adapters/common'
import {
  adaptBeneish,
  adaptCompanyInsiders,
  adaptCompanyModel,
  adaptCompanyOverview,
  adaptCompanySentiment,
  adaptFinancials,
  adaptForecast,
  adaptMoat,
  adaptPeers,
  adaptTechnicals,
} from './adapters/company'
import { modelKindOf } from './adapters/company'
import { adaptFactorMeta, adaptFreshness, adaptGlobalEvents, adaptHealth, adaptInsiderRadar, adaptModelLab, adaptSystemStatus, RANKING_MODELS } from './adapters/crossSection'
import { adaptMarketOverview } from './adapters/market'
import { type ScreenerOptions, adaptScreener, adaptSearch, adaptSectorDetail, adaptSectorTree, buildScreenerQuery } from './adapters/screener'
import { ApiError, isNetworkError, isPendingError } from './errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http, optional, queryParamEnum } from './http'

export { ApiError, API_BASE_URL, API_PREFIX, USE_MOCK }

/**
 * Data-access layer. Pages never call fetch directly — they use the hooks in ./hooks.ts which call `api`.
 *
 *   VITE_USE_MOCK=true  (default)  → deterministic in-browser mock (src/api/mock)
 *   VITE_USE_MOCK=false            → FastAPI backend (contracts/api.yaml) at VITE_API_URL, mapped by src/api/adapters
 *   VITE_MOCK_FALLBACK=true|a,b,c  → with the real API, endpoints answering 501 (not built yet) or unreachable
 *                                    fall back to the mock — for all endpoints (`true`) or the listed Api methods.
 */
export interface Api {
  marketOverview(region: RegionFilter): Promise<MarketOverview>
  sectorTree(region: RegionFilter): Promise<SectorNode[]>
  sectorDetail(sectorId: string, subsectorId: string | null, region: RegionFilter): Promise<SectorDetail>
  screener(region: RegionFilter, opts?: ScreenerOptions): Promise<ScreenerPayload>
  search(q: string): Promise<SearchResult[]>
  companyOverview(id: string): Promise<CompanyOverview>
  companyFinancials(id: string, periodicity: Periodicity): Promise<CompanyFinancials>
  companyForecast(id: string): Promise<CompanyForecast>
  /** peerSetId (v1.7): compare against a saved Model Studio peer set instead of subsector peers */
  companyPeers(id: string, peerSetId?: string | null): Promise<CompanyPeers>
  companyMoat(id: string): Promise<CompanyMoat>
  companyInsiders(id: string): Promise<CompanyInsiders>
  companySentiment(id: string): Promise<CompanySentiment>
  companyTechnicals(id: string): Promise<CompanyTechnicals>
  companyModel(id: string): Promise<CompanyModel>
  insiderRadar(region: RegionFilter): Promise<InsiderRadarItem[]>
  globalEvents(region: RegionFilter): Promise<GlobalEventsPayload>
  modelLab(): Promise<ModelLabPayload>
  systemStatus(): Promise<SystemStatus>
  freshness(): Promise<FreshnessSummary>
  health(): Promise<BackendHealth>
  factorModel(): Promise<FactorModelMeta>
}
export type ApiMethod = keyof Api

/* ───────────── shared context (/meta) & helpers ───────────── */

let ctxPromise: Promise<AdapterCtx> | null = null
let taxPromise: Promise<CTaxonomy | null> | null = null
const taxonomy = () => (taxPromise ??= optional(http.get<CTaxonomy>('/meta/taxonomy')))
function ctx(): Promise<AdapterCtx> {
  ctxPromise ??= Promise.all([taxonomy(), optional(http.get<CFactorMeta>('/meta/factors'))]).then(([t, m]) => (t || m ? buildCtx(t, m) : EMPTY_CTX))
  return ctxPromise
}

/** Weights the screener sends in its body; registered by the store so the client stays store-agnostic. */
let weightsProvider: (() => FactorWeights) | null = null
export function registerWeightsProvider(fn: () => FactorWeights): void {
  weightsProvider = fn
}

/** v1.1+: every `region` filter also accepts `regions` (repeated) — one call covers CN+HK or CA/APAC/OTHER. */
const regionQ = (region: RegionFilter): { regions?: CRegion[] } => {
  const rs = regionsToContract(region)
  return rs.length ? { regions: rs } : {}
}

/**
 * UI routes may carry a ticker (e.g. /company/MSFT); the contract wants internal security ids. An internal id is
 * exactly 'S' + 12 Crockford base32 chars (backend make_security_id); a looser test treated 568 live tickers (SHOP,
 * SAP, SBUX, SO …) as ids and sent them unresolved (404). Everything else, ETF:… ids included, goes through /search.
 */
export const SECURITY_ID_RE = /^S[0-9A-Z]{12}$/
const idCache = new Map<string, string>()
export async function resolveId(id: string): Promise<string> {
  if (SECURITY_ID_RE.test(id)) return id
  const hit = idCache.get(id)
  if (hit) return hit
  const rows = await optional(http.get<CSecurityRef[]>('/search', { q: id, limit: 5 }))
  const m = rows?.find((r) => r.ticker.toUpperCase() === id.toUpperCase()) ?? null
  const out = m?.security_id ?? id
  idCache.set(id, out)
  return out
}

/** Primary (non-ADR) listing of the same issuer, via /search on the issuer name. */
async function findHomeListing(sec: CSecurityRef): Promise<{ id: string; ticker: string; exchange: string } | null> {
  const q = sec.name.replace(/\(.*?\)|\bADRs?\b|\bADS\b/gi, '').trim()
  const rows = (await optional(http.get<CSecurityRef[]>('/search', { q, limit: 25 }))) ?? []
  const home = rows
    .filter((r) => r.issuer_id === sec.issuer_id && r.security_id !== sec.security_id && !/\bADRs?\b|\bADS\b/i.test(r.name) && !['XNYS', 'XNAS', 'ARCX', 'XASE', 'OTCM'].includes(r.mic ?? ''))
    .sort((a, b) => (b.market_cap_usd ?? 0) - (a.market_cap_usd ?? 0))[0]
  return home ? { id: home.security_id, ticker: home.ticker, exchange: home.exchange_name ?? home.mic ?? '' } : null
}

const enc = encodeURIComponent
const company = (sid: string, part: string) => `/companies/${enc(sid)}/${part}`
const prices = (sid: string, days: number) => optional(http.get<CPriceSeries>(company(sid, 'prices'), { from: daysAgo(days), interval: 'D' }))
const today = () => new Date().toISOString().slice(0, 10)

const UI_REGIONS: RegionCode[] = ['US', 'EU', 'UK', 'JP', 'KR', 'CNHK', 'LATAM']

/* ───────────── HTTP implementation (contract → adapters → UI types) ───────────── */

export const httpApi: Api = {
  async marketOverview(region) {
    // v1.29: send the region's own regime scope (KR, LATAM … answer "not modelled"); never map it to US.
    const scope = region === 'ALL' ? 'GLOBAL' : region === 'CNHK' ? 'CN' : region
    const regimeFor = async (sc: string): Promise<CMacroRegime | null> => {
      // an older API rejects scopes it does not know (422) → treat as not modelled without a failed request
      const allowed = await queryParamEnum('GET', '/macro/regime', 'scope')
      if (allowed && !allowed.includes(sc)) return null
      return optional(http.get<CMacroRegime>('/macro/regime', { scope: sc }))
    }
    // sector × region heatmap and breadth per region: one overview per region when "All" is selected
    const perRegion =
      region === 'ALL'
        ? Promise.all(
            UI_REGIONS.map(async (r) => {
              const o = await optional(http.get<CMarketOverview>('/market/overview', regionQ(r)))
              return o ? { region: r, ov: o } : null
            }),
          ).then((xs) => xs.filter((x): x is { region: RegionCode; ov: CMarketOverview } => x !== null))
        : null
    const [ov, regime0, byRegion] = await Promise.all([http.get<CMarketOverview>('/market/overview', regionQ(region)), regimeFor(scope), perRegion])
    let regime = regime0
    // no own model → the labelled GLOBAL fallback announced by the overview (v1.29 regime_scope)
    const rs = ov.regime_scope
    const usable = !!regime && regime.status !== 'not_modelled' && regime.modelled !== false && Object.keys(regime.current?.probabilities ?? {}).length > 0
    if (!usable && rs?.fallback && rs.scope && rs.scope !== scope) {
      regime = (await regimeFor(rs.scope)) ?? regime
    }
    return adaptMarketOverview(ov, regime, byRegion ?? [{ region: region as RegionCode, ov }], scope)
  },

  async sectorTree(region) {
    const [tax, sums] = await Promise.all([taxonomy(), optional(http.get<CSectorSummary[]>('/sectors', regionQ(region)))])
    if (!tax) throw new ApiError(503, `${API_BASE_URL}${API_PREFIX}/meta/taxonomy`, 'Taxonomy unavailable')
    return adaptSectorTree(tax, sums)
  },

  async sectorDetail(sectorId, subsectorId, region) {
    if (!sectorId) sectorId = (await taxonomy())?.sectors[0]?.id ?? 'TECH'
    const [c, d] = await Promise.all([ctx(), http.get<CSectorDetail>(`/sectors/${enc(sectorId)}`, { subsector_id: subsectorId, ...regionQ(region) })])
    return adaptSectorDetail(d, c, subsectorId, today())
  },

  async screener(region, opts) {
    const body = buildScreenerQuery(region, { weights: opts?.weights ?? weightsProvider?.(), ...opts })
    const [c, res] = await Promise.all([ctx(), http.post<CScreenerResult>('/screener/query', body)])
    return adaptScreener(res, c)
  },

  async search(q) {
    const [c, rows] = await Promise.all([ctx(), http.get<CSecurityRef[]>('/search', { q, limit: 20 })])
    return adaptSearch(rows, c)
  },

  async companyOverview(id) {
    const sid = await resolveId(id)
    const [c, ov, px, ratios] = await Promise.all([ctx(), http.get<CCompanyOverview>(company(sid, 'overview')), prices(sid, 380), optional(http.get<CMetricTable>(company(sid, 'ratios'), { period: 'FY', limit: 10 }))])
    const out = adaptCompanyOverview(ov, px, ratios, c)
    // v1.29 Beneish breakdown (model-based) — only fetched when a Beneish badge is raised
    if (ov.badges.some((b) => b.startsWith('beneish'))) {
      const sc = await optional(http.get<CCompanyScores>(company(sid, 'scores')))
      out.beneish = adaptBeneish(sc?.accounting_flags)
      // pre-1.29 snapshots: beneish_flag is the textbook rule (no recalibrated screen behind it) → say so
      if (!out.beneish)
        for (const a of out.alerts)
          if (a.code === 'beneish_flag') a.message = 'Beneish textbook screen (model-based): M-score above −1.78; often driven by sales growth alone (breakdown needs API 1.29)'
    }
    // ADRs carry no composite / technicals / fair value by design → point to the home listing
    const sec = ov.security
    const usLine = ['XNYS', 'XNAS', 'ARCX', 'XASE', 'BATS', 'OTCM'].includes(sec.mic ?? '')
    if (/\bADRs?\b|\bADS\b/i.test(sec.name) || (usLine && sec.country !== 'US' && ov.composite.score == null)) {
      out.profile.isAdr = true
      out.profile.homeListing = await findHomeListing(sec)
    }
    return out
  },

  async companyFinancials(id, periodicity) {
    const sid = await resolveId(id)
    const period = periodicity === 'annual' ? 'FY' : 'Q'
    const limit = periodicity === 'annual' ? 10 : 12
    const [local, usd] = await Promise.all([
      http.get<CFinancials>(company(sid, 'financials'), { statement: 'ALL', period, currency: 'LOCAL', limit }),
      optional(http.get<CFinancials>(company(sid, 'financials'), { statement: 'ALL', period, currency: 'USD', limit })),
    ])
    return adaptFinancials(local, usd, periodicity)
  },

  async companyForecast(id) {
    return adaptForecast(await http.get<CForecast>(company(await resolveId(id), 'forecast')))
  },

  async companyPeers(id, peerSetId) {
    const sid = await resolveId(id)
    const [c, p] = await Promise.all([ctx(), http.get<CPeerComparison>(company(sid, 'peers'), { limit: 15, peer_set_id: peerSetId || undefined })])
    return adaptPeers(p, sid, c)
  },

  async companyMoat(id) {
    const sid = await resolveId(id)
    return adaptMoat(await http.get<CMoatDetail>(company(sid, 'moat')), id)
  },

  async companyInsiders(id) {
    const sid = await resolveId(id)
    const [ci, px] = await Promise.all([http.get<CCompanyInsiders>(company(sid, 'insiders'), { days: 730 }), prices(sid, 760)])
    return adaptCompanyInsiders(ci, px, { id: sid, ticker: id })
  },

  async companySentiment(id) {
    const sid = await resolveId(id)
    const [cs, ev, px] = await Promise.all([
      http.get<CCompanySentiment>(company(sid, 'sentiment'), { days: 365 }),
      optional(http.get<CCompanyEvents>(company(sid, 'events'), { days: 200 })),
      prices(sid, 380),
    ])
    return adaptCompanySentiment(cs, ev, px)
  },

  async companyTechnicals(id) {
    const sid = await resolveId(id)
    const [t, px] = await Promise.all([http.get<CTechnicals>(company(sid, 'technicals')), prices(sid, 760)])
    return adaptTechnicals(t, px)
  },

  async companyModel(id) {
    return adaptCompanyModel(await http.get<CModelExplanation>(company(await resolveId(id), 'model'), { horizon: '3M' }))
  },

  async insiderRadar(region) {
    const [c, feed, signals, clusters] = await Promise.all([
      ctx(),
      http.get<CInsiderFeedPage>('/insiders/feed', { ...regionQ(region), days: 90, open_market_only: false, min_value_usd: 50000, limit: 500 }),
      optional(http.get<CInsiderSignalRow[]>('/insiders/top-signals', { ...regionQ(region), direction: 'bullish', limit: 200 })),
      optional(http.get<CClusterBuy[]>('/insiders/clusters', { ...regionQ(region), days: 90 })),
    ])
    return adaptInsiderRadar(feed, signals ?? [], clusters ?? [], c)
  },

  async globalEvents(region) {
    const [c, map, timeline, feed] = await Promise.all([
      ctx(),
      http.get<CCountryEventAgg[]>('/events/map', { days: 60 }),
      optional(http.get<CEventTimelinePoint[]>('/events/timeline', { days: 120 })),
      optional(http.get<CGdeltEventPage>('/events/feed', { days: 60, limit: 500 })),
    ])
    return adaptGlobalEvents(map, timeline ?? [], feed, c, region, today())
  },

  async modelLab() {
    const runs = await http.get<CModelRun[]>('/models/runs')
    // charts need the per-run evaluation series → one run per ranking-model family:
    // the production run if there is one, else the most recent completed run
    const pick: CModelRun[] = []
    for (const kind of ['xgboost', 'transformer'] as const) {
      const fam = runs.filter((r) => RANKING_MODELS.has(r.model_type) && modelKindOf(r.model_type) === kind && r.status === 'completed')
      const best = fam.find((r) => r.is_production) ?? [...fam].sort((a, b) => b.created_at.localeCompare(a.created_at))[0]
      if (best) pick.push(best)
    }
    const details = (await Promise.all(pick.map((r) => optional(http.get<CModelRunDetail>(`/models/runs/${enc(r.model_run_id)}`))))).filter((d): d is CModelRunDetail => d !== null)
    return adaptModelLab(runs, details)
  },

  async systemStatus() {
    const [st, runs] = await Promise.all([http.get<CSystemStatus>('/system/status'), optional(http.get<CJobRunPage>('/system/jobs/runs', { limit: 100 }))])
    return adaptSystemStatus(st, runs)
  },

  async freshness() {
    return adaptFreshness(await http.get<CSystemStatus>('/system/status'))
  },

  async health() {
    const [h, st] = await Promise.all([http.get<CHealth>('/health'), optional(http.get<CSystemStatus>('/system/status'))])
    return adaptHealth(h, st)
  },

  async factorModel() {
    return adaptFactorMeta(await http.get<CFactorMeta>('/meta/factors'))
  },
}

/* ───────────── mock fallback (per endpoint) ───────────── */

const FALLBACK_RAW = (import.meta.env.VITE_MOCK_FALLBACK ?? 'false').toString().trim()
const FALLBACK_ALL = FALLBACK_RAW.toLowerCase() === 'true'
const FALLBACK_LIST: string[] = FALLBACK_ALL || FALLBACK_RAW.toLowerCase() === 'false' ? [] : FALLBACK_RAW.split(',').map((s: string) => s.trim())
const FALLBACK_SET = new Set<string>(FALLBACK_LIST)
export const MOCK_FALLBACK_ENABLED = FALLBACK_ALL || FALLBACK_SET.size > 0
const fallbackAllowed = (m: ApiMethod) => FALLBACK_ALL || FALLBACK_SET.has(m)

/** Endpoints currently being served from the mock (observable for the top bar). */
type Listener = (s: ReadonlySet<ApiMethod>) => void
const fallbackActive = new Set<ApiMethod>()
const listeners = new Set<Listener>()
export function subscribeFallbacks(l: Listener): () => void {
  listeners.add(l)
  l(fallbackActive)
  return () => listeners.delete(l)
}
function markFallback(m: ApiMethod, on: boolean) {
  const had = fallbackActive.has(m)
  if (on && !had) fallbackActive.add(m)
  else if (!on && had) fallbackActive.delete(m)
  else return
  for (const l of listeners) l(new Set(fallbackActive))
}

let mockPromise: Promise<Api> | null = null
function loadMock(): Promise<Api> {
  mockPromise ??= import('./mock').then((m) => m.mockApi)
  return mockPromise
}

/** Proxy resolving each call against the mock, the HTTP API, or HTTP-with-mock-fallback. */
export const api: Api = new Proxy({} as Api, {
  get(_t, prop: ApiMethod) {
    return async (...args: unknown[]) => {
      const call = (impl: Api) => (impl[prop] as (...a: unknown[]) => Promise<unknown>)(...args)
      if (USE_MOCK) return call(await loadMock())
      try {
        const out = await call(httpApi)
        markFallback(prop, false)
        return out
      } catch (err) {
        if (!fallbackAllowed(prop) || !(isPendingError(err) || isNetworkError(err))) throw err
        try {
          const out = await call(await loadMock())
          markFallback(prop, true)
          return out
        } catch {
          // mock cannot serve this (e.g. unknown live security id) → surface the original "pending" error
          throw err
        }
      }
    }
  },
})
