import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { Tv3Breakout, Tv3Factors } from './types'

const live = (path: string) => {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'technical factors need the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const techV3Api = {
  async factors(id: string): Promise<Tv3Factors> {
    live('/technicals/{security_id}/factors')
    const sid = await resolveId(id)
    return http.get<Tv3Factors>(`/technicals/${encodeURIComponent(sid)}/factors`)
  },
  async breakout(id: string): Promise<Tv3Breakout> {
    live('/technicals/{security_id}/breakout')
    const sid = await resolveId(id)
    return http.get<Tv3Breakout>(`/technicals/${encodeURIComponent(sid)}/breakout`)
  },
}
