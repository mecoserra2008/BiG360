/** Regression-guard panel helpers (O2.3). */
export function qaTone(s: string | null | undefined): 'up' | 'warn' | 'down' | 'neutral' {
  return s === 'passed' ? 'up' : s === 'warn' ? 'warn' : s === 'failed' ? 'down' : 'neutral'
}

/** "137 passed · 2 warn · 11 skipped" (stable order) */
export function qaCounts(c: Record<string, number> | undefined | null): string {
  if (!c) return ''
  return ['passed', 'warn', 'failed', 'skipped']
    .filter((k) => (c[k] ?? 0) > 0)
    .map((k) => `${c[k]} ${k}`)
    .join(' · ')
}
