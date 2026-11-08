/**
 * Macro v2 (api.yaml 1.8–1.9, /api/v1/macro/v2/*). Payloads are render data and keep the contract shape;
 * the pure helpers in ./adapters derive what the pages need (oriented z, calendar days, track records …).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type MCell = S['MacroV2Cell']
export type MHeatmap = S['MacroV2Heatmap']
export type MIndicator = S['MacroV2IndicatorMeta']
export type MCycle = S['MacroV2Cycle']
export type MCycleCountry = S['MacroV2CycleCountry']
export type MCountries = S['MacroV2Countries']
export type MCountrySummary = S['MacroV2CountrySummary']
export type MCountryPage = S['MacroV2CountryPage']
export type MSection = S['MacroV2Section']
export type MSectionSeries = S['MacroV2SectionSeries']
export type MTheme = S['MacroV2Theme']
export type MThemePanel = S['MacroV2ThemePanel']
export type MThemeSeries = S['MacroV2ThemeSeries']
export type MCompareItem = S['MacroV2CompareItem']
export type MCurve = S['MacroV2Curve']
export type MSeriesData = S['MacroV2SeriesData']
export type MCompare = S['MacroV2Compare']
export type MCalendar = S['MacroV2Calendar']
export type MCalendarItem = S['MacroV2CalendarItem']
export type MReleaseSeries = S['MacroV2ReleaseSeries']
export type MExpectation = S['MacroV2Expectation']
export type MReleaseDetail = S['MacroV2ReleaseDetail']
export type MSurprise = S['MacroV2Surprise']
export type MNowcast = S['MacroV2Nowcast']
export type MNowcastTarget = S['MacroV2NowcastTarget']
export type MNowcastModel = S['MacroV2NowcastModel']
export type MRecession = S['MacroV2Recession']
export type MRecessionRow = S['MacroV2RecessionRow']
export type MPoint = S['MacroV2Point']
export type MSurpriseHistoryPoint = S['MacroV2SurpriseHistoryPoint']
export type MRevisionStats = S['MacroV2RevisionStats']
export type MImportanceTier = MCalendar['importance_tiers'][number]

export type MacroTheme = 'inflation' | 'labor' | 'rates' | 'credit_conditions' | 'liquidity' | 'commodities_fx' | 'growth'
export type SeriesTransform = 'level' | 'yoy' | 'mom' | 'z' | 'canonical'
export type SurpriseTheme = 'all' | 'growth' | 'inflation' | 'labor'

/** Required wording wherever our forecasts are shown (user rule: never "consensus"). */
export const EXPECTATION_LABEL = 'Model-based expectation (not consensus)'

export const NOWCAST_COUNTRIES = ['US', 'EA', 'GB', 'JP'] as const

export const THEMES: { id: MacroTheme; label: string }[] = [
  { id: 'growth', label: 'Growth' },
  { id: 'inflation', label: 'Inflation' },
  { id: 'labor', label: 'Labor' },
  { id: 'rates', label: 'Rates & curves' },
  { id: 'credit_conditions', label: 'Credit & conditions' },
  { id: 'liquidity', label: 'Liquidity' },
  { id: 'commodities_fx', label: 'Commodities & FX' },
]

export interface CalendarDay {
  date: string
  items: MCalendarItem[]
}

export interface NowcastTrackRow {
  target: string
  actual: number | null
  ours: number | null
  gdpnow: number | null
  nyfed: number | null
  errOurs: number | null
  errGdpnow: number | null
  errNyfed: number | null
}

export interface RevisionOverlay {
  dates: string[]
  first: (number | null)[]
  latest: (number | null)[]
  /** values are in the requested transform (value_tf) rather than native levels */
  transformed: boolean
}
