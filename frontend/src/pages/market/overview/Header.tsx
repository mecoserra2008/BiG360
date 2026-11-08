import { ChevronDown } from 'lucide-react'
import { Link } from 'react-router'
import { regimeTone } from '@/api/crossasset/adapters'
import { useXaRisk } from '@/api/crossasset/hooks'
import { REGIME_LABEL } from '@/api/crossasset/types'
import type { IndexQuote, MacroRegime, RegimeName, RegionDataAsOf } from '@/api/types'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Popover } from '@/components/ui/data'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtDateShort, fmtMonth, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { RiskGauge } from '../../crossasset/charts'
import { asOfChips, isoDaysAgo, regimeChipText, sessionsBetween } from './helpers'

export function DataAsOfStrip({ dataAsOf, indices, datesAligned, today }: { dataAsOf: RegionDataAsOf[]; indices: IndexQuote[]; datesAligned: boolean | null; today: string }) {
  const { chips, source } = asOfChips(dataAsOf, indices)
  const latest = chips.reduce((m, c) => (c.asOf > m ? c.asOf : m), '')
  const mixed = datesAligned === false || (datesAligned === null && new Set(chips.map((c) => c.asOf)).size > 1)
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5" aria-label="Market data as of, per region">
      <span className="label-caps mr-0.5">Market data as of</span>
      {chips.length === 0 && <span className="text-xs text-faint">{DASH}</span>}
      {chips.map((c) => {
        const behind = latest && c.asOf < latest ? sessionsBetween(c.asOf, latest) : 0
        const age = sessionsBetween(c.asOf, today)
        const stale = age > 2 || (c.staleShare ?? 0) > 0.25
        return (
          <Tip
            key={c.region}
            content={
              <div className="space-y-0.5">
                <div>
                  {c.region}: last session {fmtDate(c.asOf)}
                </div>
                {behind > 0 && <div className="text-warn">{behind} session(s) behind the latest region</div>}
                <div className="text-muted">{age} weekday(s) before today</div>
                {typeof c.staleShare === 'number' && <div className="text-muted">{fmtPct(c.staleShare, 0)} of the region's names have an older last bar</div>}
                {source === 'index' && <div className="text-muted">Benchmark index date (this API has no per-region data_as_of yet)</div>}
              </div>
            }
          >
            <span
              tabIndex={0}
              className={cn(
                'inline-flex h-5 items-center gap-1 rounded-sm border px-1.5 text-2xs font-semibold',
                stale ? 'border-warn/40 bg-warn-soft text-warn' : behind > 0 ? 'border-border-strong bg-surface-2 text-fg' : 'border-border bg-surface-2 text-muted',
              )}
            >
              {c.region}
              <span className="num font-medium">{fmtDateShort(c.asOf)}</span>
              {stale && <span className="sr-only">stale</span>}
            </span>
          </Tip>
        )
      })}
      {mixed && chips.length > 1 && (
        <Tip content="Regions are on different sessions: cross-region 1D figures compare different days. Each column/row carries its own date.">
          <span tabIndex={0}>
            <Badge tone="warn">mixed dates</Badge>
          </span>
        </Tip>
      )}
      {source === 'index' && <span className="text-2xs text-faint">(index dates)</span>}
    </div>
  )
}

/** Compact model-based risk-on / risk-off gauge (cross-asset composite). */
export function RiskChip() {
  const from = isoDaysAgo(400)
  const q = useXaRisk(from)
  const l = q.data?.latest
  const v = l?.risk_composite
  const tone = regimeTone(l?.risk_regime)
  return (
    <Link to="/macro/cross-asset" className="flex items-center gap-2 rounded-md px-1 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-focus" aria-label="Risk-on / risk-off composite, open Cross-asset">
      <RiskGauge value={v} size={96} label="Risk composite (model-based)" />
      <div className="min-w-0 leading-tight">
        <div className="label-caps">Risk-on / off</div>
        <div className="num text-md font-semibold text-fg">{typeof v === 'number' ? `${v > 0 ? '+' : ''}${v.toFixed(2)}` : DASH}</div>
        <div className="flex items-center gap-1">
          <Badge tone={tone === 'up' ? 'info' : tone === 'down' ? 'accent' : 'neutral'}>{REGIME_LABEL[l?.risk_regime ?? ''] ?? (q.isError ? 'n/a' : '…')}</Badge>
        </div>
        <div className="text-2xs text-faint">model-based · {l?.date ? fmtDateShort(l.date) : DASH}</div>
      </div>
    </Link>
  )
}

/* ── regime: one-line labelled chip + details popover ── */

const REGIME_COLOR_ORDER: RegimeName[] = ['Expansion', 'Slowdown', 'Contraction', 'Recovery', 'Stagflation']

function useRegimeColor() {
  const t = useChartTokens()
  return (name: RegimeName | null) => (name ? (t.series[REGIME_COLOR_ORDER.indexOf(name)] ?? t.ref) : t.faint)
}

export function RegimeChip({ r }: { r: MacroRegime }) {
  const color = useRegimeColor()
  const off = r.status === 'not_modelled' || !r.current
  return (
    <div className="flex min-w-0 items-center gap-1.5 text-xs">
      <Link
        to="/macro"
        className={cn('inline-flex min-w-0 items-center gap-1.5 rounded-sm border px-1.5 py-0.5 hover:border-border-strong focus-visible:outline-2 focus-visible:outline-focus', off ? 'border-border text-muted' : 'border-border bg-surface-2 text-fg')}
        title="Open the macro cockpit"
      >
        <span className="size-2 shrink-0 rounded-sm" style={{ background: color(r.current) }} aria-hidden />
        <span className="truncate">{regimeChipText(r)}</span>
        {r.status === 'fallback' && <Badge tone="warn">fallback</Badge>}
        {!off && <Badge tone="info">model-based</Badge>}
      </Link>
      {!off && (
        <Popover
          align="start"
          className="w-[380px]"
          trigger={
            <button type="button" className="inline-flex h-5 items-center gap-0.5 rounded-sm px-1 text-2xs text-muted hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus" aria-label="Regime details">
              details <ChevronDown className="size-3" aria-hidden />
            </button>
          }
        >
          <RegimeDetail r={r} />
        </Popover>
      )}
    </div>
  )
}

/** Months between two ISO dates (inclusive), as yyyy-mm. */
function monthsRange(a: string, b: string): string[] {
  const out: string[] = []
  const d = new Date(`${a.slice(0, 7)}-01T00:00:00Z`)
  const end = `${b.slice(0, 7)}`
  for (let i = 0; i < 400; i++) {
    const m = d.toISOString().slice(0, 7)
    out.push(m)
    if (m >= end) break
    d.setUTCMonth(d.getUTCMonth() + 1)
  }
  return out
}

export function RegimeDetail({ r }: { r: MacroRegime }) {
  const color = useRegimeColor()
  const hist = r.history
  const byMonth = new Map(hist.map((h) => [h.date.slice(0, 7), h]))
  const months = hist.length ? monthsRange(hist[0].date, hist[hist.length - 1].date) : []
  const gaps = months.filter((m) => !byMonth.has(m)).length
  return (
    <div className="space-y-3">
      <div>
        <div className="label-caps">
          {r.scope} HMM · {r.states.length} states{r.modelId ? ` · ${r.modelId}` : ''}
        </div>
        <ul className="mt-1.5 space-y-1" aria-label="Regime probabilities">
          {r.states.map((name) => (
            <li key={name} className="grid grid-cols-[86px_1fr_40px] items-center gap-2 text-xs">
              <span className={cn(name === r.current ? 'font-semibold text-fg' : 'text-muted')}>{name}</span>
              <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                <span className="block h-full rounded-full" style={{ width: `${r.probabilities[name] * 100}%`, background: color(name) }} />
              </span>
              <span className="num text-right text-fg">{fmtPct(r.probabilities[name], 0)}</span>
            </li>
          ))}
        </ul>
        <p className="mt-1 text-2xs text-faint">Posterior probabilities of the model's own states (an HMM is often over-confident).</p>
      </div>
      {months.length > 1 && (
        <div>
          <div className="label-caps mb-1">Monthly regime history{gaps ? ` · ${gaps} month(s) without a snapshot (hatched)` : ''}</div>
          <div className="flex h-4 gap-px overflow-hidden rounded-sm" role="img" aria-label="Regime history by month">
            {months.map((m) => {
              const h = byMonth.get(m)
              return h ? (
                <span key={m} className="flex-1" style={{ background: color(h.regime), opacity: Number.isFinite(h.probability) ? 0.35 + h.probability * 0.65 : 0.6 }} title={`${fmtMonth(h.date)} · ${h.regime ?? 'unknown'}${Number.isFinite(h.probability) ? ` (${fmtPct(h.probability, 0)})` : ''}`} />
              ) : (
                <span key={m} className="flex-1 bg-[repeating-linear-gradient(135deg,var(--surface-3)_0_2px,transparent_2px_4px)]" title={`${m} · no snapshot`} />
              )
            })}
          </div>
          <div className="mt-1 flex justify-between text-2xs text-faint">
            <span>{fmtMonth(hist[0].date)}</span>
            <span>{fmtMonth(hist[hist.length - 1].date)}</span>
          </div>
          <ul className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-muted" aria-label="Legend">
            {r.states.map((s) => (
              <li key={s} className="inline-flex items-center gap-1">
                <span className="size-2 rounded-sm" style={{ background: color(s) }} aria-hidden />
                {s}
              </li>
            ))}
          </ul>
        </div>
      )}
      {r.drivers.length > 0 && (
        <div className="border-t border-border pt-2">
          <div className="label-caps mb-1">Model inputs · level (z vs model fit)</div>
          <ul className="grid grid-cols-2 gap-x-4 gap-y-1">
            {r.drivers.map((x) => (
              <li key={x.label} className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate text-muted capitalize">{x.label}</span>
                <span className="num shrink-0 text-fg">
                  {x.direction === 'up' ? '▲' : x.direction === 'down' ? '▼' : '■'} {x.value}
                  {x.z !== null && <span className="text-muted"> ({x.z > 0 ? '+' : ''}{x.z.toFixed(1)})</span>}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <Link to="/macro" className="block text-xs text-accent-text hover:underline">
        Macro cockpit: cycle clock, recession models, surprises →
      </Link>
    </div>
  )
}
