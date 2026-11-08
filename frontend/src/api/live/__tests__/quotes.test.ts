import { describe, expect, it } from 'vitest'
import {
  type LiveQuote,
  type LiveQuotesTick,
  applySession,
  applyTick,
  delayedLabel,
  feedTone,
  fmtLatency,
  quoteFromTick,
  quoteTitle,
  quoteTone,
  quoteView,
  serviceChip,
  useQuoteStore,
} from '../quotes'
import { STREAM_TOPICS, streamUrl } from '../types'

const AAPL = {
  id: 'AAPL',
  security_id: 'S3NS7GBAE24GR',
  symbol: 'AAPL',
  price: 347.1,
  change: 3.19,
  change_pct: 0.00927569,
  prev_close: 343.91,
  currency: 'USD',
  market: 'XNAS',
  market_state: 'REGULAR',
  market_open: true,
  market_reason: 'open',
  next_open_at: null,
  quote_ts: '2026-10-27T18:32:00Z',
  fetched_at: '2026-10-27T18:47:10Z',
  delay_min: 15,
  source: 'yahoo',
  stale: false,
  delayed: true,
} as unknown as LiveQuote

describe('delayed quotes adapters', () => {
  it('maps a REST quote to the view model', () => {
    const v = quoteView(AAPL)
    expect(v).toMatchObject({ securityId: 'S3NS7GBAE24GR', price: 347.1, changePct: 0.00927569, marketOpen: true, delayMin: 15, source: 'yahoo' })
  })

  it('labels the print time: "delayed · hh:mm", with the date when not today', () => {
    const v = quoteView(AAPL)
    expect(delayedLabel(v, 'UTC', new Date('2026-10-27T20:00:00Z'))).toBe('delayed · 18:32')
    expect(delayedLabel(v, 'Europe/Lisbon', new Date('2026-10-27T20:00:00Z'))).toBe('delayed · 18:32')
    expect(delayedLabel(v, 'America/New_York', new Date('2026-10-27T20:00:00Z'))).toBe('delayed · 14:32')
    expect(delayedLabel(v, 'UTC', new Date('2026-10-29T09:00:00Z'))).toMatch(/^delayed · 27 Oct,? 18:32$/)
    expect(delayedLabel(null)).toBe('delayed')
  })

  it('explains the number in the tooltip and tones the tag', () => {
    const v = quoteView(AAPL)
    const t = quoteTitle(v, 'UTC', new Date('2026-10-27T20:00:00Z'))
    expect(t).toContain('Delayed quote (Yahoo, ~15 min)')
    expect(t).toContain('last print 18:32')
    expect(t).toContain('not the official close')
    expect(quoteTone(v)).toBe('up')
    expect(quoteTone({ ...v, marketOpen: false })).toBe('neutral')
    expect(quoteTone({ ...v, stale: true })).toBe('warn')
    expect(quoteTitle({ ...v, marketOpen: false, marketReason: 'closed_weekend', nextOpenAt: '2026-11-02T14:30:00Z' }, 'UTC', new Date('2026-10-31T12:00:00Z'))).toContain('closed (weekend)')
  })

  it('applies SSE quotes.tick frames, keeping the market state of the previous quote', () => {
    const prev = { S3NS7GBAE24GR: { ...quoteView(AAPL), marketOpen: true, marketReason: 'open' } }
    const tick = {
      n: 2,
      fetched_at: '2026-10-27T18:48:10Z',
      delayed: true,
      quotes: [
        { security_id: 'S3NS7GBAE24GR', price: 348.0, change: 4.09, change_pct: 0.0119, quote_ts: '2026-10-27T18:33:00Z', market: 'XNAS', source: 'yahoo' },
        { security_id: 'IDX:^GSPC', price: 7722.6, change_pct: 0.006, market: 'XNYS' },
      ],
    } as unknown as LiveQuotesTick
    const next = applyTick(prev, tick)
    expect(next.S3NS7GBAE24GR).toMatchObject({ price: 348.0, changePct: 0.0119, marketOpen: true, prevClose: 343.91, fetchedAt: '2026-10-27T18:48:10Z' })
    expect(next['IDX:^GSPC']).toMatchObject({ price: 7722.6, marketOpen: true, currency: null })
    expect(prev.S3NS7GBAE24GR.price).toBe(347.1) // pure
    expect(quoteFromTick(tick.quotes[1], null).delayMin).toBeNull()
  })

  it('applies quotes.session transitions to every quote of that market', () => {
    const m = { S1: { ...quoteView(AAPL), securityId: 'S1' }, S2: { ...quoteView(AAPL), securityId: 'S2', market: 'XTKS' } }
    const closed = applySession(m, { market: 'XNAS', open: false, reason: 'after_close', opens_at: '2026-10-28T13:30:00Z' })
    expect(closed.S1).toMatchObject({ marketOpen: false, marketReason: 'after_close', nextOpenAt: '2026-10-28T13:30:00Z' })
    expect(closed.S2.marketOpen).toBe(true)
  })

  it('store upsert maps requested ids (tickers, bare symbols) to security ids', () => {
    useQuoteStore.setState({ quotes: {}, req: {}, service: null })
    useQuoteStore.getState().upsert({
      data_status: 'ok',
      delayed: true,
      items: [AAPL, { ...AAPL, id: '^GSPC', security_id: 'IDX:^GSPC', price: 7722.6 }],
      missing: [],
      markets: [],
      service: { running: true, enabled: true, state: 'ok', delayed: true },
    } as never)
    const st = useQuoteStore.getState()
    expect(st.req.AAPL).toBe('S3NS7GBAE24GR')
    expect(st.quotes['IDX:^GSPC'].price).toBe(7722.6)
    expect(st.service?.state).toBe('ok')
  })
})

describe('live strip and feeds panel adapters', () => {
  it('service chip: live / idle with reason / source down / off / stopped', () => {
    expect(serviceChip({ running: true, enabled: true, state: 'ok', delayed: true, markets_open: 3, n_securities: 210 }).label).toBe('Quotes live')
    const idle = serviceChip({ running: true, enabled: true, state: 'idle', reason: 'all markets closed', delayed: true })
    expect(idle.label).toBe('Quotes idle')
    expect(idle.title).toContain('all markets closed')
    expect(serviceChip({ running: true, enabled: true, state: 'source_down', reason: 'retry at 15:05', delayed: true }).tone).toBe('warn')
    expect(serviceChip({ running: false, enabled: false, state: 'off', delayed: true }).label).toBe('Quotes off')
    expect(serviceChip({ running: false, enabled: true, state: 'stopped', delayed: true }).tone).toBe('down')
    expect(serviceChip(null).label).toBe('Quotes …')
  })

  it('feed tone and latency formatting', () => {
    expect(feedTone('ok')).toBe('up')
    expect(feedTone('idle')).toBe('neutral')
    expect(feedTone('disabled')).toBe('neutral')
    expect(feedTone('backoff')).toBe('warn')
    expect(feedTone('stopped')).toBe('down')
    expect(fmtLatency(37.6)).toBe('38 s')
    expect(fmtLatency(629.6)).toBe('10.5 min')
    expect(fmtLatency(17252)).toBe('4.8 h')
    expect(fmtLatency(null)).toBe('—')
  })

  it('the SSE stream subscribes the quote topics', () => {
    expect(STREAM_TOPICS).toEqual(['filings', 'news', 'macro', 'alerts', 'quotes.tick', 'quotes.session'])
    expect(streamUrl('http://h/api/v1', STREAM_TOPICS, null)).toContain('topics=filings%2Cnews%2Cmacro%2Calerts%2Cquotes.tick%2Cquotes.session')
  })
})
