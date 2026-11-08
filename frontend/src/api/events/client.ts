import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { EmChangepoints, EmCompany, EmConnectedness, EmEscalation, EmGpr, EmHotspots, EmIrf, EmSpillovers, EmStudies, EvEventDates, EvLabelPanel, EvLabelType, EvMap, EvShocks, EvStoryDetail, EvStoryPage, EvTaxonomy, EvTimeline } from './types'

/** Events v2 / event models are live-API features (labelled GDELT, model outputs); the mock shows "pending". */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/events/v2`, 'operation Events v2 needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}
const enc = encodeURIComponent
type Q = Record<string, string | number | boolean | readonly string[] | null | undefined>

export const eventsApi = {
  async taxonomy(): Promise<EvTaxonomy> {
    live()
    return http.get('/events/v2/taxonomy')
  },
  async map(q: { start?: string; end?: string; days?: number; themes?: string[]; sliderDays?: number; sliderFreq?: 'day' | 'week' }): Promise<EvMap> {
    live()
    return http.get('/events/v2/map', {
      start: q.start,
      end: q.end,
      days: q.start ? undefined : (q.days ?? 7),
      theme: q.themes?.length ? q.themes : undefined,
      slider_days: q.sliderDays ?? 90,
      slider_freq: q.sliderFreq,
    })
  },
  async timeline(labelType: EvLabelType, labelId: string, q: Q = {}): Promise<EvTimeline> {
    live()
    return http.get('/events/v2/timeline', {
      label_type: labelType,
      label_id: labelId,
      ...q,
    })
  },
  async stories(q: Q): Promise<EvStoryPage> {
    live()
    return http.get('/events/v2/stories', q)
  },
  async story(id: string): Promise<EvStoryDetail> {
    live()
    return http.get(`/events/v2/stories/${enc(id)}`)
  },
  async label(type: EvLabelType, id: string, q: { days?: number; stories?: number; freq?: 'day' | 'week' } = {}): Promise<EvLabelPanel> {
    live()
    return http.get(`/events/v2/labels/${enc(type)}/${enc(id)}`, {
      days: q.days ?? 90,
      freq: q.freq,
      stories: q.stories ?? 10,
    })
  },
  async shocks(q: Q = {}): Promise<EvShocks> {
    live()
    return http.get('/events/v2/shocks', q)
  },
  async eventDates(labelType: EvLabelType, labelId: string, q: Q = {}): Promise<EvEventDates> {
    live()
    return http.get('/events/v2/event-dates', {
      label_type: labelType,
      label_id: labelId,
      ...q,
    })
  },
  async hotspots(q: { theme?: string; measure?: string; week?: string } = {}): Promise<EmHotspots> {
    live()
    return http.get('/events/models/hotspots', {
      theme: q.theme ?? 'all',
      measure: q.measure ?? 'attention',
      week: q.week,
    })
  },
  async spillovers(q: Q = {}): Promise<EmSpillovers> {
    live()
    return http.get('/events/models/spillovers', q)
  },
  async connectedness(q: Q = {}): Promise<EmConnectedness> {
    live()
    return http.get('/events/models/connectedness', q)
  },
  async irf(q: Q = {}): Promise<EmIrf> {
    live()
    return http.get('/events/models/irf', q)
  },
  async escalation(q: Q = {}): Promise<EmEscalation> {
    live()
    return http.get('/events/models/escalation', q)
  },
  async changepoints(q: Q = {}): Promise<EmChangepoints> {
    live()
    return http.get('/events/models/changepoints', q)
  },
  async gpr(q: Q = {}): Promise<EmGpr> {
    live()
    return http.get('/events/models/gpr', q)
  },
  async eventStudies(q: Q = {}): Promise<EmStudies> {
    live()
    return http.get('/events/models/event-studies', q)
  },
  async company(securityId: string): Promise<EmCompany> {
    live()
    return http.get(`/events/models/company/${enc(securityId)}`)
  },
}
