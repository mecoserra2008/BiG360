import { bandHints, fmtTouch, thesisSessions, touchAt } from '@/api/conformal/adapters'
import { useConformalEnvelope, useConformalTouch } from '@/api/conformal/hooks'
import { USE_MOCK } from '@/api/http'
import type { ThesisDetail } from '@/api/theses/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { SkeletonRows } from '@/components/ui/feedback'
import { Panel } from '@/components/ui/panel'
import { fmtPct, fmtPrice } from '@/lib/format'
import { ModelBasedNote } from './notes'

/**
 * Thesis tracker: historical frequency of touching the thesis target and stop within the horizon (calibrated touch
 * curves; horizons beyond 20 sessions are an extrapolation and labelled so) plus the 20-session validated figure.
 */
export function ThesisTouchPanel({ t }: { t: ThesisDetail }) {
  const sid = t.subject_type === 'security' ? (t.security_id ?? t.price_id ?? '') : ''
  const levels = [t.target, t.stop].filter((x): x is number => typeof x === 'number' && x > 0)
  const { sessions, extrapolated } = thesisSessions(t.horizon)
  const q = useConformalTouch(sid, sessions, levels, !USE_MOCK && !!sid && levels.length > 0 && t.status !== 'closed')
  if (!sid || USE_MOCK || t.status === 'closed') return null
  const row = (price: number | null | undefined) => q.data?.levels.find((l) => price != null && Math.abs((l.price ?? 0) - price) <= Math.abs(price) * 1e-6)
  const items: [string, number | null | undefined, 'up' | 'down'][] = [
    ['Target', t.target, 'up'],
    ['Stop', t.stop, 'down'],
  ]
  return (
    <Panel
      id="th-touch"
      title="Chance of touching target / stop"
      subtitle={`Historical frequency of touching the level within ${t.horizon} (~${sessions} sessions) for similar setups${extrapolated ? ' · beyond 20 sessions: extrapolated, not validated' : ''}`}
      actions={<Badge tone="info">model-based</Badge>}
    >
      {!levels.length ? (
        <p className="text-xs text-muted">Set a target and / or a stop in the editor to see the calibrated touch frequencies.</p>
      ) : q.isPending ? (
        <SkeletonRows rows={2} />
      ) : q.isError || !q.data || q.data.data_status === 'pending' ? (
        <p className="text-xs text-muted">Touch calibration is not available for this security yet.</p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th className="h-7 text-left font-semibold">Level</th>
              <th className="text-right font-semibold">Price</th>
              <th className="text-right font-semibold">Distance</th>
              <th className="text-right font-semibold">Within {t.horizon}</th>
              <th className="text-right font-semibold" title="Validated horizon">Within 20 sessions</th>
            </tr>
          </thead>
          <tbody>
            {items.map(([label, price, tone]) => {
              const l = row(price)
              if (price == null || !l) return null
              return (
                <tr key={label} className="border-t border-border">
                  <td className={tone === 'up' ? 'h-7 text-up' : 'h-7 text-down'}>{label}</td>
                  <td className="num text-right">{fmtPrice(price, t.price_currency ?? undefined)}</td>
                  <td className="num text-right text-muted">{fmtPct(l.distance_pct, 1, true)}</td>
                  <td className="num text-right font-semibold">{fmtTouch(l.p_touch)}</td>
                  <td className="num text-right">{fmtTouch(touchAt(l, 20))}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      <ModelBasedNote className="mt-2">
        A path frequency from the security's calibration pool (standardised distance vs the calibrated running high / low), calibrated within 0.2 pp
        over 2011–2026 up to 20 sessions. Not a probability that the thesis is right.
      </ModelBasedNote>
    </Panel>
  )
}

/** Editor: optional entry / stop / target hints from the calibrated range (20 sessions, 80% / 95% edges). */
export function BandHintButtons({ securityId, direction, onApply }: { securityId: string; direction: string; onApply: (h: { entryLo?: string; entryHi?: string; stop?: string; target?: string }) => void }) {
  const env = useConformalEnvelope(securityId, !USE_MOCK && !!securityId)
  const h = bandHints(env.data, direction)
  if (!h) return null
  const f = (v: number) => v.toFixed(2)
  return (
    <div className="mt-2 rounded-md border border-border bg-surface-2 p-2">
      <div className="mb-1 flex items-center gap-1.5 text-2xs text-muted">
        <Badge tone="info">optional</Badge> Calibrated-range hints (20 sessions; calibrated to 80% / 95% over history, not a forecast)
      </div>
      <div className="flex flex-wrap gap-1">
        <Button size="xs" variant="outline" title="Between the 80% band edge and the last close" onClick={() => onApply({ entryLo: f(Math.min(h.entryLo, h.entryHi)), entryHi: f(Math.max(h.entryLo, h.entryHi)) })}>
          Entry {f(Math.min(h.entryLo, h.entryHi))}–{f(Math.max(h.entryLo, h.entryHi))}
        </Button>
        <Button size="xs" variant="outline" title="Beyond the 95% band edge" onClick={() => onApply({ stop: f(h.stop) })}>
          Stop {f(h.stop)}
        </Button>
        <Button size="xs" variant="outline" title="The opposite 95% band edge" onClick={() => onApply({ target: f(h.target) })}>
          Target {f(h.target)}
        </Button>
      </div>
    </div>
  )
}
