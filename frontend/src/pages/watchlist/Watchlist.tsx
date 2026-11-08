import type { ColumnDef } from '@tanstack/react-table'
import { BellPlus, Plus, Star, Trash2 } from 'lucide-react'
import { useDeferredValue, useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { MacroAlerts } from './MacroAlerts'
import { conditionText, evaluateValue, targetText } from './ruleLabels'
import { WatchlistExposure } from '../supplychain/WatchlistExposure'
import type { AlertMetric, AlertOperator, AlertRule, ScreenerRow } from '@/api/types'
import { useScreener, useSearch } from '@/api/hooks'
import { DataTable } from '@/components/tables/DataTable'
import { SecurityCell } from '@/components/tables/cells'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { CompositeLabel, useCompositeWording } from '@/components/ui/compositeLabel'
import { Delta, Flag, MoatBadge, ScorePill } from '@/components/ui/data'
import { EmptyState, QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Input, Select, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { fmtCompactCurrency, fmtDateTime, fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useLiveQuotes } from '@/api/live/quotes'
import { LiveChangeById, LivePriceById } from '@/components/live/LiveQuote'
import { useWatchlist } from '@/store'

const METRICS: { value: AlertMetric; label: string; unit: string }[] = [
  { value: 'price', label: 'Price', unit: 'local ccy' },
  { value: 'change1dPct', label: '1D change %', unit: '%' },
  { value: 'compositeScore', label: 'Composite score (descriptive)', unit: '0–100' },
  { value: 'insiderAnomaly', label: 'Insider anomaly', unit: '0–1' },
  { value: 'sentiment', label: 'Sentiment index', unit: '−1…1' },
  { value: 'zoneTouch', label: 'Touches rank-N zone', unit: 'rank' },
  { value: 'earningsUpcoming', label: 'Earnings within N days', unit: 'days' },
  { value: 'earningsSurprise', label: 'Earnings model surprise |z|', unit: 'σ (model-based)' },
  { value: 'activist13d', label: 'New 13D / 13G → 13D (SEC)', unit: '(no threshold)' },
  { value: 'siSpike', label: 'Short interest rises ≥ (FINRA)', unit: '%' },
  { value: 'euShort', label: 'New / increased EU-UK short ≥', unit: '% of capital' },
  { value: 'cfOutsideRange', label: 'Close outside calibrated range (model-based; above = up, below = down, crosses = either)', unit: 'level 80 / 90 / 95' },
  { value: 'cfPeerAnomaly', label: 'Peer-group joint anomaly ≥ percentile (descriptive, not a trading signal)', unit: 'percentile, e.g. 95' },
]
const OPS: { value: AlertOperator; label: string }[] = [
  { value: '>', label: 'above' },
  { value: '<', label: 'below' },
  { value: 'crosses', label: 'crosses' },
]

export default function Watchlist() {
  const q = useScreener('ALL')
  return (
    <>
      <PageHeader title="Watchlist & Alerts" subtitle="Saved locally in this browser · alert rules evaluate against the latest snapshot" />
      <QueryView query={q} skeleton={<Panel><SkeletonRows rows={10} /></Panel>}>
        {(d) => <WatchBody rows={d.rows} compositeValidated={d.compositeValidated === true} />}
      </QueryView>
    </>
  )
}

function evaluate(rule: AlertRule, row: ScreenerRow | undefined): 'triggered' | 'ok' | 'pending' {
  if (!row || !rule.enabled) return 'pending'
  const v = rule.metric === 'price' ? row.price : rule.metric === 'change1dPct' ? row.change1dPct * 100 : rule.metric === 'compositeScore' ? row.compositeScore : null
  return evaluateValue(rule, v)
}

function WatchBody({ rows, compositeValidated }: { rows: ScreenerRow[]; compositeValidated: boolean }) {
  const navigate = useNavigate()
  // coverage v3 A8: the Score column is the composite, a descriptive ranking unless the API confirms a passed gate
  const scoreWording = useCompositeWording({ validated: compositeValidated })
  const ids = useWatchlist((s) => s.ids)
  const add = useWatchlist((s) => s.add)
  const remove = useWatchlist((s) => s.remove)
  const rules = useWatchlist((s) => s.rules).filter((r) => r.metric !== 'macroRelease' && r.metric !== 'macroSurprise' && r.metric !== 'regimeChange')
  const addRule = useWatchlist((s) => s.addRule)
  const updateRule = useWatchlist((s) => s.updateRule)
  const removeRule = useWatchlist((s) => s.removeRule)
  const byId = useMemo(() => new Map(rows.map((r) => [r.id, r])), [rows])
  const names = useWatchlist((s) => s.names)
  const tickerOf = (id: string) => byId.get(id)?.ticker ?? names[id]?.ticker
  const watched = useMemo(() => ids.map((id) => byId.get(id)).filter((r): r is ScreenerRow => !!r), [ids, byId])
  useLiveQuotes(ids)   // workstream B: delayed quotes for the watched securities (cells subscribe per id)

  const [q, setQ] = useState('')
  const dq = useDeferredValue(q)
  const search = useSearch(dq)

  const [ruleSec, setRuleSec] = useState('')
  const [metric, setMetric] = useState<AlertMetric>('price')
  const [op, setOp] = useState<AlertOperator>('<')
  const [threshold, setThreshold] = useState('')

  const columns = useMemo<ColumnDef<ScreenerRow>[]>(
    () => [
      { id: 'sec', header: 'Security', size: 260, accessorFn: (r) => r.ticker, cell: (c) => <SecurityCell id={c.row.original.id} ticker={c.row.original.ticker} name={c.row.original.name} country={c.row.original.country} /> },
      { id: 'sector', header: 'Subsector', size: 170, accessorFn: (r) => r.subsector, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
      // workstream B: live delayed price / change ("delayed · hh:mm") when the quotes service has one, else the close
      { id: 'price', header: 'Price', size: 104, accessorFn: (r) => r.price, cell: (c) => <span className="inline-flex items-start gap-1"><LivePriceById id={c.row.original.id} fallback={c.getValue<number>()} currency={c.row.original.currency} /> <span className="text-2xs text-faint">{c.row.original.currency}</span></span>, meta: { align: 'right' } },
      { id: 'd1', header: '1D', size: 74, accessorFn: (r) => r.change1dPct, cell: (c) => <LiveChangeById id={c.row.original.id} fallback={c.getValue<number>()} />, meta: { align: 'right' } },
      { id: 'r1m', header: '1M', size: 74, accessorFn: (r) => r.return1m, cell: (c) => <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 1, true)} />, meta: { align: 'right' } },
      { id: 'mcap', header: 'Mkt cap', size: 84, accessorFn: (r) => r.marketCapUsd, cell: (c) => <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span>, meta: { align: 'right' } },
      { id: 'score', header: () => <span title={`Composite score: ${scoreWording.title}`}>Score</span>, size: 62, accessorFn: (r) => r.compositeScore, cell: (c) => <ScorePill value={c.getValue<number>()} />, meta: { align: 'right' } },
      { id: 'moat', header: 'Moat', size: 110, accessorFn: (r) => r.moat, cell: (c) => <MoatBadge rating={c.row.original.moat} /> },
      {
        id: 'rm',
        header: '',
        size: 40,
        enableSorting: false,
        cell: (c) => (
          <Button size="icon-sm" variant="ghost" aria-label={`Remove ${c.row.original.ticker} from watchlist`} onClick={(e) => { e.stopPropagation(); remove(c.row.original.id) }}>
            <Trash2 />
          </Button>
        ),
      },
    ],
    [remove, scoreWording.title],
  )

  const submitRule = () => {
    const sec = byId.get(ruleSec || watched[0]?.id || '')
    const th = Number(threshold)
    if (!sec || !Number.isFinite(th) || threshold.trim() === '') return
    addRule({ companyId: sec.id, ticker: sec.ticker, metric, operator: op, threshold: th, enabled: true })
    setThreshold('')
  }

  return (
    <div className="grid grid-cols-12 gap-3">
      <Panel
        title={`Watchlist · ${watched.length}`}
        subtitle={`Score = composite score (${scoreWording.noun}${scoreWording.signalAllowed ? '' : ', not a forecast'})`}
        className="col-span-12"
        flush
        actions={
          <>
          <CompositeLabel validated={compositeValidated} />
          <div className="relative">
            <Input aria-label="Add security to watchlist" placeholder="Add ticker / name / ISIN…" value={q} onChange={(e) => setQ(e.target.value)} className="w-64" />
            {q.trim() && (search.data?.length ?? 0) > 0 && (
              <ul className="absolute right-0 z-20 mt-1 max-h-72 w-[380px] overflow-y-auto rounded-md border border-border-strong bg-surface p-1" role="listbox" aria-label="Search results">
                {search.data?.slice(0, 10).map((r) => (
                  <li key={r.id}>
                    <button
                      type="button"
                      onClick={() => { add(r.id); setQ('') }}
                      className="flex h-7 w-full items-center gap-2 rounded-sm px-2 text-left text-sm hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-focus"
                    >
                      <Flag country={r.country} />
                      <span className="num w-20 shrink-0 font-semibold">{r.ticker}</span>
                      <span className="min-w-0 flex-1 truncate text-muted">{r.name}</span>
                      {ids.includes(r.id) ? <Star className="size-3.5 fill-accent text-accent" aria-label="Already watched" /> : <Plus className="size-3.5 text-muted" aria-hidden />}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
          </>
        }
      >
        {watched.length === 0 ? (
          <EmptyState icon={<Star />} title="Your watchlist is empty" message="Use the search above, the ★ in the Screener, or the star on any Company 360 header." />
        ) : (
          <DataTable data={watched} columns={columns} ariaLabel="Watchlist" getRowId={(r) => r.id} onRowClick={(r) => navigate(`/company/${encodeURIComponent(r.id)}`)} initialSorting={[{ id: 'score', desc: true }]} />
        )}
      </Panel>

      <Panel title="Alert rules" subtitle="Price, 1D change and score rules evaluate in-app; insider / sentiment / zone rules evaluate on the backend scheduler" className="col-span-12" flush>
        <form
          className="flex flex-wrap items-center gap-2 border-b border-border p-2"
          onSubmit={(e) => {
            e.preventDefault()
            submitRule()
          }}
        >
          <Select label="Security" value={ruleSec || watched[0]?.id || ''} onChange={setRuleSec} options={watched.map((w) => ({ value: w.id, label: `${w.ticker} — ${w.name}` }))} className="w-64" />
          <Select label="Metric" value={metric} onChange={setMetric} options={METRICS.map((m) => ({ value: m.value, label: m.label }))} className="w-48" />
          <Select label="Operator" value={op} onChange={setOp} options={OPS} className="w-28" />
          <Input aria-label="Threshold" inputMode="decimal" placeholder={`Threshold (${METRICS.find((m) => m.value === metric)?.unit})`} value={threshold} onChange={(e) => setThreshold(e.target.value)} className="w-44" />
          <Button type="submit" variant="primary" size="sm" disabled={!watched.length || threshold.trim() === ''}>
            <BellPlus /> Add rule
          </Button>
        </form>
        {rules.length === 0 ? (
          <EmptyState title="No alert rules" message="Rules are stored locally; when the backend is connected they are mirrored to the scheduler." />
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-2xs tracking-wide text-muted uppercase">
                {['On', 'Security', 'Condition', 'Current', 'Status', 'Created', ''].map((h, i) => (
                  <th key={i} scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rules.map((r) => {
                const row = byId.get(r.companyId)
                const st = evaluate(r, row)
                const cur = !row ? '—' : r.metric === 'price' ? fmtPrice(row.price, row.currency) : r.metric === 'change1dPct' ? fmtPct(row.change1dPct, 2, true) : r.metric === 'compositeScore' ? fmtNum(row.compositeScore, 0) : '—'
                return (
                  <tr key={r.id} className={cn('hover:bg-surface-2', !r.enabled && 'opacity-60')}>
                    <td className="h-8 border-b border-border px-3"><Switch label={`Enable rule for ${r.ticker}`} checked={r.enabled} onCheckedChange={(v) => updateRule(r.id, { enabled: v })} /></td>
                    <td className="num border-b border-border px-3 font-semibold">
                      {r.metric === 'studioDrift' ? (
                        <Link to="/studio" className="inline-flex items-center gap-1.5 hover:text-accent-text hover:underline" title="Open Model Studio (Studies & runs)">
                          <Badge tone="info">studio</Badge> {r.studyName ?? r.ticker}
                        </Link>
                      ) : r.metric === 'backtestDrift' ? (
                        <Link to="/backtest" className="inline-flex items-center gap-1.5 hover:text-accent-text hover:underline" title="Open the Backtest Lab (Strategies & runs)">
                          <Badge tone="info">backtest</Badge> {r.studyName ?? r.ticker}
                        </Link>
                      ) : (
                        <span title={r.name ?? undefined}>{targetText(r, tickerOf)}</span>
                      )}
                    </td>
                    <td className="border-b border-border px-3">
                      {r.metric === 'studioDrift' ? (
                        <>
                          Model drift: headline metric worse by more than <span className="num font-medium">{Number.isFinite(r.threshold) ? `${Math.round(r.threshold)}%` : '—'}</span> on a scheduled re-run
                        </>
                      ) : r.metric === 'backtestDrift' ? (
                        <>
                          Paper strategy drift / decay: Sharpe or CAGR worse by more than <span className="num font-medium">{Number.isFinite(r.threshold) ? `${Math.round(r.threshold)}%` : '—'}</span>, live decay (z &lt; −2) or a drawdown breach on the weekly re-run
                        </>
                      ) : (
                        <span data-testid="rule-condition">{conditionText(r, METRICS.find((m) => m.value === r.metric)?.label ?? r.ruleType ?? r.metric)}</span>
                      )}
                    </td>
                    <td className="num border-b border-border px-3">{cur}</td>
                    <td className="border-b border-border px-3">
                      {st === 'triggered' ? <Badge tone="accent">● Triggered</Badge> : st === 'ok' ? <Badge tone="neutral">○ Not met</Badge> : <Badge tone="info">◌ Backend</Badge>}
                    </td>
                    <td className="num border-b border-border px-3 text-muted">{fmtDateTime(r.createdAt)}</td>
                    <td className="border-b border-border px-3 text-right">
                      <Button size="icon-sm" variant="ghost" aria-label={`Delete rule for ${r.ticker}`} onClick={() => removeRule(r.id)}><Trash2 /></Button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </Panel>
      <WatchlistExposure ids={ids} />
      <MacroAlerts />
    </div>
  )
}
