import { describe, expect, it } from 'vitest'
import { alertEventFromContract, alertFromContract, alertToContract } from '../../adapters/crossSection'
import type { CAlert, CAlertEvent } from '../../contract'
import { cellIndex, groupCalendar, nowcastTrack, orientedZ, recessionBands, recessionGrid, revisionOverlay, studioFrequency, surpriseBucket, zBucket } from '../adapters'
import type { MCalendarItem, MCell, MNowcastTarget, MRecession, MSeriesData } from '../types'

const cell = (p: Partial<MCell>): MCell => ({ country: 'US', indicator: 'cpi_yoy', series_id: 'fred:CPIAUCSL', units: '% y/y', transform: 'yoy', frequency: 'M', value: 3.4, ...p })

describe('heatmap orientation', () => {
  it('orients z so positive = favourable', () => {
    expect(orientedZ(cell({ z_10y: 1.2, good_direction: -1 }))).toBeCloseTo(-1.2) // high inflation is bad
    expect(orientedZ(cell({ z_10y: 1.2, good_direction: 1 }))).toBeCloseTo(1.2) // strong growth is good
    expect(orientedZ(cell({ z_10y: 1.2, good_direction: 0 }))).toBeNull() // neutral (rates): no judgement
    expect(orientedZ(cell({ z_10y: null, good_direction: 1 }))).toBeNull()
  })
  it('buckets oriented z into −3…3', () => {
    expect(zBucket(-5)).toBe(-3)
    expect(zBucket(0.1)).toBe(0)
    expect(zBucket(1)).toBe(2)
    expect(zBucket(null)).toBeNull()
    expect(surpriseBucket(-1.6)).toBe(-2)
    expect(surpriseBucket(undefined)).toBeNull()
  })
  it('indexes cells by country and indicator', () => {
    const idx = cellIndex({ cells: [cell({}), cell({ country: 'EA', indicator: 'gdp_yoy' })] })
    expect(idx.get('EA|gdp_yoy')?.country).toBe('EA')
    expect(idx.has('US|gdp_yoy')).toBe(false)
  })
})

const item = (p: Partial<MCalendarItem>): MCalendarItem => ({
  release_event_id: 'R',
  release_name: 'CPI',
  publisher: 'BLS',
  country: 'US',
  kind: 'data',
  scheduled_at: '2026-10-14T12:30:00+00:00',
  scheduled_local: '2026-10-14T08:30:00-04:00',
  scheduled_tz: 'America/New_York',
  release_date: '2026-10-14',
  time_known: true,
  schedule_source: 'official_calendar',
  importance: 1,
  status: 'upcoming',
  series: [],
  ...p,
})

describe('calendar grouping', () => {
  const items = [
    item({ release_event_id: 'a' }),
    item({ release_event_id: 'b', country: 'EA', importance: 2, scheduled_at: '2026-10-14T09:00:00+00:00' }),
    item({ release_event_id: 'c', importance: 3, release_date: '2026-10-15', scheduled_at: '2026-10-15T12:30:00+00:00' }),
    item({ release_event_id: 'd', release_date: '2026-10-01', scheduled_at: '2026-10-01T12:30:00+00:00', status: 'released' }),
    item({ release_event_id: 'e', kind: 'policy_meeting', release_date: '2026-10-16', scheduled_at: '2026-10-16T18:00:00+00:00' }),
  ]
  it('groups upcoming releases by day in time order, filtered by tier', () => {
    const days = groupCalendar(items, { maxTier: 2, when: 'upcoming', today: '2026-10-12' })
    expect(days.map((d) => [d.date, d.items.map((i) => i.release_event_id)])).toEqual([
      ['2026-10-14', ['b', 'a']],
      ['2026-10-16', ['e']],
    ])
  })
  it('filters country, kind and past (latest day first)', () => {
    expect(groupCalendar(items, { countries: ['EA'], today: '2026-10-12' }).flatMap((d) => d.items.map((i) => i.release_event_id))).toEqual(['b'])
    expect(groupCalendar(items, { when: 'past', today: '2026-10-12' }).map((d) => d.date)).toEqual(['2026-10-01'])
    expect(groupCalendar(items, { kinds: ['data'], today: '2026-10-12' }).flatMap((d) => d.items).some((i) => i.kind === 'policy_meeting')).toBe(false)
  })
})

describe('nowcast track record', () => {
  const t = (label: string, actual: number | null, ours: number, gdpnow?: number): MNowcastTarget => ({
    target_label: label,
    target_period: '2026-04-01',
    actual,
    models: [
      { model_id: 'dfm_mq_us_v1', label: 'ours', model_based: true, official: false, latest: { as_of: '2026-07-20', nowcast: ours }, history: [] },
      ...(gdpnow !== undefined ? [{ model_id: 'gdpnow_atlanta_fed', label: 'GDPNow', model_based: false, official: true, latest: null, history: [{ as_of: '2026-07-25', nowcast: gdpnow }] }] : []),
    ],
  })
  it('computes errors against the first print; pending targets have no error', () => {
    const rows = nowcastTrack([t('2026Q2', 1.5, 2.0, 1.2), t('2026Q3', null, 2.6)])
    expect(rows[0]).toMatchObject({ target: '2026Q2', actual: 1.5, ours: 2.0, gdpnow: 1.2, nyfed: null })
    expect(rows[0].errOurs).toBeCloseTo(0.5)
    expect(rows[0].errGdpnow).toBeCloseTo(-0.3)
    expect(rows[1]).toMatchObject({ actual: null, errOurs: null })
  })
})

describe('recession & revisions', () => {
  it('pivots the recession panel by country and method', () => {
    const r = {
      as_of: '2026-10-09',
      methods: [{ method: 'sahm_realtime', label: 'Sahm', official: true }],
      countries: [{ country: 'US', rows: [{ method: 'sahm_realtime', country: 'US', date: '2026-09-01', value: 0.1, method_label: 'Sahm rule', official: true }] }],
    } as unknown as MRecession
    const g = recessionGrid(r)
    expect(g.countries).toEqual(['US'])
    expect(g.get('US', 'sahm_realtime')?.value).toBe(0.1)
    expect(g.get('EA', 'sahm_realtime')).toBeUndefined()
  })
  it('turns a 0/1 indicator into shading bands', () => {
    const pts = ['2019-12', '2020-02', '2020-03', '2020-04', '2020-05'].map((d, i) => ({ date: `${d}-01`, value: [0, 1, 1, 0, 1][i] }))
    expect(recessionBands(pts)).toEqual([
      { start: '2020-02-01', end: '2020-03-01' },
      { start: '2020-05-01', end: '2020-05-01' },
    ])
  })
  it('aligns first release with the latest revision (transformed when available)', () => {
    const v = {
      kind: 'alfred',
      transform: 'yoy',
      first_release: [
        { date: '2026-06-01', value: 330, value_tf: 3.0 },
        { date: '2026-07-01', value: 332, value_tf: 3.1 },
      ],
      latest: [
        { date: '2026-06-01', value: 331, value_tf: 3.2 },
        { date: '2026-07-01', value: 332, value_tf: 3.1 },
      ],
    } as unknown as MSeriesData['vintages']
    expect(revisionOverlay(v)).toEqual({ dates: ['2026-06-01', '2026-07-01'], first: [3.0, 3.1], latest: [3.2, 3.1], transformed: true })
    expect(revisionOverlay(v, '2026-07-01')?.dates).toEqual(['2026-07-01'])
    expect(revisionOverlay(null)).toBeNull()
  })
  it('maps macro frequencies onto Studio dataset frequencies', () => {
    expect([studioFrequency('A'), studioFrequency('Q'), studioFrequency('D'), studioFrequency('W')]).toEqual(['Q', 'Q', 'M', 'W'])
  })
})

describe('macro alert rules ↔ contract', () => {
  it('creates country-level rules without a security target', () => {
    const body = alertToContract({ companyId: '', ticker: 'US', metric: 'macroSurprise', operator: '>', threshold: 2, enabled: true, macroParams: { countries: ['US'], threshold_z: 2, max_importance: 2 } })
    expect(body).toMatchObject({ security_id: null, rule_type: 'macro_surprise', params: { countries: ['US'], threshold_z: 2, max_importance: 2, ui_metric: 'macroSurprise' } })
  })
  it('reads them back', () => {
    const a = {
      alert_id: 'A1',
      security_id: null,
      watchlist_id: null,
      rule_type: 'regime_change',
      params: { countries: ['EA'], kind: 'recession_probability', level: 0.4 },
      cooldown_days: 1,
      enabled: true,
      created_at: '2026-10-12T00:00:00Z',
    } as unknown as CAlert
    expect(alertFromContract(a)).toMatchObject({ metric: 'regimeChange', ticker: 'EA', threshold: 0.4, macroParams: { countries: ['EA'], kind: 'recession_probability', level: 0.4 } })
  })
  it('uses AlertEvent.target (v1.11) for macro events', () => {
    const e = { event_id: 'E1', alert_id: 'A1', security_id: 'macro:US', target: 'macro:US', fired_at: '2026-10-12T21:00:00Z', as_of_date: '2026-10-12', message: 'US CPI surprise +2.3σ', read: false } as CAlertEvent
    expect(alertEventFromContract(e)).toMatchObject({ target: 'macro:US', message: 'US CPI surprise +2.3σ', read: false })
    expect(alertEventFromContract({ ...e, target: null, security_id: 'SEC1' }).target).toBe('SEC1')
  })
})
