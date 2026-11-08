import { FlaskConical, History } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'
import { asOfLine, familyOptions, fmtContracts, fmtMetric, nPoints } from '@/api/positioning/adapters'
import { usePosMarket } from '@/api/positioning/hooks'
import { ASSET_CLASS_LABEL, FAMILY_HINT, FAMILY_LABEL, type PosDetail, type PosFamily } from '@/api/positioning/types'
import { type PriceRange, rangeStart } from '@/api/prices/range'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useStudio } from '@/pages/studio/store'
import { positioningSpec } from '@/api/backtest/adapters'
import { useBacktest } from '@/pages/backtest/store'
import { MacroNav } from '../macro/shared'
import { Lines, type StackPanel, StackedPanels } from './charts'
import { positioningDraft } from './draft'

const RANGES: PriceRange[] = ['1Y', '5Y', '10Y', 'Max']
const FAMS: PosFamily[] = ['legacy', 'disagg', 'tff', 'cit']
const pctFmt = (v: number) => `${Math.round(v * 100)}%`

export default function PositioningMarket() {
  const { code = '067651' } = useParams()
  const [params, setParams] = useSearchParams()
  const report = (FAMS as string[]).includes(params.get('report') ?? '') ? (params.get('report') as PosFamily) : null
  const [combined, setCombined] = useState(false)
  const [range, setRange] = useState<PriceRange>('5Y')
  const q = usePosMarket(code, report, combined, rangeStart(range))
  const d = q.data
  return (
    <>
      <PageHeader
        title={d ? `${d.market.name} · CFTC positioning` : `CFTC market ${code}`}
        subtitle={d ? `${d.market.market_and_exchange ?? d.market.market_name} · ${ASSET_CLASS_LABEL[d.market.asset_class] ?? d.market.asset_class} · code ${d.market.market_code}` : 'Commitments of Traders'}
        actions={
          <Link to="/macro/positioning" className="text-xs text-accent-text hover:underline">
            All markets
          </Link>
        }
      />
      <MacroNav />
      <QueryView query={q} skeleton={<Skeleton className="h-[720px]" />}>
        {(data) => (
          <Body
            d={data}
            range={range}
            setRange={setRange}
            combined={combined}
            setCombined={setCombined}
            setReport={(f) =>
              setParams((p) => {
                p.set('report', f)
                return p
              })
            }
            fetching={q.isFetching}
          />
        )}
      </QueryView>
    </>
  )
}

function Body({ d, range, setRange, combined, setCombined, setReport, fetching }: { d: PosDetail; range: PriceRange; setRange: (r: PriceRange) => void; combined: boolean; setCombined: (b: boolean) => void; setReport: (f: PosFamily) => void; fetching: boolean }) {
  const navigate = useNavigate()
  const setDraft = useStudio((s) => s.setDraft)
  const setTab = useStudio((s) => s.setTab)
  const setRun = useStudio((s) => s.setRun)
  const openStudio = () => {
    setDraft(positioningDraft(d))
    setRun(null)
    setTab('builder')
    navigate('/studio')
  }
  const openBacktest = useBacktest((s) => s.open)
  /** "Backtest this signal": contrarian event strategy on CFTC COT-index extremes (cross-asset assets joined to CFTC). */
  const backtestSignal = () => {
    const XA: Record<string, string> = { energy: 'commodity', metals: 'commodity', ags: 'commodity', softs: 'commodity', livestock: 'commodity', rates: 'rates', equity: 'equity_index', fx: 'fx', crypto: 'crypto' }
    const cls = XA[d.market.asset_class] ?? null
    const ids = cls === 'commodity' && d.market.root ? [d.market.root] : []
    openBacktest(positioningSpec({ assetIds: ids, assetClass: cls, marketName: d.market.name }), {
      origin: { kind: 'cftc', ref: d.market.market_code, label: `from CFTC · ${d.market.name}`, notes: [ids.length ? `asset ${ids[0]} (cross-asset registry, COT joined on the release date)` : `all ${cls ?? ''} assets with a CFTC join`, 'contrarian: fade COT-index extremes (official CFTC positioning)'] },
    })
    navigate('/backtest')
  }
  const head = d.latest.find((x) => x.category === d.headline_category)
  const fams = familyOptions(d.families_available)
  const px = d.price_instrument
  const panels = useMemo<StackPanel[]>(() => {
    const out: StackPanel[] = []
    if (px && nPoints(d.price) > 1) out.push({ title: `${px.ticker} price${px.is_proxy ? ' (proxy)' : ''}`, series: [{ name: px.ticker, data: d.price, color: 2 }], fmt: (v) => v.toLocaleString(undefined, { maximumSignificantDigits: 5 }) })
    for (const s of d.series) {
      if (s.category === 'spec_hedger' || nPoints(s.net) < 2) continue
      out.push({ title: `${s.label} · net (contracts)`, series: [{ name: `${s.label} net`, data: s.net, kind: 'signed-bar' }], fmt: fmtContracts })
    }
    return out
  }, [d, px])
  const headSeries = d.series.find((s) => s.category === d.headline_category)
  const sh = d.series.find((s) => s.category === 'spec_hedger')
  return (
    <div className="space-y-3">
      <Panel>
        <div className="flex flex-wrap items-end gap-6">
          <Stat label={`${head?.label ?? 'Headline'} net`} value={<span className="num">{fmtContracts(head?.net)}</span>} sub={head?.net_pct_oi != null ? `${fmtMetric(head.net_pct_oi, 'net_pct_oi')} of OI` : undefined} />
          <Stat label="COT index 3y" value={<span className="num">{fmtMetric(head?.cot_index_3y, 'cot_index_3y')}</span>} sub={`1y ${fmtMetric(head?.cot_index_1y, 'cot_index_1y')}`} />
          <Stat label="z 3y" value={<span className="num">{fmtMetric(head?.z_3y, 'z_3y')}</span>} />
          <Stat label="1w change" value={<span className="num">{fmtContracts(head?.chg_1w)}</span>} sub={`4w ${fmtContracts(head?.chg_4w)}`} />
          <Stat
            label="Flag (model-based)"
            value={
              head?.extreme ? (
                <Badge tone={head.extreme === 'long' ? 'info' : 'accent'}>
                  {head.crowded ? 'crowded ' : ''}
                  {head.extreme}
                </Badge>
              ) : (
                <span className="text-sm text-muted">none</span>
              )
            }
            sub={head?.divergence_flag ? `${head.divergence_flag} divergence` : undefined}
          />
          <div className="ml-auto flex flex-wrap items-center gap-2">
            {d.market.security_id && (
              <Link to={`/instruments/${encodeURIComponent(d.market.security_id)}`} className="text-xs text-accent-text hover:underline">
                Instrument page
              </Link>
            )}
            <Button variant="outline" size="sm" onClick={backtestSignal} title="Backtest this signal: event-driven strategy on COT-index extremes (Backtest Lab)">
              <History /> Backtest this signal
            </Button>
            <Button variant="primary" size="sm" onClick={openStudio}>
              <FlaskConical /> Open in Model Studio
            </Button>
          </div>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
          <Segmented label="Report type" value={d.report as PosFamily} onChange={setReport} options={fams.map((f) => ({ value: f, label: FAMILY_LABEL[f], title: FAMILY_HINT[f] }))} />
          {d.report !== 'cit' && (
            <Segmented
              label="Futures only or combined"
              size="xs"
              value={combined ? 'comb' : 'fut'}
              onChange={(v) => setCombined(v === 'comb')}
              options={[
                { value: 'fut', label: 'Futures only' },
                { value: 'comb', label: 'Futures + options' },
              ]}
            />
          )}
          <Segmented label="Range" size="xs" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r }))} />
          {fetching && <span className="text-2xs text-muted">updating…</span>}
        </div>
        <p className="mt-2 text-2xs text-muted">
          {asOfLine(d)}. {d.report_label} report{d.combined ? ', futures and options combined' : ', futures only'}. History from {fmtDate(d.market.first_report_date)}.
          {px?.is_proxy ? ` Price overlay uses ${px.ticker} as a proxy (no futures series for this contract).` : ''}
        </p>
      </Panel>
      <Panel title="Latest positions by trader category" subtitle={`Week of ${d.report_date ?? DASH}`} flush>
        <LatestTable d={d} />
      </Panel>
      <Panel title="Net positions by category" subtitle={px ? 'Price on top; one panel per category, shared time axis (scroll to zoom)' : 'One panel per category, shared time axis (scroll to zoom)'}>
        {d.dates.length ? <StackedPanels dates={d.dates} panels={panels} ariaLabel={`${d.market.name} net positions by trader category`} /> : <p className="py-6 text-center text-xs text-muted">No reports in this range.</p>}
      </Panel>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="COT index history (3y window)" subtitle="0 = most net short, 100 = most net long of the trailing 156 weeks · shaded = model-based extreme zones">
          <StackedPanels
            dates={d.dates}
            panelHeight={150}
            ariaLabel="COT index history"
            panels={[
              { title: `${headSeries?.label ?? 'Headline'} COT index`, series: [{ name: headSeries?.label ?? 'Headline', data: headSeries?.cot_index_3y ?? [], color: 1 }], min: 0, max: 1, fmt: pctFmt, bands: [{ from: 0.9, to: 1, tone: 'hi' }, { from: 0, to: 0.1, tone: 'lo' }] },
              { title: `Speculators − hedgers COT index (${d.spec_hedger_definition})`, series: [{ name: 'Spec − hedger', data: sh?.cot_index_3y ?? [], color: 3 }], min: 0, max: 1, fmt: pctFmt, bands: [{ from: 0.9, to: 1, tone: 'hi' }, { from: 0, to: 0.1, tone: 'lo' }] },
            ]}
          />
        </Panel>
        <Panel title="Open interest" subtitle="Contracts">
          <Lines dates={d.dates} series={[{ name: 'Open interest', data: d.open_interest, color: 2 }]} fmt={fmtContracts} height={150} ariaLabel="Open interest" />
          <div className="mt-2 text-2xs font-semibold tracking-wide text-muted uppercase">Concentration · 4 largest traders, net</div>
          {nPoints(d.conc_net_4_long) > 1 ? (
            <Lines
              dates={d.dates}
              series={[
                { name: 'Long', data: d.conc_net_4_long, color: 1 },
                { name: 'Short', data: d.conc_net_4_short, color: 0 },
              ]}
              fmt={pctFmt}
              height={150}
              ariaLabel="Concentration of the four largest traders"
            />
          ) : (
            <p className="py-4 text-center text-xs text-muted">Not published for this report.</p>
          )}
        </Panel>
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Number of traders" subtitle={`${headSeries?.label ?? 'Headline'} · long vs short`}>
          {headSeries && nPoints(headSeries.traders_long) > 1 ? (
            <Lines
              dates={d.dates}
              series={[
                { name: 'Long', data: headSeries.traders_long, color: 1 },
                { name: 'Short', data: headSeries.traders_short, color: 0 },
              ]}
              height={180}
              ariaLabel="Number of traders"
            />
          ) : (
            <p className="py-4 text-center text-xs text-muted">No trader counts for this category.</p>
          )}
        </Panel>
        <Panel title="Extreme episodes" subtitle="Model-based: headline COT index 3y ≥ 90 or ≤ 10 (in range)" flush>
          {d.extremes.length ? (
            <div className="max-h-[230px] overflow-y-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                    <th className="px-3 py-1">Side</th>
                    <th className="px-2 py-1">From</th>
                    <th className="px-2 py-1">To</th>
                    <th className="px-2 py-1 text-right">Weeks</th>
                    <th className="px-3 py-1 text-right">Peak COT</th>
                  </tr>
                </thead>
                <tbody>
                  {[...d.extremes].reverse().map((e) => (
                    <tr key={e.start} className="border-b border-border/60">
                      <td className="px-3 py-1">
                        <Badge tone={e.side === 'long' ? 'info' : 'accent'}>{e.side}</Badge>
                      </td>
                      <td className="num px-2 py-1">{e.start}</td>
                      <td className="num px-2 py-1">{e.end}</td>
                      <td className="num px-2 py-1 text-right">{e.weeks}</td>
                      <td className="num px-3 py-1 text-right">{fmtMetric(e.peak_cot_index, 'cot_index_3y')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="px-3 py-4 text-center text-xs text-muted">No extreme in the selected range.</p>
          )}
        </Panel>
      </div>
      <Panel title="After extremes: forward returns" subtitle="Model-based event study (full history, futures-only report): log return from the first close after the release; indicative only — small samples, overlapping windows" flush>
        <EventTable d={d} />
      </Panel>
      <p className="text-2xs text-muted">Source: CFTC (official). {d.model_note}</p>
    </div>
  )
}

function LatestTable({ d }: { d: PosDetail }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-xs" aria-label="Latest positions">
        <thead>
          <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
            <th className="px-3 py-1">Category</th>
            <th className="px-2 py-1 text-right">Long</th>
            <th className="px-2 py-1 text-right">Short</th>
            <th className="px-2 py-1 text-right">Net</th>
            <th className="px-2 py-1 text-right">% OI</th>
            <th className="px-2 py-1 text-right">1w chg</th>
            <th className="px-2 py-1 text-right">COT 1y</th>
            <th className="px-2 py-1 text-right">COT 3y</th>
            <th className="px-2 py-1 text-right">z 3y</th>
            <th className="px-2 py-1 text-right">Traders L / S</th>
            <th className="px-3 py-1">Flag</th>
          </tr>
        </thead>
        <tbody>
          {d.latest.map((x) => (
            <tr key={x.category} className={cn('border-b border-border/60', x.category === d.headline_category && 'bg-surface-2')}>
              <td className="px-3 py-1 font-medium text-fg">{x.label}</td>
              <td className="num px-2 py-1 text-right">{fmtContracts(x.long)}</td>
              <td className="num px-2 py-1 text-right">{fmtContracts(x.short)}</td>
              <td className={cn('num px-2 py-1 text-right', (x.net ?? 0) >= 0 ? 'text-fg' : 'text-muted')}>{fmtContracts(x.net)}</td>
              <td className="num px-2 py-1 text-right">{fmtMetric(x.net_pct_oi, 'net_pct_oi')}</td>
              <td className="num px-2 py-1 text-right">{fmtContracts(x.chg_1w)}</td>
              <td className="num px-2 py-1 text-right">{fmtMetric(x.cot_index_1y, 'cot_index_1y')}</td>
              <td className="num px-2 py-1 text-right">{fmtMetric(x.cot_index_3y, 'cot_index_3y')}</td>
              <td className="num px-2 py-1 text-right">{fmtMetric(x.z_3y, 'z_3y')}</td>
              <td className="num px-2 py-1 text-right">{x.traders_long != null ? `${x.traders_long} / ${x.traders_short ?? DASH}` : DASH}</td>
              <td className="px-3 py-1">
                {x.extreme ? <Badge tone={x.extreme === 'long' ? 'info' : 'accent'}>{`${x.crowded ? 'crowded ' : ''}${x.extreme}`}</Badge> : null}
                {x.divergence_flag ? <Badge className="ml-1">{x.divergence_flag} div.</Badge> : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function EventTable({ d }: { d: PosDetail }) {
  const rows = d.event_study.filter((e) => e.n_events > 0)
  if (!rows.length) return <p className="px-3 py-4 text-center text-xs text-muted">{d.price_instrument ? 'No completed extreme events for this report.' : 'No price series mapped to this market.'}</p>
  const pct = (v: number | null | undefined) => (v == null ? DASH : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(1)}%`)
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-xs" aria-label="Event study">
        <thead>
          <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
            <th className="px-3 py-1">Category</th>
            <th className="px-2 py-1">Event</th>
            <th className="px-2 py-1 text-right">Horizon</th>
            <th className="px-2 py-1 text-right">Events</th>
            <th className="px-2 py-1 text-right">Mean fwd</th>
            <th className="px-2 py-1 text-right">All weeks</th>
            <th className="px-2 py-1 text-right">Excess</th>
            <th className="px-2 py-1 text-right">Hit rate</th>
            <th className="px-3 py-1 text-right">t</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((e) => (
            <tr key={`${e.category}-${e.signal}-${e.horizon_w}`} className="border-b border-border/60">
              <td className="px-3 py-1">{e.label}</td>
              <td className="px-2 py-1 text-muted">{e.signal.replace('extreme_', '')} extreme</td>
              <td className="num px-2 py-1 text-right">{e.horizon_w}w</td>
              <td className="num px-2 py-1 text-right">{e.n_events}</td>
              <td className="num px-2 py-1 text-right">{pct(e.mean_fwd)}</td>
              <td className="num px-2 py-1 text-right text-muted">{pct(e.mean_uncond)}</td>
              <td className="num px-2 py-1 text-right">{pct(e.excess_mean)}</td>
              <td className="num px-2 py-1 text-right">{e.hit_rate == null ? DASH : `${Math.round(e.hit_rate * 100)}%`}</td>
              <td className="num px-3 py-1 text-right">{e.t_stat == null ? DASH : e.t_stat.toFixed(1)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-1.5 text-2xs text-muted">
        Prices: {d.price_instrument?.ticker}
        {d.price_instrument?.is_proxy ? ' (proxy)' : ''}. Model-based, descriptive only.
      </p>
    </div>
  )
}
