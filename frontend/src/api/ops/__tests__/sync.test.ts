import { describe, expect, it } from 'vitest'
import { jobDuration, notableEntities, reasonCounts, reasonTone, sortDatasets, untilText } from '../sync'
import type { SyncDataset, SyncStatus } from '../types'

const ds = (dataset: string, reason: SyncDataset['reason'], kind: 'coverage' | 'job' = 'coverage'): SyncDataset => ({ dataset, reason, kind, entities: [], runs: [] })

describe('startup sync helpers (v1.41 /system/sync)', () => {
  it('orders datasets by how actionable their reason is, coverage before source jobs', () => {
    const out = sortDatasets([ds('a', 'fresh'), ds('job:x', 'failed', 'job'), ds('b', 'needs_key'), ds('c', 'failed'), ds('d', 'due')])
    expect(out.map((d) => d.dataset)).toEqual(['c', 'job:x', 'd', 'b', 'a'])
  })

  it('maps every reason to a tone (failed down, key / unavailable warn, fresh up)', () => {
    expect(reasonTone('failed')).toBe('down')
    expect(reasonTone('needs_key')).toBe('warn')
    expect(reasonTone('source_unavailable')).toBe('warn')
    expect(reasonTone('fresh')).toBe('up')
    expect(reasonTone('not_yet_published')).toBe('neutral')
  })

  it('counts reasons in a stable order and formats durations / next times', () => {
    const st = { counts: { fresh: 40, needs_key: 2, failed: 1 } } as unknown as SyncStatus
    expect(reasonCounts(st)).toEqual([['fresh', 40], ['needs_key', 2], ['failed', 1]])
    expect(jobDuration({ duration_s: 42 })).toBe('42 s')
    expect(jobDuration({ duration_s: 3725 })).toBe('1 h 2 min')
    const now = new Date('2026-10-27T20:00:00Z')
    expect(untilText('2026-10-27T21:30:00Z', now)).toBe('in 1 h 30 min')
    expect(untilText('2026-10-27T19:00:00Z', now)).toBe('now')
    expect(untilText(null, now)).toBeNull()
  })

  it('lists only the entities that need a word (not plain fresh ones)', () => {
    const d: SyncDataset = { ...ds('fx.rates', 'fresh'), entities: [{ entity: 'fixings', reason: 'fresh' }, { entity: 'TWD', reason: 'no_source', secondary: true }] }
    expect(notableEntities(d).map((e) => e.entity)).toEqual(['TWD'])
  })
})
