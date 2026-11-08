import { useQueries } from '@tanstack/react-query'
import { useMemo } from 'react'
import { Link } from 'react-router'
import { crossAssetApi } from '@/api/crossasset/client'
import { useXaCockpit } from '@/api/crossasset/hooks'
import type { XaCockpit } from '@/api/crossasset/types'
import { macroApi } from '@/api/macro/client'
import type { CreditSpread, YieldCurve } from '@/api/types'
import { Delta } from '@/components/ui/data'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtMacro } from '../../macro/fmt'
import { isoDaysAgo } from './helpers'
import { TAPE_ASSETS, TAPE_GROUP_LABEL, TAPE_SERIES, type TapeGroup, type TapeItem, assetPoints, seriesChanges } from './tapeData'

const H = 30 * 60_000

/** 1D / 1M changes for the curated assets (cross-asset asset detail, ~50 days) and FRED series. */
function useTapeChanges() {
  const from = isoDaysAgo(50)
  const assets = useQueries({
    queries: TAPE_ASSETS.map((a) => ({
      queryKey: ['cross-asset', 'asset', a.id, from],
      queryFn: () => crossAssetApi.asset(a.id, from),
      staleTime: H,
      retry: false,
    })),
  })
  const series = useQueries({
    queries: TAPE_SERIES.map((s) => ({
      queryKey: ['macro', 'series-from', s.id, from],
      queryFn: () => macroApi.series(s.id, { transform: 'level', from }),
      staleTime: H,
      retry: false,
    })),
  })
  return { assets, series }
}

function cockpitRow(d: XaCockpit | undefined, id: string) {
  const row = d?.rows.find((r) => r.asset_id === id)
  if (!row || !d) return null
  const idx = (sid: string) => d.signals.findIndex((s) => s.id === sid)
  const cell = (sid: string) => {
    const i = idx(sid)
    return i >= 0 ? row.cells[i] : undefined
  }
  return { row, trend: (cell('trend')?.raw as number | null | undefined) ?? null, volZ: (cell('vol')?.z as number | null | undefined) ?? null }
}

export function CrossAssetTape({ curves, credit, className }: { curves: YieldCurve[]; credit: CreditSpread[]; className?: string }) {
  const cockpit = useXaCockpit()
  const { assets, series } = useTapeChanges()
  const items = useMemo<TapeItem[]>(() => {
    const out: TapeItem[] = []
    // rates: sovereign 10Y (and UST 2Y) from the overview's curves — level + calendar 1M change in bp
    const tenor = (cc: string, ten: string, label: string) => {
      const c = curves.find((x) => x.country === cc)
      if (!c) return
      const i = c.current.findIndex((p) => p.tenor === ten)
      if (i < 0) return
      const now = c.current[i].yield
      const m = c.oneMonthAgo.find((p) => p.tenor === ten)?.yield
      out.push({
        id: `${cc}-${ten}`,
        group: 'rates',
        label,
        level: now * 100,
        kind: 'yield',
        change1d: null,
        change1m: typeof m === 'number' && Number.isFinite(m) ? (now - m) * 1e4 : null,
        asOf: c.asOf,
        source: `${c.label} (official curve)`,
      })
    }
    tenor('US', '2Y', 'UST 2Y')
    tenor('US', '10Y', 'UST 10Y')
    tenor('DE', '10Y', 'Bund 10Y')
    tenor('GB', '10Y', 'Gilt 10Y')
    tenor('JP', '10Y', 'JGB 10Y')
    // FX, commodities, crypto: cross-asset cockpit (level, trend, vol z) + asset detail (1D / 1M)
    TAPE_ASSETS.forEach((a, k) => {
      const c = cockpitRow(cockpit.data, a.id)
      const det = assets[k]?.data
      const ch = seriesChanges(assetPoints(det?.dates, det?.series?.price), 'pct')
      const level = ch.last ?? (typeof c?.row.price === 'number' ? c.row.price : null)
      if (level === null && !c) return
      out.push({
        id: a.id,
        group: a.group,
        label: a.label,
        level,
        kind: 'price',
        change1d: ch.change1d,
        change1m: ch.change1m,
        asOf: ch.asOf ?? c?.row.date ?? null,
        source: c?.row.name ?? a.label,
        href: '/macro/cross-asset',
        trend: c?.trend ?? null,
        volZ: c?.volZ ?? null,
      })
    })
    // vol + broad dollar: FRED (official)
    TAPE_SERIES.forEach((s, k) => {
      const d = series[k]?.data
      if (!d) return
      const ch = seriesChanges(d.points, 'pct')
      if (ch.last === null) return
      out.push({ id: s.id, group: s.group, label: s.label, level: ch.last, kind: s.kind, change1d: ch.change1d, change1m: ch.change1m, asOf: ch.asOf, invert: s.invert, source: `${d.meta.title ?? s.id} (FRED, official)`, href: `/macro/series/${encodeURIComponent(s.id)}` })
    })
    // credit: corrected spreads (v1.29) — IG / HY OAS, euro HY, EM
    const want = credit.filter((c) => c.spreadType === 'oas').slice(0, 4)
    for (const c of want)
      out.push({
        id: c.id,
        group: 'credit',
        label: shortCredit(c),
        level: c.valueBps,
        kind: 'bps',
        change1d: Number.isFinite(c.change1dBps) ? c.change1dBps : null,
        change1m: Number.isFinite(c.change1mBps) ? c.change1mBps : null,
        asOf: c.asOf,
        invert: true,
        source: c.name,
      })
    return out
  }, [curves, credit, cockpit.data, assets, series])

  const groups: TapeGroup[] = ['rates', 'fx', 'commodities', 'vol', 'crypto', 'credit']
  const loading = cockpit.isLoading || assets.some((a) => a.isLoading)
  return (
    <Panel
      title="Cross-asset tape"
      subtitle="Level · 1D · 1M (calendar) · rates in bp · FX official fixings · futures front month (Yahoo) · VIX / broad USD from FRED"
      className={className}
      actions={
        <Link to="/macro/cross-asset" className="text-xs text-accent-text hover:underline">
          Cross-asset →
        </Link>
      }
    >
      <div className="grid grid-cols-2 gap-x-4 gap-y-3 md:grid-cols-3 2xl:grid-cols-6">
        {groups.map((g) => {
          const rows = items.filter((x) => x.group === g)
          return (
            <section key={g} aria-label={TAPE_GROUP_LABEL[g]} className="min-w-0">
              <div className="mb-1 grid grid-cols-[minmax(0,1fr)_auto_3.2rem_3.2rem] gap-x-1.5 px-0.5">
                <span className="label-caps">{TAPE_GROUP_LABEL[g]}</span>
                <span />
                <span className="text-right text-2xs text-faint">1D</span>
                <span className="text-right text-2xs text-faint">1M</span>
              </div>
              {rows.length === 0 ? (
                <div className="text-2xs text-faint">{loading ? 'Loading…' : g === 'credit' ? 'No curated spreads in this snapshot' : 'No data'}</div>
              ) : (
                <ul className="space-y-0.5">
                  {rows.map((x) => (
                    <TapeRow key={x.id} x={x} />
                  ))}
                </ul>
              )}
            </section>
          )
        })}
      </div>
    </Panel>
  )
}

function shortCredit(c: CreditSpread): string {
  const seg = (c.segment ?? '').toUpperCase()
  const reg = c.region === 'EU' ? 'EUR' : c.region === 'EM' ? '' : (c.region ?? '')
  return seg ? `${reg} ${seg} OAS`.trim() : c.name.replace(/ICE BofA /i, '').slice(0, 22)
}

function fmtLevel(x: TapeItem): string {
  if (x.level === null) return DASH
  if (x.kind === 'yield') return `${fmtNum(x.level, 2)}%`
  if (x.kind === 'bps') return `${fmtNum(x.level, 0)}bp`
  return fmtMacro(x.level, 5)
}

function TapeRow({ x }: { x: TapeItem }) {
  const bp = x.kind === 'yield' || x.kind === 'bps'
  const fmtCh = (v: number) => (bp ? `${v > 0 ? '+' : ''}${fmtNum(v, 0)}` : fmtPct(v, 1, true))
  const tip = (
    <div className="space-y-0.5">
      <div className="font-semibold">{x.source}</div>
      <div className="text-muted">as of {fmtDate(x.asOf)}</div>
      {typeof x.trend === 'number' && <div className="text-muted">Trend signal (model-based) {fmtNum(x.trend, 2, true)}</div>}
      {typeof x.volZ === 'number' && <div className="text-muted">Realised vol z (3y) {fmtNum(x.volZ, 1, true)}</div>}
      <div className="text-muted">1D = vs previous observation · 1M = vs the value 30 calendar days earlier{bp ? ' · in bp' : ''}</div>
    </div>
  )
  const body = (
    <span className="grid grid-cols-[minmax(0,1fr)_auto_3.2rem_3.2rem] items-baseline gap-x-1.5 text-xs">
      <span className="truncate text-muted">{x.label}</span>
      <span className="num text-right text-fg">{fmtLevel(x)}</span>
      <Delta value={x.change1d} format={fmtCh} invert={x.invert} showIcon={false} className="justify-end text-2xs" />
      <Delta value={x.change1m} format={fmtCh} invert={x.invert} showIcon={false} className="justify-end text-2xs" />
    </span>
  )
  return (
    <li>
      <Tip content={tip}>
        {x.href ? (
          <Link to={x.href} className={cn('block rounded-sm px-0.5 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-focus')}>
            {body}
          </Link>
        ) : (
          <span tabIndex={0} className="block rounded-sm px-0.5 hover:bg-surface-2">
            {body}
          </span>
        )}
      </Tip>
    </li>
  )
}
