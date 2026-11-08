import { describe, expect, it } from 'vitest'
import { NewsStream, useNewsLive } from '../stream'
import { EMPTY_FILTER, type TermRow, carPctLabel, filterFromParams, filterQuery, groupByStory, matches, mergeRows, moveLabel, moveSel, rowFromStream, salienceLevel, sentimentTone, unreadCount } from '../types'

function row(i: number, o: Partial<TermRow> = {}): TermRow {
  const ts = new Date(Date.UTC(2026, 9, 22, 12, 0, i)).toISOString()
  return {
    origin: 'live',
    item_key: `k${i}`,
    kind: 'news',
    tier: 2,
    title: `Headline ${i}`,
    tickers: [],
    security_ids: [],
    issuer_ids: [],
    countries: [],
    sectors: [],
    themes: [],
    story_size: 1,
    story_sources: 1,
    first_seen_at: ts,
    published_at: ts,
    arrivedAt: null,
    ...o,
  }
}

describe('news terminal adapters', () => {
  it('merges live rows: dedupe by key, newest first, bounded, O(n) fast path', () => {
    const base = Array.from({ length: 5 }, (_, i) => row(i)).reverse()
    const merged = mergeRows(base, [row(9, { arrivedAt: 1 }), row(3, { title: 'updated' })])
    expect(merged.map((r) => r.item_key)).toEqual(['k9', 'k4', 'k3', 'k2', 'k1', 'k0'])
    expect(merged.find((r) => r.item_key === 'k3')?.title).toBe('updated')
    expect(mergeRows(base, [row(10), row(11)], 3).map((r) => r.item_key)).toEqual(['k11', 'k10', 'k4'])
    expect(mergeRows(base, [])).toBe(base)
    // 6 000 rows + a live batch stays fast
    const big = Array.from({ length: 6000 }, (_, i) => row(i % 60, { item_key: `b${i}` }))
    const t0 = performance.now()
    mergeRows(big, [row(59, { item_key: 'new' })], 8000)
    expect(performance.now() - t0).toBeLessThan(50)
  })

  it('groups by story with source counts and first / last seen', () => {
    const rows = [row(5, { story_id: 'S1', publisher: 'WSJ', sentiment: -0.4 }), row(4, { story_id: 'S1', publisher: 'FT', sentiment: -0.2 }), row(3), row(2, { story_id: 'S1', publisher: 'FT' })]
    const g = groupByStory(rows)
    expect(g.map((x) => x.key)).toEqual(['S1', 'item:k3'])
    expect(g[0].items).toHaveLength(3)
    expect(g[0].nSources).toBe(2)
    expect(g[0].head.item_key).toBe('k5')
    expect(g[0].firstSeen < g[0].lastSeen).toBe(true)
    expect(g[0].sentiment).toBeCloseTo(-0.3)
  })

  it('filters like the server, plus watchlist (ids or tickers) and muted stories client-side', () => {
    const r = row(1, { tickers: ['AAPL'], security_ids: ['S1'], countries: ['US'], sectors: ['TECH'], themes: ['earnings'], language: 'en', story_id: 'X', tier: 1 })
    expect(matches(r, EMPTY_FILTER)).toBe(true)
    expect(matches(r, { ...EMPTY_FILTER, tickers: ['AAPL'] })).toBe(true)
    expect(matches(r, { ...EMPTY_FILTER, tickers: ['MSFT'] })).toBe(false)
    expect(matches(r, { ...EMPTY_FILTER, watchlistOnly: true, watchSecurityIds: ['aapl'] })).toBe(true)
    expect(matches(r, { ...EMPTY_FILTER, watchlistOnly: true, watchSecurityIds: ['MSFT'] })).toBe(false)
    expect(matches(r, { ...EMPTY_FILTER, countries: ['US'], sectors: ['TECH'], themes: ['earnings'], tiers: [1, 2], langs: ['en'] })).toBe(true)
    expect(matches(r, { ...EMPTY_FILTER, mutedStories: ['X'] })).toBe(false)
    expect(matches(r, { ...EMPTY_FILTER, q: 'headline 1' })).toBe(true)
    expect(filterQuery({ ...EMPTY_FILTER, tickers: ['AAPL', 'MSFT'], tiers: [1, 2] }, 600, 'B')).toMatchObject({ limit: 600, before: 'B', tickers: 'AAPL,MSFT', tier: '1,2' })
    expect(filterFromParams(new URLSearchParams('ticker=aapl&tier=1,9&country=us&watchlist=1'))).toEqual({ tickers: ['AAPL'], tiers: [1], countries: ['US'], watchlistOnly: true })
  })

  it('colours, salience buckets, keyboard moves, unread, move labels', () => {
    expect(sentimentTone({ sentiment: 0.5, tone: null })).toBe('up')
    expect(sentimentTone({ sentiment: -0.5, tone: null })).toBe('down')
    expect(sentimentTone({ sentiment: null, tone: -4 })).toBe('down')
    expect(sentimentTone({ sentiment: 0.1, tone: null })).toBe('neutral')
    expect([salienceLevel(null), salienceLevel(2), salienceLevel(3.2), salienceLevel(5)]).toEqual([0, 1, 2, 3])
    expect([moveSel(-1, 5, 1), moveSel(4, 5, 1), moveSel(0, 5, -1), moveSel(2, 0, 1)]).toEqual([0, 4, 0, -1])
    expect(unreadCount([row(1, { arrivedAt: 10 }), row(2, { arrivedAt: 30 }), row(3)], 20)).toBe(1)
    expect([moveLabel(0.0123), moveLabel(-0.05), moveLabel(null)]).toEqual(['+1.23%', '−5.00%', '—'])
  })

  it('formats the event-study impact CAR as percent, not x100 (guide 08: StoryPane impact bug)', () => {
    // em_event_studies stores mean_car / ci in percent (car x 100): -1.37 means -1.37 %, never -137 %
    expect(carPctLabel(-1.37)).toBe('−1.37%')
    expect(carPctLabel(0.42, 'percent')).toBe('+0.42%')
    expect(carPctLabel(0)).toBe('0.00%')
    expect([carPctLabel(null), carPctLabel(undefined), carPctLabel(Number.NaN)]).toEqual(['—', '—', '—'])
    // a fraction-unit payload (not produced today) would still be scaled correctly
    expect(carPctLabel(0.0137, 'fraction')).toBe('+1.37%')
  })

  it('maps SSE news.item frames and batches them into one store update per frame', async () => {
    const r = rowFromStream({ item_key: 'z', title: 'T', tier: 1, kind: 'news', origin: 'live' }, 123)
    expect(r?.tickers).toEqual([])
    expect(r?.arrivedAt).toBe(123)
    expect(rowFromStream({ title: 'no key' }, 1)).toBeNull()
    useNewsLive.setState({ rows: [] })
    const s = new NewsStream()
    let updates = 0
    const unsub = useNewsLive.subscribe(() => updates++)
    s.enqueue([row(1, { arrivedAt: 1 })])
    s.enqueue([row(2, { arrivedAt: 2 })])
    s.enqueue([], [{ story_id: 'S', origin: 'live', title: 't', n_articles: 2, n_sources: 2, themes: [], sectors: [], countries: [], securities: [] }])
    await new Promise((res) => setTimeout(res, 40))
    unsub()
    expect(useNewsLive.getState().rows.map((x) => x.item_key)).toEqual(['k2', 'k1'])
    expect(useNewsLive.getState().stories.S.n_articles).toBe(2)
    expect(updates).toBe(2) // rows + stories in one flush
    expect(useNewsLive.getState().flushAt).not.toBeNull()
  })
})
