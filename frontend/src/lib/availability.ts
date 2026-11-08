/**
 * Data-availability reasons (completeness audit, docs/DATA_COMPLETENESS.md; backend app.core.availability).
 * Every empty value in the UI can say WHY it is empty: not published yet, no free source for the market, an API key
 * missing, a pending refresh, not applicable ... Pure helpers (no React) so they are unit-testable.
 */

export type AvailabilityCode =
  | 'fresh'
  | 'due'
  | 'not_yet_published'
  | 'source_unavailable'
  | 'no_source'
  | 'needs_key'
  | 'failed'
  | 'mapping_gap'
  | 'pipeline_bug'
  | 'stale'
  | 'not_applicable'
  | 'out_of_scope'
  | 'suppressed'
  | 'insufficient_data'

export interface AvailabilityReason {
  code: AvailabilityCode
  sync_code?: string
  label: string
  message: string
  next_expected?: string | null
  detail?: string | null
  field?: string | null
  has_value?: boolean | null
}

/** GET /meta/field-availability entry */
export interface FieldAvailabilityEntry {
  group?: string | null
  reason?: AvailabilityReason | null
  regions?: Record<string, AvailabilityReason | null>
}

export interface FieldAvailabilityCatalog {
  version: number
  generated_at?: string | null
  serving_run_id?: string | null
  areas?: Record<string, AvailabilityReason>
  /** alias / screener key -> BQL mnemonic (pe_ntm -> pe_fwd) */
  aliases?: Record<string, string>
  fields: Record<string, FieldAvailabilityEntry>
}

export const CODE_LABEL: Record<AvailabilityCode, string> = {
  fresh: 'Up to date',
  due: 'Update due',
  not_yet_published: 'Not published yet',
  source_unavailable: 'Source unavailable',
  no_source: 'No source',
  needs_key: 'Needs API key',
  failed: 'Update failed',
  mapping_gap: 'Not matched',
  pipeline_bug: 'Known data gap',
  stale: 'Pending refresh',
  not_applicable: 'Not applicable',
  out_of_scope: 'Not covered',
  suppressed: 'Withheld',
  insufficient_data: 'Insufficient data',
}

/** Tone of the reason (tooltip accent): grey = by design, amber = will fill in / fixable, red = broken. */
export function reasonTone(code: AvailabilityCode): 'neutral' | 'warn' | 'down' | 'info' {
  switch (code) {
    case 'not_yet_published':
    case 'due':
    case 'stale':
      return 'info'
    case 'needs_key':
    case 'out_of_scope':
    case 'insufficient_data':
    case 'mapping_gap':
    case 'source_unavailable':
      return 'warn'
    case 'failed':
    case 'pipeline_bug':
      return 'down'
    default:
      return 'neutral'
  }
}

export const GENERIC_REASON: AvailabilityReason = {
  code: 'not_applicable',
  label: CODE_LABEL.not_applicable,
  message: 'Not available for this company (not reported, or not applicable)',
}

export function makeReason(code: AvailabilityCode, message?: string, nextExpected?: string | null): AvailabilityReason {
  return { code, label: CODE_LABEL[code], message: message ?? CODE_LABEL[code], next_expected: nextExpected ?? null }
}

/** "Not published yet: reports on 2026-11-05" for a dated future event (earnings release, macro release). */
export function notPublishedUntil(date: string | null | undefined, what = 'Not published yet'): AvailabilityReason {
  if (!date) return makeReason('not_yet_published', `${what}: no date announced`)
  return makeReason('not_yet_published', `${what}: next release ${date.slice(0, 10)}`, date)
}

/** One-line tooltip text: "<label>: <message>" (no duplicated label) + next expected date when not in the text. */
export function reasonText(r: AvailabilityReason | null | undefined): string {
  if (!r) return GENERIC_REASON.message
  const label = r.label || CODE_LABEL[r.code] || ''
  const msg = (r.message || '').trim()
  // no "label: " prefix when the message already says it ("Needs OPENDART_APIKEY", "No free source ...")
  const first = (x: string) => x.split(/[\s:]/)[0].toLowerCase()
  let out = !msg ? label : first(msg) === first(label) ? msg : `${label}: ${msg}`
  if (r.next_expected && !out.includes(r.next_expected.slice(0, 10))) out += ` (next expected ${r.next_expected.slice(0, 10)})`
  return out
}

/**
 * The reason for one empty value, most specific first: an explicit reason, the security's own reason
 * (GET /companies/{id}/availability), the field's reason in the security's region, the field's main reason
 * (field-availability catalog), else the generic "not reported / not applicable".
 */
export function resolveReason(opts: {
  reason?: AvailabilityReason | string | null
  field?: string | null
  region?: string | null
  entity?: Record<string, AvailabilityReason> | null
  catalog?: FieldAvailabilityCatalog | null
}): AvailabilityReason {
  const { reason, field, region, entity, catalog } = opts
  if (reason && typeof reason === 'object') return reason
  if (typeof reason === 'string' && reason) return { ...GENERIC_REASON, message: reason }
  if (field) {
    const key = catalog?.aliases?.[field] ?? field
    const own = entity?.[field] ?? entity?.[key]
    if (own && !own.has_value) return own
    const e = catalog?.fields?.[key] ?? (catalog?.areas?.[key] ? { reason: catalog.areas[key] } : undefined)
    if (e) {
      if (region && e.regions) {
        const rr = e.regions[region]
        if (rr) return rr
        // the field has gaps elsewhere but not in this region: an entity-level gap
        if (Object.keys(e.regions).length) return GENERIC_REASON
      }
      if (e.reason) return e.reason
    }
  }
  return GENERIC_REASON
}

/**
 * True only when the catalog / the security documents that the data does not apply here (code not_applicable from
 * the field's region rule or the security's own reason) - never for the generic fallback, which only means "no
 * documented reason". Such a panel shows a neutral "not applicable" note instead of an empty-data state
 * (alert hygiene 2026-10-30: FINRA short interest on a Tokyo listing is not missing data).
 */
export function documentedNotApplicable(r: AvailabilityReason | null | undefined): boolean {
  return Boolean(r) && r !== GENERIC_REASON && r?.code === 'not_applicable'
}

/** UI region chip (CNHK / OTHER merge contract regions) -> contract region of the availability catalog. */
export function contractRegion(region: string | null | undefined, country?: string | null): string | null {
  if (!region) return null
  if (region === 'CNHK') return country === 'HK' ? 'HK' : 'CN'
  if (region === 'OTHER') return country === 'CA' ? 'CA' : 'APAC'
  return region
}

/** Why a calendar event has no reported actuals: the release date is ahead, or it passed and the results are not
 *  ingested yet; null = use the field / region reason. */
export function releaseReason(item: { date?: string | null; status?: string | null }, asOf: string, what = 'Reports on'): AvailabilityReason | null {
  const d = item.date?.slice(0, 10)
  if (!d) return null
  if (item.status === 'upcoming' && d >= asOf) return makeReason('not_yet_published', `Not published yet: ${what.toLowerCase()} ${d}`, d)
  if (item.status === 'upcoming' && d < asOf) return makeReason('due', `Release date ${d} passed; the results are not ingested yet (awaiting the filing)`)
  return null
}

/** Macro tile / cell without a value (or a stale one): official-series status -> reason. */
export function macroCellReason(c: { status?: string | null; period_label?: string | null; age_days?: number | null; release_date?: string | null }): AvailabilityReason {
  switch (c.status) {
    case 'discontinued':
      return makeReason('no_source', 'Series discontinued by the official source; no successor series wired yet')
    case 'empty':
      return makeReason('no_source', 'No official observation for this country / indicator')
    case 'stale':
      return makeReason('stale', `Last observation ${c.period_label ?? ''}${c.age_days != null ? ` (${c.age_days} days old)` : ''}; the next official release is pending`.replace('  ', ' '))
    default:
      return makeReason('not_yet_published', 'No observation published yet for this period')
  }
}

/** Cross-asset cockpit cell without a value. */
export function crossAssetCellReason(signal: string, hasPositioning: boolean): AvailabilityReason {
  if (signal === 'positioning' && !hasPositioning) return makeReason('no_source', 'No CFTC futures market for this asset')
  if (signal === 'carry') return makeReason('not_applicable', 'No carry definition for this asset (no futures curve or rate differential)')
  if (signal === 'value') return makeReason('not_applicable', 'No value proxy for this asset class')
  if (signal === 'vol' || signal === 'volatility') return makeReason('no_source', 'No implied-volatility index for this asset; realised volatility needs more history')
  return makeReason('stale', 'Not enough recent history to compute this signal')
}
