/**
 * Integrity of the user guide in docs/guide as the in-app Help page renders it: every chapter parses with one title,
 * every embedded image exists, every guide link points at an existing chapter and heading anchor, and every
 * "?" help target (lib/guide/helpMap.ts) resolves. Skipped when docs/guide is not present.
 */
import { describe, expect, it } from 'vitest'
import { parseGuideMarkdown, resolveLink, type Block, type Inline } from '../guide/markdown'
import { HELP_RULES } from '../guide/helpMap'

// the same globs as the Help page (pages/help/content.ts); image modules are listed, never loaded
const RAW = import.meta.glob('../../../../docs/guide/*.md', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
const IMG_FILES = Object.keys(import.meta.glob('../../../../docs/guide/img/*'))
const HAS = Object.keys(RAW).length > 0
const base = (p: string) => p.replace(/^.*\//, '')

function walkInline(inl: Inline[], f: (x: Inline) => void) {
  for (const x of inl) {
    f(x)
    if (x.t === 'b' || x.t === 'i' || x.t === 'a') walkInline(x.c, f)
  }
}

function walk(blocks: Block[], fb: (b: Block) => void, fi: (x: Inline) => void) {
  for (const b of blocks) {
    fb(b)
    if (b.t === 'h' || b.t === 'p') walkInline(b.inl, fi)
    else if (b.t === 'quote') walk(b.blocks, fb, fi)
    else if (b.t === 'table') [...b.head, ...b.rows.flat()].forEach((c) => walkInline(c, fi))
    else if (b.t === 'list') {
      const lists = [b]
      while (lists.length) {
        const l = lists.pop()!
        for (const it of l.items) {
          walkInline(it.inl, fi)
          if (it.sub) lists.push(it.sub)
        }
      }
    }
  }
}

describe.skipIf(!HAS)('docs/guide content', () => {
  const docs = new Map(Object.entries(RAW).map(([p, md]) => [base(p).replace(/\.md$/, ''), parseGuideMarkdown(md)]))
  const anchors = new Map([...docs].map(([k, bs]) => [k, new Set(bs.filter((b) => b.t === 'h').map((b) => (b.t === 'h' ? b.id : '')))]))
  const imgs = new Set(IMG_FILES.map(base))

  it('has an index and one title per chapter', () => {
    expect(docs.has('README')).toBe(true)
    for (const [k, bs] of docs) expect(bs.filter((b) => b.t === 'h' && b.level === 1).length, k).toBe(1)
  })

  it('references only existing images and anchors', () => {
    const problems: string[] = []
    for (const [k, bs] of docs) {
      const checkImg = (src: string) => {
        if (/^https?:/.test(src)) return
        const name = src.replace(/^\.?\//, '').replace(/^img\//, '')
        if (!imgs.has(name)) problems.push(`${k}: missing image ${src}`)
      }
      walk(
        bs,
        (b) => {
          if (b.t === 'img') checkImg(b.src)
        },
        (x) => {
          if (x.t === 'img') checkImg(x.src)
          if (x.t !== 'a') return
          const t = resolveLink(x.href)
          if (t.kind !== 'guide') return
          const ch = t.chapter ?? k
          if (!docs.has(ch)) problems.push(`${k}: link to missing chapter ${x.href}`)
          else if (t.anchor && !anchors.get(ch)!.has(t.anchor)) problems.push(`${k}: link to missing anchor ${x.href}`)
        },
      )
    }
    expect(problems).toEqual([])
  })

  it('resolves every help-button target', () => {
    const bad = HELP_RULES.map((r) => r.target)
      .filter((t) => {
        const [ch, a] = t.split('#')
        return !docs.has(ch) || (a && !anchors.get(ch)!.has(a))
      })
    expect(bad).toEqual([])
  })
})
