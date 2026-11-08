import { Link } from 'react-router'
import type { CreditSpread, MacroIndicator, RegionCode } from '@/api/types'
import { Sparkline } from '@/components/charts/Sparkline'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { PendingState } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtBps, fmtDate, fmtDateShort, fmtNum, fmtValue } from '@/lib/format'
import { REGION_LABEL } from '@/lib/regions'
import { cn } from '@/lib/utils'
import { headlineTiles, sparkWindowLabel } from './helpers'

/* ── credit ── */

export function CreditPanel({ spreads, className }: { spreads: CreditSpread[]; className?: string }) {
  const windows = [...new Set(spreads.map((s) => s.percentileWindowYears).filter((x): x is number => x !== null))]
  const pctHead = windows.length === 1 ? `pctile (${fmtNum(windows[0], windows[0] % 1 ? 1 : 0)}y)` : 'pctile'
  const sparkDays = spreads.find((s) => s.sparkWindowDays)?.sparkWindowDays ?? null
  const legacy = spreads.length > 0 && spreads.every((s) => !Number.isFinite(s.change1mBps) && !Number.isFinite(s.percentile))
  return (
    <Panel
      title="Credit spreads"
      subtitle={legacy ? 'Change vs previous observation only (calendar 1M change and percentile need API 1.29)' : 'OAS unless marked · 1M = calendar · empirical percentile over the window shown · widening in rose'}
      className={className}
      flush
    >
      {spreads.length === 0 ? (
        <PendingState compact detail="No credit-spread series in this snapshot." />
      ) : (
        <table className="w-full text-sm" aria-label="Credit spreads">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Index</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold whitespace-nowrap">Level</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold whitespace-nowrap">1D chg</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold whitespace-nowrap">1M chg</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold" title="Empirical percentile of the latest level within the window">{pctHead}</th>
              <th scope="col" className="border-b border-border px-3 text-right font-semibold">{sparkWindowLabel(sparkDays, spreads[0]?.history.length ?? 0)}</th>
            </tr>
          </thead>
          <tbody>
            {spreads.map((c) => (
              <tr key={c.id} className="hover:bg-surface-2">
                <td className="h-8 w-full max-w-0 border-b border-border px-3 text-fg">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span className="truncate" title={`${c.name} · ${c.id} · as of ${fmtDate(c.asOf)}`}>{c.name}</span>
                    {c.spreadType === 'yield_spread' && (
                      <Tip content="Yield spread (Moody's Baa yield minus the 10-year Treasury), not option-adjusted">
                        <span tabIndex={0}>
                          <Badge>yield spread</Badge>
                        </span>
                      </Tip>
                    )}
                  </div>
                  <div className="num text-2xs text-faint">{c.asOf ? fmtDateShort(c.asOf) : DASH}</div>
                </td>
                <td className="num border-b border-border px-2 text-right whitespace-nowrap">{fmtBps(c.valueBps)}</td>
                <td className="border-b border-border px-2 text-right whitespace-nowrap">
                  <Delta value={c.change1dBps} format={(v) => fmtBps(v, true)} invert showIcon={false} />
                </td>
                <td className="border-b border-border px-2 text-right whitespace-nowrap">
                  <Delta value={c.change1mBps} format={(v) => fmtBps(v, true)} invert showIcon={false} />
                </td>
                <td className="num border-b border-border px-2 text-right whitespace-nowrap text-muted" title={c.percentileWindowYears ? `window ${fmtNum(c.percentileWindowYears, 1)} years` : undefined}>
                  {Number.isFinite(c.percentile) ? `${Math.round(c.percentile)}${windows.length > 1 && c.percentileWindowYears ? ` (${fmtNum(c.percentileWindowYears, 0)}y)` : ''}` : DASH}
                </td>
                <td className="border-b border-border px-3 text-right">
                  <Sparkline data={c.history.map((h) => h.value)} invert width={84} height={20} ariaLabel={`${c.name} ${sparkWindowLabel(c.sparkWindowDays, c.history.length)} history`} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  )
}

/* ── macro tiles ── */

export function MacroTiles({ indicators, perRegion = 6, className }: { indicators: MacroIndicator[]; perRegion?: number; className?: string }) {
  const tiles = headlineTiles(indicators, perRegion)
  const regions = new Set(tiles.map((i) => i.region)).size
  const windowed = tiles.some((i) => i.sparkWindowDays)
  return (
    <Panel
      title="Macro headlines"
      subtitle={windowed ? 'Rates of change where it matters (y/y, q/q SAAR, m/m) · observation date on each tile · spark over a fixed calendar window' : 'Latest print and change vs prior · observation date on each tile'}
      className={className}
      actions={
        <Link to="/macro" className="text-xs text-accent-text hover:underline">
          Macro cockpit →
        </Link>
      }
    >
      {tiles.length === 0 && <PendingState compact detail="No key macro series published for this region yet." />}
      <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-2">
        {tiles.map((i) => (
          <MacroTile key={i.id} i={i} showRegion={regions > 1} />
        ))}
      </div>
    </Panel>
  )
}

function MacroTile({ i, showRegion }: { i: MacroIndicator; showRegion: boolean }) {
  const isRate = i.unit === 'pct'
  const changeFmt = (v: number) => (isRate ? fmtBps(v * 1e4, true) : fmtNum(v, 1, true))
  const win = sparkWindowLabel(i.sparkWindowDays, i.history.length)
  return (
    <Link
      to={`/macro/series/${encodeURIComponent(i.id)}`}
      className="block rounded-md border border-border bg-surface-2 px-2.5 py-2 hover:border-border-strong focus-visible:outline-2 focus-visible:outline-focus"
      title={`${i.name}${i.displayLabel ? ` · ${i.displayLabel}` : ''} · ${i.source} · observation ${fmtDate(i.asOf)}`}
    >
      <div className="flex items-center gap-1.5">
        {showRegion && <span className="rounded-sm bg-surface-3 px-1 text-2xs font-semibold text-muted">{REGION_LABEL[i.region as RegionCode] ?? i.region}</span>}
        <span className="line-clamp-1 min-w-0 flex-1 text-xs text-muted">{i.name}</span>
      </div>
      <div className="mt-0.5 flex items-end justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-baseline gap-1">
            <span className="num text-lg leading-6 font-medium text-fg">{fmtValue(i.value, isRate ? 'pct' : 'number')}</span>
            {i.displayLabel && <span className="text-2xs text-muted">{i.displayLabel}</span>}
          </div>
          <div className="flex items-center gap-1 text-2xs">
            <span className={cn('num', 'text-muted')}>
              {i.change > 0 ? '▲' : i.change < 0 ? '▼' : '■'} {Number.isFinite(i.change) ? changeFmt(i.change) : DASH}
            </span>
            <span className="text-faint">{i.changeLabel}</span>
          </div>
          <div className="num text-2xs text-faint">{i.asOf ? fmtDateShort(i.asOf) : DASH}</div>
        </div>
        <Sparkline data={i.history.map((h) => h.value)} width={70} height={26} tone="neutral" ariaLabel={`${i.name} ${win} history`} />
      </div>
    </Link>
  )
}
