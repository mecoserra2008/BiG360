import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Play, Save, Trash2 } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { resolveId } from '@/api/client'
import { defaultWatchlistId } from '@/api/remote'
import { macroApi } from '@/api/macro/client'
import { useSectorTree } from '@/api/hooks'
import { useScenarios } from '@/api/macro/hooks'
import { type MxImpact, type MxRunRequest, type MxScenario, type MxScenarioResult, type MxShocks, cleanShocks } from '@/api/macro/v34'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ErrorState, QueryView, Skeleton } from '@/components/ui/feedback'
import { Checkbox, Input, Segmented, Select, Slider } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { useWatchlist } from '@/store'
import { fmtChange } from '../fmt'
import { ImpactBars } from './ImpactBars'

type Field = keyof MxShocks
const RANGE: Record<string, [number, number, number]> = {
  rates_2y_bp: [-300, 300, 5],
  rates_10y_bp: [-300, 300, 5],
  real_10y_bp: [-200, 200, 5],
  breakeven_10y_bp: [-200, 200, 5],
  credit_bp: [-200, 400, 5],
  usd_pct: [-20, 20, 0.5],
  oil_pct: [-60, 100, 1],
  equity_pct: [-40, 30, 1],
  growth_surprise: [-200, 200, 5],
  inflation_surprise: [-200, 200, 5],
  labor_surprise: [-200, 200, 5],
}
const UNIVERSES = [
  { value: 'mvp_us', label: 'US MVP universe' },
  { value: 'mvp_global', label: 'Global MVP universe' },
  { value: 'full', label: 'Full universe' },
] as const
const REGIONS = ['US', 'EU', 'UK', 'JP', 'CN', 'HK', 'KR', 'LATAM']
const SECTORS = [
  ['CDIS', 'Consumer Discretionary'],
  ['COMM', 'Communication & Media'],
  ['CSTA', 'Consumer Staples'],
  ['ENRG', 'Energy'],
  ['FINS', 'Financials'],
  ['HLTH', 'Healthcare'],
  ['INDU', 'Industrials'],
  ['MATR', 'Materials'],
  ['REAL', 'Real Estate'],
  ['TECH', 'Technology'],
  ['UTIL', 'Utilities'],
].map(([value, label]) => ({ value, label }))
const UNI_LABEL: Record<string, string> = { mvp_us: 'US MVP', mvp_global: 'Global MVP', full: 'Full universe' }

const fin = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const pc = (v: unknown, d = 1) => (fin(v) ? `${fmtChange(v * 100, d)}%` : DASH)

export function Scenarios() {
  const qc = useQueryClient()
  const list = useScenarios()
  const watchIds = useWatchlist((s) => s.ids)
  const [shocks, setShocks] = useState<Record<string, number>>({ rates_2y_bp: -100, rates_10y_bp: -100 })
  const [mode, setMode] = useState<'isolated' | 'conditional'>('isolated')
  const [horizon, setHorizon] = useState('3')
  const [universe, setUniverse] = useState<MxRunRequest['universe']>('mvp_us')
  const [region, setRegion] = useState('all')
  const [weighting, setWeighting] = useState<'cw' | 'ew'>('cw')
  const [useWatch, setUseWatch] = useState(false)
  const [sector, setSector] = useState('all')
  const [sub, setSub] = useState('all')
  const tree = useSectorTree('ALL')
  const sectorOpts = tree.data?.length ? tree.data.map((x) => ({ value: x.id, label: x.name })) : SECTORS
  const subOpts = tree.data?.find((x) => x.id === sector)?.subsectors.map((x) => ({ value: x.id, label: x.name })) ?? []
  const [name, setName] = useState('Custom scenario')
  const [loaded, setLoaded] = useState<MxScenario | null>(null)

  const options = { mode, var_propagation: mode === 'conditional', var_horizon_months: Number(horizon) }
  const invalidate = () => qc.invalidateQueries({ queryKey: ['macro', 'scenarios'] })

  const run = useMutation({
    mutationFn: async ({ sec, sub: ss }: { sec: string; sub: string }): Promise<MxScenarioResult> => {
      const security_ids = useWatch ? await Promise.all(watchIds.map((id) => resolveId(id).catch(() => id))) : null
      // the server watchlist gives the equal-weight watchlist aggregate (guide 02b: it was never requested)
      const watchlist_id = useWatch ? await defaultWatchlistId().catch(() => null) : null
      return macroApi.runScenario({ shocks: cleanShocks(shocks), options, name, universe, region: region === 'all' ? null : region, sectors: sec === 'all' ? null : [sec], subsectors: ss === 'all' ? null : [ss], weighting, top_n: 20, security_ids, ...(watchlist_id ? { watchlist_id } : {}) })
    },
    onError: (e) => toast({ tone: 'error', title: 'Scenario run failed', message: e instanceof Error ? e.message : String(e) }),
  })
  const save = useMutation({
    mutationFn: () => (loaded && !loaded.preset ? macroApi.updateScenario(loaded.scenario_id, { name, shocks: cleanShocks(shocks), options }) : macroApi.createScenario({ name, shocks: cleanShocks(shocks), options })),
    onSuccess: (s) => {
      setLoaded(s)
      toast({ tone: 'success', title: loaded && !loaded.preset ? 'Scenario updated' : 'Scenario saved', message: s.name })
      void invalidate()
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not save the scenario', message: e instanceof Error ? e.message : String(e) }),
  })
  const remove = useMutation({
    mutationFn: (s: MxScenario) => macroApi.deleteScenario(s.scenario_id).then(() => s),
    onSuccess: (s) => {
      if (loaded?.scenario_id === s.scenario_id) setLoaded(null)
      toast({ tone: 'success', title: 'Scenario deleted', message: s.name })
      void invalidate()
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not delete the scenario', message: e instanceof Error ? e.message : String(e) }),
  })

  const load = (s: MxScenario) => {
    setShocks(Object.fromEntries(Object.entries(s.shocks).filter(([, v]) => fin(v))) as Record<string, number>)
    setMode(s.options?.mode === 'conditional' ? 'conditional' : 'isolated')
    if (s.options?.var_horizon_months) setHorizon(String(s.options.var_horizon_months))
    setName(s.preset ? `${s.name} (copy)` : s.name)
    setLoaded(s)
    run.reset()
  }

  return (
    <div className="grid gap-3 xl:grid-cols-[360px_minmax(0,1fr)]">
      <div className="space-y-3">
        <Panel title="Presets & saved scenarios" flush>
          <QueryView query={list} skeleton={<Skeleton className="m-3 h-40" />}>
            {(d) => (
              <div className="max-h-[300px] overflow-auto text-xs">
                <div className="px-3 pt-2 text-2xs font-semibold tracking-wide text-muted uppercase">Saved ({d.saved.length})</div>
                {d.saved.length === 0 && <p className="px-3 py-1 text-muted">No saved scenarios yet.</p>}
                {d.saved.map((s) => (
                  <div key={s.scenario_id} className={cn('flex items-center gap-1 px-3 py-0.5 hover:bg-surface-2', loaded?.scenario_id === s.scenario_id && 'bg-accent/10')}>
                    <button type="button" className="min-w-0 flex-1 truncate text-left text-fg hover:underline" onClick={() => load(s)} title={s.description ?? s.name}>
                      {s.name}
                    </button>
                    <span className="text-2xs text-faint">{s.updated_at ? fmtDate(s.updated_at) : ''}</span>
                    <Button size="icon-sm" variant="ghost" aria-label={`Delete ${s.name}`} onClick={() => remove.mutate(s)} disabled={remove.isPending}>
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                ))}
                <div className="px-3 pt-2 text-2xs font-semibold tracking-wide text-muted uppercase">Presets</div>
                {d.presets.map((s) => (
                  <button key={s.scenario_id} type="button" className={cn('block w-full truncate px-3 py-1 text-left text-fg hover:bg-surface-2', loaded?.scenario_id === s.scenario_id && 'bg-accent/10')} onClick={() => load(s)} title={s.description ?? s.name}>
                    {s.name}
                  </button>
                ))}
              </div>
            )}
          </QueryView>
        </Panel>
        <Panel
          title="Shocks"
          subtitle="Instantaneous changes; leave at 0 to ignore"
          actions={
            <Button size="sm" variant="ghost" onClick={() => setShocks({})}>
              Reset
            </Button>
          }
        >
          <div className="space-y-2.5">
            {(list.data?.shock_fields ?? Object.keys(RANGE).map((f) => ({ field: f, label: f }))).map((f) => {
              const [min, max, step] = RANGE[f.field] ?? [-100, 100, 1]
              const v = shocks[f.field] ?? 0
              return (
                <div key={f.field}>
                  <div className="mb-1 flex items-center justify-between gap-2 text-xs">
                    <label htmlFor={`shock-${f.field}`} className={cn('truncate', v ? 'text-fg' : 'text-muted')}>
                      {f.label}
                    </label>
                    <Input
                      id={`shock-${f.field}`}
                      type="number"
                      step={step}
                      value={v}
                      onChange={(e) => {
                        const n = Number(e.target.value)
                        setShocks((s) => ({ ...s, [f.field]: Number.isFinite(n) ? n : 0 }))
                      }}
                      className="num h-6 w-20 text-right"
                    />
                  </div>
                  <Slider label={f.label} min={min} max={max} step={step} value={[Math.max(min, Math.min(max, v))]} onValueChange={([n]) => setShocks((s) => ({ ...s, [f.field as Field]: n }))} />
                </div>
              )
            })}
          </div>
        </Panel>
        <Panel title="Run settings">
          <div className="space-y-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Segmented
                label="Propagation"
                size="xs"
                value={mode}
                onChange={setMode}
                options={[
                  { value: 'isolated', label: 'Isolated' },
                  { value: 'conditional', label: 'Conditional (VAR)' },
                ]}
              />
              {mode === 'conditional' && (
                <Select label="VAR horizon" value={horizon} onChange={setHorizon} options={['1', '3', '6', '12'].map((h) => ({ value: h, label: `${h}m horizon` }))} />
              )}
            </div>
            <p className="text-2xs text-muted">{mode === 'isolated' ? 'Only the factors you shock move; everything else is held at 0.' : 'Unshocked factors respond through a monthly VAR on the factor history (model-based propagation).'}</p>
            <div className="flex flex-wrap items-center gap-2">
              <Select label="Universe" value={universe} onChange={setUniverse} options={[...UNIVERSES]} />
              <Select label="Region" value={region} onChange={setRegion} options={[{ value: 'all', label: 'All regions' }, ...REGIONS.map((r) => ({ value: r, label: r }))]} />
              <Select
                label="Sector"
                value={sector}
                onChange={(v) => {
                  setSector(v)
                  setSub('all')
                }}
                options={[{ value: 'all', label: 'All sectors' }, ...sectorOpts]}
              />
              {sector !== 'all' && subOpts.length > 0 && <Select label="Subsector" value={sub} onChange={setSub} options={[{ value: 'all', label: 'All subsectors' }, ...subOpts]} />}
              <Segmented
                label="Weighting"
                size="xs"
                value={weighting}
                onChange={setWeighting}
                options={[
                  { value: 'cw', label: 'Cap-wt' },
                  { value: 'ew', label: 'Equal-wt' },
                ]}
              />
            </div>
            <label className="flex items-center gap-2">
              <Checkbox label="Include my watchlist" checked={useWatch} onCheckedChange={(c) => setUseWatch(c === true)} />
              Include my watchlist ({watchIds.length})
            </label>
            <div className="flex items-center gap-2 pt-1">
              <Input aria-label="Scenario name" value={name} onChange={(e) => setName(e.target.value)} className="min-w-0 flex-1" />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="primary" size="sm" onClick={() => run.mutate({ sec: sector, sub: sector === 'all' ? 'all' : sub })} disabled={run.isPending || Object.keys(cleanShocks(shocks)).length === 0}>
                <Play className="size-3.5" /> {run.isPending ? 'Running…' : 'Run scenario'}
              </Button>
              <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending || !name.trim() || Object.keys(cleanShocks(shocks)).length === 0}>
                <Save className="size-3.5" /> {loaded && !loaded.preset ? 'Update saved' : 'Save as new'}
              </Button>
              {loaded && !loaded.preset && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setLoaded(null)
                    setName(`${name} (copy)`)
                  }}
                >
                  Detach
                </Button>
              )}
            </div>
          </div>
        </Panel>
      </div>
      <div className="min-w-0 space-y-3">
        {run.isError ? (
          <ErrorState error={run.error} onRetry={() => run.mutate({ sec: sector, sub: sector === 'all' ? 'all' : sub })} />
        ) : run.data ? (
          <ScenarioResult r={run.data} watch={useWatch} sectorLabel={run.variables && run.variables.sec !== 'all' ? [sectorOpts.find((x) => x.value === run.variables?.sec)?.label ?? run.variables.sec, run.variables.sub !== 'all' ? (tree.data?.flatMap((x) => x.subsectors).find((x) => x.id === run.variables?.sub)?.name ?? run.variables.sub) : null].filter(Boolean).join(' / ') : null} />
        ) : run.isPending ? (
          <Skeleton className="h-[480px]" />
        ) : (
          <Panel title="Scenario impacts">
            <p className="py-10 text-center text-xs text-muted">Set shocks (or load a preset) and press Run scenario. Impacts are model-based estimates from rolling factor betas, not forecasts.</p>
          </Panel>
        )}
      </div>
    </div>
  )
}

function ScenarioResult({ r, watch, sectorLabel }: { r: MxScenarioResult; watch: boolean; sectorLabel: string | null }) {
  const sectorName = useMemo(() => Object.fromEntries(r.sectors.map((s) => [s.entity_id, s.name])), [r.sectors])
  return (
    <>
      <Panel
        title={r.scenario.name}
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-1.5">
            <Badge tone="info">model-based</Badge> {r.model_label} · betas as of {r.as_of ? fmtDate(r.as_of) : DASH} · {r.n_securities.toLocaleString()} securities
          </span>
        }
      >
        <div className="grid grid-cols-2 gap-3 @3xl:grid-cols-4 sm:grid-cols-4">
          {r.universe.map((u) => (
            <Stat key={u.universe_id} label={`${UNI_LABEL[u.universe_id] ?? u.universe_id} (${u.weighting})`} value={<span className={cn('num', fin(u.impact) && (u.impact >= 0 ? 'text-up' : 'text-down'))}>{pc(u.impact)}</span>} sub={`90%: ${pc(u.impact_lo)} to ${pc(u.impact_hi)} · n ${u.n}`} />
          ))}
          {r.watchlist && <Stat label={`Watchlist: ${r.watchlist.name}`} value={<span className="num">{pc(r.watchlist.impact)}</span>} sub={`90%: ${pc(r.watchlist.impact_lo)} to ${pc(r.watchlist.impact_hi)} · n ${r.watchlist.n}`} />}
        </div>
        <p className="mt-2 text-2xs text-muted">{r.band}.</p>
        {r.notes.length > 0 && (
          <ul className="mt-1 list-disc pl-4 text-2xs text-muted">
            {r.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        )}
      </Panel>
      <div className="grid gap-3 2xl:grid-cols-2">
        <Panel title="Sectors" subtitle="Impact with 90% bands">
          <ImpactBars rows={r.sectors.map((s) => ({ label: s.name, value: s.impact, lo: s.impact_lo, hi: s.impact_hi }))} ariaLabel="Scenario impact by sector" />
        </Panel>
        <Panel title="Regions & style factors" subtitle="Impact with 90% bands">
          <ImpactBars rows={[...r.regions, ...r.factors].map((s) => ({ label: s.entity_type === 'factor' ? s.name : `Region ${s.name}`, value: s.impact, lo: s.impact_lo, hi: s.impact_hi }))} ariaLabel="Scenario impact by region and factor" />
        </Panel>
      </div>
      <Panel
        title="Winners & losers"
        subtitle={`Securities with the largest model-implied moves${sectorLabel ? ` · ${sectorLabel} only` : ''}`}
        flush
      >
        <div className="grid gap-0 2xl:grid-cols-2">
          <ImpactTable title="Winners" rows={r.winners} sectorName={sectorName} />
          <ImpactTable title="Losers" rows={r.losers} sectorName={sectorName} />
        </div>
      </Panel>
      {(r.watchlist || (watch && r.securities.length > 0)) && (
        <Panel title="Watchlist securities" flush>
          <ImpactTable title="" rows={r.watchlist?.items ?? r.securities} sectorName={sectorName} />
        </Panel>
      )}
      {watch && !r.watchlist && r.securities.length === 0 && <p className="text-2xs text-muted">None of the watchlist securities has macro betas.</p>}
      <div className="grid gap-3">
        <Panel title="Instruments" subtitle="ETFs and indices" flush>
          <div className="grid 2xl:grid-cols-2">
            <ImpactTable title="Top" rows={r.instruments_top.slice(0, 10)} sectorName={sectorName} />
            <ImpactTable title="Bottom" rows={r.instruments_bottom.slice(0, 10)} sectorName={sectorName} />
          </div>
        </Panel>
        <Panel title="Factor vector" subtitle={r.var ? `VAR(${r.var.lags}) on ${r.var.n_obs} months, ${r.var.horizon_months}m horizon (model-based)` : 'Isolated shock: no propagation'} flush>
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-border text-left text-2xs text-muted uppercase">
                <th className="px-3 py-1">Factor</th>
                <th className="px-3 py-1 text-right">Shocked</th>
                <th className="px-3 py-1 text-right">Total</th>
                <th className="px-3 py-1">Unit</th>
              </tr>
            </thead>
            <tbody>
              {r.factor_vector.total.map((f) => {
                const fr = r.factor_vector.first_round.find((x) => x.factor_id === f.factor_id)
                return (
                  <tr key={f.factor_id} className="border-b border-border/60">
                    <td className="px-3 py-0.5 font-medium text-fg">{f.factor_id}</td>
                    <td className="num px-3 py-0.5 text-right">{fmtChange(fr?.value, 3)}</td>
                    <td className={cn('num px-3 py-0.5 text-right', fr && Math.abs(fr.value - f.value) > 1e-9 && 'font-semibold text-accent-text')}>{fmtChange(f.value, 3)}</td>
                    <td className="px-3 py-0.5 text-muted">{f.unit}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </Panel>
      </div>
    </>
  )
}

function ImpactTable({ title, rows, sectorName }: { title: string; rows: MxImpact[]; sectorName: Record<string, string> }) {
  return (
    <div className="min-w-0 overflow-auto">
      {title && <div className="border-b border-border px-3 py-1 text-2xs font-semibold tracking-wide text-muted uppercase">{title}</div>}
      <table className="w-full text-xs">
        <tbody>
          {rows.length === 0 && (
            <tr>
              <td className="px-3 py-3 text-center text-muted">No rows for this filter.</td>
            </tr>
          )}
          {rows.map((x, i) => (
            <tr key={`${x.entity_id}-${i}`} className="border-b border-border/60">
              <td className="px-3 py-0.5 font-medium whitespace-nowrap">{x.entity_type === 'security' ? <Link to={`/company/${encodeURIComponent(x.ticker ?? x.entity_id)}`} className="text-accent-text hover:underline">{x.ticker ?? x.entity_id}</Link> : (x.ticker ?? x.entity_id)}</td>
              <td className="max-w-[200px] truncate px-1 py-0.5 text-muted" title={x.name}>
                {x.name}
              </td>
              <td className="px-1 py-0.5 text-2xs whitespace-nowrap text-faint">{[x.sector_id ? (sectorName[x.sector_id] ?? x.sector_id) : null, x.region].filter(Boolean).join(' · ')}</td>
              <td className={cn('num px-2 py-0.5 text-right font-semibold', fin(x.impact) && (x.impact >= 0 ? 'text-up' : 'text-down'))}>{pc(x.impact)}</td>
              <td className="num px-3 py-0.5 text-right text-2xs whitespace-nowrap text-muted">
                [{pc(x.impact_lo)}, {pc(x.impact_hi)}]
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
