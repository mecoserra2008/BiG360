import { FlaskConical } from 'lucide-react'
import { useMemo, useState } from 'react'
import { api } from '@/api/client'
import { useEmStudies, useEmStudyPath, useEvEventDates } from '@/api/events/hooks'
import { type EmStudyCell, type EvEventDates, type EvLabelType, type EvTaxonomy, carPath, studyKey } from '@/api/events/types'
import { EChart, type EChartsOption, ttRow, ttTitle } from '@/components/charts/EChart'
import { alpha, useChartTokens } from '@/components/charts/tokens'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { ModelBadge } from './shared'
import { useSendEventStudy } from './useSendToStudio'

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v: unknown, d = 2) => (fin(v) ? `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%` : DASH)
const WINDOWS = ['-1,5', '0,1', '0,5', '0,10']
const LEVEL_LABEL: Record<string, string> = {
  country_index: 'Country index',
  sector_etf: 'Sector ETF',
  issuer: 'Issuers',
}

export function StudiesPanel({ tax }: { tax: EvTaxonomy }) {
  const [theme, setTheme] = useState('')
  const [iso2, setIso2] = useState('')
  const [sector, setSector] = useState('')
  const [level, setLevel] = useState('')
  const [kind, setKind] = useState('attention')
  const [win, setWin] = useState('0,5')
  const [sel, setSel] = useState<EmStudyCell | null>(null)
  const q = useEmStudies({
    theme,
    iso2,
    sector,
    level,
    shockKind: kind,
    window: win,
  })
  const themeName = (id: string) => (id === 'all' ? 'All themes' : (tax.themes.find((x) => x.id === id)?.name ?? id))
  return (
    <div className="space-y-3">
      <Panel
        title="Event-study library"
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <ModelBadge /> Cumulative abnormal returns (market model) after labelled shocks, by theme × country × sector · click a row for its CAR path
          </span>
        }
        flush
      >
        <div className="border-b border-border px-3 py-2">
          <div className="flex flex-wrap items-center gap-1.5">
            <Select label="Theme" value={theme} onChange={setTheme} options={[{ value: '', label: 'Any theme' }, { value: 'all', label: 'All-theme shocks' }, ...tax.themes.map((t) => ({ value: t.id, label: t.name }))]} className="w-40" />
            <Select
              label="Country"
              value={iso2}
              onChange={setIso2}
              options={[{ value: '', label: 'Any country' }, { value: 'ALL', label: 'Pooled (ALL)' }, ...tax.countries.filter((c) => c.id.length === 2).map((c) => ({ value: c.id, label: c.name }))]}
              className="w-36"
            />
            <Select label="Sector" value={sector} onChange={setSector} options={[{ value: '', label: 'Any sector' }, { value: 'ALL', label: 'Pooled (ALL)' }, ...tax.sectors.map((s) => ({ value: s.id, label: s.name }))]} className="w-36" />
            <Select
              label="Level"
              value={level}
              onChange={setLevel}
              options={[
                { value: '', label: 'Any level' },
                ...Object.entries(LEVEL_LABEL).map(([value, label]) => ({
                  value,
                  label,
                })),
              ]}
              className="w-32"
            />
            <Segmented
              label="Shock kind"
              size="xs"
              value={kind}
              onChange={setKind}
              options={[
                { value: 'attention', label: 'Attention' },
                { value: 'neg_tone', label: 'Negative tone' },
              ]}
            />
            <Select label="Window" value={win} onChange={setWin} options={WINDOWS.map((w) => ({ value: w, label: `[${w}] days` }))} className="w-28" />
          </div>
        </div>
        <QueryView query={q} skeleton={<Skeleton className="m-3 h-72" />}>
          {(d) =>
            d.items.length === 0 ? (
              <EmptyState title="No cells" message="No event-study cells with at least 3 events for these filters." />
            ) : (
              <div className="max-h-[440px] overflow-auto">
                <table className="w-full text-xs" aria-label="Event-study cells">
                  <thead>
                    <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
                      <th className="sticky top-0 bg-surface px-3 py-1">Theme</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">Country</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">Sector</th>
                      <th className="sticky top-0 bg-surface px-2 py-1">Level</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">n</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">Mean CAR</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">95% CI</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">t</th>
                      <th className="sticky top-0 bg-surface px-2 py-1 text-right">p</th>
                      <th className="sticky top-0 bg-surface px-3 py-1 text-right">% negative</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.items.map((c, i) => (
                      <tr key={`${studyKey(c)}-${i}`} className={cn('cursor-pointer border-b border-border/60 hover:bg-surface-2', sel && studyKey(sel) === studyKey(c) && 'bg-accent/10')} onClick={() => setSel(c)}>
                        <td className="px-3 py-0.5 text-fg">{themeName(c.theme)}</td>
                        <td className="px-2 py-0.5">{c.iso2}</td>
                        <td className="px-2 py-0.5">{c.sector}</td>
                        <td className="px-2 py-0.5 text-muted">{LEVEL_LABEL[c.level] ?? c.level}</td>
                        <td className="num px-2 py-0.5 text-right">{c.n_events}</td>
                        <td className={cn('num px-2 py-0.5 text-right font-semibold', fin(c.mean_car) && (c.mean_car >= 0 ? 'text-up' : 'text-down'))}>{pc(c.mean_car)}</td>
                        <td className="num px-2 py-0.5 text-right text-muted">
                          {pc(c.ci_low, 1)} to {pc(c.ci_high, 1)}
                        </td>
                        <td className="num px-2 py-0.5 text-right">{fin(c.t_stat) ? c.t_stat.toFixed(2) : DASH}</td>
                        <td className="num px-2 py-0.5 text-right">{fin(c.p_value) ? (c.p_value < 0.001 ? '<0.001' : c.p_value.toFixed(3)) : DASH}</td>
                        <td className="num px-3 py-0.5 text-right">{fin(c.share_negative) ? `${(c.share_negative * 100).toFixed(0)}%` : DASH}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          }
        </QueryView>
        <p className="border-t border-border px-3 py-2 text-2xs text-muted">
          CAR in % vs a market model (country index, ACWI fallback). <span className="font-semibold text-fg">Clustered events → t-stats optimistic</span>: shocks on nearby dates share market moves, so the cross-sectional standard errors understate
          uncertainty. Sorted by p-value.
        </p>
      </Panel>
      {sel && <CarPathPanel cell={sel} themeName={themeName} />}
      <SendPanel tax={tax} />
    </div>
  )
}

function CarPathPanel({ cell, themeName }: { cell: EmStudyCell; themeName: (id: string) => string }) {
  const t = useChartTokens()
  const key = {
    theme: cell.theme,
    iso2: cell.iso2,
    sector: cell.sector,
    level: cell.level,
    shockKind: cell.shock_kind,
  }
  const qs = useEmStudyPath(key, WINDOWS)
  const cells = useMemo(() => qs.flatMap((q) => (q.data?.items ?? []).filter((c) => studyKey(c) === studyKey(cell))), [qs, cell])
  const path = useMemo(() => carPath(cells), [cells])
  const loading = qs.some((q) => q.isLoading)
  const option = useMemo<EChartsOption>(() => {
    const pts = [
      {
        window: '0,0',
        end: 0,
        car: 0,
        lo: 0,
        hi: 0,
        n: 0,
        p: null as number | null,
      },
      ...path.filter((p) => !p.window.startsWith('-')),
    ]
    const pre = path.find((p) => p.window.startsWith('-'))
    return {
      grid: { left: 8, right: 16, top: 16, bottom: 28, containLabel: true },
      tooltip: {
        trigger: 'axis',
        formatter: (ps: unknown) => {
          const p = pts[(ps as { dataIndex: number }[])[0].dataIndex]
          return `${ttTitle(`day ${p.end} (window ${p.window})`)}${ttRow(t.series[0], 'mean CAR', pc(p.car))}${ttRow(t.ref, '95% CI', `${pc(p.lo)} to ${pc(p.hi)}`)}${ttRow(t.ref, 'n events', String(p.n))}`
        },
      },
      xAxis: {
        type: 'value',
        name: 'trading days after the shock',
        nameLocation: 'middle',
        nameGap: 20,
        min: 0,
        max: 10,
      },
      yAxis: {
        type: 'value',
        scale: true,
        axisLabel: { formatter: (v: number) => `${v.toFixed(1)}%` },
      },
      series: [
        {
          type: 'line',
          data: pts.map((p) => [p.end, p.lo]),
          stack: 'ci',
          stackStrategy: 'all',
          lineStyle: { opacity: 0 },
          showSymbol: false,
          silent: true,
        },
        {
          type: 'line',
          data: pts.map((p) => [p.end, fin(p.hi) && fin(p.lo) ? p.hi - p.lo : null]),
          stack: 'ci',
          stackStrategy: 'all',
          lineStyle: { opacity: 0 },
          areaStyle: { color: alpha(t.series[0], 0.18) },
          showSymbol: false,
          silent: true,
        },
        {
          name: 'mean CAR',
          type: 'line',
          data: pts.map((p) => [p.end, p.car]),
          showSymbol: true,
          symbolSize: 6,
          lineStyle: { color: t.series[0], width: 2 },
          itemStyle: { color: t.series[0] },
          label: {
            show: true,
            position: 'top',
            fontSize: 9,
            color: t.muted,
            formatter: (p: unknown) => {
              const i = (p as { dataIndex: number }).dataIndex
              return i === 0 ? '' : `n ${pts[i].n}`
            },
          },
          markLine: {
            silent: true,
            symbol: 'none',
            label: { show: false },
            lineStyle: { color: t.borderStrong, type: 'solid' },
            data: [{ yAxis: 0 }],
          },
        },
        ...(pre && fin(pre.car)
          ? [
              {
                name: 'window −1..5',
                type: 'scatter' as const,
                data: [[5, pre.car]],
                symbol: 'diamond',
                symbolSize: 9,
                itemStyle: { color: t.series[1] },
              },
            ]
          : []),
      ],
    } as EChartsOption
  }, [path, t])
  return (
    <Panel
      title={`CAR path · ${themeName(cell.theme)} · ${cell.iso2} · ${cell.sector} · ${LEVEL_LABEL[cell.level] ?? cell.level}`}
      subtitle={`${cell.shock_kind === 'attention' ? 'Attention' : 'Negative-tone'} shocks · ${cell.first_event ? fmtDate(cell.first_event) : DASH} → ${cell.last_event ? fmtDate(cell.last_event) : DASH} · benchmark ${cell.benchmark ?? DASH}`}
    >
      {loading ? (
        <Skeleton className="h-60" />
      ) : path.length === 0 ? (
        <p className="py-6 text-center text-xs text-muted">No windows for this cell.</p>
      ) : (
        <>
          <EChart option={option} height={240} ariaLabel="Cumulative abnormal return path with 95% band" />
          <p className="text-2xs text-muted">
            The library stores CARs for windows [0,1], [0,5], [0,10] (and [−1,5], diamond); the path joins them, so it is not a daily path. Band = 95% CI; labels = number of events. Clustered events → t-stats optimistic.
          </p>
        </>
      )}
    </Panel>
  )
}

/** Event dates for a label → prefilled event_car study in Model Studio. */
function SendPanel({ tax }: { tax: EvTaxonomy }) {
  const [lt, setLt] = useState<EvLabelType>('country')
  const [id, setId] = useState('US')
  const [theme, setTheme] = useState('all')
  const [kind, setKind] = useState('any')
  const label = id.trim().toUpperCase()
  const q = useEvEventDates(lt, label || null, theme, kind)
  const send = useSendEventStudy()
  // issuer studies use the company's sector peers as the equal-weight market (guide 08: the sector was never passed)
  const sendWithSector = async (d: EvEventDates) => {
    let sectorId: string | null = null
    if (lt === 'issuer') {
      const sid = d.label.security?.security_id
      const tk = (d.label.security?.ticker ?? label).toUpperCase()
      const hits = await api.search(tk).catch(() => [])
      sectorId = (hits.find((h) => h.id === sid) ?? hits.find((h) => h.ticker.toUpperCase() === tk) ?? null)?.sectorId ?? null
    }
    send(d, { sectorId })
  }
  return (
    <Panel title="Send event dates to Model Studio" subtitle="Shock dates for a country / sector / company (attention or tone z ≥ 2, de-clustered) → a prefilled event-study (event_car) draft">
      <div className="mb-2">
        <div className="flex flex-wrap items-center gap-1.5">
          <Segmented
            label="Label type"
            size="xs"
            value={lt}
            onChange={(v) => {
              setLt(v)
              setId(v === 'country' ? 'US' : v === 'sector' ? 'TECH' : 'AAPL')
            }}
            options={[
              { value: 'country', label: 'Country' },
              { value: 'sector', label: 'Sector' },
              { value: 'issuer', label: 'Company' },
            ]}
          />
          {lt === 'sector' ? (
            <Select label="Sector" value={id} onChange={setId} options={tax.sectors.map((s) => ({ value: s.id, label: s.name }))} className="w-36" />
          ) : (
            <Input aria-label={lt === 'country' ? 'Country ISO2' : 'Security id'} value={id} onChange={(e) => setId(e.target.value)} className="w-28" placeholder={lt === 'country' ? 'ISO2' : 'security id'} />
          )}
          <Select label="Theme" value={theme} onChange={setTheme} options={[{ value: 'all', label: 'All themes' }, ...tax.themes.map((t) => ({ value: t.id, label: t.name }))]} className="w-40" />
          <Select
            label="Shock kind"
            value={kind}
            onChange={setKind}
            options={[
              { value: 'any', label: 'Any shock' },
              { value: 'attention', label: 'Attention' },
              { value: 'tone_drop', label: 'Tone drop' },
              { value: 'tone_jump', label: 'Tone jump' },
            ]}
            className="w-28"
          />
        </div>
      </div>
      <QueryView query={q} skeleton={<Skeleton className="h-20" />}>
        {(d) => (
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <span className="text-fg">
              <span className="font-semibold">{d.dates.length}</span> event dates for {d.label.name} ({fmtDate(d.start)} → {fmtDate(d.end)})
            </span>
            <div className="flex flex-wrap gap-1">
              {d.dates.slice(0, 12).map((x) => (
                <Badge key={x.date}>{x.date}</Badge>
              ))}
              {d.dates.length > 12 && <Badge>+{d.dates.length - 12}</Badge>}
            </div>
            <Button variant="primary" size="sm" className="ml-auto" disabled={d.dates.length === 0} onClick={() => void sendWithSector(d)} title={d.dates.length === 0 ? 'No event dates for this label' : undefined}>
              <FlaskConical /> Send to Model Studio
            </Button>
            <p className="w-full text-2xs text-muted">
              {d.method}. {d.dates.length === 0 && 'No dates yet: the z baseline needs 90 days of history (data from 2026-07-14), so shocks start to appear after mid-October unless the 2020+ backfill lands.'}
            </p>
          </div>
        )}
      </QueryView>
    </Panel>
  )
}
