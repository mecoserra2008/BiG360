import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { NewsFeeds, NewsHourly, NewsLiveHealth, NewsPage, NewsStoryDetail, NewsStoryPage } from './types'

/** The News Terminal is a live-API feature (bus + intraday store); the mock answers 501 -> "pending". */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/news`, 'operation News needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}
type Q = Record<string, string | number | undefined>

export const newsApi = {
  async terminal(q: Q): Promise<NewsPage> {
    live()
    return http.get('/news/terminal', q)
  },
  async stories(q: { hours?: number; sort?: 'salience' | 'velocity' | 'recent' | 'size'; limit?: number; country?: string; theme?: string; security_id?: string } = {}): Promise<NewsStoryPage> {
    live()
    return http.get('/news/stories', { hours: q.hours ?? 24, sort: q.sort ?? 'salience', limit: q.limit ?? 100, country: q.country, theme: q.theme, security_id: q.security_id })
  },
  async story(id: string): Promise<NewsStoryDetail> {
    live()
    return http.get(`/news/stories/${encodeURIComponent(id)}`)
  },
  async hourly(labelType: string, labelId: string, hours = 72): Promise<NewsHourly> {
    live()
    return http.get('/news/hourly', { label_type: labelType, label_id: labelId, hours })
  },
  async feeds(): Promise<NewsFeeds> {
    live()
    return http.get('/news/feeds')
  },
  async health(): Promise<NewsLiveHealth> {
    live()
    return http.get('/news/live/health')
  },
}
