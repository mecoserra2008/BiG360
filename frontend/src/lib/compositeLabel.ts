/**
 * Coverage v3 A8: composite scores are DESCRIPTIVE. Out-of-sample tests show no predictive skill (Model Studio,
 * factor scores → 3M outperformance: AUC 0.49), so the UI calls them a descriptive, peer-relative ranking and never
 * a "signal", "predictive" or buy / sell score. That wording is unlocked only for a profile whose recorded
 * out-of-sample gate passed (GET /meta/factors composite_validation; job composite_validation) AND only for
 * composites the API confirms lie inside the population that gate tested (Company 360: overview composite.validated;
 * Screener: composite_label). Fail-closed: no answer from the API, an older API without the fields, a re-weighted
 * composite, a failed gate or a composite outside the tested population → descriptive.
 */
import type { CFactorMeta } from '@/api/contract'
import type { CompositeGateUniverseView, CompositeProfileValidation, CompositeValidationView } from '@/api/types'

export const DESCRIPTIVE_NOTE =
  'Descriptive: a rules-based, peer-relative ranking of the factor percentiles, not a forecast. No recorded out-of-sample test shows predictive power (Model Studio, factor scores → 3M outperformance: AUC 0.49).'

export const DESCRIPTIVE_VALIDATION: CompositeValidationView = {
  label: 'descriptive',
  defaultProfile: 'balanced',
  signalWording: false,
  note: DESCRIPTIVE_NOTE,
  evaluatedAt: null,
  profiles: {},
}

const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

type CUniverse = NonNullable<NonNullable<CFactorMeta['composite_validation']>['universe']>

function universeFromContract(u: CUniverse | null | undefined): CompositeGateUniverseView | null {
  if (!u || typeof u !== 'object') return null
  return {
    table: typeof u.table === 'string' ? u.table : null,
    scopes: Array.isArray(u.scopes) ? u.scopes.filter((x): x is 'mvp' | 'extended' => x === 'mvp' || x === 'extended') : [],
    regions: Array.isArray(u.regions) ? u.regions.filter((x): x is string => typeof x === 'string') : [],
    nSecurities: num(u.n_securities),
  }
}

/** GET /meta/factors composite_validation → view (missing / malformed → descriptive). */
export function compositeValidationFromContract(cv: CFactorMeta['composite_validation'] | null | undefined, defaultProfile = 'balanced'): CompositeValidationView {
  if (!cv || typeof cv !== 'object') return { ...DESCRIPTIVE_VALIDATION, defaultProfile }
  const profiles: Record<string, CompositeProfileValidation> = {}
  for (const [pid, p] of Object.entries(cv.profiles ?? {})) {
    const pass = p.verdict === 'pass' && p.label === 'validated'
    profiles[pid] = {
      label: pass ? 'validated' : 'descriptive',
      verdict: p.verdict === 'pass' || p.verdict === 'fail' ? p.verdict : 'not_validated',
      signalWording: pass && p.signal_wording === true,
      horizon: p.horizon ?? null,
      rankIcMean: num(p.rank_ic_mean),
      rankIcT: num(p.rank_ic_t),
      aucMean: num(p.auc_mean),
      aucT: num(p.auc_t),
      nDates: num(p.n_months) ?? num(p.n_dates),
      universe: universeFromContract(p.universe),
    }
  }
  const dp = cv.default_profile ?? defaultProfile
  const signal = cv.signal_wording === true && cv.label === 'validated' && profiles[dp]?.signalWording === true
  return {
    label: signal ? 'validated' : 'descriptive',
    defaultProfile: dp,
    signalWording: signal,
    note: signal ? cv.note : cv.label === 'descriptive' && cv.note ? cv.note : DESCRIPTIVE_NOTE,
    evaluatedAt: cv.evaluated_at ?? null,
    profiles,
  }
}

export interface CompositeWording {
  /** badge text */
  label: string
  /** "signal" / "predictive" / buy-sell wording allowed (passed out-of-sample gate) */
  signalAllowed: boolean
  tone: 'info' | 'accent'
  /** tooltip */
  title: string
  /** what to call the score in running text */
  noun: string
}

export interface CompositeWordingOpts {
  /** weight profile (default: the API's default profile) */
  profile?: string
  /** Screener re-weighting: never validated */
  custom?: boolean
  /**
   * The API's confirmation that THESE composites lie inside the population the gate tested (Company 360:
   * overview composite.validated; Screener: composite_label === 'validated'). Anything but `true` → descriptive.
   */
  validated?: boolean | null
  /** describes the profile's gate itself (System page), not a particular set of composites */
  profileLevel?: boolean
}

const f2 = (v: number | null) => (v === null ? '—' : v.toFixed(2))
const f3 = (v: number | null) => (v === null ? '—' : v.toFixed(3))

function universeText(u: CompositeGateUniverseView | null | undefined): string {
  if (!u) return 'the tested population'
  const scope = u.scopes.length ? `${u.scopes.join(' / ')} scope` : 'its scope'
  return `the ${u.nSecurities ?? '?'} securities it was tested on (${u.table ?? 'composite_scores'}, ${scope})`
}

/**
 * Wording for a composite under `profile`. The only path to `signalAllowed` is a passed gate for that exact profile
 * AND either the API's per-composite confirmation (`validated: true`) or a profile-level description (`profileLevel`).
 */
export function compositeWording(v: CompositeValidationView | null | undefined, opts: CompositeWordingOpts = {}): CompositeWording {
  const val = v ?? DESCRIPTIVE_VALIDATION
  const pid = opts.profile ?? val.defaultProfile
  const p = val.profiles[pid]
  const passed = !opts.custom && p?.signalWording === true && p.verdict === 'pass'
  if (passed && (opts.validated === true || opts.profileLevel === true)) {
    return {
      label: 'Validated signal',
      signalAllowed: true,
      tone: 'accent',
      noun: 'validated signal',
      title: `Out-of-sample gate passed (${p.horizon ?? '3M'}): rank IC ${f3(p.rankIcMean)} (t ${f2(p.rankIcT)}), AUC ${f3(p.aucMean)} (t ${f2(p.aucT)}) over ${p.nDates ?? '—'} months. Applies only to ${universeText(p.universe)}. Still a ranking, not an expected return.`,
    }
  }
  const why = opts.custom
    ? 'Re-weighted composite: never validated out of sample.'
    : passed
      ? `The gate passed for ${universeText(p.universe)}; these composites are not confirmed inside it.`
      : p?.verdict === 'fail'
        ? `Out-of-sample gate failed: rank IC ${f3(p.rankIcMean)} (t ${f2(p.rankIcT)}), AUC ${f3(p.aucMean)} (t ${f2(p.aucT)}).`
        : 'No passed out-of-sample gate on record.'
  return { label: 'Descriptive', signalAllowed: false, tone: 'info', noun: 'descriptive ranking', title: `${val.note} ${why}`.trim() }
}
