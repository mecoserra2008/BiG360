/**
 * Technicals v2 slots (docs/TECHNICALS_V2_PLAN.md). The T1 layout of the Technicals tab reserves these places for the
 * model outputs of T2-T4, so they can be filled here without touching the tab's layout:
 *
 *   SLOT T2  <SetupProbabilities>  inside the Trade setup card, under the entry / invalidation / targets grid:
 *                                  P(win) / P(loss) / P(neither by H), expected R after costs, break-even probability.
 *   SLOT T3  <ZoneHoldHeader> +    an extra column of the Confluence zones table (after "Touch"): P(hold | touch) with
 *            <ZoneHoldCell>        its uncertainty and the edge vs random control levels ("no reliable edge" when n.s.).
 *   SLOT T4  <TechScoreCard>       its own card in the right column, under Trade setup: the calibrated technical score
 *                                  (percentile sub-scores, score -> historical outcomes).
 *
 * Data: GET /technicals/{id}/setup-probabilities, /zones, /score-calibration (contract v1.34.0, model-based, calibrated
 * on history). Outputs that failed the T6 gate (docs/TECHNICALS_V2_VALIDATION.md) are hidden unless the viewer turns on
 * "experimental" (persisted per browser). When the models have no output yet, T2 / T3 render nothing / "—".
 */
import { FlaskConical, Info } from 'lucide-react'
import { useState } from 'react'
import { bucketLines, expectancy, fmtExcess, fmtProb, fmtR, hasExperimental, holdCell, pickHorizon, probSegments, subScoreRows, visibleHorizons, zoneHoldIndex } from '@/api/techprob/adapters'
import { useScoreCalibration, useSetupProbabilities, useTechExperimental, useZoneHold } from '@/api/techprob/hooks'
import type { TpGate, TpScore, TpSetup } from '@/api/techprob/types'
import type { ConfluenceZone, TradeSetup } from '@/api/types'
import { MetaSetupRow } from '@/components/techmeta/MetaSetupRow'
import { compositeHeadline } from '@/api/techv3/adapters'
import { useTechFactors } from '@/api/techv3/hooks'
import { Badge } from '@/components/ui/badge'
import { Popover, ScorePill } from '@/components/ui/data'
import { Segmented, Switch } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'

const SEG_CLS: Record<string, string> = { up: 'bg-up', down: 'bg-down', neutral: 'bg-surface-3' }
const TONE_TXT: Record<string, string> = { up: 'text-up', down: 'text-down', neutral: 'text-fg', warn: 'text-warn', info: 'text-info' }

/** Experimental toggle (shared by the three slots; per viewer). */
function ExperimentalToggle() {
  const { show, setShow } = useTechExperimental()
  return (
    <label className="flex items-center gap-1.5 text-2xs text-muted">
      <Switch label="Show experimental outputs" checked={show} onCheckedChange={(v) => setShow(!!v)} />
      <FlaskConical className="size-3" aria-hidden /> show experimental (failed validation)
    </label>
  )
}

function GateLines({ gates }: { gates: TpGate[] }) {
  if (!gates.length) return null
  return (
    <ul className="space-y-0.5" aria-label="Validation gate">
      {gates.map((g) => (
        <li key={g.output} className="flex items-start gap-1.5">
          <Badge tone={g.verdict === 'pass' ? 'up' : g.verdict === 'fail' ? 'warn' : 'neutral'}>{g.verdict === 'pass' ? 'pass' : g.verdict === 'fail' ? 'experimental' : 'n/v'}</Badge>
          <span className="text-muted"><span className="font-semibold text-fg">{g.output.replace('t2_h', 'H=').replace('t4_h', 'H=').replace('t3_', '')}</span>{g.summary ? ` · ${g.summary}` : ''}</span>
        </li>
      ))}
    </ul>
  )
}

function CalibratedPopover({ title, children, gates }: { title: string; children: React.ReactNode; gates: TpGate[] }) {
  return (
    <Popover
      trigger={
        <button type="button" className="inline-flex h-5 items-center gap-1 rounded-sm border border-border px-1.5 text-2xs text-muted hover:bg-surface-3 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus" aria-label={`${title}: how it is calibrated`}>
          <Info className="size-3" aria-hidden /> calibrated on history
        </button>
      }
      className="w-[380px]"
    >
      <div className="space-y-2 text-xs" data-testid="tp-calibration-popover">
        <div className="font-semibold text-fg">{title}</div>
        {children}
        <GateLines gates={gates} />
        <ExperimentalToggle />
        <p className="text-2xs text-faint">Validation: docs/TECHNICALS_V2_VALIDATION.md (walk-forward 2010–2026, mvp US + major EU / UK / JP names).</p>
      </div>
    </Popover>
  )
}

/* ── SLOT T2 ── */

export function SetupProbabilities({ securityId }: { securityId: string; setup: TradeSetup }) {
  const q = useSetupProbabilities(securityId)
  const show = useTechExperimental((s) => s.show)
  const [hSel, setHSel] = useState<string | null>(null)
  const d: TpSetup | undefined = q.data
  if (!d || d.data_status === 'pending' || !d.horizons.length) return null
  const rows = visibleHorizons(d, show)
  const h = pickHorizon(d, show, hSel ? Number(hSel) : null)
  const hidden = d.horizons.length - rows.length
  if (!h) {
    return (
      <p className="mt-2 flex items-center gap-1 text-2xs text-muted" data-testid="tp-setup-hidden">
        <FlaskConical className="size-3" aria-hidden /> Setup probabilities are experimental (failed validation) — <span className="underline">enable in the popover</span>
        <CalibratedPopover title="Setup probabilities" gates={d.validation}>
          <p className="text-muted">{d.labels.setup}</p>
        </CalibratedPopover>
      </p>
    )
  }
  const seg = probSegments(h)
  const ex = expectancy(h)
  return (
    <div className="mt-3 space-y-2 rounded-md border border-border bg-surface-2 px-2 py-2" data-testid="tp-setup">
      <div className="flex items-center justify-between gap-2">
        <span className="label-caps">Chance by horizon</span>
        <Segmented
          label="Setup probability horizon"
          size="xs"
          value={String(h.horizon)}
          onChange={(v) => setHSel(v)}
          options={rows.map((r) => ({ value: String(r.horizon), label: `${r.horizon}d${r.is_setup_horizon ? '*' : ''}${r.experimental ? '⚗' : ''}`, title: r.is_setup_horizon ? "The setup's own horizon (target within one sigma)" : undefined }))}
        />
      </div>
      <div className="flex h-5 w-full overflow-hidden rounded-sm" role="img" aria-label={seg.map((s) => `${s.label} ${fmtProb(s.value)}`).join(', ')}>
        {seg.map((s) => (
          <Tip key={s.key} content={`${s.label}: ${fmtProb(s.value, 1)} (within ${h.horizon} sessions)`}>
            <span className={cn('flex h-full items-center justify-center text-2xs font-semibold', SEG_CLS[s.tone], s.tone === 'neutral' ? 'text-muted' : 'text-white')} style={{ width: `${Math.max(s.value * 100, 0)}%` }}>
              {s.value >= 0.12 ? fmtProb(s.value) : ''}
            </span>
          </Tip>
        ))}
      </div>
      <div className="flex justify-between text-2xs text-muted">
        <span><span className="mr-1 inline-block size-2 rounded-xs bg-up align-middle" />T1 first {fmtProb(h.p_win)}</span>
        <span><span className="mr-1 inline-block size-2 rounded-xs bg-down align-middle" />stop first {fmtProb(h.p_loss)}</span>
        <span><span className="mr-1 inline-block size-2 rounded-xs bg-surface-3 align-middle" />neither {fmtProb(h.p_neither)}</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1">
        <div>
          <dt className="label-caps">Expected R</dt>
          <dd className={cn('num text-md font-semibold', h.expected_r != null && h.expected_r > 0 ? 'text-up' : h.expected_r != null && h.expected_r < 0 ? 'text-down' : 'text-fg')} title={`After costs (${fmtR(h.cost_r)} round trip). Marked to market incl. the time exit: ${fmtR(h.expected_r_mtm)}`}>
            {fmtR(h.expected_r)}
          </dd>
        </div>
        <div>
          <dt className="label-caps">Break-even P(win)</dt>
          <dd className="num text-md font-medium text-fg" title={`risk / (reward + risk) at R:R ${fmtNum(d.setup?.reward_risk_close, 2)} from the close; with costs ${fmtProb(h.break_even_p_costs)}`}>{fmtProb(h.break_even_p)}</dd>
        </div>
      </dl>
      {ex && <p className={cn('text-2xs', TONE_TXT[ex.tone])}>{ex.text}</p>}
      {/* Technicals v3 #2: context-adjusted (meta-labelling) row - components/techmeta/MetaSetupRow.tsx */}
      <MetaSetupRow view={d.meta} horizon={h.horizon} />
      <div className="flex flex-wrap items-center justify-between gap-1.5 text-2xs text-faint">
        <span title="Baselines: the naive 'R:R only' rule (no horizon) and the driftless Brownian two-barrier formula">vs R:R-only {fmtProb(h.p_win_naive)} · Brownian {fmtProb(h.p_win_bm)}</span>
        <CalibratedPopover title="Setup probabilities (two-barrier, calibrated on history)" gates={d.validation}>
          <p className="text-muted">{d.labels.setup}</p>
          <p className="text-muted">{d.labels.expected_r}</p>
          <p className="text-muted">{d.labels.break_even}</p>
          <p className="rounded-sm bg-surface-2 px-2 py-1 text-fg">
            Pool {d.pool?.region ?? '—'} · vol {d.pool?.vol_regime ?? '—'}{h.earn_window ? ' · earnings window' : ''} · n = {h.n_pool?.toLocaleString() ?? '—'} historical origins
            {h.region_fallback ? ' (all-region fallback)' : ''}{h.clamped ? ' · distances beyond the calibrated grid (clamped)' : ''} · R:R from the close {fmtNum(d.setup?.reward_risk_close, 2)}× (card: from the entry)
          </p>
        </CalibratedPopover>
      </div>
      {h.experimental && <p className="flex items-center gap-1 text-2xs text-warn"><FlaskConical className="size-3" aria-hidden />Experimental horizon: failed the validation gate.</p>}
      {hidden > 0 && !show && <p className="text-2xs text-faint">{hidden} experimental horizon{hidden > 1 ? 's' : ''} hidden.</p>}
    </div>
  )
}

/* ── SLOT T3 ── */

export function ZoneHoldHeader(_props: { horizon: number }) {
  return (
    <th scope="col" className="h-7 border-b border-border px-2 text-right font-semibold" data-testid="zone-hold-header">
      <Tip content="P(hold | touch): historical share of touches of similar zones that reversed by >= 1.5 sigma before closing beyond the far edge (10 sessions); calibrated on history. The badge compares with random price bands of the same width / approach / trend: 'no reliable edge' when the walk-forward difference is not significant.">
        <span className="cursor-help underline decoration-dotted underline-offset-2">Hold | touch</span>
      </Tip>
    </th>
  )
}

export function ZoneHoldCell({ securityId, zone, horizon }: { securityId: string; zone: ConfluenceZone; horizon: number }) {
  const q = useZoneHold(securityId, horizon)
  const show = useTechExperimental((s) => s.show)
  const z = zoneHoldIndex(q.data).get(zone.id)
  const c = holdCell(z, show)
  return (
    <td className="border-b border-border px-2 text-right" data-testid="zone-hold-cell" title={c.title}>
      {c.text === '—' ? (
        <span className="inline-flex items-center justify-end gap-1.5 whitespace-nowrap text-faint">
          {c.hidden ? <FlaskConical className="inline size-3" aria-label="P(hold | touch) experimental (hidden)" /> : '—'}
          {c.badge && <Badge tone={c.badge.tone === 'up' ? 'up' : c.badge.tone === 'down' ? 'down' : 'neutral'} className="normal-case">{c.badge.label}</Badge>}
        </span>
      ) : (
        <span className="inline-flex items-center justify-end gap-1.5 whitespace-nowrap">
          <span className="num font-medium">{c.text}</span>
          {c.badge && <Badge tone={c.badge.tone === 'up' ? 'up' : c.badge.tone === 'down' ? 'down' : 'neutral'} className="normal-case">{c.badge.label}</Badge>}
          {z?.experimental && <FlaskConical className="size-3 text-warn" aria-label="experimental" />}
        </span>
      )}
    </td>
  )
}

/* ── SLOT T4 ── */

export function TechScoreCard({ securityId, setup }: { securityId: string; setup: TradeSetup }) {
  const q = useScoreCalibration(securityId)
  const show = useTechExperimental((s) => s.show)
  const fq = useTechFactors(securityId)
  const d: TpScore | undefined = q.data
  // Technicals v3 #1: the evidence-based factor composite replaces the headline ONLY when it passed the walk-forward gate
  const head = compositeHeadline(fq.data)
  if (!Number.isFinite(setup.technicalScore ?? NaN) && head.mode !== 'composite') return null
  const subs = subScoreRows(d)
  const lines = bucketLines(d, show)
  const calibrated = !!d && d.data_status !== 'pending' && subs.some((s) => s.percentile != null)
  const hiddenExp = !!d && !show && hasExperimental(d) && d.buckets.some((b) => b.experimental)
  return (
    <Panel
      title={head.mode === 'composite' ? 'Technical score (evidence-based)' : 'Technical score'}
      subtitle={head.mode === 'composite' ? 'Factor composite · gate passed (walk-forward)' : calibrated ? `Percentiles within region × sector (n = ${d?.n_peers ?? '—'})` : 'Heuristic composite (trend, structure, location, reward:risk, momentum)'}
      data-slot="t4-score"
      actions={d && calibrated ? (
        <CalibratedPopover title="Calibrated technical score" gates={d.validation}>
          <p className="text-muted">{d.labels.score}</p>
          {d.labels.score_no_edge && <p className="text-muted">{d.labels.score_no_edge}</p>}
          {d.tables.map((t) => (
            <p key={t.horizon} className="text-2xs text-muted">{t.horizon}d: walk-forward rank IC {fmtNum(t.ic_mean, 3)} (t {fmtNum(t.ic_t, 2)}) · {t.edge_significant ? 'out-of-sample edge' : 'no out-of-sample edge'}</p>
          ))}
        </CalibratedPopover>
      ) : undefined}
    >
      {head.mode === 'composite' && (
        <div className="mb-2 flex items-center gap-3 rounded-md border border-border bg-surface-2 px-2 py-1.5" data-testid="tv3-composite-headline">
          <ScorePill value={head.value} size="lg" />
          <div className="min-w-0 text-2xs" title={head.note}>
            <div className="font-semibold text-fg">Factor composite{head.percentile != null ? ` · p${Math.round(head.percentile)} in region × sector` : ''}</div>
            <div className="truncate text-muted">{head.members.join(' · ')}</div>
            {head.ic && <div className="text-faint">{head.ic}</div>}
          </div>
        </div>
      )}
      {head.mode === 'composite' && <p className="mb-1 text-2xs text-faint">Heuristic score (v2) — no out-of-sample edge:</p>}
      <div className="flex items-center gap-3">
        <div className="flex flex-col items-center gap-0.5">
          <ScorePill value={setup.technicalScore} size={head.mode === 'composite' ? 'sm' : 'lg'} />
          {calibrated && d?.pct_technical != null && <span className="num text-2xs text-muted" title="Percentile of the composite within region × sector">p{Math.round(d.pct_technical)}</span>}
        </div>
        <ul className="min-w-0 flex-1 space-y-0.5" aria-label="Technical score components">
          {calibrated
            ? subs.map((c) => (
              <li key={c.key} className="grid grid-cols-[72px_1fr_28px_34px] items-center gap-1.5 text-2xs" title={`raw ${c.raw == null ? '—' : Math.round(c.raw)} (saturating 0–100) · percentile within region × sector ${c.percentile == null ? '—' : Math.round(c.percentile)}`}>
                <span className="truncate text-muted">{c.label}</span>
                <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                  <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, c.percentile ?? 0))}%` }} />
                </span>
                <span className="num text-right">p{c.percentile == null ? '—' : Math.round(c.percentile)}</span>
                <span className="num text-right text-faint">{c.raw == null ? '—' : Math.round(c.raw)}</span>
              </li>
            ))
            : (setup.scoreComponents ?? []).map((c) => (
              <li key={c.key} className="grid grid-cols-[72px_1fr_28px] items-center gap-1.5 text-2xs" title={Number.isFinite(c.weight) ? `weight ${fmtPct(c.weight, 0)}` : undefined}>
                <span className="truncate text-muted">{c.key}</span>
                <span className="h-1.5 overflow-hidden rounded-full bg-surface-3">
                  <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(0, Math.min(100, c.score))}%` }} />
                </span>
                <span className="num text-right">{Math.round(c.score)}</span>
              </li>
            ))}
        </ul>
      </div>
      {calibrated && <p className="mt-1 text-right text-2xs text-faint">bar = percentile · grey = raw sub-score</p>}
      {lines.length > 0 && (
        <ul className="mt-2 space-y-1 border-t border-border pt-2" aria-label="Historical outcomes of this score bucket" data-testid="tp-score-buckets">
          {lines.map((b) => (
            <li key={b.horizon} className="text-2xs">
              <div className="flex items-center justify-between gap-1.5">
                <span className="font-semibold text-fg">{b.horizon}d · decile {b.decile ?? '—'}</span>
                <span className="flex items-center gap-1">
                  {b.experimental && <FlaskConical className="size-3 text-warn" aria-label="experimental" />}
                  <Badge tone={b.badge.tone === 'up' ? 'up' : 'neutral'} className="normal-case">{b.badge.label}</Badge>
                </span>
              </div>
              <div className="text-muted" title={b.line ?? undefined}>
                median excess <span className="num text-fg">{fmtExcess(b.median)}</span> · beat region <span className="num text-fg">{fmtProb(b.hit_rate)}</span>
                {b.hit_ci && <span className="num"> [{fmtProb(b.hit_ci[0])}–{fmtProb(b.hit_ci[1])}]</span>} · n {b.n?.toLocaleString() ?? '—'}
              </div>
            </li>
          ))}
        </ul>
      )}
      {calibrated && lines.length > 0 && <p className="mt-1 text-2xs text-faint">Historically this score bucket → outcomes above (2003–2026, gate universe); not a forecast.</p>}
      {hiddenExp && (
        <p className="mt-2 flex items-start gap-1 border-t border-border pt-2 text-2xs text-muted" data-testid="tp-score-no-edge">
          <FlaskConical className="mt-px size-3 shrink-0" aria-hidden />
          <span>No out-of-sample edge: in the 2010–2026 walk-forward this score's rank IC and decile spread were not significant, so its historical bucket stats are experimental (enable in “calibrated on history”).</span>
        </p>
      )}
    </Panel>
  )
}
