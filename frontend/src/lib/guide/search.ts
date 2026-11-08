/**
 * Full-text search over the guide sections (lib/guide/toc.ts buildSections). Every query term must occur in the
 * section (heading, trail or text; prefix match for terms of 3+ characters); hits are ranked by where and how often
 * the terms occur, with a bonus for the exact phrase. Returns a snippet around the first occurrence.
 */
import type { Section } from './toc'

export interface SearchHit {
  section: Section
  score: number
  /** snippet split into plain / highlighted runs */
  snippet: { text: string; hit: boolean }[]
}

export function normalise(s: string): string {
  return s
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[̀-ͯ]/g, '')
}

export function tokenize(q: string): string[] {
  const toks = normalise(q)
    .split(/[^\p{L}\p{N}.+#/_-]+/u)
    .map((t) => t.replace(/^[.\-/_]+|[.\-/_]+$/g, ''))
    .filter((t) => t.length >= 2 || /\d/.test(t))
  return [...new Set(toks)]
}

/** normalise() character by character, keeping the index of the source character of every output character. */
function normMap(s: string): { out: string; idx: number[] } {
  let out = ''
  const idx: number[] = []
  for (let i = 0; i < s.length; i++) {
    for (const ch of normalise(s[i])) {
      out += ch
      idx.push(i)
    }
  }
  return { out, idx }
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function termRe(t: string): RegExp {
  // word-start match; short terms must match a whole word
  const tail = t.length >= 3 ? '' : '(?![\\p{L}\\p{N}])'
  return new RegExp(`(?<![\\p{L}\\p{N}])${escapeRe(t)}${tail}`, 'gu')
}

function countMatches(re: RegExp, s: string, cap = 20): number {
  re.lastIndex = 0
  let n = 0
  while (re.exec(s) && n < cap) n++
  return n
}

export function makeSnippet(text: string, terms: string[], width = 180): { text: string; hit: boolean }[] {
  const norm = normalise(text)
  let first = -1
  for (const t of terms) {
    const re = termRe(t)
    re.lastIndex = 0
    const m = re.exec(norm)
    if (m && (first < 0 || m.index < first)) first = m.index
  }
  const start = first < 0 ? 0 : Math.max(0, first - Math.floor(width / 3))
  const end = Math.min(text.length, start + width)
  let cut = text.slice(start, end).replace(/\s+/g, ' ')
  if (start > 0) cut = '…' + cut
  if (end < text.length) cut += '…'
  if (!terms.length) return [{ text: cut, hit: false }]
  const re = new RegExp(terms.map((t) => `(?<![\\p{L}\\p{N}])${escapeRe(t)}`).join('|'), 'gu')
  const { out: ncut, idx } = normMap(cut)
  const runs: { text: string; hit: boolean }[] = []
  let last = 0
  for (const m of ncut.matchAll(re)) {
    const i = idx[m.index ?? 0]
    const j = idx[(m.index ?? 0) + m[0].length - 1] + 1
    if (i < last) continue
    if (i > last) runs.push({ text: cut.slice(last, i), hit: false })
    runs.push({ text: cut.slice(i, j), hit: true })
    last = j
  }
  if (last < cut.length) runs.push({ text: cut.slice(last), hit: false })
  return runs
}

export function searchSections(sections: Section[], query: string, limit = 40): SearchHit[] {
  const terms = tokenize(query)
  if (!terms.length) return []
  const phrase = normalise(query.trim())
  const res: SearchHit[] = []
  for (const s of sections) {
    const head = normalise(s.heading)
    const trail = normalise(s.trail.join(' '))
    const body = normalise(s.text)
    let score = 0
    let ok = true
    for (const t of terms) {
      const re = termRe(t)
      const h = countMatches(re, head)
      const tr = countMatches(re, trail)
      const b = countMatches(re, body)
      if (!h && !tr && !b) {
        ok = false
        break
      }
      score += h * 8 + tr * 2 + Math.min(b, 10)
    }
    if (!ok) continue
    if (terms.length > 1 && phrase.length > 3) {
      if (head.includes(phrase)) score += 25
      else if (body.includes(phrase)) score += 10
    }
    if (!s.heading) score -= 1
    res.push({ section: s, score, snippet: makeSnippet(s.text || s.heading, terms) })
  }
  return res.sort((a, b) => b.score - a.score).slice(0, limit)
}
