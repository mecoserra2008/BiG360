import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { useCompanyMacro } from '@/api/macro/hooks'
import { type CompanyMacroV4, type ExposureModel, REGIME_LABEL, betaInterval } from '@/api/macro/v34'
import { Badge } from '@/components/ui/badge'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtChange, fmtMacro } from '../../macro/fmt'
import { ImpactBars } from '../../macro/markets/ImpactBars'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v: unknown, d = 1) => (fin(v) ? `${fmtChange(v * 100, d)}%` : DASH)
const raw = (v: number) => fmtChange(v, 3)
const rawAxis = (v: number) => fmtMacro(v, 2)
const REGIME_TYPE: Record<string, string> = { cycle_quadrant: 'Cycle quadrant', hmm_regime: 'HMM regime (model)', rate_cycle: 'Rate cycle', inflation_regime: 'Inflation regime' }

export default function MacroTab({ id }: { id: string }) {
  const q = useCompanyMacro(id)
  return (
    <div className="pt-3">
      <QueryView query={q} skeleton={<Skeleton className="h-[480px]" />}>
        {(d) => (d.macro_v4 ? <Body v={d.macro_v4} /> : <EmptyState title="No macro exposures" message="This security has no macro betas yet (needs at least 52 weeks of prices)." />)}
      </QueryView>
    </div>
  )
}

function Body({ v }: { v: CompanyMacroV4 }) {
  const [model, setModel] = useState<ExposureModel>('joint')
  const ex = useMemo(() => v.exposures[model] ?? [], [v, model])
  const fit = v.fit[model]
  const groups = useMemo(() => {
    const el = ex.filter((e) => (e.per ?? '').startsWith('elasticity'))
    const lin = ex.filter((e) => !(e.per ?? '').startsWith('elasticity'))
    return [
      { key: 'lin', title: model === 'surprise' ? 'Macro surprises' : 'Rates, curve, inflation and credit', rows: lin },
      { key: 'el', title: 'Elasticities (USD, oil, equity)', rows: el },
    ].filter((g) => g.rows.length)
  }, [ex, model])
  const methodText = (k: string) => String((v.method as Record<string, unknown>)[k] ?? '')
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Badge tone="info">model-based</Badge>
        <span>{v.model_label}</span>
        <span>· betas as of {v.as_of ? fmtDate(v.as_of) : DASH}</span>
        {v.sector_id && <span>· sector {v.sector_id}</span>}
        {v.region && <span>· region {v.region}</span>}
        <Link to="/macro/markets?tab=scenarios" className="ml-auto text-accent-text hover:underline">
          Open scenario builder
        </Link>
      </div>
      <Panel
        title="Macro exposures vs sector"
        subtitle={`Shrunk security betas with 90% intervals (±1.645 SE); diamond = sector beta · ${fit ? `R² ${fmtMacro(fit.r2, 2)} on ${fit.n_obs} weeks` : 'no fit'}`}
        actions={
          <Segmented
            label="Model"
            size="xs"
            value={model}
            onChange={setModel}
            options={[
              { value: 'joint', label: 'Joint' },
              { value: 'univariate', label: 'Single-factor' },
              { value: 'surprise', label: 'Surprise' },
            ]}
          />
        }
      >
        {ex.length === 0 ? (
          <p className="py-6 text-center text-xs text-muted">No betas for this model.</p>
        ) : (
          <>
            <div className="grid gap-3 xl:grid-cols-2">
              {groups.map((g) => (
                <div key={g.key}>
                  <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">{g.title}</div>
                  <ImpactBars
                    rows={g.rows.map((e) => {
                      const ci = betaInterval(e.beta, e.se)
                      return { label: e.label ?? e.factor_id, value: e.beta, lo: ci?.[0], hi: ci?.[1], ref: e.sector_beta }
                    })}
                    valueLabel="company beta"
                    refLabel="sector beta"
                    fmt={raw}
                    axisFmt={rawAxis}
                    ariaLabel={`${g.title}: company vs sector betas`}
                  />
                </div>
              ))}
            </div>
            <div className="mt-3 overflow-auto">
              <table className="w-full text-xs" aria-label="Macro betas">
                <thead>
                  <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                    <th className="px-2 py-1">Factor</th>
                    <th className="px-2 py-1">Per</th>
                    <th className="px-2 py-1 text-right">Beta</th>
                    <th className="px-2 py-1 text-right">90% interval</th>
                    <th className="px-2 py-1 text-right">t</th>
                    <th className="px-2 py-1 text-right">Sector</th>
                    <th className="px-2 py-1 text-right">Raw (unshrunk)</th>
                    <th className="px-2 py-1 text-right">Peer pct.</th>
                  </tr>
                </thead>
                <tbody>
                  {ex.map((e) => {
                    const ci = betaInterval(e.beta, e.se)
                    const sig = fin(e.t_stat) && Math.abs(e.t_stat) >= 1.96
                    return (
                      <tr key={e.factor_id} className="border-b border-border/60">
                        <td className="px-2 py-0.5 font-medium text-fg">{e.label ?? e.factor_id}</td>
                        <td className="px-2 py-0.5 text-muted">{e.per ?? DASH}</td>
                        <td className={cn('num px-2 py-0.5 text-right', sig ? 'font-semibold text-fg' : 'text-muted')}>{fmtChange(e.beta, 3)}</td>
                        <td className="num px-2 py-0.5 text-right text-muted">{ci ? `${fmtChange(ci[0], 3)} to ${fmtChange(ci[1], 3)}` : DASH}</td>
                        <td className="num px-2 py-0.5 text-right">{fmtChange(e.t_stat, 2)}</td>
                        <td className="num px-2 py-0.5 text-right">{fmtChange(e.sector_beta, 3)}</td>
                        <td className="num px-2 py-0.5 text-right text-muted">{fmtChange(e.beta_raw, 3)}</td>
                        <td className="num px-2 py-0.5 text-right">{fin(e.peer_percentile) ? e.peer_percentile.toFixed(0) : DASH}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
        <p className="mt-2 text-2xs text-muted">
          {methodText('label')}. {methodText(model)}. {methodText('shrinkage')}.
        </p>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Scenario impacts" subtitle="Presets and your saved scenarios, model-implied return with 90% band · diamond = sector">
          {v.scenarios.length ? (
            <ImpactBars rows={v.scenarios.map((s) => ({ label: s.preset ? s.name : `${s.name} (saved)`, value: s.impact, lo: s.impact_lo, hi: s.impact_hi, ref: s.sector_impact }))} valueLabel={v.ticker ?? 'company'} refLabel="sector" ariaLabel="Scenario impacts, company vs sector" />
          ) : (
            <p className="py-6 text-center text-xs text-muted">No scenarios.</p>
          )}
        </Panel>
        <Panel title="Episode replay" subtitle={`Episode factor moves applied to the current betas · diamond = realized, dot = ${v.sector_id ?? 'sector'} portfolio model`}>
          {v.episodes.length ? (
            <>
              <ImpactBars
                rows={v.episodes.map((e) => ({ label: e.name, value: e.model_implied, lo: e.model_lo, hi: e.model_hi, ref: e.realized, ref2: e.sector?.model_implied }))}
                valueLabel={`${v.ticker ?? 'company'} model`}
                refLabel={`${v.ticker ?? 'company'} realized`}
                ref2Label="sector model"
                ariaLabel="Episode replay: company model vs realized, with the sector model"
              />
              <table className="mt-2 w-full text-xs" aria-label="Episode replay, company vs sector">
                <thead>
                  <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                    <th className="px-2 py-1">Episode</th>
                    <th className="px-2 py-1 text-right">{v.ticker ?? 'Company'} model</th>
                    <th className="px-2 py-1 text-right">Realized</th>
                    <th className="px-2 py-1 text-right">Sector model</th>
                    <th className="px-2 py-1 text-right">Sector realized</th>
                  </tr>
                </thead>
                <tbody>
                  {v.episodes.map((e) => (
                    <tr key={e.episode_id} className="border-b border-border/60">
                      <td className="px-2 py-0.5 text-fg" title={`${fmtDate(e.start)} → ${fmtDate(e.end)}`}>
                        {e.name}
                      </td>
                      <td className="num px-2 py-0.5 text-right" title={fin(e.model_lo) ? `90%: ${pc(e.model_lo)} to ${pc(e.model_hi)}` : undefined}>
                        {pc(e.model_implied)}
                      </td>
                      <td className="num px-2 py-0.5 text-right">{fin(e.realized) ? pc(e.realized) : <span className="text-faint">not listed</span>}</td>
                      <td className="num px-2 py-0.5 text-right" title={fin(e.sector?.model_lo) ? `90%: ${pc(e.sector?.model_lo)} to ${pc(e.sector?.model_hi)} · ${e.sector?.weighting ?? ''}` : undefined}>
                        {pc(e.sector?.model_implied)}
                      </td>
                      <td className="num px-2 py-0.5 text-right">{fin(e.sector?.realized) ? pc(e.sector?.realized) : <span className="text-faint">{DASH}</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <p className="py-6 text-center text-xs text-muted">No episodes.</p>
          )}
        </Panel>
      </div>
      <Panel title={`Sector regime performance${v.sector_id ? ` (${v.sector_id})` : ''}`} subtitle="Mean monthly return of the sector relative to the benchmark in each regime · current regime outlined" flush>
        <div className="overflow-auto">
          <table className="w-full text-xs">
            <tbody>
              {v.sector_regimes.map((r) => (
                <tr key={`${r.regime_type}-${r.scope}`} className="border-b border-border/60">
                  <th scope="row" className="px-3 py-1 text-left font-medium whitespace-nowrap text-fg">
                    {REGIME_TYPE[r.regime_type] ?? r.regime_type} <span className="text-muted">· {r.scope.toUpperCase()}</span>
                    {r.regime_type === 'hmm_regime' && (
                      <Badge tone="info" className="ml-1">
                        model
                      </Badge>
                    )}
                  </th>
                  {r.cells.map((c) => {
                    const sig = fin(c.t_stat) && Math.abs(c.t_stat) >= 1.96
                    return (
                      <td key={c.regime} className="px-1 py-1">
                        <div className={cn('rounded-sm px-2 py-0.5', c.is_current ? 'outline outline-1 outline-accent' : '')}>
                          <div className="text-2xs text-muted">
                            {REGIME_LABEL[c.regime] ?? c.regime}
                            {c.is_current ? ' (now)' : ''}
                          </div>
                          <div className={cn('num', sig ? 'font-semibold' : '', fin(c.mean) && (c.mean >= 0 ? 'text-up' : 'text-down'))}>{pc(c.mean, 2)}</div>
                          <div className="num text-2xs text-faint">
                            hit {fin(c.hit_rate) ? `${(c.hit_rate * 100).toFixed(0)}%` : DASH} · t {fmtMacro(c.t_stat, 2)} · n {c.n}
                          </div>
                        </div>
                      </td>
                    )
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  )
}
