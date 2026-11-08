import { DASH, getFormatLocale } from '@/lib/format'

const MINUS = '−'

/** Macro values span index levels, rates and growth: 4 significant digits, thousands grouped. */
export function fmtMacro(v: unknown, sig = 4): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return DASH
  if (v === 0) return '0'
  const a = Math.abs(v)
  if (Number.isInteger(v) && a < 1e6) return new Intl.NumberFormat(getFormatLocale(), { maximumFractionDigits: 0 }).format(v).replace('-', MINUS)
  if (a >= 1e6) return new Intl.NumberFormat(getFormatLocale(), { notation: 'compact', maximumFractionDigits: 2 }).format(v).replace('-', MINUS)
  const digits = a >= 1000 ? 0 : Math.max(0, Math.min(4, sig - 1 - Math.floor(Math.log10(a))))
  return new Intl.NumberFormat(getFormatLocale(), { minimumFractionDigits: Math.min(digits, 2), maximumFractionDigits: digits }).format(v).replace('-', MINUS)
}

export function fmtChange(v: unknown, digits = 2): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return DASH
  const abs = Math.abs(v).toFixed(digits)
  if (Number(abs) === 0) return abs
  return `${v > 0 ? '+' : MINUS}${abs}`
}

export function fmtProb(v: unknown): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return DASH
  if (v < 0.001) return '<0.1%'
  return `${(v * 100).toFixed(v < 0.1 ? 1 : 0)}%`
}

export const QUADRANT_LABEL: Record<string, string> = { recovery: 'Recovery', expansion: 'Expansion', slowdown: 'Slowdown', contraction: 'Contraction' }

/** Local time "HH:MM" of an ISO timestamp with offset (scheduled_local keeps the publisher's offset). */
export function localTime(iso: string): string {
  const m = /T(\d{2}:\d{2})/.exec(iso)
  return m ? m[1] : DASH
}

/** Background tint for an oriented z bucket: teal = good, rose = bad, grey = neutral (rates etc.). */
export function zStyle(bucket: number | null): { background: string } {
  if (bucket === null) return { background: 'var(--surface-2)' }
  if (bucket === 0) return { background: 'var(--surface-3)' }
  const pct = [0, 18, 32, 48][Math.abs(bucket)]
  return { background: `color-mix(in srgb, ${bucket > 0 ? 'var(--up)' : 'var(--down)'} ${pct}%, var(--surface))` }
}

export const METHOD_SHORT: Record<string, string> = {
  nyfed_yield_curve_official: 'NY Fed yield curve',
  yield_curve_probit: 'Yield-curve probit',
  sahm_realtime: 'Sahm rule',
  hmm_regime: 'HMM regime',
  oecd_cli_turning_point: 'OECD CLI phase',
}

/** ISO date (UTC) n days from now; today when n = 0. */
export function isoDaysFromNow(n = 0): string {
  return new Date(Date.now() + n * 86400000).toISOString().slice(0, 10)
}
