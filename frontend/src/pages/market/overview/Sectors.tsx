import { useMemo, useState } from 'react'
import { winsorBound } from '@/api/adapters/market'
import type { IndexQuote, SectorPerformance } from '@/api/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { type ChartTokens, divergingColors, useChartTokens } from '@/components/charts/tokens'
import { PendingState } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { fmtCompactCurrency, fmtDateShort, fmtInt } from '@/lib/format'
import { REGION_LABEL } from '@/lib/regions'
import { type Horizon, IDX_FIELD, type SectorBasis, type SectorCell, sectorCells } from './helpers'

function heatmapOption(cells: SectorCell[], perf: SectorPerformance[], h: Horizon, basis: SectorBasis, t: ChartTokens): EChartsOption {
  const sectors = [...new Set(perf.map((p) => p.sector))]
  const regions = [...new Set(perf.map((p) => p.region))]
  const regionDate = (r: string) => {
    const row = perf.find((p) => p.region === r)
    const ds = Object.values(row?.asOfByRegion ?? {})
    return ds.length ? ds.sort()[ds.length - 1] : null
  }
  const data = cells.map((c) => ({ value: [regions.indexOf(c.region), sectors.indexOf(c.sector), +c.value.toFixed(2)], cell: c }))
  // F19: winsorised colour scale (95th percentile of |x|) — outliers keep their label but no longer wash out the rest
  const max = winsorBound(cells.map((c) => c.value), 0.95, h === 'return1d' ? 0.5 : 2)
  return {
    grid: { left: 8, right: 8, top: 8, bottom: 44, containLabel: true },
    tooltip: {
      formatter: (p: unknown) => {
        const c = (p as { data: { cell: SectorCell } }).data.cell
        const r = c.row
        const ds = Object.entries(r.asOfByRegion)
        const n1d = h === 'return1d' && r.nRet1d !== null ? ` (${fmtInt(r.nRet1d)} on the session)` : ''
        return (
          ttTitle(`${c.sector} · ${REGION_LABEL[c.region] ?? c.region}`) +
          ttRow(t.fg, basis === 'relative' ? 'Sector − index' : 'Return (cap-wtd)', `${c.value > 0 ? '+' : ''}${c.value.toFixed(2)}%`) +
          (basis === 'relative' ? ttRow(t.ref, 'Sector return', `${c.raw > 0 ? '+' : ''}${c.raw.toFixed(2)}%`) + ttRow(t.ref, `Index ${c.bench?.name ?? ''}`, c.bench ? `${(c.bench[IDX_FIELD[h]] * 100).toFixed(2)}%` : 'n/a') : '') +
          ttRow(t.ref, 'Constituents', `${fmtInt(r.nSecurities)}${n1d}`) +
          (r.marketCapUsd !== null ? ttRow(t.ref, 'Market cap', fmtCompactCurrency(r.marketCapUsd)) : '') +
          (ds.length ? ttRow(t.ref, 'Data as of', ds.map(([k, v]) => `${k} ${fmtDateShort(v)}`).join(' · ')) : '') +
          (basis === 'relative' && h === 'return1d' && c.bench?.asOf && ds.length && !ds.some(([, v]) => v === c.bench?.asOf) ? ttRow(t.warn, 'Note', `index bar ${fmtDateShort(c.bench.asOf)}`) : '')
        )
      },
    },
    xAxis: {
      type: 'category',
      data: regions.map((r) => {
        const d = regionDate(r)
        return `${REGION_LABEL[r] ?? r}${d ? `\n${fmtDateShort(d)}` : ''}`
      }),
      splitArea: { show: false },
      axisLine: { show: false },
      position: 'top',
      axisLabel: { lineHeight: 13 },
    },
    yAxis: { type: 'category', data: sectors, inverse: true, axisLine: { show: false }, axisLabel: { width: 150, overflow: 'truncate' } },
    visualMap: {
      min: -max,
      max,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      itemHeight: 140,
      itemWidth: 10,
      inRange: { color: divergingColors(t) },
      text: [`≥ +${max.toFixed(1)}%`, `≤ −${max.toFixed(1)}%`],
      formatter: (v: unknown) => `${Number(v).toFixed(1)}%`,
    },
    series: [
      {
        type: 'heatmap',
        data,
        label: {
          show: true,
          color: t.fg,
          fontSize: 10,
          fontFamily: t.fontMono,
          formatter: (p: unknown) => {
            const v = (p as { value: number[] }).value[2]
            return `${v > 0 ? '+' : ''}${v.toFixed(1)}`
          },
        },
        itemStyle: { borderColor: t.surface, borderWidth: 2 },
        emphasis: { itemStyle: { borderColor: t.fg, borderWidth: 1 } },
      },
    ],
  }
}

function treemapOption(perf: SectorPerformance[], h: Horizon, t: ChartTokens): EChartsOption {
  const bySector = new Map<string, { mcap: number; ret: number; n: number }>()
  for (const p of perf) {
    if (p.marketCapUsd === null || !Number.isFinite(p[h])) continue
    const cur = bySector.get(p.sector) ?? { mcap: 0, ret: 0, n: 0 }
    bySector.set(p.sector, { mcap: cur.mcap + p.marketCapUsd, ret: cur.ret + p[h] * p.marketCapUsd, n: cur.n + (p.nSecurities ?? 0) })
  }
  const data = [...bySector.entries()].map(([name, v]) => ({ name, value: [v.mcap, +((v.ret / v.mcap) * 100).toFixed(2), v.n] }))
  const max = winsorBound(data.map((x) => x.value[1]), 0.95, h === 'return1d' ? 0.5 : 2)
  return {
    tooltip: {
      formatter: (p: unknown) => {
        const x = p as { name: string; value: number[] }
        return ttTitle(x.name) + ttRow(t.fg, 'Return (cap-wtd)', `${x.value[1] > 0 ? '+' : ''}${x.value[1].toFixed(2)}%`) + ttRow(t.fg, 'Market cap', fmtCompactCurrency(x.value[0])) + ttRow(t.ref, 'Constituents', fmtInt(x.value[2]))
      },
    },
    visualMap: { show: false, dimension: 1, min: -max, max, inRange: { color: divergingColors(t) } },
    series: [
      {
        type: 'treemap',
        data,
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        width: '100%',
        height: '100%',
        itemStyle: { borderColor: t.surface, borderWidth: 2, gapWidth: 2 },
        label: {
          show: true,
          color: t.fg,
          fontSize: 11,
          formatter: (p: unknown) => {
            const x = p as { name: string; value: number[] }
            return `${x.name}\n${x.value[1] > 0 ? '+' : ''}${x.value[1].toFixed(1)}%`
          },
        },
      },
    ],
  }
}

export function SectorPanel({ perf, indices, className }: { perf: SectorPerformance[]; indices: IndexQuote[]; className?: string }) {
  const t = useChartTokens()
  const [h, setH] = useState<Horizon>('return1m')
  const [view, setView] = useState<'heatmap' | 'treemap'>('heatmap')
  const [basis, setBasis] = useState<SectorBasis>('total')
  const cells = useMemo(() => sectorCells(perf, h, basis, indices), [perf, h, basis, indices])
  const option = useMemo(() => (view === 'heatmap' ? heatmapOption(cells, perf, h, basis, t) : treemapOption(perf, h, t)), [cells, perf, h, basis, t, view])
  const noCap = [...new Set(perf.filter((p) => p.marketCapUsd === null).map((p) => `${p.sector} (${REGION_LABEL[p.region] ?? p.region})`))]
  const regions = new Set(perf.map((p) => p.region)).size
  const sectors = new Set(perf.map((p) => p.sector)).size
  return (
    <Panel
      title="Sector performance"
      subtitle={
        view === 'heatmap'
          ? `${basis === 'relative' ? 'Sector minus region benchmark' : 'Cap-weighted total return'} · colour clipped at the 95th percentile · hover for constituents and dates`
          : 'Area = market cap · colour = cap-weighted return (clipped at the 95th percentile)'
      }
      className={className}
      actions={
        <>
          <Segmented size="xs" label="View" value={view} onChange={setView} options={[{ value: 'heatmap', label: 'Heatmap' }, { value: 'treemap', label: 'Treemap' }]} />
          {view === 'heatmap' && <Segmented size="xs" label="Basis" value={basis} onChange={setBasis} options={[{ value: 'total', label: 'Total' }, { value: 'relative', label: 'vs index', title: 'Sector return minus the region benchmark index' }]} />}
          <Segmented size="xs" label="Horizon" value={h} onChange={setH} options={[{ value: 'return1d', label: '1D' }, { value: 'return1m', label: '1M' }, { value: 'returnYtd', label: 'YTD' }]} />
        </>
      }
    >
      {perf.length ? (
        <>
          <EChart option={option} height={view === 'heatmap' ? Math.max(260, sectors * 30 + 70) : 380} ariaLabel="Sector performance by region" />
          {view === 'treemap' && noCap.length > 0 && <p className="mt-1 text-2xs text-muted">Not shown (market cap unknown): {noCap.join(', ')}.</p>}
          {view === 'heatmap' && regions === 1 && <p className="mt-1 text-2xs text-muted">Single region: switch to “vs index” to see what the region’s benchmark did not.</p>}
        </>
      ) : (
        <PendingState detail="Sector performance not published yet." />
      )}
    </Panel>
  )
}
