import { Suspense, lazy } from 'react'
import { useSearchParams } from 'react-router'
import { SkeletonRows } from '@/components/ui/feedback'
import { PageHeader } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import MarketMacro from '../market/MarketMacro'
import { MacroNav } from './shared'

const Exposures = lazy(() => import('./markets/Exposures').then((m) => ({ default: m.Exposures })))
const Regimes = lazy(() => import('./markets/Regimes').then((m) => ({ default: m.Regimes })))
const Scenarios = lazy(() => import('./markets/Scenarios').then((m) => ({ default: m.Scenarios })))
const Instruments = lazy(() => import('../instruments/InstrumentsBrowser').then((m) => ({ default: m.InstrumentsBrowser })))
const Episodes = lazy(() => import('./markets/Episodes').then((m) => ({ default: m.Episodes })))

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'instruments', label: 'Instruments' },
  { id: 'exposures', label: 'Exposures' },
  { id: 'regimes', label: 'Regime performance' },
  { id: 'scenarios', label: 'Scenario builder' },
  { id: 'episodes', label: 'Episode replay' },
] as const

/** Macro → Markets: market overview plus macro exposures, regime performance, scenarios and episode replay. */
export default function MacroMarkets() {
  const [params, setParams] = useSearchParams()
  const tab = TABS.some((t) => t.id === params.get('tab')) ? (params.get('tab') as string) : 'overview'
  return (
    <>
      <PageHeader title="Markets" subtitle="Market overview and how sectors, regions, factors and instruments respond to macro: exposures, regimes, scenarios and historical episodes" />
      <MacroNav />
      <Tabs
        value={tab}
        onValueChange={(v) =>
          setParams(
            (p) => {
              p.set('tab', v)
              return p
            },
            { replace: true },
          )
        }
      >
        <TabsList aria-label="Markets sections">
          {TABS.map((t) => (
            <TabsTrigger key={t.id} value={t.id}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <Suspense fallback={<SkeletonRows rows={10} className="pt-3" />}>
          <TabsContent value="overview">
            <MarketMacro embedded />
          </TabsContent>
          <TabsContent value="instruments">{tab === 'instruments' && <Instruments />}</TabsContent>
          <TabsContent value="exposures">{tab === 'exposures' && <Exposures />}</TabsContent>
          <TabsContent value="regimes">{tab === 'regimes' && <Regimes />}</TabsContent>
          <TabsContent value="scenarios">
            <Scenarios />
          </TabsContent>
          <TabsContent value="episodes">{tab === 'episodes' && <Episodes />}</TabsContent>
        </Suspense>
      </Tabs>
    </>
  )
}
