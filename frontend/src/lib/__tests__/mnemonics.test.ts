import { describe, expect, it } from 'vitest'
import { helpText, loadHistory, mnemonicsFor, parseCommand, pushHistory, routeFor, type RouteCtx } from '../mnemonics'

const ctx = (over: Partial<RouteCtx> = {}): RouteCtx => ({ hasRoute: () => false, ...over })

describe('parseCommand', () => {
  it('parses ticker + market + yellow key + mnemonic', () => {
    expect(parseCommand('AAPL US Equity DES')).toMatchObject({ kind: 'mnemonic', code: 'DES', security: 'AAPL US Equity', ticker: 'AAPL', market: 'US', yellow: 'Equity' })
    expect(parseCommand('aapl gp')).toMatchObject({ kind: 'mnemonic', code: 'GP', security: 'AAPL', ticker: 'AAPL' })
    expect(parseCommand('SX5E Index GP')).toMatchObject({ kind: 'mnemonic', code: 'GP', security: 'SX5E Index', yellow: 'Index' })
    expect(parseCommand('EURUSD Curncy GP')).toMatchObject({ kind: 'mnemonic', code: 'GP', yellow: 'Curncy', ticker: 'EURUSD' })
    expect(parseCommand('C 1 Comdty GP')).toMatchObject({ kind: 'mnemonic', code: 'GP', ticker: 'C 1', yellow: 'Comdty' })
    expect(parseCommand('SAP GY FA')).toMatchObject({ code: 'FA', security: 'SAP GY', market: 'GY' })
  })

  it('parses global mnemonics with an argument', () => {
    expect(parseCommand('ECO')).toMatchObject({ kind: 'mnemonic', code: 'ECO' })
    expect(parseCommand('WIRP US')).toMatchObject({ kind: 'mnemonic', code: 'WIRP', arg: 'US' })
    expect(parseCommand('n')).toMatchObject({ kind: 'mnemonic', code: 'N' })
    expect(parseCommand('BT')).toMatchObject({ kind: 'mnemonic', code: 'PORT' })
  })

  it('parses BQL, HELP, plain securities and falls back to search', () => {
    expect(parseCommand("BQL get(px_last) for(['AAPL US'])")).toMatchObject({ kind: 'bql', bql: "get(px_last) for(['AAPL US'])" })
    expect(parseCommand("get(px) for(['X'])")).toMatchObject({ kind: 'bql' })
    expect(parseCommand('BQL')).toMatchObject({ kind: 'bql', bql: undefined })
    expect(parseCommand('HELP DES')).toMatchObject({ kind: 'help', helpFor: 'DES' })
    expect(parseCommand('HELP')).toMatchObject({ kind: 'mnemonic', code: 'HELP' })
    expect(parseCommand('SPX Index')).toMatchObject({ kind: 'security', security: 'SPX Index' })
    expect(parseCommand('RY CN')).toMatchObject({ kind: 'security', market: 'CN' })
    expect(parseCommand('RY CN CN')).toMatchObject({ kind: 'mnemonic', code: 'CN', market: 'CN' })
    expect(parseCommand('apple')).toMatchObject({ kind: 'search' })
    expect(parseCommand('microsoft corporation inc')).toMatchObject({ kind: 'search' })
    expect(parseCommand('   ')).toMatchObject({ kind: 'empty' })
  })
})

describe('routeFor', () => {
  const aapl = ctx({ securityId: 'SS8MA4AQCCX39', ticker: 'AAPL' })
  it('routes security mnemonics to Company 360 tabs', () => {
    expect(routeFor(parseCommand('AAPL DES'), aapl)?.path).toBe('/company/SS8MA4AQCCX39')
    expect(routeFor(parseCommand('AAPL GP'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=technicals')
    expect(routeFor(parseCommand('AAPL FA'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=financials')
    expect(routeFor(parseCommand('AAPL RV'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=peers')
    expect(routeFor(parseCommand('AAPL EE'), aapl)).toMatchObject({ path: '/company/SS8MA4AQCCX39?tab=forecast', note: 'model-based, not consensus' })
    expect(routeFor(parseCommand('AAPL ERN'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=earnings')
    expect(routeFor(parseCommand('AAPL HDS'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=ownership')
    expect(routeFor(parseCommand('AAPL NET'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=network')
    expect(routeFor(parseCommand('AAPL THES'), aapl)?.path).toBe('/theses/new?subject_type=security&subject_id=SS8MA4AQCCX39')
    expect(routeFor(parseCommand('AAPL OMON'), aapl)).toBeNull()
    expect(routeFor(parseCommand('AAPL US'), aapl)?.path).toBe('/company/SS8MA4AQCCX39')
  })

  it('routes instruments to the instrument page', () => {
    const spx = ctx({ securityId: 'IDX:^GSPC', ticker: '^GSPC', instrument: true })
    expect(routeFor(parseCommand('SPX Index GP'), spx)?.path).toBe('/instruments/IDX%3A%5EGSPC')
    expect(routeFor(parseCommand('SPX Index FA'), spx)?.note).toContain('equities')
  })

  it('falls back gracefully when the News Terminal is absent', () => {
    expect(routeFor(parseCommand('N'), ctx())).toMatchObject({ path: '/events' })
    expect(routeFor(parseCommand('N'), ctx({ hasRoute: (p) => p === '/news' }))?.path).toBe('/news')
    expect(routeFor(parseCommand('TOP'), ctx({ hasRoute: () => true }))?.path).toBe('/news?view=top')
    expect(routeFor(parseCommand('AAPL US CN'), aapl)?.path).toBe('/company/SS8MA4AQCCX39?tab=sentiment')
    expect(routeFor(parseCommand('AAPL US CN'), { ...aapl, hasRoute: () => true })?.path).toBe('/news?ticker=AAPL')
  })

  it('routes global mnemonics', () => {
    const r = (s: string) => routeFor(parseCommand(s), ctx())?.path
    expect(r('ECO')).toBe('/macro/calendar')
    expect(r('ECO US')).toBe('/macro/calendar?country=US')
    expect(r('WIRP US')).toBe('/macro/cb/fed')
    expect(r('WIRP EU')).toBe('/macro/cb/ecb')
    expect(r('WIRP')).toBe('/macro/cb')
    expect(r('ECST DE')).toBe('/macro/country/DE')
    expect(r('WEI')).toBe('/macro/markets')
    expect(r('COT')).toBe('/macro/positioning')
    expect(r('EQS')).toBe('/screener')
    expect(r('PORT')).toBe('/backtest')
    expect(r('BQL')).toBe('/bql')
    expect(r('MDL')).toBe('/models')
    expect(r('SMRT')).toBe('/smart-money')
    expect(r('EVTS')).toBe('/earnings')
    expect(r('ALRT')).toBe('/watchlist')
    expect(r('DES')).toBeUndefined() // needs a security
    expect(routeFor(parseCommand("BQL get(px_last) for(['AAPL US'])"), ctx())?.path).toBe(`/bql?q=${encodeURIComponent("get(px_last) for(['AAPL US'])")}&run=1`)
  })
})

describe('suggestions, help and history', () => {
  it('suggests mnemonics by asset class', () => {
    const eq = mnemonicsFor('Equity').map((m) => m.code)
    expect(eq).toContain('FA')
    expect(eq).toContain('DES')
    const idx = mnemonicsFor('Index').map((m) => m.code)
    expect(idx).toContain('GP')
    expect(idx).not.toContain('FA')
  })
  it('describes mnemonics', () => {
    expect(helpText('DES')).toContain('Description')
    expect(helpText('wirp')).toContain('WIRP')
    expect(helpText('ZZZ')).toContain('unknown mnemonic')
  })
  it('keeps a de-duplicated recent-commands list', () => {
    const mem = new Map<string, string>()
    const st = { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v) }
    pushHistory('AAPL GP', st)
    pushHistory('ECO', st)
    pushHistory('aapl gp', st)
    expect(loadHistory(st)).toEqual(['aapl gp', 'ECO'])
    for (let i = 0; i < 30; i++) pushHistory(`X${i} DES`, st)
    expect(loadHistory(st)).toHaveLength(20)
    expect(loadHistory(null)).toEqual([])
  })
})
