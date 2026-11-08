/**
 * Display helpers for the alert-rule table (guide 13): rules created through the API carry no ui_* keys, so the
 * condition and target are rebuilt from the server rule type / params; a missing threshold never prints "NaN".
 */
import type { AlertRule } from '@/api/types'

const OP_LABEL: Record<string, string> = { '>': 'above', '<': 'below', crosses: 'crosses' }
const RAW_ID = /^S[0-9A-Z]{6,}$/

export function fmtThreshold(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(2)))
}

/** Rule types the page form cannot create (API only): described from their own parameters. */
function apiOnlyText(r: AlertRule): string | null {
  const p = r.params ?? {}
  if (p.ui_metric !== undefined || !r.ruleType) return null
  const list = (k: string) => (Array.isArray(p[k]) ? (p[k] as unknown[]).map((x) => String(x).replace(/_/g, ' ')).join(', ') : null)
  switch (r.ruleType) {
    case 'new_filing':
      return `New filing: ${list('form_classes') ?? 'annual, quarterly, current report'}`
    case 'model_decile':
      return `ML ${typeof p.horizon === 'string' ? p.horizon : '3M'} prediction enters decile ≥ ${typeof p.min_decile === 'number' ? p.min_decile : 9}`
    case 'insider_buy':
      return `Insider buy${p.cluster_only ? ' (cluster only)' : ''}${typeof p.min_value_usd === 'number' && p.min_value_usd > 0 ? ` ≥ $${fmtThreshold(p.min_value_usd)}` : ''}`
    case 'activist_13d':
      return `New 13D${p.include_switch === false ? '' : ' or 13G → 13D switch'}${p.include_amendments ? ' (incl. amendments)' : ''}`
    default:
      return null
  }
}

/** Human condition, e.g. "1D change ≥ +5%", "Composite score crosses above 70", "Price below 480". */
export function conditionText(r: AlertRule, metricLabel: string): string {
  const api = apiOnlyText(r)
  if (api) return api
  const t = r.threshold
  const p = r.params ?? {}
  if (r.metric === 'change1dPct') {
    if (!Number.isFinite(t)) return '1D change (threshold not set)'
    const m = fmtThreshold(Math.abs(t))
    return r.operator === '<' ? `1D change ≤ −${m}%` : r.operator === '>' ? `1D change ≥ +${m}%` : `|1D change| ≥ ${m}%`
  }
  if (r.metric === 'compositeScore') {
    const prof = typeof p.factor_id === 'string' ? `${p.factor_id} score` : `Composite score${typeof p.profile_id === 'string' && p.profile_id !== 'balanced' ? ` (${p.profile_id})` : ''}`
    if (p.mode === 'delta' || (r.ruleType === 'score_change' && p.mode !== 'level' && p.ui_metric === undefined)) {
      const dir = p.direction === 'up' ? ' up' : p.direction === 'down' ? ' down' : ''
      return `${prof} moves${dir} by ≥ ${fmtThreshold(Number.isFinite(t) ? Math.abs(t) : 10)} pts`
    }
    if (!Number.isFinite(t)) return `${prof} (level not set)`
    return r.operator === 'crosses' ? `${prof} crosses ${fmtThreshold(t)}` : `${prof} crosses ${OP_LABEL[r.operator]} ${fmtThreshold(t)}`
  }
  if (!Number.isFinite(t)) return `${metricLabel} (default threshold)`
  return `${metricLabel} ${OP_LABEL[r.operator] ?? r.operator} ${fmtThreshold(t)}`
}

/** Target column: ticker (resolved), watchlist name for watchlist rules; never a bare internal security id when a
 *  name is known. `lookup` = tickers from the screener rows / watchlist detail. */
export function targetText(r: AlertRule, lookup?: (id: string) => string | undefined): string {
  if (!r.companyId && r.watchlistId) return `Watchlist: ${r.watchlistName ?? r.watchlistId}`
  const known = r.companyId ? lookup?.(r.companyId) : undefined
  if (known) return known
  if (r.ticker && !RAW_ID.test(r.ticker)) return r.ticker
  return r.name ?? r.ticker ?? r.companyId ?? '—'
}

/** In-app evaluation of the snapshot-based rules (value in UI units: 1D change in %). */
export function evaluateValue(r: AlertRule, v: number | null): 'triggered' | 'ok' | 'pending' {
  if (v === null || !Number.isFinite(v) || !Number.isFinite(r.threshold)) return 'pending'
  if (r.metric === 'change1dPct') {
    const m = Math.abs(r.threshold)
    const hit = r.operator === '<' ? v <= -m : r.operator === '>' ? v >= m : Math.abs(v) >= m
    return hit ? 'triggered' : 'ok'
  }
  if (r.metric === 'compositeScore' && (r.params?.mode === 'delta')) return 'pending' // needs the score history
  if (r.operator === '>') return v > r.threshold ? 'triggered' : 'ok'
  if (r.operator === '<') return v < r.threshold ? 'triggered' : 'ok'
  return Math.abs(v - r.threshold) / Math.max(1e-9, Math.abs(r.threshold)) < 0.01 ? 'triggered' : 'ok'
}
