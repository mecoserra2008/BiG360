import type { components } from '@/api/generated/schema'
import { ageText } from '@/api/ops/adapters'
import { Badge } from '@/components/ui/badge'
import { Panel } from '@/components/ui/panel'
import { fmtDateTime } from '@/lib/format'
import { qaCounts, qaTone } from './qa'

type QaReport = components['schemas']['QaReport']
type QaSuite = components['schemas']['QaSuite']

const SUITE_LABEL: Record<string, string> = { api: 'API contract sweep', golden: 'Golden data checks', smoke: 'UI smoke suite' }
const TRIGGER_LABEL: Record<string, string> = { sync: 'after a sync publish', refresh: 'after the scheduled refresh', manual: 'manual run' }

/** Data Health: latest regression-guard run (O2.3, v1.30 DataHealth.qa). Older APIs send no `qa` → nothing rendered.
 * v1.43 (alert hygiene 2026-10-30): the state is merged per suite - each suite shows its own run age, and results
 * a newer run superseded or that aged out are marked stale (their failures no longer raise error alerts). */
export function QaPanel({ qa, className }: { qa: QaReport | null | undefined; className?: string }) {
  if (!qa) return null
  const suites = Object.entries(qa.suites ?? {}) as [string, QaSuite][]
  const skipped = Object.entries(qa.skipped ?? {})
  const age = ageText(qa.age_h)
  return (
    <Panel
      title="Regression guard"
      subtitle="API contract sweep and golden data checks after every sync that publishes a snapshot; the UI smoke suite at most once a day and after the weekly refresh (job qa_regression)"
      className={className}
      actions={
        <span className="inline-flex items-center gap-1">
          {qa.stale ? <Badge tone="neutral">stale</Badge> : null}
          <Badge tone={qaTone(qa.status)}>{qa.status}</Badge>
        </span>
      }
    >
      {qa.status === 'pending' ? (
        <p className="text-xs text-muted">Not run yet. It runs after the next sync that publishes a snapshot, or by hand with <span className="num">python -m app.qa</span> (backend/).</p>
      ) : (
        <>
          <p className="mb-2 text-2xs text-muted">
            Last run {qa.finished_at ? fmtDateTime(qa.finished_at) : '—'}
            {age ? <span className="num"> ({age})</span> : null}
            {qa.trigger ? ` · ${TRIGGER_LABEL[qa.trigger] ?? qa.trigger}` : ''} · {qa.client === 'in_process' ? 'in-process API (no server running)' : (qa.api_base_url ?? 'live API')}
          </p>
          <table className="w-full text-xs" aria-label="Regression guard suites">
            <tbody>
              {suites.map(([id, s]) => (
                <tr key={id} className="border-b border-border last:border-b-0">
                  <th scope="row" className="py-1 pr-2 text-left font-medium">{SUITE_LABEL[id] ?? id}</th>
                  <td className="py-1 pr-2">
                    <Badge tone={s.stale && s.status !== 'passed' ? 'neutral' : qaTone(s.status)}>{s.status}</Badge>
                    {s.stale ? <span className="ml-1 text-2xs text-faint" title={s.superseded_by ? `A newer QA run (${s.superseded_by}) did not re-run this suite` : 'Older than this suite’s re-check interval'}>stale</span> : null}
                  </td>
                  <td className="num py-1 pr-2 text-muted">{qaCounts(s.counts)}</td>
                  <td className="num py-1 pr-2 text-2xs text-faint">{ageText(s.age_h)}</td>
                  <td className="py-1 text-muted">{s.message ?? (s.served_version && s.contract_version && s.served_version !== s.contract_version ? `API serves ${s.served_version}, contract ${s.contract_version}` : '')}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {skipped.length > 0 && (
            <p className="mt-1 text-2xs text-muted">
              {skipped.map(([k, why]) => `${SUITE_LABEL[k] ?? k} not run in the latest run: ${why}`).join(' · ')}
            </p>
          )}
          {qa.issues.length > 0 && (
            <ul className="mt-2 space-y-1 text-2xs" aria-label="Failed checks">
              {qa.issues.slice(0, 10).map((i, k) => (
                <li key={`${i.suite}-${i.item}-${k}`} className="flex gap-2">
                  <Badge tone={qa.suites?.[i.suite]?.stale ? 'neutral' : 'down'} className="h-4 shrink-0 px-1">{i.suite}</Badge>
                  <span className="min-w-0 break-words"><span className="font-medium text-fg">{i.item}</span>{i.detail ? <span className="text-muted"> · {i.detail}</span> : null}</span>
                </li>
              ))}
              {qa.issues.length > 10 && <li className="text-muted">+{qa.issues.length - 10} more in the report</li>}
            </ul>
          )}
          {qa.report && <p className="mt-2 text-2xs text-faint">Report: <span className="num break-all">{qa.report}</span></p>}
        </>
      )}
    </Panel>
  )
}
