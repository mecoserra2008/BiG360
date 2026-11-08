import { describe, expect, it } from 'vitest'
import { type FeedItem, type LiveFiling, type LiveMacro, type LiveStreamEvent, feedFromAlert, feedFromFiling, feedFromMacro, feedFromStream, mergeFeed, streamTone, streamUrl } from '../types'

const filing = {
  live_id: 6567,
  origin: 'live',
  filing_id: 'hkex:news:12370564',
  source_id: 'hkex',
  source_entity_id: '01999',
  issuer_id: 'IH9',
  security_id: 'SM0Y',
  ticker: '1999.HK',
  company: 'MAN WAH HLDGS',
  form_type: 'ANNOUNCEMENT',
  form_class: 'current_report',
  title: 'INSIDE INFORMATION PROFIT WARNING',
  items: [],
  url: 'https://example.org/a.pdf',
  country: 'HK',
  reporting_owner: null,
  source_ts: '2026-10-13T00:00:00Z',
  knowledge_date: '2026-10-13',
  first_seen_at: '2026-10-13T00:01:30Z',
} as unknown as LiveFiling
const macro = {
  live_id: null,
  origin: 'snapshot',
  release_event_id: 'RE6a',
  series_id: 'oecd:CLI.MEX.LI',
  country: 'MX',
  release_name: 'Mexico OECD CLI',
  actual: 102.3023,
  expectation: 102.2552,
  signed_z: 1.153,
  surprise_z: 1.153,
  scheduled_at: '2026-10-11T14:00:00Z',
  captured_at: null,
  model_based: true,
} as unknown as LiveMacro

describe('feed adapters', () => {
  it('maps a live filing to a Company 360 link', () => {
    const f = feedFromFiling(filing)
    expect(f).toMatchObject({
      uid: 'filings:hkex:news:12370564',
      busId: 6567,
      topic: 'filings',
      title: '1999.HK · ANNOUNCEMENT',
      detail: 'INSIDE INFORMATION PROFIT WARNING',
      link: '/company/SM0Y',
      securityId: 'SM0Y',
      ts: '2026-10-13T00:00:00Z',
      origin: 'live',
    })
  })
  it('labels macro surprises as model-based (never consensus)', () => {
    const m = feedFromMacro(macro)
    expect(m.modelBased).toBe(true)
    expect(m.detail).toBe('actual 102.3 vs model 102.3 · surprise z +1.2')
    expect(m.detail).not.toMatch(/consensus/i)
    expect(m.tone).toBe(1)
    expect(m.link).toBe('/macro/country/MX')
    expect(m.ts).toBe('2026-10-11T14:00:00Z')
  })
  it('routes macro alerts to the country page', () => {
    const a = feedFromAlert({
      live_id: 9,
      origin: 'live',
      event_id: 'E1',
      alert_id: 'A1',
      security_id: 'macro:US',
      rule_type: 'macro_surprise',
      fired_at: '2026-10-13T12:30:00Z',
      message: 'US CPI surprise z 2.4',
      live: true,
    } as never)
    expect(a).toMatchObject({
      uid: 'alerts:E1',
      link: '/macro/country/US',
      securityId: null,
      title: 'US CPI surprise z 2.4',
      modelBased: true,
    })
  })
  it('turns an SSE frame into a feed item keyed by the bus key', () => {
    const ev: LiveStreamEvent = {
      id: 7000,
      topic: 'filings',
      type: 'filing',
      key: 'edgar:0001',
      ts_source: '2026-10-13T01:00:00Z',
      ts_ingest: '2026-10-13T01:00:05Z',
      sent_at: '2026-10-13T01:00:06Z',
      item: filing as unknown as Record<string, unknown>,
    }
    expect(feedFromStream(ev)).toMatchObject({
      uid: 'filings:edgar:0001',
      busId: 7000,
      origin: 'stream',
    })
    expect(feedFromStream({ ...ev, topic: 'signals' })).toBeNull()
  })
})

describe('feed merge + stream url', () => {
  const it0 = (uid: string, ts: string, busId: number | null = null): FeedItem => ({
    uid,
    busId,
    topic: 'news',
    type: 'news',
    ts,
    title: uid,
    detail: null,
    href: null,
    link: null,
    securityId: null,
    ticker: null,
    tone: 0,
    modelBased: false,
    origin: 'live',
  })
  it('dedupes by uid (newest copy wins), sorts newest first and bounds the list', () => {
    const prev = [it0('a', '2026-10-13T01:00:00Z'), it0('b', '2026-10-13T02:00:00Z')]
    const out = mergeFeed(prev, [{ ...it0('a', '2026-10-13T01:00:00Z'), origin: 'stream' }, it0('c', '2026-10-13T03:00:00Z')], 2)
    expect(out.map((x) => x.uid)).toEqual(['c', 'b'])
    expect(mergeFeed(prev, [{ ...it0('a', '2026-10-13T01:00:00Z'), origin: 'stream' }]).find((x) => x.uid === 'a')?.origin).toBe('stream')
  })
  it('builds the resume URL with last_event_id', () => {
    expect(streamUrl('http://h/api/v1', ['filings', 'macro'], null)).toBe('http://h/api/v1/live/stream?topics=filings%2Cmacro&heartbeat_s=15')
    expect(streamUrl('http://h/api/v1', ['alerts'], 6583)).toContain('last_event_id=6583')
  })
  it('maps stream status to a health tone', () => {
    expect(streamTone('ok')).toBe('up')
    expect(streamTone('backoff')).toBe('warn')
    expect(streamTone('failed')).toBe('down')
    expect(streamTone('stopped')).toBe('neutral')
  })
})
