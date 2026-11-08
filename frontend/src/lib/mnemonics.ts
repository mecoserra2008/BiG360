/**
 * Bloomberg-like command bar (roadmap v3 B2): `<TICKER> [<MKT>] [<Equity|Index|Curncy|Comdty>] <MNEMONIC>` or a plain
 * `<MNEMONIC> [arg]`, e.g. `AAPL GP`, `AAPL US Equity DES`, `SX5E Index GP`, `EURUSD Curncy GP`, `ECO`, `WIRP US`,
 * `N`, `HELP DES`, `BQL get(px_last) for(['AAPL US'])`. Pure functions (tested in __tests__/mnemonics.test.ts); the
 * palette resolves the security part through GET /bql/resolve.
 */

export type AssetClass = 'Equity' | 'Index' | 'Curncy' | 'Comdty'
export const YELLOW_KEYS: AssetClass[] = ['Equity', 'Index', 'Curncy', 'Comdty']

export interface MnemonicDef {
  code: string
  title: string
  description: string
  /** security: needs a security · global: no security · both: either */
  scope: 'security' | 'global' | 'both'
  /** asset classes the security form applies to (default: all) */
  classes?: AssetClass[]
  aliases?: string[]
  /** shown but not available (e.g. no options data) */
  unavailable?: string
}

const ALL: AssetClass[] = ['Equity', 'Index', 'Curncy', 'Comdty']
const EQ: AssetClass[] = ['Equity']

export const MNEMONICS: MnemonicDef[] = [
  { code: 'DES', title: 'Description', description: 'Company 360 overview (profile, scores, key ratios) or the instrument page for indices, FX and futures', scope: 'security', classes: ALL },
  { code: 'GP', title: 'Price graph', description: 'Price chart with technical levels and calibrated ranges (instruments: full-history chart)', scope: 'security', classes: ALL },
  { code: 'GIP', title: 'Intraday-like graph', description: 'Daily data only: opens the price graph (GP)', scope: 'security', classes: ALL },
  { code: 'FA', title: 'Financial analysis', description: 'Financial statements, ratios and common-size views (point-in-time filings)', scope: 'security', classes: EQ },
  { code: 'RV', title: 'Relative value', description: 'Peer comparison: subsector / region peers, conformal peer groups, valuation vs peers', scope: 'security', classes: EQ },
  { code: 'EE', title: 'Earnings estimates (model-based)', description: 'Three-statement forecast and DCF scenarios — MODEL-BASED, not consensus', scope: 'security', classes: EQ },
  { code: 'ERN', title: 'Earnings history', description: 'Released earnings, price reactions and model-based surprises', scope: 'security', classes: EQ },
  { code: 'DVD', title: 'Dividends', description: 'Dividend per share, payout and yield in the financial statements (DPS, payout ratio)', scope: 'security', classes: EQ },
  { code: 'HDS', title: 'Holders', description: '13F institutional holders, flows, crowding; insider and activist stakes', scope: 'both', classes: EQ },
  { code: 'SI', title: 'Short interest', description: 'FINRA short interest, days to cover, fails-to-deliver and EU / UK short disclosures', scope: 'both', classes: EQ },
  { code: 'CN', title: 'Company news', description: 'News for the company (News Terminal filtered on the ticker when available; else news & sentiment tab)', scope: 'security', classes: EQ },
  { code: 'TECH', title: 'Technicals', description: 'Levels, confluence zones, setups, technical factors and breakouts', scope: 'security', classes: ALL },
  { code: 'PEERS', title: 'Peers', description: 'Peer group of the security (global: sector explorer)', scope: 'both', classes: EQ },
  { code: 'NET', title: 'Network & geo', description: 'Supply-chain network and revenue geography (global: contagion & spillovers)', scope: 'both', classes: EQ },
  { code: 'SMRT', title: 'Smart money', description: 'Smart-money overview (13F flows, short interest, activism, EU shorts; score model-based)', scope: 'both', classes: EQ },
  { code: 'EVTS', title: 'Events', description: 'Earnings & corporate-events calendar (security: event-risk tab)', scope: 'both', classes: EQ },
  { code: 'THES', title: 'Thesis', description: 'Thesis tracker (security: start a thesis on it)', scope: 'both', classes: ALL },
  { code: 'BQL', title: 'BQL console', description: 'Query console: get(...) for(...) with(...) — `BQL get(...)` runs the query; with a security it pre-fills one', scope: 'both', classes: ALL },
  { code: 'OMON', title: 'Option monitor', description: 'Not available: the app has no options data', scope: 'security', classes: ALL, unavailable: 'no options data' },
  { code: 'WEI', title: 'World equity indices', description: 'Markets overview: world equity indices, sectors, breadth', scope: 'global' },
  { code: 'WIRP', title: 'Implied policy rates', description: 'Central-bank monitor; with a country / bank (WIRP US, WIRP EU) the bank page with the implied path (approximation)', scope: 'global' },
  { code: 'ECO', title: 'Economic calendar', description: 'Official release calendar with model-based expectations (ECO US: one country)', scope: 'global' },
  { code: 'ECST', title: 'Economic statistics', description: 'Country macro dashboard (ECST DE); default US', scope: 'global' },
  { code: 'GC', title: 'Curves', description: 'Yield / policy curves of the central banks', scope: 'global' },
  { code: 'FXIP', title: 'FX', description: 'FX pairs and currencies: instruments browser (FX) and cross-asset carry / value', scope: 'global', aliases: ['FX'] },
  { code: 'CMDTY', title: 'Commodities', description: 'Cross-asset dashboard: commodities trend, carry, curves', scope: 'global' },
  { code: 'COT', title: 'CFTC positioning', description: 'Commitments of Traders positioning across futures markets', scope: 'global' },
  { code: 'EQS', title: 'Equity screening', description: 'Screener / ranking with live factor re-weighting', scope: 'global' },
  { code: 'PORT', title: 'Portfolio / backtest', description: 'Backtest Lab: point-in-time strategy backtests', scope: 'global', aliases: ['BT'] },
  { code: 'TOP', title: 'Top news', description: 'Top stories (News Terminal when available; else Global Events stories)', scope: 'global' },
  { code: 'N', title: 'News terminal', description: 'Live news terminal (falls back to Global Events when the terminal is not installed)', scope: 'global' },
  { code: 'ALRT', title: 'Alerts', description: 'Watchlist alert rules and fired alerts', scope: 'global' },
  { code: 'MDL', title: 'Model registry', description: 'Model Lab (model registry & monitoring)', scope: 'global' },
  { code: 'HELP', title: 'Help', description: 'Open the user guide (and list the mnemonics); HELP <MNEMONIC> describes one', scope: 'global' },
]

const BY_CODE = new Map<string, MnemonicDef>()
for (const m of MNEMONICS) {
  BY_CODE.set(m.code, m)
  for (const a of m.aliases ?? []) BY_CODE.set(a, m)
}

export function mnemonic(code: string): MnemonicDef | undefined {
  return BY_CODE.get(code.toUpperCase())
}

/** Bloomberg market codes the command bar understands (subset of the backend map in app/bql/tickers.py). */
export const MARKET_CODES = new Set([
  'US', 'UN', 'UW', 'UQ', 'UR', 'UA', 'UP', 'UF', 'CN', 'CT', 'CV', 'LN', 'GY', 'GR', 'GF', 'FP', 'NA', 'BB', 'PL', 'ID', 'IM',
  'SM', 'SW', 'SE', 'VX', 'SS', 'DC', 'FH', 'NO', 'AV', 'PW', 'GA', 'CP', 'HB', 'RO', 'ET', 'LH', 'IR', 'JP', 'JT', 'KS', 'KQ',
  'HK', 'CH', 'CG', 'C1', 'CS', 'C2', 'TT', 'TP', 'AU', 'AT', 'BZ', 'BS', 'MM', 'CI', 'CB', 'PE', 'AR', 'IN', 'IS', 'IB', 'SP',
  'SJ', 'TI', 'IT', 'AB',
])

export interface ParsedCommand {
  kind: 'empty' | 'mnemonic' | 'security' | 'bql' | 'help' | 'search'
  raw: string
  /** security reference text as typed, without the mnemonic: 'AAPL US Equity' */
  security?: string
  ticker?: string
  market?: string
  yellow?: AssetClass
  code?: string
  /** global mnemonic argument (WIRP US -> 'US') */
  arg?: string
  bql?: string
  helpFor?: string
}

const isTickerToken = (t: string) => /^[A-Z0-9][A-Z0-9.\-=^/:]{0,20}$/i.test(t)

function yellowOf(t: string): AssetClass | undefined {
  const u = t.toLowerCase()
  return YELLOW_KEYS.find((y) => y.toLowerCase() === u)
}

/** Parse the command bar input. Never throws. */
export function parseCommand(input: string): ParsedCommand {
  const raw = input
  const s = input.trim()
  if (!s) return { kind: 'empty', raw }
  if (/^bql\b/i.test(s)) {
    const q = s.slice(3).trim()
    return { kind: 'bql', raw, bql: q || undefined, code: 'BQL' }
  }
  if (/^get\s*\(/i.test(s)) return { kind: 'bql', raw, bql: s, code: 'BQL' }
  const toks = s.split(/\s+/)
  const up = toks.map((t) => t.toUpperCase())
  if (up[0] === 'HELP') {
    if (toks.length === 1) return { kind: 'mnemonic', raw, code: 'HELP' }
    return { kind: 'help', raw, helpFor: up[1], code: 'HELP' }
  }
  // <security tokens> <MNEMONIC>  ('RY CN' = the Canadian listing: CN is a market code before it is a mnemonic)
  const last = toks.length === 2 && up[1] === 'CN' ? undefined : mnemonic(up[up.length - 1])
  if (last && toks.length >= 2) {
    const sec = parseSecurity(toks.slice(0, -1))
    if (sec) return { kind: 'mnemonic', raw, code: last.code, ...sec }
  }
  // <MNEMONIC> [arg]
  const first = mnemonic(up[0])
  if (first && toks.length <= 2 && (first.scope !== 'security' || toks.length === 1)) {
    return { kind: 'mnemonic', raw, code: first.code, arg: toks[1] ? up[1] : undefined }
  }
  if (last && toks.length === 1) return { kind: 'mnemonic', raw, code: last.code }
  // a security alone (Enter = DES)
  const sec = toks.length <= 3 ? parseSecurity(toks) : null
  if (sec && (sec.market || sec.yellow)) return { kind: 'security', raw, ...sec }
  return { kind: 'search', raw }
}

function parseSecurity(toks: string[]): Pick<ParsedCommand, 'security' | 'ticker' | 'market' | 'yellow'> | null {
  if (!toks.length || toks.length > 4) return null
  const t = [...toks]
  let yellow: AssetClass | undefined
  const y = yellowOf(t[t.length - 1])
  if (y) {
    yellow = y
    t.pop()
  }
  let market: string | undefined
  if (t.length >= 2 && MARKET_CODES.has(t[t.length - 1].toUpperCase())) market = t.pop()!.toUpperCase()
  if (!t.length) return null
  // commodity generics may carry a space: 'C 1 Comdty'
  const ticker = t.join(' ')
  if (t.length > 1 && yellow !== 'Comdty') return null
  if (!t.every(isTickerToken)) return null
  const security = [ticker.toUpperCase(), market, yellow].filter(Boolean).join(' ')
  return { security, ticker: ticker.toUpperCase(), market, yellow }
}

/** Mnemonics to suggest after a security (by asset class; instruments get the market-wide subset). */
export function mnemonicsFor(cls: AssetClass | undefined): MnemonicDef[] {
  const c = cls ?? 'Equity'
  return MNEMONICS.filter((m) => m.scope !== 'global' && (m.classes ?? ALL).includes(c))
}

export function globalMnemonics(): MnemonicDef[] {
  return MNEMONICS.filter((m) => m.scope !== 'security')
}

export function helpText(code: string): string {
  const m = mnemonic(code)
  if (!m) return `${code.toUpperCase()}: unknown mnemonic. Type HELP for the list.`
  const form = m.scope === 'security' ? `<ticker> ${m.code}` : m.scope === 'both' ? `${m.code} or <ticker> ${m.code}` : m.code
  return `${m.code} — ${m.title}: ${m.description}. Usage: ${form}${m.unavailable ? ` (unavailable: ${m.unavailable})` : ''}`
}

/** Country / region code -> central bank id of the Central-bank monitor. */
export const CB_OF: Record<string, string> = {
  US: 'fed', EU: 'ecb', EZ: 'ecb', EA: 'ecb', DE: 'ecb', FR: 'ecb', IT: 'ecb', ES: 'ecb', NL: 'ecb', UK: 'boe', GB: 'boe',
  JP: 'boj', CH: 'snb', CA: 'boc', AU: 'rba', SE: 'riksbank', NO: 'norges', BR: 'bcb', MX: 'banxico', CL: 'bcch',
  CO: 'banrep', PE: 'bcrp', IN: 'rbi', KR: 'bok', CN: 'pboc',
  FED: 'fed', ECB: 'ecb', BOE: 'boe', BOJ: 'boj', SNB: 'snb', BOC: 'boc', RBA: 'rba', RIKSBANK: 'riksbank', NORGES: 'norges',
  BCB: 'bcb', BANXICO: 'banxico', BCCH: 'bcch', BANREP: 'banrep', BCRP: 'bcrp', RBI: 'rbi', BOK: 'bok', PBOC: 'pboc',
}

export interface RouteCtx {
  /** resolved security (security-scoped commands) */
  securityId?: string
  ticker?: string
  /** true for indices / FX / futures / ETFs (instrument page) */
  instrument?: boolean
  /** does the SPA have this route (the News Terminal may not be installed) */
  hasRoute: (path: string) => boolean
}

export interface CommandTarget {
  path: string
  label: string
  note?: string
}

const enc = encodeURIComponent

function companyTab(id: string, tab?: string): string {
  return `/company/${enc(id)}${tab ? `?tab=${tab}` : ''}`
}

/** Where a parsed mnemonic command goes. null = cannot route (missing security / unavailable). */
export function routeFor(cmd: ParsedCommand, ctx: RouteCtx): CommandTarget | null {
  const code = cmd.kind === 'security' ? 'DES' : cmd.code
  if (!code) return null
  const m = mnemonic(code)
  if (!m) return null
  const id = ctx.securityId
  const tic = ctx.ticker ?? cmd.ticker ?? id ?? ''
  const label = `${m.code} · ${m.title}`
  if (m.unavailable) return null
  if (code === 'BQL') {
    if (cmd.bql) return { path: `/bql?q=${enc(cmd.bql)}&run=1`, label: 'Run in the BQL console' }
    if (id) return { path: `/bql?q=${enc(`get(px_last, chg_pct_1m, pe_ttm, roic) for(['${(cmd.security ?? tic).replace(/'/g, "''")}'])`)}`, label }
    return { path: '/bql', label }
  }
  const news = ctx.hasRoute('/news')
  if (id && m.scope !== 'global') {
    if (ctx.instrument) {
      const ip = `/instruments/${enc(id)}`
      switch (code) {
        case 'DES':
        case 'GP':
        case 'GIP':
        case 'TECH':
          return { path: ip, label, note: code === 'GIP' ? 'daily data only' : undefined }
        case 'THES':
          return { path: `/theses/new?subject_type=cross_asset&subject_id=${enc(id)}`, label }
        default:
          return { path: ip, label, note: `${code} is for equities: opening the instrument page` }
      }
    }
    switch (code) {
      case 'DES':
        return { path: companyTab(id), label }
      case 'GP':
      case 'GIP':
        return { path: companyTab(id, 'technicals'), label, note: code === 'GIP' ? 'daily data only' : undefined }
      case 'TECH':
        return { path: companyTab(id, 'technicals'), label }
      case 'FA':
        return { path: companyTab(id, 'financials'), label }
      case 'DVD':
        return { path: companyTab(id, 'financials'), label, note: 'dividends: DPS, payout and yield rows of the statements' }
      case 'RV':
      case 'PEERS':
        return { path: companyTab(id, 'peers'), label }
      case 'EE':
        return { path: companyTab(id, 'forecast'), label, note: 'model-based, not consensus' }
      case 'ERN':
        return { path: companyTab(id, 'earnings'), label }
      case 'HDS':
      case 'SI':
      case 'SMRT':
        return { path: companyTab(id, 'ownership'), label }
      case 'CN':
        return news ? { path: `/news?ticker=${enc(tic)}`, label } : { path: companyTab(id, 'sentiment'), label, note: 'News Terminal not installed: news & sentiment tab' }
      case 'NET':
        return { path: companyTab(id, 'network'), label }
      case 'EVTS':
        return { path: companyTab(id, 'events'), label }
      case 'THES':
        return { path: `/theses/new?subject_type=security&subject_id=${enc(id)}`, label }
    }
  }
  if (m.scope === 'security') return null
  const arg = cmd.arg
  switch (code) {
    case 'WEI':
      return { path: '/macro/markets', label }
    case 'WIRP': {
      const bank = arg ? CB_OF[arg] : undefined
      return { path: bank ? `/macro/cb/${bank}` : '/macro/cb', label, note: arg && !bank ? `unknown bank / country ${arg}` : undefined }
    }
    case 'ECO':
      return { path: arg ? `/macro/calendar?country=${enc(arg)}` : '/macro/calendar', label }
    case 'ECST':
      return { path: `/macro/country/${enc(arg && arg.length === 2 ? arg : 'US')}`, label }
    case 'GC':
      return { path: '/macro/cb', label, note: 'policy and yield curves per bank' }
    case 'FXIP':
      return { path: '/macro/markets?tab=instruments', label }
    case 'CMDTY':
      return { path: '/macro/cross-asset', label }
    case 'COT':
      return { path: '/macro/positioning', label }
    case 'EQS':
      return { path: '/screener', label }
    case 'PORT':
      return { path: '/backtest', label }
    case 'TOP':
      return news ? { path: '/news?view=top', label } : { path: '/events', label, note: 'News Terminal not installed: Global Events stories' }
    case 'N':
      return news ? { path: '/news', label } : { path: '/events', label, note: 'News Terminal not installed: Global Events' }
    case 'ALRT':
      return { path: '/watchlist', label }
    case 'THES':
      return { path: '/theses', label }
    case 'MDL':
      return { path: '/models', label }
    case 'HDS':
    case 'SI':
    case 'SMRT':
      return { path: '/smart-money', label }
    case 'PEERS':
      return { path: '/sectors', label }
    case 'NET':
      return { path: '/events?tab=contagion', label }
    case 'EVTS':
      return { path: '/earnings', label }
    case 'HELP':
      return { path: '/help', label: `${label}: user guide`, note: 'user guide' }
  }
  return null
}

/* ── recent commands (per-viewer convenience; storage may be unavailable) ── */

const HISTORY_KEY = 'big360.cmd.history'
const HISTORY_MAX = 20

export function loadHistory(storage: Pick<Storage, 'getItem'> | null = safeLocal()): string[] {
  try {
    const v = JSON.parse(storage?.getItem(HISTORY_KEY) ?? '[]')
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, HISTORY_MAX) : []
  } catch {
    return []
  }
}

export function pushHistory(cmd: string, storage: Pick<Storage, 'getItem' | 'setItem'> | null = safeLocal()): string[] {
  const c = cmd.trim()
  if (!c) return loadHistory(storage)
  const next = [c, ...loadHistory(storage).filter((x) => x.toUpperCase() !== c.toUpperCase())].slice(0, HISTORY_MAX)
  try {
    storage?.setItem(HISTORY_KEY, JSON.stringify(next))
  } catch {
    /* storage blocked */
  }
  return next
}

function safeLocal(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}
