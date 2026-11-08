import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router'
import { useCentralBanks, useImpliedPaths } from '@/api/macro/hooks'
import { type CbImplied, type CbOverview, type CbSummary, type CbToneTrend, IMPLIED_LABEL, pricedBucket, pricedText, toneSpark } from '@/api/macro/v34'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Flag } from '@/components/ui/data'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtChange, fmtMacro, zStyle } from './fmt'
import { MacroNav } from './shared'

export default function CentralBanks() {
  const q = useCentralBanks()
  return (
    <>
      <PageHeader title="Central banks" subtitle="17 central banks · official policy rates, decisions and meetings · market-implied paths approximated from official curves" />
      <MacroNav />
      <QueryView query={q} skeleton={<Skeleton className="h-[600px]" />}>
        {(d) => (
          <div className="@container space-y-3">
            <ImpliedNote labels={d.labels} />
            <OverviewTable d={d} />
            <ImpliedComparison />
          </div>
        )}
      </QueryView>
    </>
  )
}

/** User rule: implied paths are approximations from official curves, always said so. */
export function ImpliedNote({ labels, className }: { labels?: Record<string, unknown>; className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-2 rounded-md border border-info/40 bg-info-soft px-3 py-1.5 text-xs text-fg', className)}>
      <Badge tone="info">approximation</Badge>
      <span>{IMPLIED_LABEL}.</span>
      {labels?.taylor_rule ? (
        <span className="text-muted">
          <Badge tone="info">model-based</Badge> {String(labels.taylor_rule)} · {String(labels.tone ?? '')}
        </span>
      ) : null}
    </div>
  )
}

export function PhaseBadge({ phase }: { phase: string | null | undefined }) {
  if (!phase) return <span className="text-faint">{DASH}</span>
  return <Badge tone={phase === 'hiking' ? 'down' : phase === 'cutting' ? 'up' : 'neutral'}>{phase === 'hiking' ? '▲ hiking' : phase === 'cutting' ? '▼ cutting' : '■ pausing'}</Badge>
}

/** bp change chip: rose = tighter (hikes priced / done), teal = easier. Arrow keeps it readable without colour. */
export function BpChip({ bp, moves, step = 25, title }: { bp: number | null | undefined; moves?: number | null; step?: number; title?: string }) {
  if (typeof bp !== 'number') return <span className="text-faint">{DASH}</span>
  const b = -pricedBucket(moves ?? bp / step)
  return (
    <span className="num inline-flex h-5 items-center gap-0.5 rounded-sm px-1.5 text-xs font-medium whitespace-nowrap text-fg" style={zStyle(b)} title={title}>
      {bp > 0.5 ? '▲' : bp < -0.5 ? '▼' : '■'} {fmtChange(bp, 0)}bp
    </span>
  )
}

function OverviewTable({ d }: { d: CbOverview }) {
  const navigate = useNavigate()
  const th = 'px-2 py-1.5 text-left text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'
  return (
    <Panel title="Policy rates and pricing" subtitle={`as of ${fmtDate(d.as_of)} · click a bank for its page`} flush>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label="Central banks overview">
          <thead>
            <tr className="border-b border-border">
              <th className={th}>Bank</th>
              <th className={cn(th, 'text-right')}>Rate</th>
              <th className={th}>Last move</th>
              <th className={th}>Phase</th>
              <th className={th}>Next meeting</th>
              <th className={th}>
                Priced next mtg <Badge tone="info">approx.</Badge>
              </th>
              <th className={th}>Priced 12m</th>
              <th className={cn(th, 'text-right')}>12m Δ1w / Δ1m</th>
              <th className={cn(th, 'text-right')}>Real rate</th>
              <th className={cn(th, 'text-right')}>
                vs Taylor <Badge tone="info">model</Badge>
              </th>
              <th className={th}>
                Tone trend <Badge tone="info">model</Badge>
              </th>
            </tr>
          </thead>
          <tbody>
            {d.banks.map((b) => (
              <BankRow key={b.bank} b={b} onOpen={() => navigate(`/macro/cb/${b.bank}`)} />
            ))}
          </tbody>
        </table>
      </div>
      <p className="px-3 py-2 text-2xs text-muted">
        Priced = change implied by official government curves, in bp and in moves of 25bp: {IMPLIED_LABEL}. Rose = tightening, teal = easing. Real rate = policy rate − core (or headline) inflation. vs Taylor = policy rate − Taylor-rule rate (model-based, official inputs). “—” = no official curve for this bank.
      </p>
    </Panel>
  )
}

function BankRow({ b, onOpen }: { b: CbSummary; onOpen: () => void }) {
  const c = b.corridor
  const p = b.priced
  const ok = p?.status === 'ok' || p?.status === 'stale'
  const st = b.stance
  const real = st?.real_rate_core ?? st?.real_rate_cpi ?? null
  const tone = b.tone?.tone_model
  return (
    <tr className="cursor-pointer border-b border-border/60 hover:bg-surface-2" onClick={onOpen}>
      <td className="px-2 py-1">
        <Link to={`/macro/cb/${b.bank}`} className="inline-flex items-center gap-1.5 font-medium text-fg hover:text-accent-text hover:underline" onClick={(e) => e.stopPropagation()}>
          <Flag country={b.country} /> {b.name}
        </Link>
        <div className="num text-2xs text-muted">{b.rate_name}</div>
      </td>
      <td className="num px-2 py-1 text-right">
        <span className="font-semibold text-fg">{fmtMacro(b.policy_rate, 4)}%</span>
        {c && (typeof c.lower === 'number' || typeof c.deposit === 'number') && (
          <div className="text-2xs text-muted">{typeof c.lower === 'number' && typeof c.upper === 'number' ? `${fmtMacro(c.lower, 3)}–${fmtMacro(c.upper, 3)}` : `dep. ${fmtMacro(c.deposit, 3)}`}</div>
        )}
      </td>
      <td className="px-2 py-1">
        {b.last_move ? (
          <span className="inline-flex flex-col">
            <BpChip bp={b.last_move.change_bp} step={25} />
            <span className="num text-2xs text-muted">{fmtDate(b.last_move.decision_date)}</span>
          </span>
        ) : (
          <span className="text-faint">{DASH}</span>
        )}
      </td>
      <td className="px-2 py-1">
        <PhaseBadge phase={b.phase} />
        {b.phase_since && <div className="num text-2xs text-muted">since {fmtDate(b.phase_since)}</div>}
      </td>
      <td className="num px-2 py-1 text-xs text-fg">{b.next_meeting?.date ? fmtDate(b.next_meeting.date) : DASH}</td>
      <td className="px-2 py-1">
        {ok && p?.next_meeting ? (
          <Tip content={pricedText(p.next_meeting)}>
            <span tabIndex={0}>
              <BpChip bp={p.next_meeting.change_bp} moves={p.next_meeting.moves_priced} />
            </span>
          </Tip>
        ) : (
          <span className="text-2xs text-faint">{p?.status === 'not_available' ? 'no official curve' : DASH}</span>
        )}
      </td>
      <td className="px-2 py-1">
        {ok && p?.h12m ? (
          <span className="inline-flex flex-col">
            <BpChip bp={p.h12m.change_bp} moves={p.h12m.moves_priced} />
            <span className="num text-2xs text-muted">
              {Math.abs(p.h12m.moves_priced).toFixed(1)} {p.h12m.moves_priced >= 0 ? 'hikes' : 'cuts'}
              {p.status === 'stale' ? ' · stale' : ''}
            </span>
          </span>
        ) : (
          <span className="text-faint">{DASH}</span>
        )}
      </td>
      <td className="num px-2 py-1 text-right text-xs">
        {ok && p?.h12m ? (
          <>
            {fmtChange(p.h12m.change_1w_bp, 0)} / {fmtChange(p.h12m.change_1m_bp, 0)}
          </>
        ) : (
          DASH
        )}
      </td>
      <td className={cn('num px-2 py-1 text-right', typeof real === 'number' && real < 0 ? 'text-up' : 'text-fg')}>{typeof real === 'number' ? `${fmtChange(real)}%` : DASH}</td>
      <td className="num px-2 py-1 text-right">{typeof st?.policy_minus_taylor_bp === 'number' ? <span className={st.policy_minus_taylor_bp < 0 ? 'text-up' : 'text-down'}>{fmtChange(st.policy_minus_taylor_bp, 0)}bp</span> : DASH}</td>
      <td className="px-2 py-1">
        {typeof tone === 'number' ? (
          <span className="inline-flex items-center gap-1.5" title={`Latest ${b.tone?.doc_type ?? ''} ${b.tone?.date ?? ''} · −1 dovish … +1 hawkish (model-based)`}>
            {(b.tone_trend?.length ?? 0) >= 2 ? (
              <ToneSpark trend={b.tone_trend ?? []} bank={b.name} />
            ) : (
              <span className="relative h-1.5 w-12 rounded-full bg-surface-3" aria-hidden>
                <span className="absolute top-0 h-1.5 w-px bg-border-strong" style={{ left: '50%' }} />
                <span className={cn('absolute top-0 h-1.5 rounded-full', tone >= 0 ? 'bg-down' : 'bg-up')} style={tone >= 0 ? { left: '50%', width: `${Math.min(50, tone * 50)}%` } : { right: '50%', width: `${Math.min(50, -tone * 50)}%` }} />
              </span>
            )}
            <span className="num text-xs text-fg">{fmtChange(tone)}</span>
            <span className="num text-2xs whitespace-nowrap text-muted">{b.tone?.date ? fmtDate(b.tone.date) : ''}</span>
          </span>
        ) : (
          <span className="text-faint">{DASH}</span>
        )}
      </td>
    </tr>
  )
}

function ImpliedComparison() {
  const q = useImpliedPaths()
  return (
    <Panel title="Priced over the next 12 months" subtitle={IMPLIED_LABEL}>
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(d) => <ImpliedBars d={d} />}
      </QueryView>
    </Panel>
  )
}

function ImpliedBars({ d }: { d: CbImplied }) {
  const t = useChartTokens()
  const navigate = useNavigate()
  const rows = useMemo(() => d.banks.filter((b) => b.status !== 'not_available' && b.summary?.h12m).sort((a, b) => (a.summary?.h12m?.change_bp ?? 0) - (b.summary?.h12m?.change_bp ?? 0)), [d])
  const missing = d.banks.filter((b) => b.status === 'not_available' || !b.summary?.h12m).map((b) => b.bank.toUpperCase())
  const option = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 56, top: 8, bottom: 24, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const b = rows[(ps as { dataIndex: number }[])[0].dataIndex]
          const h = b.summary?.h12m
          return `${ttTitle(b.name)}${ttRow(t.series[0], 'priced 12m', pricedText(h, b.step_bp ?? 25))}${ttRow(t.ref, 'implied rate', `${fmtMacro(h?.implied_rate)}%`)}${ttRow(t.ref, 'policy rate', `${fmtMacro(b.policy_rate)}%`)}${ttRow(t.ref, 'curve', String(b.source ?? ''))}`
        },
      },
      xAxis: { type: 'value', name: 'bp priced (12m)', nameLocation: 'middle', nameGap: 22, axisLabel: { formatter: (v: number) => `${v}` } },
      yAxis: { type: 'category', data: rows.map((b) => b.bank.toUpperCase()), axisTick: { show: false }, axisLabel: { color: t.fg, fontSize: 10 } },
      series: [
        {
          type: 'bar',
          barMaxWidth: 14,
          data: rows.map((b) => {
            const bp = b.summary?.h12m?.change_bp ?? 0
            return { value: bp, itemStyle: { color: bp >= 0 ? t.down : t.up } }
          }),
          label: { show: true, position: 'right', fontSize: 10, color: t.muted, formatter: (p: unknown) => `${fmtChange((p as { value: number }).value, 0)}bp` },
          markLine: { silent: true, symbol: 'none', label: { show: false }, lineStyle: { color: t.borderStrong, type: 'solid', width: 1 }, data: [{ xAxis: 0 }] },
        },
      ],
    }) as EChartsOption,
    [rows, t],
  )
  return (
    <div>
      <EChart
        option={option}
        height={Math.max(200, rows.length * 22 + 40)}
        ariaLabel="Basis points of tightening or easing priced over 12 months by central bank"
        onEvents={{ click: (p: unknown) => navigate(`/macro/cb/${rows[(p as { dataIndex: number }).dataIndex]?.bank}`) }}
      />
      <p className="mt-1 text-2xs text-muted">Rose = hikes priced, teal = cuts. No official curve (not shown): {missing.join(', ') || DASH}.</p>
    </div>
  )
}

/** Last 12 scored documents: tone line on a fixed −1…+1 scale; ▲ hike (top), ▼ cut (bottom), · hold. */
function ToneSpark({ trend, bank }: { trend: CbToneTrend; bank: string }) {
  const W = 84
  const H = 24
  const g = toneSpark(trend, W, H)
  const label = `${bank} tone trend: ${trend.map((p) => `${p.date} ${typeof p.tone_model === 'number' ? p.tone_model.toFixed(2) : 'n/a'}${p.action ? ` (${p.action})` : ''}`).join('; ')}`
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label} className="shrink-0">
      <title>{label}</title>
      <line x1={0} x2={W} y1={g.zeroY} y2={g.zeroY} stroke="var(--border-strong)" strokeWidth={0.75} strokeDasharray="2 2" />
      {g.marks.map((m, i) =>
        m.action === 'hike' ? (
          <path key={i} d={`M${m.x - 2.5},4 L${m.x + 2.5},4 L${m.x},0.5 Z`} fill="var(--down)" />
        ) : m.action === 'cut' ? (
          <path key={i} d={`M${m.x - 2.5},${H - 4} L${m.x + 2.5},${H - 4} L${m.x},${H - 0.5} Z`} fill="var(--up)" />
        ) : (
          <circle key={i} cx={m.x} cy={H - 1.5} r={0.9} fill="var(--faint)" />
        ),
      )}
      {g.line && <polyline points={g.line} fill="none" stroke="var(--c1)" strokeWidth={1.4} strokeLinejoin="round" strokeLinecap="round" />}
    </svg>
  )
}
