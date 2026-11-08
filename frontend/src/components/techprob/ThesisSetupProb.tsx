import { FlaskConical } from 'lucide-react'
import { fmtProb, thesisProbRows } from '@/api/techprob/adapters'
import { useBarrierProbabilities, useTechExperimental } from '@/api/techprob/hooks'
import { USE_MOCK } from '@/api/http'
import type { ThesisDetail } from '@/api/theses/types'
import { Badge } from '@/components/ui/badge'
import { SkeletonRows } from '@/components/ui/feedback'
import { Switch } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { fmtNum } from '@/lib/format'
import { ModelBasedNote } from '@/components/conformal/notes'
import { ThesisMetaRows } from '@/components/techmeta/MetaSetupRow'

/**
 * Thesis tracker (Technicals v2 T2): two-barrier probabilities for the thesis target vs stop from the last close -
 * P(target first), P(stop first), P(neither) within 5 / 10 / 20 / 40 / 60 sessions, expected R after costs. The same
 * calibrated surface as the Technicals tab's trade setup, evaluated at the thesis' own barriers (query time).
 */
export function ThesisSetupProbPanel({ t }: { t: ThesisDetail }) {
  const sid = t.subject_type === 'security' ? (t.security_id ?? t.price_id ?? '') : ''
  const { show, setShow } = useTechExperimental()
  const dir = t.direction === 'short' ? 'short' : t.direction === 'long' ? 'long' : undefined
  const ready = !USE_MOCK && !!sid && typeof t.target === 'number' && typeof t.stop === 'number' && t.status !== 'closed'
  const q = useBarrierProbabilities(sid, t.target, t.stop, dir, ready)
  if (!sid || USE_MOCK || t.status === 'closed') return null
  const rows = thesisProbRows(q.data, show)
  const hidden = (q.data?.horizons.length ?? 0) - rows.length
  return (
    <Panel
      id="th-setup-prob"
      title="Target before stop"
      subtitle="Chance the target is reached before the stop within H sessions, from the last close (two-barrier, calibrated on history)"
      actions={<Badge tone="info">model-based</Badge>}
    >
      {typeof t.target !== 'number' || typeof t.stop !== 'number' ? (
        <p className="text-xs text-muted">Set both a target and a stop to see the target-before-stop probabilities.</p>
      ) : q.isPending ? (
        <SkeletonRows rows={2} />
      ) : q.isError ? (
        <p className="text-xs text-muted">Target and stop must lie on opposite sides of the last close for this view.</p>
      ) : !q.data || q.data.data_status === 'pending' || !q.data.horizons.length ? (
        <p className="text-xs text-muted">The setup-probability model is not available for this security yet.</p>
      ) : (
        <>
          <table className="w-full text-sm" data-testid="thesis-setup-prob">
            <thead>
              <tr className="text-2xs tracking-wide text-muted uppercase">
                <th className="h-7 text-left font-semibold">Within</th>
                <th className="text-right font-semibold">Target first</th>
                <th className="text-right font-semibold">Stop first</th>
                <th className="text-right font-semibold">Neither</th>
                <th className="text-right font-semibold" title="P(target) x R:R - P(stop) - costs">Expected R</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.horizon} className="border-t border-border">
                  <td className="h-7">{r.horizon} sessions{r.own ? ' *' : ''}</td>
                  <td className="num text-right text-up">{r.win}</td>
                  <td className="num text-right text-down">{r.loss}</td>
                  <td className="num text-right text-muted">{r.neither}</td>
                  <td className="num text-right font-semibold">{r.er}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {/* Technicals v3 #2: context-adjusted (meta) view of the same barriers (query time) */}
          <ThesisMetaRows view={q.data.meta} />
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 text-2xs text-muted">
            <span>
              R:R from the close {fmtNum(q.data.setup?.reward_risk_close, 2)}× · break-even {fmtProb(q.data.horizons[0]?.break_even_p)} · * = horizon where the target is one sigma away
              {q.data.horizons[0] && ` · R:R-only rule ${fmtProb(q.data.horizons[0].p_win_naive)}`}
            </span>
            <label className="flex items-center gap-1.5">
              <Switch label="Show experimental outputs" checked={show} onCheckedChange={(v) => setShow(!!v)} />
              <FlaskConical className="size-3" aria-hidden /> experimental{hidden > 0 && !show ? ` (${hidden} hidden)` : ''}
            </label>
          </div>
        </>
      )}
      <ModelBasedNote className="mt-2">
        Historical first-passage frequency at the same distances in volatility units (conformal EWMA sigma incl. earnings), pooled by region × vol regime ×
        earnings window; a same-bar touch of both counts as stop. Horizons beyond 60 sessions are not modelled. Not a probability that the thesis is right.
      </ModelBasedNote>
    </Panel>
  )
}
