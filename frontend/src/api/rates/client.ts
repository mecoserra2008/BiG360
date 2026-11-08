import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { CompanyDebt, CreditMaturityWalls, CreditOverview, RatesCurve, RatesCurveList, RatesMonitor, RatesSeries, RatesSovereign } from './types'

function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/rates`, 'operation Rates needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export interface CurveQuery {
  type?: string
  date?: string
  compare?: string
  historyTenors?: number[]
  from?: string | null
  to?: string | null
  frames?: 'M' | 'W' | 'none'
}

export const ratesApi = {
  async curves(group?: string): Promise<RatesCurveList> {
    live()
    return http.get('/rates/curves', { group })
  },
  async curve(id: string, q: CurveQuery = {}): Promise<RatesCurve> {
    live()
    return http.get(`/rates/curves/${encodeURIComponent(id)}`, {
      type: q.type,
      date: q.date,
      compare: q.compare,
      history_tenors: q.historyTenors?.length ? q.historyTenors.join(',') : undefined,
      from: q.from ?? undefined,
      to: q.to ?? undefined,
      frames: q.frames,
    })
  },
  async monitor(tenor = 120): Promise<RatesMonitor> {
    live()
    return http.get('/rates/monitor', { tenor })
  },
  async series(ids: string[], spreads: string[], from?: string | null, to?: string | null): Promise<RatesSeries> {
    live()
    return http.get('/rates/series', { ids: ids.join(','), spreads: spreads.join(','), from: from ?? undefined, to: to ?? undefined })
  },
  async sovereign(): Promise<RatesSovereign> {
    live()
    return http.get('/rates/sovereign')
  },
  async credit(years = 5): Promise<CreditOverview> {
    live()
    return http.get('/credit/overview', { years })
  },
  async walls(region = 'ALL', sectorId = 'ALL'): Promise<CreditMaturityWalls> {
    live()
    return http.get('/credit/maturity-walls', { region, sector_id: sectorId })
  },
  /** Company 360 → Debt. The route id may be a ticker (/company/AAPL): resolved to the security id like every other
   *  Company 360 tab (coverage v3 A7: the Debt tab used the raw route id and got a 404). */
  async companyDebt(securityId: string): Promise<CompanyDebt> {
    live()
    return http.get(`/companies/${encodeURIComponent(await resolveId(securityId))}/debt`)
  },
}
