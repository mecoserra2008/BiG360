import { describe, expect, it } from 'vitest'
import type { CFactorMeta } from '@/api/contract'
import { adaptFactorMeta } from '@/api/adapters/crossSection'
import { factorMeta } from '@/api/adapters/__tests__/fixtures'
import { compositeValidationFromContract, compositeWording, DESCRIPTIVE_VALIDATION } from '../compositeLabel'

type CV = NonNullable<CFactorMeta['composite_validation']>

const base = (balanced: Partial<CV['profiles'][string]>, top: Partial<CV> = {}): CV => ({
  label: 'descriptive',
  default_profile: 'balanced',
  signal_wording: false,
  note: 'Descriptive: a rules-based ranking.',
  evaluated_at: '2026-11-06T00:00:00+00:00',
  profiles: { balanced: { label: 'descriptive', verdict: 'not_validated', signal_wording: false, ...balanced } },
  ...top,
})

describe('composite label (coverage v3 A8: descriptive unless a recorded out-of-sample gate passed)', () => {
  it('is descriptive when the API sends nothing (older API, mock, loading)', () => {
    const v = compositeValidationFromContract(undefined)
    expect(v.label).toBe('descriptive')
    expect(v.signalWording).toBe(false)
    const w = compositeWording(undefined)
    expect(w.label).toBe('Descriptive')
    expect(w.signalAllowed).toBe(false)
    expect(w.title).toMatch(/AUC 0\.49/)
    expect(adaptFactorMeta(factorMeta).compositeValidation).toEqual(DESCRIPTIVE_VALIDATION)
  })

  it('keeps signal wording off after a failed gate and says why', () => {
    const v = compositeValidationFromContract(base({ verdict: 'fail', rank_ic_mean: -0.0028, rank_ic_t: -0.18, auc_mean: 0.502, auc_t: 0.24, n_dates: 12 }))
    const w = compositeWording(v)
    expect(w.signalAllowed).toBe(false)
    expect(w.label).toBe('Descriptive')
    expect(w.title).toContain('gate failed')
    expect(w.title).toContain('AUC 0.502 (t 0.24)')
  })

  it('unlocks "Validated signal" only for a passed profile AND API-confirmed composites, never for re-weighted ones', () => {
    const universe = { table: 'composite_scores', scopes: ['mvp' as const], regions: ['JP', 'US'], n_securities: 781 }
    const v = compositeValidationFromContract(
      base(
        { label: 'validated', verdict: 'pass', signal_wording: true, horizon: '3M', rank_ic_mean: 0.031, rank_ic_t: 2.6, auc_mean: 0.53, auc_t: 2.3, n_dates: 120, n_months: 96, universe },
        { label: 'validated', signal_wording: true, universe },
      ),
    )
    expect(v.label).toBe('validated')
    expect(v.profiles.balanced.universe).toEqual({ table: 'composite_scores', scopes: ['mvp'], regions: ['JP', 'US'], nSecurities: 781 })
    const w = compositeWording(v, { validated: true })
    expect(w.signalAllowed).toBe(true)
    expect(w.label).toBe('Validated signal')
    expect(w.title).toContain('rank IC 0.031 (t 2.60)')
    expect(w.title).toContain('over 96 months') // distinct scoring months, not raw dates
    expect(w.title).toContain('781 securities')
    expect(compositeWording(v, { custom: true, validated: true }).signalAllowed).toBe(false)
    expect(compositeWording(v, { profile: 'growth', validated: true }).signalAllowed).toBe(false) // no gate recorded for growth
    expect(compositeWording(v, { profileLevel: true }).signalAllowed).toBe(true) // System page: the gate itself
  })

  it('stays descriptive for composites outside the tested population (review A7_A8)', () => {
    const v = compositeValidationFromContract(
      base({ label: 'validated', verdict: 'pass', signal_wording: true, n_months: 96 }, { label: 'validated', signal_wording: true }),
    )
    for (const validated of [undefined, null, false]) {
      // Peers / Sector Explorer (no per-composite confirmation), a JP home listing or an extended-scope score
      const w = compositeWording(v, { validated })
      expect(w.signalAllowed).toBe(false)
      expect(w.label).toBe('Descriptive')
      expect(w.title).toContain('not confirmed inside it')
    }
  })

  it('fails closed on inconsistent payloads', () => {
    // verdict pass but label descriptive / signal_wording false -> not validated
    const v1 = compositeValidationFromContract(base({ verdict: 'pass', label: 'descriptive', signal_wording: true }))
    expect(compositeWording(v1).signalAllowed).toBe(false)
    // top-level says validated but the profile failed -> descriptive
    const v2 = compositeValidationFromContract(base({ verdict: 'fail', label: 'validated', signal_wording: true }, { label: 'validated', signal_wording: true }))
    expect(v2.label).toBe('descriptive')
    expect(v2.signalWording).toBe(false)
    expect(compositeWording(v2).signalAllowed).toBe(false)
  })
})
