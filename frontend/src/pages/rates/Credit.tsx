import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { barPct, fmtBp, fmtRate, groupCredit, thinPoints, wallBars } from '@/api/rates/adapters'
import { useCreditOverview, useCreditWalls } from '@/api/rates/hooks'
import { CREDIT_GROUP_LABEL, type CreditOverview, type CreditSeries } from '@/api/rates/types'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { Badge } from '@/components/ui/badge'
import { EmptyState, QueryView, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtCompact, fmtMultiple, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroNav } from '../macro/shared'
import { Bars } from './charts'

const YEARS = { '1Y': 1, '3Y': 3, '5Y': 5, '10Y': 10, Max: 40 } as const
type Yr = keyof typeof YEARS
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pctFmt = (v: number) => fmtRate(v)
const GROUPS = ['rating', 'maturity', 'sector', 'region', 'yield', 'moodys'] as const

/** Macro → Credit: ICE BofA OAS by rating / maturity / EM sector and region, effective yields, Moody's, issuance, TRACE, maturity walls. */
export default function Credit() {
  const [params, setParams] = useSearchParams()
  const yr = (params.get('range') as Yr) in YEARS ? (params.get('range') as Yr) : '5Y'
  const q = useCreditOverview(YEARS[yr])
  return (
    <>
      <PageHeader
        title="Credit"
        subtitle="Corporate spreads by rating, maturity and sector, Moody's yields, bond issuance, FINRA TRACE aggregates and maturity walls derived from company filings"
        actions={<Segmented label="History" size="xs" value={yr} onChange={(v) => setParams((p) => { p.set('range', v); return p }, { replace: true })} options={Object.keys(YEARS).map((k) => ({ value: k as Yr, label: k }))} />}
      />
      <MacroNav />
      <div className="space-y-3">
        <QueryView query={q} skeleton={<SkeletonRows rows={12} className="rounded-md border border-border bg-surface p-3" />}>
          {(d) => <Body d={d} />}
        </QueryView>
        <WallsPanel />
        <p className="text-2xs text-muted">
          ICE BofA option-adjusted spreads and effective yields: ICE Data Indices via FRED — copyrighted, personal use only (FRED keeps 3 years; the app archives every value it has seen). Moody&apos;s seasoned
          Aaa / Baa yields via FRED (Moody&apos;s copyright, personal use). US sector OAS are not published on FRED (EM financial / non-financial shown). Issuance: ESMA FIRDS (EU / EEA venues, first trading date, all
          debt instruments except structured products) and US Treasury Fiscal Data. TRACE aggregates need a FINRA API key; the FINRA fixed-income website forbids automated use. Maturity walls: contractual
          maturities tagged in the filings (US GAAP / IFRS); the refinancing cost is model-based.
        </p>
      </div>
    </>
  )
}

function Body({ d }: { d: CreditOverview }) {
  const g = useMemo(() => groupCredit(d.series), [d.series])
  const [grp, setGrp] = useState<(typeof GROUPS)[number]>('rating')
  const rows = g[grp] ?? []
  return (
    <>
      <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <Panel
          title="Spreads and yields"
          subtitle="Latest value and changes (bp)"
          actions={<Segmented label="Group" size="xs" value={grp} onChange={setGrp} options={GROUPS.filter((x) => g[x]?.length).map((x) => ({ value: x, label: CREDIT_GROUP_LABEL[x] }))} />}
          flush
        >
          {rows.length ? <SeriesTable rows={rows} /> : <EmptyState className="m-3" title="No credit series yet" message="The ICE BofA / Moody's series arrive with job macro_fred." />}
        </Panel>
        <Panel title={`${CREDIT_GROUP_LABEL[grp]} — history`} subtitle="Weekly, percent">
          {rows.length ? (
            <TimeSeriesChart
              height={300}
              series={rows.slice(0, 6).map((s) => ({ name: s.label, data: thinPoints(s.history, 800) }))}
              format={pctFmt}
              ariaLabel={`${CREDIT_GROUP_LABEL[grp]} history`}
            />
          ) : (
            <EmptyState />
          )}
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <IssuanceEu d={d} />
        <IssuanceUs d={d} />
      </div>
      <TracePanel d={d} />
    </>
  )
}

function SeriesTable({ rows }: { rows: CreditSeries[] }) {
  return (
    <table className="w-full text-xs">
      <thead className="text-2xs text-muted">
        <tr className="border-b border-border">
          <th className="px-3 py-1.5 text-left font-medium">Series</th>
          <th className="text-right font-medium">Latest</th>
          <th className="text-right font-medium">1w</th>
          <th className="text-right font-medium">1m</th>
          <th className="px-3 text-right font-medium">1y</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.series_id} className="border-b border-border/60">
            <td className="px-3 py-1.5">
              <span className="font-medium text-fg">{s.label}</span> <span className="text-faint">{s.series_id.replace('fred:', '')}</span>
            </td>
            <td className="num text-right">{fmtRate(s.value)}</td>
            {[s.chg_1w_bp, s.chg_1m_bp, s.chg_1y_bp].map((v, i) => (
              <td key={i} className={cn('num text-right', i === 2 && 'px-3', fin(v) && v > 0 ? 'text-down' : fin(v) && v < 0 ? 'text-up' : 'text-muted')}>{fmtBp(v)}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function IssuanceEu({ d }: { d: CreditOverview }) {
  const [ccy, setCcy] = useState<'EUR' | 'USD' | 'GBP'>('EUR')
  const rows = d.issuance_eu_firds.filter((r) => r.currency === ccy)
  return (
    <Panel title="Bond issuance on EU / EEA venues (FIRDS)" subtitle="Instruments by first trading month; amounts = total issued nominal" actions={<Segmented label="Currency" size="xs" value={ccy} onChange={setCcy} options={(['EUR', 'USD', 'GBP'] as const).map((c) => ({ value: c, label: c }))} />}>
      {rows.length ? (
        <Bars categories={rows.map((r) => r.month.slice(0, 7))} series={[{ name: `Issued (${ccy})`, data: rows.map((r) => r.amount ?? null) }]} format={(v) => fmtCompact(v)} ariaLabel="FIRDS issuance by month" height={230} />
      ) : (
        <EmptyState title="No FIRDS data yet" message="Weekly job bonds_reference (ESMA FIRDS FULINS_D)." />
      )}
    </Panel>
  )
}

function IssuanceUs({ d }: { d: CreditOverview }) {
  const months = [...new Set(d.issuance_us_treasury.map((r) => r.month))].sort()
  const types = ['Bill', 'CMB', 'Note', 'Bond', 'TIPS', 'FRN'].filter((t) => d.issuance_us_treasury.some((r) => r.security_type === t))
  const val = (m: string, t: string) => d.issuance_us_treasury.find((r) => r.month === m && r.security_type === t)?.amount ?? null
  return (
    <Panel title="US Treasury gross issuance" subtitle="Accepted at auction, by month and security type (Fiscal Data)">
      {months.length ? (
        <Bars categories={months.map((m) => m.slice(0, 7))} series={types.map((t) => ({ name: t, data: months.map((m) => val(m, t)) }))} stacked format={(v) => fmtCompact(v)} ariaLabel="US Treasury issuance by month" height={230} />
      ) : (
        <EmptyState title="No auction data yet" message="Job rates_auctions (US Treasury Fiscal Data)." />
      )}
    </Panel>
  )
}

function TracePanel({ d }: { d: CreditOverview }) {
  if (d.trace_status !== 'ok')
    return (
      <Panel title="FINRA TRACE market aggregates">
        <p className="text-xs text-muted">
          <Badge tone="warn">needs API key</Badge> Set FINRA_API_CLIENT_ID / FINRA_API_CLIENT_SECRET (free FINRA API account) and run job credit_trace. The FINRA Fixed Income Data website is not used: its terms forbid robots and automated copying.
        </p>
      </Panel>
    )
  return (
    <Panel title="FINRA TRACE market aggregates" subtitle="Latest values per dataset / category" flush>
      <table className="w-full text-xs">
        <tbody>
          {d.trace.slice(0, 60).map((r) => (
            <tr key={`${r.dataset}${r.category}${r.metric}`} className="border-b border-border/60">
              <td className="px-3 py-1 text-muted">{r.dataset}</td>
              <td>{r.category}</td>
              <td>{r.metric}</td>
              <td className="num px-3 text-right">{fin(r.value) ? r.value.toLocaleString() : DASH}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Panel>
  )
}

function WallsPanel() {
  const [region, setRegion] = useState('ALL')
  const [sector, setSector] = useState('ALL')
  const q = useCreditWalls(region, sector)
  return (
    <Panel
      title="Maturity walls and refinancing risk"
      subtitle="Contractual debt maturities tagged in company filings (USD), latest profile per issuer · refinancing cost = MODEL-BASED (local 5y govt + IG / HY OAS proxy vs average cost of debt)"
      actions={
        q.data ? (
          <>
            <Select label="Region" value={region} onChange={setRegion} options={(q.data.regions.length ? q.data.regions : ['ALL']).map((r) => ({ value: r, label: r === 'ALL' ? 'All regions' : r }))} />
            <Select label="Sector" value={sector} onChange={setSector} options={[{ value: 'ALL', label: 'All sectors' }, ...q.data.sectors.map((s) => ({ value: s.sector_id, label: s.sector_id }))]} />
          </>
        ) : null
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-[300px]" />}>
        {(d) => {
          const bars = wallBars(d.wall)
          const tot = d.wall.find((w) => w.bucket === 'total')
          if (!bars.length) return <EmptyState title="No maturity profiles yet" message="Job company_debt (needs fundamentals and the SEC companyfacts debt tags)." />
          return (
            <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
              <div>
                <Bars categories={bars.map((b) => b.label)} series={[{ name: 'Maturing (USD)', data: bars.map((b) => b.amount_usd ?? null) }]} format={(v) => fmtCompact(v)} ariaLabel="Maturity wall" height={230} />
                {tot ? (
                  <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                    <Kpi label="Issuers with a profile" value={`${tot.n_issuers} / ${tot.n_issuers_debt ?? DASH}`} />
                    <Kpi label="Debt-weighted cost" value={fmtPct(tot.wavg_cost_of_debt, 2)} />
                    <Kpi label="Due within 24m" value={fmtPct(tot.refi_share_24m, 1)} />
                    <Kpi label="Median coverage" value={fmtMultiple(tot.median_interest_coverage)} />
                    <Kpi label="Median net debt / EBITDA" value={fmtMultiple(tot.median_net_debt_to_ebitda)} />
                    <Kpi label="Refi cost (model)" value={fmtCompact(tot.refi_cost_24m_usd)} />
                  </div>
                ) : null}
              </div>
              <div className="max-h-[330px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface text-2xs text-muted">
                    <tr className="border-b border-border">
                      <th className="py-1 text-left font-medium">Sector</th>
                      <th className="text-right font-medium">Debt</th>
                      <th className="text-right font-medium">Due 24m</th>
                      <th className="text-right font-medium">Cost</th>
                      <th className="text-right font-medium">Coverage</th>
                      <th className="text-right font-medium">ND/EBITDA</th>
                      <th className="text-right font-medium">Floating</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.sectors.map((s) => {
                      const mx = Math.max(...d.sectors.map((x) => x.refi_share_24m ?? 0))
                      return (
                        <tr key={s.sector_id} onClick={() => setSector(s.sector_id)} className={cn('cursor-pointer border-b border-border/60 hover:bg-surface-2', sector === s.sector_id && 'bg-surface-3')}>
                          <td className="py-1">{s.sector_id}</td>
                          <td className="num text-right">{fmtCompact(s.total_debt_usd)}</td>
                          <td className="num text-right">
                            <span className="inline-flex items-center gap-1">
                              <span className="h-1.5 w-10 rounded bg-surface-3"><span className="block h-1.5 rounded bg-accent" style={{ width: `${barPct(s.refi_share_24m, mx)}%` }} /></span>
                              {fmtPct(s.refi_share_24m, 0)}
                            </span>
                          </td>
                          <td className="num text-right">{fmtPct(s.wavg_cost_of_debt, 2)}</td>
                          <td className="num text-right">{fmtMultiple(s.median_interest_coverage)}</td>
                          <td className="num text-right">{fmtMultiple(s.median_net_debt_to_ebitda)}</td>
                          <td className="num text-right">{fmtPct(s.floating_share, 0)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )
        }}
      </QueryView>
    </Panel>
  )
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-2 py-1">
      <div className="label-caps truncate">{label}</div>
      <div className="num text-sm text-fg">{value}</div>
    </div>
  )
}
