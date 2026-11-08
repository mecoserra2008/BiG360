import { describe, expect, it } from 'vitest'
import {
  CODE_LABEL,
  GENERIC_REASON,
  contractRegion,
  crossAssetCellReason,
  documentedNotApplicable,
  macroCellReason,
  makeReason,
  notPublishedUntil,
  reasonText,
  reasonTone,
  releaseReason,
  resolveReason,
  type FieldAvailabilityCatalog,
} from '../availability'

const catalog: FieldAvailabilityCatalog = {
  version: 1,
  aliases: { pe_ntm: 'pe_fwd' },
  areas: { 'cross_asset.positioning': makeReason('no_source', 'No CFTC futures market for this asset') },
  fields: {
    roic: {
      group: 'fundamental',
      reason: makeReason('out_of_scope', 'Computed for the analysis universe'),
      regions: { KR: makeReason('needs_key', 'Needs OPENDART_APIKEY (Korea DART financial statements)'), HK: makeReason('no_source', 'HKEX filings are PDF only') },
    },
    pe_fwd: { group: 'valuation', reason: makeReason('no_source', 'No consensus / forecast for this market') },
  },
}

describe('availability reasons', () => {
  it('resolves explicit > entity > region > field > generic', () => {
    const explicit = notPublishedUntil('2026-11-05')
    expect(resolveReason({ reason: explicit, field: 'roic', catalog })).toBe(explicit)
    const own = { roic: { ...makeReason('suppressed', 'Withheld: outside plausibility bounds'), has_value: false } }
    expect(resolveReason({ field: 'roic', region: 'KR', entity: own, catalog }).code).toBe('suppressed')
    expect(resolveReason({ field: 'roic', region: 'KR', catalog }).code).toBe('needs_key')
    expect(resolveReason({ field: 'roic', region: 'HK', catalog }).code).toBe('no_source')
    // the field has region gaps, but not in the US: an entity-level gap -> generic text
    expect(resolveReason({ field: 'roic', region: 'US', catalog })).toBe(GENERIC_REASON)
    expect(resolveReason({ field: 'roic', catalog }).code).toBe('out_of_scope')
    expect(resolveReason({ field: 'pe_ntm', region: 'JP', catalog }).code).toBe('no_source') // alias -> pe_fwd
    expect(resolveReason({ field: 'cross_asset.positioning', catalog }).code).toBe('no_source')
    expect(resolveReason({ field: 'unknown', catalog })).toBe(GENERIC_REASON)
    expect(resolveReason({ reason: 'Custom text' }).message).toBe('Custom text')
  })

  it('formats the tooltip text without repeating the label', () => {
    expect(reasonText(notPublishedUntil('2026-11-05'))).toBe('Not published yet: next release 2026-11-05')
    expect(reasonText(makeReason('needs_key', 'Needs OPENDART_APIKEY'))).toBe('Needs OPENDART_APIKEY')
    expect(reasonText(makeReason('no_source', 'No free source for this market'))).toBe('No free source for this market')
    expect(reasonText(makeReason('out_of_scope', 'Computed for the analysis universe only'))).toBe('Not covered: Computed for the analysis universe only')
    expect(reasonText({ ...makeReason('stale', 'Pending data refresh'), next_expected: '2026-10-28' })).toBe('Pending data refresh (next expected 2026-10-28)')
    expect(reasonText(null)).toBe(GENERIC_REASON.message)
  })

  it('maps every code to a label and a tone', () => {
    for (const c of Object.keys(CODE_LABEL) as (keyof typeof CODE_LABEL)[]) {
      expect(CODE_LABEL[c]).toBeTruthy()
      expect(['neutral', 'warn', 'down', 'info']).toContain(reasonTone(c))
    }
    expect(reasonTone('pipeline_bug')).toBe('down')
    expect(reasonTone('not_yet_published')).toBe('info')
  })

  it('explains earnings, macro and cross-asset gaps', () => {
    expect(releaseReason({ date: '2026-11-05', status: 'upcoming' }, '2026-10-27')?.code).toBe('not_yet_published')
    expect(releaseReason({ date: '2026-10-20', status: 'upcoming' }, '2026-10-27')?.code).toBe('due')
    expect(releaseReason({ date: '2026-10-20', status: 'reported' }, '2026-10-27')).toBeNull()
    expect(macroCellReason({ status: 'discontinued' }).code).toBe('no_source')
    expect(macroCellReason({ status: 'stale', period_label: 'Aug 2026', age_days: 70 }).message).toContain('70 days old')
    expect(crossAssetCellReason('positioning', false).message).toContain('CFTC')
    expect(crossAssetCellReason('carry', true).code).toBe('not_applicable')
  })

  it('maps UI region chips to contract regions', () => {
    expect(contractRegion('CNHK', 'HK')).toBe('HK')
    expect(contractRegion('CNHK', 'CN')).toBe('CN')
    expect(contractRegion('OTHER', 'CA')).toBe('CA')
    expect(contractRegion('OTHER', 'AU')).toBe('APAC')
    expect(contractRegion('US')).toBe('US')
    expect(contractRegion(null)).toBeNull()
  })
  it('tells a documented "not applicable" from the generic fallback (Ownership tab of a non-US listing)', () => {
    const cat: FieldAvailabilityCatalog = {
      version: 1,
      fields: { 'si.days': { reason: makeReason('out_of_scope', 'US only'), regions: { JP: makeReason('not_applicable', 'US-listed securities only (FINRA short interest)') } } },
    }
    expect(documentedNotApplicable(resolveReason({ field: 'si.days', region: 'JP', catalog: cat }))).toBe(true)
    expect(documentedNotApplicable(resolveReason({ field: 'si.days', region: 'US', catalog: cat }))).toBe(false) // US: a real gap
    expect(documentedNotApplicable(resolveReason({ field: 'si.days', region: 'JP', catalog: null }))).toBe(false) // no catalog yet
    expect(documentedNotApplicable(GENERIC_REASON)).toBe(false)
    expect(documentedNotApplicable(null)).toBe(false)
  })
})
