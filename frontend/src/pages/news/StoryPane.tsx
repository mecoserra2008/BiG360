import { ExternalLink, FileText, Radio, VolumeX } from 'lucide-react'
import { Link } from 'react-router'
import { useNewsStory } from '@/api/news/hooks'
import { useNewsLive } from '@/api/news/stream'
import { type NewsImpact, type NewsStoryDetail, TIER_LABEL, type TermRow, carPctLabel, moveLabel, sentimentTone } from '@/api/news/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { ErrorState, Skeleton } from '@/components/ui/feedback'
import { DASH, fmtDateTime, fmtInt, fmtRelative } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { cn } from '@/lib/utils'

function Timeline({ points }: { points: NewsStoryDetail['timeline'] }) {
  if (!points.length) return <p className="text-xs text-muted">No timeline yet.</p>
  const max = Math.max(...points.map((p) => p.articles), 1)
  const last = points.slice(-48)
  return (
    <div>
      <div className="flex h-16 items-end gap-px" role="img" aria-label={`Articles per hour, ${last.length} hours`}>
        {last.map((p) => (
          <div key={p.ts} className="min-w-[3px] flex-1 rounded-t-sm bg-accent/70" style={{ height: `${Math.max(6, (p.articles / max) * 100)}%` }} title={`${fmtDateTime(p.ts)} · ${p.articles} articles`} />
        ))}
      </div>
      <div className="mt-1 flex justify-between text-2xs text-faint">
        <span>{fmtDateTime(last[0].ts)}</span>
        <span>{fmtDateTime(last[last.length - 1].ts)}</span>
      </div>
    </div>
  )
}

function Impact({ x }: { x: NewsImpact | null | undefined }) {
  if (!x)
    return (
      <p className="text-xs text-muted">
        No event-study cell matches this story&apos;s themes yet <Badge tone="info">model-based</Badge>
      </p>
    )
  const car = x.mean_car
  // the event-study library stores CAR / CI in percent (1.23 = +1.23 %): format without the x100 of moveLabel
  const u = x.unit ?? 'percent'
  return (
    <div className="rounded-md border border-border bg-surface-2 p-2 text-xs" data-testid="news-impact">
      <div className="mb-1 flex items-center gap-2">
        <span className="font-semibold text-fg">Impact estimate</span>
        <Badge tone="info">model-based</Badge>
      </div>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span>
          Mean CAR {x.window ?? '0,5'} d: <span className={cn('num font-semibold', (car ?? 0) > 0 ? 'text-up' : (car ?? 0) < 0 ? 'text-down' : 'text-fg')}>{carPctLabel(car, u)}</span>
        </span>
        <span className="num text-muted">
          CI {carPctLabel(x.ci_low, u)} … {carPctLabel(x.ci_high, u)} · n = {fmtInt(x.n_events)}
          {typeof x.p_value === 'number' ? ` · p = ${x.p_value.toFixed(3)}` : ''}
        </span>
      </div>
      <p className="mt-1 text-2xs text-muted">
        {x.theme} · {x.iso2 ?? 'ALL'} · {x.sector ?? 'ALL'} ({x.level}, match {x.match}). {x.label}
      </p>
    </div>
  )
}

export function StoryPane({ storyId, item, onMute }: { storyId: string | null; item: TermRow | null; onMute: (id: string) => void }) {
  const q = useNewsStory(storyId)
  const live = useNewsLive((s) => (storyId ? s.stories[storyId] : undefined))
  if (!item && !storyId)
    return <p className="p-6 text-center text-sm text-muted">Select an item or a story: j / k to move, Enter to open, o to open the article, s to mute its story.</p>
  const d = q.data
  const st = d?.story
  return (
    <div className="flex min-h-0 flex-col gap-3 overflow-auto p-3" data-testid="news-detail">
      {item && (
        <div>
          <div className="mb-1 flex flex-wrap items-center gap-1.5 text-2xs text-muted">
            <Badge tone={item.tier === 1 ? 'accent' : item.tier === 2 ? 'info' : 'neutral'}>{TIER_LABEL[item.tier]}</Badge>
            <span className="font-medium">{item.publisher}</span>
            <span className="num">{fmtDateTime(item.published_at ?? item.first_seen_at)}</span>
            {typeof item.latency_s === 'number' && <span className="num" title="first seen by the live path - source publication time">· seen +{Math.max(0, Math.round(item.latency_s))} s</span>}
            {item.language && <span className="uppercase">· {item.language}</span>}
          </div>
          <h2 className={cn('text-md font-semibold', sentimentTone(item) === 'up' ? 'text-up' : sentimentTone(item) === 'down' ? 'text-down' : 'text-fg')}>{item.title}</h2>
          {item.snippet && <p className="mt-1 text-sm text-muted">{item.snippet}</p>}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {item.url && (
              <a href={item.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-xs font-medium text-accent-text hover:underline">
                Open article <ExternalLink className="size-3" aria-hidden />
              </a>
            )}
            {typeof item.sentiment === 'number' && (
              <span className="text-xs text-muted">
                Sentiment <span className="num font-semibold">{item.sentiment.toFixed(2)}</span> <Badge tone="info">model-based</Badge>
              </span>
            )}
            {item.story_id && (
              <Button size="xs" variant="ghost" onClick={() => onMute(item.story_id as string)} title="Hide this story (s)">
                <VolumeX /> Mute story
              </Button>
            )}
          </div>
        </div>
      )}
      {storyId && q.isLoading && <Skeleton className="h-40 w-full" />}
      {storyId && q.isError && <ErrorState error={q.error} onRetry={() => void q.refetch()} />}
      {st && d && (
        <>
          <section aria-label="Story">
            <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Story</h3>
            <p className="text-sm font-medium text-fg">{st.title}</p>
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-2xs text-muted">
              <span className="num">
                {fmtInt(Math.max(st.n_articles, live?.n_articles ?? 0))} articles · {fmtInt(Math.max(st.n_sources, live?.n_sources ?? 0))} sources
              </span>
              <span className="num">first {st.first_ts ? fmtRelative(st.first_ts) : DASH} · last {st.last_ts ? fmtRelative(st.last_ts) : DASH}</span>
              {(live?.velocity_1h ?? st.velocity_1h) ? (
                <Badge tone="warn" title="articles in the last 60 min vs the story's own baseline">
                  {live?.velocity_1h ?? st.velocity_1h}/h · {(live?.velocity_ratio ?? st.velocity_ratio ?? 0).toFixed(1)}×
                </Badge>
              ) : null}
              {st.countries.slice(0, 4).map((c) => (c.length === 2 ? <Flag key={c} country={c} /> : <Badge key={c}>{c}</Badge>))}
              {st.themes.slice(0, 4).map((t) => (
                <Badge key={t}>{t.replace(/_/g, ' ')}</Badge>
              ))}
              {typeof st.sentiment === 'number' && <Badge tone={st.sentiment > 0.2 ? 'up' : st.sentiment < -0.2 ? 'down' : 'neutral'}>sent {st.sentiment.toFixed(2)}</Badge>}
            </div>
          </section>
          <section aria-label="Timeline">
            <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Timeline (articles / hour)</h3>
            <Timeline points={d.timeline} />
          </section>
          <section aria-label="Linked securities">
            <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Linked securities · move since first publication</h3>
            {d.securities.length === 0 ? (
              <p className="text-xs text-muted">No linked securities.</p>
            ) : (
              <table className="w-full text-xs">
                <tbody>
                  {d.securities.map((s) => (
                    <tr key={s.security_id ?? s.ticker ?? ''} className="border-b border-border/60">
                      <td className="py-1 font-semibold">
                        {s.security_id ? (
                          <Link to={securityRoute(s.security_id)} className="text-accent-text hover:underline">
                            {s.ticker ?? s.security_id}
                          </Link>
                        ) : (
                          (s.ticker ?? DASH)
                        )}
                      </td>
                      <td className="truncate py-1 text-muted">{s.name}</td>
                      <td className="num py-1 text-right text-muted" title={s.base_date ? `close ${s.base_date} -> ${s.last_date}` : (s.note ?? '')}>
                        {s.base_date ?? DASH}
                      </td>
                      <td className={cn('num py-1 text-right font-semibold', (s.move_pct ?? 0) > 0 ? 'text-up' : (s.move_pct ?? 0) < 0 ? 'text-down' : 'text-muted')}>{moveLabel(s.move_pct)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section aria-label="Impact">
            <Impact x={d.impact} />
          </section>
          {d.filings.length > 0 && (
            <section aria-label="Related filings">
              <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Related filings</h3>
              <ul className="space-y-1 text-xs">
                {d.filings.map((f) => (
                  <li key={f.filing_id ?? f.url ?? ''} className="flex items-center gap-1.5">
                    <FileText className="size-3 text-muted" aria-hidden />
                    <span className="font-semibold">{f.form_type}</span>
                    {f.url ? (
                      <a href={f.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
                        {f.title ?? f.filing_id}
                      </a>
                    ) : (
                      <span className="truncate">{f.title ?? f.filing_id}</span>
                    )}
                    <span className="num ml-auto text-faint">{f.knowledge_ts ? fmtRelative(f.knowledge_ts) : ''}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {d.events.length > 0 && (
            <section aria-label="Related events">
              <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Related GDELT events</h3>
              <ul className="space-y-1 text-xs">
                {d.events.map((e) => (
                  <li key={e.key} className="flex items-center gap-1.5">
                    <Radio className="size-3 text-muted" aria-hidden />
                    <span>{e.country}</span>
                    <span className="text-muted">{(e.themes ?? []).slice(0, 2).join(', ')}</span>
                    <span className="num text-faint">G {e.goldstein ?? DASH}</span>
                    {e.url && (
                      <a href={e.url} target="_blank" rel="noreferrer" className="ml-auto text-accent-text hover:underline">
                        source
                      </a>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section aria-label="Articles">
            <h3 className="mb-1 text-xs font-semibold tracking-wide text-muted uppercase">Articles ({d.items.length})</h3>
            <ul className="divide-y divide-border/60 text-xs">
              {[...d.items].reverse().slice(0, 60).map((x) => (
                <li key={x.item_key} className="flex items-center gap-2 py-1">
                  <span className="num w-14 shrink-0 text-faint">{x.first_seen_at || x.published_at ? fmtRelative((x.first_seen_at ?? x.published_at) as string) : DASH}</span>
                  <span className="w-28 shrink-0 truncate font-medium text-muted">{x.publisher}</span>
                  {x.url ? (
                    <a href={x.url} target="_blank" rel="noreferrer" className="truncate hover:underline">
                      {x.title}
                    </a>
                  ) : (
                    <span className="truncate">{x.title}</span>
                  )}
                </li>
              ))}
            </ul>
            {d.sources.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {d.sources.slice(0, 12).map((s) => (
                  <span key={s.domain} className="rounded-full border border-border px-1.5 text-2xs text-muted">
                    {s.domain} · {s.articles}
                  </span>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  )
}
