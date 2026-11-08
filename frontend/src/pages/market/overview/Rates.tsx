import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { macroApi } from '@/api/macro/client'
import { useImpliedPaths } from '@/api/macro/hooks'
import { IMPLIED_LABEL, pricedText } from '@/api/macro/v34'
import type { YieldCurve, YieldCurvePoint } from '@/api/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { Sparkline } from '@/components/charts/Sparkline'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { PendingState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtBps, fmtDate, fmtDateShort, fmtNum } from '@/lib/format'
import { cn } from '@/lib/utils'
import { BpChip } from '../../macro/CentralBanks'
import { curveOptions, isoDaysAgo } from './helpers'

function Chip({ label, value, tip, tone }: { label: string; value: string; tip?: string; tone?: 'warn' }) {
  const body = (
    <span className={cn('inline-flex h-6 items-baseline gap-1 rounded-sm border px-1.5 py-0.5 text-xs', tone === 'warn' ? 'border-warn/40 bg-warn-soft text-warn' : 'border-border bg-surface-2')}>
      <span className="label-caps">{label}</span>
      <span className="num text-fg">{value}</span>
    </span>
  )
  return tip ? (
    <Tip content={tip}>
      <span tabIndex={0}>{body}</span>
    </Tip>
  ) : (
    body
  )
}

export function RatesPanel({ curves, defaultCountry, className }: { curves: YieldCurve[]; defaultCountry: string; className?: string }) {
  const opts = curveOptions(curves)
  const [cc, setCc] = useState<string | null>(null)
  const pick = cc && opts.some((o) => o.value === cc) ? cc : opts.some((o) => o.value === defaultCountry) ? defaultCountry : (opts[0]?.value ?? '')
  const curve = curves.find((c) => c.country === pick) ?? null
  return (
    <Panel
      title="Rates"
      subtitle={curve ? `${curve.label} · numeric tenor axis · current vs 1M and 1Y ago` : 'Sovereign curves'}
      className={className}
      actions={opts.length > 0 ? <Select label="Yield curve" value={pick} onChange={setCc} options={opts} className="w-52" /> : undefined}
    >
      {!curve ? <PendingState compact detail="No sovereign curve with at least two market tenors in this snapshot." /> : <CurveBody c={curve} />}
      <TwosTensHistory />
    </Panel>
  )
}

function CurveBody({ c }: { c: YieldCurve }) {
  const t = useChartTokens()
  const [scale, setScale] = useState<'log' | 'linear'>('log')
  const { current, oneMonthAgo, oneYearAgo, policyRate, label } = c
  const option = useMemo<EChartsOption>(() => {
    const mk = (name: string, pts: YieldCurvePoint[], color: string, dashed = false, width = 2) => ({
      name,
      type: 'line' as const,
      data: pts.filter((p) => Number.isFinite(p.yield)).map((p) => ({ value: [p.years, +(p.yield * 100).toFixed(3)], tenor: p.tenor })),
      lineStyle: { color, width, type: dashed ? ('dashed' as const) : ('solid' as const) },
      itemStyle: { color },
      symbol: 'circle',
      symbolSize: 5,
      showSymbol: true,
    })
    const cur = mk('Current', current, t.series[0])
    const policy = policyRate
    if (policy !== null && Number.isFinite(policy))
      Object.assign(cur, {
        markLine: {
          silent: true,
          symbol: 'none',
          lineStyle: { color: t.ref, type: 'dotted', width: 1 },
          label: { formatter: `Policy ${(policy * 100).toFixed(2)}%`, color: t.muted, fontSize: 10, position: 'insideStartTop' },
          data: [{ yAxis: +(policy * 100).toFixed(3) }],
        },
      })
    const minYears = Math.min(...current.map((p) => p.years), 0.25)
    return {
      grid: { left: 8, right: 16, top: 28, bottom: 8, containLabel: true },
      legend: { top: 0, right: 0 },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const x = p as { seriesName: string; data: { value: number[]; tenor: string }; color: string }
          return ttTitle(`${label} · ${x.data.tenor}`) + ttRow(x.color, x.seriesName, `${x.data.value[1].toFixed(2)}%`)
        },
      },
      xAxis: {
        type: scale === 'log' ? 'log' : 'value',
        logBase: 10,
        min: scale === 'log' ? Math.max(0.05, minYears * 0.8) : 0,
        max: Math.max(...current.map((p) => p.years), 10) * (scale === 'log' ? 1.1 : 1.02),
        axisLabel: { formatter: (v: number) => (v < 1 ? `${Math.round(v * 12)}m` : `${Number(v.toFixed(1))}y`) },
        splitLine: { show: false },
      },
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: (v: number) => `${v.toFixed(1)}%` } },
      series: [mk('1Y ago', oneYearAgo, t.ref, true, 1.5), mk('1M ago', oneMonthAgo, t.series[1], false, 1.5), cur],
    } as EChartsOption
  }, [current, oneMonthAgo, oneYearAgo, policyRate, label, t, scale])
  const mixed = c.asOf && c.asOfMin && c.asOf !== c.asOfMin
  return (
    <div>
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <Chip label="2s10s" value={fmtBps(c.spread2s10sBps, true)} />
        <Chip label="5s30s" value={fmtBps(c.spread5s30sBps, true)} />
        <Chip label="10Y 1M chg" value={fmtBps(c.change10y1mBps, true)} />
        <Chip label="Policy" value={c.policyRate !== null ? `${fmtNum(c.policyRate * 100, 2)}%` : DASH} tip="Policy / overnight rate: drawn as a dotted reference line, not as a curve tenor" />
        <Chip
          label="As of"
          value={c.asOf ? fmtDateShort(c.asOf) : DASH}
          tone={mixed ? 'warn' : undefined}
          tip={mixed ? `Tenors carry different dates: oldest ${fmtDate(c.asOfMin)}, newest ${fmtDate(c.asOf)}` : `Curve date ${fmtDate(c.asOf)}`}
        />
        {c.nMarketTenors < 3 && <Badge tone="warn">{c.nMarketTenors} market tenors only</Badge>}
        <Segmented size="xs" label="Tenor axis" value={scale} onChange={setScale} options={[{ value: 'log', label: 'log' }, { value: 'linear', label: 'linear' }]} className="ml-auto" />
      </div>
      <EChart option={option} height={210} ariaLabel={`${c.label} yield curve on a numeric tenor axis`} />
    </div>
  )
}

/** US 2s10s history (FRED T10Y2Y, official) over one year. */
function TwosTensHistory() {
  const from = isoDaysAgo(366)
  const q = useQuery({ queryKey: ['macro', 'series-from', 'fred:T10Y2Y', from], queryFn: () => macroApi.series('fred:T10Y2Y', { transform: 'level', from }), staleTime: 30 * 60_000, retry: false })
  const pts = (q.data?.points ?? []).filter((p): p is { date: string; value: number } => typeof p.value === 'number')
  if (!pts.length) return null
  const last = pts[pts.length - 1]
  return (
    <div className="mt-2 flex items-center gap-3 border-t border-border pt-2 text-xs">
      <span className="label-caps">US 2s10s · 1y</span>
      <Sparkline data={pts.map((p) => p.value)} width={180} height={24} tone="accent" ariaLabel="US 10-year minus 2-year Treasury spread, last year" />
      <span className="num text-fg">{fmtBps(last.value * 100, true)}</span>
      <span className="text-2xs text-faint">FRED T10Y2Y (official) · {fmtDateShort(last.date)}</span>
    </div>
  )
}

/* ── policy path (approximation from official curves) ── */

export function PolicyPathPanel({ className }: { className?: string }) {
  const q = useImpliedPaths()
  return (
    <Panel
      title="Policy rate vs priced 12m"
      subtitle={IMPLIED_LABEL}
      className={className}
      flush
      actions={
        <Link to="/macro/cb" className="text-xs text-accent-text hover:underline">
          Central banks →
        </Link>
      }
    >
      <QueryView query={q} skeleton={<Skeleton className="m-3 h-40" />}>
        {(d) => {
          const rows = d.banks.filter((b) => b.status !== 'not_available' && b.summary?.h12m)
          const missing = d.banks.filter((b) => b.status === 'not_available' || !b.summary?.h12m).map((b) => b.bank.toUpperCase())
          return (
            <>
              <table className="w-full text-sm" aria-label="Policy rates and 12-month implied change">
                <thead>
                  <tr className="text-2xs tracking-wide text-muted uppercase">
                    <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Bank</th>
                    <th scope="col" className="border-b border-border px-2 text-right font-semibold">Rate</th>
                    <th scope="col" className="border-b border-border px-2 text-right font-semibold">Next mtg</th>
                    <th scope="col" className="border-b border-border px-3 text-right font-semibold">Priced 12m</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((b) => (
                    <tr key={b.bank} className="hover:bg-surface-2">
                      <td className="h-7 border-b border-border px-3">
                        <Link to={`/macro/cb/${b.bank}`} className="font-medium text-fg hover:underline" title={b.name}>
                          {b.bank.toUpperCase()}
                        </Link>
                        {b.status === 'stale' && <Badge tone="warn" className="ml-1">stale</Badge>}
                      </td>
                      <td className="num border-b border-border px-2 text-right">{typeof b.policy_rate === 'number' ? `${fmtNum(b.policy_rate, 2)}%` : DASH}</td>
                      <td className="num border-b border-border px-2 text-right text-2xs text-muted">{b.next_meeting ? fmtDateShort(b.next_meeting) : DASH}</td>
                      <td className="border-b border-border px-3 text-right">
                        <BpChip bp={b.summary?.h12m?.change_bp} moves={b.summary?.h12m?.moves_priced} step={b.step_bp ?? 25} title={`${pricedText(b.summary?.h12m, b.step_bp ?? 25)} · curve as of ${fmtDate(b.as_of)}`} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="px-3 py-1.5 text-2xs text-muted">Rose = hikes priced, teal = cuts. No official curve: {missing.join(', ') || DASH}.</p>
            </>
          )
        }}
      </QueryView>
    </Panel>
  )
}
