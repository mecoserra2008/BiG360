/**
 * Shared, pure mapping helpers: contract (snake_case wire format) → UI view-model vocabulary.
 * Nothing in src/api/adapters performs I/O; every function is deterministic and unit-tested.
 */
import type { CFactorId, CFactorMeta, CMoatClass, CPriceBar, CRegion, CSecurityRef, CTaxonomy } from '../contract'
import type {
  CompanySummary,
  DataQuality,
  FactorFamily,
  FactorScores,
  FactorWeights,
  KpiProfile,
  MarketCapBucket,
  MoatRating,
  PriceBar,
  RegionCode,
  RegionFilter,
  ValueUnit,
} from '../types'

/* ───────────── context derived from /meta endpoints ───────────── */

export interface AdapterCtx {
  sectorName(id: string | null | undefined): string
  subsectorName(id: string | null | undefined): string
  sectorOfSubsector(id: string | null | undefined): string | null
  kpiProfileOfSubsector(id: string | null | undefined): KpiProfile
  /** default-profile factor weights (fractions) from /meta/factors */
  weights: FactorWeights
}

/** contract kpi_set_id → UI KPI profile (contracts/sector_kpis.yaml) */
export const KPI_SET_TO_PROFILE: Record<string, KpiProfile> = {
  software_internet: 'software',
  semis: 'semis',
  hardware: 'general',
  services: 'general',
  telecom: 'utility',
  media: 'general',
  autos_durables: 'general',
  homebuilders: 'general',
  retail: 'retail',
  consumer_brands: 'general',
  restaurants_leisure: 'general',
  pharma_biotech: 'pharma',
  medtech: 'general',
  managed_care: 'general',
  banks: 'bank',
  consumer_finance: 'bank',
  insurance_pc: 'insurance',
  insurance_life: 'insurance',
  asset_light_fin: 'general',
  holdings: 'general',
  industrials: 'general',
  distributors: 'general',
  transport: 'general',
  materials: 'general',
  energy: 'energy',
  utilities: 'utility',
  reits: 'reit',
  real_estate_dev: 'reit',
}

const EQUAL_WEIGHTS: FactorWeights = {
  quality: 0.1,
  profitability: 0.1,
  growth: 0.1,
  value: 0.1,
  balanceSheet: 0.1,
  momentum: 0.1,
  insider: 0.1,
  sentiment: 0.1,
  macro: 0.1,
  moat: 0.1,
}

const titleCase = (s: string) =>
  s
    .replace(/[_-]+/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase())

export function buildCtx(tax: CTaxonomy | null, meta: CFactorMeta | null): AdapterCtx {
  const sectors = new Map<string, string>()
  const subs = new Map<string, { name: string; sector: string; kpi: string | undefined }>()
  for (const s of tax?.sectors ?? []) {
    sectors.set(s.id, s.name)
    for (const sub of s.subsectors) subs.set(sub.id, { name: sub.name, sector: s.id, kpi: sub.kpi_set_id })
  }
  let weights = EQUAL_WEIGHTS
  if (meta) {
    const prof = meta.profiles[meta.default_profile]
    if (prof) weights = factorMap(prof, 0)
  }
  return {
    sectorName: (id) => (id ? (sectors.get(id) ?? titleCase(id)) : 'Unclassified'),
    subsectorName: (id) => (id ? (subs.get(id)?.name ?? titleCase(id)) : 'Unclassified'),
    sectorOfSubsector: (id) => (id ? (subs.get(id)?.sector ?? null) : null),
    kpiProfileOfSubsector: (id) => KPI_SET_TO_PROFILE[(id && subs.get(id)?.kpi) || ''] ?? 'general',
    weights,
  }
}

export const EMPTY_CTX: AdapterCtx = buildCtx(null, null)

/* ───────────── enums ───────────── */

const C2U_REGION: Record<CRegion, RegionCode> = {
  US: 'US',
  CA: 'OTHER',
  LATAM: 'LATAM',
  UK: 'UK',
  EU: 'EU',
  JP: 'JP',
  KR: 'KR',
  CN: 'CNHK',
  HK: 'CNHK',
  APAC: 'OTHER',
  OTHER: 'OTHER',
}
export const regionFromContract = (r: CRegion | string | null | undefined): RegionCode => C2U_REGION[r as CRegion] ?? 'OTHER'

/** UI region filter → contract region(s). `[]` = all regions (omit the query param). */
export function regionsToContract(r: RegionFilter): CRegion[] {
  switch (r) {
    case 'ALL':
      return []
    case 'CNHK':
      return ['CN', 'HK']
    case 'OTHER':
      return ['CA', 'APAC', 'OTHER']
    default:
      return [r]
  }
}

const C2U_FACTOR: Record<CFactorId, FactorFamily> = {
  quality: 'quality',
  profitability: 'profitability',
  growth: 'growth',
  value: 'value',
  balance_sheet: 'balanceSheet',
  momentum: 'momentum',
  insider: 'insider',
  sentiment: 'sentiment',
  macro: 'macro',
  moat: 'moat',
}
export const factorFromContract = (f: string | null | undefined): FactorFamily | null => C2U_FACTOR[f as CFactorId] ?? null
export const factorToContract = (f: FactorFamily): CFactorId => (f === 'balanceSheet' ? 'balance_sheet' : f)

/** NumberMap keyed by contract factor ids → full FactorScores (missing = `fill`). */
export function factorMap(m: Record<string, number | null | undefined> | null | undefined, fill = NaN): FactorScores {
  const out: FactorScores = { quality: fill, profitability: fill, growth: fill, value: fill, balanceSheet: fill, momentum: fill, insider: fill, sentiment: fill, macro: fill, moat: fill }
  for (const [k, v] of Object.entries(m ?? {})) {
    const f = factorFromContract(k)
    if (f && typeof v === 'number') out[f] = v
  }
  return out
}

export function weightsToContract(w: FactorWeights): Record<string, number> {
  const out: Record<string, number> = {}
  for (const [k, v] of Object.entries(w) as [FactorFamily, number][]) out[factorToContract(k)] = Math.max(0, v)
  return out
}

export function moatFromContract(m: CMoatClass | null | undefined): MoatRating {
  return m === 'wide' || m === 'narrow' ? m : m === 'insufficient_data' ? 'insufficient' : 'none'
}

/** dq_score may be published on 0–1 or 0–100; bucket it. Missing → 'medium'. */
export function dqFromScore(s: number | null | undefined): DataQuality {
  if (typeof s !== 'number' || !Number.isFinite(s)) return 'medium'
  const v = s > 1 ? s / 100 : s
  return v >= 0.8 ? 'high' : v >= 0.5 ? 'medium' : 'low'
}

/** Free-text contract unit → UI ValueUnit. */
export function unitFromContract(u: string | null | undefined): ValueUnit {
  const s = (u ?? '').toLowerCase().trim()
  if (!s) return 'number'
  if (/^(pct|percent|percentage|%|fraction|share)$/.test(s)) return 'pct'
  if (/bps|basis/.test(s)) return 'bps'
  if (/^(x|multiple|times)$/.test(s)) return 'multiple'
  if (/usd|eur|currency|local|money/.test(s)) return 'currency'
  if (/day/.test(s)) return 'days'
  if (/year/.test(s)) return 'years'
  if (/score/.test(s)) return 'score'
  if (/ratio|pure/.test(s)) return 'ratio'
  return 'number'
}

export function bucketOf(mcapUsd: number): MarketCapBucket {
  const b = mcapUsd / 1e9
  if (!Number.isFinite(b)) return 'mid'
  if (b >= 200) return 'mega'
  if (b >= 10) return 'large'
  if (b >= 2) return 'mid'
  if (b >= 0.3) return 'small'
  return 'micro'
}

/** Required numeric UI fields that the contract leaves null become NaN → formatters render "—". */
export const num = (v: number | null | undefined): number => (typeof v === 'number' && Number.isFinite(v) ? v : NaN)
export const numOrNull = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

export const snakeToCamel = (s: string): string => s.replace(/_([a-z0-9])/g, (_, c: string) => c.toUpperCase())

/** Exchange label from ISO 10383 MIC. */
const MIC: Record<string, string> = {
  XNYS: 'NYSE', XNAS: 'NASDAQ', ARCX: 'NYSE Arca', XETR: 'XETRA', XPAR: 'Euronext Paris', XAMS: 'Euronext Amsterdam', XMAD: 'BME Madrid',
  XMIL: 'Borsa Italiana', XLIS: 'Euronext Lisbon', XBRU: 'Euronext Brussels', XSWX: 'SIX Swiss', XCSE: 'Nasdaq Copenhagen',
  XSTO: 'Nasdaq Stockholm', XOSL: 'Oslo Børs', XHEL: 'Nasdaq Helsinki', XLON: 'LSE', XTKS: 'Tokyo SE', XKRX: 'KRX', XKOS: 'KOSDAQ',
  XHKG: 'HKEX', XSHG: 'Shanghai SE', XSHE: 'Shenzhen SE', BVMF: 'B3', XMEX: 'BMV', XTSE: 'TSX',
}
export const exchangeOf = (mic: string | null | undefined): string => (mic ? (MIC[mic] ?? mic) : '—')

/** SecurityRef + optional extras → UI CompanySummary. */
export function summaryFromSecurity(
  sec: CSecurityRef,
  ctx: AdapterCtx,
  extra: Partial<Pick<CompanySummary, 'price' | 'change1dPct' | 'compositeScore' | 'factorScores' | 'moat' | 'dataQuality' | 'isin'>> = {},
): CompanySummary {
  const mcap = num(sec.market_cap_usd)
  return {
    id: sec.security_id,
    ticker: sec.ticker,
    name: sec.name,
    exchange: sec.exchange_name ?? exchangeOf(sec.mic),
    isin: extra.isin || sec.isin || '',
    country: sec.country,
    region: regionFromContract(sec.region),
    currency: sec.currency,
    sector: ctx.sectorName(sec.sector_id),
    subsector: ctx.subsectorName(sec.subsector_id),
    kpiProfile: ctx.kpiProfileOfSubsector(sec.subsector_id),
    marketCapUsd: mcap,
    marketCapBucket: bucketOf(mcap),
    price: extra.price ?? NaN,
    change1dPct: extra.change1dPct ?? NaN,
    compositeScore: extra.compositeScore ?? NaN,
    factorScores: extra.factorScores ?? factorMap(null),
    moat: extra.moat ?? 'none',
    dataQuality: extra.dataQuality ?? 'medium',
  }
}

export function barsFromContract(bars: CPriceBar[] | null | undefined): PriceBar[] {
  return (bars ?? [])
    .filter((b) => typeof b.c === 'number')
    .map((b) => ({ time: b.t, open: b.o ?? b.c, high: b.h ?? b.c, low: b.l ?? b.c, close: b.c, volume: b.v ?? 0 }))
    .sort((a, b) => a.time.localeCompare(b.time))
}

/** Synthesise dates for an undated spark array (monthly steps back from `end`). */
export function datedSpark(spark: number[] | null | undefined, end: string | null | undefined, freq: string = 'M'): { date: string; value: number }[] {
  const arr = spark ?? []
  const last = end ? new Date(`${end}T00:00:00Z`) : new Date()
  const stepDays = freq === 'D' ? 1 : freq === 'W' ? 7 : freq === 'Q' ? 91 : freq === 'A' ? 365 : 30
  return arr.map((value, i) => {
    const d = new Date(last)
    d.setUTCDate(d.getUTCDate() - (arr.length - 1 - i) * stepDays)
    return { date: d.toISOString().slice(0, 10), value }
  })
}

export const isoDate = (d: Date): string => d.toISOString().slice(0, 10)
export function daysAgo(n: number, from = new Date()): string {
  const d = new Date(from)
  d.setUTCDate(d.getUTCDate() - n)
  return isoDate(d)
}
