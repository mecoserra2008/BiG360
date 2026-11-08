import { useMemo } from 'react'
import type { StudioCompareOosView, StudioPlot, StudioRunStatusView } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { DASH } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtP, fmtStat } from '../fmt'
import { PlotView } from '../results/charts'

const verdictTone = (v: string | null): 'up' | 'down' | 'neutral' => (!v ? 'neutral' : /^beats/i.test(v) ? 'up' : /worse/i.test(v) ? 'down' : 'neutral')

const runName = (runs: (StudioRunStatusView | undefined)[], id: string): string => runs.find((r) => r?.runId === id)?.name ?? id

const th = 'bg-surface-2 px-2 py-1 text-right text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'
const td = 'num px-2 py-1 text-right whitespace-nowrap'

/** v1.50 out-of-fold comparison vs the benchmark: Diebold-Mariano / Clark-West tests, skipped runs, equity curves. */
export function OosCompare({ oos, runs }: { oos: StudioCompareOosView; runs: (StudioRunStatusView | undefined)[] }) {
  const nameOf = (id: string) => runName(runs, id)
  const plot = useMemo<StudioPlot | null>(() => {
    if (!oos.equity.length) return null
    const nameOf = (id: string) => runName(runs, id)
    const kinds = [...new Set(oos.equity.map((e) => e.kind))]
    return {
      kind: 'line',
      title: 'Out-of-fold equity curves',
      y_label: 'cumulative return',
      series: oos.equity.map((e) => ({
        name: `${nameOf(e.runId)} · Sharpe ${fmtStat(e.sharpe, 2)}${e.meanIc !== null ? ` · IC ${fmtStat(e.meanIc, 3)}` : ''}`,
        x: e.dates,
        y: e.cumReturn,
      })),
      x_label: kinds.length === 1 ? (kinds[0] === 'top_minus_bottom_quintile' ? 'top-minus-bottom quintile of the prediction, per date' : 'long / short on the sign of the prediction') : null,
    }
  }, [oos.equity, runs])

  return (
    <section className="space-y-3" aria-label="Out-of-fold comparison">
      <div>
        <h3 className="text-sm font-semibold text-fg">Out-of-fold forecast tests</h3>
        <p className="text-2xs text-muted">
          On the (date, entity) rows each run shares with the benchmark <span className="font-semibold text-fg">{oos.benchmarkRunId ? nameOf(oos.benchmarkRunId) : DASH}</span>. DM &gt; 0: the run is
          more accurate (per-date loss, HAC). Nested runs (benchmark regressors ⊂ run’s) use the Clark-West MSPE-adjusted test (one-sided).
        </p>
      </div>
      {oos.tests.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" aria-label="Out-of-fold forecast tests">
            <thead>
              <tr className="border-b border-border">
                <th className={cn(th, 'text-left')}>Run</th>
                <th className={cn(th, 'text-left')}>Benchmark</th>
                <th className={th}>Rows / dates</th>
                <th className={th}>MSE model</th>
                <th className={th}>MSE bench.</th>
                <th className={th}>DM</th>
                <th className={th}>DM p</th>
                <th className={th}>Nested</th>
                <th className={th}>CW</th>
                <th className={th}>CW p</th>
                <th className={cn(th, 'text-left')}>Verdict</th>
              </tr>
            </thead>
            <tbody>
              {oos.tests.map((t) => (
                <tr key={t.runId} className="border-b border-border/60 align-top">
                  <td className="max-w-48 truncate px-2 py-1 font-medium text-fg" title={t.runId}>
                    {nameOf(t.runId)}
                  </td>
                  <td className="max-w-40 truncate px-2 py-1 text-muted" title={t.benchmarkRunId}>
                    {nameOf(t.benchmarkRunId)}
                  </td>
                  <td className={cn(td, 'text-muted')}>
                    {fmtStat(t.nRows)} / {fmtStat(t.nDates)}
                  </td>
                  <td className={cn(td, t.mseModel !== null && t.mseBenchmark !== null && t.mseModel < t.mseBenchmark ? 'font-semibold text-up' : 'text-fg')}>{fmtStat(t.mseModel)}</td>
                  <td className={cn(td, 'text-fg')}>{fmtStat(t.mseBenchmark)}</td>
                  <td className={cn(td, 'text-fg')}>{fmtStat(t.dmStat, 3)}</td>
                  <td className={cn(td, typeof t.dmP === 'number' && t.dmP < 0.05 ? 'font-semibold text-fg' : 'text-muted')}>{fmtP(t.dmP)}</td>
                  <td className={cn(td, 'text-muted')}>{t.nested ? 'yes' : 'no'}</td>
                  <td className={cn(td, 'text-fg')}>{t.nested ? fmtStat(t.cwStat, 3) : DASH}</td>
                  <td className={cn(td, typeof t.cwP === 'number' && t.cwP < 0.05 ? 'font-semibold text-fg' : 'text-muted')}>{t.nested ? fmtP(t.cwP) : DASH}</td>
                  <td className="px-2 py-1">
                    {t.verdict ? (
                      <Badge tone={verdictTone(t.verdict)} className="normal-case">
                        {t.verdict}
                      </Badge>
                    ) : (
                      <span className="text-faint">{DASH}</span>
                    )}
                    {t.note && <div className="mt-0.5 max-w-64 text-2xs text-muted">{t.note}</div>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-xs text-muted">No run could be tested against the benchmark (see below).</p>
      )}
      {oos.skipped.length > 0 && (
        <div>
          <h4 className="text-2xs font-semibold tracking-wide text-muted uppercase">Skipped</h4>
          <ul className="mt-0.5 space-y-0.5 text-xs">
            {oos.skipped.map((s) => (
              <li key={s.runId}>
                <span className="font-medium text-fg">{nameOf(s.runId)}</span> <span className="text-muted">— {s.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {plot && (
        <div>
          <h4 className="text-2xs font-semibold tracking-wide text-muted uppercase">Equity curves (legend: Sharpe, mean IC)</h4>
          <PlotView plot={plot} height={280} />
        </div>
      )}
    </section>
  )
}
