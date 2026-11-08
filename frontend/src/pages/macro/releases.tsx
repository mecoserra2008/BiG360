import { CalendarClock, Clock, ExternalLink, X } from 'lucide-react'
import { Dialog } from 'radix-ui'
import { Fragment, useMemo, useState } from 'react'
import { surpriseBucket } from '@/api/macro/adapters'
import { useMacroRelease } from '@/api/macro/hooks'
import type { CalendarDay, MCalendarItem, MReleaseSeries, MSurpriseHistoryPoint } from '@/api/macro/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtChange, fmtMacro, localTime, zStyle } from './fmt'
import { ModelBasedTag, SeriesDrawer } from './shared'

const dayLabel = (d: string) => new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(new Date(`${d}T00:00:00Z`))

function Expectation({ s }: { s: MReleaseSeries }) {
  const e = s.expectation
  if (!e) return <span className="text-faint">{DASH}</span>
  return (
    <Tip
      content={
        <div className="space-y-0.5 text-xs">
          <div className="font-semibold">{e.label}</div>
          <div>
            {e.model}
            {e.model_spec ? ` · ${e.model_spec}` : ''} · version {e.version ?? DASH}
          </div>
          <div>
            80% {fmtMacro(e.lo80)} … {fmtMacro(e.hi80)} · 95% {fmtMacro(e.lo95)} … {fmtMacro(e.hi95)}
          </div>
          <div>
            made {e.forecast_made_at.slice(0, 16).replace('T', ' ')} UTC · data to {e.data_cutoff ?? DASH}
            {e.backfill ? ' · point-in-time backfill' : ''}
          </div>
        </div>
      }
    >
      <span tabIndex={0} className="num cursor-help">
        <span className="font-medium text-fg">{fmtMacro(e.point)}</span>
        {typeof e.lo80 === 'number' && typeof e.hi80 === 'number' && (
          <span className="ml-1 text-2xs text-muted">
            [{fmtMacro(e.lo80, 3)}, {fmtMacro(e.hi80, 3)}]
          </span>
        )}
      </span>
    </Tip>
  )
}

export function SurpriseZ({ z, signed }: { z: number | null | undefined; signed: number | null | undefined }) {
  if (typeof z !== 'number') return <span className="text-faint">{DASH}</span>
  const b = surpriseBucket(signed)
  return (
    <span className="num inline-flex h-5 min-w-12 items-center justify-center rounded-sm px-1 text-xs font-semibold text-fg" style={zStyle(b)} title="(actual − model expectation) / std of past forecast errors; colour = signed by economic direction (teal good, rose bad)">
      {fmtChange(z, 1)}σ
    </span>
  )
}

function TimeCell({ it }: { it: MCalendarItem }) {
  return (
    <span className="num inline-flex items-center gap-1 whitespace-nowrap">
      {it.time_known ? <Clock className="size-3 text-muted" aria-label="exact time published" /> : <CalendarClock className="size-3 text-faint" aria-label="time not published (date only)" />}
      <span className={it.time_known ? 'text-fg' : 'text-muted'}>{it.time_known ? localTime(it.scheduled_local) : 'date only'}</span>
      {it.time_known && <span className="text-2xs text-faint">{it.scheduled_tz.split('/').pop()?.replace('_', ' ')}</span>}
    </span>
  )
}

/** Release list grouped by day: actual vs model-based expectation, prior (+ revision), surprise z. */
export function ReleaseList({ days, compact = false, emptyText = 'No releases match the filters.' }: { days: CalendarDay[]; compact?: boolean; emptyText?: string }) {
  const [open, setOpen] = useState<string | null>(null)
  if (!days.length) return <EmptyState title="Nothing scheduled" message={emptyText} />
  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label="Release calendar">
          <thead>
            <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1.5">Time (local)</th>
              <th className="px-2 py-1.5">Ctry</th>
              <th className="px-2 py-1.5">Release</th>
              <th className="px-2 py-1.5">Tier</th>
              <th className="px-2 py-1.5 text-right">Actual</th>
              <th className="px-2 py-1.5 text-right">
                <span className="block">Expectation</span>
                <ModelBasedTag short className="mt-0.5 font-normal normal-case" />
              </th>
              {!compact && <th className="px-2 py-1.5 text-right">Prior (rev.)</th>}
              <th className="px-2 py-1.5 text-right">Surprise</th>
              {!compact && <th className="px-3 py-1.5">Schedule</th>}
            </tr>
          </thead>
          <tbody>
            {days.map((d) => (
              <Fragment key={d.date}>
                <tr>
                  <td colSpan={compact ? 7 : 9} className="bg-surface-2 px-3 py-1 text-xs font-semibold text-fg">
                    {dayLabel(d.date)} <span className="font-normal text-muted">· {d.items.length} releases</span>
                  </td>
                </tr>
                {d.items.map((it) => {
                  const s = it.series[0]
                  return (
                    <tr key={it.release_event_id} className="cursor-pointer border-b border-border/60 hover:bg-surface-2" onClick={() => setOpen(it.release_event_id)}>
                      <td className="px-3 py-1">
                        <TimeCell it={it} />
                      </td>
                      <td className="px-2 py-1">
                        <span className="inline-flex items-center gap-1">
                          <Flag country={it.country} /> <span className="num text-xs">{it.country}</span>
                        </span>
                      </td>
                      <td className="max-w-[340px] px-2 py-1">
                        <button type="button" className="block max-w-full truncate text-left font-medium text-fg hover:text-accent-text hover:underline" onClick={() => setOpen(it.release_event_id)}>
                          {it.release_name}
                        </button>
                        <div className="truncate text-2xs text-muted">
                          {it.publisher}
                          {it.reference_period ? ` · ${it.reference_period}` : ''}
                          {it.kind === 'policy_meeting' ? ' · policy meeting' : ''}
                          {it.series.length > 1 ? ` · ${it.series.length} series` : ''}
                        </div>
                      </td>
                      <td className="px-2 py-1">
                        <Badge tone={it.importance === 1 ? 'accent' : 'neutral'}>T{it.importance}</Badge>
                      </td>
                      <td className="num px-2 py-1 text-right font-semibold text-fg">{s && typeof s.actual === 'number' ? fmtMacro(s.actual) : DASH}</td>
                      <td className="px-2 py-1 text-right">{s ? <Expectation s={s} /> : <span className="text-faint">{DASH}</span>}</td>
                      {!compact && (
                        <td className="num px-2 py-1 text-right text-muted">
                          {s && typeof s.prior === 'number' ? fmtMacro(s.prior) : DASH}
                          {s && typeof s.prior_revision === 'number' && s.prior_revision !== 0 && <span className="ml-1 text-2xs">({fmtChange(s.prior_revision)})</span>}
                        </td>
                      )}
                      <td className="px-2 py-1 text-right">{s ? <SurpriseZ z={s.surprise_z} signed={s.signed_z} /> : <span className="text-faint">{DASH}</span>}</td>
                      {!compact && (
                        <td className="px-3 py-1">
                          <Badge tone={it.schedule_source === 'official_calendar' ? 'up' : 'warn'}>{it.schedule_source === 'official_calendar' ? 'official' : 'inferred'}</Badge>
                        </td>
                      )}
                    </tr>
                  )
                })}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
      <ReleaseDrawer id={open} onClose={() => setOpen(null)} />
    </>
  )
}

/* ───────────── release detail drawer ───────────── */

export function ReleaseDrawer({ id, onClose }: { id: string | null; onClose: () => void }) {
  return (
    <Dialog.Root open={!!id} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="@container fixed top-0 right-0 z-50 flex h-full w-[min(680px,100vw)] flex-col border-l border-border-strong bg-surface focus-visible:outline-none" aria-describedby={undefined}>
          {id && <ReleaseBody id={id} onClose={onClose} />}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function ReleaseBody({ id, onClose }: { id: string; onClose: () => void }) {
  const q = useMacroRelease(id)
  const [series, setSeries] = useState<string | null>(null)
  const r = q.data?.release
  return (
    <>
      <header className="flex items-start gap-2 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <Dialog.Title className="truncate text-md font-semibold text-fg">{r?.release_name ?? 'Release'}</Dialog.Title>
          {r && (
            <div className="num flex flex-wrap items-center gap-x-2 text-xs text-muted">
              <Flag country={r.country} /> {r.country} · {r.publisher} · {fmtDate(r.release_date)} {r.time_known ? localTime(r.scheduled_local) : '(date only)'}
              {r.reference_period ? ` · ${r.reference_period}` : ''}
              <Badge tone={r.schedule_source === 'official_calendar' ? 'up' : 'warn'}>{r.schedule_source === 'official_calendar' ? 'official calendar' : 'inferred date'}</Badge>
              {r.url && (
                <a href={r.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
                  official calendar <ExternalLink className="size-3" />
                </a>
              )}
            </div>
          )}
        </div>
        <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
          <X />
        </Button>
      </header>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        <ModelBasedTag />
        {q.isPending ? (
          <SkeletonRows rows={8} />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : q.data.series.length === 0 ? (
          <EmptyState title="No linked series" message="This release (e.g. a policy meeting) has no linked data series in the lake." />
        ) : (
          q.data.series.map((s) => (
            <section key={s.series_id} className="rounded-md border border-border p-3">
              <div className="flex items-start gap-2">
                <div className="min-w-0 flex-1">
                  <button type="button" className="block max-w-full truncate text-left text-sm font-semibold text-fg hover:text-accent-text hover:underline" onClick={() => setSeries(s.series_id)}>
                    {s.title}
                  </button>
                  <div className="num text-2xs text-muted">
                    {s.series_id} · {s.theme} · {s.measure} · period {s.period ?? DASH}
                  </div>
                </div>
                <SurpriseZ z={s.surprise_z} signed={s.signed_z} />
              </div>
              <dl className="num mt-2 grid grid-cols-4 gap-2 text-xs">
                <div>
                  <dt className="text-muted">Actual</dt>
                  <dd className="text-md font-semibold text-fg">{fmtMacro(s.actual)}</dd>
                </div>
                <div>
                  <dt className="text-muted">Model expectation</dt>
                  <dd className="text-md text-fg">{fmtMacro(s.expectation?.point)}</dd>
                </div>
                <div>
                  <dt className="text-muted">80% interval</dt>
                  <dd className="text-fg">
                    {fmtMacro(s.expectation?.lo80)} … {fmtMacro(s.expectation?.hi80)}
                  </dd>
                </div>
                <div>
                  <dt className="text-muted">Prior (revised)</dt>
                  <dd className="text-fg">
                    {fmtMacro(s.prior)} ({fmtMacro(s.prior_revised)})
                  </dd>
                </div>
              </dl>
              {s.history && s.history.length > 1 && <ReleaseHistory history={s.history} title={s.title} />}
            </section>
          ))
        )}
        {q.data && <p className="text-2xs text-muted">Surprise z: {q.data.method}</p>}
      </div>
      <SeriesDrawer seriesId={series} onClose={() => setSeries(null)} />
    </>
  )
}

type HistoryPoint = MSurpriseHistoryPoint

/** Actual vs model expectation over past releases, with the surprise z underneath (separate axis, no dual axis). */
function ReleaseHistory({ history, title }: { history: HistoryPoint[]; title: string }) {
  const t = useChartTokens()
  const pts = useMemo(() => [...history].filter((h) => h.period || h.release_date).sort((a, b) => String(a.period ?? a.release_date).localeCompare(String(b.period ?? b.release_date))), [history])
  const option = useMemo<EChartsOption>(() => {
    const xs = pts.map((p) => String(p.period ?? p.release_date).slice(0, 10))
    const f = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)
    return {
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      legend: { top: 0, left: 0, data: ['actual', 'model expectation'] },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const i = (ps as { dataIndex: number }[])[0]?.dataIndex ?? 0
          const p = pts[i]
          return `${ttTitle(xs[i])}${ttRow(t.ref, 'actual', fmtMacro(p.actual))}${ttRow(t.series[0], 'model expectation', fmtMacro(p.expectation))}${ttRow(t.ref, 'surprise z', fmtChange(f(p.surprise_z), 2))}`
        },
      },
      grid: [
        { left: 8, right: 12, top: 28, height: '52%', containLabel: true },
        { left: 8, right: 12, top: '72%', bottom: 8, containLabel: true },
      ],
      xAxis: [
        { type: 'category', data: xs, gridIndex: 0, axisLabel: { show: false } },
        { type: 'category', data: xs, gridIndex: 1, axisLabel: { hideOverlap: true, fontSize: 9 } },
      ],
      yAxis: [
        { type: 'value', scale: true, gridIndex: 0, axisLabel: { formatter: (v: number) => fmtMacro(v, 3) } },
        { type: 'value', gridIndex: 1, splitNumber: 2, name: 'surprise z', nameLocation: 'end', nameGap: 4, nameTextStyle: { align: 'left', fontSize: 9 }, axisLabel: { fontSize: 9 } },
      ],
      series: [
        { type: 'line', name: 'actual', data: pts.map((p) => f(p.actual)), color: t.ref, lineStyle: { color: t.ref, width: 1.5 }, symbolSize: 4 },
        { type: 'line', name: 'model expectation', data: pts.map((p) => f(p.expectation)), color: t.series[0], lineStyle: { color: t.series[0], width: 1.5, type: 'dashed' }, symbolSize: 4 },
        {
          type: 'bar',
          name: 'surprise z',
          xAxisIndex: 1,
          yAxisIndex: 1,
          barMaxWidth: 8,
          data: pts.map((p) => ({ value: f(p.surprise_z), itemStyle: { color: f(p.signed_z) === null ? t.ref : alpha((f(p.signed_z) as number) >= 0 ? t.up : t.down, 0.85) } })),
        },
      ],
    } as EChartsOption
  }, [pts, t])
  return (
    <div className="mt-2">
      <EChart option={option} height={230} ariaLabel={`${title}: actual vs model-based expectation and surprise history`} />
      <p className="text-2xs text-muted">Dashed: model-based expectation (not consensus), snapshotted before each release. Bars: surprise z, teal = good for the economy, rose = bad (signed by economic direction).</p>
    </div>
  )
}

export function CountryReleaseHint({ className }: { className?: string }) {
  return <span className={cn('text-2xs text-muted', className)}>Times shown in the publisher&apos;s local time zone.</span>
}
