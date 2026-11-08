import { describe, expect, it } from 'vitest'
import { parseInline, parseMarkdown } from '@/lib/markdown'
import {
  chartBands,
  daysUntil,
  draftFromParams,
  draftFromThesis,
  draftToCreate,
  draftToUpdate,
  emptyDraft,
  groupBoard,
  kpiCondition,
  kpiState,
  moves,
  newThesisHref,
  placeMarkers,
  ruleLabel,
  validateDraft,
} from '../adapters'
import type { Thesis } from '../types'

const th = (p: Partial<Thesis>): Thesis => ({
  thesis_id: 'TH1',
  title: 'T',
  subject_type: 'security',
  subject_id: 'S1',
  direction: 'long',
  horizon: '12M',
  conviction: 3,
  status: 'idea',
  key_drivers: [],
  catalysts: [],
  kpis: [],
  invalidation: [],
  tags: [],
  auto_status: 'on_track',
  status_reasons: [],
  created_at: '2026-10-01T00:00:00+00:00',
  updated_at: '2026-10-01T00:00:00+00:00',
  ...p,
})

describe('board', () => {
  it('groups by column, riskiest first, closed by close date', () => {
    const g = groupBoard([
      th({ thesis_id: 'a', status: 'active', auto_status: 'on_track', updated_at: '2026-10-05T00:00:00+00:00' }),
      th({ thesis_id: 'b', status: 'active', auto_status: 'invalidated' }),
      th({ thesis_id: 'c', status: 'active', auto_status: 'at_risk' }),
      th({ thesis_id: 'd', status: 'closed', closed_at: '2026-09-01T00:00:00+00:00' }),
      th({ thesis_id: 'e', status: 'closed', closed_at: '2026-10-01T00:00:00+00:00' }),
      th({ thesis_id: 'f' }),
    ])
    expect(g.active.map((t) => t.thesis_id)).toEqual(['b', 'c', 'a'])
    expect(g.closed.map((t) => t.thesis_id)).toEqual(['e', 'd'])
    expect(g.idea).toHaveLength(1)
  })
  it('allows only lifecycle moves', () => {
    expect(moves('idea')).toEqual(['active', 'closed'])
    expect(moves('closed')).toEqual(['active'])
  })
})

describe('chart helpers', () => {
  it('lists only the bands that are set', () => {
    expect(chartBands({ target: 130, stop: 80, entry_lo: null, inception_price: 100 }).map((b) => b.key)).toEqual(['target', 'stop', 'inception'])
  })
  it('snaps markers to the next bar and stacks same-day markers', () => {
    const bars = [
      { date: '2026-10-01', close: 10 },
      { date: '2026-10-02', close: 11 },
      { date: '2026-10-05', close: 12 },
    ]
    const p = placeMarkers(
      [
        { date: '2026-10-03', kind: 'journal', label: 'weekend note' },
        { date: '2026-10-05', kind: 'journal', label: 'monday note' },
        { date: '2026-09-01', kind: 'alert', label: 'before range' },
        { date: '2026-12-01', kind: 'catalyst', label: 'future' },
        { date: '2026-10-02', kind: 'alert', label: 'stop' },
      ],
      bars,
    )
    expect(p).toEqual([
      { date: '2026-10-02', value: 11, kind: 'alert', labels: ['stop'] },
      { date: '2026-10-05', value: 12, kind: 'journal', labels: ['weekend note', 'monday note'] },
    ])
    expect(placeMarkers([{ date: '2026-10-01', kind: 'journal', label: 'x' }], [])).toEqual([])
  })
})

describe('labels', () => {
  it('formats KPI conditions and states', () => {
    expect(kpiCondition({ op: 'gte', threshold: 0.1, threshold_hi: null })).toBe('≥ 0.1')
    expect(kpiCondition({ op: 'between', threshold: 1, threshold_hi: 2 })).toBe('between 1 and 2')
    expect(kpiState(false, 'invalidate')).toEqual({ label: 'Invalidates', tone: 'down' })
    expect(kpiState(false, 'confirm').tone).toBe('warn')
    expect(kpiState(null, 'confirm').label).toBe('No data')
  })
  it('describes linked alert rules', () => {
    expect(ruleLabel({ rule_type: 'price_cross', params: { level: 130, direction: 'above', band: 'target' } })).toBe('Target: crosses above 130')
    expect(ruleLabel({ rule_type: 'thesis_kpi', params: { label: 'ROIC' } })).toBe('KPI threshold: ROIC')
    expect(ruleLabel({ rule_type: 'macro_surprise', params: { countries: ['DE'] } })).toBe('Macro surprise (model-based): DE')
  })
  it('counts days until a date', () => {
    expect(daysUntil('2026-10-11', new Date('2026-10-09T12:00:00Z'))).toBe(2)
    expect(daysUntil(null)).toBeNull()
  })
})

describe('editor drafts', () => {
  it('builds a create body (empty overrides -> server defaults)', () => {
    const d = { ...emptyDraft(), title: ' Long ACME ', subjectId: 'ACME', entryLo: '95', entryHi: '1,05e2', target: '130', stop: '', drivers: '- pricing\n\n* margins', tags: 'Quality; us', reviewEveryDays: '' }
    const b = draftToCreate(d)
    expect(b.title).toBe('Long ACME')
    expect(b.entry_hi).toBe(105)
    expect(b.stop).toBeNull()
    expect(b.key_drivers).toEqual(['pricing', 'margins'])
    expect(b.tags).toEqual(['Quality', 'us'])
    expect(b.review_every_days).toBeNull()
    expect('price_id' in b).toBe(false)
    expect(b.origin).toEqual({ kind: 'manual', ref: null })
  })
  it('round-trips a thesis and clears overrides on update', () => {
    const t = th({ entry_lo: 1, target: 2, price_id: 'S1', tags: ['a', 'b'], review_every_days: 30, key_drivers: ['x'] })
    const d = draftFromThesis(t)
    expect(d.entryLo).toBe('1')
    expect(d.tags).toBe('a, b')
    const u = draftToUpdate({ ...d, priceId: '' })
    expect(u.price_id).toBeNull()
    expect(u.review_every_days).toBe(30)
  })
  it('validates bands by direction and KPIs', () => {
    const base = { ...emptyDraft(), title: 'x', subjectId: 'S' }
    expect(validateDraft(base)).toEqual([])
    expect(validateDraft({ ...base, target: '80', stop: '90' })).toContain('A long thesis needs the target above the stop')
    expect(validateDraft({ ...base, direction: 'short', target: '100', stop: '90' })).toContain('A short thesis needs the target below the stop')
    expect(validateDraft({ ...base, kpis: [{ label: 'k', kind: 'expr', expr: '', op: 'gte', threshold: 1 }] })[0]).toContain('expression is required')
  })
  it('creates links for entry points and parses them back', () => {
    const href = newThesisHref({ subjectType: 'cross_asset', subjectId: 'CL', origin: 'cross_asset', ref: 'CL', backtestRunId: '' })
    expect(href).toBe('/theses/new?subject_type=cross_asset&subject_id=CL&origin=cross_asset&ref=CL')
    const d = draftFromParams(new URLSearchParams(href.split('?')[1] + '&studio_run_id=R1&direction=short&subject_type_x=1'))
    expect(d.subjectType).toBe('cross_asset')
    expect(d.originKind).toBe('cross_asset')
    expect(d.studioRunId).toBe('R1')
    expect(d.direction).toBe('short')
    expect(draftFromParams(new URLSearchParams('subject_type=bogus&origin=evil')).subjectType).toBe('security')
  })
})

describe('markdown', () => {
  it('parses blocks', () => {
    const b = parseMarkdown('# Title\n\nPara one\nline two\n\n- a\n- **b**\n\n1. x\n2. y\n\n> quote\n\n```\ncode\n```\n---')
    expect(b.map((x) => x.t)).toEqual(['h', 'p', 'ul', 'ol', 'quote', 'pre', 'hr'])
    expect(b[1]).toEqual({ t: 'p', inl: [{ t: 'text', v: 'Para one line two' }] })
  })
  it('keeps only safe links and parses inline marks', () => {
    expect(parseInline('see [doc](https://x.org) and [bad](javascript:alert(1))')).toEqual([
      { t: 'text', v: 'see ' },
      { t: 'a', v: 'doc', href: 'https://x.org' },
      { t: 'text', v: ' and ' },
      { t: 'text', v: 'bad' },
      { t: 'text', v: ')' },
    ])
    expect(parseInline('**b** *i* `c`').filter((x) => x.t !== 'text').map((x) => x.t)).toEqual(['b', 'i', 'code'])
  })
})
