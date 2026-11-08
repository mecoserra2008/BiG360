import { X } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { useSearch, useSectorTree } from '@/api/hooks'
import { isSeriesSource } from '@/api/studio/adapters'
import { useStudioCatalog } from '@/api/studio/hooks'
import type { CEntityFilter, StudyDraft } from '@/api/studio/types'
import { Input, Segmented, Select, Switch } from '@/components/ui/inputs'
import { cn } from '@/lib/utils'

const REGIONS = ['US', 'CA', 'LATAM', 'UK', 'EU', 'JP', 'KR', 'CN', 'HK', 'APAC', 'OTHER'] as const
type Region = (typeof REGIONS)[number]

function Chip({ label, onRemove }: { label: string; onRemove: () => void }) {
  return (
    <span className="inline-flex h-5 items-center gap-0.5 rounded-sm border border-border bg-surface-3 pl-1.5 text-2xs text-fg">
      {label}
      <button type="button" aria-label={`Remove ${label}`} className="px-0.5 text-faint hover:text-down" onClick={onRemove}>
        <X className="size-3" />
      </button>
    </span>
  )
}

export function Toggles<T extends string>({ all, selected, onChange, label, names }: { all: readonly T[]; selected: T[]; onChange: (v: T[]) => void; label: string; names?: Record<string, string> }) {
  return (
    <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
      {all.map((x) => {
        const on = selected.includes(x)
        return (
          <button
            key={x}
            type="button"
            aria-pressed={on}
            title={names?.[x]}
            onClick={() => onChange(on ? selected.filter((s) => s !== x) : [...selected, x])}
            className={cn(
              'h-5 rounded-sm border px-1.5 text-2xs font-medium transition-colors',
              on ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:text-fg',
            )}
          >
            {x}
          </button>
        )
      })}
    </div>
  )
}

function SecuritySearch({ onPick }: { onPick: (ticker: string) => void }) {
  const [q, setQ] = useState('')
  const dq = useDeferredValue(q)
  const res = useSearch(dq)
  const items = q.trim() ? (res.data ?? []).slice(0, 8) : []
  return (
    <div className="relative">
      <Input className="w-full" placeholder="Add a security (ticker or name)" aria-label="Search securities" value={q} onChange={(e) => setQ(e.target.value)} />
      {items.length > 0 && (
        <ul className="absolute top-full right-0 left-0 z-20 mt-0.5 max-h-56 overflow-y-auto rounded-md border border-border-strong bg-surface py-0.5 text-sm" role="listbox" aria-label="Matching securities">
          {items.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-surface-2"
                onClick={() => {
                  onPick(s.ticker)
                  setQ('')
                }}
              >
                <span className="num w-20 shrink-0 font-medium text-fg">{s.ticker}</span>
                <span className="truncate text-muted">{s.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const label = 'mb-0.5 block text-2xs font-semibold tracking-wide text-muted uppercase'

export function DatasetSection({ draft, patch }: { draft: StudyDraft; patch: (p: Partial<StudyDraft>) => void }) {
  const cat = useStudioCatalog()
  const sectors = useSectorTree('ALL')
  const e = draft.entities
  const setE = (p: Partial<CEntityFilter>) => patch({ entities: { ...e, ...p } })
  const sectorIds = (sectors.data ?? []).map((s) => s.id)
  const sectorNames = Object.fromEntries((sectors.data ?? []).map((s) => [s.id, s.name]))
  const usesEntities = draft.variables.some((v) => !isSeriesSource(v.source)) || draft.kind !== 'timeseries'
  return (
    <div className="space-y-2.5">
      <div>
        <span className={label}>Data shape</span>
        <Segmented
          label="Data shape"
          value={draft.kind}
          onChange={(kind) => patch({ kind })}
          options={[
            { value: 'timeseries', label: 'Time series', title: 'One row per date (entities pooled or macro series)' },
            { value: 'panel', label: 'Panel', title: 'Entity × date' },
            { value: 'cross_section', label: 'Cross-section', title: 'One row per entity at a date' },
          ]}
        />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label>
          <span className={label}>Start</span>
          <Input type="date" className="num w-full" value={draft.start ?? ''} onChange={(ev) => patch({ start: ev.target.value || null })} />
        </label>
        <label>
          <span className={label}>End</span>
          <Input type="date" className="num w-full" value={draft.end ?? ''} onChange={(ev) => patch({ end: ev.target.value || null })} />
        </label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label>
          <span className={label}>Frequency</span>
          <Select
            label="Frequency"
            className="w-full"
            value={draft.frequency}
            onChange={(frequency) => patch({ frequency })}
            options={[
              { value: 'D', label: 'Daily' },
              { value: 'W', label: 'Weekly' },
              { value: 'M', label: 'Monthly' },
              { value: 'Q', label: 'Quarterly' },
            ]}
          />
        </label>
        <label>
          <span className={label}>Entity level</span>
          <Select
            label="Entity level"
            className="w-full"
            value={draft.entityLevel}
            onChange={(entityLevel) => patch({ entityLevel })}
            options={[
              { value: 'security', label: 'Security' },
              { value: 'sector', label: 'Sector (aggregated)' },
              { value: 'region', label: 'Region (aggregated)' },
              { value: 'country', label: 'Country (aggregated)' },
            ]}
          />
        </label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label>
          <span className={label}>Macro alignment</span>
          <Select
            label="Macro alignment"
            className="w-full"
            value={draft.macroAlignment}
            onChange={(macroAlignment) => patch({ macroAlignment })}
            options={[
              { value: 'auto', label: 'Auto' },
              { value: 'pit', label: 'Point-in-time (as published)' },
              { value: 'period', label: 'Reference period' },
            ]}
          />
        </label>
      </div>
      <fieldset className={cn('space-y-2 rounded-md border border-border p-2', !usesEntities && 'opacity-70')}>
        <legend className="px-1 text-2xs font-semibold tracking-wide text-muted uppercase">Entities {!usesEntities && '(macro-only study: not used)'}</legend>
        <div className="grid grid-cols-2 items-end gap-2">
          <label>
            <span className={label}>Universe</span>
            <Select
              label="Universe"
              className="w-full"
              value={e.universe ?? ''}
              onChange={(u) => setE({ universe: u || null })}
              options={[{ value: '', label: 'Any (no universe)' }, ...(cat.data?.universes ?? []).map((u) => ({ value: u, label: u }))]}
            />
          </label>
          <label className="flex h-7 items-center gap-1.5 text-xs text-muted">
            <Switch label="Primary listings only" checked={e.primary_only !== false} onCheckedChange={(c) => setE({ primary_only: c })} /> Primary listings only
          </label>
        </div>
        <div>
          <span className={label}>Sectors</span>
          <Toggles label="Sectors" all={sectorIds} selected={e.sectors ?? []} onChange={(s) => setE({ sectors: s.length ? s : null })} names={sectorNames} />
        </div>
        <div>
          <span className={label}>Regions</span>
          <Toggles<Region> label="Regions" all={REGIONS} selected={(e.regions ?? []) as Region[]} onChange={(r) => setE({ regions: r.length ? r : null })} />
        </div>
        <div>
          <span className={label}>Securities</span>
          <SecuritySearch onPick={(t) => setE({ tickers: [...new Set([...(e.tickers ?? []), t])] })} />
          {(e.tickers?.length ?? 0) > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {e.tickers?.map((t) => (
                <Chip key={t} label={t} onRemove={() => {
                    const next = (e.tickers ?? []).filter((x) => x !== t)
                    setE({ tickers: next.length ? next : null })
                  }}
                />
              ))}
            </div>
          )}
        </div>
      </fieldset>
    </div>
  )
}
