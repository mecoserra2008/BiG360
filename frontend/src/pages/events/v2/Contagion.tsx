import { useMemo, useState } from 'react'
import { useEmConnectedness, useEmIrf, useEmSpillovers } from '@/api/events/hooks'
import { type EmConnectedness, type EmIrf, type EmSpillovers, connectednessGraph } from '@/api/events/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ModelBadge } from './shared'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const f3 = (v: unknown) => (fin(v) ? v.toFixed(3) : DASH)
const pv = (v: unknown) => (fin(v) ? (v < 0.001 ? '<0.001' : v.toFixed(3)) : DASH)

export function ConnectednessPanel() {
  const [u, setU] = useState('country_ret')
  const q = useEmConnectedness(u)
  return (
    <Panel
      title="Contagion network"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <ModelBadge /> Diebold-Yilmaz connectedness (rolling VAR, generalized FEVD): net transmitters (rose) vs receivers (blue); arrows = strongest net pairwise spillovers
        </span>
      }
      actions={
        <Segmented
          label="Universe"
          size="xs"
          value={u}
          onChange={setU}
          options={[
            { value: 'country_ret', label: 'Countries · returns' },
            { value: 'country_vol', label: 'Countries · vol' },
            { value: 'sector_ret', label: 'Sectors · returns' },
            { value: 'sector_vol', label: 'Sectors · vol' },
          ]}
        />
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-[460px]" />}>
        {(d) => <ConnBody d={d} />}
      </QueryView>
    </Panel>
  )
}

function ConnBody({ d }: { d: EmConnectedness }) {
  const t = useChartTokens()
  const g = useMemo(() => connectednessGraph(d), [d])
  const maxEdge = Math.max(1e-9, ...g.edges.map((e) => e.value))
  const option = useMemo<EChartsOption>(
    () => ({
      tooltip: {
        formatter: (p: unknown) => {
          const x = p as {
            dataType: string
            data: {
              name?: string
              id?: string
              net?: number
              source?: string
              target?: string
              value?: number
            }
          }
          if (x.dataType === 'edge') return `${ttTitle(`${x.data.source} → ${x.data.target}`)}${ttRow(t.down, 'net pairwise spillover', `${f3(x.data.value)} pp`)}`
          const n = d.nodes.find((y) => y.node === x.data.id)
          return `${ttTitle(n?.name ?? x.data.id ?? '')}${ttRow(t.down, 'to others', `${f3(n?.to_others)}`)}${ttRow(t.series[1], 'from others', `${f3(n?.from_others)}`)}${ttRow(t.fg, 'net', `${f3(n?.net)}`)}`
        },
      },
      series: [
        {
          type: 'graph',
          layout: 'force',
          roam: true,
          draggable: true,
          force: { repulsion: 170, edgeLength: [50, 130], gravity: 0.16 },
          zoom: 0.9,
          label: {
            show: true,
            position: 'right',
            fontSize: 10,
            color: t.fg,
            formatter: '{b}',
          },
          edgeSymbol: ['none', 'arrow'],
          edgeSymbolSize: 7,
          data: g.nodes.map((n) => ({
            id: n.id,
            name: n.id,
            net: n.net,
            symbolSize: n.size,
            itemStyle: {
              color: n.role === 'transmitter' ? t.down : t.series[1],
              borderColor: t.surface,
              borderWidth: 1,
            },
          })),
          links: g.edges.map((e) => ({
            source: e.source,
            target: e.target,
            value: e.value,
            lineStyle: {
              width: 0.6 + 3.4 * (e.value / maxEdge),
              color: alpha(t.fg, 0.35),
              curveness: 0.15,
            },
          })),
          emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
        },
      ],
    }),
    [g, d.nodes, t, maxEdge],
  )
  const hist = useMemo<EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 8, bottom: 20, containLabel: true },
      tooltip: { trigger: 'axis', valueFormatter: (v) => `${f3(v)}%` },
      xAxis: {
        type: 'category',
        data: d.history.map((h) => h.date),
        axisLabel: { formatter: (v: string) => v.slice(0, 7) },
      },
      yAxis: { type: 'value', scale: true },
      series: [
        {
          name: 'total connectedness',
          type: 'line',
          data: d.history.map((h) => h.total),
          showSymbol: false,
          lineStyle: { color: t.series[0], width: 1.5 },
          itemStyle: { color: t.series[0] },
        },
      ],
    }),
    [d.history, t],
  )
  const sorted = [...d.nodes].sort((a, b) => (b.net ?? 0) - (a.net ?? 0))
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,1.6fr)_minmax(280px,1fr)]">
      <div>
        <EChart option={option} height={440} ariaLabel="Connectedness network of net transmitters and receivers" />
        <p className="text-2xs text-muted">
          Total connectedness {f3(d.total)}% on {fmtDate(d.date)} · window {d.window} days, horizon {d.horizon}, VAR({d.lags}) · {d.n_nodes} nodes · node size = |net| · drag to rearrange
        </p>
      </div>
      <div className="space-y-2">
        <div className="text-2xs font-semibold tracking-wide text-muted uppercase">Total connectedness history</div>
        <EChart option={hist} height={130} ariaLabel="Total connectedness over time" />
        <div className="max-h-[260px] overflow-y-auto">
          <table className="w-full text-xs" aria-label="Net transmitters and receivers">
            <thead>
              <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                <th className="px-2 py-1">Node</th>
                <th className="px-2 py-1 text-right">To</th>
                <th className="px-2 py-1 text-right">From</th>
                <th className="px-2 py-1 text-right">Net</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((n) => (
                <tr key={n.node} className="border-b border-border/60">
                  <td className="max-w-[140px] truncate px-2 py-0.5" title={n.series_id ?? ''}>
                    {n.name ?? n.node}
                  </td>
                  <td className="num px-2 py-0.5 text-right">{fin(n.to_others) ? n.to_others.toFixed(1) : DASH}</td>
                  <td className="num px-2 py-0.5 text-right">{fin(n.from_others) ? n.from_others.toFixed(1) : DASH}</td>
                  <td className={cn('num px-2 py-0.5 text-right font-semibold', fin(n.net) && (n.net >= 0 ? 'text-down' : 'text-info'))}>{fin(n.net) ? `${n.net > 0 ? '+' : ''}${n.net.toFixed(1)}` : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export function SpilloversPanel() {
  const [resp, setResp] = useState('ret')
  const [w, setW] = useState('contiguity')
  const q = useEmSpillovers(resp, w)
  return (
    <Panel
      title="Spatial spillovers"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <ModelBadge /> Effect of event attention on country responses: OLS / SLX / SAR / SEM / SDM with direct, indirect (spillover) and total effects
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <Select
            label="Response"
            value={resp}
            onChange={setResp}
            options={(q.data?.available.responses ?? ['ret', 'vol', 'macro_surprise']).map((r) => ({
              value: r,
              label: r === 'ret' ? 'Returns' : r === 'vol' ? 'Volatility' : 'Macro surprise',
            }))}
          />
          <Select
            label="Weights"
            value={w}
            onChange={setW}
            options={(q.data?.available.w_kinds ?? ['contiguity']).map((x) => ({
              value: x,
              label: `W: ${x.replace('_', ' ')}`,
            }))}
          />
        </div>
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-72" />}>
        {(d) => <SpillBody d={d} />}
      </QueryView>
    </Panel>
  )
}

function SpillBody({ d }: { d: EmSpillovers }) {
  const t = useChartTokens()
  const effects = ['direct', 'indirect', 'total'] as const
  const bars = useMemo<EChartsOption>(() => {
    const rows = [...d.transmitters.map((x) => ({ ...x, kind: 'transmit' })), ...d.receivers.map((x) => ({ ...x, kind: 'receive' }))]
    return {
      grid: { left: 8, right: 8, top: 24, bottom: 8, containLabel: true },
      legend: { top: 0, textStyle: { color: t.muted, fontSize: 10 } },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        valueFormatter: (v) => f3(v),
      },
      xAxis: { type: 'value' },
      yAxis: {
        type: 'category',
        inverse: true,
        data: rows.map((r) => `${r.name ?? r.iso2}${r.kind === 'receive' ? ' (recv)' : ''}`),
        axisLabel: { fontSize: 10, color: t.fg },
      },
      series: [
        {
          name: 'transmit',
          type: 'bar',
          data: rows.map((r) => r.transmit),
          itemStyle: { color: t.down },
          barMaxWidth: 8,
        },
        {
          name: 'receive',
          type: 'bar',
          data: rows.map((r) => r.receive),
          itemStyle: { color: t.series[1] },
          barMaxWidth: 8,
        },
      ],
    } as EChartsOption
  }, [d, t])
  return (
    <div className="grid gap-3 xl:grid-cols-2">
      <div>
        <table className="w-full text-xs" aria-label="Spillover model effects">
          <thead>
            <tr className="border-b border-border text-left text-2xs text-muted uppercase">
              <th className="px-2 py-1">Model</th>
              <th className="px-2 py-1 text-right">ρ / λ</th>
              {effects.map((e) => (
                <th key={e} className="px-2 py-1 text-right">
                  {e}
                </th>
              ))}
              <th className="px-2 py-1 text-right">AIC</th>
            </tr>
          </thead>
          <tbody>
            {d.models.map((m) => {
              const p = (k: string) => m.params.find((x) => x.effect === k)
              const sp = p('rho') ?? p('lambda')
              return (
                <tr key={m.model} className="border-b border-border/60">
                  <td className="px-2 py-1 font-medium text-fg uppercase">{m.model}</td>
                  <td className="num px-2 py-1 text-right">{sp ? `${f3(sp.estimate)} (p ${pv(sp.p_value)})` : DASH}</td>
                  {effects.map((e) => {
                    const x = p(e) ?? (e === 'direct' ? p('beta') : undefined)
                    const sig = fin(x?.p_value) && (x?.p_value as number) < 0.05
                    return (
                      <td key={e} className={cn('num px-2 py-1 text-right', sig ? 'font-semibold text-fg' : 'text-muted')} title={x ? `90/95% CI ${f3(x.ci_low)} to ${f3(x.ci_high)} · p ${pv(x.p_value)}` : undefined}>
                        {x ? f3(x.estimate) : DASH}
                        {sig ? '*' : ''}
                      </td>
                    )
                  })}
                  <td className="num px-2 py-1 text-right text-muted">{fin(m.aic) ? m.aic.toFixed(0) : DASH}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div className="mt-2 flex flex-wrap gap-2 text-2xs text-muted">
          {d.diagnostics.map((x) => (
            <Badge key={x.test} tone={fin(x.p_value) && x.p_value < 0.05 ? 'accent' : 'neutral'}>
              {x.test.replace('_', ' ')} {fin(x.statistic) ? x.statistic.toFixed(1) : DASH} (p {pv(x.p_value)})
            </Badge>
          ))}
        </div>
        <p className="mt-2 text-2xs text-muted">
          Shock {d.shock.replace('_', ' ')} → response {d.response} · {d.n_units} countries × {d.n_periods} {d.freq === 'W' ? 'weeks' : 'days'} ({d.window_start ? fmtDate(d.window_start) : DASH} → {d.window_end ? fmtDate(d.window_end) : DASH}) · W ={' '}
          {d.w_kind} ({d.w_source}). Indirect = spillover to neighbours; bold * = p &lt; 0.05. Hover for CIs.
        </p>
      </div>
      <div>
        <div className="mb-1 text-2xs font-semibold tracking-wide text-muted uppercase">Top transmitters and receivers (SDM impacts)</div>
        <EChart option={bars} height={Math.max(220, (d.transmitters.length + d.receivers.length) * 16 + 40)} ariaLabel="Spillover transmitters and receivers" />
      </div>
    </div>
  )
}

export function IrfPanel() {
  const [resp, setResp] = useState('ret')
  const [model, setModel] = useState('lp')
  const [scope, setScope] = useState('*')
  const q = useEmIrf(resp, model, scope)
  return (
    <Panel
      title="Impulse responses"
      subtitle={
        <span className="inline-flex flex-wrap items-center gap-1.5">
          <ModelBadge /> Response of returns / volatility to a 1-sd event-attention shock: own vs spatial (neighbours) components with 95% bands
        </span>
      }
      actions={
        <div className="flex items-center gap-2">
          <Segmented
            label="Response"
            size="xs"
            value={resp}
            onChange={setResp}
            options={[
              { value: 'ret', label: 'Returns' },
              { value: 'vol', label: 'Vol' },
            ]}
          />
          <Segmented
            label="Model"
            size="xs"
            value={model}
            onChange={setModel}
            options={[
              { value: 'lp', label: 'Local projection' },
              { value: 'svar', label: 'Spatial VAR' },
            ]}
          />
          <Select
            label="Scope"
            value={scope}
            onChange={setScope}
            options={(q.data?.scopes ?? ['*']).map((s) => ({
              value: s,
              label: s === '*' ? 'Pooled' : s,
            }))}
          />
        </div>
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(d) => <IrfBody d={d} />}
      </QueryView>
    </Panel>
  )
}

function IrfBody({ d }: { d: EmIrf }) {
  const t = useChartTokens()
  if (!d.components.length) return <p className="py-8 text-center text-xs text-muted">No impulse responses for this selection.</p>
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {d.components.map((c, i) => (
        <IrfChart key={c.component} c={c} color={t.series[i % t.series.length]} />
      ))}
      <p className="text-2xs text-muted md:col-span-2">
        {d.model === 'lp' ? 'Jordà local projections' : 'Spatial VAR'} · window {d.window_start ? fmtDate(d.window_start) : DASH} → {d.window_end ? fmtDate(d.window_end) : DASH} · response in % per 1-sd shock · W {d.w_kind ?? DASH}
      </p>
    </div>
  )
}

function IrfChart({ c, color }: { c: EmIrf['components'][number]; color: string }) {
  const t = useChartTokens()
  const option = useMemo<EChartsOption>(() => {
    const h = c.points.map((p) => String(p.h))
    const lo = c.points.map((p) => p.ci_low)
    const span = c.points.map((p) => (fin(p.ci_high) && fin(p.ci_low) ? p.ci_high - p.ci_low : null))
    return {
      grid: { left: 8, right: 8, top: 24, bottom: 20, containLabel: true },
      title: {
        text: `${c.component} component`,
        left: 0,
        top: 0,
        textStyle: { fontSize: 11, color: t.fg, fontWeight: 600 },
      },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const p = c.points[(ps as { dataIndex: number }[])[0].dataIndex]
          return `${ttTitle(`h = ${p.h}`)}${ttRow(color, 'response', `${f3(p.coef)}%`)}${ttRow(t.ref, '95% band', `${f3(p.ci_low)} to ${f3(p.ci_high)}`)}${ttRow(t.ref, 'p / n', `${pv(p.p_value)} / ${p.n_obs}`)}`
        },
      },
      xAxis: {
        type: 'category',
        data: h,
        name: 'horizon (days)',
        nameLocation: 'middle',
        nameGap: 22,
        boundaryGap: false,
      },
      yAxis: { type: 'value', scale: true },
      series: [
        {
          type: 'line',
          data: lo,
          stack: 'band',
          stackStrategy: 'all',
          lineStyle: { opacity: 0 },
          showSymbol: false,
          silent: true,
        },
        {
          type: 'line',
          data: span,
          stack: 'band',
          stackStrategy: 'all',
          lineStyle: { opacity: 0 },
          areaStyle: { color: alpha(color, 0.18) },
          showSymbol: false,
          silent: true,
        },
        {
          name: 'response',
          type: 'line',
          data: c.points.map((p) => p.coef),
          lineStyle: { color, width: 2 },
          itemStyle: { color },
          symbolSize: 4,
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: t.borderStrong, type: 'solid' },
            data: [{ yAxis: 0 }],
          },
        },
      ],
    } as EChartsOption
  }, [c, color, t])
  return <EChart option={option} height={220} ariaLabel={`Impulse response, ${c.component} component`} />
}
