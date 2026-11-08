import { AlertTriangle } from 'lucide-react'
import type { CompanyForecast, CostOfCapital, FairValueDetail } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Delta } from '@/components/ui/data'
import { Panel, Stat } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { cn } from '@/lib/utils'

const fin = (v: number | null | undefined): v is number => typeof v === 'number' && Number.isFinite(v)

/** Fair-value range bar: low–high band with DCF, multiples, headline (blend), 12m target and price markers. */
function RangeBar({ fv, price, currency }: { fv: FairValueDetail; price: number; currency: string }) {
  const pts = [fv.low, fv.high, fv.dcf, fv.multiples, fv.blended, fv.target12m, price].filter(fin)
  if (pts.length < 2) return null
  const lo = Math.min(...pts)
  const hi = Math.max(...pts)
  const pad = (hi - lo) * 0.08 || hi * 0.05 || 1
  const x = (v: number) => `${((v - (lo - pad)) / (hi - lo + 2 * pad)) * 100}%`
  const markers: { v: number; label: string; color: string }[] = [
    ...(fin(fv.dcf) ? [{ v: fv.dcf, label: 'Raw DCF', color: 'var(--info)' }] : []),
    ...(fin(fv.multiples) ? [{ v: fv.multiples, label: 'Multiples', color: 'var(--c4)' }] : []),
    ...(fin(fv.blended) ? [{ v: fv.blended, label: 'Headline', color: 'var(--accent)' }] : []),
    ...(fin(fv.target12m) ? [{ v: fv.target12m, label: '12m target', color: 'var(--up)' }] : []),
    { v: price, label: 'Price', color: 'var(--fg)' },
  ]
  return (
    <div className="mt-4 mb-3">
      <div className="relative h-4" role="img" aria-label={`Fair value range ${fmtPrice(fv.low, currency)} to ${fmtPrice(fv.high, currency)}; price ${fmtPrice(price, currency)}`}>
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border-strong" />
        {fin(fv.low) && fin(fv.high) && (
          <div className="absolute top-0.5 bottom-0.5 rounded-sm bg-accent-soft ring-1 ring-accent/40" style={{ left: x(fv.low), width: `calc(${x(fv.high)} - ${x(fv.low)})` }} title="Fair-value range (low–high)" />
        )}
        {markers.map((m) => (
          <div key={m.label} className="absolute top-0 h-4 w-0.5 -translate-x-1/2" style={{ left: x(m.v), background: m.color }} title={`${m.label} ${fmtPrice(m.v, currency)}`} />
        ))}
      </div>
      <div className="mt-1.5 flex flex-wrap gap-x-4 gap-y-1 text-2xs">
        {fin(fv.low) && fin(fv.high) && (
          <span className="inline-flex items-center gap-1 text-muted">
            <span className="h-2 w-3 rounded-sm bg-accent-soft ring-1 ring-accent/40" aria-hidden /> Range <span className="num text-fg">{fmtPrice(fv.low, currency)}–{fmtPrice(fv.high, currency)}</span>
          </span>
        )}
        {markers.map((m) => (
          <span key={m.label} className="inline-flex items-center gap-1 text-muted">
            <span className="h-2.5 w-0.5" style={{ background: m.color }} aria-hidden /> {m.label} <span className="num text-fg">{fmtPrice(m.v, currency)}</span>
          </span>
        ))}
      </div>
    </div>
  )
}

function ConfidenceMeter({ value }: { value: number }) {
  const v = fin(value) ? Math.max(0, Math.min(100, value)) : NaN
  const tone = !fin(v) ? 'bg-faint' : v >= 60 ? 'bg-up' : v >= 40 ? 'bg-warn' : 'bg-down'
  return (
    <div role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={fin(v) ? Math.round(v) : undefined} aria-label="Valuation confidence">
      <div className="flex items-baseline justify-between">
        <span className="label-caps">Valuation confidence</span>
        <span className="num text-md font-semibold">{fin(v) ? Math.round(v) : '—'}<span className="text-xs text-muted">/100</span></span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-3">
        <div className={cn('h-full rounded-full', tone)} style={{ width: fin(v) ? `${v}%` : 0 }} />
      </div>
      {fin(v) && v < 40 && <div className="mt-1 text-2xs text-down">Below 40 — scoring down-weights this valuation.</div>}
    </div>
  )
}

export function ValuationPanel({ d, className }: { d: CompanyForecast; className?: string }) {
  const fv = d.fairValueDetail
  if (!fv) return null
  const price = d.reverseDcf.currentPrice
  const ccy = d.currency
  const modelLabel = fv.model === 'excess_return' ? 'Excess-return model' : fv.model === 'dcf' ? 'DCF (FCFF)' : 'Model n/a'
  return (
    <Panel
      title="Valuation"
      subtitle="Headline fair value = 0.25 × bear + 0.5 × base + 0.25 × bull; each scenario a DCF × peer-multiples blend shrunk toward price by confidence, discounted with the market-implied ERP of the region"
      className={className}
      actions={
        <Tip content={fv.model === 'excess_return' ? 'Banks / insurers: residual-income (excess return on equity) model' : 'Free cash flow to the firm, discounted at WACC'}>
          <span tabIndex={0}>
            <Badge tone="accent">{modelLabel}</Badge>
          </span>
        </Tip>
      }
    >
      <div className="grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-5">
        <Stat label="Headline fair value" value={fmtPrice(fv.blended, ccy)} sub={<Delta value={fv.upsidePct} format={(v) => `${fmtPct(v, 0, true)} vs price`} />} />
        <Stat label="12-month target" value={fmtPrice(fv.target12m, ccy)} sub={fin(fv.target12m) && fin(price) ? `${fmtPct(fv.target12m / price - 1, 0, true)} vs price` : undefined} />
        <Stat label={fv.model === 'excess_return' ? 'Raw model value' : 'Raw DCF value'} value={fmtPrice(fv.dcf, ccy)} sub={fin(fv.dcf) && fin(price) ? `${fmtPct(fv.dcf / price - 1, 0, true)} vs price` : 'not meaningful'} />
        <Stat label="Peer multiples value" value={fmtPrice(fv.multiples, ccy)} />
        <Stat label="Price" value={fmtPrice(price, ccy)} sub={ccy} />
      </div>
      <RangeBar fv={fv} price={price} currency={ccy} />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div>
          <ConfidenceMeter value={fv.confidence} />
          {fv.confidenceReasons.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted">
              {fv.confidenceReasons.map((r) => (
                <li key={r}>• {r.replace(/_/g, ' ')}</li>
              ))}
            </ul>
          )}
        </div>
        <div className="space-y-2">
          {fin(fv.tvShare) && (
            <div className={cn('flex items-center gap-2 rounded-md border px-2 py-1.5 text-xs', fv.tvShare > 0.85 ? 'border-warn/40 bg-warn-soft text-warn' : 'border-border text-muted')}>
              {fv.tvShare > 0.85 && <AlertTriangle className="size-3.5 shrink-0" aria-hidden />}
              Terminal value = <span className="num font-semibold">{fmtPct(fv.tvShare, 0)}</span> of EV
              {fv.tvShare > 0.85 ? ' — value rests mostly on the terminal assumption.' : ''}
            </div>
          )}
          {fv.flags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {fv.flags.map((f) => (
                <Badge key={f} tone="warn">{f.replace(/_/g, ' ')}</Badge>
              ))}
            </div>
          )}
        </div>
      </div>
    </Panel>
  )
}

export function CostOfCapitalPanel({ c, className }: { c: CostOfCapital | null | undefined; className?: string }) {
  if (!c) return null
  const rows: [string, string, string?][] = [
    ['WACC', fmtPct(c.wacc, 2)],
    ['Cost of equity', fmtPct(c.costOfEquity, 2)],
    ['Beta (adj. / raw)', `${fmtNum(c.beta, 2)} / ${fmtNum(c.betaRaw, 2)}`, c.betaSource ?? undefined],
    ['Risk-free rate', fmtPct(c.rf, 2), c.rfSource ?? undefined],
    ['Equity risk premium', fmtPct(c.erp, 2), 'contract ERP; the valuation uses a market-implied ERP per region'],
    ['Country risk premium', fmtPct(c.crp, 2)],
    ['Cost of debt (after tax)', fmtPct(c.kdPost, 2)],
    ['Tax rate', fmtPct(c.taxRate, 1)],
    ['Equity weight', fmtPct(c.weightEquity, 0)],
    ['Terminal growth (g)', fmtPct(c.terminalGrowth, 2)],
  ]
  return (
    <Panel title="Cost of capital" subtitle={`${c.currency ?? ''}${c.asOf ? ` · as of ${c.asOf}` : ''}${c.isFinancial ? ' · financial (equity-only)' : ''}`} className={className} flush>
      <table className="w-full text-sm">
        <tbody>
          {rows.map(([k, v, src]) => (
            <tr key={k} className="hover:bg-surface-2">
              <th scope="row" className="h-[25px] border-b border-border px-3 text-left font-normal text-muted">{k}</th>
              <td className="num border-b border-border px-3 text-right">
                {v}
                {src && <span className="ml-1.5 text-2xs text-faint">{src}</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {c.flags.length > 0 && (
        <div className="flex flex-wrap gap-1 px-3 py-2">
          {c.flags.map((f) => (
            <Badge key={f} tone="warn">{f.replace(/_/g, ' ')}</Badge>
          ))}
        </div>
      )}
    </Panel>
  )
}
