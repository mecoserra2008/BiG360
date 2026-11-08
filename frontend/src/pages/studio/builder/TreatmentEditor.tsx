import { Plus, X } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { useSearch, useSectorTree } from '@/api/hooks'
import type { StudioTreatment } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { cn } from '@/lib/utils'

const REGIONS = ['US', 'CA', 'LATAM', 'UK', 'EU', 'JP', 'KR', 'CN', 'HK', 'APAC', 'OTHER']
const lbl = 'mb-0.5 block text-2xs font-semibold tracking-wide text-muted uppercase'

/** Ticker / label input with security search suggestions; Enter commits free text (sector or entity ids too). */
export function EntityInput({ value, onCommit, placeholder = 'Ticker or entity', label, className, keepValue = false }: { value?: string; onCommit: (v: string) => void; placeholder?: string; label: string; className?: string; keepValue?: boolean }) {
  const [q, setQ] = useState<string | null>(null)
  const text = q ?? value ?? ''
  const dq = useDeferredValue(q ?? '')
  const res = useSearch(dq)
  const items = q && q.trim() ? (res.data ?? []).slice(0, 6) : []
  const commit = (v: string) => {
    if (!v.trim()) return
    onCommit(v.trim())
    setQ(keepValue ? null : '')
  }
  return (
    <div className={cn('relative', className)}>
      <Input
        className="num w-full"
        aria-label={label}
        placeholder={placeholder}
        value={text}
        onChange={(e) => setQ(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            commit(text)
          }
        }}
        onBlur={() => {
          if (keepValue && q !== null && q !== value) commit(q)
        }}
      />
      {items.length > 0 && (
        <ul className="absolute top-full right-0 left-0 z-30 mt-0.5 max-h-52 overflow-y-auto rounded-md border border-border-strong bg-surface py-0.5 text-sm" role="listbox" aria-label="Matching securities">
          {items.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className="flex w-full items-center gap-2 px-2 py-1 text-left hover:bg-surface-2"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => commit(s.ticker)}
              >
                <span className="num w-16 shrink-0 font-medium text-fg">{s.ticker}</span>
                <span className="truncate text-xs text-muted">{s.name}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function Chips({ values, onChange }: { values: string[]; onChange: (v: string[]) => void }) {
  if (!values.length) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {values.map((v) => (
        <span key={v} className="num inline-flex h-5 items-center gap-0.5 rounded-sm border border-border bg-surface-3 pl-1.5 text-2xs text-fg">
          {v}
          <button type="button" aria-label={`Remove ${v}`} className="px-0.5 text-faint hover:text-down" onClick={() => onChange(values.filter((x) => x !== v))}>
            <X className="size-3" />
          </button>
        </span>
      ))}
    </div>
  )
}

/** Explicit (entity, date) events — staggered DiD adoption or event-study events. */
export function EventsEditor({ value, onChange }: { value: { entity: string; date: string }[]; onChange: (v: { entity: string; date: string }[]) => void }) {
  const [date, setDate] = useState('')
  return (
    <div className="space-y-1">
      {value.map((ev, i) => (
        <div key={`${ev.entity}-${ev.date}-${i}`} className="flex items-center gap-1">
          <span className="num w-20 truncate text-xs text-fg">{ev.entity}</span>
          <Input
            type="date"
            aria-label={`Event date for ${ev.entity}`}
            className="num flex-1"
            value={ev.date}
            onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, date: e.target.value } : x)))}
          />
          <Button size="icon-sm" variant="ghost" aria-label={`Remove event ${ev.entity}`} onClick={() => onChange(value.filter((_, j) => j !== i))}>
            <X />
          </Button>
        </div>
      ))}
      <div className="flex items-center gap-1">
        <EntityInput label="Event entity" placeholder="Ticker…" className="w-28" onCommit={(entity) => date && onChange([...value, { entity, date }])} />
        <Input type="date" aria-label="New event date" className="num flex-1" value={date} onChange={(e) => setDate(e.target.value)} />
      </div>
      <p className="text-2xs text-muted">{date ? 'Pick or type an entity and press Enter to add the event.' : 'Set the date first, then add entities (Enter).'}</p>
      {value.length === 0 && (
        <p className="flex items-center gap-1 text-2xs text-faint">
          <Plus className="size-3" /> no events yet
        </p>
      )}
    </div>
  )
}

/** DiD treatment definition: listed entities, an attribute (sector / region / country), a 0/1 column, or explicit events. */
export function TreatmentEditor({ value, onChange, variables }: { value: StudioTreatment | null; onChange: (t: StudioTreatment) => void; variables: string[] }) {
  const t: StudioTreatment = value ?? { mode: 'entities' }
  const mode = t.mode ?? 'entities'
  const sectors = useSectorTree('ALL')
  const set = (p: Partial<StudioTreatment>) => onChange({ ...t, ...p })
  const attr = t.attribute ?? 'sector_id'
  const options = attr === 'sector_id' ? (sectors.data ?? []).map((s) => s.id) : attr === 'region' ? REGIONS : []
  return (
    <div className="space-y-2 rounded-md border border-border bg-surface p-2">
      <Segmented
        label="Treatment definition"
        size="xs"
        value={mode}
        onChange={(m) => set({ mode: m })}
        options={[
          { value: 'entities', label: 'Entities' },
          { value: 'attribute', label: 'Attribute' },
          { value: 'column', label: 'Variable' },
          { value: 'events', label: 'Events' },
        ]}
      />
      {mode === 'entities' && (
        <div>
          <span className={lbl}>Treated entities</span>
          <EntityInput label="Add treated entity" onCommit={(v) => set({ entities: [...new Set([...(t.entities ?? []), v])] })} />
          <Chips values={t.entities ?? []} onChange={(entities) => set({ entities })} />
        </div>
      )}
      {mode === 'attribute' && (
        <div className="space-y-1.5">
          <Select
            label="Attribute"
            className="w-full"
            value={attr}
            onChange={(a) => set({ attribute: a as StudioTreatment['attribute'], values: [] })}
            options={[
              { value: 'sector_id', label: 'Sector' },
              { value: 'region', label: 'Region' },
              { value: 'country', label: 'Country (ISO-2)' },
            ]}
          />
          {options.length ? (
            <div className="flex flex-wrap gap-1" role="group" aria-label="Treated values">
              {options.map((o) => {
                const on = (t.values ?? []).includes(o)
                return (
                  <button
                    key={o}
                    type="button"
                    aria-pressed={on}
                    onClick={() => set({ values: on ? (t.values ?? []).filter((x) => x !== o) : [...(t.values ?? []), o] })}
                    className={cn('h-5 rounded-sm border px-1.5 text-2xs font-medium', on ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:text-fg')}
                  >
                    {o}
                  </button>
                )
              })}
            </div>
          ) : (
            <>
              <EntityInput label="Add country code" placeholder="e.g. US" onCommit={(v) => set({ values: [...new Set([...(t.values ?? []), v.toUpperCase()])] })} />
              <Chips values={t.values ?? []} onChange={(values) => set({ values })} />
            </>
          )}
        </div>
      )}
      {mode === 'column' && (
        <Select
          label="Treatment variable (0/1)"
          className="w-full"
          value={t.column ?? ''}
          onChange={(c) => set({ column: c || null })}
          options={[{ value: '', label: 'Pick a 0/1 variable…' }, ...variables.map((v) => ({ value: v, label: v }))]}
        />
      )}
      {mode === 'events' ? (
        <EventsEditor value={t.events ?? []} onChange={(events) => set({ events })} />
      ) : mode !== 'column' ? (
        <label className="block">
          <span className={lbl}>Treatment date</span>
          <Input type="date" aria-label="Treatment date" className="num w-full" value={t.date ?? ''} onChange={(e) => set({ date: e.target.value || null })} />
        </label>
      ) : null}
    </div>
  )
}
