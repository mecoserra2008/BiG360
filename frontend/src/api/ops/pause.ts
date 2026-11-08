/**
 * Coverage v3 A2 / A4 view helpers: the gap filler's pause flag (expiry, reason, owner), the startup sync's caps and
 * deferred work, and admission waits (RAM floor / heavy slot). Payload fields are additive in the contract
 * (PauseFlag, SyncDeferred, AdmissionWait), so older backends simply omit them.
 */
import type { Tone } from './sync'

export interface PauseFlag {
  name: string
  path?: string
  present: boolean
  active: boolean
  expired: boolean
  legacy?: boolean
  reason?: string | null
  by?: string | null
  since?: string | null
  until?: string | null
  expired_at?: string | null
  remaining_h?: number | null
  detail?: string | null
}

export interface SyncDeferred {
  job: string
  pass_no?: number | null
  lane?: string | null
  trigger?: string | null
  reason: string
  datasets?: string[]
  deferred_at?: string | null
}

export interface SyncCaps {
  enabled?: boolean
  max_wall_min?: number
  pass_max_jobs?: number
  deadline?: string | null
  remaining_min?: number | null
  jobs_started?: Record<string, number>
  hit?: { at?: string; reason?: string } | null
}

export interface AdmissionWait {
  job_id: string
  pid?: number | null
  kind?: string | null
  reason?: string | null
  since?: string | null
}

const hm = (iso: string | null | undefined): string => (iso ? iso.slice(0, 16).replace('T', ' ') + ' UTC' : '—')

/** One line for a pause flag: who paused it, why, until when - or that it expired and is ignored. */
export function pauseText(p: PauseFlag | null | undefined): { text: string; tone: Tone } | null {
  if (!p || !p.present) return null
  if (p.active) {
    const who = p.by ? ` by ${p.by}` : ''
    const why = p.reason ? `: ${p.reason}` : ''
    const legacy = p.legacy ? ' (legacy flag: expires 7 days after it was written)' : ''
    return { text: `Paused${who}${why} · until ${hm(p.until)}${legacy}`, tone: 'warn' }
  }
  if (p.expired) {
    return { text: `Pause flag expired ${hm(p.expired_at)} and is ignored${p.reason ? ` (${p.reason})` : ''}: remove ${p.path ?? 'filler.PAUSE'}`, tone: 'neutral' }
  }
  return null
}

/** "Budget 120 min · 45 jobs/pass · hit: ..." for the sync panel (null when caps are off / not reported). */
export function capsText(c: SyncCaps | null | undefined): string | null {
  if (!c || c.enabled === false || c.max_wall_min == null) return c && c.enabled === false ? 'caps off (BIG360_SYNC_CAPS=0)' : null
  const base = `budget ${c.max_wall_min} min · ${c.pass_max_jobs ?? '—'} jobs per pass`
  return c.hit?.reason ? `${base} · reached: ${c.hit.reason}` : base
}

/** Deferred jobs grouped by reason (most frequent first), for a compact list. */
export function groupDeferred(items: SyncDeferred[] | null | undefined): { reason: string; jobs: string[] }[] {
  const by = new Map<string, string[]>()
  for (const d of items ?? []) {
    const k = d.reason.replace(/\s+/g, ' ').trim()
    by.set(k, [...(by.get(k) ?? []), d.job])
  }
  return [...by.entries()].map(([reason, jobs]) => ({ reason, jobs })).sort((a, b) => b.jobs.length - a.jobs.length || a.reason.localeCompare(b.reason))
}

export const WAIT_KIND_LABEL: Record<string, string> = {
  ram: 'RAM headroom',
  floor: 'RAM floor',
  starve: 'queued',
  slot: 'heavy slot',
}
