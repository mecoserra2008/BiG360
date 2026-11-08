import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { TmSetup } from './types'

export const techMetaApi = {
  async metaSetup(id: string, horizon = 20): Promise<TmSetup> {
    if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/technicals/{security_id}/meta-setup`, 'meta-labelled setups need the live API (set VITE_USE_MOCK=false)', 'Not implemented')
    const sid = await resolveId(id)
    return http.get<TmSetup>(`/technicals/${encodeURIComponent(sid)}/meta-setup?horizon=${horizon}`)
  },
}
