import { ExternalLink, FlaskConical, History, LineChart, Users } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router'
import {
  avgOffDiag,
  cellBackground,
  cellText,
  coverageOrder,
  COVERAGE_TONE,
  fmtDay,
  fmtPctS,
  fmtSignalRaw,
  fmtZ,
  groupRows,
  matrixCells,
  positioningLine,
  regimeTone,
  shapeShares,
  sortedComponents,
  trimLeading,
} from '@/api/crossasset/adapters'
import { useXaAsset, useXaCockpit, useXaCorrelation, useXaCurve, useXaCurves, useXaRisk } from '@/api/crossasset/hooks'
import { CLASS_LABEL, COT_REPORT_LABEL, REGIME_LABEL, SNAPSHOT_LABEL, type XaClass, type XaCockpit, type XaCockpitRow, type XaCorrelation, type XaCurve, type XaRisk, type XaSnapshotLabel, type XaWindow } from '@/api/crossasset/types'
import { type PriceRange, rangeStart } from '@/api/prices/range'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryView, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, getFormatLocale } from '@/lib/format'
import { crossAssetCellReason, reasonText } from '@/lib/availability'
import { cn } from '@/lib/utils'
import { useStudio } from '@/pages/studio/store'
import { crossAssetSpec } from '@/api/backtest/adapters'
import { useBacktest } from '@/pages/backtest/store'
import { MacroNav } from '../macro/shared'
import { Lines, type StackPanel, StackedPanels } from '../positioning/charts'
import { ComponentBars, CorrMatrix, CurveChart, RiskGauge } from './charts'
import { crossAssetDraft } from './draft'
import { CreateThesisLink } from '@/pages/theses/shared'

const CLASSES: ('all' | XaClass)[] = ['all', 'commodity', 'fx', 'equity_index', 'rates', 'credit', 'crypto']
const RANGES: PriceRange[] = ['1Y', '5Y', '10Y', 'Max']
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pct1 = (v: number) => fmtPctS(v)
const num2 = (v: number) => v.toFixed(2)

/** Markets → Cross-asset: signal heatmap, futures curve viewer, correlation regime and the risk-on / risk-off gauge. */
export default function CrossAsset() {
  const [params, setParams] = useSearchParams()
  const ac = (CLASSES as string[]).includes(params.get('class') ?? '') ? (params.get('class') as 'all' | XaClass) : 'all'
  const set = (k: string, v: string) =>
    setParams(
      (p) => {
        p.set(k, v)
        return p
      },
      { replace: true },
    )
  const ck = useXaCockpit(ac === 'all' ? undefined : ac)
  const all = useXaCockpit()
  const selected = params.get('asset') ?? ck.data?.rows[0]?.asset_id ?? null
  const selRow = all.data?.rows.find((r) => r.asset_id === selected) ?? null
  const root = params.get('root') ?? selRow?.futures_root ?? 'CL'
  return (
    <>
      <PageHeader title="Cross-asset" subtitle="Trend, carry, value, curve, volatility and CFTC positioning per asset class — with the correlation regime and a model-based risk-on / risk-off gauge" />
      <MacroNav />
      <div className="space-y-3">
        <Panel>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
            <nav aria-label="Asset classes" className="flex flex-wrap gap-1">
              {CLASSES.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => set('class', c)}
                  aria-pressed={ac === c}
                  className={cn('h-6 rounded-md border px-2 text-xs font-medium', ac === c ? 'border-accent bg-surface-3 text-fg' : 'border-border text-muted hover:text-fg')}
                >
                  {c === 'all' ? 'All' : CLASS_LABEL[c]}
                  {c !== 'all' && all.data ? <span className="ml-1 text-faint">{all.data.asset_classes.find((x) => x.id === c)?.n ?? 0}</span> : null}
                </button>
              ))}
            </nav>
            <span className="ml-auto text-2xs text-muted">
              {ck.data?.as_of ? `Signals as of ${fmtDay(ck.data.as_of)}` : ''}
              {ck.data?.positioning.report_date ? ` · Positioning: CFTC (official), as of report date ${fmtDay(ck.data.positioning.report_date)}` : ''}
            </span>
          </div>
        </Panel>
        <div className="grid gap-3 2xl:grid-cols-[minmax(0,1fr)_400px]">
          <Panel title="Signal heatmap" subtitle="Time-series z-scores (3-year window) · blue = high, orange = low · click a row for details" flush>
            <QueryView query={ck} skeleton={<Skeleton className="m-3 h-[520px]" />}>
              {(d) => <Heatmap d={d} selected={selected} onSelect={(id) => set('asset', id)} />}
            </QueryView>
          </Panel>
          <div className="min-w-0 space-y-3">
            <RiskPanel />
            {selected && <AssetPanel id={selected} row={selRow} onCurve={(r) => set('root', r)} />}
          </div>
        </div>
        <CurvePanel root={root} onRoot={(r) => set('root', r)} />
        <CorrelationPanel />
        <RiskHistoryPanel />
        <p className="text-2xs text-muted">
          Sources: futures curves and futures / index / ETF prices are Yahoo market data (expiry dates approximate); FX from official fixings (ECB, Fed H.10, central banks); US Treasuries are a synthetic index from official H.15 CMT yields; OAS
          and the broad dollar from FRED; positioning from the CFTC Commitments of Traders (official), point-in-time on the release date. Trend signal, curve shape, value proxies, z-scores and the risk composite are model-based
          transformations, not published statistics. Definitions: docs/CROSS_ASSET_DEFINITIONS.md.
        </p>
      </div>
    </>
  )
}

/* ───────────── heatmap ───────────── */

function Heatmap({ d, selected, onSelect }: { d: XaCockpit; selected: string | null; onSelect: (id: string) => void }) {
  if (!d.rows.length) return <EmptyState title="No assets" message={d.data_status === 'pending' ? 'Cross-asset signals have not been computed yet.' : 'No asset in the selected class.'} className="m-3" />
  const groups = groupRows(d.rows)
  return (
    <div>
      <div className="flex flex-wrap items-center gap-3 px-3 py-2 text-2xs text-muted">
        <span className="inline-flex items-center gap-2" aria-label="Colour scale">
          <span>−2.5σ</span>
          <span className="h-2 w-28 rounded-sm" style={{ background: 'linear-gradient(90deg, color-mix(in srgb, var(--c1) 62%, var(--surface)), var(--surface), color-mix(in srgb, var(--c2) 62%, var(--surface)))' }} />
          <span>+2.5σ</span>
        </span>
        <span>Trend = model-based score in [−1, 1]; positioning = CFTC speculator net z (COT index in the tooltip)</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[820px] border-separate border-spacing-[2px] text-xs" aria-label="Cross-asset signal heatmap">
          <thead>
            <tr className="text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-2 py-1">Asset</th>
              {d.signals.map((s) => (
                <th key={s.id} className="px-1 py-1 text-center">
                  <Tip content={`${s.description}${s.model_based ? ' (model-based)' : ''}`}>
                    <span className="cursor-help">{s.label}</span>
                  </Tip>
                </th>
              ))}
              <th className="px-2 py-1">Carry / curve</th>
              <th className="px-2 py-1 text-right">As of</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <HeatGroup key={g.asset_class} label={g.label} rows={g.rows} d={d} selected={selected} onSelect={onSelect} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function HeatGroup({ label, rows, d, selected, onSelect }: { label: string; rows: XaCockpitRow[]; d: XaCockpit; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <>
      <tr>
        <td colSpan={d.signals.length + 3} className="px-2 pt-2 pb-0.5 text-2xs font-semibold tracking-wide text-muted uppercase">
          {label}
        </td>
      </tr>
      {rows.map((r) => (
        <tr key={r.asset_id} className={cn(selected === r.asset_id && 'bg-surface-3')}>
          <td className="max-w-[210px] truncate px-2 py-0.5">
            <button type="button" onClick={() => onSelect(r.asset_id)} className="truncate text-left font-medium text-accent-text hover:underline" title={r.name}>
              {r.name.replace(' (official fixings)', '')}
            </button>
          </td>
          {r.cells.map((c, i) => {
            const s = d.signals[i]
            const tip =
              s.id === 'positioning'
                ? r.positioning
                  ? `${r.name}: COT index 3y ${fmtSignalRaw('positioning', c.raw)} · z ${fmtZ(c.z)} · ${positioningLine(r.positioning)}`
                  : `${r.name}: no CFTC market`
                : `${r.name} · ${s.label}: ${fmtSignalRaw(s.id, c.raw, r)}${s.z_field ? ` · z ${fmtZ(c.z)}` : ''}${s.id === 'carry' && r.carry_label ? ` · ${r.carry_label}` : ''}${s.id === 'value' && r.value_label ? ` · ${r.value_label}` : ''}`
            const v = s.scale === 'score' ? c.raw : c.z
            // completeness C: an empty cell says why (no CFTC market, no carry definition, too little history)
            const empty = (c.raw == null && c.z == null) || (s.id === 'positioning' && !r.positioning)
            const tipText = empty ? `${r.name} · ${s.label}: ${reasonText(crossAssetCellReason(s.id, !!r.positioning))}` : tip
            return (
              <td key={s.id} className="p-0" data-reason={empty ? crossAssetCellReason(s.id, !!r.positioning).code : undefined}>
                <Tip content={tipText}>
                  <div className="num flex h-6 min-w-[54px] items-center justify-center rounded-sm text-fg" style={{ background: cellBackground(v, s.scale) }}>
                    {cellText(s, c, r)}
                  </div>
                </Tip>
              </td>
            )
          })}
          <td className="px-2 py-0.5 text-2xs whitespace-nowrap text-muted">
            {r.carry_method ? r.carry_method.replace(/_/g, ' ') : '—'}
            {r.curve_shape && (
              <Badge tone={r.curve_shape === 'backwardation' ? 'info' : r.curve_shape === 'contango' ? 'accent' : 'neutral'} className="ml-1" title="Model-based curve shape label (Yahoo curve)">
                {r.curve_shape}
              </Badge>
            )}
          </td>
          <td className={cn('num px-2 py-0.5 text-right text-2xs whitespace-nowrap', r.stale ? 'text-warn' : 'text-muted')} title={r.stale ? 'Latest row older than 10 days' : undefined}>
            {r.date}
          </td>
        </tr>
      ))}
    </>
  )
}

/* ───────────── selected asset ───────────── */

function AssetPanel({ id, row, onCurve }: { id: string; row: XaCockpitRow | null; onCurve: (root: string) => void }) {
  const [range, setRange] = useState<PriceRange>('5Y')
  const q = useXaAsset(id, rangeStart(range))
  const navigate = useNavigate()
  const setDraft = useStudio((s) => s.setDraft)
  const setTab = useStudio((s) => s.setTab)
  const setRun = useStudio((s) => s.setRun)
  const openBacktest = useBacktest((s) => s.open)
  const [btSignal, setBtSignal] = useState<'trend_signal' | 'carry' | 'value' | 'cot_index_3y'>('trend_signal')
  return (
    <Panel title={row?.name ?? id} subtitle={row ? `${CLASS_LABEL[row.asset_class]}${row.sector ? ` · ${row.sector}` : ''}` : undefined} actions={<Segmented label="History range" size="xs" value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: r }))} />}>
      <QueryView query={q} skeleton={<SkeletonRows rows={6} />}>
        {(d) => {
          const panels: StackPanel[] = []
          const s = d.series
          const has = (k: string) => (s[k] ?? []).some((v) => fin(v))
          if (has('trend_signal')) panels.push({ title: 'Trend signal (model-based)', series: [{ name: 'Trend', data: s.trend_signal, color: 0 }], fmt: num2, min: -1, max: 1 })
          if (has('carry')) panels.push({ title: `Carry · ${d.carry_method?.replace(/_/g, ' ') ?? ''}`, series: [{ name: 'Carry', data: s.carry, color: 1 }], fmt: pct1 })
          if (has('cot_index_3y')) panels.push({ title: 'CFTC COT index 3y', series: [{ name: 'COT index', data: s.cot_index_3y, color: 2 }], fmt: (v) => Math.round(v * 100).toString(), min: 0, max: 1, bands: [{ from: 0.9, to: 1, tone: 'hi' }, { from: 0, to: 0.1, tone: 'lo' }] })
          const openStudio = () => {
            setDraft(crossAssetDraft(d.asset, !!d.positioning, has('carry')))
            setRun(null)
            setTab('builder')
            navigate('/studio')
          }
          return (
            <div className="space-y-2">
              <div className="flex flex-wrap gap-x-5 gap-y-2">
                <Stat label="Trend" value={fmtSignalRaw('trend', d.latest?.trend_signal as number | undefined)} sub="model-based" />
                <Stat label="Mom 12m" value={fmtPctS(d.latest?.mom_12m as number | undefined)} />
                <Stat label="Carry" value={fmtPctS(d.latest?.carry as number | undefined)} sub="p.a." />
                <Stat label="Vol 1m" value={fmtSignalRaw('vol', d.latest?.rv_1m as number | undefined)} />
              </div>
              <dl className="grid grid-cols-[88px_minmax(0,1fr)] gap-x-2 gap-y-0.5 text-2xs">
                <dt className="text-muted">Carry method</dt>
                <dd className="text-fg">{d.carry_label ?? '—'}</dd>
                <dt className="text-muted">Value method</dt>
                <dd className="text-fg">{d.value_label ?? '—'}</dd>
                <dt className="text-muted">Positioning</dt>
                <dd className="text-fg">
                  {positioningLine(d.positioning)}
                  {d.positioning ? ` · ${COT_REPORT_LABEL[d.positioning.report_type] ?? d.positioning.report_type}` : ''}
                </dd>
              </dl>
              {panels.length > 0 && d.dates.length > 1 ? <StackedPanels dates={d.dates} panels={panels} panelHeight={70} ariaLabel={`${d.asset.name} signal history`} /> : <p className="text-xs text-muted">No signal history in this range.</p>}
              <div className="flex flex-wrap items-center gap-2">
                {d.asset.instrument_id && (
                  <Link to={`/instruments/${encodeURIComponent(d.asset.instrument_id)}`} className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-fg hover:bg-surface-2">
                    <ExternalLink className="size-3.5" /> Instrument page
                  </Link>
                )}
                {d.positioning && (
                  <Link to={`/macro/positioning/${encodeURIComponent(d.positioning.market_code)}`} className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-fg hover:bg-surface-2">
                    <Users className="size-3.5" /> CFTC market
                  </Link>
                )}
                {d.has_curve && d.asset.futures_root && (
                  <button type="button" onClick={() => onCurve(d.asset.futures_root as string)} className="inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2.5 text-xs font-medium text-fg hover:bg-surface-2">
                    <LineChart className="size-3.5" /> Curve {d.asset.futures_root}
                  </button>
                )}
                <span className="ml-auto inline-flex items-center gap-1">
                  <Select label="Signal to backtest" value={btSignal} onChange={setBtSignal} options={[{ value: 'trend_signal', label: 'Trend' }, { value: 'carry', label: 'Carry' }, { value: 'value', label: 'Value' }, { value: 'cot_index_3y', label: 'CFTC COT index' }]} />
                  <Button
                    variant="outline"
                    size="sm"
                    title="Backtest this signal cross-sectionally across the asset's class (Backtest Lab)"
                    onClick={() => {
                      const label = { trend_signal: 'trend', carry: 'carry', value: 'value', cot_index_3y: 'CFTC positioning (contrarian)' }[btSignal]
                      openBacktest(crossAssetSpec(btSignal, label, d.asset.asset_class), {
                        origin: { kind: 'cross_asset', ref: d.asset.asset_id, label: `from Cross-asset · ${d.asset.name}`, notes: [`long-short ${label} across the ${d.asset.asset_class.replace('_', ' ')} assets (inverse-vol weights)`, 'spot-like prices: futures front months include roll gaps; trend / value / z metrics are model-based'] },
                      })
                      navigate('/backtest')
                    }}
                  >
                    <History /> Backtest this signal
                  </Button>
                </span>
                <CreateThesisLink link={{ subjectType: 'cross_asset', subjectId: d.asset.asset_id, origin: 'cross_asset', ref: d.asset.asset_id }} title="Create a thesis on this asset (thesis tracker)" />
                <Button variant="primary" size="sm" onClick={openStudio}>
                  <FlaskConical /> Open in Model Studio
                </Button>
              </div>
            </div>
          )
        }}
      </QueryView>
    </Panel>
  )
}

/* ───────────── curve viewer ───────────── */

function CurvePanel({ root, onRoot }: { root: string; onRoot: (r: string) => void }) {
  const list = useXaCurves()
  const q = useXaCurve(root)
  const options = (list.data?.items ?? []).filter((x) => x.last_date).map((x) => ({ value: x.root, label: `${x.root} · ${x.name}` }))
  return (
    <Panel
      title="Futures curve viewer"
      subtitle="Term structure now vs ~1 month and ~1 year ago · Yahoo market data, expiry dates approximate"
      actions={options.length ? <Select label="Futures root" value={root} onChange={onRoot} options={options} className="w-56" /> : undefined}
    >
      <QueryView query={q} skeleton={<Skeleton className="h-[320px]" />}>
        {(d) => <CurveBody d={d} />}
      </QueryView>
    </Panel>
  )
}

function CurveBody({ d }: { d: XaCurve }) {
  if (!d.last_date) return <EmptyState title="No curve stored" message={`Yahoo served no contract months for ${d.root} yet.`} />
  const latest = d.snapshots.find((s) => s.label === 'latest')
  // dates before the first fitted slope hold far-dated contracts only (Yahoo serves live contracts): start the history there
  const h = trimLeading(d.dates, d.series, d.curve_shape, 'curve_slope')
  const sh = shapeShares(h.shapes)
  const panels: StackPanel[] = [
    { title: 'Curve slope (+ = contango, %/yr)', series: [{ name: 'Slope', data: h.series.curve_slope ?? [], kind: 'signed-bar' }], fmt: pct1 },
    { title: 'Roll yield front vs next (+ = backwardation, %/yr)', series: [{ name: 'Roll yield', data: h.series.roll_yield ?? [], color: 1 }], fmt: pct1 },
  ]
  if ((h.series.front_close ?? []).some(fin)) panels.unshift({ title: 'Front contract (≥ 5 days to expiry)', series: [{ name: 'Front', data: h.series.front_close ?? [], color: 0 }], fmt: (v) => v.toLocaleString(getFormatLocale(), { maximumSignificantDigits: 6 }) })
  const cov = coverageOrder(d.coverage)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
        <Stat label="Curve date" value={fmtDay(d.last_date)} sub={`${latest?.n_contracts ?? 0} contracts`} />
        <Stat label="Shape now" value={d.curve_shape[d.curve_shape.length - 1] ?? '—'} sub="model-based label" />
        <Stat label="Curve history" value={`${h.dates.length} days`} sub={sh.n ? `contango ${Math.round(sh.contango * 100)}% · backwardation ${Math.round(sh.backwardation * 100)}%` : undefined} />
        <Stat label="Coverage" value={`${d.coverage_counts.served} served`} sub={`${d.coverage_counts.missing} missing · ${d.coverage_counts.unchecked} unchecked`} />
      </div>
      <div className="grid gap-3 xl:grid-cols-2">
        <div className="min-w-0">
          <CurveChart snapshots={d.snapshots} ariaLabel={`${d.root} term structure now vs 1 month and 1 year ago`} />
          <p className="mt-1 text-2xs text-muted">
            {d.snapshots
              .filter((s) => s.label !== 'latest')
              .map((s) => `${SNAPSHOT_LABEL[s.label as XaSnapshotLabel]}: ${s.date ? `${fmtDay(s.date)}, ${s.n_contracts} contracts` : 'no stored curve'}`)
              .join(' · ')}
          </p>
        </div>
        <div className="min-w-0">{h.dates.length > 1 ? <StackedPanels dates={h.dates} panels={panels} panelHeight={64} ariaLabel={`${d.root} contango and backwardation history`} /> : null}</div>
      </div>
      <div>
        <div className="label-caps mb-1">Contract coverage (futures_contracts)</div>
        <div className="flex flex-wrap gap-1">
          {cov.slice(0, 40).map((c) => (
            <Tip key={c.symbol} content={`${c.symbol} · ${c.status}${c.first_date ? ` · bars ${c.first_date} → ${c.last_date}` : ''} · expiry ≈ ${c.expiry}`}>
              <span>
                <Badge tone={COVERAGE_TONE[c.status]} className={cn(c.status === 'expired' && 'opacity-60')}>
                  {c.contract} {c.status === 'served' ? '' : c.status}
                </Badge>
              </span>
            </Tip>
          ))}
        </div>
      </div>
      <p className="text-2xs text-muted">
        {d.labels.curve_source}. {d.labels.curve_history} Missing months are recorded gaps, never interpolated.{d.expiry_rule ? ` Expiry rule: ${d.expiry_rule}.` : ''} {d.definitions.curve_shape}.
      </p>
    </div>
  )
}

/* ───────────── correlation regime ───────────── */

function CorrelationPanel() {
  const [win, setWin] = useState<XaWindow>(120)
  const [snap, setSnap] = useState<XaSnapshotLabel>('latest')
  const q = useXaCorrelation(win)
  return (
    <Panel
      title="Correlation regime"
      subtitle="Eigen-concentration of a fixed 12-asset basket — share of variance in the first factor ('everything moving together')"
      actions={
        <Segmented
          label="Window"
          size="xs"
          value={String(win) as '60' | '120'}
          onChange={(v) => setWin(Number(v) as XaWindow)}
          options={[
            { value: '60', label: '60d' },
            { value: '120', label: '120d' },
          ]}
        />
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-[380px]" />}>
        {(d) => <CorrBody d={d} snap={snap} setSnap={setSnap} />}
      </QueryView>
    </Panel>
  )
}

function CorrBody({ d, snap, setSnap }: { d: XaCorrelation; snap: XaSnapshotLabel; setSnap: (s: XaSnapshotLabel) => void }) {
  const names = d.basket.map((b) => b.asset_id)
  const m = d.matrices.find((x) => x.snapshot === snap) ?? d.matrices[0]
  const cells = useMemo(() => (m ? matrixCells(m.values) : []), [m])
  const key = d.window === 60 ? 'eig1_share_60' : 'eig1_share_120'
  const avg = (s: XaSnapshotLabel) => {
    const x = d.matrices.find((y) => y.snapshot === s)
    return x ? avgOffDiag(x.values) : null
  }
  if (!d.dates.length) return <EmptyState title="No regime history" message="The correlation regime has not been computed yet." />
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,460px)]">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Stat label={`First-factor share ${d.window}d`} value={fin(d.latest?.[key]) ? `${Math.round((d.latest?.[key] as number) * 100)}%` : '—'} sub={d.min_share ? `floor 1/N = ${Math.round(d.min_share * 100)}%` : undefined} />
          <Stat label="z (120d, 3y)" value={fmtZ(d.latest?.eig1_z_120 as number | undefined)} />
          <Stat label="Avg corr now / 1m / 1y" value={['latest', '1m', '1y'].map((s) => (fin(avg(s as XaSnapshotLabel)) ? (avg(s as XaSnapshotLabel) as number).toFixed(2) : '—')).join(' / ')} />
        </div>
        <Lines
          dates={d.dates}
          series={[
            { name: 'First-factor share 60d', data: d.series.eig1_share_60 ?? [], color: 0 },
            { name: 'First-factor share 120d', data: d.series.eig1_share_120 ?? [], color: 1 },
          ]}
          fmt={(v) => `${Math.round(v * 100)}%`}
          height={260}
          ariaLabel="Eigen-concentration history"
        />
        <p className="text-2xs text-muted">{d.definition}</p>
      </div>
      <div className="min-w-0">
        <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
          <Segmented label="Matrix date" size="xs" value={snap} onChange={setSnap} options={d.matrices.map((x) => ({ value: x.snapshot as XaSnapshotLabel, label: SNAPSHOT_LABEL[x.snapshot as XaSnapshotLabel] }))} />
          <span className="text-2xs text-muted">{m ? `window ending ${fmtDay(m.as_of)} · ${m.n_obs} days` : ''}</span>
        </div>
        {m ? <CorrMatrix names={names} cells={cells} ariaLabel={`Correlation matrix ${SNAPSHOT_LABEL[snap]}`} /> : <EmptyState title="No matrix" />}
      </div>
    </div>
  )
}

/* ───────────── risk gauge ───────────── */

function RiskPanel() {
  const q = useXaRisk()
  return (
    <Panel title="Risk-on / risk-off" subtitle="Model-based composite of 8 market measures (z-scores, + = risk-on)">
      <QueryView query={q} skeleton={<SkeletonRows rows={6} />}>
        {(d) => <RiskBody d={d} />}
      </QueryView>
    </Panel>
  )
}

function RiskBody({ d }: { d: XaRisk }) {
  const l = d.latest
  if (!l) return <EmptyState title="No composite yet" message="The risk composite needs at least 4 of its 8 components." />
  const tone = regimeTone(l.risk_regime)
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-4">
        <RiskGauge value={l.risk_composite} size={180} />
        <div className="space-y-1">
          <div className="num text-2xl font-semibold text-fg">{fin(l.risk_composite) ? `${l.risk_composite > 0 ? '+' : ''}${l.risk_composite.toFixed(2)}` : '—'}</div>
          <Badge tone={tone === 'up' ? 'info' : tone === 'down' ? 'accent' : 'neutral'}>{REGIME_LABEL[l.risk_regime ?? ''] ?? '—'} · model-based</Badge>
          <div className="text-2xs text-muted">
            {fmtDay(l.date)} · {l.n_components}/8 components{fin(l.change_1m) ? ` · 1m change ${l.change_1m > 0 ? '+' : ''}${l.change_1m.toFixed(2)}` : ''}
          </div>
        </div>
      </div>
      <ComponentBars components={sortedComponents(l)} ariaLabel="Risk composite components" />
      <p className="text-2xs text-muted">{d.definition}</p>
    </div>
  )
}

function RiskHistoryPanel() {
  const q = useXaRisk()
  return (
    <Panel title="Risk composite history" subtitle="Model-based · shaded: risk-on above +0.5, risk-off below −0.5">
      <QueryView query={q} skeleton={<Skeleton className="h-[260px]" />}>
        {(d) =>
          d.dates.length ? (
            <div className="space-y-2">
              <StackedPanels
                dates={d.dates}
                panels={[{ title: 'Risk composite', series: [{ name: 'Composite', data: d.risk_composite, color: 0 }], fmt: num2, bands: [{ from: 0.5, to: 3, tone: 'hi' }, { from: -3, to: -0.5, tone: 'lo' }], min: -3, max: 3 }]}
                panelHeight={130}
                ariaLabel="Risk composite history"
              />
              <div className="grid gap-3 xl:grid-cols-2">
                {[0, 4].map((k) => (
                  <div key={k} className="min-w-0">
                    <div className="label-caps mb-1">Components: {d.components.slice(k, k + 4).map((c) => c.label).join(', ') || DASH} (z, + = risk-on)</div>
                    <Lines dates={d.dates} series={d.components.slice(k, k + 4).map((c, i) => ({ name: c.label, data: c.values ?? [], color: i }))} fmt={num2} height={200} ariaLabel={`Risk components ${k + 1}-${k + 4}`} />
                  </div>
                ))}
              </div>
            </div>
          ) : (
            <EmptyState title="No history" />
          )
        }
      </QueryView>
    </Panel>
  )
}
