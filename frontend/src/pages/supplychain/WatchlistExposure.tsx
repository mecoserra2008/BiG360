import { Globe2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import { OVERLAYS, coverageOf, overlayLabel, portfolioMapData } from '@/api/supplychain/adapters'
import { usePortfolioExposure } from '@/api/supplychain/hooks'
import type { MapOverlay, ScPortfolio } from '@/api/supplychain/types'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { countryName } from '@/lib/regions'
import { ExposureMap } from './ExposureMap'

/** Watchlist-level revenue geography (equal-weight aggregation of the members' exposure) with country overlays. */
export function WatchlistExposure({ ids }: { ids: string[] }) {
  const [basis, setBasis] = useState<'direct' | 'look_through'>('direct')
  const [overlay, setOverlay] = useState<MapOverlay>('exposure')
  const sids = useMemo(() => [...ids].sort(), [ids])
  const q = usePortfolioExposure({ security_ids: sids, basis }, sids.length > 0)
  return (
    <Panel
      title={<span className="inline-flex items-center gap-2"><Globe2 className="size-4 text-accent" aria-hidden />Watchlist exposure map</span>}
      subtitle={<span className="text-2xs text-muted">Equal-weight aggregation of revenue geography · overlays are model-based · click a country for its contributors</span>}
      actions={
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<'direct' | 'look_through'> label="Exposure basis" size="xs" value={basis} onChange={setBasis} options={[{ value: 'direct', label: 'Direct' }, { value: 'look_through', label: 'Look-through' }]} />
          <Select<MapOverlay> label="Map layer" value={overlay} onChange={setOverlay} options={OVERLAYS} className="w-40" />
        </div>
      }
      className="col-span-12"
    >
      {!sids.length ? (
        <EmptyState title="Watchlist is empty" message="Add companies to see their aggregated country exposure." className="py-6" />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !q.data ? (
        <Skeleton className="h-[360px]" />
      ) : q.data.data_status === 'pending' ? (
        <PendingState compact detail="The supply-chain jobs have not produced exposure data for this snapshot yet." />
      ) : (
        <Body d={q.data} overlay={overlay} basis={basis} />
      )}
    </Panel>
  )
}

function Body({ d, overlay, basis }: { d: ScPortfolio; overlay: MapOverlay; basis: 'direct' | 'look_through' }) {
  const navigate = useNavigate()
  const [sel, setSel] = useState<string | null>(null)
  const data = useMemo(() => portfolioMapData(d, overlay, basis), [d, overlay, basis])
  const rows = useMemo(() => d.countries.map((c) => ({ iso2: c.iso2, share: basis === 'direct' ? (c.direct ?? null) : (c.look_through ?? null), overlay: c.overlay })), [d.countries, basis])
  const cur = sel ? d.countries.find((c) => c.iso2 === sel) : d.countries[0]
  const cov = d.coverage
  const sparse = cov.n_items - cov.n_geo_revenue
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <div>
        {data.length ? <ExposureMap rows={rows} data={data} overlay={overlay} ariaLabel={`Watchlist exposure map: ${overlayLabel(overlay)}`} onCountry={setSel} /> : <EmptyState title="No values for this layer" className="h-[300px]" />}
        <div className="mt-1 flex flex-wrap items-center gap-2 px-1 text-2xs text-muted">
          <Badge tone={sparse > cov.n_items / 2 ? 'warn' : 'up'}>{fmtInt(cov.n_geo_revenue)} / {fmtInt(cov.n_items)} with geographic revenue</Badge>
          {sparse > 0 && <span>{fmtInt(sparse)} names use the home country only (no geographic disclosure, common outside the US)</span>}
          <span>· {fmtInt(cov.n_with_links)} with disclosed supply-chain links</span>
        </div>
      </div>
      <div className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          <Stat label="Country risk*" value={fmtNum(d.portfolio_country_risk, 0)} sub="exposure-weighted, 0–100" />
          <Stat label="Top country" value={d.countries[0] ? `${d.countries[0].iso2} ${fmtPct(basis === 'direct' ? d.countries[0].direct : d.countries[0].look_through, 0)}` : DASH} sub={d.countries[0] ? countryName(d.countries[0].iso2) : undefined} />
          <Stat label="Countries" value={fmtInt(d.countries.length)} sub={basis === 'direct' ? 'direct' : 'look-through'} />
        </div>
        {cur && (
          <div className="rounded-sm border border-border p-2 text-xs">
            <div className="mb-1 flex items-center gap-2 font-medium"><Flag country={cur.iso2} /> {countryName(cur.iso2)} · {fmtPct(basis === 'direct' ? cur.direct : cur.look_through, 1)} of watchlist revenue</div>
            <ul className="space-y-0.5">
              {cur.contributors.map((c) => (
                <li key={c.security_id} className="flex justify-between gap-2">
                  <button type="button" className="truncate text-left text-accent-text hover:underline" onClick={() => navigate(securityRoute(c.security_id))}>{c.ticker ?? c.name ?? c.security_id}</button>
                  <span className="num text-muted">{fmtPct(c.weight, 1)}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {d.shared_counterparties.length > 0 && (
          <div className="text-xs">
            <div className="mb-1 font-medium">Shared customers / suppliers <span className="text-2xs font-normal text-muted">(disclosed)</span></div>
            <ul className="max-h-40 space-y-0.5 overflow-auto">
              {d.shared_counterparties.slice(0, 12).map((s) => (
                <li key={s.key} className="flex items-center justify-between gap-2">
                  <span className="truncate">{s.name ?? s.key} <Badge tone={s.relation === 'customer' ? 'accent' : s.relation === 'supplier' ? 'info' : 'up'}>{s.relation}</Badge></span>
                  <span className="text-2xs text-muted">{s.holders.join(', ')}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex flex-wrap gap-1 text-2xs">
          {d.items.map((it) => {
            const c = coverageOf(it.coverage_level)
            return <Badge key={it.security_id} tone={c.tone} title={`${c.label}${it.geo_source === 'home_country' ? ' · home country only' : ''}`}>{it.ticker ?? it.security_id}</Badge>
          })}
        </div>
        <p className="text-2xs text-faint">* {d.labels.country_risk}</p>
      </div>
    </div>
  )
}
