import { describe, expect, it } from 'vitest'
import { capsText, groupDeferred, pauseText } from '../pause'

describe('coverage v3 A2 / A4 ops helpers', () => {
  it('describes an active pause with owner, reason and expiry', () => {
    const t = pauseText({ name: 'filler', present: true, active: true, expired: false, by: 'tray', reason: 'maintenance', until: '2026-11-07T18:00:00+00:00' })
    expect(t?.tone).toBe('warn')
    expect(t?.text).toBe('Paused by tray: maintenance · until 2026-11-07 18:00 UTC')
  })

  it('flags a legacy pause and an expired one (ignored)', () => {
    expect(pauseText({ name: 'filler', present: true, active: true, expired: false, legacy: true, until: '2026-11-08T04:45:00+00:00' })?.text).toContain('legacy flag')
    const e = pauseText({ name: 'filler', present: true, active: false, expired: true, reason: 'API restart loop', expired_at: '2026-10-31T04:45:00+00:00', path: 'data/state/ops/filler.PAUSE' })
    expect(e?.tone).toBe('neutral')
    expect(e?.text).toBe('Pause flag expired 2026-10-31 04:45 UTC and is ignored (API restart loop): remove data/state/ops/filler.PAUSE')
    expect(pauseText(null)).toBeNull()
    expect(pauseText({ name: 'filler', present: false, active: false, expired: false })).toBeNull()
  })

  it('summarises the sync budget and groups deferred work by reason', () => {
    expect(capsText({ enabled: true, max_wall_min: 120, pass_max_jobs: 45 })).toBe('budget 120 min · 45 jobs per pass')
    expect(capsText({ enabled: true, max_wall_min: 120, pass_max_jobs: 45, hit: { reason: 'sync budget of 120 min used up' } })).toContain('reached: sync budget')
    expect(capsText({ enabled: false })).toBe('caps off (BIG360_SYNC_CAPS=0)')
    expect(capsText(null)).toBeNull()
    const g = groupDeferred([
      { job: 'a', reason: 'sync budget of 120 min used up' },
      { job: 'b', reason: 'RAM floor: ...' },
      { job: 'c', reason: 'sync budget of 120 min used up' },
    ])
    expect(g).toEqual([{ reason: 'sync budget of 120 min used up', jobs: ['a', 'c'] }, { reason: 'RAM floor: ...', jobs: ['b'] }])
  })
})
