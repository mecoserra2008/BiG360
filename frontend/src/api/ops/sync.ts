/**
 * Startup sync view helpers (contract v1.41.0, GET /system/sync): reason labels / tones, dataset ordering and the
 * "next expected" wording. The header chip text itself comes from the backend (`chip`), so the tray, the API and the
 * UI say the same thing.
 */
import type { SyncDataset, SyncEntity, SyncJob, SyncReason, SyncStatus } from './types'

export type Tone = 'neutral' | 'up' | 'down' | 'warn' | 'info' | 'accent'

export const REASON_LABEL: Record<SyncReason, string> = {
  fresh: 'Up to date',
  due: 'Fetching',
  not_yet_published: 'Not published yet',
  source_unavailable: 'Source unavailable',
  no_source: 'No source',
  needs_key: 'Needs API key',
  failed: 'Failed',
}

export const REASON_HELP: Record<SyncReason, string> = {
  fresh: 'Up to date with the latest published period',
  due: 'The source has newer data: the sync fetches it',
  not_yet_published: 'The session, release or filing deadline has not happened yet, or the source has not published it: retried automatically',
  source_unavailable: 'The source is down or rate-limited: retried with back-off',
  no_source: 'No free or official source exists: skipped permanently (documented)',
  needs_key: 'An API key is missing: add it to unlock this data',
  failed: 'The job errored: retried with back-off, parked after 3 failures',
}

export function reasonTone(r: SyncReason | string | null | undefined): Tone {
  switch (r) {
    case 'fresh':
      return 'up'
    case 'due':
      return 'info'
    case 'not_yet_published':
      return 'neutral'
    case 'source_unavailable':
    case 'needs_key':
      return 'warn'
    case 'failed':
      return 'down'
    default:
      return 'neutral'
  }
}

/** Most actionable first: failed, due, unavailable, key, not yet published, no source, fresh. */
const RANK: Record<string, number> = { failed: 0, due: 1, source_unavailable: 2, needs_key: 3, not_yet_published: 4, no_source: 5, fresh: 6 }

export function sortDatasets(items: SyncDataset[]): SyncDataset[] {
  return [...items].sort((a, b) => (RANK[a.reason] ?? 9) - (RANK[b.reason] ?? 9) || (a.kind === b.kind ? 0 : a.kind === 'job' ? 1 : -1) || a.dataset.localeCompare(b.dataset))
}

export function reasonCounts(st: SyncStatus | null | undefined): [SyncReason, number][] {
  const c = (st?.counts ?? {}) as Record<string, number>
  return (Object.keys(REASON_LABEL) as SyncReason[]).filter((k) => (c[k] ?? 0) > 0).map((k) => [k, c[k] ?? 0])
}

/** The entities worth listing under a dataset row (everything that is not plain fresh). */
export function notableEntities(d: SyncDataset): SyncEntity[] {
  return (d.entities ?? []).filter((e) => e.reason !== 'fresh' || (e.detail ?? '').includes('market open'))
}

export function jobDuration(j: Pick<SyncJob, 'duration_s'>): string {
  const s = j.duration_s
  if (s === null || s === undefined) return '—'
  if (s < 60) return `${Math.round(s)} s`
  if (s < 3600) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`
}

/** "in 2 h 5 min" / "now" for a future ISO time (null when unknown). */
export function untilText(iso: string | null | undefined, now: Date = new Date()): string | null {
  if (!iso) return null
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return null
  const m = Math.round((t - now.getTime()) / 60_000)
  if (m <= 0) return 'now'
  if (m < 60) return `in ${m} min`
  const h = Math.floor(m / 60)
  if (h < 48) return `in ${h} h ${m % 60} min`
  return `in ${Math.round(h / 24)} d`
}

export const isSyncRunning = (st: SyncStatus | null | undefined): boolean => Boolean(st?.chip?.running)
