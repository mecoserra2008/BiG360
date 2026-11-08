import { ExternalLink } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router'
import { useCentralBank, useCentralBanks } from '@/api/macro/hooks'
import { type CbDetail, IMPLIED_LABEL, inferStep, pathView, pricedText, sepView } from '@/api/macro/v34'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select, Slider, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { BpChip, ImpliedNote, PhaseBadge } from './CentralBanks'
import { MacroLines } from './charts'
import { fmtChange, fmtMacro, isoDaysFromNow } from './fmt'
import { MacroNav } from './shared'

export default function Bank() {
  const { bank = 'fed' } = useParams()
  const q = useCentralBank(bank)
  const all = useCentralBanks()
  const navigate = useNavigate()
  return (
    <>
      <PageHeader
        title={q.data ? q.data.bank.name : bank.toUpperCase()}
        subtitle="Central-bank page · official rates, decisions, projections and balance sheet · implied path approximated from official curves"
        actions={<Select label="Central bank" value={bank} onChange={(v) => navigate(`/macro/cb/${v}`)} options={(all.data?.banks ?? [{ bank, name: bank }]).map((b) => ({ value: b.bank, label: b.name }))} />}
      />
      <MacroNav />
      <QueryView query={q} skeleton={<Skeleton className="h-[700px]" />}>
        {(d) => <BankBody key={d.bank.bank} d={d} />}
      </QueryView>
    </>
  )
}

function BankBody({ d }: { d: CbDetail }) {
  const b = d.bank
  const latest = d.rates.latest
  const ip = d.implied_path
  // term-premium switch (Fed): drives the header chips too, not only the implied-path panel
  const [tp, setTp] = useState(false)
  const head = useMemo(() => {
    const pts = (ip?.points ?? []).filter((p) => typeof p.implied_rate === 'number')
    const v = pathView(pts, ip?.policy_rate, inferStep(ip?.points), tp && !!ip?.tp_adjusted)
    const i12 = pts.findIndex((p) => p.point_type === 'horizon' && p.label === '12m')
    const iN = pts.findIndex((p) => p.point_type === 'meeting')
    return { h12: i12 >= 0 ? v[i12] : null, next: iN >= 0 ? v[iN] : null }
  }, [ip, tp])
  return (
    <div className="@container space-y-3">
      <Panel>
        <div className="flex flex-wrap items-start gap-x-6 gap-y-2">
          <div>
            <div className="label-caps">{b.rate_name}</div>
            <div className="num text-xl font-semibold text-fg">{fmtMacro(latest?.policy_rate, 4)}%</div>
            <div className="num text-2xs text-muted">
              {latest && typeof latest.lower === 'number' && typeof latest.upper === 'number' ? `range ${fmtMacro(latest.lower, 3)}–${fmtMacro(latest.upper, 3)} · ` : ''}
              {latest && typeof latest.deposit === 'number' ? `deposit ${fmtMacro(latest.deposit, 3)} · ` : ''}
              as of {latest?.date ? fmtDate(latest.date) : DASH}
            </div>
            <div className="num text-2xs text-muted">source {latest?.source ?? DASH}</div>
          </div>
          <div>
            <div className="label-caps">Rate cycle</div>
            <PhaseBadge phase={d.rate_cycle?.phase} />
            <div className="num text-2xs text-muted">since {d.rate_cycle?.phase_since ? fmtDate(d.rate_cycle.phase_since) : DASH}</div>
          </div>
          <div>
            <div className="label-caps">Next meeting</div>
            <div className="num text-sm text-fg">{d.meetings.upcoming[0] ? fmtDate(d.meetings.upcoming[0].date) : DASH}</div>
            {ip?.summary?.next_meeting && (
              <div className="mt-0.5 flex items-center gap-1 text-2xs text-muted">
                priced <BpChip bp={head.next?.change_bp ?? ip.summary.next_meeting.change_bp} moves={head.next?.moves_priced ?? ip.summary.next_meeting.moves_priced} />
                {head.next?.adjusted && <span title="term-premium adjusted (model-based)">TP-adj.</span>}
              </div>
            )}
          </div>
          <div>
            <div className="label-caps">Priced 12m{head.h12?.adjusted ? ' · TP-adjusted' : ''}</div>
            {ip?.summary?.h12m ? (
              <div className="text-sm text-fg" data-testid="priced-12m">{pricedText(head.h12 && head.h12.change_bp !== null ? { change_bp: head.h12.change_bp, moves_priced: head.h12.moves_priced ?? 0 } : ip.summary.h12m, inferStep(ip.points))}</div>
            ) : (
              <div className="text-xs text-muted">no official curve</div>
            )}
          </div>
          <div className="min-w-48 flex-1 text-xs text-muted">
            <div>
              Target: <span className="text-fg">{b.inflation_target_text ?? DASH}</span>
            </div>
            <div className="mt-0.5 flex flex-wrap gap-2">
              {b.url && (
                <a href={b.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
                  official site <ExternalLink className="size-3" />
                </a>
              )}
              {b.inflation_target_url && (
                <a href={b.inflation_target_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-accent-text hover:underline">
                  inflation target <ExternalLink className="size-3" />
                </a>
              )}
            </div>
          </div>
        </div>
      </Panel>
      <ImpliedNote labels={d.labels} />
      {ip && ip.status !== 'not_available' ? <ImpliedPanel d={d} tp={tp} setTp={setTp} /> : <Panel title="Implied path">{<p className="py-6 text-center text-xs text-muted">No official curve to approximate a path for this bank.</p>}</Panel>}
      <div className="grid gap-3 @5xl:grid-cols-2">
        {ip?.curves && ip.curves.length > 0 && <CurvePanel d={d} />}
        <DecisionsPanel d={d} />
      </div>
      <div className="grid gap-3 @5xl:grid-cols-2">
        <StancePanel d={d} />
        <BalanceSheetPanel d={d} />
      </div>
      <TonePanel d={d} />
      <CaveatsPanel d={d} />
    </div>
  )
}

/* ───────────── implied path ───────────── */

function ImpliedPanel({ d, tp, setTp }: { d: CbDetail; tp: boolean; setTp: (v: boolean) => void }) {
  const t = useChartTokens()
  const ip = d.implied_path!
  const [hz, setHz] = useState('12m')
  const pts = useMemo(() => (ip.points ?? []).filter((p) => typeof p.implied_rate === 'number'), [ip])
  const view = useMemo(() => pathView(pts, ip.policy_rate, inferStep(ip.points), tp), [pts, ip, tp])
  const from = isoDaysFromNow(-3 * 365)
  const hist = useMemo(() => d.rates.history.filter((r) => r.date >= from), [d.rates.history, from])
  const sep = useMemo(() => (d.projections?.vs_market ?? []) as { year: string; sep_median?: number | null; market_meeting?: string | null; market_implied?: number | null; market_implied_tp_adj?: number | null; market_minus_sep_bp?: number | null }[], [d.projections])
  const option = useMemo<EChartsOption>(() => {
    const val = (i: number) => view[i]?.implied ?? null
    return {
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      legend: { top: 0, left: 28 },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const arr = ps as { seriesName: string; data: [string, number | null]; color: string }[]
          if (!arr.length) return ''
          const pi = pts.findIndex((x) => x.target_date === arr[0].data[0])
          const p = pi >= 0 ? pts[pi] : null
          const pv = pi >= 0 ? view[pi] : null
          return (
            ttTitle(fmtDate(String(arr[0].data[0]).slice(0, 10))) +
            arr.map((a) => ttRow(a.color, a.seriesName, `${fmtMacro(a.data[1])}%`)).join('') +
            (p && pv ? ttRow(t.ref, pv.adjusted ? 'vs today (TP-adjusted)' : 'vs today', pricedText({ change_bp: pv.change_bp ?? p.change_bp, moves_priced: pv.moves_priced ?? p.moves_priced }, inferStep(ip.points))) + ttRow(t.ref, '1w / 1m change (raw)', `${fmtChange(p.change_1w_bp, 0)} / ${fmtChange(p.change_1m_bp, 0)} bp`) : '')
          )
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', scale: true, name: '%', nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' }, axisLabel: { formatter: (v: number) => fmtMacro(v, 3) } },
      series: [
        { type: 'line', name: 'policy rate (official)', step: 'end', data: hist.map((r) => [r.date, r.policy_rate]), showSymbol: false, color: t.ref, lineStyle: { color: t.ref, width: 1.5 } },
        {
          type: 'line',
          name: tp ? 'implied path, term-premium adjusted (model-based)' : 'implied path (approx. from official curve)',
          step: 'end',
          data: [[ip.as_of, ip.policy_rate], ...pts.map((p, i) => [p.target_date, val(i)])],
          symbol: 'circle',
          symbolSize: (_v: unknown, p: { dataIndex: number }) => (p.dataIndex > 0 && pts[p.dataIndex - 1]?.point_type === 'meeting' ? 7 : 4),
          color: t.series[0],
          lineStyle: { color: t.series[0], width: 2 },
        },
        ...(sep.length
          ? [
              {
                type: 'scatter',
                name: 'official projection (median)',
                symbol: 'diamond',
                symbolSize: 11,
                data: sep.filter((s) => typeof s.sep_median === 'number' && s.market_meeting).map((s) => [s.market_meeting, s.sep_median]),
                color: t.series[1],
              },
            ]
          : []),
      ],
    } as EChartsOption
  }, [pts, view, tp, hist, sep, ip, t])
  const histLines = (ip.history ?? []).filter((h) => h.label === hz)
  return (
    <Panel
      title="Implied policy path"
      subtitle={`${ip.source ?? ''} · as of ${ip.as_of ? fmtDate(ip.as_of) : DASH}`}
      actions={
        ip.tp_adjusted ? (
          <label className="flex items-center gap-1.5 text-xs text-muted">
            <Switch label="Term-premium adjusted path" checked={tp} onCheckedChange={setTp} /> Term-premium adjusted <Badge tone="info">model-based</Badge>
          </label>
        ) : undefined
      }
    >
      <div className="grid gap-3 @4xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div>
          <EChart option={option} height={320} ariaLabel={`${d.bank.name} implied policy path vs policy-rate history`} />
          <p className="mt-1 text-2xs text-muted">
            {IMPLIED_LABEL}. Large markers = meeting dates; small = horizons. {sep.some((s) => typeof s.sep_median === 'number' && s.market_meeting) ? `Diamonds: ${d.projections?.label ?? 'official projections'} (official).` : ''}
          </p>
        </div>
        <div className="min-w-0 space-y-2">
          <table className="w-full text-xs" aria-label="Implied path by meeting and horizon">
            <thead>
              <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                <th className="py-1">Meeting / horizon</th>
                <th className="py-1 text-right">Implied</th>
                <th className="py-1 text-right">vs today{tp ? ' (adj.)' : ''}</th>
                <th className="py-1 text-right">this step{tp ? ' (adj.)' : ''}</th>
              </tr>
            </thead>
            <tbody>
              {pts.map((p, i) => (
                <tr key={`${p.point_type}-${p.label}`} className={cn('border-b border-border/60', p.point_type === 'horizon' && 'text-muted')}>
                  <td className="num py-0.5">{p.point_type === 'meeting' ? fmtDate(p.target_date) : `${p.label} (${fmtDate(p.target_date)})`}</td>
                  <td className="num py-0.5 text-right text-fg">{fmtMacro(view[i].implied)}%</td>
                  <td className="py-0.5 text-right">
                    <BpChip bp={view[i].change_bp} moves={view[i].moves_priced} step={inferStep(ip.points)} />
                  </td>
                  <td className="num py-0.5 text-right">{p.point_type === 'meeting' ? `${fmtChange(view[i].incremental_bp, 0)}bp` : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {sep.length > 0 && (
            <table className="w-full text-xs" aria-label="Official projections vs market">
              <thead>
                <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                  <th className="py-1">Year-end</th>
                  <th className="py-1 text-right">Official median</th>
                  <th className="py-1 text-right">Curve-implied</th>
                  <th className="py-1 text-right">Market − official</th>
                </tr>
              </thead>
              <tbody>
                {sep.map((s) => (
                  <tr key={s.year} className="border-b border-border/60">
                    <td className="num py-0.5">{s.year}</td>
                    <td className="num py-0.5 text-right">{fmtMacro(s.sep_median)}%</td>
                    <td className="num py-0.5 text-right">{fmtMacro(sepView(s, tp).market)}%</td>
                    <td className="num py-0.5 text-right font-medium text-fg">{fmtChange(sepView(s, tp).minus_bp, 0)}bp</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {d.projections?.vs_market_label && <p className="text-2xs text-muted">{d.projections.vs_market_label}</p>}
        </div>
      </div>
      {(ip.history ?? []).length > 0 && (
        <div className="mt-3">
          <div className="mb-1 flex items-center gap-2">
            <span className="text-xs font-semibold text-fg">How the priced rate has moved</span>
            <Segmented label="Horizon" size="xs" value={hz} onChange={setHz} options={(ip.history ?? []).map((h) => ({ value: h.label, label: h.label }))} />
          </div>
          <MacroLines
            lines={histLines.flatMap((h) => [
              { name: `implied rate in ${h.label}`, points: h.points.map((p) => ({ date: p.as_of, value: p.implied_rate })) },
              ...(tp ? [{ name: `${h.label}, term-premium adjusted (model)`, points: h.points.map((p) => ({ date: p.as_of, value: p.implied_rate_tp_adj ?? null })), dashed: true }] : []),
            ])}
            units="%"
            height={200}
            ariaLabel="History of the curve-implied rate at a fixed horizon"
          />
        </div>
      )}
    </Panel>
  )
}

/* ───────────── curve ───────────── */

function CurvePanel({ d }: { d: CbDetail }) {
  const t = useChartTokens()
  const curves = d.implied_path?.curves ?? []
  const order = ['1y', '6m', '3m', '1m', '1w', 'latest']
  const sorted = [...curves].sort((a, b) => order.indexOf(a.snapshot) - order.indexOf(b.snapshot))
  const [i, setI] = useState(sorted.length - 1)
  const [kind, setKind] = useState<'forward' | 'zero'>('forward')
  const option = useMemo<EChartsOption>(() => {
    return {
      grid: { left: 8, right: 16, top: 30, bottom: 24, containLabel: true },
      legend: { top: 0, right: 0 },
      tooltip: { trigger: 'axis', valueFormatter: (v) => `${fmtMacro(v as number, 4)}%` },
      xAxis: { type: 'value', name: 'years', nameLocation: 'middle', nameGap: 20, min: 0 },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => fmtMacro(v, 3) } },
      // selected snapshot (solid) vs the latest curve (dashed) and the other snapshots (faint)
      series: sorted.map((c, k) => {
        const sel = k === i
        const latest = k === sorted.length - 1
        return {
          type: 'line',
          name: `${c.snapshot} (${fmtDate(c.as_of)})`,
          data: c.points.map((p) => [p.tenor_years, kind === 'forward' ? p.forward_rate : p.zero_rate]),
          showSymbol: false,
          color: sel ? t.series[0] : latest ? t.series[1] : t.ref,
          lineStyle: { color: sel ? t.series[0] : latest ? t.series[1] : alpha(t.ref, 0.3), width: sel ? 2 : 1, type: sel || !latest ? 'solid' : 'dashed' },
          z: sel ? 3 : latest ? 2 : 1,
        }
      }),
    } as EChartsOption
  }, [sorted, i, kind, t])
  return (
    <Panel
      title="Official curve used"
      subtitle={`${d.implied_path?.source ?? ''} · ${kind} rates · scrub through the snapshots`}
      actions={
        <Segmented
          label="Curve kind"
          size="xs"
          value={kind}
          onChange={setKind}
          options={[
            { value: 'forward', label: 'Forwards' },
            { value: 'zero', label: 'Spot (zero)' },
          ]}
        />
      }
    >
      <EChart option={option} height={260} ariaLabel={`${d.bank.name} official curve snapshots`} />
      <div className="mt-2 flex items-center gap-3 text-xs">
        <span className="shrink-0 text-muted">Snapshot</span>
        <Slider label="Curve snapshot" min={0} max={Math.max(0, sorted.length - 1)} step={1} value={[i]} onValueChange={([v]) => setI(v)} className="max-w-xs" />
        <span className="num shrink-0 text-fg">
          {sorted[i] ? `${sorted[i].snapshot} ago · ${fmtDate(sorted[i].as_of)}`.replace('latest ago', 'latest') : DASH}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 text-2xs text-faint">
        {sorted.map((c, k) => (
          <button key={c.snapshot} type="button" onClick={() => setI(k)} className={cn('hover:text-fg', k === i && 'font-semibold text-accent-text')}>
            {c.snapshot}
          </button>
        ))}
      </div>
    </Panel>
  )
}

/* ───────────── decisions ───────────── */

function DecisionsPanel({ d }: { d: CbDetail }) {
  const [all, setAll] = useState(false)
  const rows = all ? d.decisions : d.decisions.slice(0, 12)
  return (
    <Panel title="Decision history" subtitle={String((d.labels as Record<string, unknown>).decisions ?? 'Derived from official rate changes')} flush>
      <div className="max-h-[340px] overflow-y-auto">
        <table className="w-full text-xs" aria-label="Policy decisions">
          <thead>
            <tr className="sticky top-0 border-b border-border bg-surface text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1">Decision</th>
              <th className="px-2 py-1">Change</th>
              <th className="px-2 py-1 text-right">Rate after</th>
              <th className="px-2 py-1">Vote</th>
              <th className="px-3 py-1">Basis</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((x) => (
              <tr key={`${x.decision_date}-${x.rate_after}`} className="border-b border-border/60">
                <td className="num px-3 py-0.5 text-fg">{fmtDate(x.decision_date)}</td>
                <td className="px-2 py-0.5">
                  <BpChip bp={x.change_bp} />
                </td>
                <td className="num px-2 py-0.5 text-right">{fmtMacro(x.rate_after, 4)}%</td>
                <td className="px-2 py-0.5 text-muted" title={x.vote_source ?? undefined}>
                  {x.vote ?? DASH}
                  {x.vote_detail ? ` · ${String(x.vote_detail)}` : ''}
                </td>
                <td className="px-3 py-0.5 text-muted">{x.scheduled === false ? 'unscheduled' : (x.date_basis ?? '').replace(/_/g, ' ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {d.decisions.length > 12 && (
        <button type="button" className="px-3 py-1.5 text-xs text-accent-text hover:underline" onClick={() => setAll(!all)}>
          {all ? 'Show recent only' : `Show all ${d.decisions.length} decisions`}
        </button>
      )}
    </Panel>
  )
}

/* ───────────── stance ───────────── */

function StancePanel({ d }: { d: CbDetail }) {
  const s = d.stance
  const l = s?.latest
  if (!s || !l) return <Panel title="Stance">{<p className="py-6 text-center text-xs text-muted">No stance inputs for this bank.</p>}</Panel>
  const h = s.history ?? []
  const ser = (k: string) => h.map((p) => ({ date: p.date, value: (p as unknown as Record<string, number | null>)[k] ?? null }))
  return (
    <Panel title="Stance" subtitle="Real rates (official data) and Taylor-rule benchmarks (model-based, official inputs)">
      <div className="mb-2 grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs @xl:grid-cols-3">
        <Kv k="Real rate vs core" v={`${fmtChange(l.real_rate_core)}%`} />
        <Kv k="Real rate vs headline" v={`${fmtChange(l.real_rate_cpi)}%`} />
        <Kv k="Real rate vs expected" v={`${fmtChange(l.real_rate_expected)}%`} badge="model" />
        <Kv k="r* (neutral real rate)" v={typeof l.r_star === 'number' ? `${fmtMacro(l.r_star)}%` : 'not published'} note={l.r_star_source ?? undefined} badge={typeof l.r_star !== 'number' ? undefined : /model/i.test(String(l.r_star_source ?? '')) ? 'model' : 'official'} />
        <Kv k="Output gap" v={`${fmtChange(l.output_gap)}%`} note={l.output_gap_source ?? undefined} />
        <Kv k={typeof l.taylor_rstar === 'number' ? 'Policy − Taylor (official r*)' : 'Policy − Taylor (1993)'} v={`${fmtChange(l.policy_minus_taylor_bp, 0)}bp`} badge="model" note={typeof l.taylor_rstar === 'number' ? 'Policy rate minus the Taylor rule with the official r* (Taylor 1993 is plotted below)' : 'No official r*: policy rate minus Taylor (1993)'} />
      </div>
      <div className="grid gap-2 @3xl:grid-cols-2">
        <MacroLines
          lines={[
            { name: 'real rate (core)', points: ser('real_rate_core') },
            { name: 'real rate (headline)', points: ser('real_rate_cpi') },
            { name: 'r*', points: ser('r_star'), dashed: true, muted: true },
          ]}
          units="%"
          height={200}
          zeroLine
          ariaLabel="Real policy rate history"
        />
        <MacroLines
          lines={[
            { name: 'policy rate', points: ser('policy_rate'), color: undefined },
            { name: 'Taylor 1993 (model)', points: ser('taylor_1993'), dashed: true },
            { name: 'Taylor with r* (model)', points: ser('taylor_rstar'), dashed: true },
            { name: 'balanced (model)', points: ser('taylor_balanced'), dashed: true },
          ]}
          units="%"
          height={200}
          ariaLabel="Policy rate vs Taylor-rule rates"
        />
      </div>
      <p className="mt-1 text-2xs text-muted">
        Inflation measure: {l.infl_taylor_measure ?? DASH} · expected inflation: {l.expected_infl_source ?? DASH} <Badge tone="info">model-based</Badge>
      </p>
    </Panel>
  )
}

function Kv({ k, v, note, badge }: { k: string; v: string; note?: string; badge?: 'model' | 'official' }) {
  return (
    <div className="min-w-0" title={note}>
      <div className="flex items-center gap-1 text-2xs text-muted">
        {k}
        {badge && <Badge tone={badge === 'official' ? 'up' : 'info'}>{badge === 'official' ? 'official' : 'model-based'}</Badge>}
      </div>
      <div className="num text-sm font-medium text-fg">{v}</div>
    </div>
  )
}

/* ───────────── balance sheet ───────────── */

function BalanceSheetPanel({ d }: { d: CbDetail }) {
  const bs = d.balance_sheet
  if (!bs?.available) return <Panel title="Balance sheet">{<p className="py-6 text-center text-xs text-muted">No official balance-sheet series for this bank.</p>}</Panel>
  const from = isoDaysFromNow(-6 * 365)
  return (
    <Panel title="Balance sheet" subtitle={`${bs.unit ?? ''} · official weekly / monthly statements`}>
      <MacroLines lines={bs.components.slice(0, 4).map((c) => ({ name: c.component.replace(/_/g, ' '), points: (c.history ?? []).filter((p) => p.date >= from).map((p) => ({ date: p.date, value: p.value_bn })) }))} height={200} ariaLabel="Central bank balance sheet components" />
      <table className="mt-2 w-full text-xs" aria-label="Balance sheet components and QT pace">
        <thead>
          <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
            <th className="py-1">Component</th>
            <th className="py-1 text-right">Latest</th>
            <th className="py-1 text-right">13w Δ</th>
            <th className="py-1 text-right">Pace / yr</th>
            <th className="py-1 text-right">1y Δ</th>
            <th className="py-1 pl-3">Source</th>
          </tr>
        </thead>
        <tbody>
          {bs.components.map((c) => (
            <tr key={c.component} className="border-b border-border/60">
              <td className="py-0.5 text-fg">{c.component.replace(/_/g, ' ')}</td>
              <td className="num py-0.5 text-right">{fmtMacro(c.latest?.value_bn)}</td>
              <td className="num py-0.5 text-right">{fmtChange(c.latest?.change_13w_bn, 0)}</td>
              <td className={cn('num py-0.5 text-right font-medium', (c.latest?.pace_annualized_bn ?? 0) < 0 ? 'text-down' : 'text-fg')}>{fmtChange(c.latest?.pace_annualized_bn, 0)}</td>
              <td className="num py-0.5 text-right">{fmtChange(c.latest?.change_1y_bn, 0)}</td>
              <td className="num truncate py-0.5 pl-3 text-2xs text-muted">{c.source}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-1 text-2xs text-muted">QT pace: {bs.qt_pace_method}</p>
    </Panel>
  )
}

/* ───────────── tone ───────────── */

function TonePanel({ d }: { d: CbDetail }) {
  const t = useChartTokens()
  const tone = d.tone
  const docs = useMemo(() => [...(tone?.history ?? [])].sort((a, b) => b.date.localeCompare(a.date)), [tone])
  const option = useMemo<EChartsOption>(() => {
    const h = [...(tone?.history ?? [])].sort((a, b) => a.date.localeCompare(b.date))
    const decisions = d.decisions.filter((x) => h.length && x.decision_date >= h[0].date)
    return {
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0 },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = p as { seriesName: string; dataIndex: number }
          if (q.seriesName === 'tone') {
            const x = h[q.dataIndex]
            return `${ttTitle(`${fmtDate(x.date)} · ${x.doc_type}`)}${ttRow(t.series[0], 'tone (model)', fmtChange(x.tone_model))}${ttRow(t.ref, 'hawkish / dovish share', `${Math.round((x.hawkish_share ?? 0) * 100)}% / ${Math.round((x.dovish_share ?? 0) * 100)}%`)}${x.vote_text ? ttRow(t.ref, 'vote', x.vote_text) : ''}`
          }
          const x = decisions[q.dataIndex]
          return x ? `${ttTitle(fmtDate(x.decision_date))}${ttRow(t.ref, x.action ?? '', `${fmtChange(x.change_bp, 0)}bp`)}` : ''
        },
      },
      xAxis: { type: 'time', axisLabel: { hideOverlap: true } },
      yAxis: { type: 'value', min: -1, max: 1, name: '← dovish · hawkish →', nameLocation: 'end', nameGap: 8, nameTextStyle: { align: 'left' } },
      series: [
        { type: 'line', name: 'tone', data: h.map((x) => [x.date, x.tone_model]), symbolSize: 6, color: t.series[0], lineStyle: { color: t.series[0], width: 1.5 } },
        {
          type: 'scatter',
          name: 'decisions',
          symbol: 'triangle',
          symbolSize: 9,
          data: decisions.map((x) => ({ value: [x.decision_date, (x.change_bp ?? 0) > 0 ? 0.9 : (x.change_bp ?? 0) < 0 ? -0.9 : 0], symbolRotate: (x.change_bp ?? 0) < 0 ? 180 : 0, itemStyle: { color: (x.change_bp ?? 0) > 0 ? t.down : (x.change_bp ?? 0) < 0 ? t.up : t.ref } })),
        },
      ],
    } as EChartsOption
  }, [tone, d.decisions, t])
  if (!tone?.available) return <Panel title="Communication tone">{<p className="py-6 text-center text-xs text-muted">No official statements scored for this bank.</p>}</Panel>
  return (
    <Panel title="Communication tone" subtitle={<span className="inline-flex items-center gap-1.5"><Badge tone="info">model-based</Badge> {tone.label}</span>}>
      <div className="grid gap-3 @4xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div>
          <EChart option={option} height={240} ariaLabel={`${d.bank.name} statement tone over time with decision markers`} />
          <p className="mt-1 text-2xs text-muted">Triangles: decisions (▲ rose hike at top, ▼ teal cut at bottom, grey hold). Model: {tone.model_id}.</p>
        </div>
        <ul className="max-h-[260px] divide-y divide-border/60 overflow-y-auto text-xs" aria-label="Scored official documents">
          {docs.map((x) => (
            <li key={`${x.date}-${x.doc_type}`} className="flex items-center gap-2 py-1">
              <span className="num w-24 shrink-0 text-muted">{fmtDate(x.date)}</span>
              <span className="w-20 shrink-0 text-muted">{x.doc_type}</span>
              <span className={cn('num w-12 shrink-0 text-right font-medium', (x.tone_model ?? 0) > 0.1 ? 'text-down' : (x.tone_model ?? 0) < -0.1 ? 'text-up' : 'text-fg')}>{fmtChange(x.tone_model)}</span>
              <span className="min-w-0 flex-1 truncate text-muted">{x.vote_text ? `vote ${x.vote_text}` : ''}</span>
              {x.url && (
                <a href={x.url} target="_blank" rel="noreferrer" className="text-accent-text hover:underline" aria-label={`Open ${x.doc_type} of ${x.date}`}>
                  <ExternalLink className="size-3" />
                </a>
              )}
            </li>
          ))}
        </ul>
      </div>
    </Panel>
  )
}

/* ───────────── caveats ───────────── */

function CaveatsPanel({ d }: { d: CbDetail }) {
  const ip = d.implied_path
  const labels = Object.entries(d.labels as Record<string, unknown>)
  return (
    <Panel title="Method and caveats">
      {ip && (
        <>
          <p className="text-xs text-fg">
            <b>Implied path method.</b> {ip.method}
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs text-muted">
            {(ip.caveats ?? []).map((c) => (
              <li key={c}>{c}</li>
            ))}
          </ul>
        </>
      )}
      <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-xs">
        {labels.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted">{k.replace(/_/g, ' ')}</dt>
            <dd className="text-fg">{String(v)}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  )
}
