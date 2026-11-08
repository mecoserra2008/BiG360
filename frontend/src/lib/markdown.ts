/**
 * Minimal, safe Markdown subset for thesis text / journal notes: headings (#..###), bullet and numbered lists,
 * block quotes, fenced code, horizontal rules, paragraphs; inline **bold**, *italic* / _italic_, `code` and
 * [links](https://...) (http / https / mailto only). Produces a token tree rendered with React elements (no HTML
 * injection).
 */

export type Inline = { t: 'text'; v: string } | { t: 'b' | 'i' | 'code'; v: string } | { t: 'a'; v: string; href: string }

export type Block =
  | { t: 'h'; level: 1 | 2 | 3; inl: Inline[] }
  | { t: 'p'; inl: Inline[] }
  | { t: 'ul' | 'ol'; items: Inline[][] }
  | { t: 'quote'; inl: Inline[] }
  | { t: 'pre'; v: string }
  | { t: 'hr' }

const SAFE_URL = /^(https?:\/\/|mailto:)/i
const INLINE = /(\*\*[^*]+\*\*)|(`[^`]+`)|(\[[^\]]+\]\([^)\s]+\))|(\*[^*\s][^*]*\*)|(_[^_\s][^_]*_)/g

export function parseInline(s: string): Inline[] {
  const out: Inline[] = []
  let last = 0
  for (const m of s.matchAll(INLINE)) {
    const i = m.index ?? 0
    if (i > last) out.push({ t: 'text', v: s.slice(last, i) })
    const tok = m[0]
    if (tok.startsWith('**')) out.push({ t: 'b', v: tok.slice(2, -2) })
    else if (tok.startsWith('`')) out.push({ t: 'code', v: tok.slice(1, -1) })
    else if (tok.startsWith('[')) {
      const mm = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(tok)
      if (mm && SAFE_URL.test(mm[2])) out.push({ t: 'a', v: mm[1], href: mm[2] })
      else out.push({ t: 'text', v: mm ? mm[1] : tok })
    } else out.push({ t: 'i', v: tok.slice(1, -1) })
    last = i + tok.length
  }
  if (last < s.length) out.push({ t: 'text', v: s.slice(last) })
  return out
}

export function parseMarkdown(md: string): Block[] {
  const lines = (md ?? '').replace(/\r\n?/g, '\n').split('\n')
  const out: Block[] = []
  let para: string[] = []
  const flush = () => {
    if (para.length) out.push({ t: 'p', inl: parseInline(para.join(' ')) })
    para = []
  }
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    const tr = l.trim()
    if (tr.startsWith('```')) {
      flush()
      const buf: string[] = []
      i++
      while (i < lines.length && !lines[i].trim().startsWith('```')) buf.push(lines[i++])
      out.push({ t: 'pre', v: buf.join('\n') })
      continue
    }
    if (!tr) {
      flush()
      continue
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(tr)
    if (h) {
      flush()
      out.push({ t: 'h', level: Math.min(3, h[1].length) as 1 | 2 | 3, inl: parseInline(h[2]) })
      continue
    }
    if (/^(-{3,}|\*{3,})$/.test(tr)) {
      flush()
      out.push({ t: 'hr' })
      continue
    }
    const ul = /^[-*+]\s+(.*)$/.exec(tr)
    const ol = /^\d+[.)]\s+(.*)$/.exec(tr)
    if (ul || ol) {
      flush()
      const kind = ul ? 'ul' : 'ol'
      const items: Inline[][] = []
      let j = i
      while (j < lines.length) {
        const m = kind === 'ul' ? /^[-*+]\s+(.*)$/.exec(lines[j].trim()) : /^\d+[.)]\s+(.*)$/.exec(lines[j].trim())
        if (!m) break
        items.push(parseInline(m[1]))
        j++
      }
      out.push({ t: kind, items })
      i = j - 1
      continue
    }
    if (tr.startsWith('>')) {
      flush()
      out.push({ t: 'quote', inl: parseInline(tr.replace(/^>\s?/, '')) })
      continue
    }
    para.push(tr)
  }
  flush()
  return out
}
