import { Star } from 'lucide-react'
import { lazy, Suspense } from 'react'
import { Link, Navigate, useParams, useSearchParams } from 'react-router'
import { isInstrumentId, securityRoute } from '@/lib/routes'
import type { CompanyProfile } from '@/api/types'
import { useCompanyOverview } from '@/api/hooks'
import { ScoreGauge } from '@/components/charts/ScoreGauge'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { DataQualityBadge, Flag, MoatBadge } from '@/components/ui/data'
import { ErrorState, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { fmtCompactCurrency, fmtDate, fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { countryName } from '@/lib/regions'
import { cn } from '@/lib/utils'
import { NewFilingBadge } from '@/components/live/NewFilingBadge'
import { LastPriceBlock } from '@/components/live/LiveQuote'
import { useWatchlist } from '@/store'
import { CreateThesisLink } from '@/pages/theses/shared'
import { OverviewTab } from './tabs/OverviewTab'
import { HelpLink } from '@/components/layout/HelpLink'
import { Avail, AvailabilityScope, Missing } from '@/components/ui/missing'

const FinancialsTab = lazy(() => import('./tabs/FinancialsTab'))
const ForecastTab = lazy(() => import('./tabs/ForecastTab'))
const PeersTab = lazy(() => import('./tabs/PeersTab'))
const MoatTab = lazy(() => import('./tabs/MoatTab'))
const InsidersTab = lazy(() => import('./tabs/InsidersTab'))
const SentimentTab = lazy(() => import('./tabs/SentimentTab'))
const TechnicalsTab = lazy(() => import('./tabs/TechnicalsTab'))
const ModelTab = lazy(() => import('./tabs/ModelTab'))
const MacroTab = lazy(() => import('./tabs/MacroTab'))
const EventsTab = lazy(() => import('./tabs/EventsTab'))
const EarningsTab = lazy(() => import('./tabs/EarningsTab'))
const OwnershipTab = lazy(() => import('./tabs/OwnershipTab'))
const NetworkTab = lazy(() => import('./tabs/NetworkTab'))
const DebtTab = lazy(() => import('./tabs/DebtTab'))

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'financials', label: 'Financials' },
  { id: 'forecast', label: 'Forecast' },
  { id: 'peers', label: 'Peers' },
  { id: 'moat', label: 'Moat' },
  { id: 'debt', label: 'Debt' },
  { id: 'insiders', label: 'Insiders' },
  { id: 'ownership', label: 'Ownership' },
  { id: 'sentiment', label: 'News & Sentiment' },
  { id: 'technicals', label: 'Technicals' },
  { id: 'macro', label: 'Macro' },
  { id: 'earnings', label: 'Earnings' },
  { id: 'events', label: 'Event risk' },
  { id: 'network', label: 'Network & Geo' },
  { id: 'model', label: 'Model' },
] as const
type TabId = (typeof TABS)[number]['id']

export default function Company360() {
  const { id = 'MSFT' } = useParams()
  if (isInstrumentId(id)) return <Navigate to={securityRoute(id)} replace />
  return <Company360Page id={id} />
}

function Company360Page({ id }: { id: string }) {
  const [params, setParams] = useSearchParams()
  const tab = (TABS.some((t) => t.id === params.get('tab')) ? params.get('tab') : 'overview') as TabId
  const q = useCompanyOverview(id)

  if (q.isError) {
    return (
      <ErrorState
        error={q.error}
        onRetry={() => void q.refetch()}
        className="rounded-md border border-border bg-surface"
      />
    )
  }

  return (
    // completeness C: every empty value below explains itself (security-level reasons, GET /companies/{id}/availability)
    <AvailabilityScope securityId={id}>
    <div className="space-y-3">
      {q.data ? <CompanyHeader p={q.data.profile} /> : <Skeleton className="h-[104px]" />}
      <Tabs value={tab} onValueChange={(v) => setParams((p) => { p.set('tab', v); return p }, { replace: true })}>
        {/* 14 sections: wrap onto a second row on narrow screens instead of scrolling tabs off-screen (audit F36) */}
        <TabsList aria-label="Company 360 sections" className="flex-wrap overflow-x-visible">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id} className="px-2">
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <Suspense fallback={<SkeletonRows rows={10} className="pt-3" />}>
          <TabsContent value="overview">{q.data ? <OverviewTab data={q.data} /> : <SkeletonRows rows={10} />}</TabsContent>
          <TabsContent value="financials"><FinancialsTab id={id} /></TabsContent>
          <TabsContent value="forecast"><ForecastTab id={id} /></TabsContent>
          <TabsContent value="peers"><PeersTab id={id} /></TabsContent>
          <TabsContent value="moat"><MoatTab id={id} /></TabsContent>
          <TabsContent value="debt"><DebtTab id={id} /></TabsContent>
          <TabsContent value="insiders"><InsidersTab id={id} /></TabsContent>
          <TabsContent value="ownership"><OwnershipTab id={id} /></TabsContent>
          <TabsContent value="sentiment"><SentimentTab id={id} /></TabsContent>
          <TabsContent value="technicals"><TechnicalsTab id={id} /></TabsContent>
          <TabsContent value="macro"><MacroTab id={id} /></TabsContent>
          <TabsContent value="earnings"><EarningsTab id={id} /></TabsContent>
          <TabsContent value="events"><EventsTab id={id} /></TabsContent>
          <TabsContent value="network"><NetworkTab id={id} /></TabsContent>
          <TabsContent value="model"><ModelTab id={id} /></TabsContent>
        </Suspense>
      </Tabs>
    </div>
    </AvailabilityScope>
  )
}

function CompanyHeader({ p }: { p: CompanyProfile }) {
  const on = useWatchlist((s) => s.ids.includes(p.id))
  const toggle = useWatchlist((s) => s.toggle)
  const rangePos = p.high52w > p.low52w ? ((p.price - p.low52w) / (p.high52w - p.low52w)) * 100 : 50
  return (
    <section className="flex flex-wrap items-center gap-x-6 gap-y-3 rounded-md border border-border bg-surface px-4 py-3" aria-label="Company summary">
      <div className="min-w-[260px] flex-1">
        <div className="flex items-center gap-2">
          <Flag country={p.country} className="h-3.5 w-[21px]" />
          <h1 className="truncate text-xl font-semibold tracking-tight">{p.name}</h1>
          <Button
            size="icon"
            variant="ghost"
            aria-pressed={on}
            aria-label={on ? 'Remove from watchlist' : 'Add to watchlist'}
            onClick={() => toggle(p.id)}
          >
            <Star className={cn(on && 'fill-accent text-accent')} />
          </Button>
          <CreateThesisLink link={{ subjectType: 'security', subjectId: p.id, origin: 'company', ref: p.ticker }} size="xs" variant="ghost" className="ml-1" title="Create a thesis on this company (thesis tracker)" />
          <HelpLink className="ml-0.5" />
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
          <span className="num font-semibold text-fg">{p.ticker}</span>
          <span>·</span>
          <span>{p.exchange}</span>
          <span>·</span>
          <span>{countryName(p.country)}</span>
          <span>·</span>
          {p.isin && (
            <>
              <span className="num">{p.isin}</span>
              <span>·</span>
            </>
          )}
          <Link to={`/sectors?sector=${encodeURIComponent(sectorIdFor(p.sector))}`} className="hover:text-fg hover:underline">
            {p.sector}
          </Link>
          <span aria-hidden>›</span>
          <span className="text-fg">{p.subsector}</span>
        </div>
        {p.isAdr && (
          <div className="mt-2 flex flex-wrap items-center gap-2 rounded-md border border-info/40 bg-info-soft px-2 py-1 text-xs text-info">
            <Badge tone="info">ADR</Badge>
            <span>Composite, technicals and fair value are computed on the home listing by design.</span>
            {p.homeListing ? (
              <Link to={`/company/${encodeURIComponent(p.homeListing.id)}`} className="font-semibold underline-offset-2 hover:underline">
                See home listing → {p.homeListing.ticker}
                {p.homeListing.exchange ? ` (${p.homeListing.exchange})` : ''}
              </Link>
            ) : (
              <span className="text-muted">Home listing not found in the universe.</span>
            )}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          <MoatBadge rating={p.moat} />
          <DataQualityBadge quality={p.dataQuality} notes={p.dataQualityNotes} />
          <Badge>{p.reportingCurrency} reporting</Badge>
          <Badge>FYE {p.fiscalYearEnd}</Badge>
          {p.lastFilingDate && <Badge>Last filing {fmtDate(p.lastFilingDate)}</Badge>}
          <NewFilingBadge id={p.id} ticker={p.ticker} />
        </div>
      </div>

      <div className="flex items-end gap-6">
        {/* workstream B: delayed live quote when available (labelled "delayed · hh:mm"), else the official close */}
        <LastPriceBlock id={p.id} currency={p.currency} close={p.price} closeChange={p.change1dPct} />
        <div className="space-y-1">
          <div>
            <div className="label-caps">Mkt cap</div>
            <div className="num text-md"><Avail value={p.marketCapUsd} field="mkt_cap_usd">{fmtCompactCurrency(p.marketCapUsd)}</Avail></div>
          </div>
          <div>
            <div className="label-caps">EV</div>
            <div className="num text-sm text-muted"><Avail value={p.enterpriseValueUsd} field="ev_usd">{fmtCompactCurrency(p.enterpriseValueUsd)}</Avail></div>
          </div>
        </div>
        <div className="w-36">
          <div className="label-caps">52-week range</div>
          <div className="relative mt-2 h-1 rounded-full bg-surface-3" role="img" aria-label={`Price at ${Math.round(rangePos)}% of 52-week range`}>
            <span className="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-surface bg-accent" style={{ left: `${rangePos}%` }} />
          </div>
          <div className="num mt-1.5 flex justify-between text-2xs text-muted">
            <span>{fmtPrice(p.low52w, p.currency)}</span>
            <span>{fmtPrice(p.high52w, p.currency)}</span>
          </div>
          <div className="mt-0.5 truncate text-2xs text-faint" title="Beta · average daily traded value · distance from 52-week high">
            β <Avail value={p.beta} field="beta_1y">{fmtNum(p.beta, 2)}</Avail> · ADV {fmtCompactCurrency(p.avgDailyValueUsd, 'USD', 0)} · {fmtPct(p.price / p.high52w - 1, 0, true)}
          </div>
        </div>
        <div className="flex flex-col items-center gap-1">
          {Number.isFinite(p.compositeScore) || !p.compositeReason ? (
            <ScoreGauge value={p.compositeScore} />
          ) : (
            <div className="flex h-[92px] w-[92px] flex-col items-center justify-center rounded-full border border-dashed border-border-strong text-center" data-testid="composite-missing">
              <div className="label-caps">Composite</div>
              <Missing field="composite" reason={p.compositeReason} />
            </div>
          )}
          {Number.isFinite(p.compositeScore) && <CompositeLabel validated={p.compositeValidated === true} />}
          {p.scoreScope === 'extended' && (
            <span
              className="rounded-sm bg-surface-3 px-1.5 py-0.5 text-2xs text-muted"
              title={`Outside the MVP model universe: every percentile is the position this company would take among the MVP universe of its peer group (scored against the MVP reference cross-section, MVP results unchanged); grade = MVP quintile.${p.rankSectorFull ? ` Rank ${p.rankSectorFull} of ${p.nSectorFull ?? '?'} scored companies in its region x sector.` : ''}`}
              data-testid="score-scope-extended"
            >
              Extended scope
            </span>
          )}
        </div>
      </div>
    </section>
  )
}

const SECTOR_IDS: Record<string, string> = {
  'Information Technology': 'tech',
  Financials: 'financials',
  'Health Care': 'health',
  'Consumer Discretionary': 'cons_disc',
  'Consumer Staples': 'cons_staples',
  Industrials: 'industrials',
  Energy: 'energy',
  Materials: 'materials',
  'Communication Services': 'comms',
  Utilities: 'utilities',
  'Real Estate': 'real_estate',
}
function sectorIdFor(name: string): string {
  return SECTOR_IDS[name] ?? 'tech'
}
