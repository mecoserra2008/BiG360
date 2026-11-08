import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { CbDetail, CbImplied, CbOverview, CompanyMacro, ExposureEntity, ExposureModel, MxEpisode, MxEpisodes, MxExposures, MxFactors, MxRegimes, MxRegimeScopes, MxRunRequest, MxScenario, MxScenarioList, MxScenarioResult, MxShocks, RegimeType } from './v34'
import type { MCalendar, MCompare, MCountries, MCountryPage, MCycle, MHeatmap, MNowcast, MRecession, MReleaseDetail, MSeriesData, MSurprise, MTheme, MacroTheme, SeriesTransform, SurpriseTheme } from './types'

/** Macro v2 is a live-API feature (official data, PIT snapshots); the mock shows "pending". */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/macro/v2`, 'operation Macro v2 needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent
const P = '/macro/v2'

export const macroApi = {
  async heatmap(opts: { countries?: string[]; asOf?: string } = {}): Promise<MHeatmap> {
    live()
    return http.get<MHeatmap>(`${P}/heatmap`, { countries: opts.countries?.join(',') || undefined, as_of: opts.asOf })
  },
  async cycle(trail = 12): Promise<MCycle> {
    live()
    return http.get<MCycle>(`${P}/cycle`, { trail })
  },
  async countries(): Promise<MCountries> {
    live()
    return http.get<MCountries>(`${P}/countries`)
  },
  async country(cc: string, years = 10): Promise<MCountryPage> {
    live()
    return http.get<MCountryPage>(`${P}/countries/${enc(cc)}`, { years })
  },
  async theme(theme: MacroTheme, years = 10): Promise<MTheme> {
    live()
    return http.get<MTheme>(`${P}/themes/${enc(theme)}`, { years })
  },
  async series(id: string, opts: { transform?: SeriesTransform; from?: string; vintages?: boolean } = {}): Promise<MSeriesData> {
    live()
    return http.get<MSeriesData>(`${P}/series/${enc(id)}`, { transform: opts.transform, from: opts.from, vintages: opts.vintages })
  },
  async compare(indicator: string, countries?: string[], years = 10): Promise<MCompare> {
    live()
    return http.get<MCompare>(`${P}/compare`, { indicator, countries: countries?.join(',') || undefined, years })
  },
  async calendar(opts: { from?: string; to?: string; countries?: string[]; importance?: number; kind?: 'data' | 'policy_meeting' } = {}): Promise<MCalendar> {
    live()
    return http.get<MCalendar>(`${P}/calendar`, { from: opts.from, to: opts.to, countries: opts.countries?.join(',') || undefined, importance: opts.importance, kind: opts.kind })
  },
  async release(id: string, history = 24): Promise<MReleaseDetail> {
    live()
    return http.get<MReleaseDetail>(`${P}/releases/${enc(id)}`, { history })
  },
  async surprise(opts: { countries?: string[]; theme?: SurpriseTheme; from?: string; freq?: 'D' | 'W' } = {}): Promise<MSurprise> {
    live()
    return http.get<MSurprise>(`${P}/surprise`, { countries: opts.countries?.join(',') || undefined, theme: opts.theme, from: opts.from, freq: opts.freq ?? 'W' })
  },
  async nowcast(cc: string, quarters = 4): Promise<MNowcast> {
    live()
    return http.get<MNowcast>(`${P}/nowcast/${enc(cc)}`, { quarters })
  },
  async recession(opts: { countries?: string[]; historyMonths?: number } = {}): Promise<MRecession> {
    live()
    return http.get<MRecession>(`${P}/recession`, { countries: opts.countries?.join(',') || undefined, history_months: opts.historyMonths })
  },

  /* ── v3 central banks ── */
  async centralBanks(): Promise<CbOverview> {
    live()
    return http.get<CbOverview>('/macro/v3/central-banks')
  },
  async centralBank(bank: string, historyYears = 10): Promise<CbDetail> {
    live()
    return http.get<CbDetail>(`/macro/v3/central-banks/${enc(bank)}`, { history_years: historyYears })
  },
  async impliedPaths(): Promise<CbImplied> {
    live()
    return http.get<CbImplied>('/macro/v3/implied-paths')
  },
  /* ── v4 macro → markets ── */
  async mxFactors(frequency: 'D' | 'W' | 'M' = 'M', history = 0): Promise<MxFactors> {
    live()
    return http.get<MxFactors>('/macro/v4/factors', { frequency, history })
  },
  async exposures(opts: { entityType: ExposureEntity; model: ExposureModel; entityId?: string; history?: boolean; limit?: number }): Promise<MxExposures> {
    live()
    return http.get<MxExposures>('/macro/v4/exposures', { entity_type: opts.entityType, model: opts.model, entity_id: opts.entityId, history: opts.history, limit: opts.limit ?? 500 })
  },
  async regimePerformance(opts: { regimeType: RegimeType; scope: string; entityType: string; timing: 'contemporaneous' | 'next_month'; metric?: 'excess' | 'relative' }): Promise<MxRegimes> {
    live()
    return http.get<MxRegimes>('/macro/v4/regime-performance', { regime_type: opts.regimeType, scope: opts.scope, entity_type: opts.entityType, timing: opts.timing, metric: opts.metric ?? 'relative' })
  },
  async regimeScopes(): Promise<MxRegimeScopes> {
    live()
    return http.get<MxRegimeScopes>('/macro/v4/regime-scopes')
  },
  async scenarios(): Promise<MxScenarioList> {
    live()
    return http.get<MxScenarioList>('/macro/v4/scenarios')
  },
  async runScenario(body: MxRunRequest): Promise<MxScenarioResult> {
    live()
    return http.post<MxScenarioResult>('/macro/v4/scenarios/run', body)
  },
  async createScenario(body: { name: string; description?: string | null; shocks: MxShocks; options?: MxRunRequest['options'] }): Promise<MxScenario> {
    live()
    return http.post<MxScenario>('/macro/v4/scenarios', body)
  },
  async updateScenario(id: string, body: { name?: string; shocks?: MxShocks; options?: MxRunRequest['options'] }): Promise<MxScenario> {
    live()
    return http.put<MxScenario>(`/macro/v4/scenarios/${enc(id)}`, body)
  },
  async deleteScenario(id: string): Promise<void> {
    live()
    await http.del(`/macro/v4/scenarios/${enc(id)}`)
  },
  async episodes(): Promise<MxEpisodes> {
    live()
    return http.get<MxEpisodes>('/macro/v4/episodes')
  },
  async episode(id: string): Promise<MxEpisode> {
    live()
    return http.get<MxEpisode>(`/macro/v4/episodes/${enc(id)}`)
  },
  async companyMacro(securityId: string): Promise<CompanyMacro> {
    live()
    return http.get<CompanyMacro>(`/companies/${enc(securityId)}/macro`)
  },
}
