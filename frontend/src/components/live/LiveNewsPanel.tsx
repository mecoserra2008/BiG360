import { Link } from 'react-router'
import { USE_MOCK } from '@/api/http'
import { useNewsFeeds, useNewsHealth } from '@/api/news/hooks'
import { TIER_LABEL } from '@/api/news/types'
import { Badge } from '@/components/ui/badge'
import { PendingState } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDateTime, fmtInt, fmtRelative } from '@/lib/format'
import { cn } from '@/lib/utils'

const sec = (v: number | null | undefined) => (v === null || v === undefined ? DASH : v < 120 ? `${Math.round(v)} s` : `${(v / 60).toFixed(1)} min`)

/** Data Health > Live news (roadmap v3 N3.4): service, publish -> bus latency, items / hour per source, last poll, failures. */
export function LiveNewsPanel({ className }: { className?: string }) {
  const h = useNewsHealth(30_000)
  const feeds = useNewsFeeds()
  if (USE_MOCK) return null
  const d = h.data
  const ov = d?.overall
  const hourly = (d?.hourly ?? null) as { at?: string; hours?: number; seconds?: number; xlmr_scored?: number; xlmr_error?: string | null } | null
  const intra = (d?.intraday ?? {}) as { available?: boolean; built_at?: string; counts?: Record<string, number> }
  const rejected = feeds.data?.items.filter((f) => f.status === 'rejected') ?? []
  return (
    <Panel
      className={className}
      title="Live news"
      subtitle="Realtime service (supervisor child), latency from source publication to the bus, sources, hourly fold and intraday store"
      actions={
        <Link to="/news" className="text-xs font-medium text-accent-text hover:underline">
          News Terminal
        </Link>
      }
      data-testid="live-news-panel"
    >
      {!d ? (
        <PendingState compact detail={h.isError ? 'live news health unavailable' : 'loading'} />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-x-8 gap-y-2">
            <Stat label="Service" value={<Badge tone={d.running ? 'up' : d.enabled ? 'down' : 'neutral'}>{d.running ? 'running' : d.enabled ? 'not running' : 'switched off'}</Badge>} sub={d.rss_mb ? `${fmtInt(d.rss_mb)} / ${fmtInt(d.ram_cap_mb ?? 1536)} MB` : (d.supervisor_state ?? undefined)} />
            <Stat label="Publish → bus p50" value={sec(ov?.publish_to_bus_s_p50)} sub={`p95 ${sec(ov?.publish_to_bus_s_p95)} · n ${fmtInt(ov?.n ?? 0)}`} />
            <Stat label="Pipeline p50" value={sec(ov?.pipeline_s_p50)} sub={`p95 ${sec(ov?.pipeline_s_p95)}`} />
            <Stat label="GDELT release → hot" value={sec(d.gdelt?.release_lag_s_p50)} sub={`p95 ${sec(d.gdelt?.release_lag_s_p95)}${d.gdelt?.last_slot ? ` · slot ${fmtDateTime(d.gdelt.last_slot)}` : ''}`} />
            <Stat label="Items / hour" value={fmtInt(d.sources.reduce((a, s) => a + s.items_1h, 0))} sub={`${d.sources.length} sources`} />
            <Stat label="Last feed poll" value={d.last_feed_poll_at ? fmtRelative(d.last_feed_poll_at) : DASH} sub={`${d.feeds_total} feeds · ${d.feeds_failing.length} failing`} />
            <Stat label="Hourly fold" value={hourly?.at ? fmtRelative(hourly.at) : 'not yet'} sub={hourly?.at ? `${hourly.hours ?? 0} h folded · ${hourly.seconds ?? 0} s${hourly.xlmr_scored ? ` · XLM-R ${hourly.xlmr_scored}` : ''}` : undefined} />
            <Stat label="Intraday store" value={intra.available ? fmtRelative(intra.built_at ?? '') : 'none'} sub={intra.counts ? `${fmtInt(intra.counts.nt_items ?? 0)} items · ${fmtInt(intra.counts.nt_stories ?? 0)} stories` : undefined} />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[640px] text-xs">
              <thead>
                <tr className="text-left text-2xs tracking-wide text-muted uppercase">
                  <th className="py-1">Source</th>
                  <th className="py-1">Tier</th>
                  <th className="py-1 text-right">Items / h</th>
                  <th className="py-1 text-right">Latency p50</th>
                  <th className="py-1 text-right">p95</th>
                  <th className="py-1 text-right">Last item</th>
                </tr>
              </thead>
              <tbody>
                {[...d.sources]
                  .sort((a, b) => b.items_1h - a.items_1h)
                  .slice(0, 40)
                  .map((s) => (
                    <tr key={s.source} className="border-t border-border/60">
                      <td className="py-1 font-medium">{s.source}</td>
                      <td className="py-1 text-muted">{s.tier ? TIER_LABEL[s.tier] : DASH}</td>
                      <td className="num py-1 text-right">{fmtInt(s.items_1h)}</td>
                      <td className="num py-1 text-right">{sec(s.publish_to_bus_s_p50)}</td>
                      <td className={cn('num py-1 text-right', (s.publish_to_bus_s_p95 ?? 0) > 180 && 'text-warn')}>{sec(s.publish_to_bus_s_p95)}</td>
                      <td className="num py-1 text-right text-muted">{s.last_item_at ? fmtRelative(s.last_item_at) : DASH}</td>
                    </tr>
                  ))}
                {d.sources.length === 0 && (
                  <tr>
                    <td colSpan={6} className="py-2 text-center text-muted">
                      No items in the last hour{d.running ? '' : ' (service not running)'}.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          {d.feeds_failing.length > 0 && (
            <div>
              <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Failing feeds</div>
              <ul className="space-y-0.5 text-xs">
                {d.feeds_failing.map((f) => (
                  <li key={f.feed} className="flex gap-2">
                    <span className="font-medium">{f.feed}</span>
                    <span className="text-down">{f.last_error}</span>
                    <span className="ml-auto text-faint">{f.failing_since ? `since ${fmtRelative(f.failing_since)}` : ''}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          {rejected.length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-muted">{rejected.length} candidate feeds rejected at verification</summary>
              <ul className="mt-1 space-y-0.5">
                {rejected.map((f) => (
                  <li key={f.id}>
                    <span className="font-medium">{f.publisher}</span> <span className="text-faint">({f.id})</span>: <span className="text-muted">{f.reason}</span>
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </Panel>
  )
}
