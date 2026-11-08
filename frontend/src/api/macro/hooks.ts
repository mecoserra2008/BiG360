import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { macroApi } from './client'
import { resolveId } from '../client'
import { USE_MOCK } from '../http'
import type { MacroTheme, SeriesTransform, SurpriseTheme } from './types'
import type { ExposureEntity, ExposureModel, RegimeType } from './v34'

const H = 30 * 60_000

export const mk = {
  heatmap: () => ['macro', 'heatmap'] as const,
  cycle: () => ['macro', 'cycle'] as const,
  countries: () => ['macro', 'countries'] as const,
  country: (cc: string) => ['macro', 'country', cc] as const,
  theme: (t: string, years: number) => ['macro', 'theme', t, years] as const,
  series: (id: string, tf: string, vint: boolean) => ['macro', 'series', id, tf, vint] as const,
  compare: (ind: string, cs: string) => ['macro', 'compare', ind, cs] as const,
  calendar: (from: string, to: string) => ['macro', 'calendar', from, to] as const,
  release: (id: string) => ['macro', 'release', id] as const,
  surprise: (theme: string, cs: string) => ['macro', 'surprise', theme, cs] as const,
  nowcast: (cc: string) => ['macro', 'nowcast', cc] as const,
  recession: () => ['macro', 'recession'] as const,
}

export const useMacroHeatmap = () => useQuery({ queryKey: mk.heatmap(), queryFn: () => macroApi.heatmap(), staleTime: H })
export const useMacroCycle = () => useQuery({ queryKey: mk.cycle(), queryFn: () => macroApi.cycle(12), staleTime: H })
export const useMacroCountries = () => useQuery({ queryKey: mk.countries(), queryFn: () => macroApi.countries(), staleTime: H })
export const useMacroCountry = (cc: string) => useQuery({ queryKey: mk.country(cc), queryFn: () => macroApi.country(cc), staleTime: H })
export const useMacroTheme = (t: MacroTheme, years = 10) => useQuery({ queryKey: mk.theme(t, years), queryFn: () => macroApi.theme(t, years), staleTime: H, placeholderData: keepPreviousData })
export const useMacroSeries = (id: string | null, transform: SeriesTransform = 'level', vintages = false) =>
  useQuery({ queryKey: mk.series(id ?? '', transform, vintages), queryFn: () => macroApi.series(id as string, { transform, vintages }), enabled: !!id, staleTime: H, placeholderData: keepPreviousData })
export const useMacroCompare = (indicator: string | null, countries: string[]) =>
  useQuery({ queryKey: mk.compare(indicator ?? '', countries.join(',')), queryFn: () => macroApi.compare(indicator as string, countries), enabled: !!indicator, staleTime: H, placeholderData: keepPreviousData })
export const useMacroCalendar = (from: string, to: string) => useQuery({ queryKey: mk.calendar(from, to), queryFn: () => macroApi.calendar({ from, to }), staleTime: 10 * 60_000, placeholderData: keepPreviousData })
export const useMacroRelease = (id: string | null) => useQuery({ queryKey: mk.release(id ?? ''), queryFn: () => macroApi.release(id as string), enabled: !!id, staleTime: H })
export const useMacroSurprise = (theme: SurpriseTheme, countries: string[] = []) =>
  useQuery({ queryKey: mk.surprise(theme, countries.join(',')), queryFn: () => macroApi.surprise({ theme, countries }), staleTime: H, placeholderData: keepPreviousData })
export const useMacroNowcast = (cc: string) => useQuery({ queryKey: mk.nowcast(cc), queryFn: () => macroApi.nowcast(cc, 4), staleTime: H, retry: false })
export const useMacroRecession = () => useQuery({ queryKey: mk.recession(), queryFn: () => macroApi.recession({ historyMonths: 120 }), staleTime: H })
/** NBER US recession indicator (official, FRED USREC) for chart shading. */
export const useUsRecessions = () => useQuery({ queryKey: ['macro', 'usrec'], queryFn: () => macroApi.series('fred:USREC', { transform: 'level', from: '1990-01-01' }), staleTime: 24 * 3600_000, retry: false })

/* ── v3 central banks / v4 macro → markets ── */
export const useCentralBanks = () => useQuery({ queryKey: ['macro', 'cb'], queryFn: () => macroApi.centralBanks(), staleTime: H })
export const useCentralBank = (bank: string) => useQuery({ queryKey: ['macro', 'cb', bank], queryFn: () => macroApi.centralBank(bank), staleTime: H })
export const useImpliedPaths = () => useQuery({ queryKey: ['macro', 'implied'], queryFn: () => macroApi.impliedPaths(), staleTime: H })
export const useExposures = (entityType: ExposureEntity, model: ExposureModel) =>
  useQuery({ queryKey: ['macro', 'exposures', entityType, model], queryFn: () => macroApi.exposures({ entityType, model }), staleTime: H, placeholderData: keepPreviousData })
export const useExposureHistory = (entityType: ExposureEntity, model: ExposureModel, entityId: string | null) =>
  useQuery({ queryKey: ['macro', 'exposures', entityType, model, entityId], queryFn: () => macroApi.exposures({ entityType, model, entityId: entityId as string, history: true }), enabled: !!entityId, staleTime: H })
export const useRegimeScopes = () => useQuery({ queryKey: ['macro', 'regime-scopes'], queryFn: () => macroApi.regimeScopes(), staleTime: H })
export const useRegimePerformance = (regimeType: RegimeType, scope: string, entityType: string, timing: 'contemporaneous' | 'next_month', enabled = true) =>
  useQuery({ queryKey: ['macro', 'regimes', regimeType, scope, entityType, timing], queryFn: () => macroApi.regimePerformance({ regimeType, scope, entityType, timing }), staleTime: H, placeholderData: keepPreviousData, enabled })
export const useScenarios = () => useQuery({ queryKey: ['macro', 'scenarios'], queryFn: () => macroApi.scenarios(), staleTime: 60_000 })
export const useEpisodes = () => useQuery({ queryKey: ['macro', 'episodes'], queryFn: () => macroApi.episodes(), staleTime: H })
export const useEpisode = (id: string | null) => useQuery({ queryKey: ['macro', 'episode', id], queryFn: () => macroApi.episode(id as string), enabled: !!id, staleTime: H })
export const useCompanyMacro = (id: string) =>
  useQuery({ queryKey: ['macro', 'company', id], queryFn: async () => macroApi.companyMacro(USE_MOCK ? id : await resolveId(id)), staleTime: H })
