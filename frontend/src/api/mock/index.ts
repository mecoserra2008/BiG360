import type { Api } from '../client'
import * as co from './company'
import * as xs from './crossSection'
import type { FactorWeights } from '../types'
import { DESCRIPTIVE_VALIDATION } from '@/lib/compositeLabel'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, FACTOR_LABEL } from '@/lib/factors'
import { marketOverview } from './market'
import { SUBFACTORS } from './taxonomy'

/** Simulated network latency so loading states are exercised in mock mode. */
function delay<T>(fn: () => T, ms = 120 + Math.random() * 220): Promise<T> {
  return new Promise((resolve, reject) => {
    setTimeout(() => {
      try {
        resolve(fn())
      } catch (err) {
        reject(err)
      }
    }, ms)
  })
}

export const mockApi: Api = {
  marketOverview: (region) => delay(() => marketOverview(region)),
  sectorTree: (region) => delay(() => xs.sectorTree(region), 80),
  sectorDetail: (sector, subsector, region) => delay(() => xs.sectorDetail(sector, subsector, region)),
  screener: (region) => delay(() => xs.screener(region)),
  search: (q) => delay(() => xs.search(q), 40),
  companyOverview: (id) => delay(() => co.overview(id)),
  companyFinancials: (id, periodicity) => delay(() => co.financials(id, periodicity)),
  companyForecast: (id) => delay(() => co.forecast(id)),
  companyPeers: (id) => delay(() => co.peers(id)), // peer sets are a live-API feature
  companyMoat: (id) => delay(() => co.moat(id)),
  companyInsiders: (id) => delay(() => co.insiders(id)),
  companySentiment: (id) => delay(() => co.sentiment(id)),
  companyTechnicals: (id) => delay(() => co.technicals(id)),
  companyModel: (id) => delay(() => co.model(id)),
  insiderRadar: (region) => delay(() => xs.insiderRadar(region)),
  globalEvents: (region) => delay(() => xs.globalEvents(region)),
  modelLab: () => delay(() => xs.modelLab()),
  systemStatus: () => delay(() => xs.systemStatus()),
  freshness: () => delay(() => xs.freshness(), 60),
  health: () =>
    delay(() => ({
      status: 'ok' as const,
      version: 'mock',
      time: new Date().toISOString(),
      servingRunId: 'mock',
      servingBuiltAt: null,
      keysPresent: { fred: true, polygon: true },
      universeCounts: { full: 970 },
      diskUsageGb: null,
    })),
  factorModel: () =>
    delay(() => ({
      defaultProfile: 'balanced',
      compositeValidation: DESCRIPTIVE_VALIDATION,
      profiles: { balanced: Object.fromEntries(Object.entries(DEFAULT_WEIGHTS).map(([k, v]) => [k, v / 100])) as FactorWeights },
      factors: FACTOR_FAMILIES.map((family) => ({ family, name: FACTOR_LABEL[family], description: SUBFACTORS[family].join(' · '), memberCount: SUBFACTORS[family].length })),
    })),
}
