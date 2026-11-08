import type { BtConstruction, BtDraft, BtFromScreenRequest, BtMetrics, BtResult, BtSeries, BtSignalVar, BtSpec, BtStatus, BtUniverse } from './types'

export const isTerminal = (s: BtStatus | string | null | undefined): boolean => s === 'completed' || s === 'failed' || s === 'cancelled'

export const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** A sensible starting spec: US large caps, value factor, monthly long-short quintiles, 30% holdout. */
export function defaultSpec(): BtDraft {
  return {
    name: 'New backtest',
    mode: 'rank',
    universe: { kind: 'securities', universe_id: 'mvp_us', regions: [], countries: [], sectors: [], security_ids: [], asset_ids: [], asset_classes: [], screen: [], max_entities: 3000, primary_only: true },
    signal: { variables: [{ name: 'value', source: 'factor_scores', field: 'value', transforms: [], options: {}, weight: 1, higher_is_better: true }], combine: 'single', higher_is_better: true, lag_periods: 0 },
    construction: { rebalance: 'M', side: 'long_short', selection: 'quantile', quantiles: 5, weighting: 'equal', neutralize: 'none', holding_periods: 1, execution_lag_days: 1, min_names: 5, vol_lookback_days: 63 },
    event: null,
    costs: { enabled: true, commission_bps: 1, spread_model: 'adv_buckets', fixed_spread_bps: 5, impact_coef: 0.1, capital_usd: 10_000_000, borrow_bps: 50, htb_borrow_bps: 300, htb_adv_usd: 5_000_000, fx_bps: 2, currency: 'USD' },
    period: { start: null, end: null, oos_start: null, oos_fraction: 0.3, benchmark: null, rf: 'cash' },
    robustness: { permutations: 0, stale_check: true, sweep: {}, wf_folds: 4, cscv_blocks: 10, attribution: true, deciles: 10 },
    delisting_return: 0,
    seed: 42,
    timeout_s: 900,
  }
}

/** Contract spec (API / saved strategy / preset) -> editable draft with every block present. */
export function toDraft(spec: BtSpec): BtDraft {
  const d = defaultSpec()
  return {
    ...d,
    ...spec,
    universe: { ...d.universe, ...(spec.universe ?? {}) },
    construction: { ...d.construction, ...(spec.construction ?? {}) },
    costs: { ...d.costs, ...(spec.costs ?? {}) },
    period: { ...d.period, ...(spec.period ?? {}) },
    robustness: { ...d.robustness, ...(spec.robustness ?? {}) },
    event: spec.event ?? null,
  }
}

/** Unique, identifier-safe variable name. */
export function varName(base: string, taken: string[]): string {
  let s = base.replace(/[^A-Za-z0-9_]/g, '_').replace(/^[^A-Za-z]+/, '').slice(0, 30) || 'v'
  if (s.startsWith('f_') || s.startsWith('s_')) s = `x${s}`
  let out = s
  let i = 2
  while (taken.includes(out)) out = `${s}${i++}`
  return out
}

export function signalVar(source: string, field: string, taken: string[] = [], higherIsBetter = true): BtSignalVar {
  return { name: varName(field.split(':').pop() ?? field, taken), source, field, transforms: [], options: {}, weight: 1, higher_is_better: higherIsBetter }
}

/** Cross-asset cockpit -> cross-sectional assets backtest of one signal (e.g. trend_signal, carry). */
export function crossAssetSpec(metric: string, label: string, assetClass?: string | null): BtDraft {
  const s = defaultSpec()
  const cot = metric.startsWith('cot_')
  return {
    ...s,
    name: `Cross-asset ${label}${assetClass ? ` (${assetClass})` : ''}`.slice(0, 120),
    universe: { ...s.universe, kind: 'assets', universe_id: null, asset_classes: assetClass ? [assetClass] : [] },
    signal: { ...s.signal, variables: [signalVar(cot ? 'cftc' : 'cross_asset', metric, [], !cot)] },
    construction: { ...s.construction, rebalance: metric.startsWith('cot_') ? 'W' : 'M', side: 'long_short', quantiles: 3, weighting: 'inverse_vol' },
    period: { ...s.period, benchmark: 'universe_ew' },
  }
}

/** CFTC market -> event-driven contrarian strategy on positioning extremes (assets universe). */
export function positioningSpec(opts: { assetIds?: string[]; assetClass?: string | null; marketName: string }): BtDraft {
  const s = defaultSpec()
  return {
    ...s,
    name: `CFTC extremes · ${opts.marketName}`.slice(0, 120),
    mode: 'event',
    universe: { ...s.universe, kind: 'assets', universe_id: null, asset_ids: opts.assetIds ?? [], asset_classes: opts.assetIds?.length ? [] : opts.assetClass ? [opts.assetClass] : [] },
    signal: { ...s.signal, variables: [signalVar('cftc', 'cot_index_3y', [], false)] },
    event: { trigger: 'cftc_extreme', params: { high: 0.9, low: 0.1, mode: 'contrarian' }, hold_days: 20, max_positions: 10, allow_short: true, entry_lag_days: 1 },
    period: { ...s.period, benchmark: 'universe_ew' },
  }
}

/** Model Studio panel ML run -> backtest of its out-of-fold predictions (source studio_run). */
export function studioRunSpec(runId: string, label: string, universeId: string | null = 'mvp_us'): BtDraft {
  const s = defaultSpec()
  return {
    ...s,
    name: `Studio · ${label}`.slice(0, 120),
    universe: { ...s.universe, universe_id: universeId },
    signal: { ...s.signal, variables: [{ ...signalVar('studio_run', runId), name: 'studio_pred' }] },
    construction: { ...s.construction, side: 'long_short', quantiles: 5 },
  }
}

export interface ChartPoint {
  date: string
  value: number | null
}

/** NAV / drawdown / exposure series for charts (rebased to 1 at the start). */
export function seriesPoints(s: BtSeries, key: keyof BtSeries, step = 1): ChartPoint[] {
  const ys = s[key] as (number | null)[] | null | undefined
  if (!ys) return []
  const out: ChartPoint[] = []
  for (let i = 0; i < s.dates.length; i += step) out.push({ date: s.dates[i], value: fin(ys[i]) ? (ys[i] as number) : null })
  if (s.dates.length && (s.dates.length - 1) % step !== 0) out.push({ date: s.dates[s.dates.length - 1], value: fin(ys[ys.length - 1]) ? (ys[ys.length - 1] as number) : null })
  return out
}

/** Down-sampling step that keeps charts under ~maxPoints points. */
export const stepFor = (n: number, maxPoints = 1500): number => Math.max(1, Math.ceil(n / maxPoints))

export type MetricFmt = 'pct' | 'num' | 'x' | 'int' | 'bps' | 'days'

export const METRIC_ROWS: { key: keyof BtMetrics; label: string; fmt: MetricFmt; hint?: string }[] = [
  { key: 'cagr', label: 'CAGR', fmt: 'pct' },
  { key: 'total_return', label: 'Total return', fmt: 'pct' },
  { key: 'vol', label: 'Volatility', fmt: 'pct' },
  { key: 'sharpe', label: 'Sharpe', fmt: 'num', hint: 'excess of the 3m T-bill (macro_factors cash), 252-day annualisation' },
  { key: 'sortino', label: 'Sortino', fmt: 'num' },
  { key: 'max_drawdown', label: 'Max drawdown', fmt: 'pct' },
  { key: 'max_drawdown_days', label: 'Longest drawdown', fmt: 'days' },
  { key: 'calmar', label: 'Calmar', fmt: 'num' },
  { key: 'hit_rate', label: 'Hit rate (days)', fmt: 'pct' },
  { key: 'pct_months_positive', label: 'Positive months', fmt: 'pct' },
  { key: 'beta', label: 'Beta', fmt: 'num' },
  { key: 'alpha', label: 'Alpha (ann.)', fmt: 'pct' },
  { key: 'information_ratio', label: 'Information ratio', fmt: 'num' },
  { key: 'tracking_error', label: 'Tracking error', fmt: 'pct' },
  { key: 'excess_cagr', label: 'Excess CAGR vs bench', fmt: 'pct' },
  { key: 'bench_cagr', label: 'Benchmark CAGR', fmt: 'pct' },
  { key: 'bench_sharpe', label: 'Benchmark Sharpe', fmt: 'num' },
  { key: 'skew', label: 'Skew', fmt: 'num' },
  { key: 'kurtosis', label: 'Kurtosis', fmt: 'num' },
]

export function fmtMetric(v: unknown, f: MetricFmt): string {
  if (!fin(v)) return '–'
  switch (f) {
    case 'pct':
      return `${(v * 100).toFixed(Math.abs(v) < 0.1 ? 2 : 1)}%`
    case 'bps':
      return `${v.toFixed(0)} bp`
    case 'int':
      return Math.round(v).toLocaleString()
    case 'days':
      return `${Math.round(v)} d`
    case 'x':
      return `${v.toFixed(2)}×`
    default:
      return v.toFixed(2)
  }
}

/** Calendar grid of monthly returns: rows = years (newest first), 12 columns + the year's compounded total. */
export function monthlyGrid(monthly: BtResult['monthly']): { year: number; months: (number | null)[]; total: number | null }[] {
  const by = new Map<number, (number | null)[]>()
  for (const m of monthly ?? []) {
    const y = Number(m.month.slice(0, 4))
    const k = Number(m.month.slice(5, 7)) - 1
    if (!by.has(y)) by.set(y, Array(12).fill(null))
    ;(by.get(y) as (number | null)[])[k] = fin(m.ret) ? m.ret : null
  }
  return [...by.entries()]
    .sort((a, b) => b[0] - a[0])
    .map(([year, months]) => {
      const vals = months.filter(fin)
      return { year, months, total: vals.length ? vals.reduce((a, r) => a * (1 + r), 1) - 1 : null }
    })
}

/** Equal-width histogram (for permutation Sharpes / PBO logits). */
export function histogram(values: number[], bins = 20): { x0: number; x1: number; n: number }[] {
  const v = values.filter(fin)
  if (!v.length) return []
  let lo = Math.min(...v)
  let hi = Math.max(...v)
  if (lo === hi) {
    lo -= 0.5
    hi += 0.5
  }
  const w = (hi - lo) / bins
  const out = Array.from({ length: bins }, (_, i) => ({ x0: lo + i * w, x1: lo + (i + 1) * w, n: 0 }))
  for (const x of v) out[Math.min(bins - 1, Math.floor((x - lo) / w))].n += 1
  return out
}

/** One-line description of a spec (cards, strategy lists). */
export function describeSpec(s: BtSpec): string {
  const u: Partial<BtUniverse> = s.universe ?? {}
  const uni = u.kind === 'assets' ? `assets${u.asset_classes?.length ? ` (${u.asset_classes.join(', ')})` : u.asset_ids?.length ? ` (${u.asset_ids.length})` : ''}` : (u.universe_id ?? (u.security_ids?.length ? `${u.security_ids.length} names` : 'custom'))
  const sig = s.signal.combine === 'formula' ? `formula ${s.signal.formula ?? ''}` : s.signal.variables.map((v) => `${v.source}.${v.field}${v.higher_is_better === false ? '↓' : ''}`).join(s.signal.combine === 'single' ? '' : ' + ')
  if (s.mode === 'event' && s.event) return `${uni} · event ${s.event.trigger} · hold ${s.event.hold_days ?? 20}d`
  const c: Partial<BtConstruction> = s.construction ?? {}
  const sel = c.selection === 'top_n' ? `top ${c.top_n ?? 20}` : c.selection === 'all' ? 'all' : `q${c.quantiles ?? 5}`
  return `${uni} · ${sig} · ${c.side === 'long_short' ? 'L/S' : 'long'} ${sel} · ${c.weighting ?? 'equal'} · ${c.rebalance ?? 'M'}`
}

/** Number of sweep combinations of a spec. */
export function sweepSize(s: BtSpec): number {
  const sw = s.robustness?.sweep ?? {}
  const ks = Object.keys(sw)
  return ks.length ? ks.reduce((a, k) => a * Math.max(1, (sw[k] ?? []).length), 1) : 0
}

/** Parse "3, 5, 10" / "equal, inverse_vol" into typed sweep values. */
export function parseSweepValues(text: string): (string | number | boolean)[] {
  return text
    .split(',')
    .map((t) => t.trim())
    .filter(Boolean)
    .map((t) => (t === 'true' ? true : t === 'false' ? false : Number.isFinite(Number(t)) ? Number(t) : t))
}

/** Verdict on the deflated Sharpe ratio / PBO for the robustness tab badges. */
export function overfitVerdict(dsr: number | null | undefined, pbo: number | null | undefined, permP: number | null | undefined): { tone: 'up' | 'warn' | 'down' | 'neutral'; text: string } {
  if (!fin(dsr) && !fin(pbo) && !fin(permP)) return { tone: 'neutral', text: 'not enough information' }
  const bad = (fin(dsr) && dsr < 0.5) || (fin(pbo) && pbo > 0.5) || (fin(permP) && permP > 0.2)
  const good = (!fin(dsr) || dsr >= 0.95) && (!fin(pbo) || pbo < 0.2) && (!fin(permP) || permP <= 0.05)
  if (good) return { tone: 'up', text: 'survives the overfitting checks' }
  if (bad) return { tone: 'down', text: 'likely overfit or noise' }
  return { tone: 'warn', text: 'inconclusive: treat with caution' }
}

/** UI screener state -> POST /backtest/from-screen body (sector names are mapped to ids server-side). */
export interface ScreenState {
  regions: string[]
  country: string
  sector: string
  subsector: string
  bucket: '' | 'mega' | 'large' | 'mid' | 'small' | 'micro'
  moat: string
  minScore: number
  macroFilter: string
  text: string
  sortId: string | null
  sortDesc: boolean
  factorWeights: Record<string, number>
}

const SORT_FIELD: Record<string, string> = {
  growth: 'revenue_growth', roic: 'roic', fcfy: 'fcf_yield', evebit: 'ev_ebit', pe: 'pe_ttm', model: 'ml_expected_excess_3m',
  mcap: 'market_cap_usd', r1m: 'chg_1m', r12m: 'ret_12m', divYld: 'dividend_yield', netDebt: 'net_debt_ebitda',
}

export function screenRequest(st: ScreenState): { body: BtFromScreenRequest; notes: string[] } {
  const filters: { field: string; op: string; value?: unknown }[] = []
  const notes: string[] = []
  if (st.minScore > 0) filters.push({ field: 'composite', op: 'gte', value: st.minScore })
  const B: Record<string, [number | null, number | null]> = { mega: [200e9, null], large: [10e9, 200e9], mid: [2e9, 10e9], small: [0.3e9, 2e9], micro: [null, 0.3e9] }
  if (st.bucket) {
    const [lo, hi] = B[st.bucket]
    if (lo) filters.push({ field: 'market_cap_usd', op: 'gte', value: lo })
    if (hi) filters.push({ field: 'market_cap_usd', op: 'lt', value: hi })
  }
  if (st.moat) filters.push({ field: 'moat_class', op: 'eq', value: st.moat === 'insufficient' ? 'insufficient_data' : st.moat })
  if (st.macroFilter) filters.push({ field: 'macro_impact_rates_down_100bp', op: 'not_null' })
  if (st.text) notes.push('text filter ignored (a backtest needs a rule, not a name list)')
  const sid = st.sortId ?? 'score'
  let sort_by = 'composite'
  let factor_weights: Record<string, number> | null = null
  if (sid === 'score' || sid === 'rank') factor_weights = st.factorWeights
  else if (sid.startsWith('f_')) sort_by = sid === 'f_balanceSheet' ? 'balance_sheet' : sid.slice(2)
  else if (SORT_FIELD[sid]) {
    sort_by = SORT_FIELD[sid]
    if (sid === 'pe') notes.push('P/E NTM (forecast) has no point-in-time history: ranking on P/E TTM')
  }
  else notes.push(`sort column ${sid} has no point-in-time history: ranking on the composite`)
  const desc = sid === 'score' || sid === 'rank' ? true : st.sortDesc
  return {
    body: {
      universe_id: 'full', regions: st.regions, countries: st.country ? [st.country] : [], sector_ids: st.sector ? [st.sector] : [],
      subsector_ids: st.subsector ? [st.subsector] : [], filters: filters as BtFromScreenRequest['filters'], factor_weights,
      sort_by, sort_dir: desc ? 'desc' : 'asc', limit: 50, offset: 0, columns: [],
    },
    notes,
  }
}
