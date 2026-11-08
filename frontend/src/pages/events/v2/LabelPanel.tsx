import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useEvLabel, useEvTaxonomy } from '@/api/events/hooks'
import { EV_RANGES, type EvLabelPanel, type EvLabelType, type EvRange, type EvStory, coverageText, rangeDays, rangeFreq } from '@/api/events/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate, fmtInt } from '@/lib/format'
import { StoryDrawer, StoryRow } from './Stories'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Event panel for one label (country / sector / company): coverage + tone trend, theme mix, top stories, shocks. */
/** `stories` (clustered stories of the last `storiesDays`) replace the label's top stories only while the selected
 *  range is that short; longer ranges use the label endpoint's top stories for the range (guide 05c). */
export function LabelEventsPanel({ type, id, title, stories, storiesDays = 30, className }: { type: EvLabelType; id: string; title?: string; stories?: EvStory[]; storiesDays?: number; className?: string }) {
  const tax = useEvTaxonomy()
  const [range, setRange] = useState<EvRange>('90D')
  const days = rangeDays(range, tax.data?.coverage)
  const q = useEvLabel(type, id, days, rangeFreq(days))
  const cov = coverageText(tax.data?.coverage)
  return (
    <Panel
      className={className}
      title={title ?? 'Events'}
      subtitle={`GDELT coverage labelled to this name · ${range === 'All' ? 'full history' : `last ${range}`}${days > 365 ? ' (weekly)' : ''} · theme mix · top stories by salience${cov ? ` · news coverage ${cov}` : ''}`}
      actions={<Segmented label="Events history range" size="xs" value={range} onChange={setRange} options={EV_RANGES.map((r) => ({ value: r, label: r }))} />}
      flush
    >
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-72" />}>
        {(d) => <LabelBody d={d} stories={days <= storiesDays ? stories : undefined} />}
      </QueryView>
    </Panel>
  )
}

function LabelBody({ d, stories }: { d: EvLabelPanel; stories?: EvStory[] }) {
  const t = useChartTokens()
  const tax = useEvTaxonomy()
  const [open, setOpen] = useState<string | null>(null)
  const list = stories ?? d.top_stories
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 24, bottom: 20, containLabel: true },
      legend: { top: 0, left: 0, textStyle: { color: t.muted, fontSize: 10 } },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        data: d.series.map((p) => p.date),
        axisLabel: { formatter: (v: string) => (d.series.length > 120 ? v.slice(0, 7) : v.slice(5)) },
      },
      yAxis: [
        {
          type: 'value',
          splitNumber: 3,
          axisLabel: {
            formatter: (v: number) => (v >= 1000 ? `${Math.round(v / 1000)}k` : String(v)),
          },
        },
        {
          type: 'value',
          splitNumber: 3,
          scale: true,
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: 'articles',
          type: 'bar',
          data: d.series.map((p) => p.articles),
          itemStyle: { color: alpha(t.ref, 0.55) },
          barCategoryGap: '20%',
        },
        {
          name: 'tone',
          type: 'line',
          yAxisIndex: 1,
          data: d.series.map((p) => p.tone),
          showSymbol: false,
          lineStyle: { color: t.series[0], width: 1.5 },
          itemStyle: { color: t.series[0] },
        },
      ],
    }),
    [d.series, t],
  )
  const l = d.latest
  return (
    <div>
      <div className="grid gap-3 p-3 xl:grid-cols-[minmax(0,1.4fr)_minmax(240px,1fr)]">
        <div>
          <div className="mb-1 flex flex-wrap gap-x-4 text-xs text-muted">
            <span>
              Latest ({l ? fmtDate(l.date) : DASH}): <span className="num text-fg">{l ? fmtInt(l.articles) : DASH}</span> articles · <span className="num text-fg">{l ? fmtInt(l.events) : DASH}</span> events
            </span>
            <span>
              tone <span className="num text-fg">{fin(l?.tone) ? l.tone.toFixed(2) : DASH}</span>
            </span>
            <span>
              attention z <span className="num text-fg">{fin(l?.attention_z) ? l.attention_z.toFixed(2) : 'n/a'}</span>
            </span>
          </div>
          {d.series.length > 1 ? <EChart option={option} height={180} ariaLabel={`Coverage and tone for ${d.label.name}`} /> : <p className="py-6 text-center text-xs text-muted">No coverage series.</p>}
        </div>
        <div>
          <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Theme mix</div>
          {d.themes.length === 0 && <p className="text-xs text-muted">No theme breakdown for this label.</p>}
          <ul className="space-y-1">
            {d.themes.slice(0, 8).map((th) => (
              <li key={th.theme} className="flex items-center gap-2 text-xs">
                <span className="w-40 truncate" title={th.name}>
                  {th.name}
                </span>
                <span className="relative h-1.5 flex-1 rounded-full bg-surface-3" aria-hidden>
                  <span
                    className="absolute top-0 left-0 h-1.5 rounded-full bg-accent"
                    style={{
                      width: `${Math.min(100, (th.share ?? 0) * 100)}%`,
                    }}
                  />
                </span>
                <span className="num w-10 text-right text-muted">{fin(th.share) ? `${(th.share * 100).toFixed(0)}%` : DASH}</span>
              </li>
            ))}
          </ul>
          {d.shocks.length > 0 && (
            <div className="mt-3">
              <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Recent shocks</div>
              <div className="flex flex-wrap gap-1">
                {d.shocks.slice(0, 8).map((s, i) => (
                  <Badge key={i} tone={s.kind === 'attention' ? 'accent' : s.kind === 'tone_drop' ? 'down' : 'up'}>
                    {fmtDate(s.date)} · {s.kind.replace('_', ' ')}
                  </Badge>
                ))}
              </div>
            </div>
          )}
          <Link to="/events" className="mt-3 inline-block text-xs text-accent-text hover:underline">
            Open Global Events
          </Link>
        </div>
      </div>
      <div className="border-t border-border px-3 pt-2 text-2xs font-semibold tracking-wide text-muted uppercase">Top stories</div>
      {list.length === 0 ? (
        <p className="px-3 py-4 text-xs text-muted">No stories linked in the window.</p>
      ) : (
        <ul className="max-h-[420px] overflow-y-auto">
          {list.slice(0, 15).map((s) => (
            <StoryRow key={s.story_id} s={s} tax={tax.data} onOpen={setOpen} />
          ))}
        </ul>
      )}
      <StoryDrawer id={open} onClose={() => setOpen(null)} tax={tax.data} />
    </div>
  )
}
