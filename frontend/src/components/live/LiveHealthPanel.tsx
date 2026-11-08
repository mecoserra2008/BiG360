import { useQuery } from '@tanstack/react-query'
import { Pause, Play } from 'lucide-react'
import { USE_MOCK } from '@/api/http'
import { liveApi } from '@/api/live/client'
import { useLive } from '@/api/live/store'
import { type LiveStatus, streamTone } from '@/api/live/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtCompact, fmtDateTime, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'

const secs = (v: number | null | undefined) => (typeof v !== 'number' || !Number.isFinite(v) ? '—' : v < 90 ? `${v.toFixed(0)} s` : v < 5400 ? `${(v / 60).toFixed(0)} min` : `${(v / 3600).toFixed(1)} h`)

/** System page: real-time layer health per stream / consumer (GET /live/status) + this browser's SSE connection. */
export function LiveHealthPanel({ className }: { className?: string }) {
  const q = useQuery({
    queryKey: ['live', 'status'],
    queryFn: () => liveApi.status(),
    refetchInterval: 30_000,
    staleTime: 15_000,
    enabled: !USE_MOCK,
    retry: false,
  })
  if (USE_MOCK) return null
  return (
    <Panel className={className} title="Real-time layer" subtitle="Stream workers and consumers (GET /live/status) · this browser's SSE connection" actions={<BrowserConn />} flush>
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-40" />}>
        {(d) => <Body d={d} />}
      </QueryView>
    </Panel>
  )
}

function BrowserConn() {
  const conn = useLive((s) => s.conn)
  const paused = useLive((s) => s.paused)
  const setPaused = useLive((s) => s.setPaused)
  const lastId = useLive((s) => s.lastEventId)
  const received = useLive((s) => s.received)
  return (
    <div className="flex items-center gap-2 text-xs text-muted">
      <span>
        SSE <span className="font-medium text-fg">{conn}</span> · last id {lastId ?? '—'} · {received} received
      </span>
      <Button size="xs" variant="outline" onClick={() => setPaused(!paused)} aria-pressed={paused}>
        {paused ? <Play /> : <Pause />} {paused ? 'Resume' : 'Pause'}
      </Button>
    </div>
  )
}

function Body({ d }: { d: LiveStatus }) {
  return (
    <div>
      {!d.running && (
        <p className="border-b border-border bg-warn-soft px-3 py-2 text-xs text-fg">
          The real-time supervisor is not running (last heartbeat {d.heartbeat_at ? fmtDateTime(d.heartbeat_at) : 'never'}). The app still shows the nightly snapshot and whatever is in the hot store; enable with{' '}
          <code className="num">python -m app.cli realtime start</code> or <code className="num">worker start --realtime</code>.
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 border-b border-border p-3 sm:grid-cols-5">
        <Stat label="Supervisor" value={<span className={d.running ? 'text-up' : 'text-warn'}>{d.running ? 'running' : 'stopped'}</span>} sub={d.pid ? `pid ${d.pid}` : undefined} />
        <Stat label="Bus" value={d.backend} sub={`head id ${d.head_id ?? '—'}`} />
        <Stat label="Bus size" value={typeof d.bus_bytes === 'number' ? `${fmtCompact(d.bus_bytes / 1e6)} MB` : '—'} sub={`${d.dead_letters ?? 0} dead letters`} />
        <Stat label="Queue" value={`${String((d.queue as Record<string, unknown> | null)?.size ?? '—')} / ${String((d.queue as Record<string, unknown> | null)?.max ?? '—')}`} />
        <Stat label="Heartbeat" value={secs(d.heartbeat_age_s)} sub="age" />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs" aria-label="Stream health">
          <thead>
            <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1">Stream</th>
              <th className="px-2 py-1">Status</th>
              <th className="px-2 py-1 text-right">Events (1h / total)</th>
              <th className="px-2 py-1 text-right">Per min</th>
              <th className="px-2 py-1 text-right">Freshness</th>
              <th className="px-2 py-1 text-right">Lag p50 / p95</th>
              <th className="px-2 py-1 text-right">Consumer lag</th>
              <th className="px-2 py-1 text-right">Errors</th>
              <th className="px-3 py-1">Last poll / error</th>
            </tr>
          </thead>
          <tbody>
            {d.streams.map((s) => {
              const tone = streamTone(s.status)
              return (
                <tr key={s.name} className="border-b border-border/60">
                  <td className="px-3 py-1">
                    <div className="font-medium text-fg">{s.name}</div>
                    <div className="max-w-[260px] truncate text-2xs text-muted" title={s.detail ?? ''}>
                      {s.kind}
                      {s.detail ? ` · ${s.detail}` : ''}
                    </div>
                  </td>
                  <td className="px-2 py-1">
                    <Badge tone={tone === 'neutral' ? 'neutral' : tone}>{s.status}</Badge>
                  </td>
                  <td className="num px-2 py-1 text-right">
                    {fmtInt(s.events_1h)} / {fmtInt(s.events_total)}
                  </td>
                  <td className="num px-2 py-1 text-right">{typeof s.throughput_per_min === 'number' ? s.throughput_per_min.toFixed(1) : '—'}</td>
                  <td className="num px-2 py-1 text-right">{secs(s.freshness_s)}</td>
                  <td className="num px-2 py-1 text-right">
                    {secs(s.lag_s_p50)} / {secs(s.lag_s_p95)}
                  </td>
                  <td className="num px-2 py-1 text-right">{typeof s.consumer_lag === 'number' ? fmtInt(s.consumer_lag) : '—'}</td>
                  <td className={cn('num px-2 py-1 text-right', s.errors > 0 && 'text-down')}>
                    {s.errors}
                    {s.restarts ? ` · ${s.restarts} restarts` : ''}
                  </td>
                  <td className="px-3 py-1 text-2xs text-muted">
                    {s.last_error ? (
                      <span className="text-down" title={s.last_error}>
                        {s.last_error.slice(0, 60)}
                      </span>
                    ) : s.last_poll_at ? (
                      fmtDateTime(s.last_poll_at)
                    ) : (
                      '—'
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 px-3 py-2 text-2xs text-muted">
        {d.topics.map((t) => (
          <span key={t.topic}>
            <span className="font-medium text-fg">{t.topic}</span> {fmtInt(t.messages ?? 0)} msgs · {fmtInt(t.messages_1h ?? 0)} last hour
          </span>
        ))}
      </div>
    </div>
  )
}
