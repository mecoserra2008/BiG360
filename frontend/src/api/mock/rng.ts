/** Deterministic PRNG utilities for the mock layer (mulberry32 + FNV-1a string hashing). */

export function hashString(s: string): number {
  let h = 2166136261 >>> 0
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export class Rng {
  private s: number
  constructor(seed: number | string) {
    this.s = (typeof seed === 'string' ? hashString(seed) : seed) >>> 0 || 0x9e3779b9
  }
  next(): number {
    let t = (this.s = (this.s + 0x6d2b79f5) >>> 0)
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
  range(lo: number, hi: number): number {
    return lo + (hi - lo) * this.next()
  }
  int(lo: number, hi: number): number {
    return Math.floor(this.range(lo, hi + 1))
  }
  normal(mu = 0, sigma = 1): number {
    const u = 1 - this.next()
    const v = this.next()
    return mu + sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
  }
  pick<T>(xs: readonly T[]): T {
    return xs[Math.floor(this.next() * xs.length)]
  }
  chance(p: number): boolean {
    return this.next() < p
  }
  shuffle<T>(xs: readonly T[]): T[] {
    const a = [...xs]
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(this.next() * (i + 1))
      const t = a[i]
      a[i] = a[j]
      a[j] = t
    }
    return a
  }
}

/** Fixed "today" for the mock world so screenshots are reproducible. */
export const MOCK_AS_OF = '2026-10-09'

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

/** Business days (Mon–Fri) ending at `end`, oldest first. */
export function businessDays(end: string, count: number): string[] {
  const out: string[] = []
  const d = new Date(`${end}T00:00:00Z`)
  while (out.length < count) {
    const wd = d.getUTCDay()
    if (wd !== 0 && wd !== 6) out.push(d.toISOString().slice(0, 10))
    d.setUTCDate(d.getUTCDate() - 1)
  }
  return out.reverse()
}

/** Month-end dates, oldest first, last one being the month-end on/before `end`'s month. */
export function monthEnds(end: string, count: number): string[] {
  const out: string[] = []
  const d = new Date(`${end}T00:00:00Z`)
  for (let i = 0; i < count; i++) {
    const m = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() - i, 0))
    out.push(m.toISOString().slice(0, 10))
  }
  return out.reverse()
}

export const round = (v: number, d = 2): number => {
  const f = 10 ** d
  return Math.round(v * f) / f
}

/** Memoize a 1-arg generator (mock payloads are pure functions of their arguments). */
export function memo<A extends string | number, R>(fn: (a: A) => R): (a: A) => R {
  const m = new Map<A, R>()
  return (a: A) => {
    let r = m.get(a)
    if (r === undefined) {
      r = fn(a)
      m.set(a, r)
    }
    return r
  }
}
