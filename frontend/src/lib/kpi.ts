import type { KpiDefinition } from '@/api/types'

/** Card subtitle for a KPI distribution (guide 03: band metrics used to read "Higher is better"). */
export function kpiDirectionLabel(k: Pick<KpiDefinition, 'higherIsBetter' | 'target' | 'unit'>): string {
  if (typeof k.target === 'number' && Number.isFinite(k.target)) {
    const t = k.unit === 'pct' ? `${Number((k.target * 100).toFixed(1))}%` : k.unit === 'multiple' ? `${k.target}x` : String(k.target)
    return `Closest to ${t} is best`
  }
  if (k.higherIsBetter === null) return 'No better / worse direction'
  return k.higherIsBetter ? 'Higher is better' : 'Lower is better'
}
