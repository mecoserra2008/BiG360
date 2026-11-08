import type { CurrencyCode, ValueUnit } from '@/api/types'

/**
 * Shared number/date formatters. Every figure in the UI goes through here so the locale
 * (en-US / en-GB / pt-PT / de-DE …) is applied consistently and nulls render as an em dash.
 */

let LOCALE = 'en-US'
export function setFormatLocale(locale: string): void {
  LOCALE = locale
  cache.clear()
  dfCache.clear()
}
export function getFormatLocale(): string {
  return LOCALE
}

const cache = new Map<string, Intl.NumberFormat>()
function nf(opts: Intl.NumberFormatOptions): Intl.NumberFormat {
  const key = JSON.stringify(opts)
  let f = cache.get(key)
  if (!f) {
    f = new Intl.NumberFormat(LOCALE, opts)
    cache.set(key, f)
  }
  return f
}

export const DASH = '—'
const MINUS = '−'

export const isNum = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v)

function signed(s: string, v: number, sign: boolean): string {
  const t = s.replace('-', MINUS)
  if (sign && v > 0) return `+${t}`
  return t
}

export function fmtNum(v: number | null | undefined, digits = 1, sign = false): string {
  if (!isNum(v)) return DASH
  return signed(nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v), v, sign)
}

export function fmtInt(v: number | null | undefined): string {
  if (!isNum(v)) return DASH
  return signed(nf({ maximumFractionDigits: 0 }).format(v), v, false)
}

/** Decimal ratio → percent. 0.1234 → "12.3%" */
export function fmtPct(v: number | null | undefined, digits = 1, sign = false): string {
  if (!isNum(v)) return DASH
  return signed(nf({ style: 'percent', minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v), v, sign)
}

export const fmtSignedPct = (v: number | null | undefined, digits = 1): string => fmtPct(v, digits, true)

/** Basis points (input already in bps). */
export function fmtBps(v: number | null | undefined, sign = false): string {
  if (!isNum(v)) return DASH
  return `${signed(nf({ maximumFractionDigits: 0 }).format(v), v, sign)} bp`
}

export function fmtMultiple(v: number | null | undefined, digits = 1): string {
  if (!isNum(v)) return DASH
  if (v < 0) return 'n.m.'
  return `${nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(v)}x`
}

/** 1.23e12 → "$1.23T" (locale-aware compact notation). */
export function fmtCompactCurrency(v: number | null | undefined, currency: CurrencyCode = 'USD', digits = 2): string {
  if (!isNum(v)) return DASH
  const s = nf({
    style: 'currency',
    currency,
    notation: 'compact',
    maximumFractionDigits: Math.abs(v) >= 1e3 ? digits : 0,
    minimumFractionDigits: 0,
  }).format(v)
  return s.replace('-', MINUS)
}

export function fmtCompact(v: number | null | undefined, digits = 1, sign = false): string {
  if (!isNum(v)) return DASH
  return signed(nf({ notation: 'compact', maximumFractionDigits: digits }).format(v), v, sign)
}

const ZERO_DEC = new Set(['JPY', 'KRW', 'CLP'])
export function fmtPrice(v: number | null | undefined, currency?: CurrencyCode, withSymbol = false): string {
  if (!isNum(v)) return DASH
  const d = currency && ZERO_DEC.has(currency) ? 0 : 2
  if (withSymbol && currency) {
    return nf({ style: 'currency', currency, minimumFractionDigits: d, maximumFractionDigits: d }).format(v)
  }
  return nf({ minimumFractionDigits: d, maximumFractionDigits: d }).format(v)
}

export function fmtCurrency(v: number | null | undefined, currency: CurrencyCode = 'USD', digits = 0): string {
  if (!isNum(v)) return DASH
  return nf({ style: 'currency', currency, minimumFractionDigits: digits, maximumFractionDigits: digits })
    .format(v)
    .replace('-', MINUS)
}

/** Statement style: thousands separators, negatives in parentheses. */
export function fmtMillions(v: number | null | undefined, digits = 0): string {
  if (!isNum(v)) return DASH
  const s = nf({ minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Math.abs(v))
  return v < 0 ? `(${s})` : s
}

export function fmtScore(v: number | null | undefined): string {
  if (!isNum(v)) return DASH
  return nf({ maximumFractionDigits: 0 }).format(v)
}

export function fmtZ(v: number | null | undefined): string {
  if (!isNum(v)) return DASH
  return `${signed(nf({ minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(v), v, true)}σ`
}

export function fmtValue(v: number | null | undefined, unit: ValueUnit, currency: CurrencyCode = 'USD'): string {
  switch (unit) {
    case 'pct':
      return fmtPct(v)
    case 'bps':
      return fmtBps(v)
    case 'multiple':
      return fmtMultiple(v)
    case 'currency':
      return fmtCompactCurrency(v, currency)
    case 'ratio':
      return fmtNum(v, 2)
    case 'days':
      return isNum(v) ? `${fmtNum(v, 0)}d` : DASH
    case 'years':
      return isNum(v) ? `${fmtNum(v, 1)}y` : DASH
    case 'score':
      return fmtScore(v)
    default:
      return fmtNum(v, 1)
  }
}

/* dates */

const dfCache = new Map<string, Intl.DateTimeFormat>()
function df(opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify(opts)
  let f = dfCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat(LOCALE, opts)
    dfCache.set(key, f)
  }
  return f
}

function toDate(d: string | Date): Date {
  if (d instanceof Date) return d
  return /^\d{4}-\d{2}-\d{2}$/.test(d) ? new Date(`${d}T00:00:00Z`) : new Date(d)
}

export function fmtDate(d: string | Date | null | undefined): string {
  if (!d) return DASH
  return df({ year: 'numeric', month: 'short', day: '2-digit', timeZone: 'UTC' }).format(toDate(d))
}

export function fmtDateShort(d: string | Date | null | undefined): string {
  if (!d) return DASH
  return df({ month: 'short', day: '2-digit', timeZone: 'UTC' }).format(toDate(d))
}

export function fmtMonth(d: string | Date | null | undefined): string {
  if (!d) return DASH
  return df({ month: 'short', year: '2-digit', timeZone: 'UTC' }).format(toDate(d))
}

export function fmtDateTime(d: string | Date | null | undefined): string {
  if (!d) return DASH
  return df({ year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(
    toDate(d),
  )
}

export function fmtRelative(d: string | Date, now: Date = new Date()): string {
  const ms = now.getTime() - toDate(d).getTime()
  const m = Math.round(ms / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}

export function fmtDuration(sec: number): string {
  if (!isNum(sec)) return DASH
  if (sec < 60) return `${Math.round(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`
  return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`
}

/** CSS class for directional colouring. Always pair with an arrow/sign — never colour alone. */
export function toneClass(v: number | null | undefined, invert = false): string {
  if (!isNum(v) || v === 0) return 'text-muted'
  return v > 0 !== invert ? 'text-up' : 'text-down'
}
