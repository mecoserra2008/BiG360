import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query'
import { resolveId } from '../client'
import { USE_MOCK } from '../http'
import { eventsApi } from './client'
import { type EvLabelType, type StoryFilter, storyQuery } from './types'

const M = 10 * 60_000
const H = 60 * 60_000
const sid = async (id: string) => (USE_MOCK ? id : resolveId(id))

export const useEvTaxonomy = () =>
  useQuery({
    queryKey: ['ev', 'taxonomy'],
    queryFn: () => eventsApi.taxonomy(),
    staleTime: 6 * H,
  })
export const useEvMap = (q: { start?: string; end?: string; days?: number; themes: string[]; sliderDays?: number; sliderFreq?: 'day' | 'week' }) =>
  useQuery({
    queryKey: ['ev', 'map', q],
    queryFn: () => eventsApi.map(q),
    staleTime: M,
    placeholderData: keepPreviousData,
  })
/** Country × theme matrix: one /map call per theme (same window). */
export const useEvThemeMaps = (themes: string[], window: { start?: string; end?: string; days?: number; sliderDays?: number }) =>
  useQueries({
    queries: themes.map((th) => ({
      queryKey: ['ev', 'map', { ...window, themes: [th] }],
      queryFn: () => eventsApi.map({ ...window, themes: [th] }),
      staleTime: M,
    })),
  })
export const useEvStories = (f: StoryFilter, limit = 40) =>
  useQuery({
    queryKey: ['ev', 'stories', f, limit],
    queryFn: async () =>
      eventsApi.stories(
        storyQuery(
          {
            ...f,
            securityId: f.securityId ? await sid(f.securityId) : undefined,
          },
          limit,
        ),
      ),
    staleTime: M,
    placeholderData: keepPreviousData,
  })
export const useEvStory = (id: string | null) =>
  useQuery({
    queryKey: ['ev', 'story', id],
    queryFn: () => eventsApi.story(id as string),
    enabled: !!id,
    staleTime: M,
  })
export const useEvLabel = (type: EvLabelType, id: string | null, days = 90, freq?: 'day' | 'week') =>
  useQuery({
    queryKey: ['ev', 'label', type, id, days, freq],
    queryFn: async () => eventsApi.label(type, type === 'issuer' ? await sid(id as string) : (id as string), { days, stories: 10, freq }),
    enabled: !!id,
    staleTime: M,
    placeholderData: keepPreviousData,
  })
export const useEvShocks = (days = 14) =>
  useQuery({
    queryKey: ['ev', 'shocks', days],
    queryFn: () => eventsApi.shocks({ days, min_z: 2, limit: 50 }),
    staleTime: M,
  })
export const useEvEventDates = (type: EvLabelType, id: string | null, theme: string, kind = 'any') =>
  useQuery({
    queryKey: ['ev', 'event-dates', type, id, theme, kind],
    queryFn: async () => eventsApi.eventDates(type, type === 'issuer' ? await sid(id as string) : (id as string), { theme: theme === 'all' ? undefined : theme, kind, min_z: 2 }),
    enabled: !!id,
    staleTime: M,
  })

export const useEmHotspots = (theme: string, measure: string, enabled = true) =>
  useQuery({
    queryKey: ['em', 'hotspots', theme, measure],
    queryFn: () => eventsApi.hotspots({ theme, measure }),
    staleTime: H,
    enabled,
    placeholderData: keepPreviousData,
  })
export const useEmSpillovers = (response: string, wKind: string) =>
  useQuery({
    queryKey: ['em', 'spillovers', response, wKind],
    queryFn: () => eventsApi.spillovers({ response, w_kind: wKind, limit: 10 }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
export const useEmConnectedness = (universe: string) =>
  useQuery({
    queryKey: ['em', 'connectedness', universe],
    queryFn: () => eventsApi.connectedness({ universe, pairs: 40 }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
export const useEmIrf = (response: string, model: string, scope: string) =>
  useQuery({
    queryKey: ['em', 'irf', response, model, scope],
    queryFn: () => eventsApi.irf({ response, model, scope }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
export const useEmEscalation = (themeGroup: string, sort: string) =>
  useQuery({
    queryKey: ['em', 'escalation', themeGroup, sort],
    queryFn: () =>
      eventsApi.escalation({
        theme_group: themeGroup === 'all' ? undefined : themeGroup,
        sort,
        limit: 50,
      }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
export const useEmChangepoints = (days = 30) =>
  useQuery({
    queryKey: ['em', 'changepoints', days],
    queryFn: () => eventsApi.changepoints({ days, limit: 100 }),
    staleTime: H,
  })
export const useEmGpr = (iso2: string | null, days = 365) =>
  useQuery({
    queryKey: ['em', 'gpr', iso2, days],
    queryFn: () => eventsApi.gpr({ iso2: iso2 ?? undefined, days, top: 30 }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
export const useEmStudies = (q: { theme?: string; iso2?: string; sector?: string; level?: string; shockKind: string; window: string }) =>
  useQuery({
    queryKey: ['em', 'studies', q],
    queryFn: () =>
      eventsApi.eventStudies({
        theme: q.theme || undefined,
        iso2: q.iso2 || undefined,
        sector: q.sector || undefined,
        level: q.level || undefined,
        shock_kind: q.shockKind,
        window: q.window,
        min_n: 3,
        limit: 100,
      }),
    staleTime: H,
    placeholderData: keepPreviousData,
  })
/** All windows of one cell (CAR path). */
export const useEmStudyPath = (
  q: {
    theme: string
    iso2: string
    sector: string
    level: string
    shockKind: string
  } | null,
  windows: string[],
) =>
  useQueries({
    queries: (q ? windows : []).map((w) => ({
      queryKey: ['em', 'studies', { ...q, window: w, path: true }],
      queryFn: () =>
        eventsApi.eventStudies({
          theme: q?.theme,
          iso2: q?.iso2,
          sector: q?.sector,
          level: q?.level,
          shock_kind: q?.shockKind,
          window: w,
          min_n: 2,
          limit: 100,
        }),
      staleTime: H,
    })),
  })
export const useEmCompany = (id: string) =>
  useQuery({
    queryKey: ['em', 'company', id],
    queryFn: async () => eventsApi.company(await sid(id)),
    staleTime: H,
  })
