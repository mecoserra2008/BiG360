import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { TpScore, TpSetup, TpZones } from './types'

const live = (path: string) => {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'technicals probabilities need the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const techProbApi = {
  async setup(id: string, custom?: { target: number; stop: number; direction?: 'long' | 'short' }): Promise<TpSetup> {
    live('/technicals/{security_id}/setup-probabilities')
    const sid = await resolveId(id)
    const q = custom ? `?target=${custom.target}&stop=${custom.stop}${custom.direction ? `&direction=${custom.direction}` : ''}` : ''
    return http.get<TpSetup>(`/technicals/${encodeURIComponent(sid)}/setup-probabilities${q}`)
  },
  async zones(id: string, horizon = 20): Promise<TpZones> {
    live('/technicals/{security_id}/zones')
    const sid = await resolveId(id)
    return http.get<TpZones>(`/technicals/${encodeURIComponent(sid)}/zones?horizon=${horizon}`)
  },
  async score(id: string): Promise<TpScore> {
    live('/technicals/{security_id}/score-calibration')
    const sid = await resolveId(id)
    return http.get<TpScore>(`/technicals/${encodeURIComponent(sid)}/score-calibration`)
  },
}
