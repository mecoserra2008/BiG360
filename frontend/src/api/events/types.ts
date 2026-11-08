/**
 * Events v2 (labelled GDELT, stories, shocks) and event models (hotspots, spillovers, connectedness, IRF,
 * escalation, change points, GPR, event-study library, company exposure) — contract aliases + pure helpers
 * (unit-tested in __tests__/events.test.ts). Every /events/models response is model-based.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type EvTaxonomy = S['EventsV2Taxonomy']
export type EvTheme = EvTaxonomy['themes'][number]
export type EvMap = S['EventsV2Map']
export type EvMapCountry = S['EventsV2MapCountry']
export type EvStory = S['EventsV2Story']
export type EvStoryPage = S['EventsV2StoryPage']
export type EvStoryDetail = S['EventsV2StoryDetail']
export type EvShocks = S['EventsV2Shocks']
export type EvShock = S['EventsV2Shock']
export type EvLabelPanel = S['EventsV2LabelPanel']
export type EvTimeline = S['EventsV2Timeline']
export type EvEventDates = S['EventsV2EventDates']
export type EvLabelType = S['EventsV2Label']['type']
export type EmHotspots = S['EventsModelsHotspots']
export type EmHotspotItem = S['EventsModelsHotspotItem']
export type EmSpillovers = S['EventsModelsSpillovers']
export type EmConnectedness = S['EventsModelsConnectedness']
export type EmIrf = S['EventsModelsIrf']
export type EmEscalation = S['EventsModelsEscalation']
export type EmEscalationItem = S['EventsModelsEscalationItem']
export type EmChangepoints = S['EventsModelsChangepoints']
export type EmGpr = S['EventsModelsGpr']
export type EmStudies = S['EventsModelsEventStudies']
export type EmStudyCell = S['EventsModelsStudyCell']
export type EmCompany = S['EventsModelsCompany']

export type MapMetric = 'intensity' | 'attention_z' | 'tone' | 'conflict_share'

export const MODEL_LABEL = 'Model-based'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Value of the map metric for one country (null when not available). */
export function metricValue(c: EvMapCountry, m: MapMetric): number | null {
  const v = m === 'intensity' ? c.intensity : m === 'attention_z' ? c.attention_z : m === 'tone' ? c.tone : c.conflict_share
  return fin(v) ? v : null
}

/** Hotspot class → ordinal for the overlay legend (+3 hot99 … −3 cold99, 0 not significant). */
export const HOTSPOT_LEVEL: Record<string, number> = {
  hot99: 3,
  hot95: 2,
  hot90: 1,
  ns: 0,
  cold90: -1,
  cold95: -2,
  cold99: -3,
}
export const HOTSPOT_LABEL: Record<string, string> = {
  hot99: 'Hot spot 99%',
  hot95: 'Hot spot 95%',
  hot90: 'Hot spot 90%',
  ns: 'Not significant',
  cold90: 'Cold spot 90%',
  cold95: 'Cold spot 95%',
  cold99: 'Cold spot 99%',
}

/** "consecutive_hot" → "Consecutive hot spot". */
export function emergingLabel(cls: string | null | undefined): string {
  if (!cls || cls === 'none') return 'No pattern'
  const m = /^(.*)_(hot|cold)$/.exec(cls)
  if (!m) return cls.replace(/_/g, ' ')
  return `${m[1][0].toUpperCase()}${m[1].slice(1)} ${m[2]} spot`
}

/** Relative attention: log2 of a country's share of a theme vs that country's share of all coverage (heatmap fallback when z is unavailable). */
export function relativeAttention(themeArticles: number, themeTotal: number, countryArticles: number, total: number): number | null {
  if (themeArticles <= 0 || themeTotal <= 0 || countryArticles <= 0 || total <= 0) return null
  return Math.log2(themeArticles / themeTotal / (countryArticles / total))
}

/** Escalation fit is explosive when the (raw) branching ratio exceeds 1 — probabilities are then not reliable. */
export const isExplosive = (e: Pick<EmEscalationItem, 'branching_ratio'>): boolean => fin(e.branching_ratio) && e.branching_ratio > 1

/** Event-study CAR path for one cell key: points ordered by window end (−1,5 kept as a separate pre-event point). */
export function carPath(cells: EmStudyCell[]): {
  window: string
  end: number
  car: number | null
  lo: number | null
  hi: number | null
  n: number
  p: number | null
}[] {
  return cells
    .map((c) => {
      const [a, b] = c.window.split(',').map(Number)
      return {
        window: c.window,
        start: a,
        end: b,
        car: c.mean_car,
        lo: c.ci_low,
        hi: c.ci_high,
        n: c.n_events,
        p: c.p_value,
      }
    })
    .filter((x) => Number.isFinite(x.end))
    .sort((x, y) => x.end - y.end || x.start - y.start)
    .map(({ start: _s, ...x }) => x)
}

/** Grouping key of an event-study cell (everything except the window). */
export const studyKey = (c: EmStudyCell): string => [c.shock_kind, c.level, c.theme, c.iso2, c.sector].join('|')

/** Pearson correlation of paired finite values (null below 3 pairs). */
export function pearson(xs: (number | null | undefined)[], ys: (number | null | undefined)[]): { r: number | null; n: number } {
  const p: [number, number][] = []
  for (let i = 0; i < Math.min(xs.length, ys.length); i++) if (fin(xs[i]) && fin(ys[i])) p.push([xs[i] as number, ys[i] as number])
  const n = p.length
  if (n < 3) return { r: null, n }
  const mx = p.reduce((a, q) => a + q[0], 0) / n
  const my = p.reduce((a, q) => a + q[1], 0) / n
  let sxy = 0
  let sxx = 0
  let syy = 0
  for (const [x, y] of p) {
    sxy += (x - mx) * (y - my)
    sxx += (x - mx) ** 2
    syy += (y - my) ** 2
  }
  return { r: sxx > 0 && syy > 0 ? sxy / Math.sqrt(sxx * syy) : null, n }
}

/** Connectedness network: nodes sized by |net|, coloured transmitter (net > 0) / receiver; directed edges from pairs. */
export function connectednessGraph(c: EmConnectedness): {
  nodes: {
    id: string
    name: string
    net: number
    size: number
    role: 'transmitter' | 'receiver'
  }[]
  edges: { source: string; target: string; value: number }[]
} {
  const maxNet = Math.max(1e-9, ...c.nodes.map((n) => Math.abs(n.net ?? 0)))
  const ids = new Set(c.nodes.map((n) => n.node))
  return {
    nodes: c.nodes.map((n) => ({
      id: n.node,
      name: n.name ?? n.node,
      net: n.net ?? 0,
      size: 8 + 26 * (Math.abs(n.net ?? 0) / maxNet),
      role: (n.net ?? 0) >= 0 ? 'transmitter' : 'receiver',
    })),
    edges: c.pairs.filter((p) => ids.has(p.from) && ids.has(p.to) && fin(p.net)).map((p) => ({ source: p.from, target: p.to, value: p.net as number })),
  }
}

/** Story tone bucket for chips: −1 negative (≤ −2), +1 positive (≥ +2), 0 neutral. */
export function toneBucket(tone: number | null | undefined): -1 | 0 | 1 {
  if (!fin(tone)) return 0
  return tone <= -2 ? -1 : tone >= 2 ? 1 : 0
}

/** Story filter → API query (min sources is applied client-side: the API filters on articles only). */
export interface StoryFilter {
  country?: string
  sector?: string
  securityId?: string
  theme?: string
  tone?: 'all' | 'neg' | 'pos'
  minSources?: number
  days?: number
  start?: string
  end?: string
}
export function storyQuery(f: StoryFilter, limit = 40): Record<string, string | number | undefined> {
  return {
    country: f.country || undefined,
    sector: f.sector || undefined,
    security_id: f.securityId || undefined,
    theme: f.theme || undefined,
    tone_max: f.tone === 'neg' ? -2 : undefined,
    tone_min: f.tone === 'pos' ? 2 : undefined,
    start: f.start,
    end: f.end,
    days: f.start ? undefined : (f.days ?? 7),
    sort: 'salience',
    limit,
  }
}

/* ── history ranges (v1.20.0: windows up to 2,600 days; weekly buckets beyond 400 days) ── */
export type EvRange = '7D' | '30D' | '90D' | '1Y' | '3Y' | 'All'
export const EV_RANGES: EvRange[] = ['7D', '30D', '90D', '1Y', '3Y', 'All']
export const EV_MAX_DAYS = 2600
export type EvCoverage = NonNullable<EvTaxonomy['coverage']>

/** Days covered by a preset ending at `asOf`; All = from the first covered date (capped at the API limit). */
export function rangeDays(r: EvRange, coverage?: Pick<EvCoverage, 'first_date' | 'last_date'> | null, asOf?: string | null): number {
  const fixed: Record<Exclude<EvRange, 'All'>, number> = { '7D': 7, '30D': 30, '90D': 90, '1Y': 365, '3Y': 1095 }
  if (r !== 'All') return fixed[r]
  const first = coverage?.first_date
  const end = asOf ?? coverage?.last_date
  if (!first || !end) return 1095
  return Math.max(7, Math.min(EV_MAX_DAYS, Math.round((Date.parse(end) - Date.parse(first)) / 864e5) + 1))
}

/** Weekly buckets for ranges longer than a year. */
export const rangeFreq = (days: number): 'day' | 'week' => (days > 365 ? 'week' : 'day')

/** "News coverage: <first> → <last>" text parts (null when unknown). */
export function coverageText(c: Pick<EvCoverage, 'first_date' | 'last_date'> | null | undefined): string | null {
  return c?.first_date && c.last_date ? `${c.first_date} → ${c.last_date}` : null
}
