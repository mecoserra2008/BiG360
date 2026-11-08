import type { ColumnDef, SortingState, VisibilityState } from '@tanstack/react-table'
import { Code2, Columns3, Download, History, Landmark, NotebookPen, RotateCcw, SlidersHorizontal, Star } from 'lucide-react'
import { useDeferredValue, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router'
import type { FactorFamily, MarketCapBucket, MoatRating, ScreenerRow } from '@/api/types'
import { useScreener } from '@/api/hooks'
import { regionsToContract, weightsToContract } from '@/api/adapters/common'
import { screenRequest } from '@/api/backtest/adapters'
import { backtestApi } from '@/api/backtest/client'
import { newThesisHref } from '@/api/theses/adapters'
import { useBacktest } from '@/pages/backtest/store'
import { toast } from '@/components/ui/toast'
import { DataTable } from '@/components/tables/DataTable'
import { SecurityCell } from '@/components/tables/cells'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Delta, MoatBadge, PercentileBar, Popover, ScorePill } from '@/components/ui/data'
import { Missing } from '@/components/ui/missing'
import { contractRegion } from '@/lib/availability'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { CompositeLabel } from '@/components/ui/compositeLabel'
import { Checkbox, Input, Select, Slider } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { toCsv, downloadText } from '@/lib/csv'
import { screenerToBql } from '@/lib/bql'
import { DEFAULT_WEIGHTS, FACTOR_FAMILIES, FACTOR_LABEL, FACTOR_SHORT, rankByWeights } from '@/lib/factors'
import { fmtCompactCurrency, fmtInt, fmtMultiple, fmtPct, fmtPrice } from '@/lib/format'
import { countryName } from '@/lib/regions'
import { cn } from '@/lib/utils'
import { useUi, useWatchlist, useWeights } from '@/store'

type Ranked = ScreenerRow & { score: number; liveRank: number; missing: FactorFamily[] }

/** value after it has been stable for `ms` (weights refetch once the sliders stop). */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms)
    return () => clearTimeout(id)
  }, [value, ms])
  return v
}

const BUCKETS: { value: '' | MarketCapBucket; label: string }[] = [
  { value: '', label: 'All caps' },
  { value: 'mega', label: 'Mega ≥ $200bn' },
  { value: 'large', label: 'Large $10–200bn' },
  { value: 'mid', label: 'Mid $2–10bn' },
  { value: 'small', label: 'Small $0.3–2bn' },
  { value: 'micro', label: 'Micro < $0.3bn' },
]

const MX_COLS = ['mxRates', 'mxUsd', 'mxOil', 'mxCredit', 'mxGrowth', 'mxDown100'] as const
const DEFAULT_HIDDEN: VisibilityState = { subsector: false, price: false, divYld: false, netDebt: false, dq: false, ...Object.fromEntries(MX_COLS.map((c) => [c, false])) }
const DEFAULT_SORT: SortingState = [{ id: 'score', desc: true }]

type MacroFilter = '' | 'down100_pos' | 'down100_neg' | 'rates_neg' | 'rates_pos' | 'usd_pos' | 'oil_pos' | 'has'
const MACRO_FILTERS: { value: MacroFilter; label: string }[] = [
  { value: '', label: 'Any macro profile' },
  { value: 'down100_pos', label: 'Gains if rates −100bp' },
  { value: 'down100_neg', label: 'Loses if rates −100bp' },
  { value: 'rates_neg', label: 'Rate beta < 0' },
  { value: 'rates_pos', label: 'Rate beta > 0' },
  { value: 'usd_pos', label: 'USD beta > 0' },
  { value: 'oil_pos', label: 'Oil beta > 0' },
  { value: 'has', label: 'Has macro betas' },
]
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
function macroPass(r: ScreenerRow, f: MacroFilter): boolean {
  switch (f) {
    case '':
      return true
    case 'down100_pos':
      return fin(r.mxImpactRatesDown100) && r.mxImpactRatesDown100 > 0
    case 'down100_neg':
      return fin(r.mxImpactRatesDown100) && r.mxImpactRatesDown100 < 0
    case 'rates_neg':
      return fin(r.mxBetaRates) && r.mxBetaRates < 0
    case 'rates_pos':
      return fin(r.mxBetaRates) && r.mxBetaRates > 0
    case 'usd_pos':
      return fin(r.mxBetaUsd) && r.mxBetaUsd > 0
    case 'oil_pos':
      return fin(r.mxBetaOil) && r.mxBetaOil > 0
    case 'has':
      return fin(r.mxBetaRates)
  }
}
/** completeness C: an empty cell explains itself (field reason in the row's region, docs/DATA_COMPLETENESS.md) */
const na = (field: string, r: ScreenerRow) => <Missing field={field} region={contractRegion(r.region, r.country)} />
const betaCell = (v: number | null, field: string, r: ScreenerRow) =>
  fin(v) ? <span className="num">{`${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(3)}`}</span> : na(field, r)

export default function Screener() {
  const region = useUi((s) => s.region)
  const weights = useWeights((s) => s.weights)
  // the server picks the top 1,000 under the current weights: refetch when the sliders stop moving
  const fetchWeights = useDebounced(weights, 600)
  const q = useScreener(region, fetchWeights)
  // coverage v3 A8: re-weighted composites are never validated out of sample -> always descriptive
  const custom = FACTOR_FAMILIES.some((f) => weights[f] !== DEFAULT_WEIGHTS[f])
  return (
    <>
      <PageHeader
        title="Screener / Ranking"
        subtitle="Composite and factor-family percentiles: a descriptive peer ranking, not a forecast · adjust weights to re-rank live (the top 1,000 is re-selected on the server)"
        actions={<CompositeLabel custom={custom} validated={!custom && q.data?.compositeValidated === true} />}
      />
      <QueryView query={q} skeleton={<Panel><SkeletonRows rows={20} /></Panel>}>
        {(d) => <ScreenerBody rows={d.rows} refreshing={q.isFetching && q.isPlaceholderData} />}
      </QueryView>
    </>
  )
}

function ScreenerBody({ rows, refreshing = false }: { rows: ScreenerRow[]; refreshing?: boolean }) {
  const navigate = useNavigate()
  const region = useUi((s) => s.region)
  const openBacktest = useBacktest((s) => s.open)
  const [btBusy, setBtBusy] = useState(false)
  const weights = useWeights((s) => s.weights)
  const watch = useWatchlist((s) => s.ids)
  const toggleWatch = useWatchlist((s) => s.toggle)

  const [text, setText] = useState('')
  const dText = useDeferredValue(text)
  const [sector, setSector] = useState('')
  const [sub, setSub] = useState('')
  const [country, setCountry] = useState('')
  const [bucket, setBucket] = useState<'' | MarketCapBucket>('')
  const [moat, setMoat] = useState<'' | MoatRating>('')
  const [minScore, setMinScore] = useState(0)
  const [visibility, setVisibility] = useState<VisibilityState>(DEFAULT_HIDDEN)
  const [mx, setMx] = useState<MacroFilter>('')
  const [sorting, setSorting] = useState<SortingState>(DEFAULT_SORT)
  const [showIncomplete, setShowIncomplete] = useState(true)

  const sectors = useMemo(() => [...new Set(rows.map((r) => r.sector))].sort(), [rows])
  const subs = useMemo(() => [...new Set(rows.filter((r) => !sector || r.sector === sector).map((r) => r.subsector))].sort(), [rows, sector])
  const countries = useMemo(() => [...new Set(rows.map((r) => r.country))].sort((a, b) => countryName(a).localeCompare(countryName(b))), [rows])

  // live re-ranking with the current factor weights (missing factors: weights renormalised, as on the server)
  const ranked = useMemo<Ranked[]>(() => rankByWeights(rows, weights), [rows, weights])
  const nIncomplete = useMemo(() => ranked.filter((r) => r.missing.length > 0).length, [ranked])

  const filtered = useMemo(() => {
    const s = dText.trim().toLowerCase()
    return ranked.filter(
      (r) =>
        (!s || r.ticker.toLowerCase().includes(s) || r.name.toLowerCase().includes(s) || r.isin.toLowerCase().startsWith(s)) &&
        (!sector || r.sector === sector) &&
        (!sub || r.subsector === sub) &&
        (!country || r.country === country) &&
        (!bucket || r.marketCapBucket === bucket) &&
        (!moat || r.moat === moat) &&
        (showIncomplete || r.missing.length === 0) &&
        (minScore <= 0 || r.score >= minScore) &&
        macroPass(r, mx),
    )
  }, [ranked, dText, sector, sub, country, bucket, moat, minScore, mx, showIncomplete])

  const columns = useMemo<ColumnDef<Ranked>[]>(() => {
    const factorCols: ColumnDef<Ranked>[] = FACTOR_FAMILIES.map((f) => ({
      id: `f_${f}`,
      header: FACTOR_SHORT[f],
      size: 92,
      accessorFn: (r: Ranked) => r.factorScores[f],
      cell: (c) => <PercentileBar value={c.getValue<number>()} width="w-9" />,
      meta: { title: `${FACTOR_LABEL[f]} percentile (weight ${weights[f]})` },
    }))
    return [
      {
        id: 'watch',
        header: '',
        size: 48,
        enableSorting: false,
        cell: (c) => {
          const on = watch.includes(c.row.original.id)
          return (
            <span className="inline-flex items-center">
            <button
              type="button"
              aria-label={on ? `Remove ${c.row.original.ticker} from watchlist` : `Add ${c.row.original.ticker} to watchlist`}
              aria-pressed={on}
              onClick={(e) => {
                e.stopPropagation()
                toggleWatch(c.row.original.id)
              }}
              className="inline-flex size-5 items-center justify-center rounded-sm text-faint hover:text-accent focus-visible:outline-2 focus-visible:outline-focus"
            >
              <Star className={cn('size-3.5', on && 'fill-accent text-accent')} aria-hidden />
            </button>
            <button
              type="button"
              aria-label={`Create thesis on ${c.row.original.ticker}`}
              title="Create thesis (thesis tracker)"
              onClick={(e) => {
                e.stopPropagation()
                navigate(newThesisHref({ subjectType: 'security', subjectId: c.row.original.id, origin: 'screener', ref: `screener rank ${c.row.original.liveRank}` }))
              }}
              className="inline-flex size-5 items-center justify-center rounded-sm text-faint hover:text-accent focus-visible:outline-2 focus-visible:outline-focus"
            >
              <NotebookPen className="size-3.5" aria-hidden />
            </button>
            </span>
          )
        },
        meta: { title: 'Watchlist · create thesis', align: 'center' },
      },
      { id: 'rank', header: '#', size: 44, accessorFn: (r) => r.liveRank, sortDescFirst: false, cell: (c) => <span className="num text-muted">{c.getValue<number>()}</span>, meta: { align: 'right', title: 'Live rank under current weights' } },
      { id: 'security', header: 'Security', size: 250, accessorFn: (r) => r.ticker, sortDescFirst: false, cell: (c) => <SecurityCell id={c.row.original.id} ticker={c.row.original.ticker} name={c.row.original.name} country={c.row.original.country} /> },
      {
        id: 'score',
        header: 'Composite',
        size: 100,
        accessorFn: (r) => (Number.isFinite(r.score) ? r.score : undefined),
        sortUndefined: 'last',
        cell: (c) => {
          const r = c.row.original
          return (
            <span className="inline-flex items-center gap-1">
              {r.missing.length > 0 && (
                <span className="text-2xs font-semibold text-warn" title={`Incomplete: composite over ${10 - r.missing.length} of 10 factors (no score: ${r.missing.map((f) => FACTOR_LABEL[f]).join(', ')}); weights renormalised over the available factors, as on the server`} data-testid="incomplete-flag">
                  {10 - r.missing.length}/10
                </span>
              )}
              {r.scoreScope === 'extended' && (
                <span className="text-2xs text-faint" title="Outside the MVP model universe: scored against the MVP reference cross-section (grade = MVP quintile)" data-testid="scope-ext">
                  ext
                </span>
              )}
              {Number.isFinite(r.score) ? <ScorePill value={r.score} /> : r.scoreReason ? <Missing field="composite" reason={r.scoreReason} /> : <span className="text-faint">—</span>}
            </span>
          )
        },
        meta: { align: 'right', title: 'Weighted composite of factor-family percentiles (renormalised over the factors with a score). Descriptive peer ranking, not a forecast: no out-of-sample edge on record' },
      },
      { id: 'grade', header: 'Grade', size: 58, accessorFn: (r) => r.grade ?? undefined, sortUndefined: 'last', sortDescFirst: false, cell: (c) => (c.row.original.grade ? <span className="num font-semibold">{c.row.original.grade}</span> : na('grade', c.row.original)), meta: { align: 'center', title: 'A-E quintile of the server composite under your weights (after the top 1,000 is re-selected)' } },
      { id: 'mxDown100', header: 'Rates −100bp', size: 96, accessorFn: (r) => r.mxImpactRatesDown100 ?? undefined, sortUndefined: 'last', cell: (c) => (fin(c.row.original.mxImpactRatesDown100) ? <Delta value={c.row.original.mxImpactRatesDown100} format={(v) => fmtPct(v, 1, true)} showIcon={false} /> : na('macro.rates_down_100bp', c.row.original)), meta: { align: 'right', title: 'Model-based: impact if rates fall 100bp (2y and 10y), current betas' } },
      { id: 'mxRates', header: 'β rates', size: 72, accessorFn: (r) => r.mxBetaRates ?? undefined, sortUndefined: 'last', cell: (c) => betaCell(c.row.original.mxBetaRates, 'macro.beta_rates', c.row.original), meta: { align: 'right', title: 'Model-based beta to the 10y yield (return per +100bp)' } },
      { id: 'mxUsd', header: 'β USD', size: 68, accessorFn: (r) => r.mxBetaUsd ?? undefined, sortUndefined: 'last', cell: (c) => betaCell(c.row.original.mxBetaUsd, 'macro.beta_usd', c.row.original), meta: { align: 'right', title: 'Model-based elasticity to the broad USD' } },
      { id: 'mxOil', header: 'β oil', size: 68, accessorFn: (r) => r.mxBetaOil ?? undefined, sortUndefined: 'last', cell: (c) => betaCell(c.row.original.mxBetaOil, 'macro.beta_oil', c.row.original), meta: { align: 'right', title: 'Model-based elasticity to WTI oil' } },
      { id: 'mxCredit', header: 'β credit', size: 72, accessorFn: (r) => r.mxBetaCredit ?? undefined, sortUndefined: 'last', cell: (c) => betaCell(c.row.original.mxBetaCredit, 'macro.beta_credit', c.row.original), meta: { align: 'right', title: 'Model-based beta to the credit spread (return per +100bp)' } },
      { id: 'mxGrowth', header: 'β growth surp.', size: 92, accessorFn: (r) => r.mxBetaGrowthSurprise ?? undefined, sortUndefined: 'last', cell: (c) => betaCell(c.row.original.mxBetaGrowthSurprise, 'macro.beta_growth', c.row.original), meta: { align: 'right', title: 'Model-based beta to the growth surprise index (per +100 points)' } },
      { id: 'sector', header: 'Sector', size: 150, accessorFn: (r) => r.sector, sortDescFirst: false, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
      { id: 'subsector', header: 'Subsector', size: 160, accessorFn: (r) => r.subsector, sortDescFirst: false, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
      { id: 'mcap', header: 'Mkt cap', size: 84, accessorFn: (r) => r.marketCapUsd, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span> : na('mkt_cap_usd', c.row.original)), meta: { align: 'right', title: 'Market capitalisation (USD)' } },
      { id: 'price', header: 'Price', size: 84, accessorFn: (r) => r.price, cell: (c) => <span className="num">{fmtPrice(c.getValue<number>(), c.row.original.currency)}</span>, meta: { align: 'right' } },
      { id: 'd1', header: '1D', size: 72, accessorFn: (r) => r.change1dPct, cell: (c) => <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 1, true)} />, meta: { align: 'right' } },
      ...factorCols,
      { id: 'r1m', header: '1M', size: 74, accessorFn: (r) => r.return1m, cell: (c) => <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 1, true)} />, meta: { align: 'right', title: '1-month total return' } },
      { id: 'r12m', header: '12M', size: 78, accessorFn: (r) => r.return12m, cell: (c) => <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 0, true)} />, meta: { align: 'right', title: '12-month total return' } },
      { id: 'pe', header: 'P/E', size: 62, accessorFn: (r) => r.peNtm, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtMultiple(c.getValue<number | null>())}</span> : na('pe_fwd', c.row.original)), meta: { align: 'right', title: 'P/E NTM' } },
      { id: 'evebit', header: 'EV/EBIT', size: 70, accessorFn: (r) => r.evEbit, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtMultiple(c.getValue<number | null>())}</span> : na('ev_ebit', c.row.original)), meta: { align: 'right' } },
      { id: 'fcfy', header: 'FCF yld', size: 68, accessorFn: (r) => r.fcfYield, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtPct(c.getValue<number | null>())}</span> : na('fcf_yield', c.row.original)), meta: { align: 'right' } },
      { id: 'roic', header: 'ROIC', size: 64, accessorFn: (r) => r.roic, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtPct(c.getValue<number | null>())}</span> : na('roic', c.row.original)), meta: { align: 'right' } },
      { id: 'growth', header: 'Rev g', size: 64, accessorFn: (r) => r.revenueGrowth, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtPct(c.getValue<number | null>())}</span> : na('revenue_growth_yoy', c.row.original)), meta: { align: 'right', title: 'Revenue growth LTM' } },
      { id: 'netDebt', header: 'ND/EBITDA', size: 80, accessorFn: (r) => r.netDebtEbitda, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtMultiple(c.getValue<number | null>())}</span> : na('net_debt_to_ebitda', c.row.original)), meta: { align: 'right' } },
      { id: 'divYld', header: 'Div yld', size: 64, accessorFn: (r) => r.dividendYield, cell: (c) => (fin(c.getValue<number | null>()) ? <span className="num">{fmtPct(c.getValue<number | null>())}</span> : na('div_yld', c.row.original)), meta: { align: 'right' } },
      { id: 'model', header: 'E[xs] 3M', size: 76, accessorFn: (r) => r.modelExpectedExcess, cell: (c) => (fin(c.getValue<number>()) ? <Delta value={c.getValue<number>()} format={(v) => fmtPct(v, 1, true)} showIcon={false} /> : na('ml.exp_excess_3m', c.row.original)), meta: { align: 'right', title: 'Model expected excess return, 63 trading days' } },
      { id: 'moat', header: 'Moat', size: 96, accessorFn: (r) => ({ wide: 2, narrow: 1, none: 0, insufficient: -1 })[r.moat], cell: (c) => <MoatBadge rating={c.row.original.moat} compact />, meta: { title: 'Moat classification' } },
      { id: 'dq', header: 'DQ', size: 52, accessorFn: (r) => ({ high: 2, medium: 1, low: 0 })[r.dataQuality], cell: (c) => <span className="text-xs text-muted capitalize">{c.row.original.dataQuality}</span>, meta: { title: 'Data quality' } },
    ]
  }, [weights, watch, toggleWatch, navigate])

  const exportCsv = () => {
    const headers = ['rank', 'id', 'ticker', 'name', 'isin', 'country', 'sector', 'subsector', 'market_cap_usd', 'composite', ...FACTOR_FAMILIES, 'return_1m', 'return_12m', 'pe_ntm', 'ev_ebit', 'fcf_yield', 'roic', 'revenue_growth', 'moat_rating', 'data_quality', 'grade']
    const body = filtered.map((r) => [r.liveRank, r.id, r.ticker, r.name, r.isin, r.country, r.sector, r.subsector, Math.round(r.marketCapUsd), Number.isFinite(r.score) ? r.score.toFixed(1) : '', ...FACTOR_FAMILIES.map((f) => (Number.isFinite(r.factorScores[f]) ? r.factorScores[f] : '')), r.return1m, r.return12m, r.peNtm, r.evEbit, r.fcfYield, r.roic, r.revenueGrowth, r.moat, r.dataQuality, r.grade ?? ''])
    downloadText(`big360-screener-${new Date().toISOString().slice(0, 10)}.csv`, toCsv(headers, body))
  }

  /** "Backtest this screen": the current filters / ranking re-evaluated point-in-time at every rebalance. */
  const backtestScreen = async () => {
    setBtBusy(true)
    try {
      const { body, notes } = screenRequest({
        regions: regionsToContract(region), country, sector, subsector: sub, bucket, moat, minScore, macroFilter: mx, text,
        sortId: sorting[0]?.id ?? null, sortDesc: sorting[0]?.desc ?? true, factorWeights: weightsToContract(weights),
      })
      const res = await backtestApi.fromScreen(body)
      const dropped = res.dropped.length ? [`dropped (no point-in-time history): ${res.dropped.join(', ')}`] : []
      openBacktest(res.spec, { origin: { kind: 'screener', label: 'from the Screener (rules evaluated point-in-time)', notes: [...res.notes, ...notes, ...dropped] } })
      navigate('/backtest')
    } catch (e) {
      toast({ tone: 'error', title: 'Could not build the backtest', message: e instanceof Error ? e.message : String(e) })
    } finally {
      setBtBusy(false)
    }
  }

  const reset = () => {
    setText('')
    setSector('')
    setSub('')
    setCountry('')
    setBucket('')
    setMoat('')
    setMinScore(0)
    setMx('')
    setSorting(DEFAULT_SORT)
  }

  const presetRatesDown = () => {
    reset()
    setMx('down100_pos')
    setVisibility((v) => ({ ...v, mxDown100: true, mxRates: true }))
    setSorting([{ id: 'mxDown100', desc: true }])
  }

  return (
    <Panel flush>
      <div className="flex flex-wrap items-center gap-2 border-b border-border p-2">
        <Input aria-label="Filter by ticker, name or ISIN" placeholder="Filter ticker / name / ISIN" value={text} onChange={(e) => setText(e.target.value)} className="w-48" />
        <Select label="Sector" value={sector} onChange={(v) => { setSector(v); setSub('') }} options={[{ value: '', label: 'All sectors' }, ...sectors.map((s) => ({ value: s, label: s }))]} className="w-44" />
        <Select label="Subsector" value={sub} onChange={setSub} options={[{ value: '', label: 'All subsectors' }, ...subs.map((s) => ({ value: s, label: s }))]} className="w-44" />
        <Select label="Country" value={country} onChange={setCountry} options={[{ value: '', label: 'All countries' }, ...countries.map((c) => ({ value: c, label: countryName(c) }))]} className="w-36" />
        <Select label="Market-cap bucket" value={bucket} onChange={setBucket} options={BUCKETS} className="w-36" />
        <Select label="Moat" value={moat} onChange={setMoat} options={[{ value: '', label: 'Any moat' }, { value: 'wide', label: 'Wide' }, { value: 'narrow', label: 'Narrow' }, { value: 'none', label: 'None' }, { value: 'insufficient', label: 'Insufficient data' }]} className="w-28" />
        <Select label="Macro filter (model-based)" value={mx} onChange={setMx} options={MACRO_FILTERS} className="w-44" />
        {(mx || MX_COLS.some((c) => visibility[c] !== false)) && <Badge tone="info">macro columns are model-based</Badge>}
        <label className="flex items-center gap-2 text-xs text-muted">
          Min score
          <Slider label="Minimum composite score" min={0} max={90} step={5} value={[minScore]} onValueChange={(v) => setMinScore(v[0])} className="w-24" />
          <span className="num w-5 text-fg">{minScore}</span>
        </label>
        <Button size="sm" variant="outline" onClick={presetRatesDown} title="Model-based: shows the impact of a 100bp parallel fall in rates, keeps names that gain, sorted best first">
          <Landmark /> Who benefits if rates fall 100bp
        </Button>
        <Button variant="ghost" size="sm" onClick={reset} aria-label="Reset filters">
          <RotateCcw /> Reset
        </Button>
        <label className="flex items-center gap-1.5 text-xs text-muted" title="Rows with one or more factors without a score (the composite is renormalised over the factors that have one)">
          <Checkbox label="Show incomplete rows" checked={showIncomplete} onCheckedChange={(v) => setShowIncomplete(v === true)} />
          Show incomplete <span className="num text-fg" data-testid="incomplete-count">({fmtInt(nIncomplete)})</span>
        </label>
        <div className="ml-auto flex items-center gap-2">
          {refreshing && <span className="text-2xs text-muted" role="status">re-selecting the top 1,000…</span>}
          <span className="num text-xs text-muted">
            {fmtInt(filtered.length)} / {fmtInt(rows.length)}
          </span>
          <WeightsPopover />
          <ColumnChooser columns={columns} visibility={visibility} onChange={setVisibility} />
          <Button size="sm" variant="outline" disabled={btBusy} onClick={() => void backtestScreen()} title="Backtest this screen: the filters and ranking re-evaluated point-in-time at each monthly rebalance (Backtest Lab)">
            <History /> Backtest this screen
          </Button>
          <Button
            size="sm"
            variant="outline"
            title="Open this screen in the BQL console (the filters and sort as a get / for / where query)"
            onClick={() => {
              const { query, notes } = screenerToBql({ regions: regionsToContract(region), country, sector, subsector: sub, bucket, moat, minScore, sortId: sorting[0]?.id ?? null, sortDesc: sorting[0]?.desc ?? true })
              if (notes.length) toast({ tone: 'info', title: 'Open as BQL', message: notes.join('; ') })
              navigate(`/bql?q=${encodeURIComponent(query)}&run=1`)
            }}
          >
            <Code2 /> Open as BQL
          </Button>
          <Button size="sm" variant="outline" onClick={exportCsv}>
            <Download /> CSV
          </Button>
        </div>
      </div>
      <DataTable
        data={filtered}
        columns={columns}
        ariaLabel="Screener results"
        height="max(420px, calc(100vh - 205px))"
        sorting={sorting}
        onSortingChange={setSorting}
        columnPinning={{ left: ['watch', 'rank', 'security'] }}
        columnVisibility={visibility}
        onColumnVisibilityChange={setVisibility}
        getRowId={(r) => r.id}
        onRowClick={(r) => navigate(`/company/${encodeURIComponent(r.id)}`)}
      />
    </Panel>
  )
}

function WeightsPopover() {
  const weights = useWeights((s) => s.weights)
  const setWeight = useWeights((s) => s.setWeight)
  const reset = useWeights((s) => s.reset)
  const total = FACTOR_FAMILIES.reduce((a, f) => a + weights[f], 0)
  return (
    <Popover
      className="w-80"
      trigger={
        <Button size="sm" variant="outline" aria-label="Adjust factor weights">
          <SlidersHorizontal /> Weights
        </Button>
      }
    >
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm font-semibold">Factor weights</div>
        <Button size="xs" variant="ghost" onClick={reset}>
          <RotateCcw /> Defaults
        </Button>
      </div>
      <p className="mb-3 text-xs text-muted">Weights are normalised; ranking updates as you drag and the top 1,000 is re-selected on the server when you stop.</p>
      <ul className="space-y-2.5">
        {FACTOR_FAMILIES.map((f: FactorFamily) => (
          <li key={f} className="grid grid-cols-[92px_1fr_44px] items-center gap-3">
            <span className="truncate text-xs text-fg">{FACTOR_LABEL[f]}</span>
            <Slider label={`${FACTOR_LABEL[f]} weight`} min={0} max={30} step={1} value={[weights[f]]} onValueChange={(v) => setWeight(f, v[0])} />
            <span className="num text-right text-xs text-muted">{total ? `${Math.round((weights[f] / total) * 100)}%` : '0%'}</span>
          </li>
        ))}
      </ul>
    </Popover>
  )
}

function ColumnChooser({ columns, visibility, onChange }: { columns: ColumnDef<Ranked>[]; visibility: VisibilityState; onChange: (v: VisibilityState) => void }) {
  const fixed = new Set(['watch', 'rank', 'security', 'score'])
  return (
    <Popover
      className="w-64"
      trigger={
        <Button size="sm" variant="outline" aria-label="Choose columns">
          <Columns3 /> Columns
        </Button>
      }
    >
      <div className="mb-2 text-sm font-semibold">Visible columns</div>
      <ul className="grid grid-cols-2 gap-x-3 gap-y-1.5">
        {columns
          .filter((c) => c.id && !fixed.has(c.id))
          .map((c) => {
            const id = c.id as string
            const on = visibility[id] !== false
            const label = c.meta?.title ?? (typeof c.header === 'string' ? c.header : id)
            return (
              <li key={id}>
                <label className="flex cursor-pointer items-center gap-2 text-xs">
                  <Checkbox label={label} checked={on} onCheckedChange={(v) => onChange({ ...visibility, [id]: v === true })} />
                  <span className="truncate">{typeof c.header === 'string' ? c.header : id}</span>
                </label>
              </li>
            )
          })}
      </ul>
    </Popover>
  )
}
