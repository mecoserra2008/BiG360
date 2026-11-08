import { debtKpis, fmtRate, wallBars, wallTotal } from '@/api/rates/adapters'
import { useCompanyDebt } from '@/api/rates/hooks'
import type { CompanyDebt } from '@/api/rates/types'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { Badge } from '@/components/ui/badge'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtCompact, fmtDate, fmtMultiple, fmtPct } from '@/lib/format'
import { Bars } from '../../rates/charts'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

/** Company 360 → Debt: maturity wall, cost of debt, coverage, leverage history, fixed vs floating, listed bonds. */
export default function DebtTab({ id }: { id: string }) {
  const q = useCompanyDebt(id)
  return (
    <div className="pt-3">
      <QueryView query={q} skeleton={<Skeleton className="h-[520px]" />}>
        {(d) => (d.history.length || d.bonds.length ? <Body d={d} /> : <EmptyState title="No debt data" message={d.reasons.join(' · ') || 'No debt items in the filings.'} />)}
      </QueryView>
    </div>
  )
}

function fmtKpi(v: number | null, unit: 'x' | 'pct' | 'years'): string {
  if (!fin(v)) return DASH
  if (unit === 'x') return fmtMultiple(v)
  if (unit === 'years') return `${v.toFixed(1)}y`
  return fmtPct(v, unit === 'pct' && Math.abs(v) < 0.2 ? 2 : 1)
}

function Body({ d }: { d: CompanyDebt }) {
  const L = d.latest
  const ccy = L?.currency ?? 'USD'
  const kpis = debtKpis(L)
  const wall = d.maturity_wall ? wallBars(d.maturity_wall) : []
  const tot = d.maturity_wall ? wallTotal(d.maturity_wall) : null
  const fy = d.history.filter((h) => h.period_kind === 'FY')
  const hist = fy.length >= 2 ? fy : d.history
  const peer = d.sector_peer
  return (
    <div className="space-y-3">
      <Panel
        title="Debt profile"
        subtitle={L ? `Balance sheet ${fmtDate(L.period_end)} (${L.period_kind === 'FY' ? 'fiscal year' : 'interim, TTM flows'}) · public from ${fmtDate(L.knowledge_date)} · ${ccy}` : undefined}
      >
        <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
          <Kpi label="Total debt" value={fmtCompact(L?.total_debt)} />
          <Kpi label="Net debt" value={fmtCompact(L?.net_debt)} />
          {kpis.map((k) => (
            <Kpi
              key={k.key}
              label={k.label}
              value={fmtKpi(k.value, k.unit)}
              sub={peer && k.key === 'avg_cost_of_debt' ? `sector ${fmtPct(peer.wavg_cost_of_debt, 2)}` : peer && k.key === 'interest_coverage' ? `sector median ${fmtMultiple(peer.median_interest_coverage)}` : peer && k.key === 'net_debt_to_ebitda' ? `sector median ${fmtMultiple(peer.median_net_debt_to_ebitda)}` : undefined}
            />
          ))}
        </div>
        {fin(L?.refi_rate_gap_bp) ? (
          <p className="mt-2 text-xs text-muted">
            <Badge tone="info">model-based</Badge> Refinancing at today&apos;s market rate ({fmtPct(L?.refi_market_rate, 2)} = local 5y government yield + ICE BofA {fin(L?.interest_coverage) && (L?.interest_coverage as number) >= 4 ? 'IG' : 'HY'} OAS) would change the cost of the
            debt maturing within 24 months by {Math.round(L?.refi_rate_gap_bp as number)} bp ≈ {fmtCompact(L?.refi_cost_24m)} {ccy} a year.
          </p>
        ) : null}
        {d.reasons.length ? <p className="mt-1 text-2xs text-faint">{d.reasons.join(' · ')}</p> : null}
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Maturity wall" subtitle={L?.mat_source && L.mat_source !== 'none' ? `Contractual principal repayments tagged in the filing (${L.mat_source === 'us_gaap' ? 'US GAAP' : 'IFRS'}, balance date ${fmtDate(L.mat_period_end)}) · total ${fmtCompact(tot)} ${ccy}` : 'Not tagged in the filings'}>
          {wall.length ? (
            <Bars categories={wall.map((w) => w.label)} series={[{ name: `Maturing (${ccy})`, data: wall.map((w) => w.amount ?? null) }]} format={(v) => fmtCompact(v)} ariaLabel="Debt maturity wall" height={230} />
          ) : (
            <EmptyState title="No maturity schedule" message="The issuer does not tag its contractual maturities (common for IFRS and non-US filers)." />
          )}
          {fin(L?.floating_share) ? (
            <p className="mt-2 text-xs text-muted">
              Fixed {fmtCompact(L?.fixed_debt)} · floating {fmtCompact(L?.floating_debt)} ({fmtPct(L?.floating_share, 0)} floating){fin(L?.weighted_avg_rate) ? ` · reported weighted-average rate ${fmtPct(L?.weighted_avg_rate, 2)}` : ''}
            </p>
          ) : null}
        </Panel>
        <Panel title="Leverage and coverage history" subtitle="Net debt / EBITDA and EBIT interest coverage (x)">
          {hist.length >= 2 ? (
            <TimeSeriesChart
              height={230}
              series={[
                { name: 'Net debt / EBITDA', data: hist.map((h) => ({ date: h.period_end, value: h.net_debt_to_ebitda ?? null })) },
                { name: 'Interest coverage', data: hist.map((h) => ({ date: h.period_end, value: h.interest_coverage ?? null })) },
              ]}
              format={(v) => `${v.toFixed(1)}x`}
              zeroLine
              ariaLabel="Leverage and coverage history"
            />
          ) : (
            <EmptyState title="Not enough periods" />
          )}
        </Panel>
      </div>
      <Panel title="Cost of debt" subtitle="Interest expense / average total debt">
        {hist.length >= 2 ? (
          <TimeSeriesChart height={180} series={[{ name: 'Average cost of debt', data: hist.map((h) => ({ date: h.period_end, value: fin(h.avg_cost_of_debt) ? h.avg_cost_of_debt * 100 : null })) }]} format={(v) => fmtRate(v)} ariaLabel="Average cost of debt history" />
        ) : (
          <EmptyState title="Not enough periods" />
        )}
      </Panel>
      <Panel title="Listed bonds" subtitle="ESMA FIRDS (EU / EEA venues, joined by LEI) and OpenFIGI (US TRACE bonds; coupon / maturity parsed from the FIGI description)" flush>
        {d.bonds.length ? (
          <div className="max-h-[360px] overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-surface text-2xs text-muted">
                <tr className="border-b border-border">
                  <th className="px-3 py-1 text-left font-medium">Bond</th>
                  <th className="text-left font-medium">Id</th>
                  <th className="text-left font-medium">Ccy</th>
                  <th className="text-right font-medium">Coupon</th>
                  <th className="text-left font-medium pl-2">Maturity</th>
                  <th className="text-right font-medium">Issued</th>
                  <th className="px-3 text-left font-medium">Seniority / venue</th>
                </tr>
              </thead>
              <tbody>
                {d.bonds.map((b) => (
                  <tr key={b.bond_id} className="border-b border-border/60">
                    <td className="max-w-80 truncate px-3 py-1">{b.name ?? DASH}</td>
                    <td className="text-muted">{b.isin ?? b.figi}</td>
                    <td>{b.currency ?? DASH}</td>
                    <td className="num text-right">{b.coupon_type === 'floating' ? `${b.floating_reference ?? 'FRN'}${fin(b.floating_spread_bp) ? ` +${b.floating_spread_bp}` : ''}` : b.coupon_type === 'zero' ? 'zero' : fmtRate(b.coupon, 3)}</td>
                    <td className="pl-2">{fmtDate(b.maturity_date)}</td>
                    <td className="num text-right">{fmtCompact(b.issue_amount)}</td>
                    <td className="px-3 text-muted">{[b.seniority, b.venue, b.source].filter(Boolean).join(' · ')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState className="m-3" title="No listed bonds found" message="Not in FIRDS by the issuer's LEI and not found on OpenFIGI (US issuers are searched by the filler campaign us_bonds_openfigi)." />
        )}
      </Panel>
      <p className="text-2xs text-muted">Derived from the issuer&apos;s filings (XBRL / ESEF) — point-in-time by filing date; latest filed version of each period. {d.labels.model_based}</p>
    </div>
  )
}

function Kpi({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-2 py-1.5">
      <div className="label-caps truncate" title={label}>{label}</div>
      <div className="num text-md text-fg">{value}</div>
      {sub ? <div className="truncate text-2xs text-muted">{sub}</div> : null}
    </div>
  )
}
