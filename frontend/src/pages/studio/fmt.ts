/** Formatting helpers for model output (statistics span many magnitudes, so use significant digits). */
import { DASH, fmtDate, fmtInt, getFormatLocale } from '@/lib/format'

const MINUS = '−'
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** 4 significant digits; exponent for very small / very large magnitudes; integers stay integers. */
export function fmtStat(v: unknown, sig = 4): string {
  const x = num(v)
  if (x === null) return DASH
  if (x === 0) return '0'
  const a = Math.abs(x)
  if (Number.isInteger(x) && a < 1e9) return fmtInt(x)
  if (a < 1e-4 || a >= 1e9) return x.toExponential(2).replace('-', MINUS).replace('e+', 'e')
  const digits = Math.max(0, Math.min(6, sig - 1 - Math.floor(Math.log10(a))))
  return new Intl.NumberFormat(getFormatLocale(), { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(x).replace('-', MINUS)
}

export function fmtP(v: unknown): string {
  const x = num(v)
  if (x === null) return DASH
  if (x < 0.001) return '<0.001'
  return x.toFixed(3)
}

/** Stars for p-values: *** 1%, ** 5%, * 10%. */
export function stars(v: unknown): string {
  const x = num(v)
  if (x === null) return ''
  return x < 0.01 ? '***' : x < 0.05 ? '**' : x < 0.1 ? '*' : ''
}

export const isDateLike = (v: unknown): boolean => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}/.test(v)

/** Any table / summary cell → display string. */
export function fmtCell(v: unknown): string {
  if (v === null || v === undefined) return DASH
  if (typeof v === 'number') return fmtStat(v)
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v === 'string') return isDateLike(v) && v.length > 10 ? fmtDate(v.slice(0, 10)) : v || DASH
  if (Array.isArray(v)) return v.length <= 8 ? `(${v.map((x) => (typeof x === 'number' ? fmtStat(x) : String(x))).join(', ')})` : `${v.length} items`
  if (typeof v === 'object') {
    const e = Object.entries(v as Record<string, unknown>)
    return e.length <= 4 ? e.map(([k, x]) => `${k}: ${fmtCell(x)}`).join(' · ') : `${e.length} entries`
  }
  return String(v)
}

/** snake_case key → label ("adj_r2" → "Adj R²"). */
export function labelOf(key: string): string {
  const special: Record<string, string> = {
    r2: 'R²',
    adj_r2: 'Adj. R²',
    aic: 'AIC',
    bic: 'BIC',
    hqic: 'HQIC',
    aicc: 'AICc',
    llf: 'Log-likelihood',
    f_stat: 'F-statistic',
    f_pvalue: 'Prob (F)',
    n_obs: 'Observations',
    df_resid: 'Df residuals',
    sigma: 'Residual std. error',
    sigma2: 'σ²',
    auc: 'AUC',
    rmse_in_sample: 'RMSE (in-sample)',
    oos_rmse: 'OOS RMSE',
    oos_mae: 'OOS MAE',
    oos_mape: 'OOS MAPE %',
    oos_rmse_naive: 'OOS RMSE naive',
    oos_relative_rmse: 'OOS RMSE / naive',
    oos_r2: 'OOS R² vs mean',
    pseudo_r2: 'Pseudo R²',
    pseudo_r2_mcfadden: 'Pseudo R² (McFadden)',
    pseudo_r2_cs: 'Pseudo R² (Cox–Snell)',
    cov_type: 'Covariance',
    pc1_share: 'PC1 share',
  }
  if (special[key]) return special[key]
  const s = key.replace(/_/g, ' ')
  return s.charAt(0).toUpperCase() + s.slice(1)
}

export function fmtSeconds(s: number | null | undefined): string {
  const x = num(s)
  if (x === null) return DASH
  if (x < 1) return `${Math.round(x * 1000)} ms`
  if (x < 90) return `${x.toFixed(1)} s`
  return `${Math.floor(x / 60)} m ${Math.round(x % 60)} s`
}

/** "Oct 11 22:54" (local time) for run lists. */
export function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return DASH
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return DASH
  return new Intl.DateTimeFormat(getFormatLocale(), { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(d)
}
