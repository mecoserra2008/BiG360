import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react'
import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useRequestSync, useSyncStatus } from '@/api/ops/hooks'
import { REASON_HELP, REASON_LABEL, isSyncRunning, jobDuration, notableEntities, reasonCounts, reasonTone, sortDatasets, untilText } from '@/api/ops/sync'
import { WAIT_KIND_LABEL, capsText, groupDeferred, type AdmissionWait, type SyncCaps, type SyncDeferred } from '@/api/ops/pause'
import type { SyncDataset, SyncJob, SyncReason, SyncStatus } from '@/api/ops/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtDate, fmtDateTime, fmtNum } from '@/lib/format'
import { cn } from '@/lib/utils'

type Filter = 'attention' | 'all' | 'jobs'

/**
 * Data Health > Data Sync (startup sync, v1.41 GET /system/sync): one row per dataset with its reason code, last /
 * expected date, next expected update and the jobs the sync ran for it (durations); the job list of the run; the
 * publishes. Backends without the endpoint (< 1.41) render nothing.
 */
export function SyncPanel({ className }: { className?: string }) {
  const q = useSyncStatus()
  const req = useRequestSync()
  const ref = useRef<HTMLDivElement>(null)
  const [filter, setFilter] = useState<Filter>('attention')
  useEffect(() => {
    if (typeof window !== 'undefined' && window.location.hash === '#sync' && q.data) ref.current?.scrollIntoView({ block: 'start' })
  }, [q.data])
  const st = q.data
  if (!st) return null
  const running = isSyncRunning(st)
  const pr = (st.progress ?? {}) as Record<string, number | undefined>
  const ram = (st.ram ?? {}) as { peak_tree_mb?: number; min_machine_free_gb?: number | null }
  const sched = (st.scheduler ?? null) as { state?: string; next_in_s?: number | null; interval_min?: number; enabled?: boolean } | null
  const asOf = (st.data_as_of as { overall?: string | null } | undefined)?.overall
  return (
    <div id="sync" ref={ref} className={cn('scroll-mt-14', className)}>
      <Panel
        title="Data sync"
        subtitle="At every start and every 30 minutes the app checks what is missing from the latest data, fetches only that (in parallel) and publishes the changed sections. Every skipped item has a reason."
        actions={
          <div className="flex items-center gap-2">
            {st.chip?.text && <Badge tone={st.chip.tone === 'down' ? 'down' : st.chip.tone === 'warn' ? 'warn' : st.chip.tone === 'ok' ? 'up' : 'info'}>{st.chip.text}</Badge>}
            <Button size="xs" variant="outline" disabled={running || req.isPending} onClick={() => req.mutate(undefined)} title="Fetch what is missing now (the supervisor starts it within a few seconds)">
              <RefreshCw className={running ? 'animate-spin' : undefined} /> Sync now
            </Button>
          </div>
        }
      >
        {req.data && <p className="mb-2 text-2xs text-muted">{req.data.message}</p>}
        {st.state === 'never' ? (
          <p className="text-xs text-muted">No sync has run yet. It starts about 90 s after the app opens (BiG360.exe), or now with “Sync now”.</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap gap-x-8 gap-y-2">
              <Stat label="State" value={<Badge tone={running ? 'info' : st.state === 'interrupted' ? 'warn' : st.result === 'failed' ? 'down' : st.result === 'partial' ? 'warn' : 'up'}>{running ? st.phase ?? st.state : st.result ?? st.state}</Badge>} sub={st.trigger ? `trigger ${st.trigger}` : undefined} />
              <Stat label="Data as of" value={fmtDate(asOf ?? st.as_of ?? null)} sub={st.as_of ? `target session ${fmtDate(st.as_of)}` : undefined} />
              <Stat label="Progress" value={`${pr.jobs_done ?? 0}/${pr.jobs_total ?? 0}`} sub={`${pr.datasets_checked ?? 0} checked · ${pr.due ?? 0} due · ${pr.fetched ?? 0} fetched · ${pr.skipped ?? 0} skipped`} />
              <Stat label={running ? 'Started' : 'Finished'} value={fmtDateTime((running ? st.started_at : st.finished_at) ?? null) || '—'} sub={st.seconds ? `took ${jobDuration({ duration_s: st.seconds })}` : undefined} />
              <Stat label="Peak RAM" value={ram.peak_tree_mb ? `${fmtNum(ram.peak_tree_mb / 1024, 1)} GB` : '—'} sub={ram.min_machine_free_gb != null ? `machine free ≥ ${fmtNum(ram.min_machine_free_gb, 1)} GB` : undefined} />
              <Stat
                label="Next sync"
                value={sched?.enabled === false ? 'off' : running ? 'after this one' : sched?.next_in_s != null ? `in ${Math.max(1, Math.round(sched.next_in_s / 60))} min` : '—'}
                sub={sched ? `every ${sched.interval_min ?? 30} min${sched.state && sched.state !== 'idle' ? ` · ${sched.state}` : ''}` : 'supervisor not reporting'}
              />
            </div>
            <div className="mb-2 flex flex-wrap items-center gap-2 text-2xs" aria-label="Datasets per reason">
              {reasonCounts(st).map(([r, n]) => (
                <Tip key={r} content={REASON_HELP[r]}>
                  <span tabIndex={0}><Badge tone={reasonTone(r)}>{REASON_LABEL[r]} · {n}</Badge></span>
                </Tip>
              ))}
              <span className="ml-auto">
                <Segmented
                  label="Show"
                  value={filter}
                  onChange={(v) => setFilter(v as Filter)}
                  options={[{ value: 'attention', label: 'Needs attention' }, { value: 'all', label: 'All datasets' }, { value: 'jobs', label: 'Jobs' }]}
                />
              </span>
            </div>
            {filter === 'jobs' ? <JobsTable jobs={st.jobs ?? []} /> : <DatasetRows st={st} all={filter === 'all'} />}
            <DeferredList st={st} />
            <PublishList st={st} />
            {(st.notes ?? []).length > 0 && (
              <details className="mt-2 text-2xs text-muted">
                <summary className="cursor-pointer">Notes ({st.notes?.length})</summary>
                <ul className="mt-1 space-y-0.5">{(st.notes ?? []).map((n, i) => <li key={i} className="num break-words">{n}</li>)}</ul>
              </details>
            )}
          </>
        )}
      </Panel>
    </div>
  )
}

function DatasetRows({ st, all }: { st: SyncStatus; all: boolean }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const rows = useMemo(() => {
    const items = sortDatasets(st.datasets ?? [])
    return all ? items : items.filter((d) => d.reason !== 'fresh' || (d.runs ?? []).some((r) => r.status === 'failed'))
  }, [st.datasets, all])
  if (rows.length === 0) return <p className="py-2 text-xs text-up">Every dataset is up to date with the latest published period.</p>
  const toggle = (id: string) => setOpen((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n })
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[960px] text-xs" aria-label="Datasets of the data sync">
        <thead>
          <tr className="border-b border-border text-left text-muted">
            <th scope="col" className="w-6" />
            <th scope="col" className="py-1 pr-2 font-semibold">Dataset</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Status</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Data as of</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Expected</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Next update</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Jobs run</th>
            <th scope="col" className="py-1 font-semibold">Reason / detail</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((d) => {
            const ents = notableEntities(d)
            const isOpen = open.has(d.dataset)
            return (
              <Fragment key={d.dataset}>
                <tr className="border-b border-border align-top last:border-b-0">
                  <td className="py-1">
                    {ents.length > 0 && (
                      <button type="button" className="text-muted hover:text-fg" aria-expanded={isOpen} aria-label={`${isOpen ? 'Hide' : 'Show'} ${d.label ?? d.dataset} details`} onClick={() => toggle(d.dataset)}>
                        {isOpen ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
                      </button>
                    )}
                  </td>
                  <th scope="row" className="py-1 pr-2 text-left font-medium" title={d.dataset}>{d.label ?? d.dataset}{d.kind === 'job' ? <span className="ml-1 text-2xs text-faint">source job</span> : null}</th>
                  <td className="py-1 pr-2"><ReasonBadge reason={d.reason} /></td>
                  <td className="num py-1 pr-2">{d.data_as_of ? fmtDate(d.data_as_of.slice(0, 10)) : '—'}</td>
                  <td className="num py-1 pr-2 text-muted">{d.expected_as_of ? fmtDate(d.expected_as_of) : '—'}</td>
                  <td className="num py-1 pr-2 text-muted" title={d.next_expected ?? undefined}>{nextText(d)}</td>
                  <td className="py-1 pr-2"><Runs d={d} /></td>
                  <td className="py-1 text-muted">
                    {d.keys && d.keys.length > 0 ? <span className="mr-1 text-warn">Key: {d.keys.join(', ')}.</span> : null}
                    {d.detail}
                    {d.retry_at && d.reason !== 'fresh' ? <span className="text-faint"> · retry {fmtDateTime(d.retry_at)}</span> : null}
                    {d.attempts && d.reason !== 'fresh' && d.reason !== 'due' ? <span className="text-faint"> · attempt {d.attempts}</span> : null}
                  </td>
                </tr>
                {isOpen && ents.map((e) => (
                  <tr key={`${d.dataset}:${e.entity}`} className="border-b border-border bg-surface-2/40 text-2xs">
                    <td />
                    <td className="py-0.5 pr-2 pl-3 text-muted">{e.entity}{e.secondary ? <span className="text-faint"> (alt)</span> : null}</td>
                    <td className="py-0.5 pr-2"><ReasonBadge reason={e.reason} /></td>
                    <td className="num py-0.5 pr-2">{e.data_as_of ? fmtDate(e.data_as_of.slice(0, 10)) : '—'}</td>
                    <td className="num py-0.5 pr-2 text-muted">{e.expected_as_of ? fmtDate(e.expected_as_of.slice(0, 10)) : '—'}</td>
                    <td className="num py-0.5 pr-2 text-muted">{untilText(e.next_expected) ?? '—'}</td>
                    <td className="num py-0.5 pr-2 text-muted">{(e.missing ?? []).length ? `${e.missing?.length} missing` : ''}</td>
                    <td className="py-0.5 text-muted">{e.keys?.length ? <span className="text-warn">Key: {e.keys.join(', ')}. </span> : null}{e.detail}{e.retry_at ? ` · retry ${fmtDateTime(e.retry_at)}` : ''}</td>
                  </tr>
                ))}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function nextText(d: SyncDataset): string {
  if (d.reason === 'needs_key' || d.reason === 'no_source') return '—'
  if (d.retry_at && d.reason !== 'fresh') return untilText(d.retry_at) ?? '—'
  return untilText(d.next_expected) ?? '—'
}

function ReasonBadge({ reason }: { reason: SyncReason | string }) {
  const r = reason as SyncReason
  return (
    <Tip content={REASON_HELP[r] ?? reason}>
      <span tabIndex={0}><Badge tone={reasonTone(r)}>{REASON_LABEL[r] ?? reason}</Badge></span>
    </Tip>
  )
}

function Runs({ d }: { d: SyncDataset }) {
  const runs = d.runs ?? []
  if (!runs.length) return <span className="text-faint">—</span>
  return (
    <span className="flex flex-wrap gap-1">
      {runs.map((r, i) => (
        <span key={`${r.job}-${i}`} className={cn('num', r.status === 'failed' ? 'text-down' : r.status === 'skipped' ? 'text-faint' : 'text-fg')} title={`${r.job}: ${r.status}${r.reason ? ` (${r.reason})` : ''}`}>
          {r.job} {jobDuration(r)}
        </span>
      ))}
    </span>
  )
}

function JobsTable({ jobs }: { jobs: SyncJob[] }) {
  const ran = jobs.filter((j) => j.status !== 'skipped' || j.trigger !== 'derive')
  const skipped = jobs.length - ran.length
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[900px] text-xs" aria-label="Jobs of the data sync">
        <thead>
          <tr className="border-b border-border text-left text-muted">
            <th scope="col" className="py-1 pr-2 font-semibold">Job</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Pass</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Lane</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Status</th>
            <th scope="col" className="py-1 pr-2 font-semibold">Started</th>
            <th scope="col" className="py-1 pr-2 text-right font-semibold">Duration</th>
            <th scope="col" className="py-1 pr-2 text-right font-semibold">Rows</th>
            <th scope="col" className="py-1 font-semibold">Message</th>
          </tr>
        </thead>
        <tbody>
          {ran.map((j, i) => (
            <tr key={`${j.job}-${j.pass_no}-${i}`} className="border-b border-border align-top last:border-b-0">
              <th scope="row" className="num py-1 pr-2 text-left font-medium">{j.job}</th>
              <td className="num py-1 pr-2 text-muted">{j.pass_no ?? '—'}</td>
              <td className="py-1 pr-2 text-muted">{j.lane}</td>
              <td className="py-1 pr-2"><Badge tone={j.status === 'success' ? 'up' : j.status === 'partial' ? 'warn' : j.status === 'failed' ? 'down' : j.status === 'running' ? 'info' : 'neutral'}>{j.status}</Badge></td>
              <td className="num py-1 pr-2 text-muted">{j.started_at ? fmtDateTime(j.started_at) : '—'}</td>
              <td className="num py-1 pr-2 text-right">{jobDuration(j)}</td>
              <td className="num py-1 pr-2 text-right">{j.rows != null ? fmtNum(j.rows, 0) : '—'}</td>
              <td className="py-1 break-words text-muted">{j.reason ? `${j.reason}. ` : ''}{j.message}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {skipped > 0 && <p className="mt-1 text-2xs text-faint">{skipped} derived job(s) skipped: their inputs did not change (input fingerprint).</p>}
    </div>
  )
}

/** Coverage v3 A3: the sync budget, what it deferred to the next sync (and why), and admission waits. */
function DeferredList({ st }: { st: SyncStatus }) {
  const x = st as SyncStatus & { deferred?: SyncDeferred[]; caps?: SyncCaps; admission_waits?: AdmissionWait[]; carried_in?: string[] }
  const caps = capsText(x.caps)
  const groups = groupDeferred(x.deferred)
  const waits = x.admission_waits ?? []
  if (!caps && !groups.length && !waits.length) return null
  return (
    <div className="mt-3 border-t border-border pt-2 text-xs">
      <div className="label-caps mb-1">Budget &amp; deferred work</div>
      {caps && <p className="text-muted">{caps}{(x.carried_in ?? []).length ? ` · carried in: ${(x.carried_in ?? []).join(', ')}` : ''}</p>}
      {waits.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {waits.map((w) => (
            <li key={w.job_id}>
              <Badge tone="info">{WAIT_KIND_LABEL[w.kind ?? ''] ?? w.kind ?? 'waiting'}</Badge> <span className="num">{w.job_id}</span> <span className="text-muted">— {w.reason}</span>
            </li>
          ))}
        </ul>
      )}
      {groups.length > 0 && (
        <ul className="mt-1 space-y-0.5" aria-label="Jobs deferred to the next sync">
          {groups.map((g) => (
            <li key={g.reason}>
              <Badge tone="neutral">deferred · {g.jobs.length}</Badge> <span className="text-muted">{g.reason}:</span> <span className="num break-words">{g.jobs.join(', ')}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function PublishList({ st }: { st: SyncStatus }) {
  const pubs = (st.publish ?? []) as { pass?: number; mode?: string; status?: string; seconds?: number; snapshot?: string | null; dirty?: Record<string, string>; detail?: string | null; error?: string }[]
  if (!pubs.length) return null
  return (
    <div className="mt-3 border-t border-border pt-2 text-xs">
      <div className="label-caps mb-1">Publishes</div>
      <ul className="space-y-1">
        {pubs.map((p, i) => (
          <li key={i} className="flex flex-wrap items-baseline gap-2">
            <Badge tone={p.status === 'success' ? 'up' : p.status === 'failed' ? 'down' : 'info'}>{p.mode ?? '—'}</Badge>
            <span className="text-muted">pass {p.pass}</span>
            <span className="num">{p.seconds != null ? jobDuration({ duration_s: p.seconds }) : ''}</span>
            <span className="text-muted">
              {p.mode === 'none' ? 'no serving section changed' : p.mode === 'incremental' ? `sections rebuilt: ${Object.keys(p.dirty ?? {}).join(', ')}` : p.mode === 'full' ? `full build (${Object.keys(p.dirty ?? {}).length} sections changed)` : ''}
              {p.snapshot ? ` → ${p.snapshot}` : ''}
              {p.error ? <span className="text-down"> · {p.error}</span> : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
