import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { NetworkQuery, ScCountryRisk, ScGeography, ScNetwork, ScPortfolio, ScPortfolioRequest, ScShockRequest, ScShockResult } from './types'

/** Supply-chain data comes from the live API (filings, models); the mock shows "pending". */
function live(path: string): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'operation supply-chain needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export const supplyChainApi = {
  async network(id: string, q: NetworkQuery): Promise<ScNetwork> {
    live('/companies/{security_id}/network')
    const sid = await resolveId(id)
    return http.get<ScNetwork>(`/companies/${enc(sid)}/network`, {
      depth: q.depth,
      include_statistical: q.includeStatistical ? 'true' : 'false',
      statistical_method: q.statisticalMethod,
      min_confidence: q.minConfidence || undefined,
    })
  },
  async geography(id: string): Promise<ScGeography> {
    live('/companies/{security_id}/geography')
    const sid = await resolveId(id)
    return http.get<ScGeography>(`/companies/${enc(sid)}/geography`)
  },
  async countryRisk(): Promise<ScCountryRisk> {
    live('/supply-chain/country-risk')
    return http.get<ScCountryRisk>('/supply-chain/country-risk')
  },
  async shock(body: ScShockRequest): Promise<ScShockResult> {
    live('/supply-chain/shock')
    const b = body.security_id ? { ...body, security_id: await resolveId(body.security_id) } : body
    return http.post<ScShockResult>('/supply-chain/shock', b)
  },
  async portfolio(body: ScPortfolioRequest): Promise<ScPortfolio> {
    live('/supply-chain/portfolio-exposure')
    return http.post<ScPortfolio>('/supply-chain/portfolio-exposure', body)
  },
}
