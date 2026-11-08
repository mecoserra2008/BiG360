import { describe, expect, it } from 'vitest'
import { fmtBqlValue, parseBqlHistory, screenerToBql, tokenize, wordAt } from '../bql'

describe('BQL lexer (highlighting)', () => {
  it('classifies tokens like the backend lexer', () => {
    const k = (s: string) => tokenize(s).filter((t) => t.kind !== 'ws').map((t) => `${t.kind}:${t.text}`)
    expect(k("get(chg_pct(px, 1M)) for(['AAPL US']) where(roic > 15%)")).toEqual([
      'clause:get', 'punct:(', 'func:chg_pct', 'punct:(', 'field:px', 'punct:,', 'duration:1M', 'punct:)', 'punct:)',
      'clause:for', 'punct:(', 'punct:[', "string:'AAPL US'", 'punct:]', 'punct:)',
      'clause:where', 'punct:(', 'field:roic', 'op:>', 'number:15%', 'punct:)',
    ])
    expect(k('with(as_of=2026-01-31) sort(x desc) limit(5)')).toContain('date:2026-01-31')
    expect(k('sort(x desc)')).toContain('keyword:desc')
    expect(k('tech.hi52_pct')).toEqual(['field:tech.hi52_pct'])
    expect(k("get(x) ; 'open")).toEqual(['clause:get', 'punct:(', 'field:x', 'punct:)', 'error:;', "error:'open"])
  })
  it('reconstructs the source exactly', () => {
    const s = "get(px_last, 'it''s', 1.5e3)\n  for(['A'])"
    expect(tokenize(s).map((t) => t.text).join('')).toBe(s)
  })
})

describe('autocomplete word', () => {
  it('finds the identifier before the cursor, not inside strings', () => {
    expect(wordAt('get(tech.hi', 11)).toEqual({ word: 'tech.hi', start: 4 })
    expect(wordAt("for(['AAP", 9).word).toBe('')
    expect(wordAt('get(15', 6).word).toBe('')
  })
})

describe('formatting', () => {
  it('formats by unit', () => {
    expect(fmtBqlValue(null, 'pct')).toBe('—')
    expect(fmtBqlValue(0.1234, 'pct')).toContain('12.3')
    expect(fmtBqlValue(true, 'bool')).toBe('true')
    expect(fmtBqlValue('TECH', 'text')).toBe('TECH')
  })
})

describe('Screener -> BQL', () => {
  it('translates filters and sort', () => {
    const { query, notes } = screenerToBql({ regions: ['US', 'EU'], sector: 'Information Technology', bucket: 'large', moat: 'wide', minScore: 60, sortId: 'roic', sortDesc: true })
    expect(query).toContain("for(members('full') & region('US', 'EU') & sector('Information Technology'))")
    expect(query).toContain("where(mkt_cap_usd >= 10e9 and mkt_cap_usd < 200e9 and moat == 'wide' and composite >= 60)")
    expect(query).toContain('sort(roic desc) limit(100)')
    expect(notes.length).toBe(1)
    const b = screenerToBql({ regions: [], country: "O'X", sortId: 'pe', sortDesc: false })
    expect(b.query).toContain("country('O''X')")
    expect(b.query).toContain('sort(pe_fwd asc)')
  })
})

describe('BQL history validation (guide 14: malformed entries crashed the History tab)', () => {
  it('keeps valid entries, converts legacy strings and drops junk', () => {
    const raw = JSON.stringify([
      { q: 'get(px_last) for([\'AAPL US\'])', at: '2026-10-23T10:00:00Z' },
      'get(roic) for(members(mvp_us))',
      null,
      42,
      { q: 7 },
      { at: '2026-10-23' },
      { q: '   ' },
      { q: 'get(px_last) for([\'AAPL US\'])', at: '2026-10-22T10:00:00Z' },
      { q: 'get(pe) for(members(mvp_us))', at: 'not a date' },
    ])
    expect(parseBqlHistory(raw)).toEqual([
      { q: "get(px_last) for(['AAPL US'])", at: '2026-10-23T10:00:00Z' },
      { q: 'get(roic) for(members(mvp_us))', at: '' },
      { q: 'get(pe) for(members(mvp_us))', at: '' },
    ])
    expect(parseBqlHistory('{not json')).toEqual([])
    expect(parseBqlHistory('{"q": "x"}')).toEqual([])
    expect(parseBqlHistory(Array.from({ length: 50 }, (_, i) => ({ q: `q${i}`, at: '' })))).toHaveLength(30)
  })
})

describe('Open as BQL keeps the Balance-sheet sort (guide 04)', () => {
  it('maps the Screener BS column id', () => {
    const { query } = screenerToBql({ regions: [], country: '', sector: '', subsector: '', bucket: '', moat: '', minScore: 0, sortId: 'f_balanceSheet', sortDesc: true })
    expect(query).toContain('f.balance_sheet')
  })
})
