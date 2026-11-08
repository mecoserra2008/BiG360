import type { RunStatus } from '@/api/types'
import { Badge } from './badge'

const MAP: Record<RunStatus, { tone: 'up' | 'down' | 'info' | 'neutral' | 'warn'; icon: string }> = {
  success: { tone: 'up', icon: '✓' },
  failed: { tone: 'down', icon: '✕' },
  running: { tone: 'info', icon: '◔' },
  queued: { tone: 'neutral', icon: '…' },
  skipped: { tone: 'neutral', icon: '↷' },
  partial: { tone: 'warn', icon: '◐' },
}

/** Job / run status: icon + word + tint (never colour alone). */
export function StatusBadge({ status }: { status: RunStatus | 'fresh' | 'stale' | 'error' }) {
  const m =
    status === 'fresh' ? MAP.success : status === 'stale' ? { tone: 'warn' as const, icon: '!' } : status === 'error' ? MAP.failed : MAP[status]
  return (
    <Badge tone={m.tone}>
      <span aria-hidden>{m.icon}</span> {status}
    </Badge>
  )
}
