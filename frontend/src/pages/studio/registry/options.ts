import type { ModelStage, MonitorSchedule } from '@/api/studio/types'

export const STAGE_OPTIONS: { value: ModelStage; label: string }[] = [
  { value: 'candidate', label: 'Candidate' },
  { value: 'production', label: 'Production' },
  { value: 'archived', label: 'Archived' },
]

export const MONITOR_OPTIONS: { value: MonitorSchedule; label: string }[] = [
  { value: 'none', label: 'No scheduled monitoring' },
  { value: 'weekly', label: 'Monitor weekly' },
  { value: 'monthly', label: 'Monitor monthly' },
]

/** Text input → number (blank / invalid = null). */
export const numOrNull = (s: string): number | null => (s.trim() === '' || !Number.isFinite(Number(s)) ? null : Number(s))

export const STAGE_TONE: Record<ModelStage, 'up' | 'accent' | 'neutral'> = { production: 'up', candidate: 'accent', archived: 'neutral' }
