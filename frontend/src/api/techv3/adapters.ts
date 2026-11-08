import type { Tv3Breakout, Tv3Composite, Tv3Factor, Tv3Factors } from './types'

export type Tone = 'up' | 'down' | 'warn' | 'info' | 'neutral'

const ok = (x: number | null | undefined): x is number => typeof x === 'number' && Number.isFinite(x)

export const fmtProb = (p: number | null | undefined, digits = 0) => (!ok(p) ? '—' : p > 0 && p < 0.005 ? '<1%' : `${(p * 100).toFixed(digits)}%`)

/** Rank IC with sign: "+0.021". */
export const fmtIc = (x: number | null | undefined) => (!ok(x) ? '—' : `${x >= 0 ? '+' : '−'}${Math.abs(x).toFixed(3)}`)

/** "p62" percentile label. */
export const fmtPct = (x: number | null | undefined) => (!ok(x) ? '—' : `p${Math.round(x)}`)

/** Short labels for the narrow technicals column (the tooltip carries the full name and reference). */
export const SHORT_LABEL: Record<string, string> = {
  hi52: '52-week-high proximity', mom: '12-1 momentum', rmom: 'Residual momentum', strev: '1-month reversal',
  maxret: 'MAX (lottery)', ivol: 'Idiosyncratic vol', indmom: 'Industry momentum',
}

export interface FactorRow {
  id: string
  label: string
  short: string
  percentile: number | null
  aligned: number | null
  favourable: boolean | null
  badge: { label: string; tone: Tone }
  experimental: boolean
  tooltip: string
  icLine: string
  note: string | null
  tradable: boolean | null
}

/** Factors the viewer may see: gate-passing always, failed / not validated only with the experimental toggle. */
export function visibleFactors(d: Tv3Factors | undefined, showExperimental: boolean): Tv3Factor[] {
  if (!d || d.data_status === 'pending') return []
  return d.factors.filter((f) => showExperimental || !f.experimental)
}

export function hiddenCount(d: Tv3Factors | undefined, showExperimental: boolean): number {
  if (!d || d.data_status === 'pending' || showExperimental) return 0
  return d.factors.filter((f) => f.experimental).length
}

/** IC statistics in the factor's favourable direction (aligned IC and aligned t: > 0 = works as documented). */
function icText(f: Tv3Factor): string {
  return f.ic
    .map((c) => `${c.horizon}d IC ${fmtIc(c.aligned_ic)}${ok(c.t_stat) ? ` (t ${(c.t_stat * f.sign).toFixed(1)}${ok(c.p_bh) ? `, BH p ${c.p_bh < 0.001 ? '<0.001' : c.p_bh.toFixed(3)}` : ''})` : ''}`)
    .join(' · ')
}

export function factorRows(d: Tv3Factors | undefined, showExperimental: boolean): FactorRow[] {
  return visibleFactors(d, showExperimental).map((f) => {
    const c20 = f.ic.find((c) => c.horizon === 20)
    const aligned = ok(f.aligned_percentile) ? f.aligned_percentile : null
    const region = ok(c20?.region_ic ?? null) ? ` · ${d?.region} 20d IC ${fmtIc((c20!.region_ic as number) * f.sign)}` : ''
    return {
      id: f.id,
      label: f.label,
      short: SHORT_LABEL[f.id] ?? f.label,
      percentile: ok(f.percentile) ? f.percentile : null,
      aligned,
      favourable: aligned == null ? null : aligned >= 50,
      badge: { label: f.badge.label, tone: (f.badge.tone as Tone) ?? 'neutral' },
      experimental: f.experimental,
      icLine: icText(f),
      note: f.note ?? null,
      tradable: f.tradable ?? null,
      tooltip: `${f.label}: ${f.direction_note}. Out-of-sample (2005–2026, sign-aligned): ${icText(f)}${region}. Gate: ${f.verdict}.${f.note ? ` ${f.note}.` : ''} Source: ${f.reference}`,
    }
  })
}

export interface CompositeHeadline {
  mode: 'composite' | 'no_edge' | 'none'
  value: number | null
  percentile: number | null
  members: string[]
  note: string
  ic: string | null
}

/** The technical score card's headline: the evidence-based composite ONLY when it passed the gate. */
export function compositeHeadline(d: Tv3Factors | undefined): CompositeHeadline {
  const c: Tv3Composite | undefined = d?.composite
  if (!d || d.data_status === 'pending' || !c) return { mode: 'none', value: null, percentile: null, members: [], note: '', ic: null }
  if (c.verdict === 'pass' && ok(c.value)) {
    return {
      mode: 'composite', value: c.value, percentile: ok(c.percentile) ? c.percentile : null, members: c.members, note: c.note,
      ic: `walk-forward 20d IC ${fmtIc(c.ic_20)}${ok(c.t_20) ? ` (t ${c.t_20.toFixed(1)})` : ''}${ok(c.v2_ic_20) ? ` vs v2 score ${fmtIc(c.v2_ic_20)}` : ''}`,
    }
  }
  return { mode: 'no_edge', value: null, percentile: null, members: c.members, note: c.note, ic: ok(c.ic_20) ? `walk-forward 20d IC ${fmtIc(c.ic_20)}` : null }
}

export interface BreakoutChip {
  visible: boolean
  label: string
  tone: Tone
  prob: string | null
  probHidden: boolean
  sub: string
  confirmation?: string
  title: string
}

const STATE_LABEL: Record<string, string> = { active: 'active', followed_through: 'followed through', failed: 'failed', none: 'none' }

/** Breakout state chip: the state is always shown; P(follow-through) only when it passed the gate (or with the toggle). */
export function breakoutChip(b: Tv3Breakout | undefined, showExperimental: boolean): BreakoutChip {
  const hidden: BreakoutChip = { visible: false, label: '', tone: 'neutral', prob: null, probHidden: false, sub: '', title: '' }
  if (!b || b.data_status === 'pending' || b.state === 'none' || b.direction == null) return hidden
  const up = b.direction > 0
  const arrow = up ? '▲' : '▼'
  const tone: Tone = b.state === 'failed' ? 'warn' : b.state === 'followed_through' ? (up ? 'up' : 'down') : up ? 'up' : 'down'
  const showP = !b.experimental || showExperimental
  const prob = showP && ok(b.p_follow) ? fmtProb(b.p_follow) : null
  const conf = b.confirmed == null ? '' : b.confirmed ? 'volume-confirmed' : 'not volume-confirmed'
  const ago = b.sessions_since != null ? `${b.sessions_since} session${b.sessions_since === 1 ? '' : 's'} ago` : ''
  return {
    visible: true,
    label: `${arrow} ${b.lookback ?? ''}d ${up ? 'high' : 'low'} breakout · ${STATE_LABEL[b.state] ?? b.state}`,
    tone,
    prob,
    probHidden: !showP && ok(b.p_follow),
    sub: [ok(b.confirm_score) ? `confirmation score ${Math.round(b.confirm_score)}` : '', ago].filter(Boolean).join(' · '),
    confirmation: conf,
    title: `${b.labels.breakout ?? ''} ${b.labels.confirmation ?? ''}`.trim(),
  }
}

/** Evidence line from the validation: confirmed vs unconfirmed follow-through and its verdict. */
export function evidenceLine(b: Tv3Breakout | undefined): { text: string; tone: Tone; title?: string } | null {
  const e = b?.evidence
  if (!e || !ok(e.confirmed_rate) || !ok(e.unconfirmed_rate)) return null
  const t = `Historically: confirmed ${fmtProb(e.confirmed_rate)} vs unconfirmed ${fmtProb(e.unconfirmed_rate)} follow-through (base ${fmtProb(e.base_rate)}`
    + `${ok(e.control_rate) ? `, random levels ${fmtProb(e.control_rate)}` : ''})`
  const econ = e.summary ? ' Not a tradable edge after costs (see tooltip).' : ''
  const title = e.summary ?? undefined
  return e.significant
    ? { text: `${t} — confirmed breakouts follow through significantly more often.${econ}`, tone: e.summary ? 'info' : 'up', title }
    : { text: `${t} — no reliable edge from volume confirmation.`, tone: 'neutral', title }
}
