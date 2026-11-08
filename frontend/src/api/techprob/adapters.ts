import type { TpScore, TpScoreBucket, TpSetup, TpSetupHorizon, TpZone, TpZones } from './types'

export type Tone = 'up' | 'down' | 'warn' | 'info' | 'neutral'

const ok = (x: number | null | undefined): x is number => typeof x === 'number' && Number.isFinite(x)

/** 0..1 -> "41%" ("<1%" in the calibrated tail). */
export const fmtProb = (p: number | null | undefined, digits = 0) => (!ok(p) ? '—' : p > 0 && p < 0.005 ? '<1%' : `${(p * 100).toFixed(digits)}%`)

/** Expected value in R multiples: "+0.12R". */
export const fmtR = (r: number | null | undefined) => (!ok(r) ? '—' : `${r >= 0 ? '+' : '−'}${Math.abs(r).toFixed(2)}R`)

/** Excess log return -> simple % with sign ("+0.42%"). */
export const fmtExcess = (x: number | null | undefined, digits = 2) => (!ok(x) ? '—' : `${x >= 0 ? '+' : '−'}${Math.abs((Math.exp(x) - 1) * 100).toFixed(digits)}%`)

/** Horizons the viewer may see: production always, experimental only with the toggle. */
export function visibleHorizons(d: TpSetup | undefined, showExperimental: boolean): TpSetupHorizon[] {
  if (!d || d.data_status === 'pending') return []
  return d.horizons.filter((h) => showExperimental || !h.experimental)
}

/** The horizon to display: the requested one, else the setup's own horizon, else the first visible one. */
export function pickHorizon(d: TpSetup | undefined, showExperimental: boolean, horizon?: number | null): TpSetupHorizon | null {
  const rows = visibleHorizons(d, showExperimental)
  if (!rows.length) return null
  if (horizon != null) {
    const r = rows.find((x) => x.horizon === horizon)
    if (r) return r
  }
  return rows.find((x) => x.is_setup_horizon) ?? rows.find((x) => x.horizon === 20) ?? rows[0]
}

export interface ProbSegment {
  key: 'win' | 'loss' | 'neither'
  label: string
  value: number
  tone: Tone
}

/** Stacked-bar segments P(win) / P(loss) / P(neither by H); sums to 1 (rounding guarded). */
export function probSegments(h: TpSetupHorizon | null): ProbSegment[] {
  if (!h || !ok(h.p_win) || !ok(h.p_loss)) return []
  const w = Math.max(0, h.p_win)
  const l = Math.max(0, h.p_loss)
  const n = ok(h.p_neither) ? Math.max(0, h.p_neither) : Math.max(0, 1 - w - l)
  const s = w + l + n || 1
  return [
    { key: 'win', label: 'T1 first', value: w / s, tone: 'up' },
    { key: 'loss', label: 'Stop first', value: l / s, tone: 'down' },
    { key: 'neither', label: `Neither by ${h.horizon}d`, value: n / s, tone: 'neutral' },
  ]
}

/** One-line reading of the expectancy: sign of expected R and P(win) vs the break-even probability. */
export function expectancy(h: TpSetupHorizon | null): { tone: Tone; text: string } | null {
  if (!h || !ok(h.expected_r)) return null
  const be = ok(h.break_even_p) ? ` · P(win) ${fmtProb(h.p_win)} vs break-even ${fmtProb(h.break_even_p)}` : ''
  if (h.expected_r > 0.02) return { tone: 'up', text: `Positive expectancy after costs${be}` }
  if (h.expected_r < -0.02) return { tone: 'down', text: `Negative expectancy after costs${be}` }
  return { tone: 'neutral', text: `About break-even after costs${be}` }
}

export function zoneHoldIndex(d: TpZones | undefined): Map<string, TpZone> {
  const m = new Map<string, TpZone>()
  if (!d || d.data_status === 'pending') return m
  for (const z of d.zones) m.set(`z${z.zone_id}`, z)
  return m
}

export interface HoldCell {
  text: string
  sub: string | null
  badge: { label: string; tone: Tone } | null
  title: string
  hidden: boolean
}

/** Zone-table cell: P(hold | touch), P(touch) x P(hold), and the edge badge ("no reliable edge" when n.s.). */
export function holdCell(z: TpZone | undefined, showExperimental: boolean): HoldCell {
  if (!z || !ok(z.p_hold)) return { text: '—', sub: null, badge: null, title: 'No hold model output for this zone', hidden: false }
  const holdHidden = z.experimental && !showExperimental
  const edgeShown = z.edge_significant && (!z.edge_experimental || showExperimental)
  let badge: HoldCell['badge']
  if (edgeShown && ok(z.edge)) {
    badge = z.edge_direction === 'worse_than_random'
      ? { label: `${(z.edge * 100).toFixed(1)}pp vs random`, tone: 'down' }
      : { label: `+${(z.edge * 100).toFixed(1)}pp vs random`, tone: 'up' }
  } else {
    badge = { label: 'no reliable edge', tone: 'neutral' }
  }
  const ci = z.edge_ci && ok(z.edge_ci[0]) && ok(z.edge_ci[1]) ? ` (walk-forward CI ${(z.edge_ci[0] * 100).toFixed(1)} to ${(z.edge_ci[1] * 100).toFixed(1)} pp)` : ''
  const title = [
    `P(hold | touch) ${fmtProb(z.p_hold)}; random control bands with the same width / approach / trend ${fmtProb(z.p_hold_ctrl)}`,
    edgeShown ? `edge ${ok(z.edge) ? (z.edge * 100).toFixed(1) : '—'} pp${ci}` : `No reliable edge vs random levels${ci}`,
    ok(z.p_touch) ? `P(touch) ${fmtProb(z.p_touch)} -> touch and hold ${fmtProb(z.p_touch_hold)}` : null,
    z.experimental ? 'EXPERIMENTAL (failed the validation gate)' : null,
  ].filter(Boolean).join(' · ')
  if (holdHidden) {
    // the hold probability failed the gate (hidden); the edge flag is a separate, validated output and stays visible
    const edgeOnly = edgeShown ? badge : z.edge_experimental && !showExperimental ? null : badge
    return { text: '—', sub: null, badge: edgeOnly, title: `Experimental: P(hold | touch) failed the validation gate (enable "experimental" to show it)${edgeOnly ? ` · ${edgeOnly.label}` : ''}`, hidden: true }
  }
  return { text: fmtProb(z.p_hold), sub: ok(z.p_touch_hold) ? `touch+hold ${fmtProb(z.p_touch_hold)}` : null, badge, title, hidden: false }
}

const SUB_LABEL: Record<string, string> = { trend: 'Trend', structure: 'Structure', location: 'Location', rr: 'Reward:risk', momentum: 'Momentum' }

export interface SubScoreRow {
  key: string
  label: string
  raw: number | null
  percentile: number | null
}

export function subScoreRows(d: TpScore | undefined): SubScoreRow[] {
  if (!d) return []
  return d.sub_scores.map((s) => ({ key: s.name, label: SUB_LABEL[s.name] ?? s.name, raw: ok(s.raw) ? s.raw : null, percentile: ok(s.percentile) ? s.percentile : null }))
}

/** "Historically this score bucket -> ..." lines the viewer may see (experimental hidden unless toggled). */
export function bucketLines(d: TpScore | undefined, showExperimental: boolean): (TpScoreBucket & { badge: { label: string; tone: Tone } })[] {
  if (!d || d.data_status === 'pending') return []
  return d.buckets
    .filter((b) => b.line && (showExperimental || !b.experimental))
    .map((b) => ({ ...b, badge: b.edge_significant ? { label: 'out-of-sample edge', tone: 'up' as Tone } : { label: 'no out-of-sample edge', tone: 'neutral' as Tone } }))
}

/** Thesis tracker rows: target / stop probabilities per horizon (custom barriers). */
export function thesisProbRows(d: TpSetup | undefined, showExperimental: boolean): { horizon: number; win: string; loss: string; neither: string; er: string; own: boolean }[] {
  return visibleHorizons(d, showExperimental).map((h) => ({
    horizon: h.horizon, win: fmtProb(h.p_win), loss: fmtProb(h.p_loss), neither: fmtProb(h.p_neither), er: fmtR(h.expected_r), own: h.is_setup_horizon,
  }))
}

/** Does any output of the response sit behind the experimental toggle? */
export const hasExperimental = (d: TpSetup | TpZones | TpScore | undefined) =>
  !!d && d.validation.some((v) => v.experimental)
