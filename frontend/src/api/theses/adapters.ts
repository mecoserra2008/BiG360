/** Pure helpers for the thesis tracker pages (board, thesis page, editor). Unit-tested in __tests__/theses.test.ts. */
import type {
  AutoStatus,
  BoardStatus,
  CloseReason,
  Direction,
  Horizon,
  OriginKind,
  SubjectType,
  Thesis,
  ThesisAlertRule,
  ThesisCatalyst,
  ThesisCreate,
  ThesisInvalidation,
  ThesisKpi,
  ThesisMarker,
  ThesisReason,
  ThesisUpdate,
} from './types'

export type Tone = 'neutral' | 'accent' | 'up' | 'down' | 'warn' | 'info'

export const AUTO_META: Record<AutoStatus, { label: string; tone: Tone; rank: number }> = {
  invalidated: { label: 'Invalidated', tone: 'down', rank: 0 },
  at_risk: { label: 'At risk', tone: 'warn', rank: 1 },
  on_track: { label: 'On track', tone: 'up', rank: 2 },
  unknown: { label: 'Unknown', tone: 'neutral', rank: 3 },
}
export const BOARD: { id: BoardStatus; label: string; hint: string }[] = [
  { id: 'idea', label: 'Idea', hint: 'Researching; performance measured from creation' },
  { id: 'active', label: 'Active', hint: 'Position taken; performance measured from activation' },
  { id: 'closed', label: 'Closed', hint: 'Exited, with outcome and post-mortem' },
]
export const DIRECTION_META: Record<Direction, { label: string; tone: Tone }> = {
  long: { label: 'Long', tone: 'up' },
  short: { label: 'Short', tone: 'down' },
  neutral: { label: 'Neutral', tone: 'neutral' },
}
export const SUBJECT_LABEL: Record<SubjectType, string> = {
  security: 'Security',
  sector: 'Sector',
  country: 'Country',
  macro_theme: 'Macro theme',
  cross_asset: 'Cross-asset',
}
export const SUBJECT_HINT: Record<SubjectType, string> = {
  security: 'Ticker or security id (e.g. MSFT)',
  sector: 'Sector id (e.g. TECH)',
  country: 'ISO2 country (e.g. DE)',
  macro_theme: "Theme, optionally with a country: 'inflation' or 'US:inflation'",
  cross_asset: 'Cross-asset id (e.g. CL, GC, EURUSD, UST10Y)',
}
export const HORIZONS: Horizon[] = ['1M', '3M', '6M', '12M', '24M', '36M+']
export const CLOSE_REASONS: { value: CloseReason; label: string }[] = [
  { value: 'target_hit', label: 'Target hit' },
  { value: 'stopped', label: 'Stopped out' },
  { value: 'invalidated', label: 'Thesis invalidated' },
  { value: 'time_expired', label: 'Time stop / horizon over' },
  { value: 'thesis_changed', label: 'Thesis changed' },
  { value: 'other', label: 'Other' },
]
export const REASON_TONE: Record<ThesisReason['level'], Tone> = { ok: 'up', info: 'info', risk: 'warn', invalid: 'down' }

export const autoMeta = (s: string | null | undefined) => AUTO_META[(s as AutoStatus) ?? 'unknown'] ?? AUTO_META.unknown

/** Kanban columns; inside a column the riskiest first (invalidated, at risk), then most recently updated. */
export function groupBoard(items: Thesis[]): Record<BoardStatus, Thesis[]> {
  const out: Record<BoardStatus, Thesis[]> = { idea: [], active: [], closed: [] }
  for (const t of items) out[t.status].push(t)
  for (const k of Object.keys(out) as BoardStatus[]) {
    out[k].sort((a, b) =>
      k === 'closed'
        ? (b.closed_at ?? b.updated_at).localeCompare(a.closed_at ?? a.updated_at)
        : autoMeta(a.auto_status).rank - autoMeta(b.auto_status).rank || b.updated_at.localeCompare(a.updated_at),
    )
  }
  return out
}

/** Allowed board moves from a column. */
export function moves(s: BoardStatus): BoardStatus[] {
  return s === 'idea' ? ['active', 'closed'] : s === 'active' ? ['closed', 'idea'] : ['active']
}

export const MOVE_LABEL: Record<BoardStatus, string> = { idea: 'Back to ideas', active: 'Activate', closed: 'Close' }

/** Horizontal price levels to draw on the chart (only those set). */
export function chartBands(b: { entry_lo?: number | null; entry_hi?: number | null; target?: number | null; stop?: number | null; inception_price?: number | null }) {
  const out: { key: 'entry_lo' | 'entry_hi' | 'target' | 'stop' | 'inception'; label: string; value: number }[] = []
  if (b.target != null) out.push({ key: 'target', label: 'Target', value: b.target })
  if (b.entry_hi != null) out.push({ key: 'entry_hi', label: 'Entry high', value: b.entry_hi })
  if (b.entry_lo != null) out.push({ key: 'entry_lo', label: 'Entry low', value: b.entry_lo })
  if (b.stop != null) out.push({ key: 'stop', label: 'Stop', value: b.stop })
  if (b.inception_price != null) out.push({ key: 'inception', label: 'Inception', value: b.inception_price })
  return out
}

export interface PlacedMarker {
  date: string
  value: number
  kind: ThesisMarker['kind']
  labels: string[]
}

/** Snap markers to the price series (first bar on or after the marker date, else the last bar) and stack same-day
 * markers of one kind into one symbol. Markers outside the plotted range are dropped. */
export function placeMarkers(markers: ThesisMarker[], bars: { date: string; close: number | null }[]): PlacedMarker[] {
  const pts = bars.filter((b): b is { date: string; close: number } => b.close != null)
  if (!pts.length) return []
  const first = pts[0].date
  const out = new Map<string, PlacedMarker>()
  for (const m of markers) {
    if (m.date < first) continue
    let lo = 0
    let hi = pts.length - 1
    let idx = pts.length - 1
    while (lo <= hi) {
      const mid = (lo + hi) >> 1
      if (pts[mid].date >= m.date) {
        idx = mid
        hi = mid - 1
      } else lo = mid + 1
    }
    if (m.date > pts[pts.length - 1].date) {
      if (m.kind !== 'catalyst') idx = pts.length - 1
      else continue // future catalysts are listed, not plotted
    }
    const p = pts[idx]
    const key = `${p.date}|${m.kind}`
    const cur = out.get(key)
    if (cur) cur.labels.push(m.label)
    else out.set(key, { date: p.date, value: p.close, kind: m.kind, labels: [m.label] })
  }
  return [...out.values()].sort((a, b) => a.date.localeCompare(b.date))
}

const OPS: Record<string, string> = { gt: '>', gte: '≥', lt: '<', lte: '≤', between: 'between' }

export function kpiCondition(k: Pick<ThesisKpi, 'op' | 'threshold' | 'threshold_hi'>, fmt: (v: number) => string = (v) => String(v)): string {
  if (k.op === 'between') return `between ${fmt(k.threshold)} and ${k.threshold_hi != null ? fmt(k.threshold_hi) : '?'}`
  return `${OPS[k.op] ?? k.op} ${fmt(k.threshold)}`
}

export function kpiSource(k: Pick<ThesisKpi, 'kind' | 'ref' | 'expr'>): string {
  if (k.kind === 'expr') return k.expr ?? ''
  if (k.kind === 'xa') return `cross-asset ${k.ref ?? ''}`
  if (k.kind === 'macro') return `macro cell ${k.ref ?? ''}`
  return k.ref ?? ''
}

export function kpiState(ok: boolean | null | undefined, role: string): { label: string; tone: Tone } {
  if (ok == null) return { label: 'No data', tone: 'neutral' }
  if (ok) return { label: 'OK', tone: 'up' }
  return role === 'invalidate' ? { label: 'Invalidates', tone: 'down' } : role === 'watch' ? { label: 'Watch', tone: 'info' } : { label: 'Breached', tone: 'warn' }
}

const RULE_TEXT: Record<string, string> = {
  price_cross: 'Price level',
  score_change: 'Score / grade change',
  earnings_upcoming: 'Upcoming earnings',
  earnings_surprise: 'Earnings surprise (model-based)',
  sentiment_shift: 'Sentiment shift',
  price_change_pct_1d: 'Large 1-day move',
  activist_13d: 'Activist 13D',
  insider_buy: 'Insider cluster buy',
  short_interest_spike: 'Short-interest spike',
  eu_short_position: 'EU / UK short position',
  macro_surprise: 'Macro surprise (model-based)',
  regime_change: 'Regime change',
  thesis_kpi: 'KPI threshold',
  thesis_review: 'Review reminder',
  thesis_status: 'Status change',
  thesis_catalyst: 'Catalyst date',
  thesis_positioning: 'CFTC positioning extreme',
  thesis_event_shock: 'Event shock',
}

export function ruleLabel(r: Pick<ThesisAlertRule, 'rule_type' | 'params'>, fmt: (v: number) => string = (v) => String(v)): string {
  const p = r.params as Record<string, unknown>
  const base = RULE_TEXT[r.rule_type] ?? r.rule_type
  if (r.rule_type === 'price_cross' && typeof p.level === 'number') {
    const band = typeof p.band === 'string' ? p.band.replace('_', ' ') : 'level'
    return `${band.charAt(0).toUpperCase()}${band.slice(1)}: crosses ${p.direction === 'below' ? 'below' : 'above'} ${fmt(p.level)}`
  }
  if (r.rule_type === 'thesis_kpi' && typeof p.label === 'string') return `${base}: ${p.label}`
  if (r.rule_type === 'thesis_review' && typeof p.every_days === 'number') return `${base} (every ${p.every_days} days)`
  if (r.rule_type === 'earnings_upcoming' && typeof p.days_ahead === 'number') return `${base} (${p.days_ahead} days ahead)`
  if ((r.rule_type === 'macro_surprise' || r.rule_type === 'regime_change') && Array.isArray(p.countries)) return `${base}: ${(p.countries as string[]).join(', ')}`
  return base
}

/* ───────────── editor draft ───────────── */

export interface Draft {
  title: string
  subjectType: SubjectType
  subjectId: string
  direction: Direction
  horizon: Horizon
  conviction: number
  status: BoardStatus
  thesisMd: string
  drivers: string
  catalysts: ThesisCatalyst[]
  kpis: ThesisKpi[]
  invalidation: ThesisInvalidation[]
  entryLo: string
  entryHi: string
  target: string
  stop: string
  priceId: string
  benchmarkId: string
  sectorBenchmarkId: string
  scenarioId: string
  studyId: string
  studioRunId: string
  backtestStrategyId: string
  backtestRunId: string
  tags: string
  reviewEveryDays: string
  originKind: OriginKind
  originRef: string
}

export const emptyDraft = (): Draft => ({
  title: '',
  subjectType: 'security',
  subjectId: '',
  direction: 'long',
  horizon: '12M',
  conviction: 3,
  status: 'idea',
  thesisMd: '',
  drivers: '',
  catalysts: [],
  kpis: [],
  invalidation: [],
  entryLo: '',
  entryHi: '',
  target: '',
  stop: '',
  priceId: '',
  benchmarkId: '',
  sectorBenchmarkId: '',
  scenarioId: '',
  studyId: '',
  studioRunId: '',
  backtestStrategyId: '',
  backtestRunId: '',
  tags: '',
  reviewEveryDays: '30',
  originKind: 'manual',
  originRef: '',
})

const num = (s: string): number | null => {
  const t = s.trim().replace(',', '.')
  if (!t) return null
  const v = Number(t)
  return Number.isFinite(v) ? v : null
}
const str = (s: string): string | null => (s.trim() ? s.trim() : null)
const lines = (s: string): string[] => s.split('\n').map((x) => x.replace(/^[-*]\s*/, '').trim()).filter(Boolean)
const tagList = (s: string): string[] => s.split(/[,;]/).map((x) => x.trim()).filter(Boolean)

/** Fields shared by create and update. Empty optional strings become null (= cleared). */
function common(d: Draft): ThesisUpdate {
  return {
    title: d.title.trim(),
    direction: d.direction,
    horizon: d.horizon,
    conviction: d.conviction,
    thesis_md: str(d.thesisMd),
    key_drivers: lines(d.drivers),
    catalysts: d.catalysts.map((c) => ({ ...c, date: c.date || null })),
    kpis: d.kpis,
    invalidation: d.invalidation,
    entry_lo: num(d.entryLo),
    entry_hi: num(d.entryHi),
    target: num(d.target),
    stop: num(d.stop),
    scenario_id: str(d.scenarioId),
    study_id: str(d.studyId),
    studio_run_id: str(d.studioRunId),
    backtest_strategy_id: str(d.backtestStrategyId),
    backtest_run_id: str(d.backtestRunId),
    tags: tagList(d.tags),
    review_every_days: num(d.reviewEveryDays) ? Math.round(num(d.reviewEveryDays) as number) : null,
  }
}

export function draftToCreate(d: Draft): ThesisCreate {
  const body: ThesisCreate = { ...common(d), title: d.title.trim(), subject_type: d.subjectType, subject_id: d.subjectId.trim(), status: d.status === 'closed' ? 'idea' : d.status, origin: { kind: d.originKind, ref: str(d.originRef) } }
  // empty overrides -> server defaults for the subject
  if (str(d.priceId)) body.price_id = d.priceId.trim()
  if (str(d.benchmarkId)) body.benchmark_id = d.benchmarkId.trim()
  if (str(d.sectorBenchmarkId)) body.sector_benchmark_id = d.sectorBenchmarkId.trim()
  return body
}

export function draftToUpdate(d: Draft): ThesisUpdate {
  return { ...common(d), price_id: str(d.priceId), benchmark_id: str(d.benchmarkId), sector_benchmark_id: str(d.sectorBenchmarkId) }
}

const s = (v: number | null | undefined): string => (v == null ? '' : String(v))

export function draftFromThesis(t: Thesis): Draft {
  return {
    title: t.title,
    subjectType: t.subject_type,
    subjectId: t.subject_id,
    direction: t.direction,
    horizon: t.horizon,
    conviction: t.conviction,
    status: t.status,
    thesisMd: t.thesis_md ?? '',
    drivers: (t.key_drivers ?? []).join('\n'),
    catalysts: t.catalysts ?? [],
    kpis: t.kpis ?? [],
    invalidation: t.invalidation ?? [],
    entryLo: s(t.entry_lo),
    entryHi: s(t.entry_hi),
    target: s(t.target),
    stop: s(t.stop),
    priceId: t.price_id ?? '',
    benchmarkId: t.benchmark_id ?? '',
    sectorBenchmarkId: t.sector_benchmark_id ?? '',
    scenarioId: t.scenario_id ?? '',
    studyId: t.study_id ?? '',
    studioRunId: t.studio_run_id ?? '',
    backtestStrategyId: t.backtest_strategy_id ?? '',
    backtestRunId: t.backtest_run_id ?? '',
    tags: (t.tags ?? []).join(', '),
    reviewEveryDays: t.review_every_days == null ? '' : String(t.review_every_days),
    originKind: t.origin?.kind ?? 'manual',
    originRef: t.origin?.ref ?? '',
  }
}

/** Client-side checks mirrored from the API (the server re-validates). */
export function validateDraft(d: Draft): string[] {
  const e: string[] = []
  if (!d.title.trim()) e.push('Title is required')
  if (!d.subjectId.trim()) e.push('Subject is required')
  const lo = num(d.entryLo)
  const hi = num(d.entryHi)
  const tg = num(d.target)
  const sp = num(d.stop)
  if (lo != null && hi != null && lo > hi) e.push('Entry low must be below entry high')
  if (tg != null && sp != null && d.direction === 'long' && tg <= sp) e.push('A long thesis needs the target above the stop')
  if (tg != null && sp != null && d.direction === 'short' && tg >= sp) e.push('A short thesis needs the target below the stop')
  for (const k of d.kpis) {
    if (!k.label.trim()) e.push('Every KPI needs a label')
    if (k.kind === 'expr' ? !k.expr?.trim() : !k.ref?.trim()) e.push(`KPI ${k.label || '?'}: ${k.kind === 'expr' ? 'expression' : 'reference'} is required`)
    if (!Number.isFinite(k.threshold)) e.push(`KPI ${k.label || '?'}: threshold must be a number`)
  }
  return e
}

/* ───────────── "Create thesis" entry points ───────────── */

export interface NewThesisLink {
  subjectType?: SubjectType
  subjectId?: string
  title?: string
  origin?: OriginKind
  ref?: string
  studyId?: string
  studioRunId?: string
  backtestStrategyId?: string
  backtestRunId?: string
  scenarioId?: string
  direction?: Direction
}

const PARAM: Record<keyof NewThesisLink, string> = {
  subjectType: 'subject_type',
  subjectId: 'subject_id',
  title: 'title',
  origin: 'origin',
  ref: 'ref',
  studyId: 'study_id',
  studioRunId: 'studio_run_id',
  backtestStrategyId: 'backtest_strategy_id',
  backtestRunId: 'backtest_run_id',
  scenarioId: 'scenario_id',
  direction: 'direction',
}

export function newThesisHref(l: NewThesisLink): string {
  const q = new URLSearchParams()
  for (const [k, p] of Object.entries(PARAM) as [keyof NewThesisLink, string][]) {
    const v = l[k]
    if (v) q.set(p, String(v))
  }
  const qs = q.toString()
  return `/theses/new${qs ? `?${qs}` : ''}`
}

const SUBJECTS = new Set<SubjectType>(['security', 'sector', 'country', 'macro_theme', 'cross_asset'])
const ORIGINS = new Set<OriginKind>(['manual', 'company', 'screener', 'studio', 'cross_asset', 'backtest'])

export function draftFromParams(p: URLSearchParams): Draft {
  const d = emptyDraft()
  const st = p.get('subject_type') as SubjectType | null
  if (st && SUBJECTS.has(st)) d.subjectType = st
  d.subjectId = p.get('subject_id') ?? ''
  d.title = p.get('title') ?? ''
  const o = p.get('origin') as OriginKind | null
  if (o && ORIGINS.has(o)) d.originKind = o
  d.originRef = p.get('ref') ?? ''
  d.studyId = p.get('study_id') ?? ''
  d.studioRunId = p.get('studio_run_id') ?? ''
  d.backtestStrategyId = p.get('backtest_strategy_id') ?? ''
  d.backtestRunId = p.get('backtest_run_id') ?? ''
  d.scenarioId = p.get('scenario_id') ?? ''
  const dir = p.get('direction')
  if (dir === 'long' || dir === 'short' || dir === 'neutral') d.direction = dir
  return d
}

export const newId = (prefix: string): string => `${prefix}${Math.random().toString(36).slice(2, 9)}`

/** Days until a date (negative = past); null for missing. */
export function daysUntil(date: string | null | undefined, today: Date = new Date()): number | null {
  if (!date) return null
  const t = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate())
  const d = Date.parse(`${date.slice(0, 10)}T00:00:00Z`)
  return Number.isFinite(d) ? Math.round((d - t) / 86_400_000) : null
}
