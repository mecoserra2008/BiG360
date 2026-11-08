/**
 * Technicals v3 #1 / #5 cards (docs/TECHNICALS_V3_IDEAS.md; gate docs/TECHNICALS_V3_VALIDATION.md):
 *   <TechFactorsCard>   "Technical factors (evidence-based)": per factor its percentile within region x sector, a small
 *                       IC / edge badge and a tooltip with the reference and the out-of-sample statistics
 *   <BreakoutPanel>     the latest breakout's state chip with the calibrated P(follow-through) and the validation evidence
 * Data: GET /technicals/{id}/factors and /breakout (contract v1.36.0). Items that failed the gate are hidden unless the
 * shared "experimental" toggle (Technicals v2 slots) is on.
 */
import { FlaskConical, Info } from 'lucide-react'
import { breakoutChip, evidenceLine, factorRows, fmtPct, fmtProb, hiddenCount } from '@/api/techv3/adapters'
import { useTechBreakout, useTechFactors } from '@/api/techv3/hooks'
import { useTechExperimental } from '@/api/techprob/hooks'
import { Badge } from '@/components/ui/badge'
import { Popover } from '@/components/ui/data'
import { Switch } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { Missing, useMissingReason } from '@/components/ui/missing'
import { reasonText } from '@/lib/availability'
import { fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'

function ExperimentalSwitch() {
  const { show, setShow } = useTechExperimental()
  return (
    <label className="flex items-center gap-1.5 text-2xs text-muted">
      <Switch label="Show experimental outputs" checked={show} onCheckedChange={(v) => setShow(!!v)} />
      <FlaskConical className="size-3" aria-hidden /> show experimental (failed validation)
    </label>
  )
}

export function TechFactorsCard({ securityId }: { securityId: string }) {
  const q = useTechFactors(securityId)
  const show = useTechExperimental((s) => s.show)
  const why = useMissingReason('tech.hi52_pct')
  const d = q.data
  if (!d && !q.isError) return null
  if (!d || d.data_status === 'pending') {
    // completeness C: never a silent blank - say why there are no factors for this security
    return (
      <Panel title="Technical factors (evidence-based)" data-slot="tv3-factors">
        <p className="text-2xs text-muted" data-testid="missing-reason" data-reason={why.code}>{reasonText(why)}</p>
      </Panel>
    )
  }
  const rows = factorRows(d, show)
  const hidden = hiddenCount(d, show)
  return (
    <Panel
      title="Technical factors (evidence-based)"
      subtitle={`Percentile vs ${d.cross_section === 'region x sector' ? 'sector' : 'region'} peers${d.n_peers ? ` (n ${d.n_peers})` : ''}`}
      data-slot="tv3-factors"
      actions={
        <Popover
          trigger={
            <button type="button" className="inline-flex size-5 items-center justify-center rounded-sm border border-border text-muted hover:bg-surface-3 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus" aria-label="Technical factors: how they are validated" title="How these factors are validated">
              <Info className="size-3" aria-hidden />
            </button>
          }
          className="w-[380px]"
        >
          <div className="space-y-2 text-xs" data-testid="tv3-factors-popover">
            <div className="font-semibold text-fg">Evidence-based technical factors</div>
            <p className="text-muted">{d.labels.factors}</p>
            <p className="text-muted">{d.labels.percentile}</p>
            <p className="text-2xs text-muted">{d.validation.n_cells_tested ? `${d.validation.n_cells_tested} factor × horizon × region cells tested; ` : ''}{d.validation.multiple_testing}.</p>
            {!d.region_validated && <p className="text-2xs text-warn">{d.labels.not_validated_region}</p>}
            {d.liquid === false && <p className="text-2xs text-warn">Below the liquidity floor of the validated universe (20-day ADV $1M): percentiles shown against liquid peers.</p>}
            <ExperimentalSwitch />
            <p className="text-2xs text-faint">Validation: {d.validation_doc}</p>
          </div>
        </Popover>
      }
    >
      {rows.length === 0 ? (
        <p className="flex items-start gap-1 text-2xs text-muted" data-testid="tv3-factors-none">
          <FlaskConical className="mt-px size-3 shrink-0" aria-hidden />
          No factor passed the walk-forward gate — all {d.factors.length} are experimental (enable via the ⓘ button).
        </p>
      ) : (
        <ul className="space-y-1.5" aria-label="Evidence-based technical factors" data-testid="tv3-factors">
          {rows.map((r) => (
            <li key={r.id} className="text-2xs">
              <div className="flex items-center justify-between gap-1.5">
                <Tip content={r.tooltip}>
                  <span className="min-w-0 cursor-help truncate text-muted underline decoration-dotted underline-offset-2">{r.short}</span>
                </Tip>
                <span className="flex shrink-0 items-center gap-1">
                  {r.experimental && <FlaskConical className="size-3 text-warn" aria-label="experimental" />}
                  <Badge tone={r.badge.tone === 'up' ? 'up' : r.badge.tone === 'info' ? 'info' : 'neutral'} className="normal-case">{r.badge.label}</Badge>
                </span>
              </div>
              <div className="mt-0.5 grid grid-cols-[1fr_34px] items-center gap-1.5">
                <span className="h-1.5 overflow-hidden rounded-full bg-surface-3" title={`favourable-direction percentile ${fmtPct(r.aligned)} · raw percentile ${fmtPct(r.percentile)}`}>
                  <span className={cn('block h-full rounded-full', r.favourable ? 'bg-up' : 'bg-accent')} style={{ width: `${Math.max(0, Math.min(100, r.aligned ?? 0))}%` }} />
                </span>
                <span className="num text-right text-fg">{r.aligned == null ? <Missing field={`tech.${r.id}_pct`} /> : fmtPct(r.aligned)}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
      {rows.some((r) => r.tradable === false && !r.experimental) && (
        <p className="mt-1.5 text-2xs text-muted" data-testid="tv3-factors-econ">Rank IC = the factor sorts stocks' next-month returns (median) out-of-sample, but a stand-alone quintile portfolio did not make money after costs — use as context, not as a strategy.</p>
      )}
      {hidden > 0 && rows.length > 0 && <p className="mt-1.5 text-2xs text-faint">{hidden} factor{hidden > 1 ? 's' : ''} without a validated edge hidden.</p>}
      <p className="mt-1.5 text-2xs text-faint">Bar = percentile on the historically favourable side. Historical evidence (2005–2026 walk-forward), not a forecast.</p>
    </Panel>
  )
}

const TONE_CLS: Record<string, string> = { up: 'text-up', down: 'text-down', warn: 'text-warn', neutral: 'text-muted', info: 'text-info' }

export function BreakoutPanel({ securityId }: { securityId: string }) {
  const q = useTechBreakout(securityId)
  const show = useTechExperimental((s) => s.show)
  const b = q.data
  if (!b || b.data_status === 'pending') return null
  const chip = breakoutChip(b, show)
  const ev = evidenceLine(b)
  return (
    <Panel title="Breakout" subtitle="20 / 55-session high / low · last 20 sessions" data-slot="tv3-breakout">
      {!chip.visible ? (
        <p className="text-2xs text-muted" data-testid="tv3-breakout-none">No breakout in the last 20 sessions.</p>
      ) : (
        <div className="space-y-1.5" data-testid="tv3-breakout">
          <div className="flex flex-wrap items-center gap-1.5">
            <Tip content={chip.title}>
              <span><Badge tone={chip.tone === 'up' ? 'up' : chip.tone === 'down' ? 'down' : chip.tone === 'warn' ? 'warn' : 'neutral'} className="normal-case">{chip.label}</Badge></span>
            </Tip>
            {chip.confirmation && <Badge tone={b.confirmed ? 'up' : 'neutral'} className="normal-case">{chip.confirmation}</Badge>}
          </div>
          <p className="text-2xs text-muted">{chip.sub}{b.event_date ? ` · ${fmtDate(b.event_date)}` : ''}</p>
          {chip.prob && (
            <div className="flex items-baseline justify-between rounded-md border border-border bg-surface-2 px-2 py-1">
              <span className="label-caps">P(follow-through, 20d)</span>
              <span className="num text-md font-semibold text-fg" title={`Calibrated on history (model-based). Geometry-only baseline ${fmtProb(b.p_geo)}, base rate ${fmtProb(b.base_rate)}.`}>
                {chip.prob}
                {b.experimental && <FlaskConical className="ml-1 inline size-3 text-warn" aria-label="experimental" />}
              </span>
            </div>
          )}
          {chip.probHidden && <p className="flex items-center gap-1 text-2xs text-muted"><FlaskConical className="size-3" aria-hidden />P(follow-through) is experimental (failed validation) — hidden.</p>}
          {chip.prob && <p className="text-2xs text-faint">vs geometry baseline {fmtProb(b.p_geo)} · base rate {fmtProb(b.base_rate)}</p>}
        </div>
      )}
      {ev && <p className={cn('mt-1.5 border-t border-border pt-1.5 text-2xs', TONE_CLS[ev.tone])} data-testid="tv3-breakout-evidence" title={ev.title}>{ev.text}</p>}
    </Panel>
  )
}
