import type { ColumnDef } from '@tanstack/react-table'
import { BookOpen, Download, FileJson, Play, Save, ScanSearch, Trash2 } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { useMutation } from '@tanstack/react-query'
import { bqlApi } from '@/api/bql/client'
import { useBqlFields, useBqlSaved, useBqlSavedMutations } from '@/api/bql/hooks'
import type { BqlCell, BqlExplain, BqlResult } from '@/api/bql/types'
import { TimeSeriesChart, type TsSeries } from '@/components/charts/TimeSeriesChart'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Kbd } from '@/components/ui/data'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { toast } from '@/components/ui/toast'
import { type BqlHistoryEntry, EXAMPLES, fmtBqlValue, parseBqlHistory } from '@/lib/bql'
import { fmtDateTime, fmtInt } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { safeStorage } from '@/lib/utils'
import { BqlEditor } from './Editor'

const HIST_KEY = 'big360.bql.history'
const DRAFT_KEY = 'big360.bql.draft'
const DEFAULT_Q = EXAMPLES[0].query

function loadHist(): BqlHistoryEntry[] {
  return parseBqlHistory(safeStorage.get(HIST_KEY) ?? '[]')
}

export default function BqlConsole() {
  const [params, setParams] = useSearchParams()
  const [text, setText] = useState(() => params.get('q') ?? safeStorage.get(DRAFT_KEY) ?? DEFAULT_Q)
  const [hist, setHist] = useState(loadHist)
  const [explain, setExplain] = useState<BqlExplain | null>(null)
  const run = useMutation({ mutationFn: (q: string) => bqlApi.run(q) })
  const ex = useMutation({ mutationFn: (q: string) => bqlApi.explain(q) })
  const autorun = useRef(params.get('run') === '1')

  const doRun = (q = text) => {
    if (!q.trim()) return
    setExplain(null)
    run.mutate(q, {
      onSuccess: () => {
        const next = [{ q, at: new Date().toISOString() }, ...hist.filter((h) => h.q !== q)].slice(0, 30)
        setHist(next)
        safeStorage.set(HIST_KEY, JSON.stringify(next))
      },
    })
  }

  useEffect(() => {
    safeStorage.set(DRAFT_KEY, text)
  }, [text])

  // deep links: /bql?q=...&run=1 (command bar `BQL get(...)`, Screener "Open as BQL")
  useEffect(() => {
    const q = params.get('q')
    if (q !== null && q !== text) setText(q)
    if (autorun.current && q) {
      autorun.current = false
      doRun(q)
      setParams((p) => {
        p.delete('run')
        return p
      }, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params])

  const doExplain = () => ex.mutate(text, { onSuccess: (r) => setExplain(r) })

  const doExport = async (format: 'csv' | 'xlsx' | 'json') => {
    try {
      const { blob, filename } = await bqlApi.export(text, format)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (e) {
      toast({ tone: 'error', title: 'Export failed', message: e instanceof Error ? e.message : String(e) })
    }
  }

  return (
    <>
      <PageHeader
        title="BQL console"
        subtitle="get(...) for(...) with(...) — point-in-time queries over the serving snapshot · every literal is a bound parameter"
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={doExplain} disabled={ex.isPending}>
              <ScanSearch /> Explain
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void doExport('csv')} title="Download CSV">
              <Download /> CSV
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void doExport('xlsx')} title="Download XLSX">
              <Download /> XLSX
            </Button>
            <Button variant="ghost" size="sm" onClick={() => void doExport('json')} title="Download JSON">
              <FileJson /> JSON
            </Button>
            <SaveButton query={text} />
            <Button variant="primary" size="sm" onClick={() => doRun()} disabled={run.isPending}>
              <Play /> Run <Kbd className="ml-1">Ctrl ↵</Kbd>
            </Button>
          </>
        }
      />
      <div className="grid grid-cols-12 gap-3">
        <div className="col-span-12 space-y-3 xl:col-span-8 2xl:col-span-9">
          <Panel title="Query" subtitle="Ctrl+Enter runs · Tab completes fields and functions" bodyClassName="p-2">
            <BqlEditor value={text} onChange={setText} onRun={() => doRun()} />
          </Panel>
          {ex.isError && <ErrorState error={ex.error} className="rounded-md border border-border bg-surface" />}
          {explain && <ExplainPanel e={explain} onClose={() => setExplain(null)} />}
          {run.isPending ? (
            <Panel title="Result">
              <SkeletonRows rows={10} />
            </Panel>
          ) : run.isError ? (
            <ErrorState error={run.error} onRetry={() => doRun()} className="rounded-md border border-border bg-surface" />
          ) : run.data ? (
            <ResultView r={run.data} />
          ) : (
            <Panel title="Result">
              <EmptyState title="No result yet" message="Write a query (or pick an example on the right) and press Run." />
            </Panel>
          )}
        </div>
        <aside className="col-span-12 xl:col-span-4 2xl:col-span-3">
          <SidePanel
            onPick={(q) => setText(q)}
            onRun={(q) => {
              setText(q)
              doRun(q)
            }}
            hist={hist}
            onClearHist={() => {
              setHist([])
              safeStorage.remove(HIST_KEY)
            }}
          />
        </aside>
      </div>
    </>
  )
}

function SaveButton({ query }: { query: string }) {
  const { save } = useBqlSavedMutations()
  const [open, setOpen] = useState(false)
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'query' | 'screen'>('query')
  if (!open)
    return (
      <Button variant="ghost" size="sm" onClick={() => setOpen(true)}>
        <Save /> Save
      </Button>
    )
  return (
    <div className="flex items-center gap-1">
      <Input aria-label="Saved query name" placeholder="name" className="w-36" value={name} onChange={(e) => setName(e.target.value)} autoFocus />
      <Segmented label="Kind" size="xs" value={kind} onChange={setKind} options={[{ value: 'query', label: 'query' }, { value: 'screen', label: 'screen', title: "usable as members('screen:NAME')" }]} />
      <Button
        size="sm"
        variant="secondary"
        disabled={!name.trim() || save.isPending}
        onClick={() =>
          save.mutate(
            { name: name.trim(), query, kind },
            {
              onSuccess: () => {
                toast({ tone: 'success', title: `Saved "${name.trim()}"`, message: kind === 'screen' ? `use it as members('screen:${name.trim()}')` : undefined })
                setOpen(false)
                setName('')
              },
              onError: (e) => toast({ tone: 'error', title: 'Could not save', message: e instanceof Error ? e.message : String(e) }),
            },
          )
        }
      >
        Save
      </Button>
      <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
        Cancel
      </Button>
    </div>
  )
}

function ExplainPanel({ e, onClose }: { e: BqlExplain; onClose: () => void }) {
  return (
    <Panel
      title="Explain"
      subtitle={`${e.mode.replace('_', '-')} · universe ${fmtInt(e.universe_size)} · ${fmtInt(e.estimates.cells)} cells${e.estimates.needs_daily_panel ? ` · daily panel from ${e.estimates.daily_from}` : ''} · ${e.params.length} bound parameters`}
      actions={
        <Button size="xs" variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      <pre className="max-h-72 overflow-auto rounded-sm bg-surface-2 p-2 font-mono text-2xs text-muted">{e.sql}</pre>
      <p className="mt-1 text-2xs text-faint">Parameters (names and types only): {e.params.map((p) => `${p.name}:${p.type}`).join(', ')}</p>
    </Panel>
  )
}

/* ── results ── */

function ResultView({ r }: { r: BqlResult }) {
  const valueCols = r.columns.map((c, i) => ({ c, i })).filter(({ c }) => c.role === 'value' && c.dtype === 'number')
  const chartable = r.mode === 'timeseries' && valueCols.length > 0
  const [view, setView] = useState<'table' | 'chart'>(chartable ? 'chart' : 'table')
  const [metric, setMetric] = useState(valueCols[0]?.i ?? -1)
  useEffect(() => {
    setView(chartable ? 'chart' : 'table')
    setMetric(valueCols[0]?.i ?? -1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [r])
  return (
    <Panel
      title="Result"
      subtitle={`${fmtInt(r.n_rows)} rows${r.truncated ? ' (truncated)' : ''} · ${r.mode === 'timeseries' ? `${r.dates?.n} dates ${r.dates?.start} → ${r.dates?.end}${r.dates?.freq ? ` (${r.dates.freq})` : ''}` : `as of ${r.as_of}`} · universe ${fmtInt(r.universe_size)} · ${r.elapsed_ms ?? 0} ms${r.cached ? ' (cached)' : ''}`}
      actions={
        chartable ? (
          <>
            {view === 'chart' && valueCols.length > 1 && (
              <Select label="Chart column" value={String(metric)} onChange={(v) => setMetric(Number(v))} options={valueCols.map(({ c, i }) => ({ value: String(i), label: c.name }))} />
            )}
            <Segmented label="View" size="xs" value={view} onChange={setView} options={[{ value: 'chart', label: 'chart' }, { value: 'table', label: 'table' }]} />
          </>
        ) : undefined
      }
      flush
    >
      {r.warnings.length > 0 && (
        <ul className="space-y-0.5 border-b border-border px-3 py-1.5 text-2xs text-warn" aria-label="Warnings">
          {r.warnings.map((w) => (
            <li key={w}>⚠ {w}</li>
          ))}
        </ul>
      )}
      {view === 'chart' && chartable && metric >= 0 ? <ResultChart r={r} col={metric} /> : <ResultTable r={r} />}
      <div className="flex flex-wrap gap-x-3 border-t border-border px-3 py-1 text-2xs text-faint">
        <span>snapshot {r.serving_run_id} (as of {r.snapshot_as_of})</span>
        {r.currency && <span>currency {r.currency}</span>}
        <span className="num truncate">{r.query}</span>
      </div>
    </Panel>
  )
}

function ResultTable({ r }: { r: BqlResult }) {
  const columns = useMemo<ColumnDef<BqlCell[]>[]>(() => {
    const idx = Object.fromEntries(r.columns.map((c, i) => [c.name, i]))
    const out: ColumnDef<BqlCell[]>[] = []
    if (!r.grouped) {
      out.push({
        id: 'security',
        header: 'Security',
        size: 220,
        accessorFn: (row) => row[idx.ticker] as string,
        cell: (c) => {
          const id = String(c.row.original[idx.security_id])
          return (
            <Link to={securityRoute(id)} className="flex min-w-0 items-baseline gap-1.5 hover:text-accent-text">
              <span className="num shrink-0 font-semibold">{String(c.row.original[idx.ticker] ?? id)}</span>
              <span className="truncate text-muted">{String(c.row.original[idx.name] ?? '')}</span>
            </Link>
          )
        },
      })
    }
    r.columns.forEach((col, i) => {
      if (col.role === 'key') return
      const num = col.dtype === 'number'
      out.push({
        id: `c${i}`,
        header: col.name,
        size: Math.max(90, Math.min(220, col.name.length * 8 + 24)),
        accessorFn: (row) => row[i] as never,
        sortUndefined: 'last',
        cell: (c) => {
          const v = c.getValue()
          return <span className={num ? 'num' : undefined}>{fmtBqlValue(v, col.unit)}</span>
        },
        meta: { align: num ? 'right' : 'left', title: [col.description, col.unit && `unit ${col.unit}`, col.currency && `currency ${col.currency}`, col.model_based && 'model-based'].filter(Boolean).join(' · ') || col.name },
      })
    })
    return out
  }, [r])
  return <DataTable data={r.rows} columns={columns} ariaLabel="BQL result" height={Math.min(560, 30 + r.rows.length * 26)} manualSorting={false} />
}

function ResultChart({ r, col }: { r: BqlResult; col: number }) {
  const series = useMemo<TsSeries[]>(() => {
    const di = r.columns.findIndex((c) => c.role === 'date')
    const keyIdx = r.grouped ? r.columns.findIndex((c) => c.role === 'group') : r.columns.findIndex((c) => c.name === 'ticker')
    const by = new Map<string, { date: string; value: number | null }[]>()
    for (const row of r.rows) {
      const k = String(row[keyIdx] ?? '—')
      if (!by.has(k) && by.size >= 8) continue
      const v = row[col]
      ;(by.get(k) ?? by.set(k, []).get(k)!).push({ date: String(row[di]), value: typeof v === 'number' ? v : typeof v === 'boolean' ? Number(v) : null })
    }
    return [...by.entries()].map(([name, data]) => ({ name, data }))
  }, [r, col])
  const unit = r.columns[col]?.unit
  const capped = new Set(r.rows.map((x) => x[r.grouped ? r.columns.findIndex((c) => c.role === 'group') : 0])).size > 8
  return (
    <div className="p-2">
      <TimeSeriesChart series={series} height={320} format={(v) => fmtBqlValue(v, unit)} ariaLabel={`BQL ${r.columns[col]?.name} over time`} legend />
      {capped && <p className="px-1 text-2xs text-faint">first 8 series shown · switch to the table for all rows</p>}
    </div>
  )
}

/* ── side panel: fields, examples, saved, history ── */

function SidePanel({ onPick, onRun, hist, onClearHist }: { onPick: (q: string) => void; onRun: (q: string) => void; hist: BqlHistoryEntry[]; onClearHist: () => void }) {
  return (
    <Panel flush className="h-full">
      <Tabs defaultValue="examples">
        <TabsList className="px-2">
          <TabsTrigger value="examples">Examples</TabsTrigger>
          <TabsTrigger value="fields">Fields</TabsTrigger>
          <TabsTrigger value="saved">Saved</TabsTrigger>
          <TabsTrigger value="history">History</TabsTrigger>
        </TabsList>
        <TabsContent value="examples" className="max-h-[720px] space-y-1 overflow-y-auto p-2">
          {EXAMPLES.map((e) => (
            <button key={e.title} type="button" onClick={() => onPick(e.query)} onDoubleClick={() => onRun(e.query)} className="block w-full rounded-sm border border-border px-2 py-1.5 text-left hover:border-border-strong hover:bg-surface-2">
              <div className="text-xs font-medium text-fg">{e.title}</div>
              <div className="num mt-0.5 line-clamp-2 text-2xs text-muted">{e.query.replace(/\s+/g, ' ')}</div>
            </button>
          ))}
          <p className="px-1 pt-1 text-2xs text-faint">Click to load · double-click to run</p>
        </TabsContent>
        <TabsContent value="fields" className="p-2">
          <FieldBrowser />
        </TabsContent>
        <TabsContent value="saved" className="max-h-[720px] overflow-y-auto p-2">
          <SavedList onPick={onPick} onRun={onRun} />
        </TabsContent>
        <TabsContent value="history" className="max-h-[720px] space-y-1 overflow-y-auto p-2">
          {hist.length === 0 ? (
            <p className="text-xs text-muted">Queries you run appear here (this browser only).</p>
          ) : (
            <>
              {hist.map((h) => (
                <button key={h.at + h.q} type="button" onClick={() => onPick(h.q)} className="block w-full rounded-sm px-2 py-1 text-left hover:bg-surface-2">
                  <div className="num line-clamp-2 text-2xs text-fg">{h.q.replace(/\s+/g, ' ')}</div>
                  <div className="text-2xs text-faint">{h.at ? fmtDateTime(h.at) : 'time unknown'}</div>
                </button>
              ))}
              <Button size="xs" variant="ghost" onClick={onClearHist}>
                Clear history
              </Button>
            </>
          )}
        </TabsContent>
      </Tabs>
    </Panel>
  )
}

function FieldBrowser() {
  const [q, setQ] = useState('')
  const [group, setGroup] = useState('')
  const f = useBqlFields(q, group, 60)
  return (
    <div className="space-y-2">
      <div className="flex gap-1">
        <Input aria-label="Search fields" placeholder="Search fields (pe, hi52, roic …)" className="min-w-0 flex-1" value={q} onChange={(e) => setQ(e.target.value)} />
        <Select label="Field group" value={group} onChange={setGroup} options={[{ value: '', label: 'All' }, ...(f.data?.groups ?? []).map((g) => ({ value: g, label: g }))]} />
      </div>
      {f.isPending ? (
        <SkeletonRows rows={8} />
      ) : f.isError ? (
        <ErrorState error={f.error} onRetry={() => void f.refetch()} />
      ) : (
        <>
          <div className="text-2xs text-muted">
            {fmtInt(f.data.total)} of {fmtInt(f.data.counts.fields)} fields · {fmtInt(f.data.counts.with_history)} with point-in-time history
          </div>
          <ul className="max-h-[560px] divide-y divide-border overflow-y-auto rounded-sm border border-border">
            {f.data.items.map((x) => (
              <li key={x.mnemonic} className="px-2 py-1" title={x.pit}>
                <div className="flex items-center gap-1.5">
                  <span className="num text-xs font-medium text-fg">{x.mnemonic}</span>
                  <span className="text-2xs text-faint">{x.unit}</span>
                  {x.has_history && <Badge tone="info" className="h-4 px-1">PIT</Badge>}
                  {x.model_based && <Badge tone="warn" className="h-4 px-1">model</Badge>}
                </div>
                <div className="line-clamp-2 text-2xs text-muted">{x.description}</div>
                {x.aliases.length > 0 && <div className="num text-2xs text-faint">aka {x.aliases.join(', ')}</div>}
              </li>
            ))}
          </ul>
          <details className="text-2xs text-muted">
            <summary className="cursor-pointer text-xs text-fg">
              <BookOpen className="mr-1 inline size-3" /> Functions
            </summary>
            <ul className="mt-1 space-y-1">
              {f.data.functions.map((fn) => (
                <li key={fn.signature}>
                  <span className="num text-fg">{fn.signature}</span> — {fn.description}
                </li>
              ))}
            </ul>
          </details>
        </>
      )}
    </div>
  )
}

function SavedList({ onPick, onRun }: { onPick: (q: string) => void; onRun: (q: string) => void }) {
  const s = useBqlSaved()
  const { remove } = useBqlSavedMutations()
  if (s.isPending) return <SkeletonRows rows={4} />
  if (s.isError) return <ErrorState error={s.error} onRetry={() => void s.refetch()} />
  if (!s.data.length) return <p className="text-xs text-muted">No saved queries. Use Save next to Run; kind “screen” makes a query usable as members('screen:NAME').</p>
  return (
    <ul className="space-y-1">
      {s.data.map((x) => (
        <li key={x.query_id} className="rounded-sm border border-border px-2 py-1.5">
          <div className="flex items-center gap-1.5">
            <button type="button" className="min-w-0 flex-1 truncate text-left text-xs font-medium text-fg hover:text-accent-text" onClick={() => onPick(x.query)} onDoubleClick={() => onRun(x.query)}>
              {x.name}
            </button>
            <Badge tone={x.kind === 'screen' ? 'info' : 'neutral'} className="h-4 px-1">
              {x.kind}
            </Badge>
            <Button size="icon-sm" variant="ghost" aria-label={`Delete ${x.name}`} onClick={() => remove.mutate(x.query_id)}>
              <Trash2 />
            </Button>
          </div>
          <div className="num line-clamp-2 text-2xs text-muted">{x.query.replace(/\s+/g, ' ')}</div>
        </li>
      ))}
    </ul>
  )
}
