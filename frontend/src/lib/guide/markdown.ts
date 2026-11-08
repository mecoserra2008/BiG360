/**
 * Markdown renderer model for the in-app user guide (/help). A dependency-free GitHub-flavoured subset, chosen over
 * react-markdown because the app already renders a smaller subset itself (lib/markdown.ts for thesis notes) and the
 * guide is authored against a fixed feature list (docs/guide, writer brief): headings #..#### with GitHub-style ids,
 * paragraphs, **bold**, *italic* / _italic_, `code`, [links](...), ![images](...), bullet / numbered lists with one
 * nested level, GFM pipe tables with alignment, block quotes (nested blocks), fenced code, horizontal rules.
 * HTML is never interpreted: comments are dropped and any other tag is shown as text. The output is a token tree
 * that the Help page renders with React elements (no HTML injection).
 */

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'b' | 'i'; c: Inline[] }
  | { t: 'a'; href: string; c: Inline[] }
  | { t: 'img'; src: string; alt: string }

export interface ListItem {
  inl: Inline[]
  sub?: ListBlock
}

export interface ListBlock {
  t: 'list'
  ordered: boolean
  start: number
  items: ListItem[]
}

export type Align = 'left' | 'center' | 'right' | null

export type Block =
  | { t: 'h'; level: 1 | 2 | 3 | 4; id: string; text: string; inl: Inline[] }
  | { t: 'p'; inl: Inline[] }
  | ListBlock
  | { t: 'table'; align: Align[]; head: Inline[][]; rows: Inline[][][] }
  | { t: 'quote'; blocks: Block[] }
  | { t: 'pre'; lang: string; v: string }
  | { t: 'img'; src: string; alt: string }
  | { t: 'hr' }

/* ── slugs (GitHub-compatible) ── */

/** GitHub heading anchor: lower case, punctuation removed (except - and _), spaces -> '-'. */
export function slugify(text: string): string {
  return text
    .trim()
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s_-]/gu, '')
    .replace(/\s/g, '-')
}

/** Per-document de-duplication exactly like github-slugger: 'x', then 'x-1', 'x-2', ... */
export function makeSlugger(): (text: string) => string {
  const occ = new Map<string, number>()
  return (text: string) => {
    const base = slugify(text)
    let id = base
    while (occ.has(id)) {
      const n = (occ.get(base) ?? 0) + 1
      occ.set(base, n)
      id = `${base}-${n}`
    }
    occ.set(id, 0)
    return id
  }
}

/* ── inline ── */

const PUNCT = /[!"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~]/

function findClose(s: string, from: number, marker: string, mustFollowNonSpace = true): number {
  let k = from
  while (k < s.length) {
    const j = s.indexOf(marker, k)
    if (j < 0) return -1
    if (s[j - 1] === '\\') {
      k = j + 1
      continue
    }
    if (!mustFollowNonSpace || (j > from && !/\s/.test(s[j - 1]))) return j
    k = j + 1
  }
  return -1
}

export function parseInline(src: string): Inline[] {
  const s = src.replace(/<!--[\s\S]*?-->/g, '')
  const out: Inline[] = []
  let buf = ''
  const flush = () => {
    if (buf) out.push({ t: 'text', v: buf })
    buf = ''
  }
  let i = 0
  while (i < s.length) {
    const ch = s[i]
    if (ch === '\\' && i + 1 < s.length && PUNCT.test(s[i + 1])) {
      buf += s[i + 1]
      i += 2
      continue
    }
    if (ch === '`') {
      let n = 1
      while (s[i + n] === '`') n++
      const fence = '`'.repeat(n)
      const j = s.indexOf(fence, i + n)
      if (j > 0) {
        flush()
        out.push({ t: 'code', v: s.slice(i + n, j).replace(/^ (.*) $/, '$1') })
        i = j + n
        continue
      }
      buf += fence
      i += n
      continue
    }
    if (ch === '!' && s[i + 1] === '[') {
      const m = /^!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)/.exec(s.slice(i))
      if (m) {
        flush()
        out.push({ t: 'img', alt: m[1], src: m[2] })
        i += m[0].length
        continue
      }
    }
    if (ch === '[') {
      const m = /^\[((?:[^[\]\\]|\\.|\[[^\]]*\])*)\]\(\s*<?([^)\s>]*)>?(?:\s+"[^"]*")?\s*\)/.exec(s.slice(i))
      if (m) {
        flush()
        out.push({ t: 'a', href: m[2], c: parseInline(m[1]) })
        i += m[0].length
        continue
      }
    }
    if (ch === '*' && s[i + 1] === '*' && s[i + 2] && !/\s/.test(s[i + 2])) {
      let j = findClose(s, i + 2, '**')
      while (j > 0 && s[j + 2] === '*') j++ // '***' closes an inner *italic* first
      if (j > i + 2) {
        flush()
        out.push({ t: 'b', c: parseInline(s.slice(i + 2, j)) })
        i = j + 2
        continue
      }
    }
    if (ch === '*' && s[i + 1] && s[i + 1] !== '*' && !/\s/.test(s[i + 1])) {
      const j = findClose(s, i + 1, '*')
      if (j > i + 1 && s[j + 1] !== '*') {
        flush()
        out.push({ t: 'i', c: parseInline(s.slice(i + 1, j)) })
        i = j + 1
        continue
      }
    }
    if (ch === '_' && (i === 0 || /[\s(["'—–-]/.test(s[i - 1])) && s[i + 1] && !/[\s_]/.test(s[i + 1])) {
      let k = i + 1
      let j = -1
      while (k < s.length) {
        const c = s.indexOf('_', k)
        if (c < 0) break
        if (!/\s/.test(s[c - 1]) && (c + 1 >= s.length || !/[\p{L}\p{N}_]/u.test(s[c + 1]))) {
          j = c
          break
        }
        k = c + 1
      }
      if (j > i + 1) {
        flush()
        out.push({ t: 'i', c: parseInline(s.slice(i + 1, j)) })
        i = j + 1
        continue
      }
    }
    buf += ch
    i++
  }
  flush()
  return out
}

/** Plain text of an inline tree (search index, heading ids, alt text). */
export function inlineText(inl: Inline[]): string {
  return inl
    .map((x) => (x.t === 'text' || x.t === 'code' ? x.v : x.t === 'img' ? x.alt : inlineText(x.c)))
    .join('')
}

/* ── blocks ── */

const RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+-]*)/
const RE_HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/
const RE_HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/
const RE_LIST = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/
const RE_TABLE_SEP = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/
const RE_IMG_LINE = /^\s*!\[[^\]]*\]\([^)]+\)\s*$/

function splitRow(line: string): string[] {
  let l = line.trim()
  if (l.startsWith('|')) l = l.slice(1)
  if (l.endsWith('|') && !l.endsWith('\\|')) l = l.slice(0, -1)
  const cells: string[] = []
  let cur = ''
  let inCode = false
  for (let i = 0; i < l.length; i++) {
    const c = l[i]
    if (c === '\\' && l[i + 1] === '|') {
      cur += '|'
      i++
      continue
    }
    if (c === '`') inCode = !inCode
    if (c === '|' && !inCode) {
      cells.push(cur.trim())
      cur = ''
      continue
    }
    cur += c
  }
  cells.push(cur.trim())
  return cells
}

function isBlockStart(line: string, next: string | undefined): boolean {
  return (
    RE_FENCE.test(line) ||
    RE_HEADING.test(line) ||
    RE_HR.test(line) ||
    /^\s*>/.test(line) ||
    RE_LIST.test(line) ||
    RE_IMG_LINE.test(line) ||
    (line.includes('|') && next !== undefined && RE_TABLE_SEP.test(next) && next.includes('-'))
  )
}

function indentOf(s: string): number {
  let n = 0
  for (const c of s) {
    if (c === ' ') n++
    else if (c === '\t') n += 4
    else break
  }
  return n
}

interface Ctx {
  slug: (t: string) => string
}

function parseList(lines: string[], start: number): [ListBlock, number] {
  const first = RE_LIST.exec(lines[start])!
  const baseIndent = indentOf(first[1])
  const ordered = /\d/.test(first[2])
  const list: ListBlock = { t: 'list', ordered, start: ordered ? parseInt(first[2], 10) : 1, items: [] }
  let i = start
  while (i < lines.length) {
    const line = lines[i]
    if (!line.trim()) {
      // a blank line ends the list unless the next non-blank line continues it at this level or deeper
      let j = i + 1
      while (j < lines.length && !lines[j].trim()) j++
      const m = j < lines.length ? RE_LIST.exec(lines[j]) : null
      if (m && indentOf(m[1]) >= baseIndent && /\d/.test(m[2]) === ordered) {
        i = j
        continue
      }
      if (m && indentOf(m[1]) > baseIndent) {
        i = j
        continue
      }
      break
    }
    const m = RE_LIST.exec(line)
    if (m) {
      const ind = indentOf(m[1])
      if (ind < baseIndent) break
      if (ind >= baseIndent + 2) {
        const last = list.items[list.items.length - 1]
        if (!last) break
        const [sub, next] = parseList(lines, i)
        if (last.sub) last.sub.items.push(...sub.items)
        else last.sub = sub
        i = next
        continue
      }
      if (/\d/.test(m[2]) !== ordered) break
      list.items.push({ inl: parseInline(m[3]) })
      i++
      continue
    }
    // lazy / indented continuation of the last item
    if (indentOf(line) >= 1 || !isBlockStart(line, lines[i + 1])) {
      const last = list.items[list.items.length - 1]
      if (!last || isBlockStart(line.trim(), lines[i + 1])) break
      const target = last.sub ? last.sub.items[last.sub.items.length - 1] : last
      target.inl = [...target.inl, { t: 'text', v: ' ' }, ...parseInline(line.trim())]
      i++
      continue
    }
    break
  }
  return [list, i]
}

function parseLines(lines: string[], ctx: Ctx): Block[] {
  const out: Block[] = []
  let para: string[] = []
  const flush = () => {
    if (para.length) out.push({ t: 'p', inl: parseInline(para.join(' ')) })
    para = []
  }
  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    const tr = line.trim()
    if (!tr) {
      flush()
      i++
      continue
    }
    const fence = RE_FENCE.exec(line)
    if (fence) {
      flush()
      const marker = fence[1][0]
      const len = fence[1].length
      const buf: string[] = []
      i++
      while (i < lines.length) {
        const f = /^ {0,3}(`{3,}|~{3,})\s*$/.exec(lines[i])
        if (f && f[1][0] === marker && f[1].length >= len) break
        buf.push(lines[i])
        i++
      }
      out.push({ t: 'pre', lang: fence[2] || '', v: buf.join('\n') })
      i++
      continue
    }
    const h = RE_HEADING.exec(line)
    if (h) {
      flush()
      const inl = parseInline(h[2])
      const text = inlineText(inl)
      out.push({ t: 'h', level: Math.min(4, h[1].length) as 1 | 2 | 3 | 4, id: ctx.slug(text), text, inl })
      i++
      continue
    }
    if (RE_HR.test(line)) {
      flush()
      out.push({ t: 'hr' })
      i++
      continue
    }
    if (tr.startsWith('>')) {
      flush()
      const buf: string[] = []
      while (i < lines.length && lines[i].trim().startsWith('>')) {
        buf.push(lines[i].trim().replace(/^>\s?/, ''))
        i++
      }
      out.push({ t: 'quote', blocks: parseLines(buf, ctx) })
      continue
    }
    if (line.includes('|') && i + 1 < lines.length && RE_TABLE_SEP.test(lines[i + 1]) && lines[i + 1].includes('-')) {
      flush()
      const head = splitRow(line)
      const align: Align[] = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : c.startsWith(':') ? 'left' : null,
      )
      i += 2
      const rows: Inline[][][] = []
      while (i < lines.length && lines[i].trim() && lines[i].includes('|')) {
        const cells = splitRow(lines[i])
        rows.push(head.map((_, k) => parseInline(cells[k] ?? '')))
        i++
      }
      out.push({ t: 'table', align: head.map((_, k) => align[k] ?? null), head: head.map((c) => parseInline(c)), rows })
      continue
    }
    if (RE_LIST.test(line)) {
      flush()
      const [list, next] = parseList(lines, i)
      out.push(list)
      i = next
      continue
    }
    if (RE_IMG_LINE.test(line) && !para.length) {
      const m = /!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?/.exec(line)!
      out.push({ t: 'img', alt: m[1], src: m[2] })
      i++
      continue
    }
    if (para.length && isBlockStart(line, lines[i + 1])) {
      flush()
      continue
    }
    para.push(tr)
    i++
  }
  flush()
  return out
}

/** Parse a Markdown document into blocks; heading ids are unique within the document. */
export function parseGuideMarkdown(md: string): Block[] {
  const text = (md ?? '').replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, (m) => '\n'.repeat((m.match(/\n/g) ?? []).length))
  const slugger = makeSlugger()
  return parseLines(text.split('\n'), { slug: slugger })
}

/* ── links ── */

export type LinkTarget =
  | { kind: 'external'; href: string }
  | { kind: 'guide'; chapter: string | null; anchor: string | null }
  | { kind: 'none' }

/**
 * Resolve a link found in chapter `chapter` (file name without .md): external URLs open in a new tab, guide links
 * (`x.md#a`, `#a`, `README.md`) stay in the Help page, anything else (relative repo paths) is not a link in the app.
 */
export function resolveLink(href: string): LinkTarget {
  const h = href.trim()
  if (/^(https?:|mailto:)/i.test(h)) return { kind: 'external', href: h }
  if (h.startsWith('#')) return { kind: 'guide', chapter: null, anchor: decodeURIComponent(h.slice(1)) || null }
  const m = /^(?:\.\/)?([\w.-]+)\.md(?:#(.*))?$/i.exec(h)
  if (m) return { kind: 'guide', chapter: m[1], anchor: m[2] ? decodeURIComponent(m[2]) : null }
  return { kind: 'none' }
}
