import { describe, expect, it } from 'vitest'
import { adaptEstimate, adaptLane, adaptLive } from '@/api/studio/adapters'
import { estimateBreakdown, extrasLine, pendingHeadline, previewLabel, progressPct, remainingS } from '../runLive'

const est = adaptEstimate({
  total_s: 3.4, run_s: 3.1, queue_s: 0, overhead_s: 0.3, build_s: 0.1, fit_s: 2.9, post_s: 0.1, path: 'lane', model: 'learned',
  n_history: 12, extras: [{ name: 'shap', label: 'SHAP values', enabled: false, cost_s: 4.2 }],
})

describe('S1 live run chrome', () => {
  it('maps the live status fields', () => {
    const live = adaptLive({ lane: 'lane', stage: 'preview', eta_s: 2.5, queue: { position: 3, ahead: 2, blocked_by: 'studio SR1', queue_s: 4 }, estimate: null, preview: null })
    expect(live.queue).toEqual({ position: 3, ahead: 2, blockedBy: 'studio SR1', queueS: 4 })
    expect(pendingHeadline({ ...live, status: 'queued', estimate: null, estimateS: null })).toBe('Queued on the studio lane · #3')
    expect(pendingHeadline({ ...live, status: 'running', estimate: null, estimateS: null })).toBe('Running · computing the preview')
    expect(pendingHeadline({ ...live, lane: 'worker', status: 'queued', estimate: null, estimateS: null })).toMatch(/worker/)
  })

  it('formats the learned estimate, extras cost and ETA', () => {
    expect(estimateBreakdown(est)).toBe('≈ 3.4 s (overhead 300 ms · data 100 ms · fit 2.9 s) · learned from 12 runs')
    expect(extrasLine(est)).toBe('SHAP values off · would add ≈ 4.2 s')
    const r = { status: 'running', lane: 'lane' as const, stage: null, etaS: null, queue: null, estimate: est, estimateS: 3.4 }
    expect(remainingS(r, 1.4)).toBeCloseTo(2.0)
    expect(remainingS({ ...r, etaS: 1.5 }, 1.4)).toBe(1.5)
    expect(progressPct(r, 1.7)).toBeCloseTo(50)
  })

  it('labels previews', () => {
    expect(previewLabel({ status: 'completed', fraction: 0.2, by: 'dates', nRows: 360, nRowsFull: 1800, durationS: 0.4, error: null, skipped: [] }))
      .toBe('Preview on 20 % of the most recent dates · 360 of 1,800 rows — the full run is refining it')
    expect(previewLabel({ status: 'completed', fraction: null, by: 'robustness', nRows: null, nRowsFull: null, durationS: 2, error: null, skipped: ['permutations'] }))
      .toMatch(/^Preview without permutations \(2\.0 s\)/)
  })

  it('maps the lane status with accuracy diagnostics', () => {
    const l = adaptLane({ enabled: true, alive: true, running: [{}], queue: [], accuracy: { studio: { n: 40, median_ratio: 1.02, within_30pct: 0.55, median_abs_err: 0.3 } } })
    expect(l.running).toBe(1)
    expect(l.accuracy.studio).toEqual({ n: 40, medianRatio: 1.02, within30: 0.55 })
    expect(l.accuracy.backtest.n).toBe(0)
  })
})
