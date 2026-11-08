import type { ColumnDef } from '@tanstack/react-table'
import type { InsiderTransaction, InsiderTxnCode } from '@/api/types'
import { Badge } from '../ui/badge'
import { Tip } from '../ui/tooltip'
import { fmtCompactCurrency, fmtDate, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'

export const TXN_CODE: Record<InsiderTxnCode, { label: string; tone: 'up' | 'down' | 'neutral' | 'info' }> = {
  P: { label: 'Open-market purchase', tone: 'up' },
  S: { label: 'Open-market sale', tone: 'down' },
  A: { label: 'Grant / award', tone: 'neutral' },
  M: { label: 'Option exercise', tone: 'info' },
  F: { label: 'Tax withholding', tone: 'neutral' },
  G: { label: 'Gift', tone: 'neutral' },
}

export function CodeBadge({ code }: { code: InsiderTxnCode }) {
  const c = TXN_CODE[code]
  return (
    <Tip content={`${code} — ${c.label}`}>
      <span tabIndex={0}>
        <Badge tone={c.tone} className="num w-6 justify-center">
          {code}
        </Badge>
      </span>
    </Tip>
  )
}

const FEATURE_LABEL: Record<string, string> = {
  role_weight: 'Role weight',
  n_insiders_same_dir_30d: 'Insiders same direction (30d)',
  cluster_n_insiders: 'Cluster size',
  first_buy_2y: 'First buy in 2 years',
  routine: 'Routine trader',
  size_vs_hist: 'Size vs own history',
  pre_drawdown_252d: 'Drawdown before (1y)',
  pre_ret_126d: 'Return before (6m)',
  value_usd: 'Value (USD)',
}
function fmtFeature(k: string, v: unknown): string {
  if (typeof v === 'boolean') return v ? 'yes' : 'no'
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—'
  if (/^pre_/.test(k)) return fmtPct(v, 1, true)
  if (k === 'value_usd') return fmtCompactCurrency(v, 'USD', 1)
  return fmtNum(v, Math.abs(v) >= 10 ? 0 : 2)
}

/** Anomaly score with the model features behind it as a tooltip (v1.2 insider_txn_scores). */
export function AnomalyCell({ value, features }: { value: number; features?: Record<string, unknown> | null }) {
  const entries = Object.entries(features ?? {}).filter(([, v]) => v !== null && v !== undefined)
  const bar = <AnomalyBar value={value} />
  if (!entries.length) return bar
  return (
    <Tip
      content={
        <div className="space-y-0.5">
          <div className="font-semibold">Anomaly features</div>
          {entries.map(([k, v]) => (
            <div key={k} className="flex justify-between gap-4">
              <span className="text-muted">{FEATURE_LABEL[k] ?? k.replace(/_/g, ' ')}</span>
              <span className="num">{fmtFeature(k, v)}</span>
            </div>
          ))}
        </div>
      }
    >
      <span tabIndex={0} className="cursor-help">{bar}</span>
    </Tip>
  )
}

export function AnomalyBar({ value }: { value: number }) {
  const hi = value >= 0.75
  const mid = value >= 0.5
  return (
    <span className="inline-flex items-center gap-1.5" role="meter" aria-valuemin={0} aria-valuemax={1} aria-valuenow={value} aria-label="Anomaly score">
      <span className="num w-7 text-right">{fmtNum(value, 2)}</span>
      <span className="relative h-1.5 w-12 overflow-hidden rounded-full bg-surface-3">
        <span className={cn('absolute inset-y-0 left-0 rounded-full', hi ? 'bg-accent' : mid ? 'bg-warn' : 'bg-faint')} style={{ width: `${Number.isFinite(value) ? value * 100 : 0}%` }} />
      </span>
    </span>
  )
}

/** Form-4 style transaction columns (shared by Company › Insiders and Insider Radar). */
export function insiderColumns<T extends InsiderTransaction>(currency?: string): ColumnDef<T>[] {
  return [
    { id: 'tradeDate', header: 'Trade date', size: 110, accessorFn: (r) => r.tradeDate, cell: (c) => <span className="num">{fmtDate(c.getValue<string>())}</span> },
    { id: 'filedAt', header: 'Filed', size: 106, accessorFn: (r) => r.filedAt, cell: (c) => <span className="num text-muted">{fmtDate(c.getValue<string>())}</span> },
    { id: 'insider', header: 'Insider', size: 150, accessorFn: (r) => r.insiderName, sortDescFirst: false },
    { id: 'role', header: 'Role', size: 104, accessorFn: (r) => r.role, cell: (c) => <span className="text-muted">{c.getValue<string>()}</span> },
    { id: 'code', header: 'Code', size: 66, accessorFn: (r) => r.code, cell: (c) => <CodeBadge code={c.row.original.code} />, meta: { align: 'center', title: 'Transaction code (Form 4 convention)' } },
    { id: 'shares', header: 'Shares', size: 90, accessorFn: (r) => r.shares, cell: (c) => <span className={cn('num', c.getValue<number>() < 0 ? 'text-down' : 'text-up')}>{c.getValue<number>() > 0 ? '+' : '−'}{fmtInt(Math.abs(c.getValue<number>()))}</span>, meta: { align: 'right' } },
    { id: 'price', header: 'Price', size: 92, accessorFn: (r) => r.price, cell: (c) => <span className="num">{fmtNum(c.getValue<number>(), 2)}</span>, meta: { align: 'right', title: currency ? `Price (${currency})` : 'Price (local)' } },
    { id: 'value', header: 'Value $', size: 84, accessorFn: (r) => r.valueUsd, cell: (c) => <span className="num">{fmtCompactCurrency(c.getValue<number>(), 'USD', 1)}</span>, meta: { align: 'right', title: 'Transaction value (USD)' } },
    { id: 'owned', header: 'Owned after', size: 112, accessorFn: (r) => r.sharesOwnedAfter, cell: (c) => <span className="num text-muted">{fmtInt(c.getValue<number>())}</span>, meta: { align: 'right' } },
    { id: 'chg', header: 'Δ holding', size: 100, accessorFn: (r) => r.ownershipChangePct, cell: (c) => <span className="num">{fmtPct(c.getValue<number>(), 1, true)}</span>, meta: { align: 'right' } },
    { id: 'anomaly', header: 'Anomaly', size: 112, accessorFn: (r) => r.anomalyScore, cell: (c) => <AnomalyCell value={c.getValue<number>()} features={c.row.original.features} />, meta: { title: 'IsolationForest anomaly score per transaction (0–1); hover for features' } },
    { id: 'cluster', header: 'Cluster', size: 80, accessorFn: (r) => (r.isClusterBuy ? 1 : 0), cell: (c) =>
        c.row.original.isClusterBuy ? (
          <Tip content={c.row.original.clusterId ? `Cluster ${c.row.original.clusterId}` : 'Part of a cluster buy'}>
            <span tabIndex={0}>
              <Badge tone="accent">CB</Badge>
            </span>
          </Tip>
        ) : null,
      meta: { align: 'center', title: 'Part of a cluster buy' } },
    { id: 'source', header: 'Source', size: 92, accessorFn: (r) => r.source, cell: (c) => <span className="text-xs text-faint">{c.getValue<string>()}</span> },
  ]
}
