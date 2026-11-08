import { ExternalLink } from 'lucide-react'
import { useMemo } from 'react'
import type { CompanySentiment, EventCategory } from '@/api/types'
import { useCompanySentiment } from '@/api/hooks'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, divergingColors, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtDate, fmtDateTime, fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'

export default function SentimentTab({ id }: { id: string }) {
  const q = useCompanySentiment(id)
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
      {(d) => <SentimentBody d={d} />}
    </QueryView>
  )
}

const CATS: EventCategory[] = ['Policy', 'Economic', 'Legal', 'Corporate', 'Diplomacy', 'Protest', 'Disaster', 'Conflict']

export function SentimentChip({ v }: { v: number }) {
  const label = v > 0.15 ? 'Pos' : v < -0.15 ? 'Neg' : 'Neu'
  return (
    <span className={cn('num inline-flex h-5 w-[70px] shrink-0 items-center justify-center gap-1 rounded-sm text-2xs font-semibold', v > 0.15 ? 'bg-up-soft text-up' : v < -0.15 ? 'bg-down-soft text-down' : 'bg-surface-3 text-muted')}>
      {v > 0.15 ? '▲' : v < -0.15 ? '▼' : '■'} {label} {fmtNum(v, 2, true)}
    </span>
  )
}

function SentimentBody({ d }: { d: CompanySentiment }) {
  const t = useChartTokens()
  const linked = useMemo<EChartsOption>(() => {
    const dates = d.index.map((p) => p.date)
    return {
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { axisValue: string; seriesName: string; value: number; color: string }[]
          return ttTitle(fmtDate(arr[0]?.axisValue)) + arr.map((p) => ttRow(p.color, p.seriesName, p.seriesName === 'Close' ? fmtNum(p.value, 2) : p.seriesName === 'News volume' ? String(p.value) : fmtNum(p.value, 2, true))).join('')
        },
      },
      grid: [
        { left: 8, right: 14, top: 12, height: '46%', containLabel: true },
        { left: 8, right: 14, top: '60%', bottom: 8, containLabel: true },
      ],
      xAxis: [
        { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, boundaryGap: false },
        { type: 'category', data: dates, gridIndex: 1, boundaryGap: false, axisLabel: { formatter: (v: string) => v.slice(0, 7) } },
      ],
      yAxis: [
        { type: 'value', gridIndex: 0, scale: true, name: 'Price', nameTextStyle: { align: 'left' } },
        { type: 'value', gridIndex: 1, min: -1, max: 1, splitNumber: 2, name: 'Sentiment', nameTextStyle: { align: 'left' } },
      ],
      series: [
        { name: 'Close', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: d.index.map((p) => p.close), showSymbol: false, lineStyle: { color: t.fg, width: 1.5 }, itemStyle: { color: t.fg } },
        {
          name: 'Sentiment index',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          barCategoryGap: '10%',
          data: d.index.map((p) => ({ value: p.sentiment, itemStyle: { color: p.sentiment >= 0 ? t.up : t.down } })),
        },
      ],
    }
  }, [d.index, t])

  const timeline = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 14, top: 8, bottom: 8, containLabel: true },
    tooltip: {
      formatter: (p: unknown) => {
        const x = p as { data: { value: [string, string, number, number]; name: string } }
        return ttTitle(x.data.name) + ttRow(t.fg, 'Date', fmtDate(x.data.value[0])) + ttRow(t.fg, 'Avg tone', fmtNum(x.data.value[2], 2, true)) + ttRow(t.fg, 'Intensity', fmtPct(x.data.value[3], 0))
      },
    },
    xAxis: { type: 'time' },
    yAxis: { type: 'category', data: CATS, axisLine: { show: false } },
    visualMap: { show: false, dimension: 2, min: -6, max: 6, inRange: { color: divergingColors(t) } },
    series: [{ type: 'scatter', data: d.events.map((e) => ({ name: e.headline, value: [e.date, e.category, e.tone, e.intensity] })), symbolSize: (v: unknown) => 8 + (v as number[])[3] * 18, itemStyle: { borderColor: t.surface, borderWidth: 2, opacity: 0.9 } }],
  }), [d.events, t])

  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel className="col-span-12">
        <div className="flex flex-wrap gap-x-10 gap-y-2">
          <Stat label="Sentiment index" value={fmtNum(d.currentIndex, 2, true)} sub="FinBERT / multilingual, −1…+1" />
          {d.score != null && <Stat label="Sentiment score" value={fmtNum(d.score, 0)} sub="0–100 percentile vs universe" />}
          <Stat label="Δ 30 days" value={<Delta value={d.change30d} format={(v) => fmtNum(v, 2, true)} />} />
          <Stat label="GDELT avg tone 30d" value={fmtNum(d.toneAvg30d, 2, true)} />
          <Stat label="Lazy-prices similarity" value={d.lazyPricesSimilarity === null ? 'n/a' : fmtNum(d.lazyPricesSimilarity, 3)} sub="10-K/10-Q text vs prior filing" />
        </div>
      </Panel>
      <Panel title="Price and sentiment" subtitle="Linked panels, shared time axis (one scale per panel)" className="col-span-12 xl:col-span-8" bodyClassName="p-1.5">
        <EChart option={linked} height={360} ariaLabel="Price and news sentiment index over one year" />
      </Panel>
      <Panel title="News" subtitle="Per-item sentiment · items with relevance < 0.5 are dimmed" className="col-span-12 xl:col-span-4" flush>
        <ul className="max-h-[392px] overflow-y-auto">
          {d.news.map((n) => (
            <li key={n.id} className={cn("flex gap-2 border-b border-border px-3 py-2", n.relevance < 0.5 && "opacity-50")} title={n.relevance < 0.5 ? "Low issuer-link confidence (relevance < 0.5)" : undefined}>
              <SentimentChip v={n.sentiment} />
              <div className="min-w-0 flex-1">
                <a href={n.url} className="line-clamp-2 text-sm text-fg hover:text-accent-text" onClick={(e) => n.url === '#' && e.preventDefault()}>
                  {n.headline}
                  {n.url !== '#' && <ExternalLink className="ml-1 inline size-3" aria-hidden />}
                </a>
                <div className="mt-0.5 flex items-center gap-2 text-2xs text-muted">
                  <span>{n.source}</span>
                  <span className="num">{fmtDateTime(n.publishedAt)}</span>
                  <span className="uppercase">{n.language}</span>
                  <span title="Relevance" className="num ml-auto">rel {fmtPct(n.relevance, 0)}</span>
                </div>
              </div>
            </li>
          ))}
        </ul>
      </Panel>
      <Panel title="GDELT event timeline" subtitle="Bubble = intensity · colour = tone (rose negative → teal positive)" className="col-span-12 xl:col-span-8" bodyClassName="p-1.5">
        <EChart option={timeline} height={260} ariaLabel="Timeline of related GDELT events by category" />
      </Panel>
      <Panel title="Recent events" className="col-span-12 xl:col-span-4" flush>
        <ul className="max-h-[292px] overflow-y-auto">
          {d.events.map((e) => (
            <li key={e.id} className="border-b border-border px-3 py-1.5 text-sm">
              <div className="flex items-center gap-2">
                <Badge>{e.category}</Badge>
                <span className="num text-2xs text-muted">{fmtDate(e.date)}</span>
                <span className={cn('num ml-auto text-xs', e.tone < 0 ? 'text-down' : 'text-up')}>{e.tone < 0 ? '▼' : '▲'} {fmtNum(e.tone, 1, true)}</span>
              </div>
              <div className="mt-0.5 truncate text-muted" style={{ color: undefined }}>{e.headline}</div>
              <div className="mt-1 h-1 rounded-full bg-surface-3"><div className="h-1 rounded-full" style={{ width: `${e.intensity * 100}%`, background: alpha(t.ref, 0.8) }} /></div>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  )
}
