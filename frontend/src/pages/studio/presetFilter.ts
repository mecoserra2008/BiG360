import type { StudioPresetView } from '@/api/studio/types'

export const groupLabel = (g: string) => g.charAt(0).toUpperCase() + g.slice(1).replace(/_/g, ' ')

/** v1.50 recipe filter: question group + free text over title / description / method note / tags. */
export function filterPresets(presets: StudioPresetView[], group: string, q: string): StudioPresetView[] {
  const t = q.trim().toLowerCase()
  return presets.filter((p) => (!group || (p.question ?? 'other') === group) && (!t || [p.title, p.description, p.methodNote ?? '', p.method, ...p.tags].join(' ').toLowerCase().includes(t)))
}
