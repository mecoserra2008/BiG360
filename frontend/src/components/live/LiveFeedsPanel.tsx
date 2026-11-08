import { useQuery } from '@tanstack/react-query'
import { USE_MOCK } from '@/api/http'
import { liveApi } from '@/api/live/client'
import { MARKET_REASON, feedTone, fmtLatency, serviceChip } from '@/api/live/quotes'
import { Badge } from '@/components/ui/badge'
import { PendingState } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDateTime, fmtInt, fmtRelative } from '@/lib/format'
import { cn } from '@/lib/utils'

const GROUP_LABEL: Record<string, string> = { quotes: 'Delayed quotes', filings: 'Filings', macro: 'Macro releases', news: 'News', other: 'Other' }

/**
 * Data Health > Live feeds (workstream B): per feed last poll, latency, items / hour, errors, and the reason when a
 * feed is idle (market closed), backing off (source down) or disabled (missing key); market sessions of the quotes
 * universe; the next official macro releases the watcher is waiting for.
 */
export function LiveFeedsPanel({ className }: { className?: string }) {
  const q = useQuery({ queryKey: ['live', 'feeds'], queryFn: () => liveApi.feeds(), refetchInterval: 30_000, staleTime: 15_000, enabled: !USE_MOCK, retry: false })
  if (USE_MOCK) return null
  const d = q.data
  const chip = serviceChip(d?.quotes)
  return (
    <Panel
      className={className}
      title="Live feeds"
      subtitle="Delayed quotes, SEC filings, official macro releases and news while the app is open · latency = source time → received (quotes: last print → fetch, includes the exchange delay)"
      data-testid="live-feeds-panel"
    >
      {!d ? (
        <PendingState compact detail={q.isError ? 'live feeds health unavailable (API older than v1.40.0?)' : 'loading'} />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap gap-x-8 gap-y-2">
            <Stat
              label="Quotes service"
              value={<Badge tone={chip.tone === 'up' ? 'up' : chip.tone === 'warn' ? 'warn' : chip.tone === 'down' ? 'down' : 'neutral'}>{chip.label}</Badge>}
              sub={d.quotes.reason ?? (d.quotes.rss_mb ? `${fmtInt(d.quotes.rss_mb)} MB · ${d.quotes.n_securities ?? 0} securities` : undefined)}
            />
            <Stat label="Markets open" value={d.markets.filter((m) => m.open).length} sub={`${d.markets.length} sessions in the universe`} />
            <Stat label="News service" value={<Badge tone={d.news.running ? 'up' : 'down'}>{d.news.running ? 'running' : 'not running'}</Badge>} sub={`${d.news.feeds_ok ?? 0}/${d.news.feeds ?? 0} news feeds ok`} />
            <Stat
              label="Next macro release"
              value={d.macro_next[0]?.scheduled_at ? fmtDateTime(d.macro_next[0].scheduled_at) : DASH}
              sub={d.macro_next[0] ? `${d.macro_next[0].country ?? ''} ${d.macro_next[0].release_name ?? d.macro_next[0].series_id ?? ''}`.trim() : 'none scheduled in the next 24 h'}
            />
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-xs" aria-label="Live feeds">
              <thead>
                <tr className="text-left text-2xs tracking-wide text-muted uppercase">
                  <th className="py-1">Feed</th>
                  <th className="py-1">Status</th>
                  <th className="py-1 text-right">Last poll</th>
                  <th className="py-1 text-right">Latency p50</th>
                  <th className="py-1 text-right">p95</th>
                  <th className="py-1 text-right">Items / h</th>
                  <th className="py-1 text-right">Requests / h</th>
                  <th className="py-1 text-right">Errors</th>
                  <th className="py-1 pl-3">Reason / last error</th>
                </tr>
              </thead>
              <tbody>
                {d.feeds.map((f) => (
                  <tr key={f.feed} className="border-t border-border/60">
                    <td className="py-1">
                      <span className="font-medium text-fg">{f.label}</span>
                      <span className="ml-1.5 text-2xs text-faint">{GROUP_LABEL[f.group] ?? f.group}</span>
                    </td>
                    <td className="py-1">
                      <Badge tone={feedTone(f.status)}>{f.status}</Badge>
                    </td>
                    <td className="num py-1 text-right text-muted">{f.last_poll_at ? <span title={fmtDateTime(f.last_poll_at)}>{fmtRelative(f.last_poll_at)}</span> : DASH}</td>
                    <td className="num py-1 text-right">
                      {fmtLatency(f.latency_s_p50)}
                      {f.fetch_ms_p50 != null && <span className="ml-1 text-2xs text-faint">({fmtInt(f.fetch_ms_p50)} ms req)</span>}
                    </td>
                    <td className={cn('num py-1 text-right', (f.latency_s_p95 ?? 0) > 1800 && f.group !== 'quotes' && 'text-warn')}>{fmtLatency(f.latency_s_p95)}</td>
                    <td className="num py-1 text-right">{fmtInt(f.items_1h)}</td>
                    <td className="num py-1 text-right text-muted">{f.requests_1h == null ? DASH : fmtInt(f.requests_1h)}</td>
                    <td className={cn('num py-1 text-right', f.errors > 0 && 'text-warn')}>{fmtInt(f.errors)}</td>
                    <td className="max-w-[380px] truncate py-1 pl-3 text-muted">
                      {f.reason || f.last_error ? (
                        <Tip content={[f.reason, f.last_error && f.last_error !== f.reason ? `last error: ${f.last_error}` : null].filter(Boolean).join(' · ')}>
                          <span tabIndex={0}>{f.reason ?? f.last_error}</span>
                        </Tip>
                      ) : (
                        (f.detail ?? DASH)
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {d.markets.length > 0 && (
            <div className="flex flex-wrap gap-1.5" aria-label="Market sessions">
              {d.markets.map((m) => (
                <Tip key={m.market} content={`${m.market}: ${MARKET_REASON[m.reason ?? ''] ?? m.reason ?? ''}${m.open ? (m.every_s ? ` · polled every ${m.every_s} s` : '') : m.opens_at ? ` · opens ${fmtDateTime(m.opens_at)}` : ''} · ${m.n_targets ?? 0} securities`}>
                  <span tabIndex={0}>
                    <Badge tone={m.open ? 'up' : 'neutral'}>{m.market}</Badge>
                  </span>
                </Tip>
              ))}
            </div>
          )}
          <p className="text-2xs text-muted">
            Quotes are delayed (0–20 min by exchange, personal use) and never replace the official daily close. A closed market idles with its reason; a failing
            source backs off (shown with the retry time); a missing API key disables that feed with the reason.
          </p>
        </div>
      )}
    </Panel>
  )
}
