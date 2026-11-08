import { describe, expect, it } from 'vitest'
import { inlineText, makeSlugger, parseGuideMarkdown, parseInline, resolveLink, slugify, type Block } from '../guide/markdown'
import { buildSections, buildToc, chapterOrder, chapterTitle, sortChapters } from '../guide/toc'
import { makeSnippet, searchSections, tokenize } from '../guide/search'
import { helpHref, targetHref } from '../guide/helpMap'

const DOC = `# 5B. Company 360: Technicals

Intro with **bold**, *italic*, \`code\` and a [link](02a-macro.md#macro-cockpit).

## Technicals

**Route:** \`/company/:id?tab=technicals\` · **Menu:** Company 360

### Purpose

The trade setup card shows first-passage probabilities.

![Technicals tab](img/05-aapl-technicals.webp)

*Figure 5B.1: Technicals tab.*

| # | Panel | What it shows |
|---|:---|---:|
| 1 | Price | Calibrated fan \\| bands |
| 2 | Setup | T2 P(win) |

- one
- two
  - two.a
  - two.b
- three

1. first
2. second

> **Experimental:** hidden by default.
> Second line of the same quote.

\`\`\`
z = (x − μ) / σ
\`\`\`

<!-- PENDING-SHOT id=x route=/y -->

---

## News & Sentiment

### Purpose

Text again.
`

const blocks = parseGuideMarkdown(DOC)
const kinds = (bs: Block[]) => bs.map((b) => b.t)

describe('guide markdown', () => {
  it('slugs headings like GitHub, de-duplicating', () => {
    expect(slugify('News & Sentiment')).toBe('news--sentiment')
    expect(slugify('2A. Macro')).toBe('2a-macro')
    expect(slugify('Activism (13D)')).toBe('activism-13d')
    expect(slugify('Escalation & GPR')).toBe('escalation--gpr')
    const s = makeSlugger()
    expect([s('Purpose'), s('Purpose'), s('Purpose'), s('Purpose-1')]).toEqual(['purpose', 'purpose-1', 'purpose-2', 'purpose-1-1'])
  })

  it('parses the block structure', () => {
    expect(kinds(blocks)).toEqual(['h', 'p', 'h', 'p', 'h', 'p', 'img', 'p', 'table', 'list', 'list', 'quote', 'pre', 'hr', 'h', 'h', 'p'])
    const hs = blocks.filter((b) => b.t === 'h')
    expect(hs.map((h) => (h.t === 'h' ? `${h.level}:${h.id}` : ''))).toEqual(['1:5b-company-360-technicals', '2:technicals', '3:purpose', '2:news--sentiment', '3:purpose-1'])
  })

  it('parses tables with alignment and escaped pipes', () => {
    const t = blocks.find((b) => b.t === 'table')
    expect(t?.t).toBe('table')
    if (t?.t !== 'table') return
    expect(t.align).toEqual([null, 'left', 'right'])
    expect(t.rows).toHaveLength(2)
    expect(inlineText(t.rows[0][2])).toBe('Calibrated fan | bands')
  })

  it('parses nested lists, numbered lists and multi-line quotes', () => {
    const [ul, ol] = blocks.filter((b) => b.t === 'list')
    expect(ul.t === 'list' && ul.items.map((i) => inlineText(i.inl))).toEqual(['one', 'two', 'three'])
    expect(ul.t === 'list' && ul.items[1].sub?.items.map((i) => inlineText(i.inl))).toEqual(['two.a', 'two.b'])
    expect(ol.t === 'list' && ol.ordered).toBe(true)
    const q = blocks.find((b) => b.t === 'quote')
    expect(q?.t === 'quote' && q.blocks).toHaveLength(1)
    expect(q?.t === 'quote' && inlineText((q.blocks[0] as Extract<Block, { t: 'p' }>).inl)).toBe('Experimental: hidden by default. Second line of the same quote.')
  })

  it('keeps code blocks verbatim and drops HTML comments', () => {
    const pre = blocks.find((b) => b.t === 'pre')
    expect(pre?.t === 'pre' && pre.v).toBe('z = (x − μ) / σ')
    expect(JSON.stringify(blocks)).not.toContain('PENDING-SHOT')
  })

  it('parses inline markup', () => {
    const inl = parseInline('a **b *c*** `d*e` [f](g.md#h) ![i](img/j.webp) snake_case_word _it_ \\*lit\\*')
    expect(inl.map((x) => x.t)).toEqual(['text', 'b', 'text', 'code', 'text', 'a', 'text', 'img', 'text', 'i', 'text'])
    expect(inlineText(inl)).toBe('a b c d*e f i snake_case_word it *lit*')
  })

  it('resolves links', () => {
    expect(resolveLink('https://example.org')).toEqual({ kind: 'external', href: 'https://example.org' })
    expect(resolveLink('#zones')).toEqual({ kind: 'guide', chapter: null, anchor: 'zones' })
    expect(resolveLink('05b-company-technicals.md#trade-setup')).toEqual({ kind: 'guide', chapter: '05b-company-technicals', anchor: 'trade-setup' })
    expect(resolveLink('./README.md')).toEqual({ kind: 'guide', chapter: 'README', anchor: null })
    expect(resolveLink('../USER_GUIDE.md')).toEqual({ kind: 'none' })
    expect(resolveLink('javascript:alert(1)')).toEqual({ kind: 'none' })
  })
})

describe('guide toc and search', () => {
  it('orders chapters README, numbered, then the rest', () => {
    const order = sortChapters(['glossary', '05b-x', '10-y', 'README', '02a-z', '00-a', '05a-w', '02b-v'].map((slug) => ({ slug }))).map((c) => c.slug)
    expect(order).toEqual(['README', '00-a', '02a-z', '02b-v', '05a-w', '05b-x', '10-y', 'glossary'])
    expect(chapterOrder('README')).toBeLessThan(chapterOrder('00-a'))
  })

  it('builds the toc and the sections', () => {
    expect(chapterTitle(blocks, 'x')).toBe('5B. Company 360: Technicals')
    expect(buildToc(blocks).map((t) => t.id)).toEqual(['technicals', 'news--sentiment'])
    expect(buildToc(blocks, 3)).toHaveLength(4)
    const secs = buildSections('05b', '5B', blocks)
    expect(secs.map((s) => s.anchor)).toEqual([null, 'technicals', 'purpose', 'news--sentiment', 'purpose-1'])
    expect(secs[2].trail).toEqual(['5B', 'Technicals'])
    expect(secs[2].text).toContain('T2 P(win)')
  })

  it('searches every term, ranks headings first and highlights the snippet', () => {
    const secs = buildSections('05b', '5B', blocks)
    expect(tokenize('First-passage  probabilities, P(win)')).toEqual(['first-passage', 'probabilities', 'win'])
    const hits = searchSections(secs, 'first-passage probabilities')
    expect(hits[0].section.anchor).toBe('purpose')
    expect(hits[0].snippet.some((r) => r.hit && r.text.toLowerCase() === 'first-passage')).toBe(true)
    expect(searchSections(secs, 'sentiment')[0].section.anchor).toBe('news--sentiment')
    expect(searchSections(secs, 'nonexistentword')).toEqual([])
    expect(searchSections(secs, '')).toEqual([])
    const snip = makeSnippet('Café crème — calibrated over history', ['cafe'])
    expect(snip[0]).toEqual({ text: 'Café', hit: true })
  })
})

describe('help route map', () => {
  it('maps pages and tabs to guide anchors', () => {
    expect(helpHref('/macro')).toBe('/help/02a-macro#macro-cockpit')
    expect(helpHref('/company/AAPL', '?tab=technicals')).toBe('/help/05b-company-technicals#technicals')
    expect(helpHref('/company/7203.T', '?tab=sentiment')).toBe('/help/05a-company-fundamentals#news--sentiment')
    expect(helpHref('/company/AAPL')).toBe('/help/05a-company-fundamentals#overview')
    expect(helpHref('/macro/markets', '?tab=scenarios')).toBe('/help/02b-markets-cross-asset#scenario-builder')
    expect(helpHref('/macro/markets')).toBe('/help/02b-markets-cross-asset#markets-overview')
    expect(helpHref('/theses/T123/edit')).toBe('/help/12-thesis-tracker#thesis-editor')
    expect(helpHref('/unknown/page')).toBe('/help')
    expect(targetHref('README')).toBe('/help')
  })
})
