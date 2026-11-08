import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { ActivismQuery, CoOwnership, EuShortQuery, SmActivism, SmEuShorts, SmOverview, SmSectors } from './types'

/** Smart money is a live-API feature (official 13F / FINRA / SEC / EU registers); the mock shows "pending". */
function live(path: string): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'operation smart-money needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export const smartMoneyApi = {
  async overview(region?: string, limit = 15): Promise<SmOverview> {
    live('/smart-money/overview')
    return http.get<SmOverview>('/smart-money/overview', { region: region || undefined, limit })
  },
  async euShorts(q: EuShortQuery): Promise<SmEuShorts> {
    live('/smart-money/eu-shorts')
    return http.get<SmEuShorts>('/smart-money/eu-shorts', {
      regulator: q.regulator || undefined,
      q: q.q || undefined,
      holder: q.holder || undefined,
      current: q.current === false ? 'false' : undefined,
      position_type: q.positionType || undefined,
      limit: q.limit ?? 200,
      offset: q.offset || undefined,
    })
  },
  async activism(q: ActivismQuery): Promise<SmActivism> {
    live('/smart-money/activism')
    return http.get<SmActivism>('/smart-money/activism', { days: q.days ?? 90, event_types: q.eventTypes?.length ? q.eventTypes.join(',') : undefined })
  },
  async sectors(region?: string): Promise<SmSectors> {
    live('/smart-money/sectors')
    return http.get<SmSectors>('/smart-money/sectors', { region: region || undefined })
  },
  async ownership(id: string): Promise<CoOwnership> {
    live('/companies/{security_id}/ownership')
    const sid = await resolveId(id)
    return http.get<CoOwnership>(`/companies/${enc(sid)}/ownership`)
  },
}
