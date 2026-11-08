import { describe, expect, it } from 'vitest'
import type { CSystemStatus } from '../../contract'
import {
  ageText,
  campaignProgressText,
  cellTone,
  datasetAlerting,
  datasetReasonText,
  datasetReasonTone,
  gapPeriod,
  headerFreshness,
  isOptionalTable,
  lastRefreshSource,
  lastRefreshText,
  sortedRegions,
  statusCacheAlert,
  statusCacheView,
} from '../adapters'
import type { DataHealthCampaign } from '../types'

const base: CSystemStatus = {
  version: '0.1',
  serving_run_id: 'SV1',
  serving_built_at: '2026-10-19T21:39:00Z',
  serving_as_of: '2026-10-13',
  data_freshness: [
    { table: 'prices_daily', status: 'stale', region: 'US' },
    { table: 'live_macro', status: 'missing' },
  ],
  last_dag_runs: [],
  keys_present: {},
}

const sched = (enabled: boolean, off_hours: number | null = null) => ({
  enabled,
  worker_alive: true,
  profile: 'weekly',
  next_runs: {},
  last_success: {},
  off_hours,
})

describe('headerFreshness (F01 / F03 / F13)', () => {
  it('shows the data as-of date, not the snapshot rebuild time', () => {
    const h = headerFreshness({ ...base, data_as_of: { US: '2026-10-12', EU: '2026-10-13' }, data_as_of_overall: '2026-10-13', ops_alerts: [], scheduler: sched(true) })
    expect(h.asOf).toBe('2026-10-13')
    expect(h.builtAt).toBe('2026-10-19T21:39:00Z')
    expect(h.regions.US).toBe('2026-10-12')
  })
  it('ignores the optional live_macro table (no permanent "Error")', () => {
    const h = headerFreshness({ ...base, data_freshness: [{ table: 'prices_daily', status: 'fresh' }, { table: 'live_macro', status: 'missing' }], ops_alerts: [], scheduler: sched(true) })
    expect(h.status).toBe('fresh')
    expect(h.chip).toBe('')
    expect(isOptionalTable('live_macro')).toBe(true)
    expect(isOptionalTable('prices_daily', true)).toBe(true)
  })
  it('raises a chip when the scheduler is off (red after a day)', () => {
    const h = headerFreshness({ ...base, ops_alerts: [{ id: 'scheduler_off', severity: 'error', title: 'Scheduler off' }], scheduler: sched(false, 30) })
    expect(h.chip).toBe('Scheduler off')
    expect(h.chipTone).toBe('down')
    expect(h.schedulerOn).toBe(false)
    expect(headerFreshness({ ...base, ops_alerts: [], scheduler: sched(false, 2) }).chipTone).toBe('warn')
  })
  it('marks data stale beyond the SLA', () => {
    const h = headerFreshness({ ...base, ops_alerts: [{ id: 'sla_critical', severity: 'error', title: 'Core data stale' }], scheduler: sched(true) })
    expect(h.status).toBe('stale')
    expect(h.chip).toBe('Data stale')
  })
  it('a missing critical table is an error', () => {
    expect(headerFreshness({ ...base, data_freshness: [{ table: 'prices_daily', status: 'missing' }] }).status).toBe('error')
  })
  it('follows the per-section serving status (F13)', () => {
    const fresh = { ...base, data_freshness: [{ table: 'prices_daily', status: 'fresh' as const }], ops_alerts: [], scheduler: sched(true) }
    const sec = (section: string, group: 'market' | 'analytics', status: 'fresh' | 'stale' | 'failed' | 'missing') => ({ section, group, status, data_as_of: '2026-10-13' })
    const ok = headerFreshness({ ...fresh, serving_sections: [sec('prices:US', 'market', 'fresh'), sec('scores', 'analytics', 'fresh')] })
    expect(ok.status).toBe('fresh')
    expect(ok.chip).toBe('')
    const an = headerFreshness({ ...fresh, serving_sections: [sec('prices:US', 'market', 'fresh'), sec('scores', 'analytics', 'stale')] })
    expect(an.status).toBe('fresh')
    expect(an.chip).toBe('Analytics stale')
    expect(an.staleSources).toEqual(['scores (stale)'])
    const mk = headerFreshness({ ...fresh, serving_sections: [sec('prices:US', 'market', 'fresh'), sec('macro', 'market', 'stale')] })
    expect(mk.status).toBe('stale')
    expect(mk.chip).toBe('Data stale')
    const px = headerFreshness({ ...fresh, serving_sections: [sec('prices:KR', 'market', 'failed')] })
    expect(px.status).toBe('error')
    expect(px.chip).toBe('Prices failed')
    expect(px.chipTone).toBe('down')
  })
  it('falls back to table freshness on pre-1.28 backends', () => {
    const h = headerFreshness(base)
    expect(h.status).toBe('stale')
    expect(h.staleSources).toEqual(['prices_daily'])
    expect(h.asOf).toBe('2026-10-13')
    expect(h.schedulerOn).toBeNull()
  })
})

describe('data health helpers', () => {
  it('maps coverage ratios to heatmap tones', () => {
    expect(cellTone(null)).toBe('none')
    expect(cellTone(1)).toBe('full')
    expect(cellTone(0.9)).toBe('ok')
    expect(cellTone(0.3)).toBe('partial')
    expect(cellTone(0.01)).toBe('missing')
    expect(cellTone(0, true)).toBe('pending')
    expect(cellTone(1, true)).toBe('full')
  })
  it('formats campaign progress with an ETA', () => {
    const c: DataHealthCampaign = { id: 'gdelt_e6', label: 'GDELT', status: 'running', progress_pct: 30.16, eta_h: 82, eta_at: '2026-10-23T10:00' }
    expect(campaignProgressText(c)).toBe('30.2% · ETA 3.4 d (10-23)')
    expect(campaignProgressText({ ...c, status: 'done', progress_pct: 100 })).toBe('100% · done')
    expect(campaignProgressText({ ...c, eta_h: 5.5 })).toBe('30.2% · ETA 5.5 h (10-23)')
  })
  it('formats gap periods and orders regions', () => {
    expect(gapPeriod({ period_start: '2026-10-12', period_end: '2026-10-12' })).toBe('2026-10-12')
    expect(gapPeriod({ period_start: '2026-10-12', period_end: '2026-10-16' })).toBe('2026-10-12 → 2026-10-16')
    expect(gapPeriod({ period_start: null, period_end: null })).toBe('—')
    expect(sortedRegions({ KR: 'a', US: 'b', ZZ: 'c', EU: 'd' }).map((x) => x[0])).toEqual(['US', 'EU', 'KR', 'ZZ'])
  })
})

describe('alert hygiene 2026-10-30 (v1.43)', () => {
  it('counts a dataset as stale only when its one verdict alerts', () => {
    // FINRA: the detector said stale, the sync says not published yet -> not stale beyond SLA
    expect(datasetAlerting({ status: 'fresh', alerting: false })).toBe(false)
    expect(datasetAlerting({ status: 'stale', alerting: true })).toBe(true)
    expect(datasetAlerting({ status: 'stale' })).toBe(true) // pre-1.43 API: the status decides
    expect(datasetAlerting({ status: 'partial' })).toBe(false)
  })
  it('explains the reason of a dataset', () => {
    expect(datasetReasonText({ reason: 'not_yet_published', next_expected: '2026-11-11T23:00:00+00:00', retry_at: '2026-10-30T08:36:02+00:00' })).toBe(
      'Not published yet · next 2026-11-11 · retry 10-30 08:36',
    )
    expect(datasetReasonText({ reason: 'needs_key', keys: ['KRX_APIKEY'] })).toBe('Needs API key · KRX_APIKEY')
    expect(datasetReasonText({ reason: 'failed', attempts: 3 })).toBe('Update failed · 3 attempts')
    expect(datasetReasonText({ reason: null })).toBe('')
    expect(datasetReasonTone({ reason: 'not_yet_published', alerting: false })).toBe('info')
    expect(datasetReasonTone({ reason: 'due', alerting: true })).toBe('warn')
    expect(datasetReasonTone({ reason: 'failed', alerting: true })).toBe('down')
    expect(datasetReasonTone({ reason: 'no_source', alerting: false })).toBe('neutral')
  })
  it('shows the age of the last QA run and the last refresh source', () => {
    expect(ageText(0.4)).toBe('24 min ago')
    expect(ageText(5.25)).toBe('5.3 h ago')
    expect(ageText(150)).toBe('6.3 d ago')
    expect(ageText(null)).toBe('')
    expect(lastRefreshSource({ dag: 'sync' })).toBe('startup / interval sync')
    expect(lastRefreshSource({ dag: 'weekly' })).toBe('weekly run')
    expect(lastRefreshSource(null)).toBe('')
    expect(lastRefreshText({ at: '2026-10-30T07:32:54+00:00', dag: 'sync' }, (x) => x.slice(11, 16))).toBe('07:32 · startup / interval sync')
  })
})

describe('status cache (coverage v3 A7 review)', () => {
  const ok: CSystemStatus = { ...base, data_freshness: [{ table: 'prices_daily', status: 'fresh' }], ops_alerts: [], scheduler: sched(true) }
  const cache = (state: 'fresh' | 'stale' | 'partial' | 'warming' | 'error', section_errors: string[] = [], section_as_of: Record<string, string> = {}) => ({
    state,
    ttl_s: 10,
    computed_at: '2026-11-07T10:00:00+00:00',
    section_errors,
    section_as_of,
  })
  it('a fresh payload changes nothing', () => {
    const h = headerFreshness({ ...ok, cache: cache('fresh') })
    expect(h.status).toBe('fresh')
    expect(h.chip).toBe('')
    expect(h.statusCache?.freshnessUnknown).toBe(false)
  })
  it('reused freshness rows are never "fresh": partial chip, stale badge', () => {
    const st = { ...ok, cache: cache('partial', ['data_freshness: ValueError: jobs.yaml invalid'], { data_freshness: '2026-11-07T09:55:00+00:00' }) }
    const h = headerFreshness(st)
    expect(h.status).toBe('stale')
    expect(h.chip).toBe('Status partially stale')
    expect(h.chipTone).toBe('warn')
    expect(h.statusCache?.failedSections).toEqual(['data_freshness'])
    const a = statusCacheAlert(statusCacheView(st))
    expect(a?.id).toBe('status_partial')
    expect(a?.detail).toContain('data_freshness (from 2026-11-07T09:55:00+00:00)')
  })
  it('a failed ops block (scheduler / alerts) is not "all clear"', () => {
    const h = headerFreshness({ ...ok, cache: cache('partial', ['ops_status: ValueError: jobs.yaml invalid']) })
    expect(h.status).toBe('stale')
    expect(h.chip).toBe('Status partially stale')
    expect(statusCacheAlert(h.statusCache)?.detail).toContain('ops_status (empty)')
  })
  it('an older backend reporting fresh with section errors still reads partial', () => {
    expect(statusCacheView({ ...ok, cache: cache('fresh', ['last_dag_runs: RuntimeError: x']) })?.state).toBe('partial')
    expect(headerFreshness({ ...ok, cache: cache('fresh', ['last_dag_runs: RuntimeError: x']) }).status).toBe('fresh') // jobs only: data still current
  })
  it('warming / error payloads', () => {
    expect(headerFreshness({ ...ok, cache: cache('warming') }).chip).toBe('Status loading')
    const e = headerFreshness({ ...ok, cache: cache('error') })
    expect(e.chip).toBe('Status unavailable')
    expect(e.chipTone).toBe('down')
    expect(e.status).toBe('stale')
    expect(statusCacheAlert(statusCacheView({ ...ok, cache: cache('fresh') }))).toBeNull()
    expect(statusCacheView(ok)).toBeNull() // older backend without the cache block
  })
  it('stronger chips win over the status chip', () => {
    const h = headerFreshness({ ...ok, ops_alerts: [{ id: 'worker_down', severity: 'error', title: 'Worker not running' }], cache: cache('partial', ['data_freshness: x']) })
    expect(h.chip).toBe('Worker off')
  })
})
