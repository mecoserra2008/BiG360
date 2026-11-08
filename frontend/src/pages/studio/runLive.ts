/**
 * Live run chrome shared by Model Studio and the Backtesting lab (docs/STUDIO_V2_PLAN.md S1): where a run executes,
 * its queue position / what it waits behind, a live ETA and the learned estimate breakdown. Pure functions (tested).
 */
import type { RunEstimateView, RunPreviewInfo, RunQueueView } from '@/api/studio/types'
import { fmtSeconds } from './fmt'

export interface LiveRun {
  status: string
  lane: 'sync' | 'lane' | 'worker' | null
  stage: string | null
  etaS: number | null
  queue: RunQueueView | null
  estimate: RunEstimateView | null
  estimateS: number | null
}

const STAGE: Record<string, string> = {
  dataset: 'building the dataset',
  panel: 'building the panel',
  preview: 'computing the preview',
  fit: 'fitting',
  full: 'full run (robustness)',
}

export function pendingHeadline(r: LiveRun): string {
  if (r.status === 'queued') {
    if (r.lane === 'lane') {
      const pos = r.queue?.position
      return pos && pos > 1 ? `Queued on the studio lane · #${pos}` : 'Starting on the studio lane'
    }
    return 'Queued on the worker (studio lane unavailable)'
  }
  const st = r.stage ? STAGE[r.stage] ?? r.stage : null
  return st ? `Running · ${st}` : 'Running'
}

/** Seconds left: the server's ETA (refreshed by every poll), counted down locally between polls. */
export function remainingS(r: LiveRun, elapsedS: number | null, sincePollS = 0): number | null {
  if (r.etaS !== null) return Math.max(0, r.etaS - sincePollS)
  const total = r.estimate?.totalS ?? r.estimateS
  return total !== null && total !== undefined && elapsedS !== null ? Math.max(0, total - elapsedS) : null
}

export function progressPct(r: LiveRun, elapsedS: number | null): number | null {
  const total = r.estimate?.totalS ?? r.estimateS
  if (!total || elapsedS === null) return null
  return Math.min(95, (100 * elapsedS) / total)
}

export function estimateBreakdown(e: RunEstimateView | null): string | null {
  if (!e) return null
  const parts = [
    e.queueS > 0.05 ? `queue ${fmtSeconds(e.queueS)}` : null,
    e.overheadS > 0.05 ? `${e.path === 'worker' ? 'worker start' : 'overhead'} ${fmtSeconds(e.overheadS)}` : null,
    `data ${fmtSeconds(e.buildS)}`,
    `fit ${fmtSeconds(e.fitS)}`,
  ].filter(Boolean)
  const src = e.model === 'learned' ? `learned from ${e.nHistory} run${e.nHistory === 1 ? '' : 's'}` : e.model === 'global' ? 'learned (all methods)' : 'static formula'
  return `≈ ${fmtSeconds(e.totalS)} (${parts.join(' · ')}) · ${src}`
}

export function previewLabel(p: RunPreviewInfo): string {
  if (p.status === 'failed') return `Preview failed: ${p.error ?? 'unknown error'}`
  if (p.by === 'robustness') return `Preview without ${p.skipped.length ? p.skipped.join(', ').replace('stale_check', 'stale check') : 'robustness'}${p.durationS !== null ? ` (${fmtSeconds(p.durationS)})` : ''} — the full run is refining it`
  const pct = p.fraction ? `${Math.round(p.fraction * 100)} %` : 'a sample'
  const what = p.by === 'dates' ? 'of the most recent dates' : 'of the entities'
  const rows = p.nRows !== null && p.nRowsFull !== null ? ` · ${p.nRows.toLocaleString('en-US')} of ${p.nRowsFull.toLocaleString('en-US')} rows` : ''
  return `Preview on ${pct} ${what}${rows} — the full run is refining it`
}

/** Heavy extras of the estimate: enabled ones add to the time; disabled ones say what they would cost. */
export function extrasLine(e: RunEstimateView | null): string | null {
  if (!e || !e.extras.length) return null
  return e.extras.map((x) => `${x.label} ${x.enabled ? '+' : 'off · would add ≈ '}${fmtSeconds(x.costS)}`).join(' · ')
}
