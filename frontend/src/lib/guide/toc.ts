/**
 * Guide structure helpers for the in-app Help page: chapter ordering and titles, table of contents (from parsed
 * headings) and the plain-text sections used by the full-text search.
 */
import { inlineText, type Block, type Inline } from './markdown'

export interface ChapterMeta {
  /** file name without .md, e.g. '05b-company-technicals'; 'README' is the index */
  slug: string
  title: string
  /** 'README' first, numbered chapters in file order, the glossary last */
  order: number
}

export interface TocEntry {
  level: 2 | 3
  id: string
  text: string
}

export interface Section {
  chapter: string
  chapterTitle: string
  /** heading of the section ('' for the text before the first ## heading) */
  heading: string
  /** anchor of the heading (null = top of the chapter) */
  anchor: string | null
  /** headings above it (chapter > h2 > h3), for display */
  trail: string[]
  text: string
}

/** Sort key of a chapter file name: README, then the numbered chapters, then anything else (glossary) by name. */
export function chapterOrder(slug: string): number {
  if (slug.toLowerCase() === 'readme') return -1
  const m = /^(\d{1,2})([a-z]?)-/.exec(slug)
  if (m) return parseInt(m[1], 10) * 10 + (m[2] ? m[2].charCodeAt(0) - 96 : 0)
  return 1000
}

export function chapterTitle(blocks: Block[], fallback: string): string {
  const h1 = blocks.find((b) => b.t === 'h' && b.level === 1)
  return h1 && h1.t === 'h' ? h1.text : fallback
}

export function sortChapters<T extends { slug: string }>(list: T[]): T[] {
  return [...list].sort((a, b) => chapterOrder(a.slug) - chapterOrder(b.slug) || a.slug.localeCompare(b.slug))
}

export function buildToc(blocks: Block[], maxLevel: 2 | 3 = 2): TocEntry[] {
  const out: TocEntry[] = []
  for (const b of blocks) {
    if (b.t === 'h' && (b.level === 2 || (b.level === 3 && maxLevel === 3))) out.push({ level: b.level, id: b.id, text: b.text })
  }
  return out
}

function cellsText(cells: Inline[][]): string {
  return cells.map((c) => inlineText(c)).join(' · ')
}

/** Plain text of one block (tables row by row, lists item by item). */
export function blockText(b: Block): string {
  switch (b.t) {
    case 'h':
      return b.text
    case 'p':
      return inlineText(b.inl)
    case 'list':
      return b.items.map((it) => [inlineText(it.inl), it.sub ? blockText(it.sub) : ''].filter(Boolean).join(' ')).join(' \n')
    case 'table':
      return [cellsText(b.head), ...b.rows.map((r) => cellsText(r))].join(' \n')
    case 'quote':
      return b.blocks.map(blockText).join(' \n')
    case 'pre':
      return b.v
    case 'img':
      return b.alt
    case 'hr':
      return ''
  }
}

/**
 * Split a chapter into searchable sections at every ## and ### heading. Each section carries its anchor so a search
 * hit deep-links to the right place.
 */
export function buildSections(chapter: string, chapterTitleText: string, blocks: Block[]): Section[] {
  const out: Section[] = []
  let cur: Section = { chapter, chapterTitle: chapterTitleText, heading: '', anchor: null, trail: [chapterTitleText], text: '' }
  let h2 = ''
  const parts: string[] = []
  const push = () => {
    cur.text = parts.join('\n').replace(/[ \t]+/g, ' ').trim()
    if (cur.text || cur.heading) out.push(cur)
    parts.length = 0
  }
  for (const b of blocks) {
    if (b.t === 'h' && (b.level === 2 || b.level === 3)) {
      push()
      if (b.level === 2) h2 = b.text
      const trail = b.level === 2 ? [chapterTitleText] : [chapterTitleText, h2].filter(Boolean)
      cur = { chapter, chapterTitle: chapterTitleText, heading: b.text, anchor: b.id, trail, text: '' }
      continue
    }
    if (b.t === 'h' && b.level === 1) continue
    const t = blockText(b)
    if (t) parts.push(t)
  }
  push()
  return out
}
