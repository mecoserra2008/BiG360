import type { SourceFreshness } from '@/api/types'

/** One row per dataset (source table) instead of one per region combination (audit F22). Descriptions carry dates ("Data through …"), so the key is the source. */
export interface SourceGroup extends SourceFreshness {
  key: string
  regions: string[]
  nRows: number
  /** freshest / stalest region (by lag) for the tooltip */
  best: { region: string; lagHours: number } | null
  worst: { region: string; lagHours: number } | null
  counts: { fresh: number; stale: number; error: number }
  critical: number
}

const STATUS_RANK = { fresh: 0, stale: 1, error: 2 } as const
const DQ_RANK: Record<string, number> = { ok: 0, info: 1, no_data: 2, warn: 3, error: 4 }

/** Lower = more critical for a portfolio manager's views (prices, FX, macro first). */
export function criticality(s: Pick<SourceFreshness, 'source' | 'description'>): number {
  const t = `${s.source} ${s.description}`.toLowerCase()
  if (/price|bars|quote/.test(t)) return 0
  if (/\bfx\b|fx_|currenc/.test(t)) return 1
  if (/macro|fred|ecb|eurostat|yield|curve/.test(t)) return 2
  if (/fundamental|filing|xbrl|edgar/.test(t)) return 3
  if (/live|realtime|stream/.test(t)) return 9
  return 5
}

export function groupSources(rows: SourceFreshness[]): SourceGroup[] {
  const by = new Map<string, SourceFreshness[]>()
  for (const r of rows) {
    const k = r.source
    by.set(k, [...(by.get(k) ?? []), r])
  }
  const out: SourceGroup[] = []
  for (const [key, list] of by) {
    const worstRow = [...list].sort((a, b) => STATUS_RANK[b.status] - STATUS_RANK[a.status] || b.lagHours - a.lagHours)[0]
    const byLag = [...list].filter((r) => Number.isFinite(r.lagHours)).sort((a, b) => a.lagHours - b.lagHours)
    const dq = list.map((r) => r.dqStatus).filter((x): x is NonNullable<SourceFreshness['dqStatus']> => !!x).sort((a, b) => (DQ_RANK[b] ?? 0) - (DQ_RANK[a] ?? 0))[0] ?? null
    const lastSuccess = list.map((r) => r.lastSuccess).filter(Boolean).sort().reverse()[0] ?? worstRow.lastSuccess
    out.push({
      ...worstRow,
      key,
      lastSuccess,
      dqStatus: dq,
      coverage: list.reduce((a, r) => a + (Number.isFinite(r.coverage) ? r.coverage : 0), 0),
      regions: [...new Set(list.map((r) => r.region))],
      nRows: list.length,
      best: byLag.length ? { region: byLag[0].region, lagHours: byLag[0].lagHours } : null,
      worst: byLag.length ? { region: byLag[byLag.length - 1].region, lagHours: byLag[byLag.length - 1].lagHours } : null,
      counts: { fresh: list.filter((r) => r.status === 'fresh').length, stale: list.filter((r) => r.status === 'stale').length, error: list.filter((r) => r.status === 'error').length },
      critical: criticality(worstRow),
    })
  }
  return out.sort((a, b) => a.critical - b.critical || STATUS_RANK[b.status] - STATUS_RANK[a.status] || a.source.localeCompare(b.source))
}
