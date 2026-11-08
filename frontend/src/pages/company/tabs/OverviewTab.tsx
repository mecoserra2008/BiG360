import { AlertTriangle, Info, OctagonAlert } from 'lucide-react'
import { useMemo } from 'react'
import type { BeneishDetail, CompanyAlert, CompanyOverview, FactorScoreDetail, KeyRatio } from '@/api/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { RangePriceChart } from '@/components/charts/RangePriceChart'
import { Sparkline } from '@/components/charts/Sparkline'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { PercentileBar, ScorePill } from '@/components/ui/data'
import { Avail } from '@/components/ui/missing'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { FACTOR_LABEL } from '@/lib/factors'
import { fmtDate, fmtNum, fmtPct, fmtScore, fmtValue, fmtZ } from '@/lib/format'
import { cn } from '@/lib/utils'

export function OverviewTab({ data }: { data: CompanyOverview }) {
  return (
    <div className="grid grid-cols-12 gap-3">
      <FactorRadar factors={data.factors} ticker={data.profile.ticker} className="col-span-12 lg:col-span-5 xl:col-span-4" />
      <ScoreBreakdown factors={data.factors} total={data.profile.compositeScore} validated={data.profile.compositeValidated === true} className="col-span-12 lg:col-span-7 xl:col-span-4" />
      <Panel title="Price" subtitle={`${data.profile.currency} · adjusted close · full history available`} className="col-span-12 xl:col-span-4" bodyClassName="p-1.5">
        <RangePriceChart id={data.profile.id} initial={data.priceHistory} mode="area" height={230} showVolume={false} currency={data.profile.currency} ariaLabel={`${data.profile.ticker} price`} />
      </Panel>
      <KeyRatios ratios={data.keyRatios} className="col-span-12 xl:col-span-8" />
      <Alerts alerts={data.alerts} beneish={data.beneish ?? null} className="col-span-12 xl:col-span-4" />
    </div>
  )
}

function FactorRadar({ factors, ticker, className }: { factors: FactorScoreDetail[]; ticker: string; className?: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(
    () => ({
      legend: { bottom: 0, data: [ticker, 'Peer median'] },
      tooltip: { trigger: 'item' },
      radar: {
        radius: '66%',
        center: ['50%', '48%'],
        indicator: factors.map((f) => ({ name: FACTOR_LABEL[f.family], max: 100 })),
        axisName: { color: t.muted, fontSize: 10 },
        splitNumber: 4,
        splitLine: { lineStyle: { color: t.grid } },
        axisLine: { lineStyle: { color: t.grid } },
        splitArea: { show: false },
      },
      series: [
        {
          type: 'radar',
          symbol: 'circle',
          symbolSize: 4,
          data: [
            { name: 'Peer median', value: factors.map((f) => f.peerMedian), lineStyle: { color: t.ref, width: 1.5, type: 'dashed' }, itemStyle: { color: t.ref }, areaStyle: { color: 'transparent' } },
            { name: ticker, value: factors.map((f) => f.score), lineStyle: { color: t.accent, width: 2 }, itemStyle: { color: t.accent }, areaStyle: { color: alpha(t.accent, 0.16) } },
          ],
        },
      ],
    }),
    [factors, ticker, t],
  )
  return (
    <Panel title="Factor profile" subtitle="Family percentiles vs the sector × peer-region median (≈ 50 by construction)" className={className} bodyClassName="p-1.5">
      <EChart option={option} height={268} ariaLabel={`${ticker} factor radar versus peer median`} />
    </Panel>
  )
}

function ScoreBreakdown({ factors, total, validated, className }: { factors: FactorScoreDetail[]; total: number; validated: boolean; className?: string }) {
  const maxAbs = Math.max(0.5, ...factors.map((f) => Math.abs(f.contribution)))
  const sorted = [...factors].sort((a, b) => b.contribution - a.contribution)
  return (
    <Panel title="Score breakdown" subtitle={`Composite ${fmtScore(total)} = 50 + Σ weight × (score − 50) · a peer ranking, not a forecast`} actions={<CompositeLabel validated={validated} />} className={className} flush>
      <table className="w-full text-sm">
        <thead>
          <tr className="text-2xs tracking-wide text-muted uppercase">
            <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Family</th>
            <th scope="col" className="border-b border-border px-2 text-right font-semibold">Pctl</th>
            <th scope="col" className="border-b border-border px-2 text-right font-semibold">Wt</th>
            <th scope="col" className="w-[38%] border-b border-border px-3 text-center font-semibold">Contribution</th>
            <th scope="col" className="border-b border-border px-3 text-right font-semibold">Pts</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((f) => {
            const w = (Math.abs(f.contribution) / maxAbs) * 50
            return (
              <tr key={f.family}>
                <td className="h-[25px] border-b border-border px-3">
                  <Tip content={<div className="space-y-0.5">{f.subFactors.map((s) => <div key={s.name} className="flex justify-between gap-4"><span>{s.name}</span><span className="num">{s.score}</span></div>)}</div>}>
                    <span tabIndex={0} className="cursor-help underline decoration-border-strong decoration-dotted underline-offset-2">{FACTOR_LABEL[f.family]}</span>
                  </Tip>
                </td>
                <td className="border-b border-border px-2 text-right"><ScorePill value={f.score} /></td>
                <td className="num border-b border-border px-2 text-right text-muted">{fmtPct(f.weight, 0)}</td>
                <td className="border-b border-border px-3">
                  <div className="relative flex h-3 items-center" aria-hidden>
                    <span className="absolute inset-y-0 left-1/2 w-px bg-border-strong" />
                    <span className={cn('absolute h-2 rounded-sm', f.contribution >= 0 ? 'left-1/2 bg-up' : 'right-1/2 bg-down')} style={{ width: `${w}%` }} />
                  </div>
                </td>
                <td className={cn('num border-b border-border px-3 text-right whitespace-nowrap', f.contribution > 0 ? 'text-up' : f.contribution < 0 ? 'text-down' : 'text-muted')}>
                  {f.contribution > 0 ? '▲' : f.contribution < 0 ? '▼' : ''} {fmtNum(f.contribution, 1, true)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </Panel>
  )
}

function KeyRatios({ ratios, className }: { ratios: KeyRatio[]; className?: string }) {
  return (
    <Panel title="Key ratios" subtitle="Sector-specific KPI set · peer percentile (higher = better) · z-score vs own 5-year history" className={className} flush>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[640px] text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Metric</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold">Value</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold">Peer median</th>
              <th scope="col" className="border-b border-border px-2 text-left font-semibold">Peer pctl</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold">5y z</th>
              <th scope="col" className="border-b border-border px-3 text-right font-semibold">5y trend</th>
            </tr>
          </thead>
          <tbody>
            {ratios.map((r) => {
              const z = r.selfZScore5y ?? 0
              // self_z is already direction-adjusted by the API (positive = better than own history): no second flip
              const zGood = z
              return (
                <tr key={r.id} className="hover:bg-surface-2">
                  <td className="h-[27px] border-b border-border px-3">
                    {r.label} <span className="text-2xs text-faint">{r.higherIsBetter ? '↑' : '↓'} better</span>
                  </td>
                  <td className="num border-b border-border px-2 text-right font-medium text-fg"><Avail value={r.value} field={r.id}>{fmtValue(r.value, r.unit)}</Avail></td>
                  <td className="num border-b border-border px-2 text-right text-muted"><Avail value={r.peerMedian} reason="No peer median: too few companies of the peer group report this metric">{fmtValue(r.peerMedian, r.unit)}</Avail></td>
                  <td className="border-b border-border px-2"><PercentileBar value={r.peerPercentile} width="w-20" /></td>
                  <td className={cn('num border-b border-border px-2 text-right', Math.abs(z) >= 1.5 ? (zGood > 0 ? 'text-up' : 'text-down') : 'text-muted')}>{fmtZ(r.selfZScore5y)}</td>
                  <td className="border-b border-border px-3 text-right">
                    <Sparkline data={r.history} invert={!r.higherIsBetter} width={80} height={18} ariaLabel={`${r.label} 5-year history`} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}

const SEV = {
  critical: { Icon: OctagonAlert, cls: 'text-down', tone: 'down' as const },
  warning: { Icon: AlertTriangle, cls: 'text-warn', tone: 'warn' as const },
  info: { Icon: Info, cls: 'text-info', tone: 'info' as const },
}

function Alerts({ alerts, beneish, className }: { alerts: CompanyAlert[]; beneish: BeneishDetail | null; className?: string }) {
  return (
    <Panel title="Alerts" subtitle="Signals raised in the last 40 days" className={className} flush>
      {alerts.length === 0 ? (
        <div className="p-6 text-center text-sm text-muted">No active alerts.</div>
      ) : (
        <ul>
          {alerts.map((a) => {
            const s = SEV[a.severity]
            return (
              <li key={a.id} className="flex gap-2.5 border-b border-border px-3 py-2 last:border-b-0">
                <s.Icon className={cn('mt-0.5 size-4 shrink-0', s.cls)} aria-label={a.severity} />
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-fg">{a.message}</div>
                  <div className="mt-0.5 flex flex-wrap items-center gap-2 text-2xs text-muted">
                    <Badge tone={s.tone}>{a.severity}</Badge>
                    {a.modelBased && <Badge tone="info">model-based</Badge>}
                    <span className="capitalize">{a.category}</span>
                    <span className="num">{fmtDate(a.date)}</span>
                  </div>
                  {a.code?.startsWith('beneish') && beneish && <BeneishBreakdown b={beneish} />}
                </div>
              </li>
            )
          })}
        </ul>
      )}
    </Panel>
  )
}

const fmtM = (v: number | null) => (v === null ? '—' : fmtNum(v, 2))

/** v1.29 Beneish breakdown: indices, contributions, growth-neutral M, red flags and corroboration (model-based screen). */
function BeneishBreakdown({ b }: { b: BeneishDetail }) {
  return (
    <details className="mt-1.5 rounded-sm border border-border bg-surface-2 px-2 py-1 text-xs">
      <summary className="cursor-pointer text-muted select-none">
        Breakdown · M {fmtM(b.mScore)} · growth-neutral M {fmtM(b.mScoreGrowthNeutral)} · threshold {fmtNum(b.threshold, 2)}
      </summary>
      <div className="mt-1.5 space-y-1.5">
        <div className="flex flex-wrap gap-1">
          <Badge tone="info">model-based</Badge>
          <Badge tone={b.level === 'flag' ? 'warn' : 'neutral'}>{b.level.replace('_', ' ')}</Badge>
          {b.growthDriven && <Badge>growth-driven</Badge>}
          {!b.applicable && <Badge>not applicable (financials / REITs)</Badge>}
        </div>
        {b.components.length > 0 && (
          <table className="w-full" aria-label="Beneish M-score components">
            <thead>
              <tr className="text-2xs text-muted uppercase">
                <th scope="col" className="py-0.5 text-left font-semibold">Index</th>
                <th scope="col" className="text-right font-semibold">Value</th>
                <th scope="col" className="text-right font-semibold" title="Mean of Beneish's manipulator sample">Manip. mean</th>
                <th scope="col" className="text-right font-semibold" title="weight × (index − neutral)">Contrib.</th>
              </tr>
            </thead>
            <tbody>
              {b.components.map((c) => (
                <tr key={c.id} className={c.redFlag ? 'text-warn' : 'text-fg'}>
                  <td className="py-0.5 pr-1">
                    {c.label}
                    {c.redFlag && <span className="sr-only"> (red flag)</span>}
                    {c.redFlag && <span aria-hidden> ●</span>}
                  </td>
                  <td className="num text-right">{fmtM(c.value)}</td>
                  <td className="num text-right text-muted">{fmtM(c.manipulatorMean)}</td>
                  <td className="num text-right">{c.contribution === null ? '—' : fmtNum(c.contribution, 2, true)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div className="text-2xs text-muted">
          Red flags (above the manipulator mean, SGI excluded): {b.redFlags.length ? b.redFlags.map((x) => x.toUpperCase()).join(', ') : 'none'}
        </div>
        <div className="text-2xs text-muted">Corroboration outside the M-score: {b.corroboration.length ? b.corroboration.join(', ') : 'none'}</div>
        <p className="text-2xs text-faint">
          {b.method ?? 'Beneish (1999) 8-variable M-score'}
          {b.periodEnd ? ` · period ${fmtDate(b.periodEnd)}` : ''}. A statistical screen, not an accounting finding; high sales growth alone lifts the textbook score.
        </p>
      </div>
    </details>
  )
}
