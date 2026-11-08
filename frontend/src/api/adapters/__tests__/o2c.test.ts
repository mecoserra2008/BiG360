import { describe, expect, it } from 'vitest'
import { qaCounts, qaTone } from '@/pages/system/qa'
import { adaptMover, adaptMoversFilter, moversFilterText } from '../market'

describe('v1.30 movers screen (F21)', () => {
  it('maps movers_filter and ignores older payloads', () => {
    expect(adaptMoversFilter(undefined)).toBeNull()
    const f = adaptMoversFilter({ liquid: true, min_adv_usd: 5e6, min_mcap_usd: 1e9, min_price_usd: 2, unknown_mcap_min_adv_usd: 2e7, unknown_adv_passes: false, limit: 25 })
    expect(f).toEqual({ liquid: true, minAdvUsd: 5e6, minMcapUsd: 1e9, minPriceUsd: 2, unknownMcapMinAdvUsd: 2e7, limit: 25 })
    expect(moversFilterText(f!)).toMatch(/^ADV ≥ .*5.*price ≥ \$2.*cap ≥ .*unknown cap: ADV ≥ /)
  })
  it('carries the ADV of a mover', () => {
    const m = adaptMover({ security: { security_id: 'S1', issuer_id: 'I1', ticker: 'X', name: 'X', region: 'US', country: 'US', currency: 'USD', market_cap_usd: null }, close: 10, chg_1d: 0.1, adv_usd_20d: 3e7 })
    expect(m.advUsd20d).toBe(3e7)
    expect(m.marketCapUsd).toBeNull()
  })
})

describe('regression guard panel (O2.3)', () => {
  it('formats suite counts in a stable order and maps tones', () => {
    expect(qaCounts({ skipped: 11, passed: 137, warn: 2 })).toBe('137 passed · 2 warn · 11 skipped')
    expect(qaCounts(null)).toBe('')
    expect(qaTone('failed')).toBe('down')
    expect(qaTone('passed')).toBe('up')
    expect(qaTone('pending')).toBe('neutral')
  })
})
