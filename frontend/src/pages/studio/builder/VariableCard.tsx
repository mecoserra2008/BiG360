import { ArrowDown, ArrowUp, ChevronDown, ChevronRight, Minus, Plus, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { defaultTransform, describeTransform, netLag, withNetLag } from '@/api/studio/adapters'
import type { CTransform, CVariableSpec, DraftVariable, TransformOp, VariableRole } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Input, Select } from '@/components/ui/inputs'
import { Popover } from '@/components/ui/data'
import { cn } from '@/lib/utils'

/** Ops offered in the chain editor (lag / lead are driven by the spinner). */
const OPS: { op: TransformOp; label: string }[] = [
  { op: 'diff', label: 'diff' },
  { op: 'log', label: 'log' },
  { op: 'log_diff', label: 'log diff' },
  { op: 'pct_change', label: '% change' },
  { op: 'rolling_mean', label: 'rolling mean' },
  { op: 'rolling_std', label: 'rolling std' },
  { op: 'rolling_sum', label: 'rolling sum' },
  { op: 'rolling_min', label: 'rolling min' },
  { op: 'rolling_max', label: 'rolling max' },
  { op: 'rolling_z', label: 'rolling z' },
  { op: 'drawdown', label: 'drawdown' },
  { op: 'winsorize', label: 'winsorize' },
  { op: 'clip', label: 'clip' },
  { op: 'standardize', label: 'standardize' },
  { op: 'cs_rank', label: 'cs-rank' },
  { op: 'cs_zscore', label: 'cs-z' },
  { op: 'cs_demean', label: 'cs-demean' },
  { op: 'fwd_sum', label: 'fwd sum (target)' },
  { op: 'fwd_compound', label: 'fwd compound (target)' },
  { op: 'binarize', label: 'binarize' },
  { op: 'scale', label: 'scale' },
  { op: 'abs', label: 'abs' },
  { op: 'square', label: 'square' },
  { op: 'neg', label: 'negate' },
]

const ROLE_LABEL: Record<VariableRole, string> = {
  y: 'Target (y)',
  x: 'Regressor (x)',
  weights: 'Weights',
  feature: 'Feature',
  endog: 'Endogenous (IV)',
  instrument: 'Instrument (IV)',
  none: 'Not used',
}

const AGGS = ['last', 'first', 'mean', 'sum', 'min', 'max', 'median', 'count'] as const
const EAGGS = ['mean', 'median', 'sum', 'count', 'min', 'max'] as const

function num(s: string): number | null {
  if (s.trim() === '') return null
  const n = Number(s)
  return Number.isFinite(n) ? n : null
}

function TransformEditor({ t, onChange }: { t: CTransform; onChange: (t: CTransform) => void }) {
  const fields: { key: 'k' | 'window' | 'min_periods' | 'lower' | 'upper' | 'threshold' | 'factor'; label: string }[] = []
  if (['diff', 'pct_change', 'fwd_sum', 'fwd_compound'].includes(t.op)) fields.push({ key: 'k', label: 'k (periods)' })
  if (t.op.startsWith('rolling_') || t.op === 'drawdown') fields.push({ key: 'window', label: 'window' }, { key: 'min_periods', label: 'min periods' })
  if (t.op === 'winsorize' || t.op === 'clip') fields.push({ key: 'lower', label: t.op === 'winsorize' ? 'lower quantile' : 'lower' }, { key: 'upper', label: t.op === 'winsorize' ? 'upper quantile' : 'upper' })
  if (t.op === 'binarize') fields.push({ key: 'threshold', label: 'threshold' })
  if (t.op === 'scale') fields.push({ key: 'factor', label: 'factor' })
  const scoped = ['winsorize', 'standardize'].includes(t.op)
  if (!fields.length && !scoped) return <p className="text-xs text-muted">No parameters.</p>
  return (
    <div className="grid w-56 grid-cols-2 gap-2">
      {fields.map((f) => (
        <label key={f.key} className="text-2xs text-muted">
          {f.label}
          <Input type="number" className="num mt-0.5 w-full" value={t[f.key] ?? ''} onChange={(e) => onChange({ ...t, [f.key]: num(e.target.value) })} />
        </label>
      ))}
      {scoped && (
        <label className="col-span-2 text-2xs text-muted">
          scope
          <Select
            label="scope"
            className="mt-0.5 w-full"
            value={t.scope ?? ''}
            onChange={(v) => onChange({ ...t, scope: (v || null) as CTransform['scope'] })}
            options={[
              { value: '', label: 'default' },
              { value: 'cross_section', label: 'per date (cross-section)' },
              { value: 'entity', label: 'per entity (time series)' },
              { value: 'all', label: 'whole sample' },
            ]}
          />
        </label>
      )}
    </div>
  )
}

function Chain({ label, chain, onChange, withSpinner }: { label: string; chain: CTransform[]; onChange: (c: CTransform[]) => void; withSpinner: boolean }) {
  const shown = withSpinner ? chain.filter((t) => t.op !== 'lag' && t.op !== 'lead') : chain
  const [adding, setAdding] = useState<string>('')
  return (
    <div className="flex flex-wrap items-center gap-1">
      <span className="mr-0.5 text-2xs text-muted">{label}</span>
      {shown.map((t, i) => {
        const idx = chain.indexOf(t)
        return (
          <span key={i} className="inline-flex h-5 items-center rounded-sm border border-border bg-surface-3 text-2xs text-fg">
            <Popover
              align="start"
              trigger={
                <button type="button" className="num px-1.5 hover:text-accent-text" title="Edit transform parameters">
                  {describeTransform(t)}
                </button>
              }
            >
              <TransformEditor t={t} onChange={(nt) => onChange(chain.map((x, j) => (j === idx ? nt : x)))} />
            </Popover>
            <button type="button" aria-label={`Remove ${t.op}`} className="px-0.5 text-faint hover:text-down" onClick={() => onChange(chain.filter((_, j) => j !== idx))}>
              <X className="size-3" />
            </button>
          </span>
        )
      })}
      <Select
        label={`Add ${label.toLowerCase()} transform`}
        className="[&_select]:h-5 [&_select]:text-2xs"
        value={adding}
        onChange={(op) => {
          if (!op) return
          const t = defaultTransform(op as TransformOp)
          // keep lag/lead last so the spinner reads naturally
          const lags = chain.filter((x) => x.op === 'lag' || x.op === 'lead')
          onChange(withSpinner ? [...chain.filter((x) => x.op !== 'lag' && x.op !== 'lead'), t, ...lags] : [...chain, t])
          setAdding('')
        }}
        options={[{ value: '', label: '+ transform' }, ...OPS.map((o) => ({ value: o.op, label: o.label }))]}
      />
    </div>
  )
}

export function VariableCard({
  v,
  roles,
  isFirst,
  isLast,
  onPatch,
  onRemove,
  onMove,
  panelLike,
  featureLabel = 'Feature',
}: {
  v: DraftVariable
  roles: VariableRole[]
  isFirst: boolean
  isLast: boolean
  onPatch: (p: Partial<DraftVariable>) => void
  onRemove: () => void
  onMove: (dir: -1 | 1) => void
  panelLike: boolean
  /** what an unsupervised input is called for this method (feature / system variable) */
  featureLabel?: string
}) {
  const [open, setOpen] = useState(false)
  const lag = netLag(v.transforms)
  const nameOk = /^[A-Za-z_][A-Za-z0-9_]*$/.test(v.name)
  const lookAhead = v.role === 'x' && v.transforms.some((t) => t.op === 'lead' || t.op === 'fwd_sum' || t.op === 'fwd_compound')
  const roleOptions = [...new Set([...roles, v.role])].map((r) => ({ value: r, label: r === 'feature' ? featureLabel : ROLE_LABEL[r] }))
  return (
    <div className={cn('rounded-md border bg-surface-2 p-2', v.role === 'y' ? 'border-accent/60' : v.role === 'endog' || v.role === 'instrument' ? 'border-info/50' : 'border-border')}>
      <div className="flex items-center gap-1.5">
        <Input
          aria-label="Variable name"
          className={cn('num h-6 w-32 px-1.5 text-sm font-medium', !nameOk && 'border-down')}
          value={v.name}
          onChange={(e) => onPatch({ name: e.target.value.replace(/\s+/g, '_') })}
        />
        <Select label="Role" className="[&_select]:h-6" value={v.role} onChange={(r) => onPatch({ role: r })} options={roleOptions} />
        <div className="ml-auto flex items-center">
          <Button size="icon-sm" variant="ghost" aria-label="Move up" disabled={isFirst} onClick={() => onMove(-1)}>
            <ArrowUp />
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label="Move down" disabled={isLast} onClick={() => onMove(1)}>
            <ArrowDown />
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label={`Remove ${v.name}`} onClick={onRemove}>
            <Trash2 />
          </Button>
        </div>
      </div>
      <div className="num mt-1 truncate text-2xs text-muted" title={v.title ?? `${v.source}:${v.field}`}>
        {v.source}:{v.field}
        {v.title ? ` — ${v.title}` : ''}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1">
        <div className="flex items-center gap-1" role="group" aria-label="Lag / lead">
          <span className="text-2xs text-muted">{lag >= 0 ? 'lag' : 'lead'}</span>
          <Button size="icon-sm" variant="outline" aria-label="Decrease lag (towards lead)" onClick={() => onPatch({ transforms: withNetLag(v.transforms, lag - 1) })}>
            <Minus />
          </Button>
          <span className={cn('num w-7 text-center text-sm', lag < 0 ? 'text-warn' : lag > 0 ? 'text-fg' : 'text-muted')} aria-live="polite">
            {lag > 0 ? `t−${lag}` : lag < 0 ? `t+${-lag}` : 't'}
          </span>
          <Button size="icon-sm" variant="outline" aria-label="Increase lag" onClick={() => onPatch({ transforms: withNetLag(v.transforms, lag + 1) })}>
            <Plus />
          </Button>
        </div>
        <Chain label="Transforms" chain={v.transforms} withSpinner onChange={(c) => onPatch({ transforms: c })} />
      </div>
      {lookAhead && <p className="mt-1 text-2xs text-warn">Look-ahead: a regressor uses future values. Only targets may lead.</p>}
      <button type="button" className="mt-1 flex items-center gap-0.5 text-2xs text-muted hover:text-fg" aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />} Aggregation{panelLike ? ' & entity pooling' : ''}
        {(v.agg || v.entityAgg || v.preTransforms.length > 0 || v.source === 'factor_ls') && <span className="ml-1 size-1.5 rounded-full bg-accent" aria-label="customised" />}
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5 border-t border-border pt-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-2xs text-muted">
              time agg.
              <Select
                label="Time aggregation"
                className="[&_select]:h-6"
                value={v.agg ?? ''}
                onChange={(a) => onPatch({ agg: (a || null) as CVariableSpec['agg'] })}
                options={[{ value: '', label: 'default' }, ...AGGS.map((a) => ({ value: a, label: a }))]}
              />
            </label>
            <label className="flex items-center gap-1 text-2xs text-muted">
              pool entities
              <Select
                label="Entity aggregation"
                className="[&_select]:h-6"
                value={v.entityAgg ?? ''}
                onChange={(a) => onPatch({ entityAgg: (a || null) as CVariableSpec['entity_agg'] })}
                options={[{ value: '', label: 'none' }, ...EAGGS.map((a) => ({ value: a, label: a }))]}
              />
            </label>
          </div>
          {v.source === 'factor_ls' && (
            <label className="flex items-center gap-1.5 text-2xs text-muted">
              long-short quantiles
              <Input
                type="number"
                aria-label="Long-short quantiles"
                className="num h-6 w-16"
                min={2}
                max={10}
                value={String(((v.extra.options as Record<string, unknown> | undefined)?.quantiles as number | undefined) ?? 5)}
                onChange={(e) => onPatch({ extra: { ...v.extra, options: { ...((v.extra.options as Record<string, unknown>) ?? {}), quantiles: Math.max(2, Math.min(10, Math.trunc(Number(e.target.value) || 5))) } } })}
              />
              <span>top minus bottom, sorted on the previous period&apos;s score over the entity filter</span>
            </label>
          )}
          <Chain label="Before resampling" chain={v.preTransforms} withSpinner={false} onChange={(c) => onPatch({ preTransforms: c })} />
          {v.entities && (
            <div className="text-2xs text-muted">
              Own entity filter:{' '}
              <span className="num text-fg">
                {Object.entries(v.entities)
                  .filter(([k, x]) => k !== 'primary_only' && x !== null && x !== undefined)
                  .map(([k, x]) => `${k}=${Array.isArray(x) ? x.join('|') : String(x)}`)
                  .join(' · ') || 'all'}
              </span>
              <button type="button" className="ml-1.5 text-accent-text hover:underline" onClick={() => onPatch({ entities: null })}>
                clear
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
