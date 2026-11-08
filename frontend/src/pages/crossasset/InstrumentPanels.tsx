import { Link } from 'react-router'
import { fmtDay, fmtPctS, positioningLine, trimLeading } from '@/api/crossasset/adapters'
import { useXaAsset, useXaCockpit, useXaCurve } from '@/api/crossasset/hooks'
import type { XaCockpitRow } from '@/api/crossasset/types'
import { rangeStart } from '@/api/prices/range'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/feedback'
import { Panel, Stat } from '@/components/ui/panel'
import { type StackPanel, StackedPanels } from '../positioning/charts'
import { CurveChart } from './charts'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pct1 = (v: number) => fmtPctS(v)

/** Instrument page: carry and futures-curve panels for instruments linked to a cross-asset asset (shown only when data exists). */
export function InstrumentCrossAsset({ securityId }: { securityId: string }) {
  const ck = useXaCockpit(undefined, securityId)
  const row = ck.data?.rows[0]
  if (!row) return null
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      <CarryPanel row={row} />
      {row.futures_root && <CurvePanel root={row.futures_root} />}
    </div>
  )
}

function CarryPanel({ row }: { row: XaCockpitRow }) {
  const q = useXaAsset(row.asset_id, rangeStart('5Y'))
  const d = q.data
  const panels: StackPanel[] = []
  let dates: string[] = []
  if (d) {
    const has0 = (k: string) => (d.series[k] ?? []).some((v) => fin(v))
    // futures roll carry only exists while the curve has a front contract (recent history for most roots): start there
    const h = trimLeading(d.dates, d.series, d.curve_shape, has0('carry') ? 'carry' : 'carry_curve')
    dates = h.dates
    const s = h.series
    const has = (k: string) => (s[k] ?? []).some((v) => fin(v))
    if (has('carry')) panels.push({ title: `Carry · ${(d.carry_method ?? '').replace(/_/g, ' ')} (annualised)`, series: [{ name: 'Carry', data: s.carry ?? [], color: 0 }], fmt: pct1 })
    if (has('carry_12m') || has('carry_curve'))
      panels.push({
        title: 'Futures roll yield (Yahoo curve)',
        series: [
          { name: 'Front vs next', data: s.carry_curve ?? [], color: 1 },
          { name: '12m (seasonality-neutral)', data: s.carry_12m ?? [], color: 2 },
        ],
        fmt: pct1,
      })
  }
  return (
    <Panel
      title="Carry"
      subtitle={`Cross-asset signals · ${row.name}`}
      actions={
        <Link to={`/macro/cross-asset?asset=${encodeURIComponent(row.asset_id)}`} className="text-xs text-accent-text hover:underline">
          Cross-asset cockpit
        </Link>
      }
    >
      {!d ? (
        <Skeleton className="h-[220px]" />
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-x-5 gap-y-2">
            <Stat label="Carry" value={fmtPctS(d.latest?.carry as number | undefined)} sub="p.a." />
            <Stat label="Roll yield" value={fmtPctS(d.latest?.carry_curve as number | undefined)} sub={d.latest?.carry_front_contract ? `front ${String(d.latest.carry_front_contract)}` : undefined} />
            <Stat label="Curve" value={(d.latest?.curve_shape as string | undefined) ?? '—'} sub="model-based label" />
          </div>
          <p className="text-2xs text-muted">
            {d.carry_label ?? 'No carry definition for this asset.'} · {positioningLine(d.positioning)}
          </p>
          {panels.length > 0 && dates.length > 1 ? <StackedPanels dates={dates} panels={panels} panelHeight={80} ariaLabel={`${row.name} carry history`} /> : <p className="text-xs text-muted">No carry history.</p>}
        </div>
      )}
    </Panel>
  )
}

function CurvePanel({ root }: { root: string }) {
  const q = useXaCurve(root)
  const d = q.data
  if (q.isSuccess && !d?.last_date) return null
  return (
    <Panel
      title={`Futures curve · ${root}`}
      subtitle="Now vs ~1m and ~1y ago · Yahoo market data, expiry dates approximate"
      actions={
        <Link to={`/macro/cross-asset?root=${encodeURIComponent(root)}`} className="text-xs text-accent-text hover:underline">
          Curve viewer
        </Link>
      }
    >
      {!d ? (
        <Skeleton className="h-[260px]" />
      ) : (
        <div className="space-y-2">
          <CurveChart snapshots={d.snapshots} height={240} ariaLabel={`${root} term structure`} />
          <div className="flex flex-wrap items-center gap-1 text-2xs text-muted">
            <span>Curve {fmtDay(d.last_date)} ·</span>
            <Badge tone="up">{d.coverage_counts.served} served</Badge>
            {d.coverage_counts.missing > 0 && <Badge tone="down">{d.coverage_counts.missing} missing</Badge>}
            {d.coverage_counts.unchecked > 0 && <Badge tone="warn">{d.coverage_counts.unchecked} unchecked</Badge>}
          </div>
          <p className="text-2xs text-muted">{d.labels.curve_history}</p>
        </div>
      )}
    </Panel>
  )
}
