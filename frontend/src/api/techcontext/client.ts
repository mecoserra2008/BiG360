import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { TechContext } from './types'

export const techContextApi = {
  async context(id: string): Promise<TechContext> {
    if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/technicals/{security_id}/context`, 'technicals context needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
    const sid = await resolveId(id)
    return http.get<TechContext>(`/technicals/${encodeURIComponent(sid)}/context`)
  },
}
