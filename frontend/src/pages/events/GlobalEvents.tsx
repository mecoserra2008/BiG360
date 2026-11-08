import { useSearchParams } from 'react-router'
import { useEvTaxonomy } from '@/api/events/hooks'
import { type EvRange, type EvTaxonomy, coverageText, rangeDays, rangeFreq } from '@/api/events/types'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { PageHeader } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useState } from 'react'
import { ConnectednessPanel, IrfPanel, SpilloversPanel } from './v2/Contagion'
import { EscalationPanel, GprPanel } from './v2/Risk'
import { StoriesPanel } from './v2/Stories'
import { StudiesPanel } from './v2/Studies'
import { ShocksPanel, ThemeHeatmap } from './v2/ThemeHeatmap'
import { type MapWindow, WorldMapPanel } from './v2/WorldMapPanel'

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'risk', label: 'Escalation & GPR' },
  { id: 'contagion', label: 'Contagion & spillovers' },
  { id: 'studies', label: 'Event studies' },
] as const

export default function GlobalEvents() {
  const tax = useEvTaxonomy()
  return (
    <>
      <PageHeader title="Global Events" subtitle="Labelled GDELT coverage (41 themes) and clustered news stories by country, sector and company · model-based risk, contagion and event-study layers" />
      <QueryView query={tax} skeleton={<Skeleton className="h-[640px]" />}>
        {(d) => (
          <>
            <p className="-mt-2 mb-2 text-2xs text-muted">
              News coverage: <span className="num text-fg">{coverageText(d.coverage) ?? 'n/a'}</span>
              {d.coverage?.n_days ? ` · ${d.coverage.n_days} days with labelled coverage (backfill in progress adds older history)` : ''}
            </p>
            <EventsBody tax={d} />
          </>
        )}
      </QueryView>
    </>
  )
}

function EventsBody({ tax }: { tax: EvTaxonomy }) {
  const [params, setParams] = useSearchParams()
  const tab = TABS.some((t) => t.id === params.get('tab')) ? (params.get('tab') as string) : 'overview'
  const [themes, setThemes] = useState<string[]>([])
  const [span, setSpan] = useState<EvRange>('7D')
  const [win, setWin] = useState<MapWindow>({ days: 7, sliderDays: 90, sliderFreq: 'day' })
  const pickSpan = (r: EvRange) => {
    const dd = rangeDays(r, tax.coverage)
    const sd = Math.max(90, dd)
    setSpan(r)
    setWin({ days: dd, sliderDays: sd, sliderFreq: rangeFreq(sd) })
  }
  const [country, setCountry] = useState<string | null>(params.get('country'))
  const days = win.start && win.end ? Math.max(1, Math.round((Date.parse(win.end) - Date.parse(win.start)) / 864e5) + 1) : (win.days ?? 7)
  return (
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
      <TabsList aria-label="Global Events sections">
        {TABS.map((t) => (
          <TabsTrigger key={t.id} value={t.id}>
            {t.label}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value="overview">
        <div className="grid grid-cols-12 gap-3 pt-3">
          <div className="col-span-12 2xl:col-span-8">
            <WorldMapPanel span={span} onSpan={pickSpan} tax={tax} themes={themes} onThemes={setThemes} win={win} onWin={setWin} country={country} onCountry={setCountry} />
          </div>
          <div className="col-span-12 2xl:col-span-4">
            <ShocksPanel tax={tax} />
          </div>
          <div className="col-span-12 xl:col-span-7">
            <StoriesPanel tax={tax} country={country} onCountry={setCountry} themes={themes} days={days} start={win.start} end={win.end} />
          </div>
          <div className="col-span-12 xl:col-span-5">
            <ThemeHeatmap tax={tax} themes={themes} win={win} onCountry={setCountry} />
          </div>
        </div>
      </TabsContent>
      <TabsContent value="risk">
        <div className="space-y-3 pt-3">
          <EscalationPanel />
          <GprPanel />
        </div>
      </TabsContent>
      <TabsContent value="contagion">
        <div className="space-y-3 pt-3">
          <ConnectednessPanel />
          <SpilloversPanel />
          <IrfPanel />
        </div>
      </TabsContent>
      <TabsContent value="studies">
        <div className="pt-3">
          <StudiesPanel tax={tax} />
        </div>
      </TabsContent>
    </Tabs>
  )
}
