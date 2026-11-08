import { fmtProb, fmtR, type Tone } from '../techprob/adapters'
import type { TmDriver, TmHorizon, TmSetup, TmView } from './types'

const ok = (x: number | null | undefined): x is number => typeof x === 'number' && Number.isFinite(x)

/** The meta row matching the T2 horizon on screen (10 / 20 / 40), else H = 20, else the first one. */
export function metaHorizon(v: TmView | TmSetup | null | undefined, horizon?: number | null): TmHorizon | null {
  const rows = v?.horizons ?? []
  if (!rows.length) return null
  return rows.find((h) => h.horizon === horizon) ?? rows.find((h) => h.horizon === 20) ?? rows[0]
}

/** The row to show: the horizon on screen when visible; otherwise (toggle off and that horizon experimental) the first
 * production horizon, so a validated meta view is shown by default even when the T2 card sits on another horizon. */
export function pickMetaHorizon(v: TmView | TmSetup | null | undefined, horizon: number | null | undefined, showExperimental: boolean): TmHorizon | null {
  const h = metaHorizon(v, horizon)
  if (!h || showExperimental || !h.experimental) return h
  return (v?.horizons ?? []).find((x) => !x.experimental) ?? h
}

/** "0.80% of equity at risk" (quarter-Kelly fraction, capped at 2%). */
export const fmtKelly = (f: number | null | undefined) => (!ok(f) ? '—' : `${(f * 100).toFixed(2)}% of equity at risk`)

/** "+3.1 pp" (probability difference). */
export const fmtPp = (d: number | null | undefined, digits = 1) => (!ok(d) ? '—' : `${d >= 0 ? '+' : '−'}${Math.abs(d * 100).toFixed(digits)} pp`)

export interface DriverChip {
  key: string
  text: string
  title: string
  tone: Tone
}

export function driverChip(d: TmDriver): DriverChip {
  const up = d.direction === 'raises'
  const c = ok(d.contribution) ? `${d.contribution >= 0 ? '+' : '−'}${Math.abs(d.contribution).toFixed(2)}` : '—'
  const v = ok(d.value) ? ` (value ${Math.abs(d.value) >= 100 ? d.value.toFixed(0) : d.value.toFixed(2)})` : ''
  return { key: d.feature, text: `${up ? '↑' : '↓'} ${d.label}`, title: `${d.label}${v}: ${up ? 'raises' : 'lowers'} the chance of T1 first (${c} logit vs T2)`, tone: up ? 'up' : 'down' }
}

export interface MetaRow {
  horizon: number
  pWin: string
  t2: string
  delta: string
  deltaTone: Tone
  er: string
  erTone: Tone
  decision: 'take' | 'skip' | null
  decisionHidden: boolean
  size: string | null
  drivers: DriverChip[]
  experimental: boolean
  decisionExperimental: boolean
  /** The whole row sits behind the experimental toggle (failed gate / out of domain, toggle off). */
  hidden: boolean
}

/** Display model of the "Context-adjusted (meta)" row. Production rows always show; experimental ones only with the
 * toggle. Take / skip and size have their own (economic) gate: hidden unless that passed or the toggle is on. */
export function metaRow(h: TmHorizon | null, showExperimental: boolean): MetaRow | null {
  if (!h || !ok(h.p_win)) return null
  const hidden = h.experimental && !showExperimental
  const decisionHidden = h.decision_experimental && !showExperimental
  const d = ok(h.delta_p_win) ? h.delta_p_win : ok(h.t2_p_win) ? h.p_win - h.t2_p_win : null
  const rel = ok(h.size_rel) && h.size_rel > 0 ? ` · ${h.size_rel.toFixed(1)}× the median taken setup` : ''
  return {
    horizon: h.horizon,
    pWin: fmtProb(h.p_win),
    t2: fmtProb(h.t2_p_win),
    delta: fmtPp(d),
    deltaTone: d == null || Math.abs(d) < 0.005 ? 'neutral' : d > 0 ? 'up' : 'down',
    er: fmtR(h.expected_r),
    erTone: !ok(h.expected_r) ? 'neutral' : h.expected_r > 0.02 ? 'up' : h.expected_r < -0.02 ? 'down' : 'neutral',
    decision: h.decision ?? null,
    decisionHidden,
    size: h.decision === 'take' && ok(h.kelly_fraction) ? `${fmtKelly(h.kelly_fraction)}${rel}` : null,
    drivers: (h.drivers ?? []).slice(0, 3).map(driverChip),
    experimental: h.experimental,
    decisionExperimental: h.decision_experimental,
    hidden,
  }
}

/** Thesis table rows (custom barriers): one per horizon present (10 / 20 / 40). */
export function thesisMetaRows(v: TmView | null | undefined, showExperimental: boolean): MetaRow[] {
  return (v?.horizons ?? []).map((h) => metaRow(h, showExperimental)).filter((r): r is MetaRow => !!r && !r.hidden)
}

/** One-line validation status for the popover / footnote. */
export function metaGateLine(v: TmView | TmSetup | null | undefined, horizon: number): string {
  const g = (v?.validation ?? []).find((x) => x.output === `meta_h${horizon}`)
  const d = (v?.validation ?? []).find((x) => x.output === `decision_h${horizon}`)
  const word = (x?: { verdict: string }) => (x?.verdict === 'pass' ? 'passed' : x?.verdict === 'fail' ? 'failed' : 'not validated')
  return `Calibration gate ${word(g)} · take / skip + size ${word(d)} (H = ${horizon})`
}
