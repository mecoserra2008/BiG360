import { useMemo } from 'react'
import { psiStatus } from '@/api/studio/adaptersS3'
import type { PsiStatus, StudioModelMonitorView, StudioPlot } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { DASH } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtStat } from '../fmt'
import { PlotView } from '../results/charts'

const PSI_TONE: Record<PsiStatus, 'up' | 'warn' | 'down'> = { stable: 'up', watch: 'warn', drift: 'down' }

function PsiBadge({ status }: { status: PsiStatus | null }) {
  if (!status) return <span className="text-faint">{DASH}</span>
  return <Badge tone={PSI_TONE[status]}>{status}</Badge>
}

const METRIC_LABEL: Record<string, string> = { rank_ic: 'expanding OOS rank IC', r2_oos: 'expanding OOS R²', none: 'no OOS metric' }

/** Result of POST /studio/registry/{id}/monitor: drift verdict, expanding OOS metric, feature / prediction PSI. */
export function MonitorView({ mon }: { mon: StudioModelMonitorView }) {
  const kind = mon.metric?.kind ?? 'none'
  const plot = useMemo<StudioPlot | null>(() => {
    const pts = mon.expanding
    if (!pts.length) return null
    const ic = pts.some((p) => p.expandingIc !== null)
    const series = ic
      ? [{ name: 'expanding rank IC', x: pts.map((p) => p.date), y: pts.map((p) => p.expandingIc) }]
      : [{ name: 'expanding R² OOS', x: pts.map((p) => p.date), y: pts.map((p) => p.expandingR2Oos) }]
    if (mon.metricFloor !== null) series.push({ name: 'floor', x: [pts[0].date, pts[pts.length - 1].date], y: [mon.metricFloor, mon.metricFloor] })
    return { kind: 'line', title: 'Expanding out-of-sample metric', y_label: ic ? 'rank IC' : 'R² OOS', series }
  }, [mon.expanding, mon.metricFloor])
  const predStatus = psiStatus(mon.predictionPsi)
  const w = mon.currentWindow

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <Badge tone={mon.drift ? 'down' : 'up'}>{mon.drift ? 'drift' : 'no drift'}</Badge>
        <span className="text-muted">
          {METRIC_LABEL[kind] ?? kind}: <span className="num font-semibold text-fg">{fmtStat(mon.metric?.value ?? null, 3)}</span>
          {mon.metric?.baseline !== null && mon.metric?.baseline !== undefined && <span className="num"> (baseline {fmtStat(mon.metric.baseline, 3)})</span>}
        </span>
        <span className="text-muted">
          prediction PSI <span className="num font-semibold text-fg">{fmtStat(mon.predictionPsi, 3)}</span> <PsiBadge status={predStatus} />
        </span>
        <span className="num text-muted">
          PSI threshold {fmtStat(mon.psiThreshold)} · floor {mon.metricFloor === null ? 'none' : fmtStat(mon.metricFloor)}
        </span>
        {w && (w.start || w.end) && (
          <span className="num text-muted" title={w.afterBaselineEnd ? 'The evaluated window has data after the baseline end' : 'No data after the baseline end yet'}>
            window {w.start?.slice(0, 10) ?? DASH} → {w.end?.slice(0, 10) ?? DASH}
            {w.afterBaselineEnd === false ? ' (no new data since promotion)' : ''}
          </span>
        )}
        <span className="num text-2xs text-faint">run {mon.runId}</span>
      </div>

      {mon.breaches.length > 0 && (
        <ul role="alert" className="space-y-0.5 rounded-md border border-down/50 bg-down-soft px-2 py-1.5 text-xs text-down" aria-label="Monitoring breaches">
          {mon.breaches.map((b) => (
            <li key={b}>• {b}</li>
          ))}
        </ul>
      )}

      {plot ? <PlotView plot={plot} height={220} /> : <p className="text-xs text-muted">No expanding out-of-sample metric (the run has no out-of-fold predictions).</p>}

      {mon.featurePsi.length > 0 && (
        <div className="max-h-72 overflow-y-auto">
          <table className="w-full text-sm" aria-label="Feature PSI">
            <thead className="sticky top-0">
              <tr className="border-b border-border">
                <th className="bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide text-muted uppercase">Feature</th>
                <th className="bg-surface-2 px-2 py-1 text-right text-2xs font-semibold tracking-wide text-muted uppercase">PSI</th>
                <th className="bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide text-muted uppercase">Status</th>
                <th className="bg-surface-2 px-2 py-1 text-right text-2xs font-semibold tracking-wide text-muted uppercase">n current</th>
              </tr>
            </thead>
            <tbody>
              {mon.featurePsi.map((f) => (
                <tr key={f.feature} className="border-b border-border/60">
                  <td className="px-2 py-1 font-medium text-fg">{f.feature}</td>
                  <td className={cn('num px-2 py-1 text-right', f.status === 'drift' ? 'font-semibold text-down' : f.status === 'watch' ? 'text-warn' : 'text-fg')}>{fmtStat(f.psi, 3)}</td>
                  <td className="px-2 py-1">
                    <PsiBadge status={f.status} />
                  </td>
                  <td className="num px-2 py-1 text-right text-muted">{fmtStat(f.nCurrent)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-2xs text-muted">PSI &lt; 0.1 stable · 0.1–0.25 watch · ≥ 0.25 drift (vs the promotion baseline).</p>
        </div>
      )}
    </div>
  )
}
