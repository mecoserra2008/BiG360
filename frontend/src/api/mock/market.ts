import type {
  CreditSpread,
  IndexQuote,
  MacroIndicator,
  MarketBreadth,
  MarketMover,
  MacroRegime,
  MarketOverview,
  RegimeName,
  RegionCode,
  RegionFilter,
  SectorPerformance,
  TimePoint,
  YieldCurve,
  YieldCurvePoint,
} from '../types'
import { SECTORS } from './taxonomy'
import { UNIVERSE } from './universe'
import { MOCK_AS_OF, Rng, businessDays, monthEnds, round } from './rng'

const TENORS: [string, number][] = [
  ['3M', 0.25],
  ['6M', 0.5],
  ['1Y', 1],
  ['2Y', 2],
  ['3Y', 3],
  ['5Y', 5],
  ['7Y', 7],
  ['10Y', 10],
  ['20Y', 20],
  ['30Y', 30],
]

/** Nelson–Siegel curve: level, slope, curvature (decimals). */
function ns(b0: number, b1: number, b2: number, tau = 2.2): YieldCurvePoint[] {
  return TENORS.map(([tenor, t]) => {
    const x = t / tau
    const f = (1 - Math.exp(-x)) / x
    return { tenor, years: t, yield: round(b0 + b1 * f + b2 * (f - Math.exp(-x)), 5) }
  })
}

function curves(): YieldCurve[] {
  const spec: [string, string, [number, number, number], [number, number, number], [number, number, number]][] = [
    ['US', 'US Treasuries', [0.0465, -0.008, -0.006], [0.0472, -0.0075, -0.007], [0.0445, 0.002, -0.012]],
    ['DE', 'German Bunds', [0.031, -0.011, -0.004], [0.0305, -0.0105, -0.005], [0.0275, -0.004, -0.008]],
    ['GB', 'UK Gilts', [0.0515, -0.0125, -0.006], [0.0522, -0.012, -0.007], [0.0475, -0.002, -0.009]],
    ['JP', 'JGBs', [0.0315, -0.0255, -0.002], [0.031, -0.0255, -0.003], [0.024, -0.0235, -0.002]],
  ]
  return spec.map(([country, label, now, m1, y1]) => {
    const current = ns(...now)
    const y2 = current.find((p) => p.tenor === '2Y')?.yield ?? 0
    const y10 = current.find((p) => p.tenor === '10Y')?.yield ?? 0
    const y5 = current.find((p) => p.tenor === '5Y')?.yield ?? 0
    const y30 = current.find((p) => p.tenor === '30Y')?.yield ?? 0
    const m10 = ns(...m1).find((p) => p.tenor === '10Y')?.yield ?? 0
    return {
      country,
      label,
      current,
      oneMonthAgo: ns(...m1),
      oneYearAgo: ns(...y1),
      policyRate: round(now[0] + now[1], 4),
      spread2s10sBps: round((y10 - y2) * 1e4, 0),
      spread5s30sBps: round((y30 - y5) * 1e4, 0),
      change10y1mBps: round((y10 - m10) * 1e4, 0),
      asOf: MOCK_AS_OF,
      asOfMin: MOCK_AS_OF,
      nMarketTenors: current.length,
    }
  })
}

function hist(seed: string, n: number, last: number, sd: number, mr = 0.12): TimePoint[] {
  const r = new Rng(seed)
  const dates = monthEnds(MOCK_AS_OF, n)
  const vals: number[] = new Array(n)
  vals[n - 1] = last
  for (let i = n - 2; i >= 0; i--) vals[i] = vals[i + 1] + r.normal(0, sd) + (last - vals[i + 1]) * mr * 0.2
  return dates.map((date, i) => ({ date, value: round(vals[i], 4) }))
}

type IndSpec = [string, string, RegionCode, MacroIndicator['unit'], number, number, string, boolean | null, string]
// id, name, region, unit, value, monthly sd, change label, higherIsBetter, source
const IND: IndSpec[] = [
  ['us_cpi', 'CPI y/y', 'US', 'pct', 0.0281, 0.0012, 'vs prior', false, 'FRED · BLS'],
  ['us_core_pce', 'Core PCE y/y', 'US', 'pct', 0.0268, 0.0008, 'vs prior', false, 'FRED · BEA'],
  ['us_unemp', 'Unemployment', 'US', 'pct', 0.0432, 0.0007, 'vs prior', false, 'FRED · BLS'],
  ['us_ism', 'ISM Manufacturing', 'US', 'number', 49.4, 0.9, 'vs prior', true, 'ISM'],
  ['us_ffr', 'Fed funds (upper)', 'US', 'pct', 0.0375, 0.0008, '3m chg', null, 'FRED'],
  ['us_gdp', 'Real GDP q/q saar', 'US', 'pct', 0.019, 0.006, 'vs prior', true, 'FRED · BEA'],
  ['ea_hicp', 'HICP y/y', 'EU', 'pct', 0.0214, 0.001, 'vs prior', false, 'ECB SDW'],
  ['ea_depo', 'ECB deposit rate', 'EU', 'pct', 0.02, 0.0005, '3m chg', null, 'ECB SDW'],
  ['ea_pmi', 'Composite PMI', 'EU', 'number', 50.9, 0.8, 'vs prior', true, 'HCOB / S&P'],
  ['ea_unemp', 'Unemployment', 'EU', 'pct', 0.0628, 0.0005, 'vs prior', false, 'Eurostat'],
  ['uk_cpi', 'CPI y/y', 'UK', 'pct', 0.0342, 0.0012, 'vs prior', false, 'ONS'],
  ['uk_bank', 'Bank Rate', 'UK', 'pct', 0.0375, 0.0006, '3m chg', null, 'BoE'],
  ['uk_pmi', 'Composite PMI', 'UK', 'number', 51.3, 0.9, 'vs prior', true, 'S&P Global'],
  ['jp_cpi', 'Core CPI y/y', 'JP', 'pct', 0.0262, 0.001, 'vs prior', false, 'e-Stat'],
  ['jp_boj', 'BoJ policy rate', 'JP', 'pct', 0.0075, 0.0004, '3m chg', null, 'BoJ'],
  ['jp_tankan', 'Tankan large mfg DI', 'JP', 'number', 14, 1.5, 'vs prior', true, 'BoJ'],
  ['kr_cpi', 'CPI y/y', 'KR', 'pct', 0.0208, 0.001, 'vs prior', false, 'BoK ECOS'],
  ['kr_bok', 'BoK base rate', 'KR', 'pct', 0.025, 0.0004, '3m chg', null, 'BoK ECOS'],
  ['kr_exports', 'Exports y/y', 'KR', 'pct', 0.042, 0.03, 'vs prior', true, 'MOTIE'],
  ['cn_cpi', 'CPI y/y', 'CNHK', 'pct', 0.0031, 0.0015, 'vs prior', null, 'NBS'],
  ['cn_pmi', 'Caixin mfg PMI', 'CNHK', 'number', 50.4, 0.8, 'vs prior', true, 'Caixin / S&P'],
  ['cn_lpr', '1Y loan prime rate', 'CNHK', 'pct', 0.03, 0.0003, '3m chg', null, 'PBoC'],
  ['br_ipca', 'IPCA y/y (BR)', 'LATAM', 'pct', 0.0489, 0.0018, 'vs prior', false, 'IBGE'],
  ['br_selic', 'Selic (BR)', 'LATAM', 'pct', 0.1425, 0.0012, '3m chg', null, 'BCB'],
  ['mx_cpi', 'CPI y/y (MX)', 'LATAM', 'pct', 0.0372, 0.0015, 'vs prior', false, 'INEGI'],
  ['mx_banxico', 'Banxico rate (MX)', 'LATAM', 'pct', 0.0725, 0.0012, '3m chg', null, 'Banxico'],
]

function indicators(): MacroIndicator[] {
  return IND.map(([id, name, region, unit, value, sd, changeLabel, higherIsBetter, source]) => {
    const history = hist(`ind:${id}`, 36, value, sd)
    const back = changeLabel === '3m chg' ? 3 : 1
    const change = round(value - history[history.length - 1 - back].value, 5)
    return { id, name, region, unit, value, change, changeLabel, higherIsBetter, asOf: history[history.length - 1].date, source, history }
  })
}

function spreads(): CreditSpread[] {
  const s: [string, string, number, number][] = [
    ['us_ig', 'US IG OAS', 84, 6],
    ['us_hy', 'US HY OAS', 312, 22],
    ['eu_ig', 'EUR IG OAS', 104, 6],
    ['eu_hy', 'EUR HY OAS', 338, 24],
    ['em_sov', 'EM sovereign (EMBI)', 286, 18],
    ['itraxx', 'iTraxx Europe Main', 59, 4],
  ]
  return s.map(([id, name, v, sd]) => {
    const history = hist(`cs:${id}`, 60, v, sd)
    const vals = history.map((h) => h.value).sort((a, b) => a - b)
    const pct = (vals.filter((x) => x < v).length / vals.length) * 100
    return {
      id,
      name,
      spreadType: id === 'em_sov' || id === 'itraxx' ? 'unknown' : 'oas',
      asOf: MOCK_AS_OF,
      valueBps: v,
      change1dBps: NaN,
      change1mBps: round(v - history[history.length - 2].value, 0),
      percentile: round(pct, 0),
      percentileWindowYears: 5,
      sparkWindowDays: 1826,
      history,
    }
  })
}

function regime(): MacroRegime {
  const r = new Rng('regime')
  const dates = monthEnds(MOCK_AS_OF, 48)
  let cur: RegimeName = 'Recovery'
  const history = dates.map((date, i) => {
    if (i === 12) cur = 'Expansion'
    if (i === 30) cur = 'Slowdown'
    if (i === 38) cur = 'Expansion'
    return { date, regime: cur, probability: round(0.5 + r.next() * 0.4, 2) }
  })
  const probabilities: Record<RegimeName, number> = { Expansion: 0.55, Slowdown: 0.25, Contraction: 0.05, Recovery: 0.09, Stagflation: 0.06 }
  return {
    status: 'modelled',
    requestedScope: 'GLOBAL',
    scope: 'GLOBAL',
    modelId: 'mock_hmm',
    current: 'Expansion',
    date: MOCK_AS_OF,
    probability: probabilities.Expansion,
    probabilities,
    states: ['Expansion', 'Slowdown', 'Contraction', 'Recovery', 'Stagflation'],
    history,
    drivers: [
      { label: 'Global PMI composite', value: '51.2', z: 0.4, direction: 'up' },
      { label: 'US 2s10s', value: '0.52%', z: 0.3, direction: 'up' },
      { label: 'HY OAS', value: '3.12%', z: -0.5, direction: 'down' },
      { label: 'Global CPI momentum', value: '2.6%', z: 0.1, direction: 'down' },
    ],
  }
}

function sectorPerf(): SectorPerformance[] {
  const out: SectorPerformance[] = []
  const regions: RegionCode[] = ['US', 'EU', 'UK', 'JP', 'KR', 'CNHK', 'LATAM']
  for (const region of regions) {
    const rr = new Rng(`reg:${region}`)
    const regDrift = rr.normal(0.01, 0.02)
    for (const s of SECTORS) {
      const r = new Rng(`sp:${region}:${s.id}`)
      const mcap = UNIVERSE.filter((e) => e.region === region && e.sector === s.name).reduce((a, e) => a + e.marketCapUsd, 0)
      out.push({
        sector: s.name,
        region,
        return1d: round(r.normal(0.0005, 0.009), 4),
        return1m: round(regDrift + r.normal(0, 0.035), 4),
        returnYtd: round(regDrift * 6 + r.normal(0.06, 0.1), 4),
        marketCapUsd: Math.max(mcap, 1e9),
        nSecurities: UNIVERSE.filter((e) => e.region === region && e.sector === s.name).length,
        nRet1d: null,
        asOf: MOCK_AS_OF,
        asOfByRegion: { [region]: MOCK_AS_OF },
      })
    }
  }
  return out
}

const INDEX_SPEC: [string, string, string, string, number][] = [
  ['^GSPC', 'S&P 500', 'US', 'USD', 6400],
  ['^STOXX50E', 'EURO STOXX 50', 'EU', 'EUR', 5450],
  ['^FTSE', 'FTSE 100', 'UK', 'GBP', 9200],
  ['^N225', 'Nikkei 225', 'JP', 'JPY', 44000],
  ['^KS11', 'KOSPI Composite', 'KR', 'KRW', 3300],
  ['^HSI', 'Hang Seng', 'CNHK', 'HKD', 25500],
  ['^BVSP', 'Ibovespa', 'LATAM', 'BRL', 141000],
]

function indices(): IndexQuote[] {
  const dates = businessDays(MOCK_AS_OF, 60)
  return INDEX_SPEC.map(([id, name, region, currency, last]) => {
    const r = new Rng(`idx:${id}`)
    const vals: number[] = new Array(dates.length)
    vals[dates.length - 1] = last
    for (let i = dates.length - 2; i >= 0; i--) vals[i] = vals[i + 1] * (1 + r.normal(0, 0.009))
    const spark = dates.map((date, i) => ({ date, value: round(vals[i], 2) }))
    const at = (k: number) => vals[Math.max(0, vals.length - 1 - k)]
    return { id, name, region, currency, last, change1d: round(last / at(1) - 1, 4), change1w: round(last / at(5) - 1, 4), change1m: round(last / at(21) - 1, 4), changeYtd: round(r.normal(0.08, 0.06), 4), asOf: MOCK_AS_OF, spark, bigMove: false }
  })
}

function breadth(region: string): MarketBreadth {
  const r = new Rng(`breadth:${region}`)
  return { region, pctAbove200d: round(0.4 + r.next() * 0.3, 3), pctAbove50d: round(0.35 + r.next() * 0.3, 3), advancers: 400 + Math.round(r.next() * 600), decliners: 400 + Math.round(r.next() * 600), newHighs: Math.round(r.next() * 60), newLows: Math.round(r.next() * 40), asOfByRegion: { [region]: MOCK_AS_OF } }
}

function movers(sign: 1 | -1, region: RegionFilter): MarketMover[] {
  return UNIVERSE.filter((e) => region === 'ALL' || e.region === region)
    .slice(0, 400)
    .map((e) => ({ e, c: new Rng(`mv:${e.id}`).normal(0, 0.03) }))
    .sort((a, b) => sign * (b.c - a.c))
    .slice(0, 10)
    .map(({ e, c }) => ({ id: e.id, ticker: e.ticker, name: e.name, region: e.region, currency: e.currency, close: e.price, change1d: round(c, 4), volumeZ: null, marketCapUsd: e.marketCapUsd, pxDate: MOCK_AS_OF }))
}

let cached: MarketOverview | null = null
export function marketOverview(region: RegionFilter): MarketOverview {
  const base: MarketOverview = (cached ??= {
    asOf: MOCK_AS_OF,
    regime: regime(),
    yieldCurves: curves(),
    indicators: indicators(),
    creditSpreads: spreads(),
    sectorPerformance: sectorPerf(),
    dataAsOf: [],
    datesAligned: null,
    indices: indices(),
    breadth: breadth('ALL'),
    breadthByRegion: (['US', 'EU', 'UK', 'JP', 'KR', 'CNHK', 'LATAM'] as const).map(breadth),
    gainers: movers(1, 'ALL'),
    losers: movers(-1, 'ALL'),
  })
  if (region === 'ALL') return base
  return {
    ...base,
    indicators: base.indicators.filter((i) => i.region === region),
    sectorPerformance: base.sectorPerformance.filter((s) => s.region === region),
    indices: base.indices.filter((i) => i.region === region),
    breadth: breadth(region),
    breadthByRegion: [],
    gainers: movers(1, region),
    losers: movers(-1, region),
  }
}
