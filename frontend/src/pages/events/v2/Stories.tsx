import * as Dialog from '@radix-ui/react-dialog'
import { ExternalLink, X } from 'lucide-react'
import { useDeferredValue, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useEvStories, useEvStory } from '@/api/events/hooks'
import { type EvStory, type EvStoryDetail, type EvTaxonomy, type StoryFilter, toneBucket } from '@/api/events/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDateTime, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export function ToneChip({ tone }: { tone: number | null | undefined }) {
  const b = toneBucket(tone)
  return (
    <span className={cn('num rounded-sm px-1 text-2xs font-semibold', b < 0 ? 'bg-down-soft text-down' : b > 0 ? 'bg-up-soft text-up' : 'bg-surface-3 text-muted')} title="Mean GDELT tone of the story's articles">
      tone {fin(tone) ? `${tone > 0 ? '+' : tone < 0 ? '−' : ''}${Math.abs(tone).toFixed(1)}` : DASH}
    </span>
  )
}

/** One story row (shared with the Company 360 / country panels). */
export function StoryRow({ s, tax, onOpen }: { s: EvStory; tax?: EvTaxonomy; onOpen: (id: string) => void }) {
  const themeName = (id: string) => tax?.themes.find((t) => t.id === id)?.name ?? id.replace(/_/g, ' ')
  return (
    <li className="border-b border-border/60 px-3 py-2">
      <div className="flex items-start gap-2">
        <span className="num mt-0.5 w-10 shrink-0 text-right text-xs font-semibold text-accent-text" title="Salience (model-free: coverage × spread × recency)">
          {s.salience.toFixed(1)}
        </span>
        <div className="min-w-0 flex-1">
          <button type="button" className="text-left text-sm font-medium text-fg hover:underline" onClick={() => onOpen(s.story_id)}>
            {s.title}
          </button>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-2xs text-muted">
            <span className="num">
              {fmtInt(s.n_articles)} articles · {fmtInt(s.n_sources)} sources · {s.n_days}d
            </span>
            <ToneChip tone={s.tone} />
            {s.countries.slice(0, 4).map((c) => (c.length === 2 ? <Flag key={c} country={c} /> : <Badge key={c}>{c}</Badge>))}
            {s.themes.slice(0, 3).map((t) => (
              <Badge key={t}>{themeName(t)}</Badge>
            ))}
            {s.securities
              .filter((x) => x.security_id)
              .slice(0, 4)
              .map((x) => (
                <Link
                  key={x.security_id}
                  to={`/company/${encodeURIComponent(x.security_id ?? '')}?tab=events`}
                  className="rounded-sm bg-accent-soft px-1 font-semibold text-accent-text hover:underline"
                  title={`${x.name} · relevance ${x.relevance?.toFixed(2) ?? '—'}`}
                >
                  {x.ticker}
                </Link>
              ))}
          </div>
          {s.domains.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {s.domains.slice(0, 5).map((d) => (
                <span key={d} className="rounded-full border border-border px-1.5 text-2xs text-muted">
                  {d}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  )
}

export function StoriesPanel({ tax, country, onCountry, themes, days, start, end }: { tax: EvTaxonomy; country: string | null; onCountry: (c: string | null) => void; themes: string[]; days: number; start?: string; end?: string }) {
  const [sector, setSector] = useState('')
  const [secText, setSecText] = useState('')
  const security = useDeferredValue(secText.trim().toUpperCase())
  const [theme, setTheme] = useState('')
  const [tone, setTone] = useState<'all' | 'neg' | 'pos'>('all')
  const [minSources, setMinSources] = useState('1')
  const [open, setOpen] = useState<string | null>(null)
  const f: StoryFilter = {
    country: country ?? undefined,
    sector,
    securityId: security || undefined,
    theme: theme || (themes.length === 1 ? themes[0] : ''),
    tone,
    days,
    start,
    end,
  }
  const q = useEvStories(f, 60)
  const rows = useMemo(() => (q.data?.items ?? []).filter((s) => s.n_sources >= Number(minSources)).slice(0, 25), [q.data, minSources])
  const countries = useMemo(() => tax.countries.filter((c) => c.id.length === 2 || c.id === 'GLOBAL'), [tax])
  return (
    <Panel title="What's moving" subtitle={`Top stories by salience (clustered news) · ${q.data ? `${fmtInt(q.data.total)} stories in the window` : '…'}`} flush>
      <div className="border-b border-border px-3 py-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Select label="Country" value={country ?? ''} onChange={(v) => onCountry(v || null)} options={[{ value: '', label: 'All countries' }, ...countries.map((c) => ({ value: c.id, label: c.name }))]} className="w-36" />
          <Select label="Sector" value={sector} onChange={setSector} options={[{ value: '', label: 'All sectors' }, ...tax.sectors.map((s) => ({ value: s.id, label: s.name }))]} className="w-36" />
          <Input aria-label="Security ticker or id" placeholder="Ticker" value={secText} onChange={(e) => setSecText(e.target.value)} className="w-20" />
          <Select
            label="Theme"
            value={theme}
            onChange={setTheme}
            options={[
              {
                value: '',
                label: themes.length === 1 ? 'Map theme' : 'All themes',
              },
              ...tax.themes.map((t) => ({ value: t.id, label: t.name })),
            ]}
            className="w-40"
          />
          <Segmented
            label="Tone"
            size="xs"
            value={tone}
            onChange={setTone}
            options={[
              { value: 'all', label: 'Any tone' },
              { value: 'neg', label: 'Negative' },
              { value: 'pos', label: 'Positive' },
            ]}
          />
          <Select
            label="Minimum sources"
            value={minSources}
            onChange={setMinSources}
            options={['1', '3', '10', '25', '50'].map((v) => ({
              value: v,
              label: `≥ ${v} sources`,
            }))}
            className="w-28"
          />
        </div>
      </div>
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-80" />}>
        {() =>
          rows.length === 0 ? (
            <EmptyState title="No stories" message="No stories match these filters in the window." />
          ) : (
            <ul className="max-h-[620px] overflow-y-auto">
              {rows.map((s) => (
                <StoryRow key={s.story_id} s={s} tax={tax} onOpen={setOpen} />
              ))}
            </ul>
          )
        }
      </QueryView>
      <StoryDrawer id={open} onClose={() => setOpen(null)} tax={tax} />
    </Panel>
  )
}

export function StoryDrawer({ id, onClose, tax }: { id: string | null; onClose: () => void; tax?: EvTaxonomy }) {
  return (
    <Dialog.Root open={!!id} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content className="fixed top-0 right-0 z-50 flex h-full w-[min(620px,100vw)] flex-col border-l border-border-strong bg-surface focus-visible:outline-none" aria-describedby={undefined}>
          {id && <StoryBody id={id} onClose={onClose} tax={tax} />}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}

function StoryBody({ id, onClose, tax }: { id: string; onClose: () => void; tax?: EvTaxonomy }) {
  const q = useEvStory(id)
  return (
    <>
      <header className="flex items-start gap-2 border-b border-border px-4 py-3">
        <Dialog.Title className="min-w-0 flex-1 text-md font-semibold text-fg">{q.data?.story.title ?? 'Story'}</Dialog.Title>
        <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
          <X />
        </Button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <QueryView query={q} skeleton={<Skeleton className="h-80" />}>
          {(d) => <StoryDetailView d={d} tax={tax} />}
        </QueryView>
      </div>
    </>
  )
}

function StoryDetailView({ d, tax }: { d: EvStoryDetail; tax?: EvTaxonomy }) {
  const t = useChartTokens()
  const s = d.story
  const themeName = (x: string) => tax?.themes.find((y) => y.id === x)?.name ?? x.replace(/_/g, ' ')
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 8, bottom: 20, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: d.timeline.map((p) => p.ts),
        axisLabel: {
          formatter: (v: string) => `${v.slice(5, 10)} ${v.slice(11, 16)}`,
          fontSize: 9,
        },
      },
      yAxis: { type: 'value', splitNumber: 3 },
      series: [
        {
          name: 'articles',
          type: 'bar',
          data: d.timeline.map((p) => p.articles),
          itemStyle: { color: t.accent },
        },
      ],
    }),
    [d.timeline, t],
  )
  return (
    <div className="space-y-4 text-xs">
      <div className="flex flex-wrap items-center gap-2 text-muted">
        <span className="num">
          {fmtInt(s.n_articles)} articles · {fmtInt(s.n_sources)} sources · {fmtInt(s.n_events)} GDELT events
        </span>
        <ToneChip tone={s.tone} />
        {fin(s.sentiment) && <Badge tone="info">sentiment {s.sentiment.toFixed(2)} (model)</Badge>}
        <span>
          {s.first_ts ? fmtDateTime(s.first_ts) : DASH} → {s.last_ts ? fmtDateTime(s.last_ts) : DASH}
        </span>
      </div>
      <section>
        <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Articles per hour</h3>
        {d.timeline.length > 1 ? <EChart option={option} height={140} ariaLabel="Articles per hour" /> : <p className="text-muted">Single time slot.</p>}
      </section>
      {s.securities.length > 0 && (
        <section>
          <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Linked securities</h3>
          <ul className="space-y-0.5">
            {s.securities
              .filter((x) => x.security_id)
              .map((x) => (
                <li key={x.security_id} className="flex items-center gap-2">
                  <Link to={`/company/${encodeURIComponent(x.security_id ?? '')}?tab=events`} className="font-semibold text-accent-text hover:underline">
                    {x.ticker}
                  </Link>
                  <span className="truncate text-muted">{x.name}</span>
                  <span className="num ml-auto text-faint">relevance {x.relevance?.toFixed(2) ?? DASH}</span>
                </li>
              ))}
          </ul>
        </section>
      )}
      <section className="grid grid-cols-2 gap-4">
        <div>
          <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Sources ({d.sources.length})</h3>
          <ul className="max-h-48 space-y-0.5 overflow-y-auto">
            {d.sources.map((x) => (
              <li key={x.domain} className="flex justify-between gap-2">
                <span className="truncate">{x.domain}</span>
                <span className="num text-muted">{x.articles}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Languages</h3>
          <ul className="space-y-0.5">
            {d.languages.map((x) => (
              <li key={x.lang} className="flex justify-between gap-2">
                <span className="uppercase">{x.lang}</span>
                <span className="num text-muted">{x.articles}</span>
              </li>
            ))}
          </ul>
          <h3 className="mt-3 mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Labels</h3>
          <div className="flex flex-wrap gap-1">
            {s.countries.map((c) => (
              <Link key={c} to={c.length === 2 ? `/macro/country/${c}` : '#'} className="rounded-sm border border-border px-1 hover:underline">
                {c}
              </Link>
            ))}
            {s.sectors.map((x) => (
              <Badge key={x}>{x}</Badge>
            ))}
            {s.themes.map((x) => (
              <Badge key={x}>{themeName(x)}</Badge>
            ))}
          </div>
        </div>
      </section>
      {s.urls.length > 0 && (
        <section>
          <h3 className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Representative articles</h3>
          <ul className="space-y-1">
            {s.urls.map((u) => (
              <li key={u}>
                <a href={u} target="_blank" rel="noreferrer" className="inline-flex max-w-full items-center gap-1 text-accent-text hover:underline">
                  <ExternalLink className="size-3 shrink-0" />
                  <span className="truncate">{u.replace(/^https?:\/\//, '')}</span>
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
