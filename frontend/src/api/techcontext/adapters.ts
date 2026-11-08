import type { TechContext } from './types'

export type ChipTone = 'up' | 'down' | 'warn' | 'info' | 'neutral'

export interface ContextChip {
  key: 'range' | 'rv' | 'iv' | 'atr' | 'adx' | 'trend' | 'earnings'
  label: string
  value: string
  tone: ChipTone
  tooltip: string
}

const pct = (v: number | null | undefined, d = 1) => (v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(d)}%`)
/** percentile 0..1 -> "p45" */
export const pctl = (p: number | null | undefined) => (p == null || !Number.isFinite(p) ? null : `p${Math.round(p * 100)}`)

const ARROW: Record<string, string> = { up: '▲', down: '▼', flat: '■', 'n/a': '–' }

/** Compact chips for the context row; the tooltip text comes from the API (`notes`) plus the live numbers. */
export function contextChips(c: TechContext | undefined): ContextChip[] {
  if (!c || c.data_status === 'pending') return []
  const n = c.notes ?? {}
  const out: ContextChip[] = []
  const rw = c.range_width
  if (rw.percentile != null) {
    const st = rw.state
    out.push({
      key: 'range',
      label: st === 'squeeze' ? 'Squeeze' : st === 'expansion' ? 'Expansion' : 'Range normal',
      value: `20d ±${pct(rw.width_pct != null ? rw.width_pct / 2 : null)} · ${pctl(rw.percentile)}`,
      tone: st === 'squeeze' ? 'info' : st === 'expansion' ? 'warn' : 'neutral',
      tooltip: `${n.range_width ?? ''} Today: ${pctl(rw.percentile)} of the last ${c.lookback_sessions} sessions (n = ${rw.n}).`.trim(),
    })
  }
  const rv = c.realised_vol
  if (rv.vol != null) {
    out.push({ key: 'rv', label: 'RV 20d', value: `${pct(rv.vol, 0)}${rv.percentile != null ? ` · ${pctl(rv.percentile)}` : ''}`, tone: rv.state === 'expansion' ? 'warn' : rv.state === 'squeeze' ? 'info' : 'neutral', tooltip: n.realised_vol ?? '' })
  }
  out.push({ key: 'iv', label: 'IV', value: c.implied_vol.available && c.implied_vol.value != null ? pct(c.implied_vol.value, 0) : 'n/a', tone: 'neutral', tooltip: c.implied_vol.note })
  const a = c.atr
  if (a.atr_pct != null) {
    out.push({ key: 'atr', label: 'ATR 14', value: `${pct(a.atr_pct)}${a.percentile != null ? ` · ${pctl(a.percentile)}` : ''}`, tone: a.state === 'expansion' ? 'warn' : a.state === 'squeeze' ? 'info' : 'neutral', tooltip: n.atr ?? '' })
  }
  const x = c.adx
  if (x.adx != null) {
    out.push({
      key: 'adx',
      label: 'ADX 14',
      value: `${x.adx.toFixed(0)} ${x.state}${x.direction ? ` ${ARROW[x.direction]}` : ''}`,
      tone: x.state === 'trending' || x.state === 'strong' ? (x.direction === 'down' ? 'down' : 'up') : 'neutral',
      tooltip: `${n.adx ?? ''}${x.plus_di != null && x.minus_di != null ? ` +DI ${x.plus_di.toFixed(0)} / −DI ${x.minus_di.toFixed(0)}.` : ''}`,
    })
  }
  const t = c.trend
  if (t.alignment !== 'n/a') {
    const al = t.alignment
    out.push({
      key: 'trend',
      label: al === 'aligned_up' || al === 'aligned_down' ? 'Trend aligned' : al === 'mixed' ? 'Trend mixed' : 'Trend',
      value: `D ${ARROW[t.daily]} · W ${ARROW[t.weekly]}`,
      tone: al === 'aligned_up' ? 'up' : al === 'aligned_down' ? 'down' : al === 'mixed' ? 'warn' : 'neutral',
      tooltip: n.trend ?? '',
    })
  }
  const e = c.earnings
  if (e.next_date || e.typical_abs_move != null) {
    const when = e.sessions_ahead != null ? `in ${e.sessions_ahead} session${e.sessions_ahead === 1 ? '' : 's'}` : e.next_date ? `on ${e.next_date}` : 'no date'
    const move = e.typical_abs_move != null ? ` · typical ±${pct(e.typical_abs_move)}` : ''
    out.push({
      key: 'earnings',
      label: 'Earnings',
      value: `${when}${e.in_window ? ' · in range' : ''}${move}`,
      tone: e.in_window ? 'warn' : 'neutral',
      tooltip: `${n.earnings ?? ''}${e.in_window ? ' The calibrated range includes the earnings-event pool.' : ''}${e.n_events ? ` Typical move from the last ${e.n_events} releases.` : ''}`.trim(),
    })
  }
  return out
}
