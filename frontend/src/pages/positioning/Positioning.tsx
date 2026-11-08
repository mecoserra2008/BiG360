import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { asOfLine, cellBackground, fmtContracts, fmtMetric, groupRows } from '@/api/positioning/adapters'
import { usePosCrowded, usePosHeatmap } from '@/api/positioning/hooks'
import { ASSET_CLASS_LABEL, FAMILY_HINT, FAMILY_LABEL, METRIC_LABEL, type PosFamily, type PosHeatmap, type PosItem, type PosMetric } from '@/api/positioning/types'
import { Badge } from '@/components/ui/badge'
import { EmptyState, QueryView, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroNav } from '../macro/shared'

const FAMILIES: PosFamily[] = ['legacy', 'disagg', 'tff', 'cit']
const METRICS: PosMetric[] = ['cot_index_3y', 'cot_index_1y', 'z_3y', 'net_pct_oi']
const CLASSES = ['all', 'energy', 'metals', 'ags', 'softs', 'livestock', 'rates', 'equity', 'fx', 'crypto'] as const

/** Markets → Positioning (CFTC): heatmap of COT indices by market x trader category, crowded longs / shorts. */
export default function Positioning() {
  const [params, setParams] = useSearchParams()
  const fam = (FAMILIES as string[]).includes(params.get('report') ?? '') ? (params.get('report') as PosFamily) : 'legacy'
  const [metric, setMetric] = useState<PosMetric>('cot_index_3y')
  const [combined, setCombined] = useState(false)
  const ac = (CLASSES as readonly string[]).includes(params.get('class') ?? '') ? (params.get('class') as string) : 'all'
  const set = (k: string, v: string) =>
    setParams((p) => {
      p.set(k, v)
      return p
    })
  const acArg = ac === 'all' ? undefined : ac
  const hm = usePosHeatmap(fam, metric, combined, acArg)
  const cr = usePosCrowded(combined, acArg)
  return (
    <>
      <PageHeader
        title="Positioning (CFTC)"
        subtitle="Commitments of Traders: who is long and short across futures markets — COT index (where net positioning sits in its 1- and 3-year range), z-scores, extremes and crowding"
      />
      <MacroNav />
      <div className="space-y-3">
        <Panel>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
            <Segmented label="Report type" value={fam} onChange={(v) => set('report', v)} options={FAMILIES.map((f) => ({ value: f, label: FAMILY_LABEL[f], title: FAMILY_HINT[f] }))} />
            <Segmented label="Metric" size="xs" value={metric} onChange={setMetric} options={METRICS.map((m) => ({ value: m, label: METRIC_LABEL[m] }))} />
            <Segmented
              label="Futures only or combined with options"
              size="xs"
              value={combined ? 'comb' : 'fut'}
              onChange={(v) => setCombined(v === 'comb')}
              options={[
                { value: 'fut', label: 'Futures only' },
                { value: 'comb', label: 'Futures + options' },
              ]}
            />
            <span className="ml-auto text-2xs text-muted">{hm.data ? asOfLine(hm.data) : ''}</span>
          </div>
          <nav aria-label="Asset classes" className="mt-2 flex flex-wrap gap-1">
            {CLASSES.map((c) => (
              <button
                key={c}
                type="button"
                onClick={() => set('class', c)}
                aria-pressed={ac === c}
                className={cn('h-6 rounded-md border px-2 text-xs font-medium', ac === c ? 'border-accent bg-surface-3 text-fg' : 'border-border text-muted hover:text-fg')}
              >
                {c === 'all' ? 'All' : ASSET_CLASS_LABEL[c]}
              </button>
            ))}
          </nav>
        </Panel>
        <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_420px]">
          <Panel title="Positioning heatmap" subtitle={`${FAMILY_LABEL[fam]} · ${METRIC_LABEL[metric]} by trader category · headline markets`} flush>
            <QueryView query={hm} skeleton={<Skeleton className="m-3 h-[480px]" />}>
              {(d) => <Heatmap d={d} metric={metric} fam={fam} />}
            </QueryView>
          </Panel>
          <div className="space-y-3">
            <Panel title="Crowded longs" subtitle="Model-based: COT index 3y ≥ 90, headline speculators, curated markets" flush>
              <QueryView query={cr} skeleton={<SkeletonRows rows={6} className="p-3" />}>
                {(d) => <CrowdList items={d.longs} empty="No market at a long extreme." />}
              </QueryView>
            </Panel>
            <Panel title="Crowded shorts" subtitle="Model-based: COT index 3y ≤ 10" flush>
              <QueryView query={cr} skeleton={<SkeletonRows rows={6} className="p-3" />}>
                {(d) => <CrowdList items={d.shorts} empty="No market at a short extreme." />}
              </QueryView>
            </Panel>
            <Panel title="Positioning vs price divergences" subtitle="Model-based: 13-week net change and price return of opposite sign (|z| ≥ 1)" flush>
              <QueryView query={cr} skeleton={<SkeletonRows rows={4} className="p-3" />}>
                {(d) => <CrowdList items={d.divergences} empty="No divergence flagged this week." divergence />}
              </QueryView>
            </Panel>
          </div>
        </div>
        <p className="text-2xs text-muted">
          Source: CFTC (official) Commitments of Traders via the CFTC Public Reporting Environment. Positions as of Tuesday, published Friday 15:30 ET. COT index = position of the net (long − short) within its trailing 52- or
          156-week range (0 = most short, 100 = most long). {cr.data?.definition ? 'Extremes, crowding and divergences are model-based heuristics, not CFTC data.' : ''}
        </p>
      </div>
    </>
  )
}

function Legend({ metric }: { metric: PosMetric }) {
  const lo = metric.startsWith('cot') ? '0 · most short' : metric.startsWith('z') ? '−2.5σ' : '−40% OI'
  const hi = metric.startsWith('cot') ? '100 · most long' : metric.startsWith('z') ? '+2.5σ' : '+40% OI'
  return (
    <div className="flex items-center gap-2 text-2xs text-muted" aria-label="Colour scale">
      <span>{lo}</span>
      <span className="h-2 w-28 rounded-sm" style={{ background: 'linear-gradient(90deg, color-mix(in srgb, var(--c1) 62%, var(--surface)), var(--surface), color-mix(in srgb, var(--c2) 62%, var(--surface)))' }} />
      <span>{hi}</span>
      <span className="ml-2 inline-flex items-center gap-1">
        <span className="rounded-sm px-1 ring-2 ring-fg/70">L</span>/<span className="rounded-sm px-1 ring-2 ring-fg/70">S</span> extreme (model-based)
      </span>
    </div>
  )
}

function Heatmap({ d, metric, fam }: { d: PosHeatmap; metric: PosMetric; fam: PosFamily }) {
  if (!d.rows.length) return <EmptyState title="No markets" message={d.data_status === 'pending' ? 'CFTC positioning has not been ingested yet.' : 'No market of this report type in the selected asset class.'} className="m-3" />
  const groups = groupRows(d)
  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2">
        <Legend metric={metric} />
        <span className="text-2xs text-muted">Speculators − hedgers: {d.spec_hedger_definition}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] border-separate border-spacing-[2px] text-xs" aria-label="Positioning heatmap">
          <thead>
            <tr className="text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-2 py-1">Market</th>
              {d.categories.map((c) => (
                <th key={c.id} className="px-1 py-1 text-center">
                  {c.label}
                </th>
              ))}
              <th className="px-2 py-1 text-right">As of</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <HeatGroup key={g.asset_class} label={ASSET_CLASS_LABEL[g.asset_class] ?? g.asset_class} rows={g.rows} cats={d.categories} metric={metric} fam={fam} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function HeatGroup({ label, rows, cats, metric, fam }: { label: string; rows: PosHeatmap['rows']; cats: PosHeatmap['categories']; metric: PosMetric; fam: PosFamily }) {
  return (
    <>
      <tr>
        <td colSpan={cats.length + 2} className="px-2 pt-2 pb-0.5 text-2xs font-semibold tracking-wide text-muted uppercase">
          {label}
        </td>
      </tr>
      {rows.map((r) => (
        <tr key={r.market_code}>
          <td className="max-w-[220px] truncate px-2 py-0.5">
            <Link to={`/macro/positioning/${encodeURIComponent(r.market_code)}?report=${fam}`} className="font-medium text-accent-text hover:underline">
              {r.name}
            </Link>
          </td>
          {r.values.map((v, i) => {
            const x = r.extremes[i]
            return (
              <td key={cats[i].id} className="p-0">
                <Tip content={`${r.name} · ${cats[i].label}: ${METRIC_LABEL[metric]} ${fmtMetric(v, metric)}${x ? ` · ${x} extreme (model-based)` : ''}${r.crowded[i] ? ' · crowded' : ''}`}>
                  <div
                    className={cn('num flex h-6 min-w-[52px] items-center justify-center gap-1 rounded-sm text-fg', x && 'ring-2 ring-fg/70 ring-inset')}
                    style={{ background: cellBackground(v, metric) }}
                  >
                    {fmtMetric(v, metric)}
                    {x && <span className="text-2xs font-bold">{x === 'long' ? 'L' : 'S'}</span>}
                  </div>
                </Tip>
              </td>
            )
          })}
          <td className="num px-2 py-0.5 text-right text-2xs text-muted">{r.report_date ?? DASH}</td>
        </tr>
      ))}
    </>
  )
}

function CrowdList({ items, empty, divergence = false }: { items: PosItem[]; empty: string; divergence?: boolean }) {
  if (!items.length) return <p className="px-3 py-4 text-center text-xs text-muted">{empty}</p>
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
          <th className="px-3 py-1">Market</th>
          <th className="px-2 py-1">Category</th>
          <th className="px-2 py-1 text-right">COT 3y</th>
          <th className="px-2 py-1 text-right">{divergence ? 'Div. 13w' : 'z 3y'}</th>
          <th className="px-3 py-1 text-right">Net</th>
        </tr>
      </thead>
      <tbody>
        {items.map((x) => (
          <tr key={x.market_code} className="border-b border-border/60">
            <td className="max-w-[150px] truncate px-3 py-1">
              <Link to={`/macro/positioning/${encodeURIComponent(x.market_code)}`} className="font-medium text-accent-text hover:underline">
                {x.name}
              </Link>
            </td>
            <td className="px-2 py-1 text-muted">
              {x.category_label}
              {x.crowded && (
                <Badge tone="warn" className="ml-1">
                  crowded
                </Badge>
              )}
              {divergence && x.divergence_flag && <Badge className="ml-1">{x.divergence_flag}</Badge>}
            </td>
            <td className="num px-2 py-1 text-right">{fmtMetric(x.cot_index_3y, 'cot_index_3y')}</td>
            <td className="num px-2 py-1 text-right">{divergence ? fmtMetric(x.divergence_13w, 'z_3y') : fmtMetric(x.z_3y, 'z_3y')}</td>
            <td className="num px-3 py-1 text-right">{fmtContracts(x.net)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
