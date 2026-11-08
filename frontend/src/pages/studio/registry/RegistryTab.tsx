import { Boxes } from 'lucide-react'
import { useState } from 'react'
import { modelHeadline } from '@/api/studio/adaptersS3'
import { useStudioRegistry } from '@/api/studio/hooks'
import type { StudioModelView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtStat, labelOf } from '../fmt'
import { useStudio } from '../store'
import { ModelDetail } from './ModelDetail'
import { STAGE_TONE } from './options'

function DriftBadge({ m }: { m: StudioModelView }) {
  const lm = m.lastMonitor
  if (!lm || lm.drift === null) return <Badge tone="neutral">never</Badge>
  return (
    <Badge tone={lm.drift ? 'down' : 'up'} title={lm.breaches.join('\n') || undefined}>
      {lm.drift ? `drift${lm.breaches.length ? ` (${lm.breaches.length})` : ''}` : 'ok'}
    </Badge>
  )
}

const th = 'bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'

/** v1.50 Model Registry: promoted runs as versioned models with stage, owner and drift monitoring. */
export function RegistryTab() {
  const q = useStudioRegistry()
  const modelId = useStudio((s) => s.modelId)
  const setModelId = useStudio((s) => s.setModelId)
  const [filter, setFilter] = useState('')
  const t = filter.trim().toLowerCase()
  const rows = (q.data ?? []).filter((m) => !t || `${m.name} ${m.owner} ${m.method} ${m.stage}`.toLowerCase().includes(t))
  return (
    <div className={cn('grid items-start gap-3', modelId && '2xl:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]')}>
      <Panel
        title="Model registry"
        subtitle="Promote a completed run from its results header · click a model for versions, stage, monitoring"
        flush
        actions={<Input placeholder="Filter models" aria-label="Filter models" className="w-40" value={filter} onChange={(e) => setFilter(e.target.value)} />}
      >
        {q.isPending ? (
          <SkeletonRows rows={6} className="p-3" />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : !q.data.length ? (
          <EmptyState icon={<Boxes />} title="No registered models" message="Open a completed run and use “Promote” to register it as a versioned model." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm" aria-label="Registered models">
              <thead>
                <tr className="border-b border-border">
                  <th className={th}>Name</th>
                  <th className={cn(th, 'text-right')}>Ver.</th>
                  <th className={th}>Stage</th>
                  <th className={th}>Owner</th>
                  <th className={th}>Method</th>
                  <th className={th}>Spec</th>
                  <th className={th}>Snapshot</th>
                  <th className={cn(th, 'text-right')}>Headline</th>
                  <th className={th}>Monitor</th>
                  <th className={th}>Drift</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((m) => {
                  const h = modelHeadline(m)
                  const sel = m.modelId === modelId
                  return (
                    <tr key={m.modelId} className={cn('cursor-pointer border-b border-border/60 hover:bg-surface-2', sel && 'bg-accent-soft/40')} onClick={() => setModelId(m.modelId)}>
                      <td className="max-w-52 px-2 py-1">
                        <button type="button" className="max-w-full truncate text-left font-medium text-fg hover:underline" aria-current={sel} onClick={() => setModelId(m.modelId)}>
                          {m.name}
                        </button>
                      </td>
                      <td className="num px-2 py-1 text-right text-fg">v{m.version}</td>
                      <td className="px-2 py-1">
                        <Badge tone={STAGE_TONE[m.stage]}>{m.stage}</Badge>
                      </td>
                      <td className="max-w-28 truncate px-2 py-1 text-muted">{m.owner || DASH}</td>
                      <td className="px-2 py-1 text-muted">{m.method}</td>
                      <td className="num px-2 py-1 text-muted" title={m.specHash}>
                        {m.specHash.slice(0, 8) || DASH}
                      </td>
                      <td className="num max-w-28 truncate px-2 py-1 text-muted">{m.snapshotId ?? DASH}</td>
                      <td className="num px-2 py-1 text-right whitespace-nowrap text-fg">{h ? `${labelOf(h.key)} ${fmtStat(h.value, 3)}` : DASH}</td>
                      <td className="px-2 py-1 text-muted">{m.monitor}</td>
                      <td className="px-2 py-1">
                        <DriftBadge m={m} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {rows.length === 0 && <p className="p-3 text-center text-xs text-muted">No model matches the filter.</p>}
          </div>
        )}
      </Panel>
      {modelId && <ModelDetail modelId={modelId} />}
    </div>
  )
}
