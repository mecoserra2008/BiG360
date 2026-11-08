import type {
  CountryEventStat,
  EventCategory,
  FreshnessSummary,
  GlobalEvent,
  GlobalEventsPayload,
  InsiderRadarItem,
  JobRun,
  KpiDistribution,
  ModelLabPayload,
  ModelRun,
  RegionFilter,
  ScreenerPayload,
  ScreenerRow,
  SearchResult,
  SectorDetail,
  SectorNode,
  SourceFreshness,
} from '../types'
import { countryName, matchesRegion } from '@/lib/regions'
import { DEFAULT_WEIGHTS, KPI_SETS, SECTORS } from './taxonomy'
import { UNIVERSE, toScreenerRow } from './universe'
import { insiderTransactions } from './company'
import { MOCK_AS_OF, Rng, addDays, monthEnds, round } from './rng'

/* ── screener / search ── */

let rowsCache: ScreenerRow[] | null = null
function allRows(): ScreenerRow[] {
  if (!rowsCache) {
    const sorted = [...UNIVERSE].sort((a, b) => b.compositeScore - a.compositeScore)
    rowsCache = sorted.map((e, i) => toScreenerRow(e, i + 1))
  }
  return rowsCache
}

export function screener(region: RegionFilter): ScreenerPayload {
  const rows = allRows().filter((r) => matchesRegion(r.region, region))
  return { asOf: MOCK_AS_OF, rows, defaultWeights: { ...DEFAULT_WEIGHTS } }
}

export function search(q: string, limit = 20): SearchResult[] {
  const s = q.trim().toLowerCase()
  if (!s) return []
  const scored: [number, SearchResult][] = []
  for (const e of UNIVERSE) {
    const t = e.ticker.toLowerCase()
    const n = e.name.toLowerCase()
    const isin = e.isin.toLowerCase()
    let score = 0
    if (t === s || e.id.toLowerCase() === s) score = 100
    else if (isin === s) score = 95
    else if (t.startsWith(s)) score = 80
    else if (n.startsWith(s)) score = 70
    else if (isin.startsWith(s)) score = 60
    else if (n.includes(s)) score = 40
    if (score) {
      score += Math.log10(e.marketCapUsd) - (e.synthetic ? 2 : 0)
      scored.push([score, { id: e.id, ticker: e.ticker, name: e.name, isin: e.isin, exchange: e.exchange, country: e.country, sector: e.sector }])
    }
  }
  return scored
    .sort((a, b) => b[0] - a[0])
    .slice(0, limit)
    .map(([, r]) => r)
}

/* ── sectors ── */

export function sectorTree(region: RegionFilter): SectorNode[] {
  const pool = UNIVERSE.filter((e) => matchesRegion(e.region, region))
  return SECTORS.map((s) => ({
    id: s.id,
    name: s.name,
    companyCount: pool.filter((e) => e.sector === s.name).length,
    subsectors: s.subsectors.map((sub) => ({ id: sub.id, name: sub.name, kpiProfile: sub.profile, companyCount: pool.filter((e) => e.subsector === sub.name).length })),
  }))
}

export function sectorDetail(sectorId: string, subsectorId: string | null, region: RegionFilter): SectorDetail {
  const sector = SECTORS.find((s) => s.id === sectorId) ?? SECTORS[0]
  const sub = subsectorId ? sector.subsectors.find((x) => x.id === subsectorId) ?? null : null
  // KPI profile: subsector's if chosen, else the dominant profile in the sector
  const profile = sub?.profile ?? sector.subsectors[0].profile
  const rows = allRows().filter(
    (r) => r.sector === sector.name && (!sub || r.subsector === sub.name) && matchesRegion(r.region, region) && (sub ? true : r.kpiProfile === profile || profile === 'general'),
  )
  const universeById = new Map(UNIVERSE.map((e) => [e.id, e]))
  const specs = KPI_SETS[profile]
  const distributions: KpiDistribution[] = specs.map((spec) => {
    const values = rows
      .map((r) => ({ companyId: r.id, ticker: r.ticker, value: universeById.get(r.id)?.kpis[spec.id] }))
      .filter((v): v is { companyId: string; ticker: string; value: number } => typeof v.value === 'number')
    const sorted = values.map((v) => v.value).sort((a, b) => a - b)
    const q = (p: number) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor(p * (sorted.length - 1)))] : 0)
    return {
      kpi: { id: spec.id, label: spec.label, unit: spec.unit, higherIsBetter: spec.higherIsBetter, description: spec.description },
      values,
      p10: q(0.1),
      p25: q(0.25),
      median: q(0.5),
      p75: q(0.75),
      p90: q(0.9),
    }
  })
  const byScore = [...rows].sort((a, b) => b.compositeScore - a.compositeScore)
  const ret = rows.map((r) => r.return1m).sort((a, b) => a - b)
  const sc = rows.map((r) => r.compositeScore).sort((a, b) => a - b)
  return {
    asOf: MOCK_AS_OF,
    sector: sector.name,
    subsector: sub?.name ?? null,
    kpiProfile: profile,
    kpis: distributions.map((d) => d.kpi),
    distributions,
    stats: {
      companyCount: rows.length,
      medianScore: sc.length ? sc[Math.floor(sc.length / 2)] : 0,
      totalMarketCapUsd: rows.reduce((a, r) => a + r.marketCapUsd, 0),
      medianReturn1m: ret.length ? ret[Math.floor(ret.length / 2)] : 0,
    },
    leaders: byScore.slice(0, 10),
    laggards: byScore.slice(-10).reverse(),
  }
}

/* ── insider radar ── */

let radarCache: InsiderRadarItem[] | null = null
export function insiderRadar(region: RegionFilter): InsiderRadarItem[] {
  if (!radarCache) {
    const r = new Rng('radar')
    const items: InsiderRadarItem[] = []
    const pool = r.shuffle(UNIVERSE).slice(0, 220)
    for (const e of pool) {
      const txs = insiderTransactions(e).filter((t) => t.tradeDate >= addDays(MOCK_AS_OF, -120) && t.anomalyScore >= 0.45)
      for (const t of txs.filter((x, i) => !x.isClusterBuy || i === txs.findIndex((y) => y.isClusterBuy)).slice(0, 3)) {
        const reason = t.isClusterBuy
          ? `Cluster buy: ${r.int(3, 5)} insiders within ${r.int(4, 10)} days`
          : t.code === 'P'
            ? `Open-market purchase ${round(Math.abs(t.ownershipChangePct) * 100, 0)}% of holding; first in ${r.int(14, 60)} months`
            : t.code === 'S'
              ? `Sale ahead of earnings window; ${r.int(2, 4)}σ above insider's typical size`
              : `Unusual ${t.code}-code filing timing (filed ${r.int(3, 9)}d late)`
        items.push({ ...t, companyName: e.name, country: e.country, region: e.region, sector: e.sector, marketCapUsd: e.marketCapUsd, reason })
      }
    }
    radarCache = items.sort((a, b) => b.anomalyScore - a.anomalyScore || b.tradeDate.localeCompare(a.tradeDate))
  }
  return radarCache.filter((i) => matchesRegion(i.region, region))
}

/* ── global events ── */

const EVENT_COUNTRIES = ['US', 'CA', 'MX', 'BR', 'AR', 'CL', 'CO', 'PE', 'GB', 'FR', 'DE', 'ES', 'PT', 'IT', 'NL', 'BE', 'CH', 'SE', 'NO', 'FI', 'DK', 'PL', 'UA', 'RU', 'TR', 'IL', 'SA', 'AE', 'IR', 'EG', 'NG', 'ZA', 'KE', 'IN', 'PK', 'CN', 'JP', 'KR', 'TW', 'VN', 'TH', 'ID', 'MY', 'PH', 'AU', 'NZ']
const EXTRA_NAMES: Record<string, string> = {
  CA: 'Canada', CO: 'Colombia', PE: 'Peru', PL: 'Poland', UA: 'Ukraine', RU: 'Russia', TR: 'Türkiye', IL: 'Israel', SA: 'Saudi Arabia', AE: 'United Arab Emirates', IR: 'Iran', EG: 'Egypt', NG: 'Nigeria', ZA: 'South Africa', KE: 'Kenya', IN: 'India', PK: 'Pakistan', TW: 'Taiwan', VN: 'Vietnam', TH: 'Thailand', ID: 'Indonesia', MY: 'Malaysia', PH: 'Philippines', AU: 'Australia', NZ: 'New Zealand',
}
const REGION_COUNTRIES: Record<string, string[]> = {
  US: ['US'], EU: ['FR', 'DE', 'ES', 'PT', 'IT', 'NL', 'BE', 'CH', 'SE', 'NO', 'FI', 'DK'], UK: ['GB'], JP: ['JP'], KR: ['KR'], CNHK: ['CN', 'HK'], LATAM: ['BR', 'MX', 'AR', 'CL'],
}
const HEADLINES: Record<EventCategory, string[]> = {
  Policy: ['Central bank signals pause as inflation cools', 'Government unveils industrial subsidy package', 'New export controls on advanced chips announced'],
  Economic: ['Manufacturing PMI rebounds above 50', 'Retail sales surprise to the upside', 'Currency slides after weak trade data'],
  Conflict: ['Escalation near shipping lanes raises freight risk', 'Border clashes reported; markets on alert'],
  Protest: ['Nationwide strike disrupts ports and rail', 'Farmers block highways over fuel tax'],
  Disaster: ['Typhoon forces factory shutdowns', 'Flooding disrupts mining operations', 'Earthquake damages logistics hubs'],
  Diplomacy: ['Trade talks resume; tariff truce extended', 'Bilateral summit yields energy agreement'],
  Legal: ['Antitrust regulator opens probe into platform fees', 'Supreme court ruling reshapes tax regime'],
  Corporate: ['Mega-merger announced in telecoms', 'Large IPO prices at top of range'],
}

let eventsCache: GlobalEventsPayload | null = null
export function globalEvents(region: RegionFilter): GlobalEventsPayload {
  if (!eventsCache) {
    const r = new Rng('gdelt')
    const countries: CountryEventStat[] = EVENT_COUNTRIES.map((c) => {
      const hot = ['UA', 'RU', 'IL', 'IR', 'TW', 'AR'].includes(c)
      return {
        country: c,
        countryName: EXTRA_NAMES[c] ?? countryName(c),
        tone: round(r.normal(hot ? -4.5 : -1.2, 1.4), 2),
        intensity: round(Math.min(1, Math.max(0.02, hot ? r.range(0.6, 1) : r.range(0.05, 0.6))), 2),
        eventCount: Math.round((hot ? 4000 : 600) * r.range(0.4, 1.6)),
        change7d: round(r.normal(0, 0.18), 3),
      }
    })
    const timeline = Array.from({ length: 120 }, (_, i) => ({ date: addDays(MOCK_AS_OF, i - 119), tone: round(-1.4 + Math.sin(i / 9) * 0.5 + r.normal(0, 0.35), 2), eventCount: Math.round(52000 + Math.sin(i / 7) * 5000 + r.normal(0, 3000)) }))
    const cats = Object.keys(HEADLINES) as EventCategory[]
    const events: GlobalEvent[] = Array.from({ length: 60 }, (_, i) => {
      const category = r.pick(cats)
      const cc = r.pick(EVENT_COUNTRIES.concat(['US', 'CN', 'DE', 'JP', 'BR', 'GB', 'KR', 'MX']))
      const exposed = UNIVERSE.filter((e) => e.country === cc || (cc === 'HK' && e.country === 'CN'))
      const affected = r.shuffle(exposed.filter((e) => !e.synthetic)).slice(0, r.int(0, 5))
      return {
        id: `gev-${i}`,
        date: addDays(MOCK_AS_OF, -r.int(0, 60)),
        category,
        headline: r.pick(HEADLINES[category]),
        countries: [cc],
        tone: round(r.normal(category === 'Conflict' || category === 'Disaster' ? -5 : -0.8, 1.8), 2),
        intensity: round(r.range(0.1, 1), 2),
        articleCount: r.int(20, 2500),
        affectedSectors: r.shuffle(SECTORS.map((s) => s.name)).slice(0, r.int(1, 3)),
        affectedCompanies: affected.map((e) => ({ id: e.id, ticker: e.ticker, name: e.name, exposure: round(r.range(0.1, 0.9), 2) })),
      }
    }).sort((a, b) => b.date.localeCompare(a.date))
    eventsCache = { asOf: MOCK_AS_OF, countries, timeline, events }
  }
  if (region === 'ALL') return eventsCache
  const keep = new Set(REGION_COUNTRIES[region] ?? [])
  return { ...eventsCache, events: eventsCache.events.filter((e) => e.countries.some((c) => keep.has(c))) }
}

/* ── model lab ── */

let labCache: ModelLabPayload | null = null
export function modelLab(): ModelLabPayload {
  if (labCache) return labCache
  const r = new Rng('lab')
  const runs: ModelRun[] = Array.from({ length: 16 }, (_, i) => {
    const model = i % 2 === 0 ? 'xgboost' : 'transformer'
    const started = addDays('2026-10-01', -i * 7)
    const ic = round(model === 'xgboost' ? r.range(0.025, 0.055) : r.range(0.018, 0.062), 4)
    return {
      id: `run-${String(240 - i).padStart(4, '0')}`,
      model,
      version: model === 'xgboost' ? `xgb-wf-${started.replace(/-/g, '.')}` : `mft-${started.replace(/-/g, '.')}`,
      status: i === 0 ? 'running' : r.chance(0.08) ? 'failed' : 'success',
      startedAt: `${started}T03:00:00Z`,
      durationSec: model === 'xgboost' ? r.int(600, 2400) : r.int(5400, 14400),
      trainStart: '2008-01-31',
      trainEnd: addDays(started, -95),
      testStart: addDays(started, -90),
      testEnd: started,
      ic,
      rankIc: round(ic + r.normal(0.004, 0.004), 4),
      icir: round(r.range(0.3, 0.9), 2),
      hitRate: round(r.range(0.51, 0.57), 3),
      decileSpreadAnn: round(r.range(0.04, 0.12), 4),
      nFeatures: model === 'xgboost' ? 148 : 212,
      promoted: i === 2 || i === 1,
    }
  })
  const months = monthEnds(MOCK_AS_OF, 72)
  const icSeries = months.map((date) => {
    const x = r.normal(0.038, 0.06)
    const t = r.normal(0.042, 0.075)
    return { date, xgboostIc: round(x, 4), xgboostRankIc: round(x + r.normal(0.004, 0.01), 4), transformerIc: round(t, 4), transformerRankIc: round(t + r.normal(0.004, 0.012), 4) }
  })
  const deciles = Array.from({ length: 10 }, (_, i) => ({ decile: i + 1, xgboost: round(-0.03 + i * 0.0105 + r.normal(0, 0.006), 4), transformer: round(-0.038 + i * 0.012 + r.normal(0, 0.008), 4) }))
  let a = 1
  let b = 1
  let c = 1
  const cumulativeSpread = months.map((date) => {
    a *= 1 + r.normal(0.0062, 0.022)
    b *= 1 + r.normal(0.0071, 0.028)
    c *= 1 + r.normal(0.0008, 0.012)
    return { date, xgboost: round(a - 1, 4), transformer: round(b - 1, 4), benchmark: round(c - 1, 4) }
  })
  const feats: [string, ModelLabPayload['featureImportance'][number]['family']][] = [
    ['12-1m momentum', 'momentum'], ['EPS revisions 3m', 'growth'], ['EV/EBIT sector-rel.', 'value'], ['ROIC − WACC', 'moat'], ['Gross profitability', 'profitability'], ['Accruals', 'quality'], ['Insider anomaly', 'insider'], ['News sentiment Δ', 'sentiment'], ['Net debt/EBITDA', 'balanceSheet'], ['Regime × beta', 'macro'], ['FCF yield', 'value'], ['Short interest', 'sentiment'], ['Realised vol 60d', 'momentum'], ['Revenue CAGR 3y', 'growth'], ['Piotroski F', 'quality'], ['Altman Z', 'balanceSheet'], ['Lazy-prices sim.', 'sentiment'], ['13F Δ holders', 'insider'], ['Margin stability', 'moat'], ['Rate duration', 'macro'],
  ]
  let imp = 0.14
  const featureImportance = feats.map(([feature, family]) => {
    imp *= r.range(0.78, 0.95)
    return { feature, family, importance: round(imp, 4) }
  })
  const folds = Array.from({ length: 10 }, (_, i) => ({ fold: i + 1, trainStart: '2008-01-31', trainEnd: `${2015 + i}-12-31`, testStart: `${2016 + i}-03-31`, testEnd: `${2016 + i + 1}-03-31`, rankIc: round(r.normal(0.042, 0.018), 4), sharpe: round(r.normal(0.9, 0.35), 2) }))
  labCache = { runs, icSeries, deciles, cumulativeSpread, featureImportance, folds }
  return labCache
}

/* ── system ── */

const SOURCES: [string, string, SourceFreshness['region'], SourceFreshness['cadence'], number][] = [
  ['SEC EDGAR XBRL', 'Company facts & financial statements (10-K/10-Q)', 'US', 'daily', 5200],
  ['SEC Form 4', 'Insider transactions', 'US', 'intraday', 3100],
  ['SEC 13F', 'Institutional holdings', 'US', 'quarterly', 4800],
  ['Polygon', 'Daily OHLCV, splits, dividends (free tier)', 'US', 'daily', 5800],
  ['yfinance', 'Global daily prices & fundamentals fallback', 'GLOBAL', 'daily', 14200],
  ['FRED', 'US & global macro series', 'GLOBAL', 'daily', 412],
  ['GDELT 2.0', 'Global events & tone (15-min files)', 'GLOBAL', 'intraday', 1],
  ['ECB SDW', 'Euro-area macro, yields', 'EU', 'daily', 96],
  ['Companies House', 'UK filings & officers', 'UK', 'daily', 1450],
  ['EDINET', 'Japanese securities reports', 'JP', 'daily', 3700],
  ['OpenDART', 'Korean disclosures & insider reports', 'KR', 'daily', 2300],
  ['BoK ECOS', 'Korean macro statistics', 'KR', 'monthly', 64],
  ['HKEXnews', 'HK filings & disclosure of interests', 'CNHK', 'daily', 2500],
  ['CNINFO', 'Mainland A-share disclosures', 'CNHK', 'daily', 5100],
  ['CVM', 'Brazilian filings (ITR/DFP)', 'LATAM', 'daily', 420],
  ['OpenFIGI', 'Identifier mapping (ISIN ↔ ticker)', 'GLOBAL', 'weekly', 31000],
]

export function systemStatus(): { sources: SourceFreshness[]; jobs: JobRun[]; asOf: string } {
  const r = new Rng('sys')
  const now = new Date(`${MOCK_AS_OF}T22:30:00Z`).getTime()
  const sources: SourceFreshness[] = SOURCES.map(([source, description, region, cadence, coverage]) => {
    const expected = cadence === 'intraday' ? 1 : cadence === 'daily' ? 26 : cadence === 'weekly' ? 170 : cadence === 'monthly' ? 750 : 2300
    const lag = source === 'CVM' ? expected * 2.4 : source === 'BoK ECOS' ? -1 : r.range(0.1, 0.9) * expected
    const status: SourceFreshness['status'] = lag < 0 ? 'error' : lag > expected ? 'stale' : 'fresh'
    const lagHours = round(Math.abs(lag), 1)
    return { source, description, region, cadence, lastSuccess: new Date(now - lagHours * 3.6e6 - (status === 'error' ? 40 * 3.6e6 : 0)).toISOString(), lagHours: status === 'error' ? round(40 + lagHours, 1) : lagHours, status, coverage }
  })
  const jobs: JobRun[] = Array.from({ length: 40 }, (_, i) => {
    const [source] = r.pick(SOURCES)
    const st = i === 0 ? 'running' : i === 1 ? 'queued' : source === 'BoK ECOS' ? 'failed' : r.chance(0.06) ? 'failed' : r.chance(0.04) ? 'skipped' : 'success'
    const started = new Date(now - i * r.range(0.4, 1.4) * 3.6e6)
    return {
      id: `job-${9000 - i}`,
      job: `ingest_${source.toLowerCase().replace(/[^a-z0-9]+/g, '_')}`,
      source,
      status: st,
      startedAt: started.toISOString(),
      durationSec: st === 'queued' ? 0 : r.int(4, 1900),
      rowsWritten: st === 'success' ? r.int(100, 250000) : 0,
      message: st === 'failed' ? (source === 'BoK ECOS' ? 'HTTP 401: API key missing (BOK_ECOS_APIKEY)' : 'HTTP 429 rate-limited; retry scheduled') : st === 'skipped' ? 'No new filings since last watermark' : st === 'running' ? 'Processing batch 3/7' : 'OK',
    }
  })
  return { sources, jobs, asOf: new Date(now).toISOString() }
}

export function freshness(): FreshnessSummary {
  const s = systemStatus()
  const bad = s.sources.filter((x) => x.status !== 'fresh')
  return {
    asOf: MOCK_AS_OF,
    lastUpdate: s.asOf,
    status: bad.some((b) => b.status === 'error') ? 'error' : bad.length ? 'stale' : 'fresh',
    staleSources: bad.map((b) => b.source),
  }
}
