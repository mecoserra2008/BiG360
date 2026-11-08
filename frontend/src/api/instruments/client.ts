import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { InstrumentDetail, InstrumentList } from './types'

function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/instruments`, 'operation Instruments needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const instrumentsApi = {
  async list(type?: string): Promise<InstrumentList> {
    live()
    return http.get('/instruments', { type })
  },
  async detail(id: string): Promise<InstrumentDetail> {
    live()
    return http.get(`/instruments/${encodeURIComponent(id)}`)
  },
}
