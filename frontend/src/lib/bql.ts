/** BQL-lite helpers for the console: a lexer mirroring app/bql/grammar.py (syntax highlighting), the word under the
 * cursor (autocomplete), unit formatting, examples and the Screener -> BQL translation. */

import { fmtCompact, fmtNum, fmtPct } from './format'

export type TokKind = 'clause' | 'keyword' | 'func' | 'field' | 'string' | 'number' | 'date' | 'duration' | 'op' | 'punct' | 'ws' | 'error'

export interface Tok {
  kind: TokKind
  text: string
  start: number
}

export const CLAUSES = new Set(['get', 'for', 'with', 'where', 'group', 'sort', 'limit'])
export const KEYWORDS = new Set(['and', 'or', 'not', 'in', 'true', 'false', 'null', 'asc', 'desc', 'as', 'agg'])
export const FUNCTIONS = [
  'chg', 'chg_pct', 'lag', 'avg', 'mean', 'std', 'min', 'max', 'sum', 'median', 'count', 'zscore', 'pct_rank', 'rolling',
  'resample', 'ytd', 'corr', 'beta', 'rank', 'cs_rank', 'cs_zscore', 'cs_demean', 'cs_mean', 'cs_median', 'abs', 'ln',
  'log', 'log10', 'exp', 'sqrt', 'sign', 'floor', 'ceil', 'round', 'least', 'greatest', 'coalesce', 'nz', 'if', 'clip',
  'isnull', 'members', 'region', 'sector', 'subsector', 'country', 'type', 'exchange', 'currency', 'range',
]
const FUNC_SET = new Set(FUNCTIONS)

const RX: [TokKind, RegExp][] = [
  ['ws', /^\s+/],
  ['date', /^\d{4}-\d{2}-\d{2}(?![\w])/],
  ['duration', /^\d+[DWMQYdwmqy](?![A-Za-z0-9_])/],
  ['number', /^(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?%?/],
  ['string', /^'(?:[^']|'')*'|^"(?:[^"]|"")*"/],
  ['field', /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*/],
  ['op', /^(?:<=|>=|==|!=|<>|[-+*/^&|<>=])/],
  ['punct', /^[(),[\].]/],
]

/** Tokenize for highlighting. Never throws: unknown characters and unterminated strings become `error` tokens. */
export function tokenize(src: string): Tok[] {
  const out: Tok[] = []
  let i = 0
  while (i < src.length) {
    const rest = src.slice(i)
    let hit: [TokKind, string] | null = null
    for (const [k, rx] of RX) {
      const m = rx.exec(rest)
      if (m) {
        hit = [k, m[0]]
        break
      }
    }
    if (!hit) {
      const q = rest[0] === "'" || rest[0] === '"'
      hit = ['error', q ? rest : rest[0]]
    }
    let [kind, text] = hit
    if (kind === 'field') {
      const low = text.toLowerCase()
      const next = src.slice(i + text.length).match(/^\s*\(/)
      if (CLAUSES.has(low) && next) kind = 'clause'
      else if (KEYWORDS.has(low)) kind = 'keyword'
      else if (next && FUNC_SET.has(low)) kind = 'func'
      else if (next) kind = 'func'
    }
    out.push({ kind, text, start: i })
    i += text.length
  }
  return out
}

/** The identifier being typed at `pos` (for autocomplete): {word, start}. */
export function wordAt(src: string, pos: number): { word: string; start: number } {
  let s = pos
  while (s > 0 && /[A-Za-z0-9_.]/.test(src[s - 1])) s--
  const word = src.slice(s, pos)
  // inside a string literal -> no completion
  const before = src.slice(0, s)
  const quotes = (before.match(/'/g) ?? []).length
  if (quotes % 2 === 1 || /^\d/.test(word)) return { word: '', start: pos }
  return { word, start: s }
}

/** Format one BQL cell by its unit (pct = fraction, pctile = 0..100). */
export function fmtBqlValue(v: unknown, unit: string | null | undefined): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'boolean') return v ? 'true' : 'false'
  if (typeof v !== 'number') return Array.isArray(v) ? v.join(', ') : String(v)
  if (!Number.isFinite(v)) return '—'
  switch (unit) {
    case 'pct':
      return fmtPct(v, Math.abs(v) < 0.001 && v !== 0 ? 3 : 1)
    case 'pctile':
    case 'score':
      return fmtNum(v, 1)
    case 'price':
      return Math.abs(v) >= 1e5 ? fmtCompact(v, 2) : fmtNum(v, 2)
    case 'currency_usd':
    case 'currency':
      return Math.abs(v) >= 1e5 ? fmtCompact(v, 2) : fmtNum(v, 2)
    case 'shares':
    case 'count':
      return Math.abs(v) >= 1e5 ? fmtCompact(v, 2) : fmtNum(v, 0)
    case 'x':
    case 'ratio':
    case 'z':
      return fmtNum(v, 2)
    case 'rank':
    case 'decile':
      return fmtNum(v, 0)
    default:
      return Math.abs(v) >= 1e6 ? fmtCompact(v, 2) : Math.abs(v) >= 100 ? fmtNum(v, 2) : fmtNum(v, 4)
  }
}

export const EXAMPLES: { title: string; query: string }[] = [
  { title: 'Quality tech at a discount (cross-section)', query: "get(px_last, chg_pct(px, 1M), pe_fwd, roic, tech.hi52_pct)\n  for(members('S&P 500') & sector('Tech'))\n  where(roic > 15% and tech.ivol_pct < 30)\n  sort(chg_pct desc) limit(50)" },
  { title: 'Weekly price history, two years, in USD', query: "get(px_last, chg_pct(px, 1M))\n  for(['AAPL US', 'SAP GY', '7203 JT'])\n  with(dates=range(-2Y, 0D), freq=W, currency=USD)" },
  { title: 'Sector medians', query: "get(chg_pct_3m, roic, pe_ttm, div_yld)\n  for(members('mvp_us'))\n  group(sector).agg(median)" },
  { title: 'Point-in-time screen as of a past date', query: "get(roic, pe_ttm, mkt_cap_usd)\n  for(members('mvp_us'))\n  with(as_of=2024-06-28)\n  where(roic > 20%)\n  sort(pe_ttm asc) limit(25)" },
  { title: 'Cross-sectional z-scores and ranks', query: "get(cs_zscore(chg_pct(px, 3M)) as mom_z, cs_zscore(roic, by=sector) as roic_z, rank(mkt_cap_usd) as size_rank)\n  for(members('eu_broad'))\n  sort(mom_z desc) limit(30)" },
  { title: 'Beta and correlation to the S&P 500', query: "get(beta(px, 'SPX Index', 1Y), corr(px, 'SPX Index', 6M), vol_1y)\n  for(members('mvp_us') & sector('Utilities'))\n  sort(vol_1y asc)" },
  { title: 'Monthly ROIC and revenue history (filing dates)', query: "get(roic, fa.revenue, f.quality)\n  for(['MSFT US'])\n  with(dates=range(-5Y, 0D), freq=Q)" },
  { title: 'Smart money and short interest', query: "get(sm.score, si.si_pct_out, si.days_to_cover, own.inst_own_pct)\n  for(members('mvp_us'))\n  where(si.si_pct_out > 5%)\n  sort(si.si_pct_out desc) limit(20)" },
  { title: 'Indices, FX and commodities', query: "get(px_last, chg_pct_1m, chg_pct_ytd)\n  for(['SPX Index', 'SX5E Index', 'NKY Index', 'EURUSD Curncy', 'CL1 Comdty', 'GC1 Comdty'])" },
]

/* ── Screener -> BQL ── */

export interface ScreenState {
  regions: string[]
  country?: string
  sector?: string
  subsector?: string
  bucket?: string
  moat?: string
  minScore?: number
  sortId?: string | null
  sortDesc?: boolean
}

/** Screener column id -> BQL field. */
export const SCREENER_SORT_FIELD: Record<string, string> = {
  score: 'composite', mcap: 'mkt_cap_usd', price: 'px_last', d1: 'chg_pct_1d', r1m: 'chg_pct_1m', r12m: 'chg_pct_12m',
  pe: 'pe_fwd', evebit: 'ev_ebit', fcfy: 'fcf_yield', roic: 'roic', growth: 'revenue_growth_yoy', netDebt: 'net_debt_to_ebitda',
  divYld: 'div_yld', model: 'ml.exp_excess_3m', mxDown100: 'macro.rates_down_100bp', mxRates: 'macro.beta_rates',
  mxUsd: 'macro.beta_usd', mxOil: 'macro.beta_oil', mxCredit: 'macro.beta_credit', mxGrowth: 'macro.beta_growth',
  f_quality: 'f.quality', f_profitability: 'f.profitability', f_growth: 'f.growth', f_value: 'f.value',
  f_balance_sheet: 'f.balance_sheet', f_balanceSheet: 'f.balance_sheet', f_momentum: 'f.momentum', f_insider: 'f.insider', f_sentiment: 'f.sentiment',
  f_macro: 'f.macro', f_moat: 'f.moat',
}

const BUCKET_WHERE: Record<string, string> = {
  mega: 'mkt_cap_usd >= 200e9', large: 'mkt_cap_usd >= 10e9 and mkt_cap_usd < 200e9', mid: 'mkt_cap_usd >= 2e9 and mkt_cap_usd < 10e9',
  small: 'mkt_cap_usd >= 0.3e9 and mkt_cap_usd < 2e9', micro: 'mkt_cap_usd < 0.3e9',
}

const q = (s: string) => `'${s.replace(/'/g, "''")}'`

/** The Screener's filters as a BQL query (the live custom-weight composite maps to the stored balanced composite). */
export function screenerToBql(s: ScreenState): { query: string; notes: string[] } {
  const notes: string[] = []
  const uni = ["members('full')"]
  if (s.regions.length) uni.push(`region(${s.regions.map(q).join(', ')})`)
  if (s.country) uni.push(`country(${q(s.country)})`)
  if (s.sector) uni.push(`sector(${q(s.sector)})`)
  if (s.subsector) uni.push(`subsector(${q(s.subsector)})`)
  const where: string[] = []
  if (s.bucket && BUCKET_WHERE[s.bucket]) where.push(BUCKET_WHERE[s.bucket])
  if (s.moat) where.push(`moat == ${q(s.moat === 'insufficient' ? 'insufficient_data' : s.moat)}`)
  if (s.minScore && s.minScore > 0) {
    where.push(`composite >= ${s.minScore}`)
    notes.push('min score uses the stored balanced composite (the Screener re-weights live)')
  }
  const sortField = (s.sortId && SCREENER_SORT_FIELD[s.sortId]) || 'composite'
  const fields = ['composite', 'mkt_cap_usd', 'chg_pct_1m', 'pe_fwd', 'ev_ebit', 'roic', 'f.quality', 'f.value', 'f.momentum']
  if (!fields.includes(sortField)) fields.push(sortField)
  const lines = [`get(${fields.join(', ')})`, `  for(${uni.join(' & ')})`]
  if (where.length) lines.push(`  where(${where.join(' and ')})`)
  lines.push(`  sort(${sortField} ${s.sortDesc === false ? 'asc' : 'desc'}) limit(100)`)
  return { query: lines.join('\n'), notes }
}

export interface BqlHistoryEntry {
  q: string
  at: string
}

/** History entries from localStorage, validated (guide 14: a malformed entry crashed the History tab). Accepts the
 *  current {q, at} objects and legacy plain strings; drops anything else, empty and duplicate queries; max 30. */
export function parseBqlHistory(raw: unknown, max = 30): BqlHistoryEntry[] {
  let v = raw
  if (typeof v === 'string') {
    try {
      v = JSON.parse(v)
    } catch {
      return []
    }
  }
  if (!Array.isArray(v)) return []
  const out: BqlHistoryEntry[] = []
  const seen = new Set<string>()
  for (const x of v) {
    const q = typeof x === 'string' ? x : x && typeof x === 'object' && typeof (x as { q?: unknown }).q === 'string' ? (x as { q: string }).q : null
    if (!q || !q.trim() || seen.has(q)) continue
    const a = x && typeof x === 'object' ? (x as { at?: unknown }).at : null
    const at = typeof a === 'string' && Number.isFinite(Date.parse(a)) ? a : ''
    seen.add(q)
    out.push({ q, at })
    if (out.length >= max) break
  }
  return out
}
