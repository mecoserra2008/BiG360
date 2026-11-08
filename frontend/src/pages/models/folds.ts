/** Walk-forward fold helpers for the Model Lab subtitles (guide 10: the folds are 24 months, not 12). */

/** Whole months between two ISO dates (fold test length), null when unknown. */
export function monthsBetween(a: string, b: string): number | null {
  const x = Date.parse(a)
  const y = Date.parse(b)
  if (!Number.isFinite(x) || !Number.isFinite(y) || y <= x) return null
  return Math.round((y - x) / (86400000 * 30.4375))
}

/** Test-fold length of a walk-forward run: the most common length of its full folds (the last one may be partial). */
export function foldTestMonths(folds: { testStart: string; testEnd: string }[]): number | null {
  const n = folds.map((f) => monthsBetween(f.testStart, f.testEnd)).filter((v): v is number => v !== null)
  if (!n.length) return null
  const cnt = new Map<number, number>()
  for (const v of n) cnt.set(v, (cnt.get(v) ?? 0) + 1)
  return [...cnt.entries()].sort((p, q) => q[1] - p[1] || q[0] - p[0])[0][0]
}
