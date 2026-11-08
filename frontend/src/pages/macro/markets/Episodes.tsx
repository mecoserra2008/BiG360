import { useMemo, useState } from 'react'
import { useEpisode, useEpisodes } from '@/api/macro/hooks'
import { type MxEpisode, type MxReplayRow, replayPoints } from '@/api/macro/v34'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtChange } from '../fmt'

type Block = 'sectors' | 'regions' | 'factors' | 'instruments' | 'securities'
const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v: unknown, d = 1) => (fin(v) ? `${fmtChange(v * 100, d)}%` : DASH)
const MOVE_UNIT: Record<string, string> = { d2y: 'pp', d10y: 'pp', dcurve: 'pp', dbe10: 'pp', dreal10: 'pp', dcredit: 'pp', usd: 'log', oil: 'log', equity: 'log' }
const MOVE_LABEL: Record<string, string> = { d2y: '2y yield', d10y: '10y yield', dcurve: '10y–2y curve', dbe10: '10y breakeven', dreal10: '10y real yield', dcredit: 'Credit spread', usd: 'USD', oil: 'Oil', equity: 'S&P 500' }

function fmtMove(k: string, v: number | null | undefined): string {
  if (!fin(v)) return 'n/a'
  return MOVE_UNIT[k] === 'log' ? `${fmtChange((Math.exp(v) - 1) * 100, 1)}%` : `${fmtChange(v * 100, 0)}bp`
}

export function Episodes() {
  const list = useEpisodes()
  const [id, setId] = useState<string | null>(null)
  const sel = id ?? list.data?.episodes.find((e) => e.episode_id === 'hiking_2022')?.episode_id ?? list.data?.episodes[0]?.episode_id ?? null
  const ep = useEpisode(sel)
  return (
    <div className="space-y-3">
      <Panel
        title="Episode replay"
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <Badge tone="info">model-based</Badge> {list.data?.method ?? 'Official factor moves over a historical window applied to current betas, vs realized total returns'}
          </span>
        }
        flush
      >
        <QueryView query={list} skeleton={<Skeleton className="m-3 h-24" />}>
          {(d) => (
            <div className="grid gap-2 p-3 sm:grid-cols-2 xl:grid-cols-3">
              {[...d.episodes]
                .sort((a, b) => b.start.localeCompare(a.start))
                .map((e) => (
                  <button
                    key={e.episode_id}
                    type="button"
                    onClick={() => setId(e.episode_id)}
                    className={cn('rounded-md border px-3 py-2 text-left text-xs transition-colors', sel === e.episode_id ? 'border-accent bg-accent/10' : 'border-border hover:bg-surface-2')}
                  >
                    <div className="font-semibold text-fg">{e.name}</div>
                    <div className="text-2xs text-muted">
                      {fmtDate(e.start)} → {fmtDate(e.end)} · {e.n_realized.toLocaleString()} with realized returns
                    </div>
                    <div className="mt-1 flex flex-wrap gap-x-2 text-2xs text-muted">
                      {['d10y', 'dcredit', 'equity', 'oil'].map((k) => (
                        <span key={k}>
                          {MOVE_LABEL[k]} <span className="num text-fg">{fmtMove(k, e.factor_moves[k])}</span>
                        </span>
                      ))}
                    </div>
                  </button>
                ))}
            </div>
          )}
        </QueryView>
      </Panel>
      {sel && (
        <QueryView query={ep} skeleton={<Skeleton className="h-[480px]" />}>
          {(d) => <EpisodeBody d={d} />}
        </QueryView>
      )}
    </div>
  )
}

function EpisodeBody({ d }: { d: MxEpisode }) {
  const t = useChartTokens()
  const [block, setBlock] = useState<Block>('sectors')
  const rows: MxReplayRow[] = useMemo(() => (block === 'securities' ? [...d.securities.winners, ...d.securities.losers] : d[block].items), [d, block])
  const stats = block === 'securities' ? d.securities.stats : d[block].stats
  const pts = useMemo(() => replayPoints(rows), [rows])
  const option = useMemo<EChartsOption>(() => {
    const all = pts.flatMap((p) => [p.model, p.realized])
    const lo = Math.min(-0.05, ...all)
    const hi = Math.max(0.05, ...all)
    const pad = (hi - lo) * 0.06
    const showLabels = pts.length <= 30
    return {
      grid: { left: 8, right: 16, top: 12, bottom: 30, containLabel: true },
      tooltip: {
        trigger: 'item',
        formatter: (p: unknown) => {
          const q = pts[(p as { dataIndex: number }).dataIndex]
          return q ? `${ttTitle(q.label)}${ttRow(t.series[0], 'model', pc(q.model))}${ttRow(t.series[1], 'realized', pc(q.realized))}` : ''
        },
      },
      xAxis: { type: 'value', name: 'model-implied', nameLocation: 'middle', nameGap: 22, min: lo - pad, max: hi + pad, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: t.grid } } },
      yAxis: { type: 'value', name: 'realized', min: lo - pad, max: hi + pad, axisLabel: { formatter: (v: number) => `${(v * 100).toFixed(0)}%` }, splitLine: { lineStyle: { color: t.grid } } },
      series: [
        {
          type: 'scatter',
          symbolSize: block === 'instruments' || block === 'securities' ? 6 : 9,
          itemStyle: { color: t.series[0], opacity: 0.85 },
          label: { show: showLabels, position: 'right', fontSize: 9, color: t.muted, formatter: (p: unknown) => pts[(p as { dataIndex: number }).dataIndex]?.label ?? '' },
          data: pts.map((p) => [p.model, p.realized]),
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: t.borderStrong, type: 'dashed', width: 1 },
            data: [[{ coord: [lo - pad, lo - pad] }, { coord: [hi + pad, hi + pad] }]],
          },
        },
      ],
    } as EChartsOption
  }, [pts, t, block])
  return (
    <>
      <Panel title={d.episode.name} subtitle={`${fmtDate(d.episode.start)} → ${fmtDate(d.episode.end)} · ${d.episode.rationale}`}>
        <div className="grid grid-cols-3 gap-2 text-xs sm:grid-cols-5 xl:grid-cols-9">
          {Object.keys(MOVE_LABEL).map((k) => (
            <div key={k} className="rounded-md border border-border px-2 py-1">
              <div className="truncate text-2xs text-muted">{MOVE_LABEL[k]}</div>
              <div className={cn('num font-semibold', fin(d.factor_moves[k]) ? 'text-fg' : 'text-faint')}>{fmtMove(k, d.factor_moves[k])}</div>
            </div>
          ))}
        </div>
        <p className="mt-2 text-2xs text-muted">
          Official level changes over the window (FRED / H.15) applied to the current joint betas (as of {d.as_of_betas ? fmtDate(d.as_of_betas) : DASH}). {d.model_label}.
          {d.missing_factors.length > 0 && ` Not available for this window: ${d.missing_factors.map((m) => MOVE_LABEL[m] ?? m).join(', ')} (treated as 0).`}
        </p>
      </Panel>
      <Panel
        title="Model vs realized"
        subtitle="Dashed line = perfect fit"
        actions={
          <Segmented
            label="Block"
            size="xs"
            value={block}
            onChange={setBlock}
            options={[
              { value: 'sectors', label: 'Sectors' },
              { value: 'regions', label: 'Regions' },
              { value: 'factors', label: 'Factors' },
              { value: 'instruments', label: 'Instruments' },
              { value: 'securities', label: 'Securities' },
            ]}
          />
        }
      >
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Stat label="Correlation" value={<span className="num">{fin(stats.corr) ? stats.corr.toFixed(2) : DASH}</span>} sub={`n ${stats.n}`} />
          <Stat label="Sign hit rate" value={<span className="num">{fin(stats.sign_hit_rate) ? `${(stats.sign_hit_rate * 100).toFixed(0)}%` : DASH}</span>} />
          <Stat label="Mean abs. error" value={<span className="num">{fin(stats.mae) ? `${(stats.mae * 100).toFixed(1)}pp` : DASH}</span>} />
          <Stat label="Mean model" value={<span className="num">{pc(stats.mean_model)}</span>} />
          <Stat label="Mean realized" value={<span className="num">{pc(stats.mean_realized)}</span>} />
        </div>
        {block === 'securities' && <p className="mt-1 text-2xs text-muted">Stats cover all securities with betas and realized returns; the chart and table show the 20 best and worst by model.</p>}
        <div className="mt-3 grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          {pts.length > 1 ? <EChart option={option} height={360} ariaLabel={`Model-implied vs realized returns, ${block}`} /> : <p className="py-10 text-center text-xs text-muted">No realized returns available for this block and window.</p>}
          <div className="max-h-[360px] overflow-auto">
            <table className="w-full text-xs" aria-label="Replay rows">
              <thead>
                <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                  <th className="sticky top-0 bg-surface px-2 py-1">Name</th>
                  <th className="sticky top-0 bg-surface px-2 py-1 text-right">Model</th>
                  <th className="sticky top-0 bg-surface px-2 py-1 text-right">90% band</th>
                  <th className="sticky top-0 bg-surface px-2 py-1 text-right">Realized</th>
                  <th className="sticky top-0 bg-surface px-2 py-1 text-right">Error</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r, i) => {
                  const err = fin(r.model_implied) && fin(r.realized) ? r.realized - r.model_implied : null
                  const hit = fin(r.model_implied) && fin(r.realized) ? Math.sign(r.model_implied) === Math.sign(r.realized) : null
                  return (
                    <tr key={`${r.entity_id}-${i}`} className="border-b border-border/60">
                      <td className="max-w-[220px] truncate px-2 py-0.5 text-fg" title={r.name}>
                        <span className="font-medium">{r.ticker && r.ticker !== r.name ? `${r.ticker} ` : ''}</span>
                        <span className="text-muted">{r.name}</span>
                      </td>
                      <td className={cn('num px-2 py-0.5 text-right', fin(r.model_implied) && (r.model_implied >= 0 ? 'text-up' : 'text-down'))}>{pc(r.model_implied)}</td>
                      <td className="num px-2 py-0.5 text-right text-2xs text-muted">{fin(r.model_lo) ? `${pc(r.model_lo, 0)} to ${pc(r.model_hi, 0)}` : DASH}</td>
                      <td className={cn('num px-2 py-0.5 text-right', fin(r.realized) && (r.realized >= 0 ? 'text-up' : 'text-down'))}>{pc(r.realized)}</td>
                      <td className="num px-2 py-0.5 text-right text-muted">
                        {err === null ? DASH : `${fmtChange(err * 100, 1)}pp`} {hit === null ? '' : hit ? '✓' : '✗'}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      </Panel>
    </>
  )
}
