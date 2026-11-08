import { useMemo } from 'react'
import { useEmChangepoints, useEvMap, useEvShocks, useEvThemeMaps } from '@/api/events/hooks'
import { type EvTaxonomy, relativeAttention } from '@/api/events/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { divergingColors, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate, fmtInt } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ModelBadge } from './shared'
import type { MapWindow } from './WorldMapPanel'

const DEFAULT_HEAT_THEMES = ['conflict_military', 'geopolitical_tension', 'sanctions', 'trade_tariffs', 'elections_politics', 'protest_unrest', 'monetary_policy', 'inflation', 'oil_gas', 'natural_disaster', 'cyber', 'banking_financial_stress']

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Country × theme matrix: attention z when the API has it, else relative attention (log2 share vs the country's overall share). */
export function ThemeHeatmap({ tax, themes, win, onCountry }: { tax: EvTaxonomy; themes: string[]; win: MapWindow; onCountry: (c: string) => void }) {
  const t = useChartTokens()
  const cols = themes.length >= 2 ? themes : DEFAULT_HEAT_THEMES
  // the heatmap only needs the window aggregates: keep the per-theme calls' slider timeline minimal
  const w = { start: win.start, end: win.end, days: win.days, sliderDays: 7 }
  const base = useEvMap({ ...w, themes: [] })
  const qs = useEvThemeMaps(cols, w)
  const loading = base.isLoading || qs.some((q) => q.isLoading)
  const m = useMemo(() => {
    if (!base.data) return null
    const total = base.data.countries.reduce((a, c) => a + c.articles, 0)
    const rows = [...base.data.countries]
      .filter((c) => c.country !== 'GLOBAL')
      .sort((a, b) => b.articles - a.articles)
      .slice(0, 24)
    const anyZ = qs.some((q) => q.data?.countries.some((c) => fin(c.attention_z)))
    const cells: [number, number, number | null][] = []
    const raw: Record<string, { z: number | null; articles: number }> = {}
    cols.forEach((_th, j) => {
      const d = qs[j]?.data
      const by = new Map((d?.countries ?? []).map((c) => [c.country, c]))
      const thTotal = (d?.countries ?? []).reduce((a, c) => a + c.articles, 0)
      rows.forEach((r, i) => {
        const c = by.get(r.country)
        const v = anyZ ? (fin(c?.attention_z) ? (c?.attention_z as number) : null) : relativeAttention(c?.articles ?? 0, thTotal, r.articles, total)
        cells.push([j, i, v === null ? null : Math.round(v * 100) / 100])
        raw[`${i}:${j}`] = {
          z: c?.attention_z ?? null,
          articles: c?.articles ?? 0,
        }
      })
    })
    return { rows, cells, anyZ, raw }
  }, [base.data, qs, cols])
  const option = useMemo<EChartsOption | null>(() => {
    if (!m) return null
    const name = (id: string) => tax.themes.find((x) => x.id === id)?.name ?? id
    const lim = m.anyZ ? 4 : 3
    return {
      grid: { left: 8, right: 8, top: 8, bottom: 80, containLabel: true },
      tooltip: {
        formatter: (p: unknown) => {
          const v = (p as { value: [number, number, number | null] }).value
          const r = m.rows[v[1]]
          const raw = m.raw[`${v[1]}:${v[0]}`]
          return `${ttTitle(`${r.name} · ${name(cols[v[0]])}`)}${ttRow(t.accent, m.anyZ ? 'attention z' : 'relative attention (log2)', v[2] === null ? DASH : v[2].toFixed(2))}${ttRow(t.ref, 'articles', fmtInt(raw?.articles ?? 0))}`
        },
      },
      xAxis: {
        type: 'category',
        data: cols.map(name),
        axisLabel: { rotate: 40, fontSize: 10, color: t.fg, interval: 0 },
        splitArea: { show: false },
      },
      yAxis: {
        type: 'category',
        inverse: true,
        data: m.rows.map((r) => r.name),
        axisLabel: {
          fontSize: 10,
          color: t.fg,
          width: 120,
          overflow: 'truncate',
        },
      },
      visualMap: {
        min: -lim,
        max: lim,
        calculable: false,
        orient: 'horizontal',
        left: 'center',
        bottom: 0,
        itemWidth: 10,
        itemHeight: 140,
        inRange: { color: [...divergingColors(t)].reverse() },
        textStyle: { color: t.muted, fontSize: 10 },
        text: m.anyZ ? ['+z', '−z'] : ['over-covered', 'under-covered'],
      },
      series: [
        {
          type: 'heatmap',
          data: m.cells.map((c) => [c[0], c[1], c[2] ?? '-']),
          itemStyle: { borderColor: t.surface, borderWidth: 1 },
          emphasis: { itemStyle: { borderColor: t.fg } },
        },
      ],
    } as EChartsOption
  }, [m, cols, t, tax])
  return (
    <Panel
      title="Country × theme heatmap"
      subtitle={m?.anyZ ? 'Max daily attention z in the window (vs each label’s trailing 90 days)' : 'Relative attention: log2 of a country’s share of the theme vs its share of all coverage (attention z not available for this window yet)'}
      actions={m && !m.anyZ ? <Badge tone="warn">z pending</Badge> : undefined}
    >
      {loading || !option ? (
        <Skeleton className="h-[520px]" />
      ) : (
        <EChart
          option={option}
          height={Math.max(360, (m?.rows.length ?? 0) * 18 + 130)}
          ariaLabel="Country by theme attention heatmap"
          onEvents={{
            click: (p: unknown) => {
              const v = (p as { value?: [number, number] }).value
              if (v && m) onCountry(m.rows[v[1]].country)
            },
          }}
        />
      )}
    </Panel>
  )
}

/** Shocks (attention z / tone breaks) + model-based change points (BOCPD). */
export function ShocksPanel({ tax }: { tax: EvTaxonomy }) {
  const q = useEvShocks(14)
  const cp = useEmChangepoints(30)
  const name = (id: string) => (id === 'all' ? 'all themes' : (tax.themes.find((x) => x.id === id)?.name ?? id))
  return (
    <Panel title="Shocks feed" subtitle="Attention z or tone breaks ≥ 2 per label (last 14 days) · change points (model-based, last 30 days)" flush>
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-40" />}>
        {(d) =>
          d.items.length === 0 ? (
            <p className="border-b border-border px-3 py-2 text-xs text-muted">No attention / tone shocks in the window (z needs 90 days of history; data starts {fmtDate('2026-07-14')}).</p>
          ) : (
            <ul className="max-h-60 overflow-y-auto border-b border-border">
              {d.items.map((s, i) => (
                <li key={i} className="flex items-center gap-2 border-b border-border/60 px-3 py-1 text-xs">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(s.date)}</span>
                  {s.label.type === 'country' && s.label.id.length === 2 && <Flag country={s.label.id} />}
                  <span className="truncate font-medium text-fg">{s.label.name}</span>
                  <span className="truncate text-muted">{name(s.theme)}</span>
                  <Badge tone={s.kind === 'attention' ? 'accent' : s.kind === 'tone_drop' ? 'down' : 'up'}>{s.kind.replace('_', ' ')}</Badge>
                  <span className="num ml-auto">{fin(s.attention_z) ? `z ${s.attention_z.toFixed(1)}` : fin(s.tone_z) ? `tone z ${s.tone_z.toFixed(1)}` : DASH}</span>
                </li>
              ))}
            </ul>
          )
        }
      </QueryView>
      <div className="flex items-center gap-2 px-3 pt-2 text-2xs font-semibold tracking-wide text-muted uppercase">
        Change points <ModelBadge />
      </div>
      <QueryView query={cp} skeleton={<Skeleton className="m-3 h-40" />}>
        {(d) =>
          d.items.length === 0 ? (
            <EmptyState title="No change points" />
          ) : (
            <ul className="max-h-72 overflow-y-auto">
              {d.items.slice(0, 40).map((c, i) => (
                <li key={i} className="flex items-center gap-2 border-b border-border/60 px-3 py-1 text-xs">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(c.date)}</span>
                  {c.label_type === 'country' && c.label.length === 2 && <Flag country={c.label} />}
                  <span className="max-w-[200px] truncate font-medium text-fg" title={c.label}>
                    {c.name ?? c.label}
                  </span>
                  <span className="text-2xs text-muted">
                    {c.label_type} · {c.stream} · {name(c.theme)}
                  </span>
                  <span className={cn('num ml-auto', c.direction === 'up' ? 'text-down' : c.direction === 'down' ? 'text-up' : 'text-muted')} title="Shift in standard deviations of the pre-change regime">
                    {c.direction === 'up' ? '▲' : c.direction === 'down' ? '▼' : '■'} {fin(c.shift_sd) ? `${c.shift_sd.toFixed(1)} sd` : DASH}
                  </span>
                  <span className="num w-12 text-right text-2xs text-muted">p {fin(c.cp_prob) ? c.cp_prob.toFixed(2) : DASH}</span>
                </li>
              ))}
            </ul>
          )
        }
      </QueryView>
    </Panel>
  )
}
