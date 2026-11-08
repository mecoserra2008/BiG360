import { FlaskConical, Info } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import { VALIDATION_ROUTE, calibrationText, coverageFor } from '@/api/conformal/adapters'
import type { CfEnvelope } from '@/api/conformal/types'
import { Popover } from '@/components/ui/data'
import { cn } from '@/lib/utils'

/** "Experimental - failed / neutral in validation" banner with a link to the validation page. */
export function ExperimentalNote({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <div role="note" className={cn('flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-2.5 py-1.5 text-xs text-warn', className)}>
      <FlaskConical className="mt-px size-3.5 shrink-0" aria-hidden />
      <span>
        <b>Experimental — failed / neutral in validation.</b> {children}{' '}
        <Link to={VALIDATION_ROUTE} className="font-semibold underline underline-offset-2 hover:text-fg">
          Validation report
        </Link>
      </span>
    </div>
  )
}

/** Model-based label used by descriptive conformal panels. */
export function ModelBasedNote({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <p className={cn('flex items-start gap-1.5 text-2xs text-muted', className)}>
      <Info className="mt-px size-3 shrink-0" aria-hidden />
      <span>
        {children}{' '}
        <Link to={VALIDATION_ROUTE} className="underline underline-offset-2 hover:text-fg">
          How it was validated
        </Link>
      </span>
    </p>
  )
}

/** Small "calibration" popover for the calibrated range: wording per the validation gate, realised coverage. `fan` adds
 * the notes of the chart fan (every horizon 1..20; Technicals v2 T1). */
export function CalibrationPopover({ env, level, fan, label = 'Calibration' }: { env: CfEnvelope | undefined; level: number; fan?: { source: string; adjusted: boolean } | null; label?: string }) {
  const hz = [1, 5, 10, 20]
  return (
    <Popover
      trigger={
        <button type="button" className="inline-flex h-6 items-center gap-1 rounded-sm border border-border px-1.5 text-2xs text-muted hover:bg-surface-3 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus" aria-label="Calibration details">
          <Info className="size-3" aria-hidden /> {label}
        </button>
      }
      className="w-[360px]"
    >
      <div className="space-y-2 text-xs">
        <div className="font-semibold text-fg">Calibrated range ({level}%)</div>
        <p className="rounded-sm bg-surface-2 px-2 py-1 font-medium text-fg" data-testid="calibration-headline">
          Calibrated to {level}% over history (2010–2026 walk-forward), not "{level}% chance today".
        </p>
        <p className="text-muted">
          Each band is <b>calibrated to {level}% over history</b>: across all names and days of the calibration pool ({env?.pool_key ?? 'region × sector'}),
          about {level}% of h-session closes ended inside the h-session band (2010–2026 walk-forward). It is <b>not</b> "{level}% chance today" for this stock, it
          does not predict direction, and it is no sharper than a calibrated ATR band — its value over a 2×ATR band is honest calibration.
        </p>
        <table className="w-full">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th className="py-1 text-left font-semibold">Horizon</th>
              <th className="text-right font-semibold">Walk-forward</th>
              <th className="text-right font-semibold">Last year ({env?.region ?? 'region'})</th>
            </tr>
          </thead>
          <tbody>
            {hz.map((h) => {
              const c = coverageFor(env, h, level)
              const f = (v: number | null | undefined) => (v == null ? '—' : `${(v * 100).toFixed(1)}%`)
              return (
                <tr key={h} className="border-t border-border">
                  <td className="py-1">{h} session{h > 1 ? 's' : ''}</td>
                  <td className="num text-right">{f(c.walkForward?.coverage)}</td>
                  <td className="num text-right">{f(c.rolling?.coverage)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="text-2xs text-faint">{calibrationText(env, 20, level)}.</p>
        {env?.earn_in_window && <p className="text-2xs text-warn">Earnings expected within 20 sessions: event calibration pool, wider bands.</p>}
        {fan && (
          <p className="text-2xs text-muted">
            <b className="text-fg">Fan:</b> the band for every horizon from 1 to 20 sessions, each calibrated alone (a marginal band per date, not a joint path band); the
            three shades are 95 / 90 / 80%, and the dashed centre is the last close — no direction call.
            {fan.source === 'interpolated' ? ' This snapshot predates the per-horizon calibration: radii between 1 / 5 / 10 / 20 sessions are interpolated (on target in the walk-forward too).' : ' Radius calibrated at each horizon (walk-forward 89.6–90.0% at the 90% level for every h).'}
            {fan.adjusted ? ' Drawn as a non-narrowing envelope where a longer horizon came out slightly narrower (only ever wider).' : ''}
          </p>
        )}
        <p className="text-2xs text-muted">
          The joint 1/5/10/20 simultaneous envelope failed validation and is not shown. Model-based.{' '}
          <Link to={VALIDATION_ROUTE} className="underline underline-offset-2 hover:text-fg">
            Validation report
          </Link>
        </p>
      </div>
    </Popover>
  )
}
