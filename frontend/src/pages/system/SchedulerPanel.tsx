import { CalendarClock, HeartPulse, TriangleAlert } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import type { SystemStatus } from '@/api/types'
import { lastRefreshSource, sortedRegions } from '@/api/ops/adapters'
import { useSchedule, useSetSchedule } from '@/api/ops/hooks'
import type { OpsAlert, ScheduleProfileId } from '@/api/ops/types'
import { USE_MOCK } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { fmtDate, fmtDateTime } from '@/lib/format'

const DAG_ORDER = ['nightly', 'weekly', 'monthly', 'cftc_weekly']

/** Red / amber banners for ops alerts (scheduler off, refresh overdue, SLA breaches ...). */
export function OpsAlertBanner({ alerts }: { alerts: OpsAlert[] }) {
  const shown = alerts.filter((a) => a.severity !== 'info')
  if (!shown.length) return null
  return (
    <div className="space-y-1.5" role="status">
      {shown.map((a) => (
        <div
          key={a.id}
          className={
            a.severity === 'error'
              ? 'flex items-start gap-2 rounded-md border border-down/40 bg-down-soft px-3 py-2 text-sm text-down'
              : 'flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-sm text-warn'
          }
        >
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="min-w-0">
            <span className="font-semibold">{a.title}</span>
            {a.detail ? <span className="text-fg/80"> — {a.detail}</span> : null}
            {a.since ? <span className="num text-fg/60"> · since {fmtDateTime(a.since)}</span> : null}
            {a.stale ? <span className="text-fg/60"> · stale result</span> : null}
          </div>
          {a.link && (
            <Link to={a.link} className="ml-auto shrink-0 text-xs underline">
              Open
            </Link>
          )}
        </div>
      ))}
    </div>
  )
}

/** Scheduler state (F01 / F03): on/off, profile, next run and last completed run per DAG, data as-of per region. */
export function SchedulerPanel({ d, className }: { d: SystemStatus; className?: string }) {
  const sch = d.scheduler
  const regions = sortedRegions(d.dataAsOf ?? {})
  const dags = DAG_ORDER.filter((x) => sch?.next_runs?.[x] !== undefined || sch?.last_success?.[x] !== undefined)
  return (
    <Panel
      title="Scheduler"
      subtitle={sch ? `${sch.profile_label ?? sch.profile} profile · ${sch.timezone ?? ''}` : 'Backend without scheduler status (API < 1.28)'}
      className={className}
      actions={
        <Link to="/system/data-health" className="inline-flex items-center gap-1 text-xs text-accent-text underline">
          <HeartPulse className="size-3.5" aria-hidden /> Data health
        </Link>
      }
    >
      {!sch ? (
        <p className="text-sm text-muted">Restart the API on contract 1.28 to see the scheduler state.</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={sch.enabled ? 'up' : 'down'}>{sch.enabled ? '● scheduler on' : '○ scheduler off'}</Badge>
            <Badge tone={sch.worker_alive ? 'up' : 'down'}>{sch.worker_alive ? 'worker alive' : 'worker down'}</Badge>
            {sch.reason && <span className="text-xs text-down">{sch.reason}</span>}
            {sch.off_hours ? <span className="num text-xs text-muted">off {(sch.off_hours / 24).toFixed(1)} d</span> : null}
            {sch.catchup_due && sch.catchup_due.length > 0 && <Badge tone="warn">catch-up due: {sch.catchup_due.join(', ')}</Badge>}
          </div>
          {sch.last_refresh ? (
            <p className="text-xs">
              <span className="text-muted">Last refresh</span> <span className="num text-fg">{fmtDateTime(sch.last_refresh.at)}</span>{' '}
              <span className="text-2xs text-muted">· {lastRefreshSource(sch.last_refresh)}{sch.last_refresh.status === 'partial' ? ` (partial, ${sch.last_refresh.jobs_failed ?? 0} job(s) failed)` : ''}</span>
            </p>
          ) : null}
          <table className="w-full text-sm" aria-label="Scheduled DAGs">
            <thead>
              <tr className="text-2xs tracking-wide text-muted uppercase">
                <th scope="col" className="py-1 text-left font-semibold">DAG</th>
                <th scope="col" className="text-left font-semibold">Next run{sch.next_runs_computed ? ' (computed)' : ''}</th>
                <th scope="col" className="text-left font-semibold">Last completed</th>
              </tr>
            </thead>
            <tbody>
              {dags.map((x) => (
                <tr key={x} className="border-t border-border">
                  <th scope="row" className="py-1 text-left font-medium">{x}</th>
                  <td className="num">{sch.next_runs?.[x] ? fmtDateTime(sch.next_runs[x] as string) : <span className="text-faint">not scheduled</span>}</td>
                  <td className="num">
                    {sch.last_success?.[x] ? fmtDateTime(sch.last_success[x] as string) : <span className="text-faint">never</span>}
                    {sch.last_success_trigger?.[x] ? <span className="text-2xs text-muted"> · {sch.last_success_trigger[x]}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div>
            <div className="label-caps mb-1">Data as of (published snapshot)</div>
            {regions.length ? (
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
                {regions.map(([r, v]) => (
                  <span key={r}>
                    <span className="text-muted">{r}</span> <span className="num text-fg">{fmtDate(v)}</span>
                  </span>
                ))}
              </div>
            ) : (
              <span className="text-xs text-muted">—</span>
            )}
          </div>
          {d.servingSections && d.servingSections.length > 0 && (
            <div>
              <div className="label-caps mb-1">Snapshot sections</div>
              <div className="flex flex-wrap gap-1">
                {d.servingSections.map((s) => (
                  <Badge key={s.section} tone={s.status === 'fresh' ? 'up' : s.status === 'stale' ? 'warn' : 'down'} title={`${s.source_job ?? ''} ${s.detail ?? ''}`}>
                    {s.section} {s.data_as_of ? `· ${s.data_as_of.slice(5)}` : ''}
                  </Badge>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}

/** Settings › schedule profile (PUT /system/schedule; the running worker hot-reloads it). */
export function ScheduleSettingsPanel({ className }: { className?: string }) {
  const q = useSchedule()
  const m = useSetSchedule()
  const [pick, setPick] = useState<ScheduleProfileId | null>(null)
  if (USE_MOCK) return null
  const data = q.data
  const value = (pick ?? data?.active ?? 'weekly') as ScheduleProfileId
  const prof = data?.profiles.find((p) => p.id === value)
  const save = () =>
    m.mutate(value, {
      onSuccess: (r) => {
        setPick(null)
        toast({ tone: 'success', title: `Schedule profile: ${r.active}`, message: r.worker_alive ? 'The worker applies it within a few seconds.' : 'Applied when the worker starts.' })
      },
      onError: (e) => toast({ tone: 'error', title: 'Could not change the schedule profile', message: String(e) }),
    })
  return (
    <Panel title="Settings · refresh schedule" subtitle="Profile switch takes effect without a code change" className={className}>
      {q.isError ? (
        <p className="text-sm text-muted">Schedule endpoint unavailable (API &lt; 1.28).</p>
      ) : !data ? (
        <p className="text-sm text-muted">Loading…</p>
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <CalendarClock className="size-4 text-muted" aria-hidden />
            <Select
              label="Schedule profile"
              value={value}
              onChange={(v) => setPick(v)}
              className="w-56"
              options={data.profiles.map((p) => ({ value: p.id as ScheduleProfileId, label: `${p.label}${p.id === data.active ? ' (active)' : ''}` }))}
            />
            <Button size="sm" onClick={save} disabled={m.isPending || value === data.active}>
              {m.isPending ? 'Saving…' : 'Apply'}
            </Button>
            <span className="text-2xs text-muted">
              source {data.source}
              {data.updated_at ? ` · changed ${fmtDateTime(data.updated_at)}` : ''}
            </span>
            {data.pending_apply && <Badge tone="warn">worker still on {data.worker_profile}</Badge>}
          </div>
          {prof && (
            <div className="space-y-1 text-xs">
              <p className="text-muted">{prof.description}</p>
              <ul className="space-y-0.5">
                {prof.entries.map((e) => (
                  <li key={e.id}>
                    <span className="font-medium">{e.id}</span> <span className="num text-muted">{e.cron} ({e.timezone})</span>
                    {e.next_run ? <span className="num"> · next {fmtDateTime(e.next_run)}</span> : null}
                    {e.description ? <span className="text-muted"> — {e.description}</span> : null}
                  </li>
                ))}
              </ul>
              {prof.placeholder && <Badge tone="info">placeholder: behaves like daily for now</Badge>}
            </div>
          )}
        </div>
      )}
    </Panel>
  )
}

