import { CircleCheck, CircleX, TrendingDown, TrendingUp, MoveRight } from 'lucide-react'
import type { CompanyMoat, MoatEvidence } from '@/api/types'
import { useCompanyMoat } from '@/api/hooks'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { MoatBadge } from '@/components/ui/data'
import { PendingState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtNum, fmtPct, fmtValue } from '@/lib/format'

export default function MoatTab({ id }: { id: string }) {
  const q = useCompanyMoat(id)
  return (
    <QueryView query={q} skeleton={<Skeleton className="h-96" />}>
      {(d) => <MoatBody d={d} />}
    </QueryView>
  )
}

const pctFmt = (v: number) => fmtPct(v, 1)
const pctAxis = (v: number) => fmtPct(v, 0)

function MoatBody({ d }: { d: CompanyMoat }) {
  const t = useChartTokens()
  const TrendIcon = d.trend === 'widening' ? TrendingUp : d.trend === 'narrowing' ? TrendingDown : MoveRight
  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel title="Classification" className="col-span-12 lg:col-span-4">
        <div className="flex items-center gap-3">
          <MoatBadge rating={d.rating} />
          {d.modelVariant === 'financial' && (
            <Badge tone="info" title="Financial-company variant: deposit franchise / underwriting economics replace ROIC-based components">financial model</Badge>
          )}
          <Badge tone={d.trend === 'widening' ? 'up' : d.trend === 'narrowing' ? 'down' : 'neutral'}>
            <TrendIcon aria-hidden /> {d.trend}
          </Badge>
        </div>
        <div className="mt-3 grid grid-cols-3 gap-3">
          <Stat label="Moat score" value={fmtNum(d.score, 0)} sub={d.yearsEvaluated ? `${d.yearsEvaluated} years evaluated` : '0–100'} />
          <Stat label="Confidence" value={fmtPct(d.confidence, 0)} />
          <Stat
            label="Core evidence passed"
            value={`${d.evidence.filter((e) => e.passes && !e.supplementary).length}/${d.evidence.filter((e) => !e.supplementary).length}`}
            sub={d.evidence.some((e) => e.supplementary) ? `+ ${d.evidence.filter((e) => e.supplementary && e.passes).length}/${d.evidence.filter((e) => e.supplementary).length} supplementary` : undefined}
          />
        </div>
        <p className="mt-3 text-sm text-muted">{d.summary}</p>
        <div className="mt-3 border-t border-border pt-2">
          <div className="label-caps mb-1.5">Moat sources (strength)</div>
          <ul className="space-y-1.5">
            {d.sources.map((s) => (
              <li key={s.source} className="grid grid-cols-[120px_1fr_36px] items-center gap-2 text-xs">
                <span className="text-muted">{s.source}</span>
                <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                  <span className="block h-full rounded-full bg-accent" style={{ width: `${s.strength * 100}%` }} />
                </span>
                <span className="num text-right">{Math.round(s.strength * 100)}</span>
              </li>
            ))}
          </ul>
        </div>
      </Panel>

      <Panel title={d.roicVsWacc.some((p) => Number.isFinite(p.hurdle ?? NaN) && p.hurdle !== p.wacc) ? 'ROIC vs hurdle' : 'ROIC vs WACC'} subtitle="Per-year hurdle rate · the spread is the economic moat" className="col-span-12 lg:col-span-8">
        <TimeSeriesChart
          height={280}
          ariaLabel="Return on invested capital versus weighted average cost of capital"
          format={pctFmt}
          axisFormat={pctAxis}
          series={[
            { name: 'ROIC', data: d.roicVsWacc.map((p) => ({ date: p.date, value: p.roic })), color: t.accent, area: true },
            { name: 'WACC', data: d.roicVsWacc.map((p) => ({ date: p.date, value: p.wacc })), color: t.ref, dashed: true },
            ...(d.roicVsWacc.some((p) => Number.isFinite(p.hurdle ?? NaN) && p.hurdle !== p.wacc)
              ? [{ name: 'Hurdle', data: d.roicVsWacc.map((p) => ({ date: p.date, value: p.hurdle ?? null })), color: t.warn, dashed: true, step: true }]
              : []),
          ]}
        />
      </Panel>

      {d.evidence.map((e) => (
        <EvidenceCard key={e.id} e={e} />
      ))}
      <Panel title="Market share" subtitle="Share of subsector revenue" className="col-span-12 md:col-span-6 xl:col-span-4">
        {d.marketShare.length === 0 ? <PendingState compact detail="No market-share series for this company yet." /> : <TimeSeriesChart height={150} ariaLabel="Market share history" format={(v) => fmtPct(v, 1)} axisFormat={(v) => fmtPct(v, 1)} series={[{ name: 'Share', data: d.marketShare, color: t.series[1], area: true }]} />}
      </Panel>
    </div>
  )
}

/** Raw component history: ratio-like values (|v| ≤ 2) as %, else plain numbers. */
function rawFormat(hist: { value: number }[]): (v: number) => string {
  const ratio = hist.length > 0 && hist.every((p) => Math.abs(p.value) <= 2)
  return ratio ? (v) => fmtPct(v, 1) : (v) => fmtNum(v, 2)
}

function EvidenceCard({ e }: { e: MoatEvidence }) {
  const t = useChartTokens()
  const scored = typeof e.score === 'number'
  const f = scored ? rawFormat(e.history) : (v: number) => fmtValue(v, e.unit)
  return (
    <Panel
      className="col-span-12 md:col-span-6 xl:col-span-4"
      title={
        <span className="inline-flex items-center gap-1.5">
          {e.label}
          {e.supplementary && <Badge tone="neutral" title="Supplementary component — lower weight, shown for context">supplementary</Badge>}
        </span>
      }
      subtitle={e.description}
      actions={
        e.passes ? (
          <Badge tone="up"><CircleCheck aria-hidden /> Pass</Badge>
        ) : (
          <Badge tone="down"><CircleX aria-hidden /> Fail</Badge>
        )
      }
    >
      {scored ? (
        <div className="mb-2">
          <div className="flex items-baseline gap-2">
            <span className="num text-lg font-semibold">{fmtNum(e.score, 0)}</span>
            <span className="text-xs text-muted">score · threshold {fmtNum(e.threshold, 0)}</span>
          </div>
          <div className="relative mt-1 h-1.5 rounded-full bg-surface-3" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(e.score ?? 0)} aria-label={`${e.label} score`}>
            <span className={`absolute inset-y-0 left-0 rounded-full ${e.passes ? 'bg-up' : 'bg-down'}`} style={{ width: `${Math.max(0, Math.min(100, e.score ?? 0))}%` }} />
            <span className="absolute -top-0.5 -bottom-0.5 w-0.5 bg-fg" style={{ left: `${Math.max(0, Math.min(100, e.threshold))}%` }} title="threshold" />
          </div>
        </div>
      ) : (
        <div className="mb-1 flex items-baseline gap-2">
          <span className="num text-lg font-semibold">{fmtValue(e.value, e.unit)}</span>
          <span className="text-xs text-muted">threshold {fmtValue(e.threshold, e.unit)}</span>
        </div>
      )}
      {e.history.length === 0 ? (
        <div className="py-6 text-center text-xs text-muted">No yearly history published.</div>
      ) : (
        <TimeSeriesChart
          height={scored ? 112 : 130}
          ariaLabel={`${e.label} history`}
          format={f}
          axisFormat={f}
          legend={false}
          threshold={scored || e.id === 'gmStability' ? undefined : { value: e.threshold, label: 'threshold' }}
          series={[{ name: scored ? `${e.label} (raw)` : e.label, data: e.history, color: t.series[0] }]}
        />
      )}
    </Panel>
  )
}
