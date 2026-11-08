import type { CMacroRegime, CMacroSeriesSummary, CMarketOverview, CRegimeSnapshot, CSectorPerf, components } from '../contract'
import type {
  CreditSpread,
  IndexQuote,
  MacroIndicator,
  MacroRegime,
  MarketBreadth,
  MarketMover,
  MarketOverview,
  MoversFilter,
  RegimeDriver,
  RegimeName,
  RegimeStatus,
  RegionCode,
  RegionDataAsOf,
  SectorPerformance,
  TimePoint,
  YieldCurve,
} from '../types'
import { fmtCompactCurrency } from '@/lib/format'
import { COUNTRIES } from '@/lib/regions'
import { datedSpark, num, numOrNull } from './common'

type S = components['schemas']
type CYieldCurve = S['YieldCurve']
type CCreditSpread = S['CreditSpread']
type CNamedSeries = S['NamedSeries']
type CIndexPerf = S['IndexPerf']
type CBreadth = S['Breadth']
type CMoverRow = S['MoverRow']
type CRegionDataAsOf = S['RegionDataAsOf']
type CRegimeScopeInfo = S['RegimeScopeInfo']

/* ───────────────────────── regime ───────────────────────── */

const REGIME: Record<string, RegimeName> = {
  expansion: 'Expansion',
  slowdown: 'Slowdown',
  contraction: 'Contraction',
  recovery: 'Recovery',
  stagflation: 'Stagflation',
}
export const REGIME_ORDER: RegimeName[] = ['Expansion', 'Slowdown', 'Contraction', 'Recovery', 'Stagflation']
/** Unknown labels (incl. v1.29 `not_modelled`) map to null — never to a default regime. */
export const regimeName = (l: string | null | undefined): RegimeName | null => REGIME[l ?? ''] ?? null

const hasProbs = (s: CRegimeSnapshot | null | undefined): s is CRegimeSnapshot => !!s && Object.keys(s.probabilities ?? {}).length > 0
/** A regime answer that carries a real model label (not a pending / not_modelled placeholder). */
function usableFull(f: CMacroRegime | null | undefined): f is CMacroRegime {
  return !!f && f.status !== 'not_modelled' && f.modelled !== false && f.data_status !== 'pending' && regimeName(f.current?.label) !== null && hasProbs(f.current)
}

/** Driver level in its true unit. Pre-1.29 APIs said unit "z" for raw levels: never print "z" on a level. */
export function fmtDriverLevel(v: number | null, unit: string | null | undefined, legacy: boolean): string {
  if (v === null) return '—'
  const u = (unit ?? '').toLowerCase()
  const n = (x: number, d: number) => Number(x.toFixed(d)).toString()
  if (!legacy && u === 'fraction') return `${n(v * 100, 2)}%`
  if (!legacy && u === 'percent') return `${n(v, 2)}%`
  if (!legacy && u && !['level', 'index', 'z'].includes(u)) return `${n(v, 2)} ${unit}`
  return n(v, Math.abs(v) >= 100 ? 1 : 2)
}

function adaptDriver(s: CNamedSeries): RegimeDriver {
  const pts = s.points.filter((x) => typeof x.value === 'number')
  const legacy = s.z_points === undefined // pre-1.29: values are levels whatever `unit` says
  const last = numOrNull(s.latest_value) ?? pts[pts.length - 1]?.value ?? null
  const prev = pts[pts.length - 2]?.value ?? null
  const direction: RegimeDriver['direction'] = last === null || prev === null || last === prev ? 'flat' : last > prev ? 'up' : 'down'
  return { label: s.label || s.id, value: fmtDriverLevel(last, s.unit, legacy), z: legacy ? null : numOrNull(s.latest_z), direction }
}

function probs(snap: CRegimeSnapshot | null | undefined): { p: Record<RegimeName, number>; states: RegimeName[] } {
  const p: Record<RegimeName, number> = { Expansion: 0, Slowdown: 0, Contraction: 0, Recovery: 0, Stagflation: 0 }
  const seen = new Set<RegimeName>()
  for (const [k, v] of Object.entries(snap?.probabilities ?? {})) {
    const n = regimeName(k)
    if (!n) continue
    p[n] += v
    seen.add(n)
  }
  return { p, states: REGIME_ORDER.filter((s) => seen.has(s)) }
}

const NOT_MODELLED = (requestedScope: string, extra: Partial<MacroRegime> = {}): MacroRegime => ({
  status: 'not_modelled',
  requestedScope,
  scope: null,
  modelId: null,
  current: null,
  date: null,
  probability: NaN,
  probabilities: { Expansion: 0, Slowdown: 0, Contraction: 0, Recovery: 0, Stagflation: 0 },
  states: [],
  history: [],
  drivers: [],
  ...extra,
})

/**
 * /market/overview.regime (+ /macro/regime for the requested scope, or the GLOBAL fallback) → UI MacroRegime.
 * The regime is labelled with the scope of the model that produced it; a scope without its own model is
 * "not modelled" (or the labelled GLOBAL fallback), never a silent US / "Expansion" placeholder.
 */
export function adaptRegime(
  snapshot: CRegimeSnapshot | null | undefined,
  full: CMacroRegime | null,
  requestedScope = 'US',
  scopeInfo: CRegimeScopeInfo | null = null,
): MacroRegime {
  const extra = { message: full?.message ?? null, availableScopes: full?.available_scopes ?? scopeInfo?.available_scopes }
  let cur: CRegimeSnapshot | null = null
  let status: RegimeStatus = 'not_modelled'
  let scope: string | null = null
  let modelId: string | null = null
  if (usableFull(full)) {
    cur = full.current
    scope = full.scope || full.current.scope || null
    modelId = full.model_id || full.current.model_id || null
    status = scope && scope !== requestedScope ? 'fallback' : 'modelled'
  } else if (hasProbs(snapshot) && regimeName(snapshot.label) && snapshot.label !== 'not_modelled') {
    if (snapshot.scope !== undefined && snapshot.scope !== null) {
      // v1.29 snapshot: says which model answered
      if (snapshot.modelled || snapshot.fallback) {
        cur = snapshot
        scope = snapshot.scope
        modelId = snapshot.model_id ?? null
        status = snapshot.fallback || snapshot.scope !== requestedScope ? 'fallback' : 'modelled'
      }
    } else if (requestedScope === 'US') {
      // pre-1.29 overview: its snapshot always came from the US model
      cur = snapshot
      scope = 'US'
      status = 'modelled'
    }
  }
  if (!cur) return NOT_MODELLED(requestedScope, extra)
  const { p, states } = probs(cur)
  const name = regimeName(cur.label)
  // monthly history is long (HMM since 2007) — keep the last 48 observations for the strip
  const hist = (usableFull(full) ? full.history : [cur]).slice(-48)
  return {
    status,
    requestedScope,
    scope,
    modelId,
    current: name,
    date: cur.date ?? null,
    probability: name ? p[name] : NaN,
    probabilities: p,
    states,
    history: hist.map((h) => ({ date: h.date, regime: regimeName(h.label), probability: h.probabilities[h.label] ?? NaN })),
    drivers: (usableFull(full) ? (full.inputs ?? []) : []).slice(0, 6).map(adaptDriver),
    ...extra,
  }
}

/* ───────────────────────── macro tiles ───────────────────────── */

/** v1.2 `scale`: percent → UI fraction; fraction as-is; level = raw number. Falls back to `units` text. */
function scaleOf(s: CMacroSeriesSummary): { k: number; pct: boolean } {
  const sc = s.scale ?? (/percent|%/i.test(s.units ?? '') ? 'percent' : 'level')
  if (sc === 'percent') return { k: 0.01, pct: true }
  if (sc === 'fraction') return { k: 1, pct: true }
  return { k: 1, pct: false }
}

const EXTRA_REGION: Record<string, RegionCode> = { CO: 'LATAM', PE: 'LATAM', CL: 'LATAM', AR: 'LATAM', UY: 'LATAM' }
function regionOfCountry(c: string | null | undefined): RegionCode {
  if (!c) return 'OTHER'
  if (c === 'EA' || c === 'EU') return 'EU'
  if (EXTRA_REGION[c]) return EXTRA_REGION[c]
  return COUNTRIES[c]?.region ?? 'OTHER'
}

const pointsOf = (pts: { date: string; value?: number | null }[] | null | undefined, k: number): TimePoint[] =>
  (pts ?? []).filter((p) => typeof p.value === 'number' && Number.isFinite(p.value)).map((p) => ({ date: p.date, value: (p.value as number) * k }))

function legacyHistory(s: CMacroSeriesSummary, k: number): TimePoint[] {
  return s.spark_points?.length ? pointsOf(s.spark_points, k) : datedSpark(s.spark, s.last_observation_date, s.frequency).map((p) => ({ date: p.date, value: p.value * k }))
}

/** Macro tile: v1.29 display transform (y/y, q/q SAAR, m/m change …) when served, else the raw level. */
export function adaptIndicator(s: CMacroSeriesSummary): MacroIndicator {
  const base = { id: s.series_id, name: s.title, region: regionOfCountry(s.country), country: s.country ?? null, higherIsBetter: null, source: s.source_id, changeLabel: 'vs prior' }
  if (typeof s.display_value === 'number' && Number.isFinite(s.display_value)) {
    const du = s.display_unit ?? 'level'
    const k = du === 'percent' ? 0.01 : 1
    const pct = du === 'percent' || du === 'fraction'
    return {
      ...base,
      unit: pct ? 'pct' : 'number',
      value: s.display_value * k,
      change: num(s.display_change_1p) * k,
      asOf: s.observation_date ?? s.last_observation_date ?? '',
      history: pointsOf(s.display_spark_points, k),
      displayLabel: s.display_label ?? null,
      transform: s.display_transform ?? null,
      sparkWindowDays: s.spark_window_days ?? null,
    }
  }
  const { k, pct } = scaleOf(s)
  return {
    ...base,
    unit: pct ? 'pct' : 'number',
    value: num(s.last_value) * k,
    change: num(s.change_1p) * k,
    asOf: s.last_observation_date ?? '',
    history: legacyHistory(s, k),
    displayLabel: null,
    transform: null,
    sparkWindowDays: null,
  }
}

const OFFICIAL_RANK = (source: string): number => (/akshare|baostock|yahoo|yf/i.test(source) ? 2 : /imf|oecd|world.?bank/i.test(source) ? 1 : 0)

/** F40: the same concept from two sources (e.g. China CPI y/y from AKShare and the IMF) → keep the official one. */
export function dedupeIndicators(xs: MacroIndicator[]): MacroIndicator[] {
  const best = new Map<string, MacroIndicator>()
  const keyOf = (i: MacroIndicator) => `${i.country ?? i.region}|${i.displayLabel ?? ''}|${Number.isFinite(i.value) ? i.value.toFixed(4) : i.id}|${i.asOf.slice(0, 7)}`
  for (const i of xs) {
    const k = keyOf(i)
    const cur = best.get(k)
    if (!cur || OFFICIAL_RANK(i.source) < OFFICIAL_RANK(cur.source)) best.set(k, i)
  }
  const keep = new Set(best.values())
  return xs.filter((i) => keep.has(i))
}

/* ───────────────────────── credit ───────────────────────── */

const bpsFactor = (unit: string | null | undefined): number => {
  const u = (unit ?? 'percent').toLowerCase()
  return /bp|basis/.test(u) ? 1 : u === 'fraction' ? 1e4 : 100
}

/** v1.29 curated credit spread (calendar 1M change, empirical percentile with its real window, fixed 1y spark). */
export function adaptCreditSpread(c: CCreditSpread): CreditSpread {
  const k = bpsFactor(c.unit)
  return {
    id: c.series_id,
    name: c.label,
    region: c.region ?? null,
    spreadType: c.spread_type ?? 'unknown',
    segment: c.segment ?? null,
    asOf: c.as_of ?? null,
    valueBps: num(c.last) * k,
    change1dBps: num(c.change_1d) * k,
    change1mBps: num(c.change_1m) * k,
    percentile: typeof c.percentile === 'number' ? c.percentile * 100 : NaN,
    percentileWindowYears: numOrNull(c.percentile_window_years),
    sparkWindowDays: c.spark_window_days ?? null,
    history: pointsOf(c.spark_points, k),
  }
}

/** Pre-1.29 fallback (key_macro credit series): only the 1-observation change is known; no fabricated percentile. */
export function adaptLegacyCreditSpread(s: CMacroSeriesSummary): CreditSpread {
  const u = (s.units ?? '').toLowerCase()
  const sc = s.scale ?? (/bp|basis/.test(u) ? 'level' : 'percent')
  const k = /bp|basis/.test(u) ? 1 : sc === 'fraction' ? 1e4 : 100
  const yieldSpread = /baa|moody|minus|yield spread/i.test(`${s.title} ${s.series_id}`)
  return {
    id: s.series_id,
    name: s.title,
    region: s.country ?? null,
    spreadType: yieldSpread ? 'yield_spread' : /option-adjusted|oas/i.test(`${s.title} ${s.series_id}`) || /^fred:BAML/i.test(s.series_id) ? 'oas' : 'unknown',
    segment: null,
    asOf: s.last_observation_date ?? null,
    valueBps: num(s.last_value) * k,
    change1dBps: s.frequency === 'D' ? num(s.change_1p) * k : NaN,
    change1mBps: NaN,
    percentile: NaN,
    percentileWindowYears: null,
    sparkWindowDays: null,
    history: legacyHistory(s, k),
  }
}

/* ───────────────────────── yield curves ───────────────────────── */

/** v1.2 yield curves → UI curves (fractions; tenors in years). The O/N policy rate becomes a marker, not a tenor. */
export function adaptYieldCurve(c: CYieldCurve): YieldCurve {
  const k = c.scale === 'percent' ? 0.01 : 1
  const pts = [...c.points].sort((a, b) => a.tenor_months - b.tenor_months)
  const isPolicy = (p: CYieldCurve['points'][number]) => p.is_policy_rate ?? p.tenor === 'O/N'
  const market = pts.filter((p) => !isPolicy(p))
  const policy = pts.find((p) => isPolicy(p) && typeof p.yield === 'number')
  const map = (sel: (p: CYieldCurve['points'][number]) => number | null | undefined) =>
    market.map((p) => ({ tenor: p.tenor, years: p.tenor_months / 12, yield: num(sel(p)) * k }))
  const at = (months: number, sel: (p: CYieldCurve['points'][number]) => number | null | undefined = (p) => p.yield) => {
    const v = sel(market.find((p) => p.tenor_months === months) ?? ({} as CYieldCurve['points'][number]))
    return typeof v === 'number' ? v * k : NaN
  }
  const sp = (a: number, b: number) => {
    const d = (at(b) - at(a)) * 1e4
    return Number.isFinite(d) ? Math.round(d) : NaN
  }
  const dates = pts.map((p) => p.as_of).filter((d): d is string => !!d).sort()
  const ch10 = (at(120) - at(120, (p) => p.yield_1m_ago)) * 1e4
  return {
    country: c.country,
    label: c.label,
    region: c.region ?? null,
    current: map((p) => p.yield).filter((p) => Number.isFinite(p.yield)),
    oneMonthAgo: map((p) => p.yield_1m_ago),
    oneYearAgo: map((p) => p.yield_1y_ago),
    policyRate: policy ? num(policy.yield) * k : null,
    spread2s10sBps: sp(24, 120),
    spread5s30sBps: sp(60, 360),
    change10y1mBps: Number.isFinite(ch10) ? Math.round(ch10) : NaN,
    asOf: c.as_of ?? dates[dates.length - 1] ?? null,
    asOfMin: c.as_of_min ?? dates[0] ?? null,
    nMarketTenors: c.n_market_tenors ?? market.filter((p) => typeof p.yield === 'number').length,
  }
}

/* ───────────────────────── sectors, indices, breadth, movers ───────────────────────── */

export function adaptSectorPerf(rows: CSectorPerf[], region: RegionCode): SectorPerformance[] {
  return rows.map((r) => ({
    sector: r.name,
    sectorId: r.sector_id,
    region,
    return1d: num(r.ret_1d),
    return1m: num(r.ret_1m),
    returnYtd: num(r.ret_ytd),
    // null market cap → left out of the treemap (with a note) instead of an invisible area of 1
    marketCapUsd: typeof r.market_cap_usd === 'number' && r.market_cap_usd > 0 ? r.market_cap_usd : null,
    nSecurities: numOrNull(r.n_securities),
    nRet1d: numOrNull(r.n_ret_1d),
    asOf: r.as_of ?? null,
    asOfByRegion: r.as_of_by_region ?? {},
  }))
}

const DAY = 86_400_000
/** Index row: 1W from the dated spark; "big move" = |1D| above 2σ of the spark's daily moves. */
export function adaptIndex(i: CIndexPerf): IndexQuote {
  const spark = i.spark_points?.length ? pointsOf(i.spark_points, 1) : datedSpark(i.spark, i.as_of, 'D')
  const last = numOrNull(i.last) ?? spark[spark.length - 1]?.value ?? NaN
  const lastDate = i.as_of ?? spark[spark.length - 1]?.date ?? null
  let change1w = NaN
  if (lastDate && spark.length > 1) {
    const cut = Date.parse(`${lastDate}T00:00:00Z`) - 7 * DAY
    const base = [...spark].reverse().find((p) => Date.parse(`${p.date}T00:00:00Z`) <= cut)
    if (base && base.value) change1w = last / base.value - 1
  }
  const rets: number[] = []
  for (let k = 1; k < spark.length; k++) if (spark[k - 1].value) rets.push(spark[k].value / spark[k - 1].value - 1)
  const mean = rets.reduce((a, b) => a + b, 0) / (rets.length || 1)
  const sd = rets.length > 10 ? Math.sqrt(rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1)) : NaN
  const c1d = num(i.chg_1d)
  return {
    id: i.id,
    name: i.name,
    region: i.region ?? 'OTHER',
    currency: i.currency ?? null,
    last,
    change1d: c1d,
    change1w,
    change1m: num(i.chg_1m),
    changeYtd: num(i.chg_ytd),
    // pre-1.29 payloads carry no as_of: the last dated spark point is the index's latest bar
    asOf: lastDate,
    spark,
    bigMove: Number.isFinite(sd) && Number.isFinite(c1d) && Math.abs(c1d) > 2 * sd,
  }
}

export function adaptBreadth(b: CBreadth | null | undefined, region: string): MarketBreadth | null {
  if (!b) return null
  return {
    region,
    pctAbove200d: num(b.pct_above_200d),
    pctAbove50d: num(b.pct_above_50d),
    advancers: b.advancers ?? 0,
    decliners: b.decliners ?? 0,
    newHighs: b.new_highs_52w ?? 0,
    newLows: b.new_lows_52w ?? 0,
    asOfByRegion: b.as_of_by_region ?? {},
  }
}

export function adaptMover(m: CMoverRow): MarketMover {
  return {
    id: m.security.security_id,
    ticker: m.security.ticker,
    name: m.security.name,
    region: m.security.region ?? 'OTHER',
    currency: m.security.currency ?? 'USD',
    close: num(m.close),
    change1d: num(m.chg_1d),
    volumeZ: numOrNull(m.volume_z),
    marketCapUsd: numOrNull(m.security.market_cap_usd),
    pxDate: m.px_date ?? null,
    advUsd20d: numOrNull(m.adv_usd_20d),
  }
}

/** "ADV ≥ $5m · price ≥ $2 · cap ≥ $1bn (unknown cap: ADV ≥ $20m)" from the API's movers_filter (v1.30). */
export function moversFilterText(f: MoversFilter): string {
  const m = (v: number | null) => (v === null ? '—' : fmtCompactCurrency(v, 'USD', 0))
  const parts = [`ADV ≥ ${m(f.minAdvUsd)}`]
  if (f.minPriceUsd !== null) parts.push(`price ≥ $${f.minPriceUsd}`)
  if (f.minMcapUsd !== null) parts.push(`cap ≥ ${m(f.minMcapUsd)}${f.unknownMcapMinAdvUsd !== null ? ` (unknown cap: ADV ≥ ${m(f.unknownMcapMinAdvUsd)})` : ''}`)
  return parts.join(' · ')
}

/** v1.30 MarketOverview.movers_filter → UI (null on older APIs). */
export function adaptMoversFilter(f: CMarketOverview['movers_filter']): MoversFilter | null {
  if (!f || typeof f.liquid !== 'boolean') return null
  return {
    liquid: f.liquid,
    minAdvUsd: numOrNull(f.min_adv_usd),
    minMcapUsd: numOrNull(f.min_mcap_usd),
    minPriceUsd: numOrNull(f.min_price_usd),
    unknownMcapMinAdvUsd: numOrNull(f.unknown_mcap_min_adv_usd),
    limit: numOrNull(f.limit),
  }
}

/** Liquid-only movers (F21): a known market cap above the floor; the API's own ADV filter lets unknown ADV through. */
export function liquidMovers(xs: MarketMover[], minCapUsd = 1e9): MarketMover[] {
  return xs.filter((m) => m.marketCapUsd !== null && m.marketCapUsd >= minCapUsd && !(m.currency === 'USD' && m.close < 2))
}

export function adaptDataAsOf(xs: CRegionDataAsOf[] | null | undefined): RegionDataAsOf[] {
  return (xs ?? []).map((x) => ({
    region: x.region,
    asOf: x.as_of,
    latest: x.latest ?? null,
    nSecurities: numOrNull(x.n_securities),
    shareAtAsOf: numOrNull(x.share_at_as_of),
    staleShare: numOrNull(x.stale_share),
  }))
}

/* ───────────────────────── colour scale helper ───────────────────────── */

/** Symmetric colour bound winsorised at the given percentile of |x| (F19: outliers must not wash out the scale). */
export function winsorBound(values: number[], q = 0.95, floor = 1): number {
  const a = values.filter((v) => Number.isFinite(v)).map(Math.abs).sort((x, y) => x - y)
  if (!a.length) return floor
  const idx = Math.min(a.length - 1, Math.max(0, Math.ceil(q * a.length) - 1))
  return Math.max(floor, a[idx])
}

/* ───────────────────────── overview ───────────────────────── */

export interface RegionPayload {
  region: RegionCode
  ov: Pick<CMarketOverview, 'sector_performance' | 'breadth'> & Partial<Pick<CMarketOverview, 'data_as_of' | 'key_macro'>>
}

/** GET /market/overview (+ /macro/regime, + per-region overviews for "All") → UI MarketOverview. */
export function adaptMarketOverview(ov: CMarketOverview, regime: CMacroRegime | null, byRegion: RegionPayload[], requestedScope = 'US'): MarketOverview {
  // "All": the global payload carries US macro only → add each region's headline series (deduplicated)
  const seen = new Set<string>()
  const macro = [...(ov.key_macro ?? []), ...(byRegion.length > 1 ? byRegion.flatMap((r) => r.ov.key_macro ?? []) : [])].filter((s) => !seen.has(s.series_id) && !!seen.add(s.series_id))
  const curated = ov.credit_spreads
  const dataAsOf = adaptDataAsOf(ov.data_as_of?.length ? ov.data_as_of : byRegion.flatMap((r) => r.ov.data_as_of ?? []))
  return {
    dataStatus: ov.data_status,
    asOf: ov.as_of,
    regime: adaptRegime(ov.regime, regime, requestedScope, ov.regime_scope ?? null),
    yieldCurves: (ov.yield_curves ?? []).filter((c) => c.points.some((p) => typeof p.yield === 'number')).map(adaptYieldCurve),
    indicators: dedupeIndicators(macro.filter((s) => s.category !== 'credit').map(adaptIndicator)),
    creditSpreads: curated !== undefined ? curated.map(adaptCreditSpread) : macro.filter((s) => s.category === 'credit').map(adaptLegacyCreditSpread),
    sectorPerformance: byRegion.flatMap((x) => adaptSectorPerf(x.ov.sector_performance, x.region)),
    dataAsOf,
    datesAligned: typeof ov.dates_aligned === 'boolean' ? ov.dates_aligned : dataAsOf.length ? new Set(dataAsOf.map((d) => d.asOf)).size === 1 : null,
    indices: (ov.indices ?? []).map(adaptIndex),
    breadth: adaptBreadth(ov.breadth, ov.region ?? 'ALL'),
    breadthByRegion: byRegion.length > 1 ? byRegion.map((x) => adaptBreadth(x.ov.breadth, x.region)).filter((b): b is MarketBreadth => b !== null) : [],
    gainers: (ov.gainers ?? []).map(adaptMover),
    losers: (ov.losers ?? []).map(adaptMover),
    moversFilter: adaptMoversFilter(ov.movers_filter),
  }
}
