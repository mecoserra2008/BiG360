import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { nowcastTrack } from '@/api/macro/adapters'
import { useMacroNowcast } from '@/api/macro/hooks'
import { type MNowcast, type MNowcastModel, NOWCAST_COUNTRIES } from '@/api/macro/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroLines, type MLine } from './charts'
import { fmtChange, fmtMacro } from './fmt'
import { MacroNav, OfficialTag } from './shared'

export default function Nowcast() {
  const [params, setParams] = useSearchParams()
  const cc = (NOWCAST_COUNTRIES as readonly string[]).includes(params.get('cc') ?? '') ? (params.get('cc') as string) : 'US'
  const q = useMacroNowcast(cc)
  return (
    <>
      <PageHeader title="GDP nowcast" subtitle="Model-based nowcast (ours, DynamicFactorMQ on official monthly data) with the official GDPNow / NY Fed nowcasts as reference" />
      <MacroNav />
      <div className="mb-3">
        <Segmented label="Country" value={cc} onChange={(v) => setParams({ cc: v })} options={NOWCAST_COUNTRIES.map((c) => ({ value: c, label: c }))} />
      </div>
      <QueryView query={q} skeleton={<Skeleton className="h-[500px]" />}>
        {(n) => <NowcastBody key={n.country} n={n} />}
      </QueryView>
    </>
  )
}

const isOurs = (m: MNowcastModel) => !m.official

function NowcastBody({ n }: { n: MNowcast }) {
  const t = useChartTokens()
  const [ti, setTi] = useState(0)
  const target = n.targets[ti] ?? n.targets[0]
  const ours = target?.models.find(isOurs)
  const official = useMemo(() => target?.models.filter((m) => m.official) ?? [], [target])
  const lines = useMemo<MLine[]>(() => {
    if (!target) return []
    const out: MLine[] = []
    if (ours)
      out.push({
        name: 'Model-based nowcast (ours)',
        points: ours.history.map((h) => ({ date: h.as_of, value: h.nowcast })),
        band: { lo: ours.history.map((h) => h.lo ?? null), hi: ours.history.map((h) => h.hi ?? null), label: '80% interval (ours)' },
      })
    official.forEach((m) => out.push({ name: `${m.label} (official)`, points: m.history.map((h) => ({ date: h.as_of, value: h.nowcast })), dashed: true }))
    if (typeof target.actual === 'number' && out.some((l) => l.points.length)) {
      const dates = out.flatMap((l) => l.points.map((p) => p.date)).sort()
      out.push({ name: 'actual (first print)', points: [{ date: dates[0], value: target.actual }, { date: dates[dates.length - 1], value: target.actual }], muted: true, color: t.fg })
    }
    return out
  }, [target, ours, official, t.fg])
  if (!target) return <p className="py-8 text-center text-sm text-muted">No nowcast targets.</p>
  const track = nowcastTrack(n.targets)
  return (
    <div className="@container space-y-3">
      <div className="grid gap-3 @5xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel
          title={`Nowcast path · ${target.target_label}`}
          subtitle={`${n.units} · each point = the nowcast as of that date`}
          actions={<Select label="Target quarter" value={String(ti)} onChange={(v) => setTi(Number(v))} options={n.targets.map((x, k) => ({ value: String(k), label: `${x.target_label}${typeof x.actual === 'number' ? ' (released)' : ''}` }))} />}
        >
          <MacroLines lines={lines} height={300} units={n.units} ariaLabel={`${n.country} GDP nowcast path for ${target.target_label}`} />
          <div className="mt-2 flex flex-wrap gap-2">
            {target.models.map((m) => (
              <Tip key={m.model_id} content={<div className="max-w-sm">{m.source ?? m.label}</div>}>
                <span tabIndex={0} className="inline-flex cursor-help items-center gap-1 text-xs">
                  <OfficialTag official={m.official} label={m.official ? 'official' : 'model-based (ours)'} />
                  <span className="text-fg">{m.label}</span>
                  <span className="num font-semibold text-fg">{fmtMacro(m.latest?.nowcast)}</span>
                  {m.latest?.as_of && <span className="num text-muted">as of {fmtDate(m.latest.as_of)}</span>}
                </span>
              </Tip>
            ))}
          </div>
        </Panel>
        <Panel title="What moved the nowcast" subtitle="News decomposition of our model since the previous nowcast">
          {ours ? <NewsWaterfall m={ours} /> : <p className="py-8 text-center text-xs text-muted">No model nowcast for this target.</p>}
        </Panel>
      </div>
      <Panel title="Track record" subtitle="Last nowcast before the first official print vs the print (error = nowcast − actual)" flush>
        <table className="w-full text-sm" aria-label="Nowcast track record">
          <thead>
            <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1.5">Quarter</th>
              <th className="px-3 py-1.5 text-right">Actual</th>
              <th className="px-3 py-1.5 text-right">Ours (model-based)</th>
              <th className="px-3 py-1.5 text-right">error</th>
              <th className="px-3 py-1.5 text-right">GDPNow (official)</th>
              <th className="px-3 py-1.5 text-right">error</th>
              <th className="px-3 py-1.5 text-right">NY Fed (official)</th>
              <th className="px-3 py-1.5 text-right">error</th>
            </tr>
          </thead>
          <tbody>
            {track.map((r) => (
              <tr key={r.target} className="border-b border-border/60">
                <td className="num px-3 py-1 font-medium text-fg">{r.target}</td>
                <td className="num px-3 py-1 text-right font-semibold text-fg">{r.actual === null ? <span className="font-normal text-muted">pending</span> : fmtMacro(r.actual)}</td>
                <td className="num px-3 py-1 text-right">{fmtMacro(r.ours)}</td>
                <Err v={r.errOurs} />
                <td className="num px-3 py-1 text-right">{fmtMacro(r.gdpnow)}</td>
                <Err v={r.errGdpnow} />
                <td className="num px-3 py-1 text-right">{fmtMacro(r.nyfed)}</td>
                <Err v={r.errNyfed} />
              </tr>
            ))}
          </tbody>
        </table>
        <p className="px-3 py-2 text-2xs text-muted">{n.method}</p>
        <p className="px-3 pb-2 text-2xs text-muted">
          Panel: <span className="num">{(n.panel ?? []).join(', ')}</span>
        </p>
      </Panel>
    </div>
  )
}

function Err({ v }: { v: number | null }) {
  return <td className={cn('num px-3 py-1 text-right text-xs', v === null ? 'text-faint' : Math.abs(v) < 0.5 ? 'text-up' : Math.abs(v) > 1.5 ? 'text-down' : 'text-muted')}>{v === null ? DASH : fmtChange(v)}</td>
}

/** Waterfall: previous nowcast → contributions of each data release → latest nowcast. */
function NewsWaterfall({ m }: { m: MNowcastModel }) {
  const t = useChartTokens()
  const news = useMemo(() => m.news ?? [], [m])
  const prev = m.prev_nowcast ?? null
  const latest = m.latest?.nowcast ?? null
  const option = useMemo<EChartsOption | null>(() => {
    if (prev === null || latest === null) return null
    const labels = ['previous', ...news.map((x) => x.title ?? x.series_id), 'latest']
    const base: (number | null)[] = []
    const up: (number | null)[] = []
    const down: (number | null)[] = []
    const total: (number | null)[] = [prev]
    base.push(null)
    up.push(null)
    down.push(null)
    let run = prev
    for (const x of news) {
      const c = x.contribution ?? 0
      const lo = Math.min(run, run + c)
      base.push(lo)
      up.push(c >= 0 ? c : null)
      down.push(c < 0 ? -c : null)
      total.push(null)
      run += c
    }
    base.push(null)
    up.push(null)
    down.push(null)
    total.push(latest)
    const all = [prev, latest, ...base.filter((v): v is number => v !== null)]
    const minV = Math.min(...all)
    return {
      grid: { left: 8, right: 16, top: 8, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (ps: unknown) => {
          const i = (ps as { dataIndex: number }[])[0]?.dataIndex ?? 0
          if (i === 0) return ttTitle('Previous nowcast') + ttRow(t.ref, 'value', fmtMacro(prev))
          if (i === labels.length - 1) return ttTitle('Latest nowcast') + ttRow(t.series[0], 'value', fmtMacro(latest))
          const x = news[i - 1]
          return ttTitle(labels[i]) + ttRow((x.contribution ?? 0) >= 0 ? t.up : t.down, 'contribution', fmtChange(x.contribution, 3)) + `<div style="opacity:.7">${x.series_id}</div>`
        },
      },
      xAxis: { type: 'value', min: Math.floor(Math.min(minV, 0) * 2) / 2, axisLabel: { formatter: (v: number) => fmtMacro(v, 2) } },
      yAxis: { type: 'category', inverse: true, data: labels.map((l) => (l.length > 28 ? `${l.slice(0, 27)}…` : l)), axisTick: { show: false }, axisLabel: { fontSize: 10, color: t.fg } },
      series: [
        { type: 'bar', stack: 'w', data: base, itemStyle: { color: 'transparent' }, silent: true, barMaxWidth: 16 },
        { type: 'bar', stack: 'w', name: 'raised', data: up, itemStyle: { color: t.up }, barMaxWidth: 16 },
        { type: 'bar', stack: 'w', name: 'lowered', data: down, itemStyle: { color: t.down }, barMaxWidth: 16 },
        { type: 'bar', stack: 'w', name: 'nowcast', data: total, itemStyle: { color: t.series[0] }, barMaxWidth: 16, label: { show: true, position: 'right', fontSize: 10, color: t.muted, formatter: (p: unknown) => fmtMacro((p as { value: number }).value, 3) } },
      ],
    } as EChartsOption
  }, [news, prev, latest, t])
  if (!option) return <p className="py-8 text-center text-xs text-muted">No news since the previous nowcast.</p>
  return (
    <div>
      <EChart option={option} height={Math.max(160, (news.length + 2) * 30 + 20)} ariaLabel="Nowcast news decomposition waterfall" />
      <p className="mt-1 text-2xs text-muted">
        <Badge tone="info">model-based</Badge> Teal raised, rose lowered the nowcast. Change {fmtChange(latest !== null && prev !== null ? latest - prev : null, 3)} pp.
      </p>
    </div>
  )
}
