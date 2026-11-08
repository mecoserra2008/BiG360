import type { ReactNode } from 'react'
import { Link, useSearchParams } from 'react-router'
import { euChange } from '@/api/smartmoney/adapters'
import { useSmartMoneyOverview } from '@/api/smartmoney/hooks'
import type { SmOverview, SmRow } from '@/api/smartmoney/types'
import { Badge } from '@/components/ui/badge'
import { PercentileBar } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate, fmtInt, fmtNum, fmtPct, isNum } from '@/lib/format'
import { Missing } from '@/components/ui/missing'
import { CompanyLink, EventBadge, LagNote, ModelBasedTag, OfficialTag, Signed, SmartMoneyNav, SourcesFooter } from './shared'

const REGION_OPTS = ['', 'US', 'EU', 'UK', 'JP', 'KR', 'CN', 'HK', 'LATAM', 'APAC'].map((r) => ({ value: r, label: r || 'All regions' }))

export default function SmartMoney() {
  const [sp, setSp] = useSearchParams()
  const region = sp.get('region') ?? ''
  const q = useSmartMoneyOverview(region)
  return (
    <>
      <PageHeader title="Smart money" subtitle="Who is buying, selling, shorting and crowding - official 13F, FINRA, SEC and EU / UK regulator disclosures">
        <Select label="Region" value={region} onChange={(v) => setSp((p) => { if (v) p.set('region', v); else p.delete('region'); return p }, { replace: true })} options={REGION_OPTS} />
      </PageHeader>
      <SmartMoneyNav />
      {q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <Skeleton className="h-96" /> : q.data.data_status === 'pending' ? <PendingState detail="The smart-money jobs have not produced data for this snapshot yet." /> : <Body d={q.data} />}
    </>
  )
}

type Col = { h: string; title?: string; right?: boolean; cell: (r: SmRow) => ReactNode }

function RowTable({ rows, cols, label }: { rows: SmRow[]; cols: Col[]; label: string }) {
  if (!rows.length) return <EmptyState title="Nothing to show" className="py-6" />
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs" aria-label={label}>
        <thead className="text-left text-muted">
          <tr className="border-b border-border">
            <th scope="col" className="px-2 py-1.5 font-medium">Company</th>
            {cols.map((c) => (
              <th key={c.h} scope="col" title={c.title} className={`px-2 py-1.5 font-medium ${c.right !== false ? 'text-right' : ''}`}>{c.h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.security.security_id} className="border-b border-border/60">
              <td className="max-w-[220px] px-2 py-1"><CompanyLink ticker={r.security.ticker} name={r.security.name} id={r.security.security_id} className="flex truncate" /></td>
              {cols.map((c) => (
                <td key={c.h} className={`px-2 py-1 ${c.right !== false ? 'num text-right' : ''}`}>{c.cell(r)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

const pct1 = (v: number | null | undefined) => fmtPct(v, 1)
/** completeness C: an empty cell explains itself (field reason in the security's region) */
const nv = (r: SmRow, v: number | null | undefined, field: string, render: (x: number) => ReactNode): ReactNode =>
  isNum(v) ? render(v) : <Missing field={field} region={r.security?.region} />
const COLS_FLOW: Col[] = [
  { h: 'Inst. %', title: '13F long shares / shares outstanding', cell: (r) => nv(r, r.inst_own_pct, 'own.inst_own_pct', pct1) },
  { h: 'Δ q/q', title: 'Change in institutional ownership vs the previous quarter (percentage points)', cell: (r) => nv(r, r.inst_own_chg, 'own.inst_own_chg', (x) => <Signed v={x} pp />) },
  { h: 'Net flow', title: '13F net share flow / shares outstanding', cell: (r) => nv(r, r.flow_pct_out, 'own.flow_pct_out', (x) => <Signed v={x} digits={2} />) },
  { h: 'New / exited', cell: (r) => `${fmtInt(r.n_new)} / ${fmtInt(r.n_exited)}` },
  { h: 'Holders', cell: (r) => fmtInt(r.n_holders) },
]
const COLS_SMART: Col[] = [
  { h: 'Buyers', title: 'Smart-cohort filers that opened or added (model-based)', cell: (r) => fmtInt(r.smart_buyers) },
  { h: 'Sellers', title: 'Smart-cohort filers that trimmed or exited (model-based)', cell: (r) => fmtInt(r.smart_sellers) },
  { h: 'Cohort flow', cell: (r) => nv(r, r.smart_flow_pct_out, 'own.smart_flow_pct_out', (x) => <Signed v={x} digits={2} />) },
  { h: 'Inst. %', cell: (r) => nv(r, r.inst_own_pct, 'own.inst_own_pct', pct1) },
]
const COLS_CROWD: Col[] = [
  { h: 'HF top-20', title: 'Hedge-fund-like filers holding it among their top-20 positions (model-based)', cell: (r) => fmtInt(r.hf_top20_count) },
  { h: 'Crowding', cell: (r) => <PercentileBar value={r.crowding_pct != null ? r.crowding_pct * 100 : null} /> },
  { h: 'Inst. %', cell: (r) => nv(r, r.inst_own_pct, 'own.inst_own_pct', pct1) },
]
const COLS_SHORT: Col[] = [
  { h: 'SI % out', title: 'Short interest / shares outstanding (float proxy)', cell: (r) => nv(r, r.si_pct_out, 'si.si_pct_out', pct1) },
  { h: 'Days to cover', cell: (r) => nv(r, r.days_to_cover, 'si.days_to_cover', (x) => fmtNum(x, 1)) },
  { h: 'Δ SI', title: 'Short interest change vs the previous settlement', cell: (r) => nv(r, r.si_chg_pct, 'si.si_chg_pct', (x) => <Signed v={x} invert digits={0} />) },
  { h: 'z 1y', cell: (r) => nv(r, r.si_z_1y, 'si.si_z_1y', (x) => fmtNum(x, 1, true)) },
]
const COLS_SQUEEZE: Col[] = [
  { h: 'SI % out', cell: (r) => nv(r, r.si_pct_out, 'si.si_pct_out', pct1) },
  { h: 'DTC', title: 'Days to cover', cell: (r) => nv(r, r.days_to_cover, 'si.days_to_cover', (x) => fmtNum(x, 1)) },
  { h: '20d ret.', cell: (r) => nv(r, r.ret_20d, 'si.ret_20d', (x) => <Signed v={x} digits={0} />) },
  { h: 'Risk', title: 'Squeeze-risk score (model-based)', cell: (r) => <PercentileBar value={r.squeeze_score != null ? r.squeeze_score * 100 : null} /> },
  { h: 'Flag', cell: (r) => (r.squeeze_flag ? <Badge tone="warn">squeeze risk</Badge> : <span className="text-faint">{DASH}</span>) },
]
const COLS_FTD: Col[] = [
  { h: 'FTD % out', title: 'Largest daily fails-to-deliver / shares outstanding', cell: (r) => nv(r, r.ftd_pct_out, 'si.ftd_pct_out', (x) => fmtPct(x, 2)) },
  { h: 'SI % out', cell: (r) => nv(r, r.si_pct_out, 'si.si_pct_out', pct1) },
]
const COLS_SCORE: Col[] = [
  { h: 'Score', title: 'Combined smart-money score (model-based z)', cell: (r) => nv(r, r.score, 'sm.score', (x) => fmtNum(x, 2, true)) },
  { h: 'Pctl', cell: (r) => <PercentileBar value={r.score_pct} /> },
  { h: 'SI % out', cell: (r) => nv(r, r.si_pct_out, 'si.si_pct_out', pct1) },
  { h: 'EU short', title: 'Disclosed EU / UK net short % of capital', cell: (r) => (r.eu_short_pct ? `${fmtNum(r.eu_short_pct, 2)}%` : <Missing field="sm.eu_short_pct" region={r.security?.region} />) },
]

function Body({ d }: { d: SmOverview }) {
  const L = d.labels as Record<string, string>
  return (
    <div className="space-y-3">
      <Panel title="Snapshot" subtitle={<span className="inline-flex flex-wrap items-center gap-3">As of {fmtDate(d.as_of)} <LagNote lag={d.lag} /></span>}>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <Stat label="13F quarter" value={d.lag ? fmtDate(d.lag.period_end) : DASH} sub={d.lag ? `filed by ${fmtDate(d.lag.cutoff)}` : undefined} />
          <Stat label="Securities with 13F data" value={fmtInt(d.lag?.n_securities)} />
          <Stat label="FINRA settlement" value={fmtDate(d.si_settlement_date)} sub="short interest" />
          <Stat label="EU / UK short positions" value={fmtInt(d.eu_register.n_positions)} sub={`${fmtInt(d.eu_register.n_issuers)} issuers · ${fmtInt(d.eu_register.n_holders)} holders`} />
          <Stat label="Activist 13D (60d)" value={fmtInt(d.activist_count_60d ?? d.activist_feed.length)} sub="new 13D / 13G → 13D" />
          <Stat label="Score date" value={fmtDate(d.score_date)} sub="combined (model-based)" />
        </div>
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Top accumulation" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="SEC 13F" /><LagNote lag={d.lag} /></span>} flush>
          <RowTable rows={d.accumulation} cols={COLS_FLOW} label="Top 13F accumulation" />
        </Panel>
        <Panel title="Top distribution" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="SEC 13F" /><LagNote lag={d.lag} /></span>} flush>
          <RowTable rows={d.distribution} cols={COLS_FLOW} label="Top 13F distribution" />
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Smart-cohort buying" subtitle={<ModelBasedTag tip={L.smart_cohort}>smart cohort selected on past 13F performance</ModelBasedTag>} flush>
          <RowTable rows={d.smart_buying} cols={COLS_SMART} label="Smart-cohort buying" />
        </Panel>
        <Panel title="Most crowded (hedge-fund-like)" subtitle={<ModelBasedTag tip={`${L.crowding}. ${L.hedge_fund_like}`}>top-holder overlap</ModelBasedTag>} flush>
          <RowTable rows={d.crowded} cols={COLS_CROWD} label="Most crowded" />
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Most shorted" subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="FINRA short interest" /><span className="text-2xs text-muted">{L.si_float}</span></span>} flush>
          <RowTable rows={d.most_shorted} cols={COLS_SHORT} label="Most shorted" />
        </Panel>
        <Panel title="Squeeze watch" subtitle={<ModelBasedTag tip={L.squeeze}>high SI + rising price + low float</ModelBasedTag>} flush>
          <RowTable rows={d.squeeze_watch} cols={COLS_SQUEEZE} label="Squeeze watch" />
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-3">
        <Panel title="Short-interest risers" subtitle={<OfficialTag label="FINRA" />} flush>
          <RowTable rows={d.si_risers} cols={COLS_SHORT.slice(0, 3)} label="Short interest risers" />
        </Panel>
        <Panel title="Fails-to-deliver spikes" subtitle={<ModelBasedTag tip={L.ftd_spike}>SEC FTD data</ModelBasedTag>} flush>
          <RowTable rows={d.ftd_spikes} cols={COLS_FTD} label="Fails-to-deliver spikes" />
        </Panel>
        <Panel title="Futures positioning (CFTC)" subtitle={<span className="inline-flex items-center gap-2"><OfficialTag label="CFTC COT" /><Link to="/macro/positioning" className="text-2xs text-accent-text hover:underline">all markets →</Link></span>} flush>
          {d.futures_positioning.length ? (
            <table className="w-full text-xs" aria-label="Equity index futures positioning">
              <thead className="text-left text-muted">
                <tr className="border-b border-border"><th className="px-2 py-1.5 font-medium">Market</th><th className="px-2 py-1.5 text-right font-medium">Net % OI</th><th className="px-2 py-1.5 text-right font-medium">COT 3y</th><th className="px-2 py-1.5 text-right font-medium">Report</th></tr>
              </thead>
              <tbody>
                {d.futures_positioning.map((f) => (
                  <tr key={f.market_code} className="border-b border-border/60">
                    <td className="px-2 py-1"><Link to={`/macro/positioning/${f.market_code}`} className="hover:underline">{f.name}</Link><div className="text-2xs text-faint">{f.category}</div></td>
                    <td className="num px-2 py-1 text-right"><Signed v={f.net_pct_oi} /></td>
                    <td className="px-2 py-1 text-right"><PercentileBar value={f.cot_index_3y != null ? f.cot_index_3y * 100 : null} /></td>
                    <td className="num px-2 py-1 text-right text-muted">{fmtDate(f.report_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState title="No CFTC data" className="py-6" />
          )}
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Combined score - top" subtitle={<ModelBasedTag tip={L.score}>13F flows · smart cohort · insiders · short interest · EU shorts</ModelBasedTag>} flush>
          <RowTable rows={d.top_score} cols={COLS_SCORE} label="Highest combined smart-money score" />
        </Panel>
        <Panel title="Combined score - bottom" subtitle={<ModelBasedTag tip={L.score}>lowest scores</ModelBasedTag>} flush>
          <RowTable rows={d.bottom_score} cols={COLS_SCORE} label="Lowest combined smart-money score" />
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Activist feed" subtitle={<span className="inline-flex items-center gap-2"><OfficialTag label="SEC 13D / 13G" /><Link to="/smart-money/activism" className="text-2xs text-accent-text hover:underline">full feed →</Link></span>} flush>
          {d.activist_feed.length ? (
            <ul className="divide-y divide-border/60 text-xs">
              {d.activist_feed.slice(0, 15).map((e) => (
                <li key={e.filing_id} className="flex min-w-0 items-center gap-2 px-2 py-1.5">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(e.event_date)}</span>
                  <EventBadge type={e.event_type} />
                  <span className="min-w-0 flex-1 truncate">
                    <span className="font-medium text-fg">{e.filer_name ?? 'Unknown filer'}</span>
                    <span className="text-muted"> → </span>
                    {e.security ? <CompanyLink ticker={e.security.ticker} name={e.security.name} id={e.security.security_id} /> : <span className="text-muted">{e.subject_name}</span>}
                  </span>
                  <a href={e.url} target="_blank" rel="noreferrer" className="shrink-0 text-2xs text-accent-text hover:underline">EDGAR</a>
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState title="No new 13D filings in the last 60 days" className="py-6" />
          )}
        </Panel>
        <Panel title="EU / UK short-seller register" subtitle={<span className="inline-flex items-center gap-2"><OfficialTag label="FCA · AMF · CNMV · AFM · CBI · FSMA · FI · Finanstilsynet" /><Link to="/smart-money/eu-shorts" className="text-2xs text-accent-text hover:underline">register →</Link></span>} flush>
          {d.eu_register.latest.length ? (
            <table className="w-full text-xs" aria-label="Latest EU and UK net short positions">
              <thead className="text-left text-muted">
                <tr className="border-b border-border"><th className="px-2 py-1.5 font-medium">Holder</th><th className="px-2 py-1.5 font-medium">Issuer</th><th className="px-2 py-1.5 text-right font-medium">Net short</th><th className="px-2 py-1.5 text-right font-medium">Change</th><th className="px-2 py-1.5 text-right font-medium">Date</th></tr>
              </thead>
              <tbody>
                {d.eu_register.latest.map((r, i) => (
                  <tr key={i} className="border-b border-border/60">
                    <td className="max-w-[180px] truncate px-2 py-1" title={r.holder_name}>{r.holder_name}</td>
                    <td className="max-w-[180px] truncate px-2 py-1">{r.ticker && r.security_id ? <CompanyLink ticker={r.ticker} id={r.security_id} /> : <span title={r.isin}>{r.issuer_name}</span>} <span className="text-2xs text-faint">{r.regulator_label}</span></td>
                    <td className="num px-2 py-1 text-right">{fmtNum(r.position_pct, 2)}%</td>
                    <td className="num px-2 py-1 text-right">{euChange(r) == null ? <Badge tone="info">new</Badge> : <Signed v={(euChange(r) ?? 0) / 100} invert digits={2} pp />}</td>
                    <td className="num px-2 py-1 text-right text-muted">{fmtDate(r.position_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState title="No current positions" className="py-6" />
          )}
        </Panel>
      </div>
      <SourcesFooter sources={d.sources} />
      <p className="text-2xs text-faint">{L.model_based}.</p>
    </div>
  )
}
