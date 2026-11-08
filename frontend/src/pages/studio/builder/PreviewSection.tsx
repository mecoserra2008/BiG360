import { useMutation } from '@tanstack/react-query'
import { Eye, Loader2, TriangleAlert } from 'lucide-react'
import { useMemo, useState } from 'react'
import { draftToSpec } from '@/api/studio/adapters'
import { studioApi } from '@/api/studio/client'
import type { StudioPreviewView, StudyDraft } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { DASH, fmtDate } from '@/lib/format'
import { fmtCell, fmtSeconds, fmtStat } from '../fmt'
import { CoverageBar } from '../results/tabs'

const th = 'sticky top-0 bg-surface-2 px-1.5 py-1 text-left text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'
const td = 'num px-1.5 py-[2px] whitespace-nowrap'

function PreviewBody({ p }: { p: StudioPreviewView }) {
  const [view, setView] = useState<'coverage' | 'stats' | 'rows'>('coverage')
  return (
    <div className="space-y-2">
      <div className="num flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-muted">
        <span>
          <b className="text-fg">{fmtStat(p.nRows)}</b> rows
        </span>
        <span>
          <b className="text-fg">{fmtStat(p.nEntities)}</b> entities
        </span>
        <span>
          <b className="text-fg">{fmtStat(p.nPeriods)}</b> periods
        </span>
        <span>
          {fmtDate(p.start.slice(0, 10))} → {fmtDate(p.end.slice(0, 10))}
        </span>
        <span>
          built in {fmtSeconds(p.buildSeconds)}
          {p.cached ? ' (cached)' : ''}
        </span>
      </div>
      {p.warnings.map((w, i) => (
        <p key={i} className="flex items-start gap-1.5 rounded-sm bg-warn-soft px-2 py-1 text-xs text-warn">
          <TriangleAlert className="mt-0.5 size-3 shrink-0" aria-hidden /> {w}
        </p>
      ))}
      <Segmented
        label="Preview view"
        size="xs"
        value={view}
        onChange={setView}
        options={[
          { value: 'coverage', label: 'Coverage' },
          { value: 'stats', label: 'Summary' },
          { value: 'rows', label: `Rows (${p.rows.length})` },
        ]}
      />
      <div className="max-h-72 overflow-auto rounded-sm border border-border">
        {view === 'coverage' ? (
          <table className="w-full text-xs" aria-label="Variable coverage">
            <thead>
              <tr>
                <th className={th}>Variable</th>
                <th className={th}>Coverage</th>
                <th className={th}>First</th>
                <th className={th}>Last</th>
              </tr>
            </thead>
            <tbody>
              {p.coverage.map((c) => (
                <tr key={c.column} className="border-t border-border/60">
                  <td className="px-1.5 py-[2px] font-medium text-fg">
                    {c.column}
                    {c.usesFuture && <span className="ml-1 text-2xs text-warn">future</span>}
                  </td>
                  <td className="px-1.5 py-[2px]">
                    <CoverageBar pct={c.pctNonNull} />
                  </td>
                  <td className={td}>{c.firstDate ? fmtDate(c.firstDate.slice(0, 10)) : DASH}</td>
                  <td className={td}>{c.lastDate ? fmtDate(c.lastDate.slice(0, 10)) : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : view === 'stats' ? (
          <table className="w-full text-xs" aria-label="Summary statistics">
            <thead>
              <tr>
                {['Variable', 'n', 'mean', 'std', 'min', 'median', 'max'].map((h, i) => (
                  <th key={h} className={i ? `${th} text-right` : th}>
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {p.stats.map((s) => (
                <tr key={s.column} className="border-t border-border/60">
                  <td className="px-1.5 py-[2px] font-medium text-fg">{s.column}</td>
                  {[s.n, s.mean, s.std, s.min, s.median, s.max].map((v, i) => (
                    <td key={i} className={`${td} text-right`}>
                      {fmtStat(v, 3)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="w-full text-xs" aria-label="First rows">
            <thead>
              <tr>
                {p.columns.map((c) => (
                  <th key={c.name} className={th} title={c.dtype}>
                    {c.name}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {p.rows.map((r, i) => (
                <tr key={i} className="border-t border-border/60">
                  {p.columns.map((c) => (
                    <td key={c.name} className={td}>
                      {fmtCell(r[c.name])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

/** Live dataset preview: builds the dataset only (no model) so coverage problems show up before a run. */
export function PreviewSection({ draft, disabled }: { draft: StudyDraft; disabled: boolean }) {
  const datasetJson = useMemo(() => JSON.stringify(draftToSpec(draft).dataset), [draft])
  const [shownFor, setShownFor] = useState<string | null>(null)
  const m = useMutation({
    mutationFn: (json: string) => studioApi.preview(JSON.parse(json), 50),
    onSuccess: (_d, json) => setShownFor(json),
  })
  const stale = !!m.data && shownFor !== datasetJson
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <Button size="sm" variant="secondary" disabled={disabled || m.isPending} onClick={() => m.mutate(datasetJson)}>
          {m.isPending ? <Loader2 className="animate-spin" /> : <Eye />} {m.data ? 'Refresh preview' : 'Preview dataset'}
        </Button>
        {stale && <span className="text-2xs text-warn">Builder changed since this preview</span>}
        {disabled && <span className="text-2xs text-muted">Add a variable first</span>}
      </div>
      {m.isError ? <ErrorState error={m.error} onRetry={() => m.mutate(datasetJson)} className="py-4" /> : m.data ? <PreviewBody p={m.data} /> : null}
    </div>
  )
}
