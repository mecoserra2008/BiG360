import { FlaskConical, Users } from 'lucide-react'
import { useMemo } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { useInstrument } from '@/api/instruments/hooks'
import { usePosMarkets } from '@/api/positioning/hooks'
import { type InstrumentDetail, priceDigits } from '@/api/instruments/types'
import { EChart, type EChartsOption } from '@/components/charts/EChart'
import { RangePriceChart } from '@/components/charts/RangePriceChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Button } from '@/components/ui/button'
import { useLiveQuotes } from '@/api/live/quotes'
import { QuoteTag } from '@/components/live/LiveQuote'
import { Delta } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate, fmtNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useStudio } from '@/pages/studio/store'
import { instrumentDraft } from './draft'
import { InstrumentCrossAsset } from '../crossasset/InstrumentPanels'
import { MacroNav } from '../macro/shared'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pct = (v: unknown, d = 1) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v * 100).toFixed(d)}%` : DASH)
const TYPE_LABEL: Record<string, string> = { fx: 'FX pair', future: 'Futures (continuous front month)', index: 'Index', etf: 'ETF' }

export default function InstrumentPage() {
  const { id = 'IDX:^GSPC' } = useParams()
  const q = useInstrument(id)
  return (
    <>
      <PageHeader
        title={q.data ? `${q.data.ticker} · ${q.data.name.replace(' (Yahoo market rate)', '')}` : id}
        subtitle={q.data ? `${TYPE_LABEL[q.data.type] ?? q.data.type} · ${q.data.group_label} · ${q.data.currency ?? ''}` : 'Market instrument'}
        actions={
          <Link to="/macro/markets/instruments" className="text-xs text-accent-text hover:underline">
            All instruments
          </Link>
        }
      />
      <MacroNav />
      <QueryView query={q} skeleton={<Skeleton className="h-[600px]" />}>
        {(d) => <Body d={d} />}
      </QueryView>
    </>
  )
}

function Body({ d }: { d: InstrumentDetail }) {
  const navigate = useNavigate()
  const setDraft = useStudio((s) => s.setDraft)
  const setTab = useStudio((s) => s.setTab)
  const setRun = useStudio((s) => s.setRun)
  // CFTC positioning markets linked to this instrument (futures contract, mini/micro or labelled price proxy)
  const pos = usePosMarkets({ security_id: d.security_id, active: null, limit: 5 })
  const posMarket = pos.data?.items[0]
  const live = useLiveQuotes([d.security_id]).get(d.security_id)   // workstream B: delayed live quote
  const openStudio = () => {
    setDraft(instrumentDraft(d))
    setRun(null)
    setTab('builder')
    navigate('/studio')
  }
  return (
    <div className="space-y-3">
      <Panel>
        <div className="flex flex-wrap items-end gap-6">
          {live && (
            <Stat
              label="Live · delayed"
              value={<span className="num">{fmtNum(live.price, priceDigits(live.price, d.type))}</span>}
              sub={<span className="inline-flex items-center gap-1.5"><Delta value={live.changePct} showIcon={false} format={(x) => pct(x, 2)} /><QuoteTag q={live} /></span>}
            />
          )}
          <Stat label={`${live ? 'Close' : 'Last'}${d.last_date ? ` · ${fmtDate(d.last_date)}` : ''}`} value={<span className="num">{fin(d.last) ? fmtNum(d.last, priceDigits(d.last, d.type)) : DASH}</span>} sub={d.currency ?? undefined} />
          {(['chg_1d', 'chg_1m', 'chg_ytd', 'chg_1y'] as const).map((k) => (
            <Stat key={k} label={k.replace('chg_', '').toUpperCase()} value={<Delta value={d[k] ?? null} showIcon={false} format={(x) => pct(x)} />} />
          ))}
          <Stat label="History" value={<span className="num text-sm">{d.first_date ? fmtDate(d.first_date) : DASH}</span>} sub={d.daily_from ? `daily from ${fmtDate(d.daily_from)}` : undefined} />
          {posMarket && (
            <Link
              to={`/macro/positioning/${encodeURIComponent(posMarket.market_code)}`}
              className="ml-auto inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-fg hover:bg-surface-2"
              title={`CFTC Commitments of Traders: ${posMarket.name}`}
            >
              <Users className="size-3.5" /> CFTC positioning
            </Link>
          )}
          <Button variant="primary" size="sm" className={posMarket ? undefined : 'ml-auto'} onClick={openStudio}>
            <FlaskConical /> Open in Model Studio
          </Button>
        </div>
        <p className="mt-2 text-2xs text-muted">
          Source: {d.source}.{d.note ? ` ${d.note}` : ''}
        </p>
      </Panel>
      <Panel title="Price" subtitle="Adjusted close · pick a range; Max covers the full history">
        <RangePriceChart kind="instrument" id={d.security_id} initialRange="5Y" height={420} currency={d.currency ?? undefined} ariaLabel={`${d.ticker} price history`} />
      </Panel>
      <InstrumentCrossAsset securityId={d.security_id} />
      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Returns and risk" subtitle="Annualized from weekly closes" flush>
          <table className="w-full text-xs" aria-label="Return statistics">
            <thead>
              <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                <th className="px-3 py-1">Window</th>
                <th className="px-2 py-1 text-right">Ann. return</th>
                <th className="px-2 py-1 text-right">Ann. vol</th>
                <th className="px-2 py-1 text-right">Max drawdown</th>
                <th className="px-3 py-1 text-right">From</th>
              </tr>
            </thead>
            <tbody>
              {d.stats.map((s) => (
                <tr key={s.window} className="border-b border-border/60">
                  <td className="px-3 py-1 font-medium text-fg">{s.window}</td>
                  <td className={cn('num px-2 py-1 text-right', fin(s.ann_return) && (s.ann_return >= 0 ? 'text-up' : 'text-down'))}>{pct(s.ann_return)}</td>
                  <td className="num px-2 py-1 text-right">{pct(s.ann_vol)}</td>
                  <td className="num px-2 py-1 text-right text-down">{pct(s.max_drawdown)}</td>
                  <td className="num px-3 py-1 text-right text-muted">{s.ann_return == null ? 'not enough history' : s.start ? fmtDate(s.start) : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {d.type === 'index' && /yield|VIX|Volatility|Skew/i.test(d.name) && <p className="px-3 py-2 text-2xs text-muted">Level series (yields / volatility): return statistics describe changes in the level, not an investable return.</p>}
        </Panel>
        <Panel title="Correlations" subtitle={d.correlation_method} flush>
          <table className="w-full text-xs" aria-label="Correlations with related instruments">
            <thead>
              <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                <th className="px-3 py-1">Instrument</th>
                <th className="px-2 py-1 text-right">3 years</th>
                <th className="px-3 py-1 text-right">10 years</th>
              </tr>
            </thead>
            <tbody>
              {d.correlations.map((c) => (
                <tr key={c.security_id} className="border-b border-border/60">
                  <td className="px-3 py-1">
                    <Link to={`/instruments/${encodeURIComponent(c.security_id)}`} className="font-medium text-accent-text hover:underline">
                      {c.ticker}
                    </Link>
                    <span className="ml-1.5 text-muted">{c.name.replace(' (Yahoo market rate)', '').replace(' (continuous front month)', '')}</span>
                  </td>
                  <CorrCell v={c.corr_3y} n={c.n_3y} />
                  <CorrCell v={c.corr_10y} n={c.n_10y} last />
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
      <Panel title="Rolling 1-year volatility" subtitle="52-week standard deviation of weekly log returns, annualized">
        <RollingVol d={d} />
      </Panel>
    </div>
  )
}

function CorrCell({ v, n, last = false }: { v: number | null | undefined; n?: number; last?: boolean }) {
  const s = fin(v) ? Math.max(-1, Math.min(1, v)) : null
  const bg = s === null ? undefined : `color-mix(in srgb, ${s >= 0 ? 'var(--c2)' : 'var(--c1)'} ${Math.round(Math.abs(s) * 45)}%, transparent)`
  return (
    <td className={cn('num py-1 text-right', last ? 'px-3' : 'px-2')}>
      <span className="rounded-sm px-1.5" style={{ background: bg }} title={n ? `${n} weekly returns` : undefined}>
        {s === null ? DASH : s.toFixed(2)}
      </span>
    </td>
  )
}

function RollingVol({ d }: { d: InstrumentDetail }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 8, bottom: 20, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (v) => pct(v) },
      xAxis: { type: 'category', data: d.rolling_vol.map((p) => p.date), axisLabel: { formatter: (v: string) => v.slice(0, 4) } },
      yAxis: { type: 'value', axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
      series: [{ type: 'line', name: '1y vol', data: d.rolling_vol.map((p) => p.vol), showSymbol: false, lineStyle: { color: t.series[0], width: 1.5 }, itemStyle: { color: t.series[0] }, areaStyle: { opacity: 0.08 } }],
    }),
    [d.rolling_vol, t],
  )
  if (d.rolling_vol.length < 2) return <p className="py-6 text-center text-xs text-muted">Not enough history.</p>
  return <EChart option={option} height={220} ariaLabel={`${d.ticker} rolling one-year volatility`} />
}

