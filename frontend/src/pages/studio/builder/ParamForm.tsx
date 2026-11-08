import { RotateCcw } from 'lucide-react'
import { useState } from 'react'
import { validateParam } from '@/api/studio/adapters'
import type { LeaderboardCandidate, ParamField, StudioTreatment } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Input, Select, Switch } from '@/components/ui/inputs'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { CandidatesEditor } from './CandidatesEditor'
import { Chips, EntityInput, EventsEditor, TreatmentEditor } from './TreatmentEditor'

const same = (a: unknown, b: unknown) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null)

function numOrNull(s: string): number | null {
  if (s.trim() === '') return null
  const n = Number(s)
  return Number.isFinite(n) ? n : NaN
}

/** Text-backed control that keeps the raw string while typing and commits parsed values. */
function TextParam({ value, toText, parse, onChange, placeholder, label, className }: { value: unknown; toText: (v: unknown) => string; parse: (s: string) => unknown; onChange: (v: unknown) => void; placeholder?: string; label: string; className?: string }) {
  const [draft, setDraft] = useState<string | null>(null)
  return (
    <Input
      aria-label={label}
      className={cn('w-full', className)}
      value={draft ?? toText(value)}
      placeholder={placeholder}
      onChange={(e) => {
        setDraft(e.target.value)
        onChange(parse(e.target.value))
      }}
      onBlur={() => setDraft(null)}
    />
  )
}

function Control({ f, value, onChange, variables }: { f: ParamField; value: unknown; onChange: (v: unknown) => void; variables: string[] }) {
  switch (f.kind) {
    case 'boolean':
      return <Switch label={f.title} checked={value === true} onCheckedChange={(c) => onChange(c)} />
    case 'integer':
    case 'number':
      return (
        <TextParam
          label={f.title}
          value={value}
          toText={(v) => (typeof v === 'number' ? String(v) : '')}
          parse={(s) => numOrNull(s)}
          onChange={onChange}
          placeholder={f.nullable ? 'auto' : undefined}
          className="num"
        />
      )
    case 'date':
      return <Input type="date" aria-label={f.title} className="num w-full" value={typeof value === 'string' ? value : ''} onChange={(e) => onChange(e.target.value || null)} />
    case 'treatment':
      return <TreatmentEditor value={(value as StudioTreatment | null) ?? null} onChange={onChange} variables={variables} />
    case 'events':
      return <EventsEditor value={Array.isArray(value) ? (value as { entity: string; date: string }[]) : []} onChange={onChange} />
    case 'object': {
      const obj = value && typeof value === 'object' ? (value as Record<string, unknown>) : {}
      return (
        <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 rounded-sm border border-border p-2">
          {f.children.map((c) => (
            <label key={c.key} className={cn('min-w-0 text-2xs text-muted', c.kind === 'boolean' && 'flex items-center justify-between gap-2')} title={c.description ?? undefined}>
              <span className="mb-0.5 block truncate">{c.title}</span>
              <Control
                f={c}
                value={c.key in obj ? obj[c.key] : c.defaultValue}
                variables={variables}
                onChange={(nv) => {
                  const next = { ...obj }
                  if (same(nv, c.defaultValue)) delete next[c.key]
                  else next[c.key] = nv
                  onChange(Object.keys(next).length ? next : null)
                }}
              />
            </label>
          ))}
        </div>
      )
    }
    case 'candidates':
      return <CandidatesEditor value={Array.isArray(value) ? (value as LeaderboardCandidate[]) : []} onChange={onChange} />
    case 'string_list': {
      const arr = Array.isArray(value) ? (value as string[]) : []
      if (!/donor|entit|ticker/.test(f.key) && variables.length)
        return (
          <div className="flex flex-wrap gap-1" role="group" aria-label={f.title}>
            {variables.map((v) => {
              const on = arr.includes(v)
              return (
                <button
                  key={v}
                  type="button"
                  aria-pressed={on}
                  onClick={() => onChange(on ? arr.filter((x) => x !== v) : [...arr, v])}
                  className={cn('num h-5 rounded-sm border px-1.5 text-2xs', on ? 'border-accent bg-accent-soft text-accent-text' : 'border-border bg-surface-2 text-muted hover:text-fg')}
                >
                  {v}
                </button>
              )
            })}
          </div>
        )
      return (
        <div>
          <EntityInput label={`Add to ${f.title}`} onCommit={(v) => onChange([...new Set([...arr, v])])} />
          <Chips values={arr} onChange={onChange} />
        </div>
      )
    }
    case 'enum':
      return (
        <Select
          label={f.title}
          className="w-full"
          value={value === null || value === undefined ? '' : String(value)}
          onChange={(v) => onChange(v === '' ? null : (f.enumValues.find((x) => String(x) === v) ?? v))}
          options={[...(f.nullable ? [{ value: '', label: 'default' }] : []), ...f.options.map((o) => ({ value: o, label: o }))]}
        />
      )
    case 'string':
      if (/^(treated|entity)$/.test(f.key)) return <EntityInput label={f.title} value={typeof value === 'string' ? value : ''} keepValue onCommit={onChange} />
      if (/var|offset|market|column/.test(f.key) && variables.length)
        return (
          <Select
            label={f.title}
            className="w-full"
            value={typeof value === 'string' ? value : ''}
            onChange={(v) => onChange(v === '' ? null : v)}
            options={[{ value: '', label: f.nullable ? 'default' : '—' }, ...variables.map((v) => ({ value: v, label: v }))]}
          />
        )
      return <TextParam label={f.title} value={value} toText={(v) => (typeof v === 'string' ? v : '')} parse={(s) => (s === '' && f.nullable ? null : s)} onChange={onChange} placeholder={f.nullable ? 'default' : undefined} />
    case 'int_tuple': {
      const arr = Array.isArray(value) ? (value as unknown[]) : Array.from({ length: f.length ?? 1 }, () => 0)
      const names = f.key === 'order' ? ['p', 'd', 'q'] : f.key === 'seasonal_order' ? ['P', 'D', 'Q', 's'] : []
      return (
        <div className="flex gap-1">
          {Array.from({ length: f.length ?? arr.length }, (_, i) => (
            <label key={i} className="flex min-w-0 flex-1 items-center gap-1">
              {names[i] && <span className="text-2xs text-muted">{names[i]}</span>}
              <Input
                aria-label={`${f.title} ${names[i] ?? i + 1}`}
                type="number"
                className="num w-full min-w-0 px-1"
                value={typeof arr[i] === 'number' ? String(arr[i]) : ''}
                onChange={(e) => {
                  const next = [...arr]
                  next[i] = e.target.value === '' ? 0 : Math.trunc(Number(e.target.value))
                  onChange(next)
                }}
              />
            </label>
          ))}
        </div>
      )
    }
    case 'number_list':
      return (
        <TextParam
          label={f.title}
          value={value}
          toText={(v) => (Array.isArray(v) ? v.join(', ') : '')}
          parse={(s) =>
            s
              .split(/[,\s]+/)
              .filter(Boolean)
              .map(Number)
              .filter((n) => Number.isFinite(n))
          }
          onChange={onChange}
          placeholder="0.1, 0.5, 0.9"
          className="num"
        />
      )
    case 'int_map':
      return (
        <TextParam
          label={f.title}
          value={value}
          toText={(v) =>
            v && typeof v === 'object'
              ? Object.entries(v as Record<string, unknown>)
                  .map(([k, x]) => `${k}=${x}`)
                  .join(', ')
              : ''
          }
          parse={(s) =>
            Object.fromEntries(
              s
                .split(/[,;]+/)
                .map((p) => p.split('=').map((x) => x.trim()))
                .filter((p) => p.length === 2 && p[0] && Number.isInteger(Number(p[1])))
                .map(([k, x]) => [k, Number(x)]),
            )
          }
          onChange={onChange}
          placeholder="var=4, other=2"
        />
      )
    default:
      return (
        <TextParam
          label={f.title}
          value={value}
          toText={(v) => (v === null || v === undefined ? '' : JSON.stringify(v))}
          parse={(s) => {
            if (s.trim() === '') return null
            try {
              return JSON.parse(s) as unknown
            } catch {
              return s
            }
          }}
          onChange={onChange}
          placeholder="JSON"
          className="num"
        />
      )
  }
}

/** Form auto-generated from a method's params JSON schema. Values equal to the default are not sent. */
export function ParamForm({
  fields: allFields,
  params,
  onChange,
  variables,
  hidden,
}: {
  fields: ParamField[]
  params: Record<string, unknown>
  onChange: (p: Record<string, unknown>) => void
  variables: string[]
  /** params derived from variable roles (not edited here) */
  hidden?: Set<string>
}) {
  // required / structured inputs (treatment, events, dates) first
  const fields = allFields.filter((f) => !hidden?.has(f.key)).sort((a, b) => Number(b.required) - Number(a.required))
  const modified = fields.filter((f) => f.key in params && !same(params[f.key], f.defaultValue)).length
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-2 text-xs text-muted">
        <span>{fields.length} parameters</span>
        {modified > 0 && <span className="text-accent-text">· {modified} changed</span>}
        {modified > 0 && (
          <Button size="xs" variant="ghost" className="ml-auto" onClick={() => onChange({})}>
            <RotateCcw /> Defaults
          </Button>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-2">
        {fields.map((f) => {
          const v = f.key in params ? params[f.key] : f.defaultValue
          const err = validateParam(f, v)
          const changed = f.key in params && !same(params[f.key], f.defaultValue)
          const wide = ['int_tuple', 'json', 'int_map', 'number_list', 'string_list', 'treatment', 'events', 'object', 'candidates'].includes(f.kind)
          return (
            <div key={f.key} className={cn('min-w-0', wide && 'col-span-2', f.kind === 'boolean' && 'flex items-center justify-between gap-2')}>
              <div className={cn('flex items-center gap-1', f.kind !== 'boolean' && 'mb-0.5')}>
                {changed && <span className="size-1.5 shrink-0 rounded-full bg-accent" aria-label="changed from default" />}
                {f.required && <span className="text-2xs text-accent-text" aria-label="required">*</span>}
                <Tip content={f.description ? <span>{f.description}</span> : null}>
                  <span tabIndex={f.description ? 0 : undefined} className={cn('truncate text-xs', f.description ? 'cursor-help text-fg underline decoration-dotted decoration-faint underline-offset-2' : 'text-fg')}>
                    {f.title}
                  </span>
                </Tip>
              </div>
              <Control
                f={f}
                value={v}
                variables={variables}
                onChange={(nv) => {
                  const next = { ...params }
                  if (same(nv, f.defaultValue)) delete next[f.key]
                  else next[f.key] = nv
                  onChange(next)
                }}
              />
              {err && f.kind !== 'boolean' && f.kind !== 'treatment' && <div className="mt-0.5 text-2xs text-down">{err}</div>}
            </div>
          )
        })}
      </div>
    </div>
  )
}
