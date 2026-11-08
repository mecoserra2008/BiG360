import { resolveId } from '../client'
import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { CalendarQuery, ECalendar, ECompany, ESeason } from './types'

/** The earnings calendar is a live-API feature (official filings + model-based expectations); the mock shows "pending". */
function live(path: string): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'operation earnings needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent

export const earningsApi = {
  async calendar(q: CalendarQuery): Promise<ECalendar> {
    live('/earnings/calendar')
    const ids = q.watchlistIds?.length ? await Promise.all(q.watchlistIds.map((x) => resolveId(x))) : undefined
    return http.get<ECalendar>('/earnings/calendar', {
      from: q.from,
      to: q.to,
      universe: q.universe || undefined,
      security_ids: ids?.join(',') || undefined,
      region: q.region || undefined,
      sector: q.sector || undefined,
      types: q.types?.length ? q.types.join(',') : undefined,
      date_source: q.dateSources?.length ? q.dateSources.join(',') : undefined,
    })
  },
  async company(id: string, history = 40): Promise<ECompany> {
    live('/companies/{security_id}/earnings')
    const sid = await resolveId(id)
    return http.get<ECompany>(`/companies/${enc(sid)}/earnings`, { history })
  },
  async season(season?: string, region?: string): Promise<ESeason> {
    live('/earnings/season')
    return http.get<ESeason>('/earnings/season', { season: season || undefined, region: region || undefined })
  },
}
