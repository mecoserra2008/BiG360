import { CircleAlert, Info, OctagonAlert, Play, X } from 'lucide-react'
import type { LintIssueView, LintSeverity, LintView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { fmtStat } from '../fmt'

const SEV: Record<LintSeverity, { tone: 'down' | 'warn' | 'info'; label: string; box: string; Icon: typeof Info }> = {
  error: { tone: 'down', label: 'error', box: 'border-down/50 bg-down-soft', Icon: OctagonAlert },
  warning: { tone: 'warn', label: 'warning', box: 'border-warn/40 bg-warn-soft', Icon: CircleAlert },
  info: { tone: 'info', label: 'info', box: 'border-border bg-surface-2', Icon: Info },
}

function lintCounts(l: LintView): string {
  const n = (s: LintSeverity) => l.issues.filter((i) => i.severity === s).length
  const parts = [
    [n('error'), 'error'],
    [n('warning'), 'warning'],
    [n('info'), 'note'],
  ] as const
  return (
    parts
      .filter(([k]) => k > 0)
      .map(([k, w]) => `${k} ${w}${k === 1 ? '' : 's'}`)
      .join(' · ') || 'no issues'
  )
}

/** Severity-coloured list of pre-run lint issues with the suggested fix. */
export function LintIssues({ issues, className }: { issues: LintIssueView[]; className?: string }) {
  if (!issues.length) return <p className={cn('text-xs text-up', className)}>No look-ahead, leakage, survivorship or sample-size issues found.</p>
  return (
    <ul className={cn('space-y-1.5', className)} aria-label="Pre-run check issues">
      {issues.map((i, k) => {
        const s = SEV[i.severity]
        return (
          <li key={`${i.code}-${k}`} className={cn('flex items-start gap-2 rounded-md border px-2 py-1.5 text-xs', s.box)}>
            <s.Icon className={cn('mt-0.5 size-3.5 shrink-0', i.severity === 'error' ? 'text-down' : i.severity === 'warning' ? 'text-warn' : 'text-info')} aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-1">
                <Badge tone={s.tone}>{s.label}</Badge>
                <span className="num text-2xs text-muted">{i.code.replace(/_/g, ' ')}</span>
                {i.columns.slice(0, 6).map((c) => (
                  <Badge key={c} tone="neutral" className="normal-case">
                    {c}
                  </Badge>
                ))}
              </div>
              <div className="mt-0.5 text-fg">{i.message}</div>
              {i.fix && (
                <div className="mt-0.5 text-muted">
                  <span className="font-semibold">Fix:</span> {i.fix}
                </div>
              )}
            </div>
          </li>
        )
      })}
    </ul>
  )
}

/** Rows / entities the lint expects the dataset to have. */
export function LintSize({ lint }: { lint: LintView }) {
  if (lint.estimatedRows === null && lint.nEntities === null) return null
  return (
    <span className="num text-2xs text-muted">
      {lint.estimatedRows !== null ? `≈ ${fmtStat(lint.estimatedRows)} rows` : ''}
      {lint.estimatedRows !== null && lint.nEntities !== null ? ' · ' : ''}
      {lint.nEntities !== null ? `${fmtStat(lint.nEntities)} entities` : ''}
    </span>
  )
}

/**
 * Pre-run check result shown above the results: blocking (errors → "Run anyway" / "Cancel")
 * or informational (warnings / notes, dismissable; the run already started).
 */
export function LintPanel({ lint, blocked, onRunAnyway, onDismiss, running }: { lint: LintView; blocked: boolean; onRunAnyway: () => void; onDismiss: () => void; running: boolean }) {
  return (
    <section role={blocked ? 'alert' : 'status'} aria-label="Pre-run check" className={cn('rounded-md border px-3 py-2', blocked ? 'border-down/50 bg-surface' : 'border-border bg-surface')}>
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <h3 className={cn('text-sm font-semibold', blocked ? 'text-down' : 'text-fg')}>{blocked ? 'Pre-run check found blocking issues' : 'Pre-run check'}</h3>
        <span className="text-xs text-muted">{lintCounts(lint)}</span>
        <LintSize lint={lint} />
        <div className="ml-auto flex gap-1.5">
          {blocked ? (
            <>
              <Button size="xs" variant="danger" onClick={onRunAnyway} disabled={running} title="Submit despite the errors (look-ahead errors are still rejected by the engine)">
                <Play /> Run anyway
              </Button>
              <Button size="xs" variant="ghost" onClick={onDismiss}>
                Cancel
              </Button>
            </>
          ) : (
            <Button size="icon-sm" variant="ghost" aria-label="Dismiss pre-run check" onClick={onDismiss}>
              <X />
            </Button>
          )}
        </div>
      </div>
      <LintIssues issues={lint.issues} />
      {blocked && <p className="mt-1.5 text-2xs text-muted">Fix the study in the builder and run again. Look-ahead errors are rejected by the engine even with “Run anyway”.</p>}
    </section>
  )
}
