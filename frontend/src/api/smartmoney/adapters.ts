/** Pure helpers for the smart-money pages (labels, tones, chart series). Dates are ISO yyyy-mm-dd. */
import type { CoHolder, CoOwnership, CoTrendPoint, SmEuShort, SmLag, SmRow } from './types'

export type Tone = 'neutral' | 'accent' | 'up' | 'down' | 'warn' | 'info'

export const REGULATOR_LABEL: Record<string, string> = {
  fca: 'UK FCA',
  amf: 'France AMF',
  cnmv: 'Spain CNMV',
  afm: 'Netherlands AFM',
  cbi: 'Ireland CBI',
  fsma: 'Belgium FSMA',
  fi_se: 'Sweden FI',
  ftno: 'Norway Finanstilsynet',
}
export const regulatorLabel = (id: string | null | undefined): string => (id ? (REGULATOR_LABEL[id] ?? id.toUpperCase()) : '—')

export const EVENT_META: Record<string, { label: string; tone: Tone }> = {
  new_13d: { label: 'New 13D', tone: 'accent' },
  switch_13g_to_13d: { label: '13G → 13D', tone: 'warn' },
  amend_13d: { label: '13D/A', tone: 'neutral' },
  new_13g: { label: 'New 13G', tone: 'info' },
  amend_13g: { label: '13G/A', tone: 'neutral' },
}
export const eventMeta = (t: string) => EVENT_META[t] ?? { label: t, tone: 'neutral' as Tone }

export const HOLDER_STATUS: Record<CoHolder['status'], { label: string; tone: Tone }> = {
  new: { label: 'New', tone: 'up' },
  added: { label: 'Added', tone: 'up' },
  trimmed: { label: 'Trimmed', tone: 'down' },
  unchanged: { label: 'Unchanged', tone: 'neutral' },
  unknown: { label: '—', tone: 'neutral' },
}

/** "13F positions as of quarter end Q, filed by D" (always shown next to 13F numbers). */
export function lagText(lag: SmLag | null | undefined): string {
  if (!lag) return '13F: no quarter available yet'
  return `Positions as of quarter end ${lag.period_end}, filed by ${lag.cutoff}${lag.is_complete ? '' : ' (filing window still open)'}`
}

/** Days between the quarter end and the cutoff (the 13F disclosure lag actually applied). */
export function lagDays(lag: SmLag | null | undefined): number | null {
  if (!lag) return null
  return Math.round((Date.parse(`${lag.cutoff}T00:00:00Z`) - Date.parse(`${lag.period_end}T00:00:00Z`)) / 86_400_000)
}

/** Signed numbers carry their sign in text; tone is a redundant cue. */
export function signTone(v: number | null | undefined, invert = false): Tone {
  if (typeof v !== 'number' || !Number.isFinite(v) || v === 0) return 'neutral'
  return (v > 0) !== invert ? 'up' : 'down'
}

/** 13F new vs exited positions label for a row. */
export function flowLabel(r: Pick<SmRow, 'n_new' | 'n_exited'>): string {
  const n = r.n_new ?? 0
  const x = r.n_exited ?? 0
  return `${n} new / ${x} exited`
}

/** Ownership trend chart series (inst % of shares outstanding, holders) in quarter order. */
export function trendSeries(rows: CoTrendPoint[]): { x: string[]; pct: (number | null)[]; holders: number[]; complete: boolean[] } {
  const r = [...rows].sort((a, b) => a.period_end.localeCompare(b.period_end))
  return { x: r.map((p) => p.period_end), pct: r.map((p) => p.inst_own_pct ?? null), holders: r.map((p) => p.n_holders), complete: r.map((p) => p.is_complete) }
}

/** Short-interest series (% of shares outstanding, falls back to shares when the share count is unknown). */
export function siSeries(rows: CoOwnership['short_interest']): { x: string[]; y: (number | null)[]; unit: 'pct' | 'shares' } {
  const r = [...rows].sort((a, b) => a.settlement_date.localeCompare(b.settlement_date))
  const hasPct = r.some((p) => p.si_pct_out != null)
  return { x: r.map((p) => p.settlement_date), y: r.map((p) => (hasPct ? (p.si_pct_out ?? null) : (p.si_shares ?? null))), unit: hasPct ? 'pct' : 'shares' }
}

/** Disclosed short % over time from the notification list: per date, the sum of each holder's latest position. */
export function euShortSeries(rows: SmEuShort[]): { x: string[]; y: number[] } {
  const ind = rows.filter((r) => r.position_type === 'individual').sort((a, b) => a.position_date.localeCompare(b.position_date))
  const agg = rows.filter((r) => r.position_type === 'aggregate').sort((a, b) => a.position_date.localeCompare(b.position_date))
  if (agg.length) return { x: agg.map((r) => r.position_date), y: agg.map((r) => r.position_pct ?? 0) }
  const cur = new Map<string, number>()
  const x: string[] = []
  const y: number[] = []
  for (const r of ind) {
    cur.set(`${r.regulator}|${r.holder_name}`, r.below_threshold ? 0 : (r.position_pct ?? 0))
    const tot = [...cur.values()].reduce((s, v) => s + (v >= 0.5 ? v : 0), 0)
    if (x.length && x[x.length - 1] === r.position_date) y[y.length - 1] = tot
    else {
      x.push(r.position_date)
      y.push(tot)
    }
  }
  return { x, y }
}

/** Change of a register row vs the holder's previous notification, in percentage points. */
export function euChange(r: Pick<SmEuShort, 'position_pct' | 'prev_pct'>): number | null {
  if (r.position_pct == null || r.prev_pct == null) return null
  return Math.round((r.position_pct - r.prev_pct) * 100) / 100
}

/** Split holders into the hedge-fund-like / smart-cohort counts shown in the summary. */
export function holderMix(hs: CoHolder[]): { total: number; hf: number; smart: number; newOrAdded: number; trimmed: number } {
  return {
    total: hs.length,
    hf: hs.filter((h) => h.hedge_fund_like).length,
    smart: hs.filter((h) => h.in_smart_cohort).length,
    newOrAdded: hs.filter((h) => h.status === 'new' || h.status === 'added').length,
    trimmed: hs.filter((h) => h.status === 'trimmed').length,
  }
}
