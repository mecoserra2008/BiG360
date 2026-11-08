import { useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CalendarClock, Play, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { describeSpec, fin, fmtMetric, isTerminal } from '@/api/backtest/adapters'
import { adaptEstimate, adaptLive } from '@/api/studio/adapters'
import { backtestApi } from '@/api/backtest/client'
import { bk, useBtCatalog, useBtCompare, useBtRun, useBtRuns, useBtStrategies } from '@/api/backtest/hooks'
import type { BtDraft, BtRun, BtRunStatus, BtSpec, BtStrategy } from '@/api/backtest/types'
import { TimeSeriesChart } from '@/components/charts/TimeSeriesChart'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState, ErrorState, QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Checkbox } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import { cn } from '@/lib/utils'
import { Builder } from './Builder'
import { RunResults } from './Results'
import { estimateBreakdown, extrasLine, pendingHeadline, previewLabel, remainingS } from '../studio/runLive'
import { type BtTab, useBacktest } from './store'

const fmtS = (s: number | null | undefined) => (fin(s) ? (s < 60 ? `${s.toFixed(1)} s` : `${(s / 60).toFixed(1)} min`) : '–')

function StatusBadge({ s }: { s: BtRunStatus['status'] }) {
  const tone = s === 'completed' ? 'up' : s === 'failed' ? 'down' : s === 'cancelled' ? 'neutral' : 'info'
  return <Badge tone={tone}>{s}</Badge>
}

function useSubmit() {
  const qc = useQueryClient()
  const setRun = useBacktest((s) => s.setRun)
  const watch = useBacktest((s) => s.watch)
  const [error, setError] = useState<unknown>(null)
  const m = useMutation({
    mutationFn: (a: { spec: BtSpec; mode: 'auto' | 'async'; strategyId: string | null }) => backtestApi.submit(a.spec, { mode: a.mode, strategyId: a.strategyId }),
    onMutate: () => setError(null),
    onSuccess: (run: BtRun) => {
      const s = run.run
      qc.setQueryData(bk.run(s.run_id), run)
      setRun(s.run_id)
      void qc.invalidateQueries({ queryKey: bk.runs() })
      void qc.invalidateQueries({ queryKey: bk.strategies() })
      if (!isTerminal(s.status)) {
        watch(s.run_id)
        toast({ tone: 'info', title: s.lane === 'lane' ? 'Backtest started on the studio lane' : 'Backtest queued on the worker', message: `${s.name ?? ''}${s.estimate_s ? ` · estimate ≈ ${fmtS(s.estimate_s)}` : ''}` })
      } else if (s.status === 'completed') toast({ tone: 'success', title: 'Backtest complete', message: `${s.name ?? ''} · ${fmtS(s.duration_s)}` })
      else toast({ tone: 'error', title: 'Backtest failed', message: s.error ?? undefined })
    },
    onError: (e) => {
      setError(e)
      toast({ tone: 'error', title: 'Backtest rejected', message: e instanceof Error ? e.message : String(e) })
    },
  })
  return { submit: m.mutate, pending: m.isPending, error, clearError: () => setError(null) }
}

function ResultsPane({ submitting, error, onDismiss }: { submitting: boolean; error: unknown; onDismiss: () => void }) {
  const runId = useBacktest((s) => s.runId)
  const strategyId = useBacktest((s) => s.strategyId)
  const unwatch = useBacktest((s) => s.unwatch)
  const q = useBtRun(runId)
  const strategies = useBtStrategies()
  const qc = useQueryClient()
  const st = strategies.data?.find((x) => x.strategy_id === (q.data?.run.strategy_id ?? strategyId))
  const status = q.data?.run.status
  useEffect(() => {
    if (runId && status && isTerminal(status)) {
      unwatch(runId)
      void qc.invalidateQueries({ queryKey: bk.runs() })
    }
  }, [runId, status, unwatch, qc])
  if (error)
    return (
      <Panel title="Backtest rejected" actions={<Button size="icon-sm" variant="ghost" aria-label="Dismiss" onClick={onDismiss}><X /></Button>}>
        <ErrorState error={error} />
      </Panel>
    )
  if (submitting && !q.data) return <Panel title="Running…"><SkeletonRows rows={10} /></Panel>
  if (!runId) return <EmptyState title="No run yet" message="Build a strategy on the left and run it. Quick runs finish inline; sweeps and big universes are queued on the worker." />
  if (q.isLoading) return <Panel title="Loading run…"><SkeletonRows rows={10} /></Panel>
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />
  const run = q.data as BtRun
  if (!isTerminal(run.run.status)) {
    // S1: lane / worker, queue position, what it waits behind, live ETA, learned estimate; preview result first
    const live = { ...adaptLive(run.run), status: run.run.status, estimateS: run.run.estimate_s ?? null }
    const left = remainingS(live, null)
    const breakdown = estimateBreakdown(live.estimate)
    return (
      <div className="space-y-3">
        <Panel title={`${run.run.name ?? 'Backtest'} · ${run.run.status}`} subtitle={`${pendingHeadline(live)}${left !== null ? ` · ≈ ${fmtS(left)} left` : ''}`} actions={<Button size="xs" variant="danger" onClick={() => void backtestApi.cancel(run.run.run_id).then(() => q.refetch())}>Cancel</Button>}>
          {live.status === 'queued' && live.queue?.blockedBy && <div className="mb-1 text-xs text-warn">Waiting for: {live.queue.blockedBy}</div>}
          {breakdown && <div className="num mb-2 text-2xs text-muted">estimate {breakdown}</div>}
          {live.preview ? <div className="text-xs text-fg">{previewLabel(live.preview)}</div> : <SkeletonRows rows={6} />}
        </Panel>
        {run.preview?.result ? <RunResults run={{ ...run, result: run.preview.result as unknown as BtRun['result'] }} nTrialsStrategy={st?.n_trials ?? null} oosViews={st?.n_oos_views ?? null} /> : null}
      </div>
    )
  }
  if (run.run.status !== 'completed' || !run.result)
    return (
      <Panel title={`${run.run.name ?? 'Backtest'} · ${run.run.status}`}>
        <div className="flex items-start gap-2 text-sm text-down">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
          <span>
            {run.run.error ?? 'no result'} {run.run.error_code ? <Badge tone="neutral">{run.run.error_code}</Badge> : null}
          </span>
        </div>
      </Panel>
    )
  return <RunResults run={run} nTrialsStrategy={st?.n_trials ?? null} oosViews={st?.n_oos_views ?? null} />
}

function SaveDialog({ spec, onClose }: { spec: BtDraft; onClose: () => void }) {
  const qc = useQueryClient()
  const origin = useBacktest((s) => s.origin)
  const strategyId = useBacktest((s) => s.strategyId)
  const open = useBacktest((s) => s.open)
  const [name, setName] = useState(spec.name)
  const m = useMutation({
    mutationFn: async () =>
      strategyId ? backtestApi.updateStrategy(strategyId, { name, spec: { ...spec, name } }) : backtestApi.createStrategy({ name, spec: { ...spec, name }, origin: origin ? { ...origin } : { kind: 'manual' } }),
    onSuccess: (st: BtStrategy) => {
      void qc.invalidateQueries({ queryKey: bk.strategies() })
      open(st.spec, { strategyId: st.strategy_id, origin, runId: useBacktest.getState().runId })
      toast({ tone: 'success', title: strategyId ? 'Strategy updated' : 'Strategy saved', message: 'runs now count toward its multiple-testing counter' })
      onClose()
    },
    onError: (e) => toast({ tone: 'error', title: 'Save failed', message: e instanceof Error ? e.message : String(e) }),
  })
  return (
    <Panel title={strategyId ? 'Update strategy' : 'Save as strategy'} className="mb-2" actions={<Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}><X /></Button>}>
      <div className="flex items-center gap-2">
        <input aria-label="Strategy name" className="h-7 min-w-0 flex-1 rounded-md border border-border bg-surface-2 px-2 text-sm" value={name} onChange={(e) => setName(e.target.value)} />
        <Button variant="primary" disabled={!name.trim() || m.isPending} onClick={() => m.mutate()}>
          Save
        </Button>
      </div>
    </Panel>
  )
}

function BuilderTab() {
  const cat = useBtCatalog()
  const spec = useBacktest((s) => s.spec)
  const patch = useBacktest((s) => s.patchSpec)
  const strategyId = useBacktest((s) => s.strategyId)
  const origin = useBacktest((s) => s.origin)
  const reset = useBacktest((s) => s.reset)
  const { submit, pending, error, clearError } = useSubmit()
  const [saving, setSaving] = useState(false)
  const val = useMutation({
    mutationFn: () => backtestApi.validate(spec),
    onSuccess: (v) => toast({ tone: v.warnings.length ? 'info' : 'success', title: `Spec OK · ${v.will_run} (≈ ${fmtS(v.estimate_s)})`, message: [`benchmark ${v.benchmark ?? '–'}`, v.n_sweep ? `${v.n_sweep} sweep configs` : '', v.delisted ? 'delisted names included' : '', v.estimate ? extrasLine(adaptEstimate(v.estimate)) : '', ...v.warnings].filter(Boolean).join(' · ') }),
    onError: (e) => toast({ tone: 'error', title: 'Spec rejected', message: e instanceof Error ? e.message : String(e) }),
  })
  return (
    <div className="grid items-start gap-3 lg:grid-cols-[360px_minmax(0,1fr)] 2xl:grid-cols-[400px_minmax(0,1fr)]">
      <Panel
        flush
        title="Strategy builder"
        subtitle={origin?.label ?? (strategyId ? `saved strategy ${strategyId}` : 'universe → signal → construction → costs → period')}
        className="sticky top-0 max-h-[calc(100vh-7.5rem)]"
        bodyClassName="overflow-y-auto"
        actions={<Button size="xs" variant="ghost" onClick={reset} title="Start from the default spec">New</Button>}
      >
        {(origin?.notes ?? []).length > 0 && (
          <div className="border-b border-border px-3 py-2 text-xs text-muted">
            {(origin?.notes ?? []).map((n, i) => (
              <div key={i}>• {n}</div>
            ))}
          </div>
        )}
        {cat.isError ? <ErrorState error={cat.error} className="p-3" /> : <Builder catalog={cat.data} spec={spec} patch={patch} running={pending} validating={val.isPending} onValidate={() => val.mutate()} onSave={() => setSaving(true)} onRun={(mode) => submit({ spec, mode, strategyId })} />}
      </Panel>
      <div className="min-w-0">
        {saving && <SaveDialog spec={spec} onClose={() => setSaving(false)} />}
        <ResultsPane submitting={pending} error={error} onDismiss={clearError} />
      </div>
    </div>
  )
}

function RunsTable({ runs, compare, toggle, onOpen }: { runs: BtRunStatus[]; compare: string[]; toggle: (id: string) => void; onOpen: (r: BtRunStatus) => void }) {
  if (!runs.length) return <EmptyState title="No runs yet" />
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-2xs text-muted uppercase">
          <th className="w-8 px-3 py-1" />
          <th className="py-1 font-medium">Run</th>
          <th className="py-1 font-medium">Status</th>
          <th className="py-1 text-right font-medium">Sharpe</th>
          <th className="py-1 text-right font-medium">OOS Sharpe</th>
          <th className="py-1 text-right font-medium">CAGR</th>
          <th className="py-1 text-right font-medium">Max DD</th>
          <th className="py-1 text-right font-medium">DSR</th>
          <th className="py-1 text-right font-medium">Trials</th>
          <th className="px-3 py-1 text-right font-medium">Time</th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.run_id} className="border-t border-border/60 hover:bg-surface-2">
            <td className="px-3 py-1">
              <Checkbox label="" aria-label={`Compare ${r.run_id}`} checked={compare.includes(r.run_id)} disabled={r.status !== 'completed'} onCheckedChange={() => toggle(r.run_id)} />
            </td>
            <td className="py-1">
              <button type="button" className="text-left hover:text-accent-text" onClick={() => onOpen(r)}>
                <div className="font-medium">{r.name ?? r.run_id}</div>
                <div className="text-2xs text-muted">
                  {r.run_id} · {r.created_at?.slice(0, 16).replace('T', ' ')} {r.scheduled && <Badge tone="info">paper</Badge>}
                </div>
              </button>
            </td>
            <td className="py-1">
              <StatusBadge s={r.status} />
            </td>
            <td className="num py-1 text-right">{fmtMetric(r.metrics.sharpe, 'num')}</td>
            <td className="num py-1 text-right text-accent-text">{fmtMetric(r.metrics.oos_sharpe, 'num')}</td>
            <td className="num py-1 text-right">{fmtMetric(r.metrics.cagr, 'pct')}</td>
            <td className="num py-1 text-right">{fmtMetric(r.metrics.max_drawdown, 'pct')}</td>
            <td className="num py-1 text-right">{fmtMetric(r.metrics.dsr, 'num')}</td>
            <td className="num py-1 text-right text-muted">{r.n_trials ?? '–'}</td>
            <td className="num px-3 py-1 text-right text-muted">{fmtS(r.duration_s)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function StrategiesTab() {
  const strategies = useBtStrategies()
  const runs = useBtRuns()
  const qc = useQueryClient()
  const compare = useBacktest((s) => s.compare)
  const toggle = useBacktest((s) => s.toggleCompare)
  const open = useBacktest((s) => s.open)
  const setRun = useBacktest((s) => s.setRun)
  const setTab = useBacktest((s) => s.setTab)
  const [busy, setBusy] = useState<string | null>(null)
  const act = async (id: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(id)
    try {
      await fn()
      toast({ tone: 'success', title: ok })
      void qc.invalidateQueries({ queryKey: bk.strategies() })
      void qc.invalidateQueries({ queryKey: bk.runs() })
    } catch (e) {
      toast({ tone: 'error', title: 'Action failed', message: e instanceof Error ? e.message : String(e) })
    } finally {
      setBusy(null)
    }
  }
  const openRun = (r: BtRunStatus) => {
    setRun(r.run_id)
    setTab('builder')
  }
  return (
    <div className="space-y-3">
      <Panel title="Saved strategies" subtitle="multiple-testing counter per strategy (feeds the deflated Sharpe) · weekly paper re-runs with drift / decay alerts" flush>
        <QueryView query={strategies} skeleton={<SkeletonRows rows={4} className="p-3" />}>
          {(list) =>
            list.length ? (
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-2xs text-muted uppercase">
                    <th className="px-3 py-1 font-medium">Strategy</th>
                    <th className="py-1 text-right font-medium">Trials</th>
                    <th className="py-1 text-right font-medium">OOS views</th>
                    <th className="py-1 font-medium">Paper</th>
                    <th className="px-3 py-1 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((st) => (
                    <tr key={st.strategy_id} className="border-t border-border/60">
                      <td className="max-w-[460px] px-3 py-1">
                        <button type="button" className="text-left hover:text-accent-text" onClick={() => open(st.spec, { strategyId: st.strategy_id, runId: st.last_run_id, origin: (st.origin as never) ?? null })}>
                          <div className="font-medium">{st.name}</div>
                          <div className="truncate text-2xs text-muted">{describeSpec(st.spec)}</div>
                        </button>
                      </td>
                      <td className="num py-1 text-right">{st.n_trials}</td>
                      <td className={cn('num py-1 text-right', st.n_oos_views > 5 ? 'text-warn' : 'text-muted')} title="each look at the holdout erodes its independence">
                        {st.n_oos_views}
                      </td>
                      <td className="py-1">
                        <Button
                          size="xs"
                          variant={st.schedule === 'weekly' ? 'secondary' : 'ghost'}
                          disabled={busy === st.strategy_id}
                          onClick={() => void act(st.strategy_id, () => backtestApi.updateStrategy(st.strategy_id, { schedule: st.schedule === 'weekly' ? 'none' : 'weekly' }), st.schedule === 'weekly' ? 'Paper schedule off' : 'Weekly paper re-runs on (drift alerts in Watchlist & Alerts)')}
                          title="Re-run weekly on the latest data and alert on drift / decay"
                        >
                          <CalendarClock /> {st.schedule === 'weekly' ? 'weekly' : 'off'}
                        </Button>
                      </td>
                      <td className="px-3 py-1 text-right">
                        <Button size="xs" variant="ghost" disabled={busy === st.strategy_id} onClick={() => void act(st.strategy_id, async () => { const r = await backtestApi.runStrategy(st.strategy_id); setRun(r.run.run_id) }, 'Strategy run submitted')}>
                          <Play /> Run
                        </Button>
                        <Button size="icon-sm" variant="ghost" aria-label={`Delete ${st.name}`} disabled={busy === st.strategy_id} onClick={() => void act(st.strategy_id, () => backtestApi.deleteStrategy(st.strategy_id), 'Strategy deleted')}>
                          <Trash2 />
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <EmptyState title="No saved strategies" message="Save a draft from the builder (Save)." />
            )
          }
        </QueryView>
      </Panel>
      <Panel title="Runs" subtitle={`tick completed runs to compare (${compare.length} selected)`} flush actions={compare.length > 0 ? <Button size="xs" variant="primary" onClick={() => setTab('compare')}>Compare {compare.length}</Button> : undefined}>
        <QueryView query={runs} skeleton={<SkeletonRows rows={6} className="p-3" />}>
          {(list) => <RunsTable runs={list} compare={compare} toggle={toggle} onOpen={openRun} />}
        </QueryView>
      </Panel>
    </div>
  )
}

function CompareTab() {
  const ids = useBacktest((s) => s.compare)
  const clear = useBacktest((s) => s.clearCompare)
  const q = useBtCompare(ids)
  if (!ids.length) return <EmptyState title="Nothing to compare" message="Tick completed runs in Strategies & runs." />
  return (
    <QueryView query={q} skeleton={<SkeletonRows rows={8} />}>
      {(d) => (
        <div className="space-y-3">
          <Panel title="Equity curves" subtitle="NAV (start = 1), each on its own window" actions={<Button size="xs" variant="ghost" onClick={clear}>Clear</Button>}>
            <TimeSeriesChart series={d.curves.map((c) => ({ name: c.name ?? c.run_id, data: c.dates.map((x, i) => ({ date: x, value: c.nav[i] ?? null })) }))} format={(v) => v.toFixed(2)} height={300} ariaLabel="Equity curves of the compared runs" />
          </Panel>
          <Panel title="Metrics" flush>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-2xs text-muted uppercase">
                    <th className="px-3 py-1 font-medium">Metric</th>
                    {d.runs.map((r) => (
                      <th key={r.run_id} className="py-1 pr-3 text-right font-medium">
                        {r.name ?? r.run_id}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {d.metrics.map((m) => (
                    <tr key={m.metric} className="border-t border-border/60">
                      <td className="px-3 py-1 text-muted">{m.metric}</td>
                      {d.runs.map((r) => {
                        const v = m.values[r.run_id]
                        const isPct = /cagr|vol|drawdown|return|hit|alpha|spread|turnover/.test(m.metric)
                        return (
                          <td key={r.run_id} className="num py-1 pr-3 text-right">
                            {fmtMetric(v, m.metric.endsWith('_bps') ? 'bps' : isPct ? 'pct' : 'num')}
                          </td>
                        )
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>
      )}
    </QueryView>
  )
}

function PresetsTab() {
  const cat = useBtCatalog()
  const open = useBacktest((s) => s.open)
  return (
    <QueryView query={cat} skeleton={<SkeletonRows rows={6} />}>
      {(c) => (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {c.presets.map((p) => (
            <Panel key={p.id} title={p.title} subtitle={describeSpec(p.spec)} actions={<Button size="xs" variant="primary" onClick={() => open(p.spec, { origin: { kind: 'preset', ref: p.id, label: `preset · ${p.title}` } })}>Open</Button>}>
              <div className="text-xs text-muted">{p.spec.mode === 'event' ? 'event-driven' : 'rank / quantile'} · {p.spec.universe?.kind === 'assets' ? 'cross-asset' : (p.spec.universe?.universe_id ?? '')}</div>
            </Panel>
          ))}
        </div>
      )}
    </QueryView>
  )
}

export default function BacktestLab() {
  const tab = useBacktest((s) => s.tab)
  const setTab = useBacktest((s) => s.setTab)
  const cat = useBtCatalog()
  return (
    <>
      <PageHeader
        title="Backtest Lab"
        subtitle="Point-in-time strategy backtests: universe → signal → construction → costs → period with a mandatory holdout, deflated Sharpe, PBO and permutation tests"
        actions={cat.data ? <Badge tone="neutral" title="Data snapshot the catalog was measured on">snapshot {cat.data.snapshot_id ?? '–'}</Badge> : undefined}
      />
      <div className="mb-3 flex items-start gap-2 rounded-md border border-warn/40 bg-warn-soft px-3 py-2 text-xs text-fg" role="note">
        <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />
        <span>
          {cat.data?.survivorship_caveat ?? 'Survivorship: universe membership history starts 2026-10-10; earlier dates use the initial member list. The 753 US delisted names are included where priced.'} Model-based inputs (ML, model expectations, HMM regimes, trend / curve signals) are labelled.
        </span>
      </div>
      <Tabs value={tab} onValueChange={(v) => setTab(v as BtTab)}>
        <TabsList>
          <TabsTrigger value="builder">Builder & results</TabsTrigger>
          <TabsTrigger value="strategies">Strategies & runs</TabsTrigger>
          <TabsTrigger value="compare">Compare</TabsTrigger>
          <TabsTrigger value="presets">Presets</TabsTrigger>
        </TabsList>
        <TabsContent value="builder">
          <BuilderTab />
        </TabsContent>
        <TabsContent value="strategies">
          <StrategiesTab />
        </TabsContent>
        <TabsContent value="compare">
          <CompareTab />
        </TabsContent>
        <TabsContent value="presets">
          <PresetsTab />
        </TabsContent>
      </Tabs>
    </>
  )
}
