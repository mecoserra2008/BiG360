import type { MarketOverview, RegionFilter } from '@/api/types'
import { useMarketOverview } from '@/api/hooks'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { PageHeader } from '@/components/ui/panel'
import { useUi } from '@/store'
import { MacroNav } from '../macro/shared'
import { BreadthPanel, IndexBoard, MoversPanel } from './overview/Equities'
import { DataAsOfStrip, RegimeChip, RiskChip } from './overview/Header'
import { isoDaysAgo } from './overview/helpers'
import { CreditPanel, MacroTiles } from './overview/MacroPanels'
import { PolicyPathPanel, RatesPanel } from './overview/Rates'
import { SectorPanel } from './overview/Sectors'
import { CrossAssetTape } from './overview/Tape'

/**
 * Macro › Markets › Overview — a portfolio manager's morning view: what moved (index board, movers),
 * how broad (breadth, sectors), risk on/off and the cross-asset tape, rates / credit / policy pricing,
 * and how fresh each region's data is. Uses existing endpoints only.
 */
export default function MarketMacro({ embedded = false }: { embedded?: boolean }) {
  const region = useUi((s) => s.region)
  const q = useMarketOverview(region)
  return (
    <>
      {!embedded && (
        <>
          <PageHeader title="Markets" subtitle="Indices, breadth, cross-asset tape, sectors, rates, credit and macro headlines — with per-region data dates" />
          <MacroNav />
        </>
      )}
      <QueryView query={q} skeleton={<MarketSkeleton />}>
        {(d) => <MarketBody d={d} region={region} />}
      </QueryView>
    </>
  )
}

function MarketSkeleton() {
  return (
    <div className="grid grid-cols-12 gap-3">
      <Skeleton className="col-span-12 h-24" />
      <Skeleton className="col-span-12 h-72 xl:col-span-8" />
      <Skeleton className="col-span-12 h-72 xl:col-span-4" />
      <Skeleton className="col-span-12 h-40" />
      <Skeleton className="col-span-12 h-80 xl:col-span-8" />
      <Skeleton className="col-span-12 h-80 xl:col-span-4" />
    </div>
  )
}

const CURVE_DEFAULT: Record<RegionFilter, string> = { ALL: 'US', US: 'US', EU: 'DE', UK: 'GB', JP: 'JP', KR: 'KR', CNHK: 'CN', LATAM: 'BR', OTHER: 'AU' }

function MarketBody({ d, region }: { d: MarketOverview; region: RegionFilter }) {
  const today = isoDaysAgo(0)
  return (
    <div className="grid grid-cols-12 gap-3">
      <section className="col-span-12 flex flex-wrap items-center gap-x-6 gap-y-2 rounded-md border border-border bg-surface px-3 py-2" aria-label="Data dates, regime and risk appetite">
        <div className="min-w-0 flex-1 space-y-1.5">
          <DataAsOfStrip dataAsOf={d.dataAsOf} indices={d.indices} datesAligned={d.datesAligned} today={today} />
          <RegimeChip r={d.regime} />
        </div>
        <RiskChip />
      </section>
      <IndexBoard indices={d.indices} className="col-span-12 xl:col-span-8" />
      <BreadthPanel rows={d.breadthByRegion} total={d.breadth} className="col-span-12 xl:col-span-4" />
      <CrossAssetTape curves={d.yieldCurves} credit={d.creditSpreads} className="col-span-12" />
      <SectorPanel perf={d.sectorPerformance} indices={d.indices} className="col-span-12 xl:col-span-8" />
      <MoversPanel gainers={d.gainers} losers={d.losers} filter={d.moversFilter} className="col-span-12 xl:col-span-4" />
      <RatesPanel curves={d.yieldCurves} defaultCountry={CURVE_DEFAULT[region] ?? 'US'} className="col-span-12 xl:col-span-6" />
      <CreditPanel spreads={d.creditSpreads} className="col-span-12 xl:col-span-6" />
      <PolicyPathPanel className="col-span-12 xl:col-span-4" />
      <MacroTiles indicators={d.indicators} perRegion={region === 'ALL' ? 3 : 6} className="col-span-12 xl:col-span-8" />
    </div>
  )
}
