import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * coverage v3 A7 review: resolveId() treated any 'S…' string as an internal id, so tickers such as SHOP, SAP, SBUX or
 * SO skipped /search and every Company 360 tab (Debt included) answered 404. Only the real id shape (S + 12 Crockford
 * base32 chars) may bypass /search.
 */
const SHOP_ID = 'S9EFQQXK1Y63T'
const rows: Record<string, { security_id: string; ticker: string; name: string; issuer_id: string }[]> = {
  SHOP: [
    { security_id: SHOP_ID, ticker: 'SHOP', name: 'Shopify Inc', issuer_id: 'I1' },
    { security_id: 'SAAAAAAAAAAA1', ticker: 'SHOP.TO', name: 'Shopify Inc', issuer_id: 'I1' },
  ],
  SAP: [{ security_id: 'S2MZ7RR28QA9E', ticker: 'SAP', name: 'SAP SE', issuer_id: 'I2' }],
}
const get = vi.fn()

beforeEach(() => {
  vi.resetModules()
  get.mockReset().mockImplementation(async (path: string, q?: { q?: string }) => {
    if (path === '/search') return rows[String(q?.q ?? '').toUpperCase()] ?? []
    return { security_id: path.split('/')[2], history: [], bonds: [], reasons: [], labels: {}, data_status: 'pending' }
  })
  vi.doMock('../http', () => ({
    USE_MOCK: false,
    API_BASE_URL: 'http://x',
    API_PREFIX: '/api/v1',
    http: { get },
    optional: async <T,>(p: Promise<T>) => {
      try {
        return await p
      } catch {
        return null
      }
    },
    queryParamEnum: async () => null,
  }))
})

describe('resolveId', () => {
  it('sends S-prefixed tickers through /search and returns the matching security id', async () => {
    const { resolveId } = await import('../client')
    expect(await resolveId('SHOP')).toBe(SHOP_ID)
    expect(get).toHaveBeenCalledWith('/search', { q: 'SHOP', limit: 5 })
    expect(await resolveId('sap')).toBe('S2MZ7RR28QA9E')
  })
  it('passes real internal ids through unchanged without a /search call', async () => {
    const { resolveId, SECURITY_ID_RE } = await import('../client')
    expect(await resolveId(SHOP_ID)).toBe(SHOP_ID)
    expect(get).not.toHaveBeenCalled()
    expect(['SHOP', 'SO', 'SBUX', 'SPGI', 'S_AAPL', 'SHOP-B', 'S9EFQQXK1Y63', 'ETF:AAXJ'].some((x) => SECURITY_ID_RE.test(x))).toBe(false)
  })
  it('the Debt client resolves an S-prefixed ticker before calling the endpoint', async () => {
    const { ratesApi } = await import('../rates/client')
    await ratesApi.companyDebt('SHOP')
    expect(get).toHaveBeenCalledWith('/search', { q: 'SHOP', limit: 5 })
    expect(get).toHaveBeenLastCalledWith(`/companies/${SHOP_ID}/debt`)
  })
  it('an unknown ticker is sent unchanged (the API answers its own 404 / ambiguity message)', async () => {
    const { resolveId } = await import('../client')
    expect(await resolveId('SNOPE')).toBe('SNOPE')
  })
})
