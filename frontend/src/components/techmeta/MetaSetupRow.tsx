/**
 * Technicals v3 #2: "Context-adjusted (meta)" row of the Trade setup card (Technicals tab) and the thesis "Target
 * before stop" panel. Meta-labelling (AFML ch. 3): a secondary model re-estimates the chance that the setup wins,
 * starting from the T2 two-barrier probability and adjusting it for market regime, risk gauge, VIX, peer break-aways,
 * insider / short-interest / smart-money flow, earnings proximity, squeeze, ADX and trend alignment.
 *
 * Shown by default only when its gate passed (docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"); otherwise it sits
 * behind the shared "show experimental" toggle. Take / skip and the suggested size have their own economic gate.
 */
import { FlaskConical } from 'lucide-react'
import { metaGateLine, metaRow, pickMetaHorizon, thesisMetaRows } from '@/api/techmeta/adapters'
import type { TmView } from '@/api/techmeta/types'
import { useTechExperimental } from '@/api/techprob/hooks'
import { Badge } from '@/components/ui/badge'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

const TXT: Record<string, string> = { up: 'text-up', down: 'text-down', neutral: 'text-muted', warn: 'text-warn', info: 'text-info' }

const META_TIP = 'Context-adjusted (meta-labelling): the chance of T1 before the stop re-estimated from the T2 probability plus the context at the setup close (market regime, risk gauge, VIX, peer break-aways, insider / short interest / smart money, earnings proximity, squeeze, ADX, trend alignment). Model-based, calibrated on the 2013–2026 purged walk-forward; not a statement about this trade.'

export function MetaSetupRow({ view, horizon }: { view: TmView | null | undefined; horizon: number }) {
  const show = useTechExperimental((s) => s.show)
  const h = pickMetaHorizon(view, horizon, show)
  const r = metaRow(h, show)
  if (!view || !h || !r) return null
  if (r.hidden) {
    return (
      <p className="flex items-center gap-1 text-2xs text-muted" data-testid="tm-row-hidden">
        <FlaskConical className="size-3 shrink-0" aria-hidden />
        Context-adjusted (meta) {h.horizon}d is experimental ({h.in_domain ? 'failed the validation gate' : 'outside the validated universe'}) — turn on “show experimental” to see it.
      </p>
    )
  }
  return (
    <div className="space-y-1.5 rounded-sm border border-border px-2 py-1.5" data-testid="tm-row">
      <div className="flex items-center justify-between gap-2">
        <Tip content={META_TIP}>
          <span className="label-caps cursor-help underline decoration-dotted underline-offset-2">Context-adjusted (meta) · {r.horizon}d</span>
        </Tip>
        {r.experimental && <span className="flex items-center gap-1 text-2xs text-warn"><FlaskConical className="size-3" aria-hidden />experimental</span>}
      </div>
      <dl className="grid grid-cols-[auto_auto_1fr] gap-x-4 gap-y-1">
        <div>
          <dt className="label-caps">T1 first</dt>
          <dd className="num text-md font-semibold text-fg">{r.pWin}</dd>
        </div>
        <div>
          <dt className="label-caps">Exp. R</dt>
          <dd className={cn('num text-md font-semibold', r.erTone === 'up' ? 'text-up' : r.erTone === 'down' ? 'text-down' : 'text-fg')} title="Context-adjusted P(T1) x R:R - P(stop) - costs + the marked-to-market time exit">{r.er}</dd>
        </div>
        <div>
          <dt className="label-caps">Decision</dt>
          <dd data-testid="tm-decision">
            {r.decisionHidden ? (
              <span className="inline-flex items-center gap-1 text-2xs text-muted" title="Take / skip and size failed (or have not passed) the economic check in the Backtest Lab engine: experimental">
                <FlaskConical className="size-3" aria-hidden /> hidden
              </span>
            ) : r.decision ? (
              <Badge tone={r.decision === 'take' ? 'up' : 'neutral'}>{r.decision}{r.decisionExperimental ? ' ⚗' : ''}</Badge>
            ) : '—'}
          </dd>
        </div>
      </dl>
      <p className={cn('num text-2xs', TXT[r.deltaTone])} title={`T2 two-barrier probability ${r.t2} (same horizon)`}>{r.delta} vs T2 ({r.t2})</p>
      {r.size && !r.decisionHidden && <p className="text-2xs text-muted" title="Quarter Kelly on the context-adjusted outcome probabilities, capped at 2% of equity at risk per 1R">Suggested size: <span className="num text-fg">{r.size}</span></p>}
      {r.drivers.length > 0 && (
        <ul className="flex flex-wrap gap-1" aria-label="Top drivers of the adjustment vs T2">
          {r.drivers.map((d) => (
            <li key={d.key}>
              <Tip content={d.title}>
                <span className={cn('inline-flex h-5 items-center rounded-sm border border-border px-1.5 text-2xs', TXT[d.tone])}>{d.text}</span>
              </Tip>
            </li>
          ))}
        </ul>
      )}
      <p className="text-2xs text-faint">{metaGateLine(view, r.horizon)} · model-based, calibrated on history</p>
    </div>
  )
}

/** Thesis panel rows: meta P(target first) + expected R + decision per horizon (custom barriers, query time). */
export function ThesisMetaRows({ view }: { view: TmView | null | undefined }) {
  const show = useTechExperimental((s) => s.show)
  const rows = thesisMetaRows(view, show)
  const hidden = (view?.horizons.length ?? 0) - rows.length
  if (!view) return null
  return (
    <div className="mt-2 border-t border-border pt-2" data-testid="thesis-meta">
      <Tip content={META_TIP}>
        <span className="label-caps cursor-help underline decoration-dotted underline-offset-2">Context-adjusted (meta)</span>
      </Tip>
      {rows.length ? (
        <table className="mt-1 w-full text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th className="h-6 text-left font-semibold">Within</th>
              <th className="text-right font-semibold">Target first</th>
              <th className="text-right font-semibold">vs T2</th>
              <th className="text-right font-semibold">Expected R</th>
              <th className="text-right font-semibold">Decision</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.horizon} className="border-t border-border">
                <td className="h-7">{r.horizon} sessions{r.experimental ? ' ⚗' : ''}</td>
                <td className="num text-right text-up">{r.pWin}</td>
                <td className={cn('num text-right text-2xs', TXT[r.deltaTone])}>{r.delta}</td>
                <td className="num text-right font-semibold">{r.er}</td>
                <td className="text-right">{r.decisionHidden ? <span className="text-2xs text-muted">—</span> : r.decision ? <Badge tone={r.decision === 'take' ? 'up' : 'neutral'}>{r.decision}</Badge> : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="mt-1 text-2xs text-muted"><FlaskConical className="mr-1 inline size-3" aria-hidden />Experimental (failed the validation gate) — turn on “show experimental” to see it.</p>
      )}
      {rows.length > 0 && hidden > 0 && <p className="mt-1 text-2xs text-faint">{hidden} experimental horizon{hidden > 1 ? 's' : ''} hidden.</p>}
    </div>
  )
}
