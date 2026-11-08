import { Brain, Cpu } from 'lucide-react'
import { useMemo } from 'react'
import type { CompanyModel, ModelPrediction } from '@/api/types'
import { useCompanyModel } from '@/api/hooks'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { FACTOR_LABEL } from '@/lib/factors'
import { fmtDate, fmtDateTime, fmtNum, fmtPct } from '@/lib/format'

export default function ModelTab({ id }: { id: string }) {
  const q = useCompanyModel(id)
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
      {(d) => <ModelBody d={d} />}
    </QueryView>
  )
}

const MODEL_META = {
  xgboost: { label: 'XGBoost (gradient-boosted trees)', Icon: Cpu },
  transformer: { label: 'Multi-factor Transformer', Icon: Brain },
}

const TYPE_LABEL: Record<string, string> = {
  xgboost_rank: 'XGBoost ranker (gradient-boosted trees)',
  ensemble: 'Ensemble (XGBoost + Transformer blend)',
  ft_transformer: 'FT-Transformer',
  cs_attention: 'Cross-sectional attention Transformer',
}
const modelLabel = (p: ModelPrediction) => (p.modelType && TYPE_LABEL[p.modelType]) || MODEL_META[p.model].label
/** model types that failed (or never passed) the label-permutation leakage test: comparison only (guide 05c / 10) */
const DEMOTED = new Set(['ft_transformer', 'cs_attention', 'ensemble'])
const isDemoted = (p: ModelPrediction) => DEMOTED.has(p.modelType ?? (p.model === 'transformer' ? 'ft_transformer' : ''))

function PredictionCard({ p }: { p: ModelPrediction }) {
  const m = { ...MODEL_META[p.model], label: modelLabel(p) }
  const lo = -0.1
  const hi = 0.1
  const pos = (v: number) => `${((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo)) * 100}%`
  const demoted = isDemoted(p)
  const hasInterval = Number.isFinite(p.intervalLow) && Number.isFinite(p.intervalHigh) && p.intervalHigh > p.intervalLow
  return (
    <Panel
      title={<span className="inline-flex items-center gap-1.5"><m.Icon className="size-4 text-accent" aria-hidden /> {m.label}</span>}
      subtitle={`${p.modelVersion} · trained ${fmtDateTime(p.trainedAt)}`}
      actions={
        demoted ? (
          <Badge tone="down" title="Failed (or never passed) the label-permutation leakage test: demoted, comparison only - its numbers are not evidence of skill" data-testid="model-demoted">
            DEMOTED · failed leakage test
          </Badge>
        ) : p.modelType === 'xgboost_rank' ? (
          <Badge tone="up" title="Production ranking model (passed the label-permutation leakage test)">production</Badge>
        ) : undefined
      }
      className={`col-span-12 lg:col-span-6 xl:col-span-4${demoted ? ' opacity-80' : ''}`}
    >
      <div className="grid grid-cols-3 gap-3">
        <Stat label="Predicted rank" value={`p${Math.round(p.predictedRankPct)}`} sub="cross-sectional percentile" />
        <Stat label={`E[excess] ${p.horizonDays}d`} value={<Delta value={p.expectedExcessReturn} format={(v) => fmtPct(v, 1, true)} />} sub="decile mean (out-of-sample)" />
        <Stat label="Confidence" value={demoted ? <span className="text-faint">not credible</span> : fmtPct(p.confidence, 0)} sub={demoted ? `heuristic ${fmtPct(p.confidence, 0)}, model failed its gate` : 'heuristic, not a probability'} />
      </div>
      <div className="mt-4">
        <div className="label-caps mb-1">80% interval of excess return{hasInterval ? '' : ' · not available (zero width in this snapshot)'}</div>
        <div className="relative h-6" role="img" aria-label={`Interval ${fmtPct(p.intervalLow, 1)} to ${fmtPct(p.intervalHigh, 1)}`}>
          <div className="absolute top-1/2 h-px w-full bg-border-strong" />
          <div className="absolute top-1/2 h-4 w-px -translate-y-1/2 bg-faint" style={{ left: pos(0) }} />
          {hasInterval && <div className="absolute top-1/2 h-2 -translate-y-1/2 rounded-sm bg-accent-soft ring-1 ring-accent/50" style={{ left: pos(p.intervalLow), width: `calc(${pos(p.intervalHigh)} - ${pos(p.intervalLow)})` }} />}
          <div className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent" style={{ left: pos(p.expectedExcessReturn) }} />
        </div>
        <div className="num flex justify-between text-2xs text-faint">
          <span>{fmtPct(lo, 0)}</span>
          <span>0</span>
          <span>{fmtPct(hi, 0, true)}</span>
        </div>
      </div>
    </Panel>
  )
}

function ModelBody({ d }: { d: CompanyModel }) {
  if (!d.predictions.length)
    return (
      <EmptyState
        title="No model output for this listing"
        message={d.reason ? `${d.reason.label}: ${d.reason.message}` : 'ADRs and listings outside the training universes carry no prediction of their own; for an ADR open the home listing from the header.'}
      />
    )
  return (
    <>
      {d.scope === 'extrapolated' && (
        <div className="mb-3 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-fg" data-testid="ml-extrapolated">
          <span className="font-semibold">Extrapolated prediction.</span> This company is outside the MVP model universe: the production model (trained on the MVP universe) scored it with its features placed in the MVP cross-section, and the rank is its position among the MVP predictions. No expected return or interval is attached (not validated out of universe); the model metrics below are the MVP out-of-sample ones.{d.scopeNote ? ` ${d.scopeNote}.` : ''}
        </div>
      )}
      <ModelContent d={d} />
    </>
  )
}

function ModelContent({ d }: { d: CompanyModel }) {
  const t = useChartTokens()
  const xgb = d.predictions.find((p) => p.model === 'xgboost') ?? d.predictions[0]

  const waterfall = useMemo<EChartsOption>(() => {
    // floating bars via a custom series: [row, start, end, kind] (kind 0 = base/prediction, 1 = +, 2 = −)
    // v1.2: SHAP may be published in model-score units (shapUnit) rather than % excess return
    const raw = !!d.shapUnit
    const k = raw ? 1 : 100
    const unit = raw ? '' : '%'
    const fv = (v: number, sign = false) => (raw ? fmtNum(v, 3, sign) : fmtPct(v, 2, sign))
    const labels: string[] = ['Base', ...d.shap.map((s) => s.feature), raw ? 'Score' : 'Prediction']
    const rows: [number, number, number, number][] = [[0, 0, d.baseValue * k, 0]]
    let run = d.baseValue
    d.shap.forEach((s, i) => {
      const next = run + s.contribution
      rows.push([i + 1, run * k, next * k, s.contribution >= 0 ? 1 : 2])
      run = next
    })
    const total = run
    rows.push([labels.length - 1, 0, total * k, 0])
    const fill = (kind: number, row: number) => (kind === 1 ? t.up : kind === 2 ? t.down : row === 0 ? t.ref : t.accent)
    return {
      grid: { left: 8, right: 40, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const i = (ps as { dataIndex: number }[])[0].dataIndex
          if (i === 0) return ttTitle('Base value') + ttRow(t.ref, raw ? 'Base' : 'Avg. excess', fv(d.baseValue))
          if (i === labels.length - 1) return ttTitle(raw ? 'Sum of top contributions' : 'Prediction (XGBoost)') + ttRow(t.accent, raw ? 'Score' : 'Excess return', fv(total, true))
          const s = d.shap[i - 1]
          return ttTitle(s.feature) + ttRow(s.contribution >= 0 ? t.up : t.down, 'SHAP', fv(s.contribution, true)) + ttRow(t.fg, 'Feature value', s.featureValue) + ttRow(t.fg, 'Family', FACTOR_LABEL[s.family])
        },
      },
      yAxis: { type: 'category', data: labels, inverse: true, axisLine: { show: false }, axisLabel: { width: 170, overflow: 'truncate' } },
      xAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => `${v.toFixed(raw ? 2 : 1)}${unit}` } },
      series: [
        {
          type: 'custom',
          encode: { x: [1, 2], y: 0 },
          data: rows,
          renderItem: (_params: unknown, api: unknown) => {
            const a = api as { value: (i: number) => number; coord: (v: [number, number]) => number[]; size: (v: [number, number]) => number[] }
            const row = a.value(0)
            const s = a.coord([a.value(1), row])
            const e = a.coord([a.value(2), row])
            const h = a.size([0, 1])[1] * 0.62
            const kind = a.value(3)
            const delta = a.value(2) - a.value(1)
            const right = Math.max(s[0], e[0])
            const text = kind === 0 ? `${a.value(2) >= 0 ? '+' : '−'}${Math.abs(a.value(2)).toFixed(raw ? 3 : 2)}${unit}` : `${delta >= 0 ? '+' : '−'}${Math.abs(delta).toFixed(raw ? 3 : 2)}`
            return {
              type: 'group',
              children: [
                { type: 'rect', shape: { x: Math.min(s[0], e[0]), y: s[1] - h / 2, width: Math.max(1.5, Math.abs(e[0] - s[0])), height: h, r: 2 }, style: { fill: fill(kind, row) } },
                { type: 'text', style: { x: right + 4, y: s[1], text, fill: t.muted, font: `10px ${t.fontMono}`, verticalAlign: 'middle' } },
              ],
            }
          },
        },
      ],
    }
  }, [d, t])

  return (
    <div className="grid grid-cols-12 gap-3">
      {d.predictions.map((p) => (
        <PredictionCard key={`${p.modelVersion}-${p.horizonDays}`} p={p} />
      ))}
      <Panel title="Model info" className="col-span-12 xl:col-span-4">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
          <dt className="text-muted">As of</dt>
          <dd className="num">{fmtDate(d.asOf)}</dd>
          <dt className="text-muted">Feature set</dt>
          <dd className="num">{d.featureSetVersion}</dd>
          {d.predictions.map((p) => (
            <div key={`${p.modelVersion}-${p.horizonDays}`} className="contents">
              <dt className="text-muted">{p.modelType ? p.modelType.replace(/_/g, ' ') : p.model} · {p.horizonDays}d</dt>
              <dd className="num">{p.modelVersion}</dd>
            </div>
          ))}
          <dt className="text-muted">Target</dt>
          <dd>63-day forward return in excess of the sector × region mean (fwd_excess_sector), cross-sectionally ranked</dd>
          <dt className="text-muted">Validation</dt>
          <dd>Walk-forward, purged & embargoed folds; point-in-time features</dd>
        </dl>
        <div className="mt-3 flex gap-1.5">
          <Badge tone="info" title="Agreement of the first two cards; comparison models failed their leakage gate">Agreement {Math.abs(d.predictions[0].predictedRankPct - (d.predictions[1]?.predictedRankPct ?? 0)) < 15 ? 'high' : 'low'}</Badge>
          <Badge>{fmtNum(Math.abs(d.predictions[0].predictedRankPct - (d.predictions[1]?.predictedRankPct ?? 0)), 0)} pctl gap</Badge>
        </div>
      </Panel>
      <Panel title="Top drivers · SHAP waterfall" subtitle={d.shapUnit ? `${xgb.modelVersion} · contributions to the model's raw score (≈ standard-normal rank units) · starts at 0; last bar = sum of the ten shown` : `XGBoost ${xgb.modelVersion} · contributions in % excess return`} className="col-span-12 xl:col-span-7" bodyClassName="p-1.5">
        <EChart option={waterfall} height={400} ariaLabel="SHAP waterfall of top model drivers" />
      </Panel>
      <Panel title="Predicted rank history" subtitle="Weekly cross-sectional percentile of the production model (the Transformer line stays empty while XGBoost is production)" className="col-span-12 xl:col-span-5">
        <TimeSeriesChart
          height={380}
          ariaLabel="Predicted rank history"
          format={(v) => `p${fmtNum(v, 0)}`}
          yMin={0}
          yMax={100}
          series={[
            { name: 'XGBoost', data: d.rankHistory.map((r) => ({ date: r.date, value: r.xgboost })), color: t.series[0] },
            { name: 'Transformer', data: d.rankHistory.map((r) => ({ date: r.date, value: r.transformer })), color: t.series[1] },
          ]}
        />
      </Panel>
    </div>
  )
}
