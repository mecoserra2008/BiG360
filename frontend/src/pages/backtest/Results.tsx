import { useMutation } from '@tanstack/react-query'
import { ExperimentalNote } from '@/components/conformal/notes'
import { AlertTriangle, Download, Info } from 'lucide-react'
import { type ReactNode, useMemo, useState } from 'react'
import { fin, fmtMetric, METRIC_ROWS, monthlyGrid, overfitVerdict, seriesPoints, stepFor } from '@/api/backtest/adapters'
import { backtestApi } from '@/api/backtest/client'
import { useBtHoldings, useBtTrades } from '@/api/backtest/hooks'
import type {
  BtAttribution,
  BtAttributionModel,
  BtDeciles,
  BtEventBlock,
  BtMetrics,
  BtRebalance,
  BtResult,
  BtRobustnessView,
  BtRun,
} from '@/api/backtest/types'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, SkeletonRows } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import { downloadText } from '@/lib/csv'
import { cn } from '@/lib/utils'
import { BarChart, Histogram, LinesChart, PathChart } from './charts'
import { type BtResultTab, useBacktest } from './store'
import { CreateThesisLink } from '@/pages/theses/shared'

const pct = (v: number) => fmtMetric(v, 'pct')
const num2 = (v: number) => (fin(v) ? v.toFixed(2) : '–')
const navFmt = (v: number) => v.toFixed(2)

function Note({ children, tone = 'info' }: { children: ReactNode; tone?: 'info' | 'warn' }) {
  return (
    <div className={cn('flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs', tone === 'warn' ? 'border-warn/40 bg-warn-soft text-fg' : 'border-border bg-surface-2 text-muted')}>
      {tone === 'warn' ? <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden /> : <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />}
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function MetricTable({ m }: { m: BtResult['metrics'] }) {
  const cols: { key: 'full' | 'in_sample' | 'out_of_sample'; label: string }[] = [
    { key: 'full', label: 'Full' },
    { key: 'in_sample', label: 'In-sample' },
    { key: 'out_of_sample', label: 'Holdout (OOS)' },
  ]
  const have = cols.filter((c) => m[c.key])
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-2xs text-muted uppercase">
          <th className="py-1 font-medium">Metric</th>
          {have.map((c) => (
            <th key={c.key} className={cn('py-1 text-right font-medium', c.key === 'out_of_sample' && 'text-accent-text')}>
              {c.label}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {METRIC_ROWS.filter((r) => have.some((c) => fin((m[c.key] as BtMetrics | undefined)?.[r.key]))).map((r) => (
          <tr key={r.key} className="border-t border-border/60">
            <td className="py-1 text-muted" title={r.hint}>
              {r.label}
            </td>
            {have.map((c) => (
              <td key={c.key} className="num py-1 text-right">
                {fmtMetric((m[c.key] as BtMetrics | undefined)?.[r.key], r.fmt)}
              </td>
            ))}
          </tr>
        ))}
        <tr className="border-t border-border/60">
          <td className="py-1 text-muted">Period</td>
          {have.map((c) => (
            <td key={c.key} className="num py-1 text-right text-xs text-muted">
              {(m[c.key] as BtMetrics).start} → {(m[c.key] as BtMetrics).end}
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  )
}

function MonthlyTable({ monthly }: { monthly: BtResult['monthly'] }) {
  const grid = useMemo(() => monthlyGrid(monthly), [monthly])
  if (!grid.length) return null
  const cell = (v: number | null) => {
    if (!fin(v)) return 'text-faint'
    const a = Math.min(Math.abs(v) / 0.06, 1)
    return v >= 0 ? (a > 0.5 ? 'bg-up-soft text-up' : 'text-up') : a > 0.5 ? 'bg-down-soft text-down' : 'text-down'
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-xs">
        <thead>
          <tr className="text-2xs text-muted">
            <th className="py-1 text-left font-medium">Year</th>
            {['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'].map((x, i) => (
              <th key={i} className="py-1 text-right font-medium">
                {x}
              </th>
            ))}
            <th className="py-1 text-right font-medium">Year</th>
          </tr>
        </thead>
        <tbody>
          {grid.map((r) => (
            <tr key={r.year} className="border-t border-border/50">
              <td className="py-0.5 text-muted">{r.year}</td>
              {r.months.map((v, i) => (
                <td key={i} className={cn('num rounded-sm px-1 py-0.5 text-right', cell(v))}>
                  {fin(v) ? (v * 100).toFixed(1) : ''}
                </td>
              ))}
              <td className={cn('num px-1 py-0.5 text-right font-semibold', cell(r.total))}>{fin(r.total) ? `${(r.total * 100).toFixed(1)}%` : ''}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Performance({ res }: { res: BtResult }) {
  const t = useChartTokens()
  const s = res.series
  const step = stepFor(s.dates.length)
  const nav = seriesPoints(s, 'nav', step)
  const series = [
    { name: 'Strategy (net)', data: nav, color: t.series[0] },
    { name: 'Gross of costs', data: seriesPoints(s, 'gross_nav', step), color: t.series[0], dashed: true, width: 1 },
    ...(s.bench_nav ? [{ name: `Benchmark ${res.summary.benchmark ?? ''}`, data: seriesPoints(s, 'bench_nav', step), color: t.ref }] : []),
  ]
  const full = res.metrics.full
  const oos = res.metrics.out_of_sample
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-7">
        <Stat label="CAGR" value={pct(full.cagr as number)} sub={`bench ${fmtMetric(full.bench_cagr, 'pct')}`} />
        <Stat label="Sharpe" value={num2(full.sharpe as number)} sub={oos ? `holdout ${num2(oos.sharpe as number)}` : undefined} />
        <Stat label="Sortino" value={num2(full.sortino as number)} />
        <Stat label="Max drawdown" value={pct(full.max_drawdown as number)} sub={full.max_drawdown_trough ? `trough ${full.max_drawdown_trough}` : undefined} />
        <Stat label="Volatility" value={pct(full.vol as number)} />
        <Stat label="Turnover / yr" value={fin(full.annual_turnover) ? `${(full.annual_turnover * 100).toFixed(0)}%` : '–'} sub={fin(full.cost_drag_bps) ? `costs ${full.cost_drag_bps.toFixed(0)} bp/yr` : undefined} />
        <Stat label="Hit rate" value={pct(full.hit_rate as number)} sub={`${pct(full.pct_months_positive as number)} months +`} />
      </div>
      <Panel title="Equity curve" subtitle={`NAV (start = 1) · ${res.summary.execution} · holdout from ${res.summary.oos_start}`}>
        <TimeSeriesChart series={series} format={navFmt} height={300} ariaLabel="Equity curve of the strategy versus the benchmark" threshold={undefined} />
      </Panel>
      <Panel title="Drawdown">
        <TimeSeriesChart
          series={[
            { name: 'Strategy', data: seriesPoints(s, 'drawdown', step), color: t.down, area: true },
            ...(s.bench_drawdown ? [{ name: 'Benchmark', data: seriesPoints(s, 'bench_drawdown', step), color: t.ref }] : []),
          ]}
          format={pct}
          height={180}
          ariaLabel="Drawdown of the strategy and the benchmark"
        />
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Metrics" subtitle="net of costs · Sharpe / Sortino on excess of the 3m T-bill">
          <MetricTable m={res.metrics} />
        </Panel>
        <Panel title="Monthly returns" subtitle="net, %">
          <MonthlyTable monthly={res.monthly} />
        </Panel>
      </div>
    </div>
  )
}

function Risk({ res }: { res: BtResult }) {
  const t = useChartTokens()
  const r = res.rolling
  const pts = (ys: (number | null)[] | undefined) => (r?.dates ?? []).map((d, i) => ({ date: d, value: fin(ys?.[i]) ? (ys?.[i] as number) : null }))
  const s = res.series
  const step = stepFor(s.dates.length)
  const reb = (res.rebalances ?? []) as unknown as BtRebalance[]
  const full = res.metrics.full
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-6">
        <Stat label="Beta" value={num2(full.beta as number)} />
        <Stat label="Alpha (ann.)" value={pct(full.alpha as number)} />
        <Stat label="Tracking error" value={pct(full.tracking_error as number)} />
        <Stat label="Information ratio" value={num2(full.information_ratio as number)} />
        <Stat label="Gross / net exposure" value={`${num2(full.avg_gross_exposure as number)} / ${num2(full.avg_net_exposure as number)}`} />
        <Stat label="Names long / short" value={`${fin(full.avg_n_long) ? full.avg_n_long.toFixed(0) : '–'} / ${fin(full.avg_n_short) ? full.avg_n_short.toFixed(0) : '–'}`} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title={`Rolling ${r?.window_days ?? 252}-day Sharpe`}>
          <TimeSeriesChart series={[{ name: 'Sharpe', data: pts(r?.sharpe) }]} format={num2} height={200} zeroLine ariaLabel="Rolling Sharpe ratio" />
        </Panel>
        <Panel title={`Rolling ${r?.window_days ?? 252}-day volatility and beta`}>
          <TimeSeriesChart series={[{ name: 'Volatility', data: pts(r?.vol) }, ...((r?.beta ?? []).length ? [{ name: 'Beta', data: pts(r?.beta), color: t.ref }] : [])]} format={num2} height={200} ariaLabel="Rolling volatility and beta" />
        </Panel>
        <Panel title="Exposure">
          <TimeSeriesChart series={[{ name: 'Gross', data: seriesPoints(s, 'gross_exposure', step) }, { name: 'Net', data: seriesPoints(s, 'net_exposure', step), color: t.ref }]} format={num2} height={200} ariaLabel="Gross and net exposure" />
        </Panel>
        <Panel title="Turnover per rebalance" subtitle={`avg one-way ${fmtMetric(res.turnover?.avg_turnover, 'pct')} · trading ${fmtMetric(full.trading_cost_bps, 'bps')}/yr · borrow ${fmtMetric(full.borrow_cost_bps, 'bps')}/yr`}>
          <TimeSeriesChart
            series={[
              { name: 'One-way turnover', data: reb.map((x) => ({ date: x.exec_date, value: x.turnover })), step: true },
            ]}
            format={pct}
            height={200}
            ariaLabel="Turnover and cost per rebalance"
          />
        </Panel>
      </div>
    </div>
  )
}

function FactorTable({ m, title }: { m: BtAttributionModel | undefined; title: string }) {
  if (!m || !m.factors?.length) return <EmptyState title={title} message="not enough overlapping periods / factor data" />
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-x-5 gap-y-1">
        <Stat label="Alpha (ann.)" value={pct(m.alpha_ann as number)} sub={fin(m.alpha_t) ? `t ${m.alpha_t.toFixed(2)}` : undefined} />
        <Stat label="R²" value={fin(m.r2) ? m.r2.toFixed(2) : '–'} />
        <Stat label="Periods" value={String(m.n)} />
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-2xs text-muted uppercase">
            <th className="py-1 font-medium">Factor</th>
            <th className="py-1 text-right font-medium">Beta</th>
            <th className="py-1 text-right font-medium">t</th>
            <th className="py-1 text-right font-medium">Contribution / yr</th>
          </tr>
        </thead>
        <tbody>
          {m.factors.map((f) => (
            <tr key={f.factor} className="border-t border-border/60">
              <td className="py-1 text-muted" title={f.label}>
                {f.label}
              </td>
              <td className="num py-1 text-right">{f.beta.toFixed(2)}</td>
              <td className={cn('num py-1 text-right', fin(f.t_stat) && Math.abs(f.t_stat) >= 2 ? 'font-semibold text-fg' : 'text-muted')}>{fin(f.t_stat) ? f.t_stat.toFixed(1) : '–'}</td>
              <td className={cn('num py-1 text-right', f.contribution_ann >= 0 ? 'text-up' : 'text-down')}>{pct(f.contribution_ann)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function Attribution({ res }: { res: BtResult }) {
  const t = useChartTokens()
  const at = (res.attribution ?? {}) as BtAttribution
  const d = (res.deciles ?? null) as BtDeciles | null
  const regimes = res.regimes?.rows ?? []
  const qColors = (n: number) => Array.from({ length: n }, (_, i) => (i === n - 1 ? t.up : i === 0 ? t.down : t.ref))
  return (
    <div className="space-y-3">
      {d && d.n_periods > 0 && (
        <div className="grid gap-3 xl:grid-cols-2">
          <Panel title={`${d.n_quantiles === 10 ? 'Decile' : 'Quantile'} spreads`} subtitle={`annualised return per quantile of the eligible universe (equal weight, held to the next rebalance) · top − bottom ${fmtMetric(d.spread_ann, 'pct')}/yr${fin(d.spread_t) ? ` (t ${d.spread_t.toFixed(1)})` : ''}`}>
            <BarChart labels={(d.ann_return ?? []).map((_, i) => `Q${i + 1}`)} values={d.ann_return ?? []} format={pct} height={220} ariaLabel="Annualised return by signal quantile" />
          </Panel>
          <Panel title="Information coefficient" subtitle={`rank IC per rebalance · mean ${fin(d.ic_mean) ? d.ic_mean.toFixed(3) : '–'} · IR ${fin(d.ic_ir) ? d.ic_ir.toFixed(2) : '–'} · t ${fin(d.ic_t) ? d.ic_t.toFixed(1) : '–'} · hit ${fmtMetric(d.ic_hit_rate, 'pct')}`}>
            <TimeSeriesChart series={[{ name: 'Rank IC', data: (d.dates ?? []).map((x, i) => ({ date: x, value: fin(d.ic?.[i]) ? (d.ic?.[i] as number) : null })) }]} format={(v) => v.toFixed(2)} height={220} zeroLine ariaLabel="Rank information coefficient per rebalance" />
          </Panel>
          <Panel title="Cumulative quantile returns" className="xl:col-span-2">
            <LinesChart x={d.dates ?? []} series={(d.cumulative ?? []).map((c, i) => ({ name: `Q${i + 1}`, data: c, color: qColors(d.cumulative?.length ?? 0)[i], dashed: i > 0 && i < (d.cumulative?.length ?? 0) - 1 }))} format={pct} height={240} ariaLabel="Cumulative return of each signal quantile" />
          </Panel>
        </div>
      )}
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Style attribution" subtitle="market / size / value / momentum proxies from the same point-in-time panel (Newey-West t)">
          <FactorTable m={at.style} title="Style attribution" />
        </Panel>
        <Panel title="Our factors" subtitle="long-short tercile returns of the factor scores (market included)">
          <FactorTable m={at.factors} title="Factor attribution" />
        </Panel>
      </div>
      {at.exposures && Object.keys(at.exposures).length > 0 && (
        <Panel title="Average book exposures" subtitle="weighted cross-sectional z-score of the holdings (long − short)">
          <BarChart labels={Object.keys(at.exposures).map((k) => k.replace(/^f_/, '').replace('_', ' '))} values={Object.values(at.exposures)} format={num2} height={200} ariaLabel="Average factor exposures of the book" />
        </Panel>
      )}
      <Panel title="Regime breakdown" subtitle={res.regimes?.note}>
        {regimes.length ? (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-2xs text-muted uppercase">
                <th className="py-1 font-medium">Regime</th>
                <th className="py-1 font-medium">State</th>
                <th className="py-1 text-right font-medium">Months</th>
                <th className="py-1 text-right font-medium">Return / yr</th>
                <th className="py-1 text-right font-medium">Vol</th>
                <th className="py-1 text-right font-medium">Sharpe</th>
                <th className="py-1 text-right font-medium">Hit</th>
                <th className="py-1 text-right font-medium">Bench / yr</th>
              </tr>
            </thead>
            <tbody>
              {regimes.map((r) => (
                <tr key={`${r.regime_type}${r.scope}${r.state}`} className="border-t border-border/60">
                  <td className="py-1 text-muted">
                    {r.regime_type.replace('_', ' ')} · {r.scope} {r.model_based && <Badge tone="warn">model</Badge>}
                  </td>
                  <td className="py-1">{r.state.replace(/_/g, ' ')}</td>
                  <td className="num py-1 text-right">{r.n_months}</td>
                  <td className={cn('num py-1 text-right', fin(r.ann_return) && r.ann_return < 0 ? 'text-down' : 'text-up')}>{fmtMetric(r.ann_return, 'pct')}</td>
                  <td className="num py-1 text-right">{fmtMetric(r.ann_vol, 'pct')}</td>
                  <td className="num py-1 text-right">{fmtMetric(r.sharpe, 'num')}</td>
                  <td className="num py-1 text-right">{fmtMetric(r.hit_rate, 'pct')}</td>
                  <td className="num py-1 text-right text-muted">{fmtMetric(r.bench_ann_return, 'pct')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState title="No regime breakdown" message="macro regime states unavailable or the history is too short" />
        )}
      </Panel>
      {at.note && <Note>{at.note}</Note>}
    </div>
  )
}

function Holdings({ run }: { run: BtRun }) {
  const res = run.result as BtResult
  const dates = res.holdings_dates ?? []
  const [date, setDate] = useState<string | null>(null)
  const [offset, setOffset] = useState(0)
  const h = useBtHoldings(run.run.run_id, date, dates.length > 0)
  const tr = useBtTrades(run.run.run_id, offset, null)
  const roundTrip = tr.data?.kind === 'round_trip'
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      {dates.length > 0 && (
        <Panel
          title="Holdings"
          subtitle={`target book at the rebalance (${h.data?.total ?? 0} names)`}
          flush
          actions={<Select label="Rebalance date" value={date ?? dates[dates.length - 1]} onChange={(v) => setDate(v)} options={[...dates].reverse().map((d) => ({ value: d, label: d }))} />}
        >
          {h.isLoading ? (
            <SkeletonRows rows={8} className="p-3" />
          ) : (
            <div className="max-h-[520px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-surface">
                  <tr className="text-left text-2xs text-muted uppercase">
                    <th className="px-3 py-1 font-medium">Name</th>
                    <th className="py-1 font-medium">Group</th>
                    <th className="py-1 text-right font-medium">Weight</th>
                    <th className="py-1 text-right font-medium">Signal</th>
                    <th className="px-3 py-1 text-right font-medium">Rank pct</th>
                  </tr>
                </thead>
                <tbody>
                  {(h.data?.rows ?? []).map((r) => (
                    <tr key={r.entity_id} className="border-t border-border/50">
                      <td className="max-w-[220px] truncate px-3 py-0.5" title={r.name ?? r.entity_id}>
                        <span className="font-medium">{r.label ?? r.entity_id}</span> <span className="text-xs text-muted">{r.name}</span>
                      </td>
                      <td className="py-0.5 text-xs text-muted">{r.group ?? r.sector_id ?? ''}</td>
                      <td className={cn('num py-0.5 text-right', r.weight < 0 ? 'text-down' : 'text-up')}>{(r.weight * 100).toFixed(2)}%</td>
                      <td className="num py-0.5 text-right">{fin(r.signal) ? r.signal.toFixed(3) : '–'}</td>
                      <td className="num px-3 py-0.5 text-right">{fin(r.rank_pct) ? r.rank_pct.toFixed(2) : '–'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      )}
      <Panel
        title={roundTrip ? 'Trades (round trips)' : 'Trades'}
        subtitle={`${tr.data?.total ?? 0} rows${roundTrip ? '' : ' · one row per name traded at each rebalance'}`}
        flush
        className={dates.length ? '' : 'xl:col-span-2'}
        actions={
          <div className="flex items-center gap-1">
            <Button size="xs" variant="ghost" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 200))}>
              ‹ Newer
            </Button>
            <Button size="xs" variant="ghost" disabled={!tr.data || offset + 200 >= tr.data.total} onClick={() => setOffset(offset + 200)}>
              Older ›
            </Button>
          </div>
        }
      >
        <div className="max-h-[520px] overflow-y-auto">
          <table className="w-full text-sm">
            <thead className="sticky top-0 bg-surface">
              <tr className="text-left text-2xs text-muted uppercase">
                {roundTrip ? (
                  <>
                    <th className="px-3 py-1 font-medium">Name</th>
                    <th className="py-1 font-medium">Entry</th>
                    <th className="py-1 font-medium">Exit</th>
                    <th className="py-1 text-right font-medium">Dir</th>
                    <th className="py-1 text-right font-medium">Return</th>
                    <th className="px-3 py-1 font-medium">Exit reason</th>
                  </>
                ) : (
                  <>
                    <th className="px-3 py-1 font-medium">Date</th>
                    <th className="py-1 font-medium">Name</th>
                    <th className="py-1 text-right font-medium">Δ weight</th>
                    <th className="py-1 text-right font-medium">After</th>
                    <th className="px-3 py-1 text-right font-medium">Cost</th>
                  </>
                )}
              </tr>
            </thead>
            <tbody>
              {(tr.data?.rows ?? []).map((r, i) => {
                const x = r as Record<string, unknown>
                return roundTrip ? (
                  <tr key={i} className="border-t border-border/50">
                    <td className="px-3 py-0.5 font-medium">{String(x.label ?? x.entity_id)}</td>
                    <td className="num py-0.5 text-xs">{String(x.entry_date)}</td>
                    <td className="num py-0.5 text-xs">{String(x.exit_date)}</td>
                    <td className="py-0.5 text-right text-xs">{Number(x.direction) > 0 ? 'long' : 'short'}</td>
                    <td className={cn('num py-0.5 text-right', Number(x.return) >= 0 ? 'text-up' : 'text-down')}>{pct(Number(x.return))}</td>
                    <td className="px-3 py-0.5 text-xs text-muted">{String(x.exit_reason)}</td>
                  </tr>
                ) : (
                  <tr key={i} className="border-t border-border/50">
                    <td className="num px-3 py-0.5 text-xs">{String(x.date)}</td>
                    <td className="py-0.5 font-medium">{String(x.label ?? x.entity_id)}</td>
                    <td className={cn('num py-0.5 text-right', Number(x.delta_weight) >= 0 ? 'text-up' : 'text-down')}>{(Number(x.delta_weight) * 100).toFixed(2)}%</td>
                    <td className="num py-0.5 text-right">{(Number(x.weight_after) * 100).toFixed(2)}%</td>
                    <td className="num px-3 py-0.5 text-right text-xs text-muted">{fin(x.cost_bps) ? `${(x.cost_bps as number).toFixed(1)} bp` : '–'}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  )
}

function Robustness({ res, nTrialsStrategy, oosViews }: { res: BtResult; nTrialsStrategy: number | null; oosViews: number | null }) {
  const t = useChartTokens()
  const rb = (res.robustness ?? {}) as BtRobustnessView
  const v = overfitVerdict(rb.dsr?.dsr, rb.sweep?.pbo?.pbo, rb.permutation?.p_value)
  const sw = rb.sweep
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={v.tone}>{v.text}</Badge>
        <span className="text-xs text-muted">
          {rb.n_trials ?? 1} configuration(s) tested{nTrialsStrategy !== null ? ` · strategy counter ${nTrialsStrategy}` : ''}
          {oosViews !== null ? ` · holdout viewed ${oosViews}×` : ''}
        </span>
      </div>
      <div className="grid gap-3 lg:grid-cols-3">
        <Panel title="Deflated Sharpe ratio" subtitle="Bailey & López de Prado (2014), in-sample window">
          {rb.dsr ? (
            <div className="grid grid-cols-2 gap-3">
              <Stat label="DSR" value={rb.dsr.dsr.toFixed(3)} sub="P(true SR > SR₀)" />
              <Stat label="PSR (SR > 0)" value={rb.dsr.psr.toFixed(3)} />
              <Stat label="Sharpe (ann.)" value={rb.dsr.sharpe_ann.toFixed(2)} sub={`${rb.dsr.n_obs} days`} />
              <Stat label="SR₀ (ann.)" value={rb.dsr.sr0_ann.toFixed(2)} sub={`expected max of ${rb.dsr.n_trials} trials`} />
              <Stat label="Skew / kurtosis" value={`${fin(rb.dsr.skew) ? rb.dsr.skew.toFixed(2) : '–'} / ${fin(rb.dsr.kurtosis) ? rb.dsr.kurtosis.toFixed(1) : '–'}`} />
              <Stat label="Trial SR variance" value={rb.dsr.sr_var.toExponential(1)} sub={rb.dsr.sr_var_source} />
            </div>
          ) : (
            <EmptyState title="Not computed" />
          )}
        </Panel>
        <Panel title="Label permutation" subtitle={rb.permutation?.note ?? 'signal shuffled across names within each date'}>
          {rb.permutation ? (
            <>
              <div className="mb-1 flex flex-wrap gap-x-4">
                <Stat label="Permuted mean Sharpe" value={rb.permutation.mean.toFixed(2)} sub={`sd ${fin(rb.permutation.std) ? rb.permutation.std.toFixed(2) : '–'} · n ${rb.permutation.n}`} />
                <Stat label="Actual (gross)" value={fin(rb.permutation.actual_gross_sharpe) ? rb.permutation.actual_gross_sharpe.toFixed(2) : '–'} />
                <Stat label="p-value" value={rb.permutation.p_value.toFixed(3)} />
              </div>
              <Histogram values={rb.permutation.sharpes} marker={rb.permutation.actual_gross_sharpe} markerLabel="actual" format={num2} height={150} ariaLabel="Distribution of permuted Sharpe ratios versus the actual Sharpe" zeroLine />
            </>
          ) : (
            <EmptyState title="Not run" message="set permutations > 0" />
          )}
        </Panel>
        <Panel title="Stale-signal check" subtitle={rb.stale?.note}>
          {rb.stale ? (
            <div className="space-y-2">
              <div className="flex flex-wrap gap-x-4">
                <Stat label="Actual (gross)" value={fin(rb.stale.actual_gross_sharpe) ? rb.stale.actual_gross_sharpe.toFixed(2) : '–'} />
                <Stat label={`Lagged ${rb.stale.lag}`} value={fin(rb.stale.sharpe) ? rb.stale.sharpe.toFixed(2) : '–'} />
                <Stat label="Δ Sharpe" value={fin(rb.stale.delta) ? rb.stale.delta.toFixed(2) : '–'} />
              </div>
              <Badge tone={rb.stale.suspicious ? 'down' : 'up'}>{rb.stale.suspicious ? 'staler signal does better: check timing' : 'no timing red flag'}</Badge>
            </div>
          ) : (
            <EmptyState title="Not run" />
          )}
        </Panel>
      </div>
      {sw && sw.grid?.length ? (
        <div className="grid gap-3 xl:grid-cols-2">
          <Panel title="Parameter sweep" subtitle={sw.note} flush>
            <div className="max-h-[360px] overflow-y-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-surface">
                  <tr className="text-left text-2xs text-muted uppercase">
                    <th className="px-3 py-1 font-medium">Parameters</th>
                    <th className="py-1 text-right font-medium">IS Sharpe</th>
                    <th className="py-1 text-right font-medium">IS CAGR</th>
                    <th className="px-3 py-1 text-right font-medium">OOS Sharpe</th>
                  </tr>
                </thead>
                <tbody>
                  {sw.grid.map((g, i) => {
                    const best = JSON.stringify(g.params) === JSON.stringify(sw.best_in_sample)
                    return (
                      <tr key={i} className={cn('border-t border-border/50', best && 'bg-accent-soft')}>
                        <td className="px-3 py-0.5 font-mono text-xs">{Object.entries(g.params).map(([k, x]) => `${k.split('.').pop()}=${String(x)}`).join(' ')}{g.error ? <span className="ml-1 text-down"> {g.error}</span> : null}</td>
                        <td className="num py-0.5 text-right">{fmtMetric(g.is_sharpe, 'num')}</td>
                        <td className="num py-0.5 text-right">{fmtMetric(g.is_cagr, 'pct')}</td>
                        <td className="num px-3 py-0.5 text-right text-muted">{fmtMetric(g.oos_sharpe, 'num')}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </Panel>
          <Panel title="Probability of backtest overfitting (CSCV)" subtitle="share of in/out-of-sample splits where the in-sample winner ranks below the median out of sample">
            {fin(sw.pbo?.pbo) ? (
              <>
                <div className="mb-1 flex flex-wrap gap-x-4">
                  <Stat label="PBO" value={(sw.pbo.pbo * 100).toFixed(0) + '%'} sub={`${sw.pbo.n_combinations} splits · ${sw.pbo.n_strategies} configs`} />
                  <Stat label="P(OOS loss)" value={fmtMetric(sw.pbo.prob_oos_loss, 'pct')} />
                  <Stat label="IS→OOS slope" value={fmtMetric(sw.pbo.degradation_slope, 'num')} />
                </div>
                <Histogram values={sw.pbo.logits ?? []} format={num2} height={150} ariaLabel="Distribution of the relative-rank logits of the in-sample winner" zeroLine />
              </>
            ) : (
              <EmptyState title="Not computed" message={sw.pbo?.note ?? 'needs 2+ configurations and enough in-sample history'} />
            )}
          </Panel>
          {sw.walk_forward && (
            <Panel title="Walk-forward" subtitle={`configuration re-selected on each expanding in-sample window · WF Sharpe ${fmtMetric(sw.walk_forward.metrics.sharpe, 'num')}`}>
              <TimeSeriesChart series={[{ name: 'Walk-forward NAV', data: sw.walk_forward.dates.map((d, i) => ({ date: d, value: sw.walk_forward?.nav[i] ?? null })), color: t.series[1] }]} format={navFmt} height={200} ariaLabel="Walk-forward NAV" />
              <div className="mt-2 space-y-0.5 text-xs text-muted">
                {sw.walk_forward.folds.map((f) => (
                  <div key={f.fold} className="font-mono">
                    fold {f.fold} {f.test_start_date}→{f.test_end_date}: {Object.entries(f.params ?? {}).map(([k, x]) => `${k.split('.').pop()}=${String(x)}`).join(' ')} · train {fmtMetric(f.train_sharpe, 'num')} · test {fmtMetric(f.test_sharpe, 'num')}
                  </div>
                ))}
              </div>
            </Panel>
          )}
          {sw.holdout && (
            <Panel title="Holdout of the in-sample choice" subtitle="the configuration picked on the in-sample window, out of sample">
              <div className="flex flex-wrap gap-x-5 gap-y-1">
                <Stat label="Chosen" value={<span className="font-mono text-sm">{Object.entries(sw.holdout.params).map(([k, x]) => `${k.split('.').pop()}=${String(x)}`).join(' ')}</span>} />
                <Stat label="IS Sharpe" value={fmtMetric(sw.holdout.is_sharpe, 'num')} />
                <Stat label="OOS Sharpe" value={fmtMetric(sw.holdout.metrics.sharpe, 'num')} />
                <Stat label="OOS CAGR" value={fmtMetric(sw.holdout.metrics.cagr, 'pct')} />
              </div>
            </Panel>
          )}
        </div>
      ) : (
        <Note>Add a parameter sweep in the builder (Robustness) to get walk-forward selection and the probability of backtest overfitting. Every configuration counts toward the multiple-testing counter.</Note>
      )}
    </div>
  )
}

function Events({ res }: { res: BtResult }) {
  const e = res.events as unknown as BtEventBlock | null
  if (!e) return null
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 xl:grid-cols-8">
        <Stat label="Events" value={String(e.n_events)} />
        <Stat label="Trades" value={String(e.n_trades)} />
        <Stat label="Hit rate" value={fmtMetric(e.hit_rate, 'pct')} />
        <Stat label="Avg return" value={fmtMetric(e.avg_return, 'pct')} sub={`median ${fmtMetric(e.median_return, 'pct')}`} />
        <Stat label="Avg win / loss" value={`${fmtMetric(e.avg_win, 'pct')} / ${fmtMetric(e.avg_loss, 'pct')}`} />
        <Stat label="Profit factor" value={fmtMetric(e.profit_factor, 'num')} />
        <Stat label="Avg holding" value={fin(e.avg_holding_days) ? `${e.avg_holding_days.toFixed(0)} d` : '–'} />
        <Stat label="Exits" value={<span className="text-sm">{Object.entries(e.exit_reasons).map(([k, n]) => `${k} ${n}`).join(' · ') || '–'}</span>} />
      </div>
      <Panel title="Average path after entry" subtitle={`${e.path?.n ?? 0} events with full history · direction-adjusted, with a 95% band`}>
        {e.path && e.path.n ? <PathChart days={e.path.days} mean={e.path.mean} lo={e.path.lo} hi={e.path.hi} excess={e.path.excess_mean} format={pct} height={260} ariaLabel="Average cumulative return after event entries" /> : <EmptyState title="No complete event paths" />}
      </Panel>
    </div>
  )
}

export function RunResults({ run, nTrialsStrategy = null, oosViews = null }: { run: BtRun; nTrialsStrategy?: number | null; oosViews?: number | null }) {
  const res = run.result as BtResult
  const tab = useBacktest((s) => s.resultTab)
  const setTab = useBacktest((s) => s.setResultTab)
  const exp = useMutation({
    mutationFn: () => backtestApi.exportPython(run.run.run_id),
    onSuccess: (src) => {
      downloadText(`backtest_${run.run.run_id}.py`, src, 'text/x-python')
      toast({ tone: 'success', title: 'Script exported', message: 'standalone: rebuilds the panel from the lake and re-simulates' })
    },
    onError: (e) => toast({ tone: 'error', title: 'Export failed', message: e instanceof Error ? e.message : String(e) }),
  })
  const isEvent = res.summary.mode === 'event'
  const active: BtResultTab = isEvent && tab === 'attribution' ? 'events' : tab
  const warn = res.warnings ?? []
  return (
    <div className="space-y-3">
      <Panel
        title={res.summary.name}
        subtitle={`${res.summary.universe} · ${res.summary.signal} · ${res.summary.start} → ${res.summary.end}`}
        actions={
          <>
            <Badge tone="neutral" title="spec hash · snapshot">{run.reproducibility?.spec_hash ? String(run.reproducibility.spec_hash).slice(0, 8) : ''}</Badge>
            <Badge tone="neutral">snapshot {String(run.reproducibility?.snapshot_id ?? run.run.snapshot_id ?? '–')}</Badge>
            <Button size="xs" variant="outline" disabled={exp.isPending} onClick={() => exp.mutate()} title="Export a standalone Python script that reproduces this run">
              <Download /> Python
            </Button>
            <CreateThesisLink
              size="xs"
              link={{ subjectType: 'macro_theme', origin: 'backtest', ref: run.run.run_id, backtestRunId: run.run.run_id, backtestStrategyId: run.run.strategy_id ?? undefined, title: `Thesis from backtest ${res.summary.name}` }}
              title="Create a thesis linked to this backtest run (pick the subject in the editor)"
            />
          </>
        }
      >
        <div className="space-y-1.5">
          {warn.some((w) => w.includes('EXPERIMENTAL')) && (
            <ExperimentalNote>Conformal triggers / band stops were not promoted after validation: {warn.filter((w) => w.includes('EXPERIMENTAL')).map((w) => w.split(':')[0]).join(' · ')}.</ExperimentalNote>
          )}
          {(res.summary.universe ?? '').startsWith('Assets') && <Note>{res.caveats.survivorship}</Note>}
          {(res.caveats.model_based ?? []).length > 0 && <Note>Model-based inputs: {(res.caveats.model_based ?? []).join(' · ')}</Note>}
          {warn.length > 0 && (
            <details className="text-xs text-muted">
              <summary className="cursor-pointer">{warn.length} warning(s)</summary>
              <ul className="mt-1 list-disc space-y-0.5 pl-5">
                {warn.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      </Panel>
      <Tabs value={active} onValueChange={(v) => setTab(v as BtResultTab)}>
        <TabsList>
          <TabsTrigger value="performance">Performance</TabsTrigger>
          <TabsTrigger value="risk">Risk</TabsTrigger>
          {isEvent ? <TabsTrigger value="events">Events</TabsTrigger> : <TabsTrigger value="attribution">Attribution</TabsTrigger>}
          <TabsTrigger value="trades">Trades & holdings</TabsTrigger>
          <TabsTrigger value="robustness">Robustness</TabsTrigger>
        </TabsList>
        <TabsContent value="performance">
          <Performance res={res} />
        </TabsContent>
        <TabsContent value="risk">
          <Risk res={res} />
        </TabsContent>
        <TabsContent value="attribution">
          <Attribution res={res} />
        </TabsContent>
        <TabsContent value="events">
          <Events res={res} />
        </TabsContent>
        <TabsContent value="trades">
          <Holdings run={run} />
        </TabsContent>
        <TabsContent value="robustness">
          <Robustness res={res} nTrialsStrategy={nTrialsStrategy} oosViews={oosViews} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
