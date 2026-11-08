import type { CSystemStatus } from '../contract'
import type { DataGap, DataHealthCampaign, DataHealthDataset, HeaderFreshness, OpsAlert, StatusCacheView } from './types'

const CACHE_STATES = new Set(['fresh', 'stale', 'partial', 'warming', 'error'])

/**
 * Coverage v3 A7: the status cache block of /system/status (null on older backends). A failed data_freshness /
 * ops_status section (or no full payload yet) means the freshness rows and ops alerts are not current.
 */
export function statusCacheView(st: CSystemStatus): StatusCacheView | null {
  const c = st.cache
  if (!c || typeof c !== 'object') return null
  const sectionErrors = (c.section_errors ?? []).filter((e): e is string => typeof e === 'string')
  const failedSections = [...new Set(sectionErrors.map((e) => e.split(':')[0].trim()).filter(Boolean))]
  const raw = String(c.state)
  // an older backend reports 'fresh' with section errors: still partial
  const state = (sectionErrors.length && (raw === 'fresh' || raw === 'stale') ? 'partial' : CACHE_STATES.has(raw) ? raw : 'partial') as StatusCacheView['state']
  return {
    state,
    computedAt: c.computed_at ?? st.as_of ?? null,
    failedSections,
    sectionErrors,
    sectionAsOf: { ...(c.section_as_of ?? {}) },
    freshnessUnknown: state === 'warming' || state === 'error' || failedSections.includes('data_freshness') || failedSections.includes('ops_status'),
  }
}

/** System page banner for a status payload that is not fully current (null when it is). */
export function statusCacheAlert(c: StatusCacheView | null | undefined): OpsAlert | null {
  if (!c) return null
  if (c.state === 'error') {
    return { id: 'status_unavailable', severity: 'error', title: 'Status unavailable', detail: `The status computation failed and no earlier result exists: freshness, jobs and alerts below are empty. ${c.sectionErrors[0] ?? ''}`.trim() }
  }
  if (c.state === 'warming') {
    return { id: 'status_loading', severity: 'info', title: 'Status loading', detail: 'The API just started: freshness, jobs and alerts appear once the first status computation finishes.' }
  }
  if (c.state !== 'partial') return null
  const since = c.failedSections.map((s) => (c.sectionAsOf[s] ? `${s} (from ${c.sectionAsOf[s]})` : `${s} (empty)`))
  return {
    id: 'status_partial',
    severity: 'warn',
    title: 'Status partially stale',
    detail: `These sections failed to refresh and show their last good value or nothing: ${since.join(', ')}. ${c.sectionErrors[0] ?? ''}`.trim(),
  }
}

/** Optional / realtime tables never degrade the header badge (F13: `live_macro` made it read "Error" permanently). */
export const isOptionalTable = (t: string, optional?: boolean | null): boolean => Boolean(optional) || t.startsWith('live_')

/** Tables whose absence is a real error (everything else missing = pending pipeline). */
const CRITICAL_TABLES = new Set(['prices_daily', 'fx_rates', 'macro_observations'])

/**
 * Header summary from /system/status: the DATA as-of date (per region in the tooltip), not the snapshot rebuild
 * time, plus a warning chip when the scheduler is off or core data is stale beyond the profile SLA (F01 / F03 / F13).
 * With serving_sections (O1) the badge reflects the snapshot's per-section status: a failed / missing price section
 * = error, any stale market section = stale, stale / failed analytics sections = an "Analytics stale" chip only.
 * Works against pre-1.28 backends (no scheduler / data_as_of fields): then only non-optional tables count.
 */
export function headerFreshness(st: CSystemStatus): HeaderFreshness {
  const items = st.data_freshness ?? []
  const relevant = items.filter((f) => !isOptionalTable(f.table, f.optional))
  const bad = relevant.filter((f) => f.status !== 'fresh')
  const alerts = (st.ops_alerts ?? []) as OpsAlert[]
  const regions = (st.data_as_of ?? {}) as Record<string, string>
  const asOf = st.data_as_of_overall ?? st.serving_as_of ?? null
  const sched = st.scheduler ?? null
  const schedulerOn = sched ? Boolean(sched.enabled) : null
  const has = (id: string) => alerts.some((a) => a.id === id)
  const dataStale = has('sla_critical') || has('refresh_overdue')
  const missingCritical = relevant.some((f) => f.status === 'missing' && CRITICAL_TABLES.has(f.table))
  // F13 (v1.28 serving_sections): the badge follows the published snapshot's per-section status - market sections
  // (prices per region, FX, macro, filings, events) drive fresh / stale / error, analytics sections only add a chip
  const sections = st.serving_sections ?? []
  const secBad = sections.filter((x) => x.status !== 'fresh')
  const market = secBad.filter((x) => (x.group ?? 'market') === 'market')
  const analytics = secBad.filter((x) => x.group === 'analytics')
  const pricesBroken = market.some((x) => x.section.startsWith('prices:') && (x.status === 'failed' || x.status === 'missing'))
  const cache = statusCacheView(st)
  let status: HeaderFreshness['status'] = 'fresh'
  if (missingCritical || pricesBroken) status = 'error'
  else if (dataStale || market.length > 0) status = 'stale'
  else if (!st.ops_alerts && bad.length) status = 'stale' // pre-1.28 backend: table-level freshness only
  else if (items.length === 0 && sections.length === 0) status = 'stale'
  else if (cache?.freshnessUnknown) status = 'stale' // coverage v3 A7: reused / missing freshness is never "fresh"
  let chip = ''
  let chipTone: HeaderFreshness['chipTone'] = null
  if (has('worker_down')) {
    chip = 'Worker off'
    chipTone = 'down'
  } else if (schedulerOn === false) {
    chip = 'Scheduler off'
    chipTone = (sched?.off_hours ?? 0) > 24 ? 'down' : 'warn'
  } else if (dataStale || market.length > 0) {
    chip = pricesBroken ? 'Prices failed' : 'Data stale'
    chipTone = pricesBroken ? 'down' : 'warn'
  } else if (analytics.length > 0) {
    chip = analytics.some((x) => x.status === 'failed') ? 'Analytics failed' : 'Analytics stale'
    chipTone = 'warn'
  } else if (cache && cache.state === 'error') {
    chip = 'Status unavailable'
    chipTone = 'down'
  } else if (cache && cache.state === 'warming') {
    chip = 'Status loading'
    chipTone = 'warn'
  } else if (cache && cache.state === 'partial') {
    chip = 'Status partially stale'
    chipTone = 'warn'
  }
  return {
    asOf,
    regions,
    builtAt: st.serving_built_at ?? null,
    status,
    schedulerOn,
    profile: sched?.profile ?? null,
    chip,
    chipTone,
    alerts,
    staleSources: sections.length ? secBad.map((x) => `${x.section} (${x.status})`) : [...new Set(bad.map((b) => b.table))],
    sections: sections.map((x) => ({ section: x.section, group: x.group ?? null, status: x.status, dataAsOf: x.data_as_of ?? null, detail: x.detail ?? null })),
    statusCache: cache,
  }
}

/** Ordered region list for tooltips (core regions first). */
const REGION_ORDER = ['US', 'EU', 'UK', 'JP', 'KR', 'CN', 'HK', 'CA', 'LATAM', 'APAC', 'OTHER']
export function sortedRegions(regions: Record<string, string>): [string, string][] {
  return Object.entries(regions).sort((a, b) => {
    const ia = REGION_ORDER.indexOf(a[0])
    const ib = REGION_ORDER.indexOf(b[0])
    return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a[0].localeCompare(b[0])
  })
}

/** Heatmap cell tone: coverage ratio actual / expected (null = no session that day). */
export function cellTone(v: number | null | undefined, afterAsOf = false): 'none' | 'full' | 'ok' | 'partial' | 'missing' | 'pending' {
  if (v === null || v === undefined) return 'none'
  if (afterAsOf && v < 0.8) return 'pending' // after the dataset's data as-of: not ingested yet (staleness), not a hole
  if (v >= 0.95) return 'full'
  if (v >= 0.8) return 'ok'
  if (v >= 0.05) return 'partial'
  return 'missing'
}

export const CELL_CLASS: Record<ReturnType<typeof cellTone>, string> = {
  none: 'bg-surface-2',
  full: 'bg-up',
  ok: 'bg-up/50',
  partial: 'bg-warn',
  missing: 'bg-down',
  pending: 'bg-warn-soft',
}

export type Tone = 'up' | 'down' | 'warn' | 'neutral' | 'info'

export function datasetTone(s: DataHealthDataset['status']): Tone {
  return s === 'fresh' ? 'up' : s === 'partial' ? 'info' : s === 'stale' ? 'warn' : s === 'missing' || s === 'error' ? 'down' : 'neutral'
}

/**
 * Alert hygiene 2026-10-30 (v1.43): a dataset needs attention only when the one freshness verdict says so
 * (`alerting`: failed, due / source unavailable past the SLA, or stale). Older APIs: status stale / missing.
 */
export function datasetAlerting(d: Pick<DataHealthDataset, 'status' | 'alerting'>): boolean {
  if (typeof d.alerting === 'boolean') return d.alerting
  return d.status === 'stale' || d.status === 'missing'
}

export const DATASET_REASON_LABEL: Record<NonNullable<DataHealthDataset['reason']>, string> = {
  fresh: 'Up to date',
  due: 'Update due',
  not_yet_published: 'Not published yet',
  source_unavailable: 'Source unavailable',
  no_source: 'No source',
  needs_key: 'Needs API key',
  failed: 'Update failed',
  stale: 'Stale beyond SLA',
}

/** Tone of the dataset reason: green up to date, grey by design, blue waiting, amber / red when it alerts. */
export function datasetReasonTone(d: Pick<DataHealthDataset, 'reason' | 'alerting'>): Tone {
  if (d.alerting) return d.reason === 'failed' ? 'down' : 'warn'
  switch (d.reason) {
    case 'fresh':
      return 'up'
    case 'not_yet_published':
    case 'due':
    case 'source_unavailable':
      return 'info'
    default:
      return 'neutral'
  }
}

/** "Not published yet · next 2026-11-11 · retry 08:36" / "Needs API key · KRX_APIKEY" */
export function datasetReasonText(d: Pick<DataHealthDataset, 'reason' | 'next_expected' | 'retry_at' | 'keys' | 'attempts'>): string {
  if (!d.reason) return ''
  const bits = [DATASET_REASON_LABEL[d.reason] ?? d.reason]
  if (d.next_expected) bits.push(`next ${d.next_expected.slice(0, 10)}`)
  if (d.retry_at && (d.reason === 'not_yet_published' || d.reason === 'source_unavailable')) bits.push(`retry ${d.retry_at.slice(5, 16).replace('T', ' ')}`)
  if (d.keys && d.keys.length) bits.push(d.keys.join(', '))
  if (d.reason === 'failed' && d.attempts) bits.push(`${d.attempts} attempt${d.attempts === 1 ? '' : 's'}`)
  return bits.join(' · ')
}

/** "2.4 h ago" / "35 min ago" / "3.1 d ago" for an age in hours. */
export function ageText(h: number | null | undefined): string {
  if (h === null || h === undefined || !Number.isFinite(h)) return ''
  if (h < 1) return `${Math.max(0, Math.round(h * 60))} min ago`
  if (h < 48) return `${h.toFixed(1)} h ago`
  return `${(h / 24).toFixed(1)} d ago`
}

/** "<time> · sync" / "<time> · weekly run" for the most recent refresh (sync or scheduled DAG run); `fmt` formats
 * the ISO time (default: as-is). */
export function lastRefreshText(
  lr: { at?: string | null; dag?: string | null } | null | undefined,
  fmt: (iso: string) => string = (x) => x,
): string {
  if (!lr || !lr.at) return ''
  return `${fmt(lr.at)} · ${lastRefreshSource(lr)}`
}

/** "startup / interval sync" or "weekly run". */
export function lastRefreshSource(lr: { dag?: string | null } | null | undefined): string {
  if (!lr) return ''
  return lr.dag === 'sync' ? 'startup / interval sync' : `${lr.dag ?? 'refresh'} run`
}

export function gapStatusTone(s: DataGap['status']): Tone {
  switch (s) {
    case 'filled':
      return 'up'
    case 'filling':
    case 'queued':
      return 'info'
    case 'failed':
      return 'warn'
    case 'unfillable':
      return 'down'
    default:
      return 'neutral'
  }
}

export function alertTone(s: OpsAlert['severity']): Tone {
  return s === 'error' ? 'down' : s === 'warn' ? 'warn' : 'info'
}

/** "63% · ETA 4.2 d (Oct 24)" */
export function campaignProgressText(c: DataHealthCampaign): string {
  const pct = typeof c.progress_pct === 'number' ? `${c.progress_pct.toFixed(c.progress_pct >= 99.95 ? 0 : 1)}%` : '—'
  if (c.status === 'done') return `${pct} · done`
  const eta = typeof c.eta_h === 'number' && c.eta_h > 0 ? ` · ETA ${c.eta_h >= 48 ? `${(c.eta_h / 24).toFixed(1)} d` : `${c.eta_h.toFixed(1)} h`}` : ''
  const at = c.eta_at ? ` (${c.eta_at.slice(5, 10)})` : ''
  return `${pct}${eta}${eta ? at : ''}`
}

/** "2026-10-12" or "2026-10-12 → 2026-10-16" */
export function gapPeriod(g: Pick<DataGap, 'period_start' | 'period_end'>): string {
  const a = g.period_start ?? null
  const b = g.period_end ?? null
  if (!a && !b) return '—'
  if (!a || !b || a === b) return (a ?? b) as string
  return `${a} → ${b}`
}

export const GAP_CLASS_LABEL: Record<DataGap['gap_class'], string> = {
  missing: 'Missing',
  stale: 'Stale',
  partial: 'Partial',
  suspicious: 'Suspicious',
  unfillable: 'Unfillable',
}
