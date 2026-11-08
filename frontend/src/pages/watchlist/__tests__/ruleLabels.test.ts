import { describe, expect, it } from 'vitest'
import { alertFromContract, alertToContract } from '@/api/adapters/crossSection'
import type { CAlert } from '@/api/contract'
import { conditionText, evaluateValue, targetText } from '../ruleLabels'

const base: Omit<CAlert, 'rule_type' | 'params'> = { alert_id: 'a9', security_id: 'SABCDEF1234', enabled: true, cooldown_days: 5, created_at: '2026-10-20T00:00:00Z' }

describe('watchlist alert payloads (guide 13: 1D change % and Composite score were rejected with HTTP 400)', () => {
  it('1D change % posts a price_change_pct_1d rule with pct (fraction) and direction up | down | any', () => {
    const up = alertToContract({ companyId: 'SABCDEF1234', ticker: 'MSFT', metric: 'change1dPct', operator: '>', threshold: 5, enabled: true })
    expect(up).toMatchObject({ rule_type: 'price_change_pct_1d', params: { pct: 0.05, direction: 'up' } })
    expect(up.params).not.toHaveProperty('pct_change_1d')
    const dn = alertToContract({ companyId: 'SABCDEF1234', ticker: 'MSFT', metric: 'change1dPct', operator: '<', threshold: -3, enabled: true })
    expect(dn.params).toMatchObject({ pct: 0.03, direction: 'down' })
    const any = alertToContract({ companyId: 'SABCDEF1234', ticker: 'MSFT', metric: 'change1dPct', operator: 'crosses', threshold: 4, enabled: true })
    expect(any.params).toMatchObject({ direction: 'any' })
  })

  it('Composite score posts a score_change level rule (mode level, direction up | down)', () => {
    const b = alertToContract({ companyId: 'SABCDEF1234', ticker: 'MSFT', metric: 'compositeScore', operator: '>', threshold: 70, enabled: true })
    expect(b).toMatchObject({ rule_type: 'score_change', params: { profile_id: 'balanced', mode: 'level', level: 70, direction: 'up' } })
  })
})

describe('rule table labels (guide 13: API-created rules showed raw ids and "above NaN")', () => {
  it('rebuilds the condition and target from server params without ui_* keys', () => {
    const r = alertFromContract({ ...base, rule_type: 'price_change_pct_1d', params: { pct: 0.05, direction: 'down' }, ticker: 'AAPL', security_name: 'Apple Inc.' })
    expect(r).toMatchObject({ metric: 'change1dPct', operator: '<', threshold: 5, ticker: 'AAPL', name: 'Apple Inc.' })
    expect(conditionText(r, '1D change %')).toBe('1D change ≤ −5%')
    expect(targetText(r)).toBe('AAPL')

    const s = alertFromContract({ ...base, rule_type: 'score_change', params: { profile_id: 'balanced', mode: 'delta', delta: 8, direction: 'any' } })
    expect(conditionText(s, 'Composite score')).toBe('Composite score moves by ≥ 8 pts')
    expect(conditionText(s, 'Composite score')).not.toContain('NaN')

    const l = alertFromContract({ ...base, rule_type: 'score_change', params: { mode: 'level', level: 70, direction: 'up' } })
    expect(conditionText(l, 'Composite score')).toBe('Composite score crosses above 70')

    const si = alertFromContract({ ...base, rule_type: 'short_interest_spike', params: { min_chg_pct: 0.25 } })
    expect(conditionText(si, 'Short interest rises ≥ (FINRA)')).toBe('Short interest rises ≥ (FINRA) above 25')
  })

  it('never shows a bare security id when a ticker or name is known; watchlist rules show the watchlist', () => {
    const raw = alertFromContract({ ...base, rule_type: 'price_cross', params: { level: 10, direction: 'above' } })
    expect(raw.ticker).toBe('SABCDEF1234')
    expect(targetText(raw, (id) => (id === 'SABCDEF1234' ? 'NVDA' : undefined))).toBe('NVDA')
    const named = { ...raw, name: 'Nvidia Corp' }
    expect(targetText(named)).toBe('Nvidia Corp')
    const wl = alertFromContract({ ...base, security_id: null, watchlist_id: 'W1', watchlist_name: 'Core longs', rule_type: 'new_filing', params: {} })
    expect(targetText(wl)).toBe('Watchlist: Core longs')
    expect(conditionText(wl, 'Touches rank-N zone')).toBe('New filing: annual, quarterly, current report')
    const md = alertFromContract({ ...base, rule_type: 'model_decile', params: { horizon: '3M', min_decile: 10 } })
    expect(conditionText(md, 'Touches rank-N zone')).toBe('ML 3M prediction enters decile ≥ 10')
  })

  it('evaluates 1D change in both directions', () => {
    const r = { id: 'x', companyId: 'S1', ticker: 'X', metric: 'change1dPct' as const, operator: '<' as const, threshold: 5, enabled: true, createdAt: '' }
    expect(evaluateValue(r, -6)).toBe('triggered')
    expect(evaluateValue(r, 6)).toBe('ok')
    expect(evaluateValue({ ...r, operator: 'crosses' }, 6)).toBe('triggered')
    expect(evaluateValue({ ...r, threshold: Number.NaN }, 6)).toBe('pending')
  })
})
