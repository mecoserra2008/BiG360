import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import type { ModelKind, ModelLabPayload, ModelRun } from '@/api/types'
import { StatusBadge } from '@/components/ui/status'
import { useModelLab } from '@/api/hooks'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { FACTOR_LABEL } from '@/lib/factors'
import { fmtDate, fmtDateTime, fmtDuration, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { foldTestMonths } from './folds'

export default function ModelLab({ embedded = false }: { embedded?: boolean }) {
  const q = useModelLab()
  return (
    <>
      {embedded ? (
        <p className="mb-3 text-sm text-muted">{SUBTITLE}.</p>
      ) : (
        <PageHeader title="Model Lab" subtitle={SUBTITLE} />
      )}
      <QueryView query={q} skeleton={<div className="grid grid-cols-2 gap-3"><Skeleton className="h-72" /><Skeleton className="h-72" /><Skeleton className="col-span-2 h-72" /></div>}>
        {(d) => <LabBody d={d} />}
      </QueryView>
    </>
  )
}

const SUBTITLE = 'Weekly ML monitor: walk-forward evaluation of the production XGBoost model and the demoted comparison models (FT-Transformer, ensemble)'

const MODEL_LABEL: Record<string, string> = { xgboost_rank: 'XGBoost', ft_transformer: 'FT-Transformer', cs_attention: 'CS-Attention', ensemble: 'Ensemble' }

/** Red "demoted" badge for a run that failed the label-permutation leakage gate. */
function GateBadge({ gate, note }: { gate?: 'passed' | 'failed' | null; note?: string | null }) {
  if (gate === 'failed')
    return (
      <Badge tone="down" title={note ?? 'Failed the label-permutation leakage test: demoted, comparison only'} data-testid="gate-failed">
        DEMOTED · failed leakage test
      </Badge>
    )
  if (gate === 'passed') return <Badge tone="up" title={note ?? 'Passed the label-permutation leakage test'}>leakage test passed</Badge>
  return null
}

/** Trailing mean over the last n points, ignoring gaps (NaN); null until 6 finite values exist. */
function rolling(xs: number[], n: number): (number | null)[] {
  return xs.map((_, i) => {
    const w = xs.slice(Math.max(0, i - n + 1), i + 1).filter((v) => Number.isFinite(v))
    return w.length >= Math.min(6, n) ? w.reduce((a, b) => a + b, 0) / w.length : null
  })
}

function LabBody({ d }: { d: ModelLabPayload }) {
  const t = useChartTokens()
  const [model, setModel] = useState<ModelKind>('xgboost')
  const trRun = d.transformerRun
  const trFailed = (trRun?.leakageGate ?? 'failed') === 'failed'
  const trName = `${MODEL_LABEL[trRun?.modelType ?? ''] ?? 'Transformer'}${trRun?.horizon ? ` ${trRun.horizon}` : ''}${trFailed ? ' (demoted)' : ''}`
  const xgbName = `XGBoost${d.xgboostRun?.horizon ? ` ${d.xgboostRun.horizon}` : ''}`
  const testMonths = foldTestMonths(d.folds) ?? 24

  const runCols = useMemo<ColumnDef<ModelRun>[]>(
    () => [
      { id: 'id', header: 'Run', size: 82, accessorFn: (r) => r.id, cell: (c) => <span className="num">{c.getValue<string>()}</span> },
      { id: 'model', header: 'Model', size: 110, accessorFn: (r) => r.modelType ?? r.model, cell: (c) => <span>{MODEL_LABEL[c.row.original.modelType ?? ''] ?? (c.row.original.model === 'xgboost' ? 'XGBoost' : 'Transformer')}</span> },
      { id: 'gate', header: 'Leakage gate', size: 200, accessorFn: (r) => r.leakageGate ?? '', cell: (c) => <GateBadge gate={c.row.original.leakageGate} note={c.row.original.gateNote} /> },
      { id: 'version', header: 'Version', size: 190, accessorFn: (r) => r.version, cell: (c) => <span className="num text-muted">{c.getValue<string>()}{c.row.original.promoted && <Badge tone="accent" className="ml-1.5">prod</Badge>}</span> },
      { id: 'status', header: 'Status', size: 92, accessorFn: (r) => r.status, cell: (c) => <StatusBadge status={c.row.original.status} /> },
      { id: 'started', header: 'Started', size: 150, accessorFn: (r) => r.startedAt, cell: (c) => <span className="num text-muted">{fmtDateTime(c.getValue<string>())}</span> },
      { id: 'dur', header: 'Duration', size: 92, accessorFn: (r) => r.durationSec, cell: (c) => <span className="num">{fmtDuration(c.getValue<number>())}</span>, meta: { align: 'right' } },
      { id: 'test', header: 'Test window', size: 216, accessorFn: (r) => r.testStart, cell: (c) => <span className="num text-muted">{fmtDate(c.row.original.testStart)} → {fmtDate(c.row.original.testEnd)}</span> },
      { id: 'ic', header: 'IC', size: 62, accessorFn: (r) => r.ic, cell: (c) => <span className="num">{fmtNum(c.getValue<number>(), 3)}</span>, meta: { align: 'right' } },
      { id: 'rankIc', header: 'Rank IC', size: 86, accessorFn: (r) => r.rankIc, cell: (c) => <span className="num font-medium">{fmtNum(c.getValue<number>(), 3)}</span>, meta: { align: 'right' } },
      { id: 'icir', header: 'ICIR', size: 58, accessorFn: (r) => r.icir, cell: (c) => <span className="num">{fmtNum(c.getValue<number>(), 2)}</span>, meta: { align: 'right' } },
      { id: 'hit', header: 'Hit rate', size: 86, accessorFn: (r) => r.hitRate, cell: (c) => <span className="num">{fmtPct(c.getValue<number>(), 1)}</span>, meta: { align: 'right' } },
      { id: 'spread', header: 'D10−D1 ann.', size: 112, accessorFn: (r) => r.decileSpreadAnn, cell: (c) => <span className="num">{fmtPct(c.getValue<number>(), 1)}</span>, meta: { align: 'right' } },
      { id: 'nf', header: 'Features', size: 92, accessorFn: (r) => r.nFeatures, cell: (c) => <span className="num text-muted">{fmtInt(c.getValue<number>())}</span>, meta: { align: 'right' } },
    ],
    [],
  )

  const deciles = useMemo<EChartsOption>(() => ({
    grid: { left: 8, right: 12, top: 26, bottom: 8, containLabel: true },
    legend: { top: 0, right: 0 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v) => fmtPct(Number(v), 2, true) },
    xAxis: { type: 'category', data: d.deciles.map((x) => `D${x.decile}`) },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => fmtPct(v, 0) } },
    series: [
      { name: xgbName, type: 'bar', data: d.deciles.map((x) => x.xgboost), itemStyle: { color: t.series[0], borderRadius: 2 }, barGap: '10%' },
      { name: trName, type: 'bar', data: d.deciles.map((x) => x.transformer), itemStyle: { color: t.series[1], borderRadius: 2 } },
    ],
  }), [d.deciles, t, trName, xgbName])

  const fi = useMemo<EChartsOption>(() => {
    const items = [...d.featureImportance].sort((a, b) => a.importance - b.importance)
    return {
      grid: { left: 8, right: 36, top: 4, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps: unknown) => { const i = (ps as { dataIndex: number }[])[0].dataIndex; const f = items[i]; return `<b>${f.feature}</b><br/>${FACTOR_LABEL[f.family]} · gain ${fmtPct(f.importance, 1)}` } },
      xAxis: { type: 'value', axisLabel: { formatter: (v: number) => fmtPct(v, 0) } },
      yAxis: { type: 'category', data: items.map((f) => f.feature), axisLine: { show: false }, axisLabel: { fontSize: 10 } },
      series: [{ type: 'bar', data: items.map((f) => f.importance), barMaxWidth: 10, itemStyle: { color: alpha(t.accent, 0.85), borderRadius: [0, 3, 3, 0] }, label: { show: true, position: 'right', fontSize: 9, color: t.muted, formatter: (p: unknown) => { const it = items[(p as { dataIndex: number }).dataIndex]; return it.familyLabel ? it.familyLabel.replace(/_/g, ' ') : FACTOR_LABEL[it.family] } } }],
    }
  }, [d.featureImportance, t])

  const icKey = model === 'xgboost' ? 'xgboostRankIc' : 'transformerRankIc'
  // series of both models are merged by date → skip dates where this model has no IC
  const icVals = d.icSeries.map((p) => p[icKey]).filter((v) => Number.isFinite(v))
  const roll = rolling(d.icSeries.map((p) => p[icKey]), 12)
  const meanIc = icVals.reduce((a, b) => a + b, 0) / icVals.length
  const sdIc = Math.sqrt(icVals.reduce((a, b) => a + (b - meanIc) ** 2, 0) / (icVals.length - 1))
  const tStat = (meanIc / sdIc) * Math.sqrt(icVals.length)
  const minYear = 2008
  const maxYear = 2027.5
  const xPct = (iso: string) => ((new Date(iso).getUTCFullYear() + new Date(iso).getUTCMonth() / 12 - minYear) / (maxYear - minYear)) * 100

  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel
        title="Rank IC over time"
        subtitle="Spearman IC of out-of-sample predictions vs realised excess returns per rebalance date · 12-period rolling mean"
        className="col-span-12 xl:col-span-7"
        actions={<Segmented label="Model" value={model} onChange={setModel} options={[{ value: 'xgboost', label: xgbName }, { value: 'transformer', label: trFailed ? 'Transformer (demoted)' : 'Transformer' }]} />}
      >
        {model === 'transformer' && trFailed && (
          <div className="mb-2 flex flex-wrap items-center gap-2 rounded-md border border-down/50 bg-down/10 px-2 py-1.5 text-xs" role="note" data-testid="transformer-warning">
            <GateBadge gate="failed" note={trRun?.gateNote} />
            <span className="text-fg">
              Not evidence of skill: the FT-Transformer failed the label-permutation leakage test (it still ranked well on shuffled labels), so it and the ensemble that blends it are demoted to comparison only. Production is XGBoost.
            </span>
          </div>
        )}
        <div className="mb-2 flex gap-8">
          <Stat label="Mean rank IC" value={fmtNum(meanIc, 3)} />
          <Stat label="IC σ" value={fmtNum(sdIc, 3)} />
          <Stat label="IC / σ" value={fmtNum(meanIc / sdIc, 2)} />
          <Stat label="t-stat" value={fmtNum(tStat, 2)} />
          <Stat label="% periods > 0" value={fmtPct(icVals.filter((v) => v > 0).length / icVals.length, 0)} />
        </div>
        <TimeSeriesChart
          height={240}
          ariaLabel="Rank IC over time"
          format={(v) => fmtNum(v, 3)}
          zeroLine
          series={[
            { name: "Rank IC", data: d.icSeries.map((p) => ({ date: p.date, value: p[icKey] })), color: alpha(t.ref, 0.9), width: 1 },
            { name: "12-period rolling", data: d.icSeries.map((p, i) => ({ date: p.date, value: roll[i] })), color: t.accent, width: 2 },
          ]}
        />
      </Panel>
      <Panel
        title="Decile returns"
        subtitle="Mean excess return per label horizon by predicted decile, out-of-sample test folds (not annualised)"
        className="col-span-12 xl:col-span-5"
        actions={trFailed ? <GateBadge gate="failed" note={trRun?.gateNote} /> : undefined}
      >
        <EChart option={deciles} height={300} ariaLabel="Mean excess return per label horizon by prediction decile" />
      </Panel>
      <Panel
        title="Long-short backtest"
        subtitle="Cumulative D10 − D1 spread, compounded over non-overlapping horizon-length windows, gross of trading costs"
        className="col-span-12 xl:col-span-7"
        actions={trFailed ? <GateBadge gate="failed" note={trRun?.gateNote} /> : undefined}
      >
        <TimeSeriesChart
          height={260}
          ariaLabel="Cumulative decile spread backtest"
          format={(v) => fmtPct(v, 1, true)}
          axisFormat={(v) => fmtPct(v, 0)}
          zeroLine
          series={[
            { name: xgbName, data: d.cumulativeSpread.map((p) => ({ date: p.date, value: p.xgboost })), color: t.series[0] },
            { name: trName, data: d.cumulativeSpread.map((p) => ({ date: p.date, value: p.transformer })), color: t.series[1] },
            { name: 'Equal-weight benchmark', data: d.cumulativeSpread.map((p) => ({ date: p.date, value: p.benchmark })), color: t.ref, dashed: true },
          ]}
        />
      </Panel>
      <Panel title="Feature importance" subtitle="XGBoost total gain, top 20 (label = factor family)" className="col-span-12 row-span-2 xl:col-span-5" bodyClassName="p-1.5">
        <EChart option={fi} height={560} ariaLabel="Feature importance" />
      </Panel>
      <Panel title="Walk-forward folds" subtitle={`${d.xgboostRun ? xgbName : 'Transformer'} · expanding train window (grey) → ${testMonths}-month test folds (orange; the last may be partial), purged & embargoed`} className="col-span-12 xl:col-span-7" flush>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 w-12 border-b border-border px-3 text-left font-semibold">Fold</th>
              <th scope="col" className="border-b border-border px-2 text-left font-semibold">
                <span className="flex justify-between"><span>2008</span><span>2018</span><span>2027</span></span>
              </th>
              <th scope="col" className="w-20 border-b border-border px-2 text-right font-semibold">Rank IC</th>
              <th scope="col" className="w-20 border-b border-border px-3 text-right font-semibold">Sharpe</th>
            </tr>
          </thead>
          <tbody>
            {d.folds.map((f) => (
              <tr key={f.fold} className="hover:bg-surface-2">
                <td className="num h-[24px] border-b border-border px-3 text-muted">{f.fold}</td>
                <td className="border-b border-border px-2">
                  <div className="relative h-2.5" role="img" aria-label={`Train ${f.trainStart} to ${f.trainEnd}, test ${f.testStart} to ${f.testEnd}`} title={`Train ${fmtDate(f.trainStart)} → ${fmtDate(f.trainEnd)} · Test ${fmtDate(f.testStart)} → ${fmtDate(f.testEnd)}`}>
                    <span className="absolute inset-y-0 rounded-sm bg-surface-3" style={{ left: `${xPct(f.trainStart)}%`, width: `${xPct(f.trainEnd) - xPct(f.trainStart)}%` }} />
                    <span className="absolute inset-y-0 rounded-sm bg-accent" style={{ left: `${xPct(f.testStart)}%`, width: `${xPct(f.testEnd) - xPct(f.testStart)}%` }} />
                  </div>
                </td>
                <td className={`num border-b border-border px-2 text-right ${f.rankIc < 0 ? 'text-down' : ''}`}>{fmtNum(f.rankIc, 3)}</td>
                <td className="num border-b border-border px-3 text-right">{fmtNum(f.sharpe, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
      <Panel title="Training runs" subtitle="Most recent first · 'prod' = promoted to scoring" className="col-span-12" flush>
        <DataTable data={d.runs} columns={runCols} ariaLabel="Model training runs" initialSorting={[{ id: 'started', desc: true }]} getRowId={(r) => r.id} />
      </Panel>
    </div>
  )
}
