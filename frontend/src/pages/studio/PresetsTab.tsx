import { Play, Search, Timer, Upload } from 'lucide-react'
import { useState } from 'react'
import { useStudioMethods, useStudioPresets } from '@/api/studio/hooks'
import type { CStudySpec, StudioPresetView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Input, Select } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { fmtSeconds } from './fmt'
import { filterPresets, groupLabel } from './presetFilter'
import { useStudio } from './store'

function runtimeLabel(s: number | null): string | null {
  if (s === null) return null
  return `≈ ${s < 10 ? fmtSeconds(s) : fmtSeconds(Math.round(s))}`
}

function describe(p: StudioPresetView): string {
  const d = p.study.dataset
  const parts = [d.kind.replace('_', '-'), { D: 'daily', W: 'weekly', M: 'monthly', Q: 'quarterly' }[d.frequency] ?? d.frequency]
  if (d.start) parts.push(`from ${d.start.slice(0, 4)}`)
  if (d.entities?.universe) parts.push(`universe ${d.entities.universe}`)
  if (d.entity_level && d.entity_level !== 'security') parts.push(`by ${d.entity_level}`)
  return parts.join(' · ')
}

export function PresetsTab({ onRun }: { onRun: (spec: CStudySpec) => void }) {
  const q = useStudioPresets()
  const methods = useStudioMethods()
  const loadSpec = useStudio((s) => s.loadSpec)
  const [group, setGroup] = useState('')
  const [search, setSearch] = useState('')
  const load = (p: StudioPresetView) => {
    const m = methods.data?.find((x) => x.id === p.study.method)
    loadSpec(p.study, { needsY: m?.needsY, presetId: p.id, studyId: null })
  }
  return (
    <QueryView
      query={q}
      skeleton={
        <div className="grid grid-cols-2 gap-3 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-40" />
          ))}
        </div>
      }
    >
      {(all) => {
        const groups = [...new Set(all.map((p) => p.question ?? 'other'))].sort()
        const presets = filterPresets(all, group, search)
        return (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-1.5">
              <div className="relative min-w-48 flex-1 sm:max-w-80">
                <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
                <Input className="w-full pl-7" placeholder="Search recipes (title, description, method note)" aria-label="Search recipes" value={search} onChange={(e) => setSearch(e.target.value)} />
              </div>
              {groups.length > 1 && (
                <Select
                  label="Question group"
                  value={group}
                  onChange={setGroup}
                  options={[
                    { value: '', label: `All questions (${all.length})` },
                    ...groups.map((g) => ({ value: g, label: `${groupLabel(g)} (${all.filter((p) => (p.question ?? 'other') === g).length})` })),
                  ]}
                />
              )}
              <span className="num text-2xs text-muted">
                {presets.length} / {all.length} recipes
              </span>
            </div>
            {presets.length === 0 ? (
              <EmptyState title="No recipe matches" message="Clear the search or pick another question group." />
            ) : (
              <div className="grid grid-cols-2 gap-3 xl:grid-cols-3 2xl:grid-cols-4">
                {presets.map((p) => {
                  const m = methods.data?.find((x) => x.id === p.method)
                  return (
                    <article key={p.id} className="flex min-w-0 flex-col rounded-md border border-border bg-surface p-3">
                      <div className="flex items-start gap-2">
                        <h3 className="min-w-0 flex-1 text-sm font-semibold text-fg">{p.title}</h3>
                        <Badge tone="accent">{m?.title ?? p.method}</Badge>
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-1">
                        {p.question && (
                          <button type="button" className="rounded-sm" title={`Show only “${groupLabel(p.question)}” recipes`} onClick={() => setGroup(p.question ?? '')}>
                            <Badge tone="info">{groupLabel(p.question)}</Badge>
                          </button>
                        )}
                        {runtimeLabel(p.runtimeS) && (
                          <span
                            className="num inline-flex items-center gap-0.5 text-2xs text-muted"
                            title={`Measured end-to-end runtime on the real lake${p.validated ? ` (validated ${p.validated})` : ''}`}
                          >
                            <Timer className="size-3" aria-hidden /> {runtimeLabel(p.runtimeS)}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 line-clamp-3 text-xs text-muted">{p.description}</p>
                      {p.methodNote && (
                        <details className="mt-1 text-xs">
                          <summary className="cursor-pointer text-2xs font-semibold text-muted hover:text-fg">Method note</summary>
                          <p className="mt-0.5 text-fg/80">{p.methodNote}</p>
                        </details>
                      )}
                      {p.expectedOutputs.length > 0 && (
                        <div className="mt-1 text-2xs text-muted">
                          <span className="font-semibold">Returns:</span> {p.expectedOutputs.join(' · ')}
                        </div>
                      )}
                      <div className="num mt-2 text-2xs text-muted">{describe(p)}</div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {p.study.y && (
                          <Badge tone="neutral" className="normal-case">
                            y: {p.study.y}
                          </Badge>
                        )}
                        <Badge tone="neutral" className="normal-case">
                          {p.study.x?.length ?? 0} {m?.needsY === false ? 'features' : 'regressors'}
                        </Badge>
                        {p.tags.slice(0, 4).map((t) => (
                          <Badge key={t} tone="neutral" className="normal-case">
                            {t}
                          </Badge>
                        ))}
                      </div>
                      <div className="mt-auto flex gap-1.5 pt-3">
                        <Button
                          size="sm"
                          variant="primary"
                          onClick={() => {
                            load(p)
                            onRun(p.study)
                          }}
                        >
                          <Play /> Load & run
                        </Button>
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => {
                            load(p)
                            toast({ tone: 'info', title: 'Preset loaded into the builder', message: p.title })
                          }}
                        >
                          <Upload /> Load
                        </Button>
                      </div>
                    </article>
                  )
                })}
              </div>
            )}
          </div>
        )
      }}
    </QueryView>
  )
}
