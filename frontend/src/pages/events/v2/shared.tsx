import { ChevronDown } from 'lucide-react'
import type { EvTaxonomy } from '@/api/events/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Popover } from '@/components/ui/data'
import { Checkbox } from '@/components/ui/inputs'

/** Every /events/models output carries this badge. */
export function ModelBadge({ text = 'model-based' }: { text?: string }) {
  return <Badge tone="info">{text}</Badge>
}

/** Theme multi-select grouped by taxonomy group (empty = all themes). */
export function ThemeMultiSelect({ tax, value, onChange, max = 12, label = 'Themes' }: { tax: EvTaxonomy; value: string[]; onChange: (v: string[]) => void; max?: number; label?: string }) {
  const groups = [...new Set(tax.themes.map((t) => t.group))].sort()
  const name = (id: string) => tax.themes.find((t) => t.id === id)?.name ?? id
  const summary = value.length === 0 ? 'All themes' : value.length === 1 ? name(value[0]) : `${value.length} themes`
  return (
    <Popover
      className="max-h-[420px] w-[420px] overflow-y-auto"
      trigger={
        <Button size="sm" variant="outline" aria-label={`${label}: ${summary}`}>
          <span className="max-w-[160px] truncate">{summary}</span>
          <ChevronDown className="size-3.5" />
        </Button>
      }
    >
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-semibold">{label}</span>
        <Button size="xs" variant="ghost" onClick={() => onChange([])}>
          All themes
        </Button>
      </div>
      {groups.map((g) => (
        <div key={g} className="mb-2">
          <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">{g}</div>
          <ul className="grid grid-cols-2 gap-x-3 gap-y-1">
            {tax.themes
              .filter((t) => t.group === g)
              .map((t) => {
                const on = value.includes(t.id)
                return (
                  <li key={t.id}>
                    <label className="flex cursor-pointer items-center gap-2 text-xs">
                      <Checkbox label={t.name} checked={on} disabled={!on && value.length >= max} onCheckedChange={(c) => onChange(c === true ? [...value, t.id] : value.filter((x) => x !== t.id))} />
                      <span className="truncate">{t.name}</span>
                    </label>
                  </li>
                )
              })}
          </ul>
        </div>
      ))}
    </Popover>
  )
}
