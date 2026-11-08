import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { LiveFeeds, LiveQuotesPage } from './quotes'
import type { LiveAlert, LiveFiling, LiveMacro, LiveNews, LivePage, LiveStatus } from './types'

function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/live`, 'operation Live needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const liveApi = {
  async status(): Promise<LiveStatus> {
    live()
    return http.get('/live/status')
  },
  async filings(q: { limit?: number; securityId?: string } = {}): Promise<LivePage<LiveFiling>> {
    live()
    return http.get('/live/filings', {
      limit: q.limit ?? 30,
      security_id: q.securityId,
    })
  },
  async news(limit = 30): Promise<LivePage<LiveNews>> {
    live()
    return http.get('/live/news', { limit })
  },
  async macro(limit = 30, country?: string): Promise<LivePage<LiveMacro>> {
    live()
    return http.get('/live/macro', { limit, country })
  },
  async alerts(limit = 30): Promise<LivePage<LiveAlert>> {
    live()
    return http.get('/live/alerts', { limit })
  },
  /** workstream B: last delayed quotes (ids = security ids, tickers or ^GSPC-style symbols) */
  async quotes(ids: string[], set?: 'header', record = true): Promise<LiveQuotesPage> {
    live()
    return http.get('/live/quotes', { ids: ids.length ? ids.join(',') : undefined, set, record: record ? undefined : false })
  },
  /** workstream B: per-feed health of the live fetchers (Data Health > Live feeds) */
  async feeds(): Promise<LiveFeeds> {
    live()
    return http.get('/live/feeds')
  },
}
