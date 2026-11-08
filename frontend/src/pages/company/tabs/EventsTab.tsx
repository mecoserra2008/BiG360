import { useMemo } from 'react'
import { useEmCompany, useEvStories } from '@/api/events/hooks'
import type { EmCompany } from '@/api/events/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { LabelEventsPanel } from '../../events/v2/LabelPanel'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v: unknown, d = 2) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%` : DASH)
const W_SOURCE: Record<string, string> = {
  home_country: 'home country only',
  geo_revenue: 'geographic revenue',
  'geo_revenue+supply_chain': 'geographic revenue + supply chain',
  'home_country+supply_chain': 'home country + supply chain',
}

export default function EventsTab({ id }: { id: string }) {
  const exp = useEmCompany(id)
  const st = useEvStories({ securityId: id, days: 30 }, 30)
  return (
    <div className="space-y-3 pt-3">
      <Panel
        title="Event exposure"
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <Badge tone="info">model-based</Badge> Event pressure in the countries this company earns in (geographic revenue and supply-chain weights) and the expected 5-day CAR from active shocks (event-study library)
          </span>
        }
      >
        <QueryView query={exp} skeleton={<Skeleton className="h-56" />}>
          {(d) => <ExposureBody d={d} />}
        </QueryView>
      </Panel>
      <LabelEventsPanel type="issuer" id={id} title="Company events" stories={st.data?.items} />
    </div>
  )
}

function ExposureBody({ d }: { d: EmCompany }) {
  const t = useChartTokens()
  const hist = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 8, bottom: 20, containLabel: true },
      tooltip: { trigger: 'axis' },
      xAxis: { type: 'category', data: d.history.map((h) => h.as_of) },
      yAxis: { type: 'value', min: 0, max: 100 },
      series: [
        {
          name: 'exposure score',
          type: 'line',
          data: d.history.map((h) => h.exposure_score),
          lineStyle: { color: t.series[0] },
          itemStyle: { color: t.series[0] },
        },
      ],
    }),
    [d.history, t],
  )
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Exposure score" value={<span className="num">{fin(d.exposure_score) ? d.exposure_score.toFixed(0) : DASH}</span>} sub="percentile of event pressure (0–100)" />
        <Stat label="Event pressure" value={<span className="num">{fin(d.event_pressure) ? d.event_pressure.toFixed(2) : DASH}</span>} sub={`${d.n_countries} countries weighted`} />
        <Stat label="Expected impact (5d)" value={<span className={cn('num', fin(d.expected_impact) && (d.expected_impact >= 0 ? 'text-up' : 'text-down'))}>{pc(d.expected_impact)}</span>} sub="expected CAR from active shocks (model)" />
        <Stat label="Weights" value={<span className="text-sm">{d.w_source ? (W_SOURCE[d.w_source] ?? d.w_source) : DASH}</span>} sub={`as of ${d.as_of ? fmtDate(d.as_of) : DASH}`} />
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
        <div>
          <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Top countries</div>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                <th className="py-1">Country</th>
                <th className="py-1 text-right">Weight</th>
                <th className="py-1 text-right">Pressure</th>
              </tr>
            </thead>
            <tbody>
              {d.top_countries.map((c) => (
                <tr key={c.iso2} className="border-b border-border/60">
                  <td className="py-0.5">
                    <span className="inline-flex items-center gap-1.5">
                      {c.iso2.length === 2 && <Flag country={c.iso2} />}
                      {c.name ?? c.iso2}
                    </span>
                  </td>
                  <td className="num py-0.5 text-right">{fin(c.weight) ? `${(c.weight * 100).toFixed(0)}%` : DASH}</td>
                  <td className="num py-0.5 text-right">{fin(c.pressure) ? c.pressure.toFixed(2) : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div>
          <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Active shocks driving the expected impact</div>
          {d.top_shocks.length === 0 ? (
            <p className="text-xs text-muted">No active shocks.</p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                  <th className="py-1">Country · theme</th>
                  <th className="py-1 text-right">z</th>
                  <th className="py-1 text-right">Exp. CAR</th>
                  <th className="py-1 text-right">n</th>
                </tr>
              </thead>
              <tbody>
                {d.top_shocks.map((s, i) => (
                  <tr key={i} className="border-b border-border/60">
                    <td className="py-0.5">
                      {s.iso2} · {s.theme.replace(/_/g, ' ')}
                    </td>
                    <td className="num py-0.5 text-right">{fin(s.attention_z) ? s.attention_z.toFixed(1) : DASH}</td>
                    <td className={cn('num py-0.5 text-right', fin(s.expected_car) && (s.expected_car >= 0 ? 'text-up' : 'text-down'))}>{pc(s.expected_car, 1)}</td>
                    <td className="num py-0.5 text-right text-muted">{s.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div>
          <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Escalation risk in exposed countries</div>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                <th className="py-1">Country · group</th>
                <th className="py-1 text-right">P 7d</th>
                <th className="py-1 text-right">P 30d</th>
              </tr>
            </thead>
            <tbody>
              {d.country_escalation.slice(0, 10).map((e, i) => (
                <tr key={i} className="border-b border-border/60">
                  <td className="py-0.5">
                    {e.iso2} · {e.theme_group}
                    {fin(e.branching_ratio) && e.branching_ratio > 1 && (
                      <Badge tone="warn" className="ml-1">
                        explosive
                      </Badge>
                    )}
                  </td>
                  <td className="num py-0.5 text-right">{fin(e.p_escalation_7d) ? `${(e.p_escalation_7d * 100).toFixed(0)}%` : DASH}</td>
                  <td className="num py-0.5 text-right">{fin(e.p_escalation_30d) ? `${(e.p_escalation_30d * 100).toFixed(0)}%` : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      {d.history.length > 1 && <EChart option={hist} height={140} ariaLabel="Exposure score history" />}
    </div>
  )
}
