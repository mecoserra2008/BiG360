import { Link } from 'react-router'
import { APP_LABEL, VALIDATION_ROUTE, headlineCoverage } from '@/api/conformal/adapters'
import { useConformalCoverage } from '@/api/conformal/hooks'
import type { CfCoverageRow } from '@/api/conformal/types'
import { USE_MOCK } from '@/api/http'
import { Badge } from '@/components/ui/badge'
import { ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { fmtDate, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'

const STATUS_TONE: Record<string, 'up' | 'warn' | 'down' | 'neutral'> = { ok: 'up', over: 'neutral', under: 'down', thin: 'warn' }

function horizonLabel(r: CfCoverageRow): string {
  if (r.horizon === 'joint') return r.application.startsWith('nextbar') ? 'next bar (H/L/C)' : 'joint'
  if (r.horizon === 'member') return 'per member'
  const h = r.horizon.replace(/^m/, '')
  return `${h} session${h === '1' ? '' : 's'}`
}

export function CoverageTable({ rows }: { rows: CfCoverageRow[] }) {
  return (
    <div className="overflow-x-auto"><table className="w-full text-sm">
      <thead>
        <tr className="text-2xs tracking-wide text-muted uppercase">
          <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Set</th>
          <th scope="col" className="border-b border-border px-2 text-left font-semibold">Horizon</th>
          <th scope="col" className="border-b border-border px-2 text-right font-semibold">Target</th>
          <th scope="col" className="border-b border-border px-2 text-right font-semibold">Realised</th>
          <th scope="col" className="border-b border-border px-2 text-right font-semibold">Sets audited</th>
          <th scope="col" className="border-b border-border px-3 text-left font-semibold">Status</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={`${r.application}|${r.filter}|${r.segment}|${r.horizon}|${r.level}`} className="hover:bg-surface-2">
            <td className="h-[26px] border-b border-border px-3">{APP_LABEL[r.application] ?? r.application} <span className="text-2xs text-faint">{r.filter}</span></td>
            <td className="border-b border-border px-2">{horizonLabel(r)}</td>
            <td className="num border-b border-border px-2 text-right">{r.target == null ? '—' : `${Math.round(r.target * 100)}%`}</td>
            <td className={cn('num border-b border-border px-2 text-right font-semibold', r.status === 'under' && 'text-down')}>{r.coverage == null ? '—' : `${(r.coverage * 100).toFixed(1)}%`}</td>
            <td className="num border-b border-border px-2 text-right text-muted">{fmtInt(r.n)}</td>
            <td className="border-b border-border px-3"><Badge tone={STATUS_TONE[r.status] ?? 'neutral'}>{r.status}</Badge></td>
          </tr>
        ))}
      </tbody>
    </table></div>
  )
}

/** Data Health: realised coverage of the conformal sets at 90% (rolling audit + walk-forward) and flagged segments. */
export function ConformalCoveragePanel({ className }: { className?: string }) {
  const q = useConformalCoverage(false, !USE_MOCK)
  if (USE_MOCK) return null
  const d = q.data
  return (
    <Panel
      title="Conformal coverage (model-based sets)"
      subtitle={`Realised vs target coverage at 90% · audit ${fmtDate(d?.as_of)} · flags: overall < −1.5 pp or a segment < −5 pp (n ≥ 2000)`}
      className={className}
      flush
      actions={<Link to={VALIDATION_ROUTE} className="text-xs text-accent-text hover:underline">Validation & full audit →</Link>}
    >
      {q.isPending ? (
        <SkeletonRows className="p-3" rows={5} />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !d || d.data_status === 'pending' ? (
        <PendingState compact detail="No coverage audit in the serving snapshot yet (job conformal_coverage)." />
      ) : (
        <>
          <CoverageTable rows={headlineCoverage(d.rows)} />
          <div className="border-t border-border px-3 py-2 text-xs">
            {d.flags.length === 0 ? (
              <span className="text-muted">No under-coverage flags in the rolling audit.</span>
            ) : (
              <span className="text-warn">
                {d.flags.length} flagged segment{d.flags.length > 1 ? 's' : ''}:{' '}
                {d.flags.slice(0, 6).map((f) => `${f.application} ${f.segment_type}=${f.segment} ${f.horizon} ${f.coverage == null ? '' : `${(f.coverage * 100).toFixed(1)}%`}`).join(' · ')}
              </span>
            )}
          </div>
        </>
      )}
    </Panel>
  )
}
