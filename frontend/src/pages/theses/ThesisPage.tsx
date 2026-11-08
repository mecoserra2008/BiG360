import { useQuery } from '@tanstack/react-query'
import { Activity, Camera, Download, ExternalLink, FileText, Pencil, Printer, RotateCw, Trash2 } from 'lucide-react'
import { useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { macroApi } from '@/api/macro/client'
import { CLOSE_REASONS, daysUntil, kpiCondition, kpiSource, kpiState, MOVE_LABEL, moves, ruleLabel, SUBJECT_LABEL } from '@/api/theses/adapters'
import { thesesApi } from '@/api/theses/client'
import { useThesis, useThesisEvidence, useThesisJournal, useThesisMutations, useThesisPerformance, useThesisReviews, useThesisStatus, useThesisTimeline } from '@/api/theses/hooks'
import type { ReviewInput, ThesisDetail, ThesisJournalLink, ThesisKpi, ThesisReason } from '@/api/theses/types'
import { ThesisTouchPanel } from '@/components/conformal/ThesisConformal'
import { HelpLink } from '@/components/layout/HelpLink'
import { ThesisSetupProbPanel } from '@/components/techprob/ThesisSetupProb'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { EmptyState, ErrorState, PendingState, Skeleton, SkeletonRows } from '@/components/ui/feedback'
import { Input, Select } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { DESCRIPTIVE_NOTE } from '@/lib/compositeLabel'
import { DASH, fmtDate, fmtDateTime, fmtNum, fmtPct, fmtPrice } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { cn } from '@/lib/utils'
import { useUi } from '@/store'
import { CloseDialog } from './Board'
import { AutoStatusBadge, ConvictionDots, DirectionBadge, Markdown, ReasonList, textareaCls } from './shared'
import { ThesisPerfChart, ThesisPriceChart } from './ThesisChart'
import { downloadText, slug } from './util'

const SECTIONS = [
  ['overview', 'Overview'],
  ['kpis', 'KPIs'],
  ['performance', 'Performance'],
  ['thesis', 'Thesis'],
  ['journal', 'Journal'],
  ['reviews', 'Reviews'],
  ['evidence', 'Evidence'],
  ['scenario', 'Scenario'],
  ['alerts', 'Alerts'],
] as const

export default function ThesisPage() {
  const { id = '' } = useParams()
  const q = useThesis(id)
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} className="rounded-md border border-border bg-surface" />
  if (!q.data) return <SkeletonRows rows={14} />
  return <Body t={q.data} />
}

function subjectLink(t: ThesisDetail): string | null {
  if (t.subject_type === 'security') return securityRoute(t.security_id ?? t.subject_id)
  if (t.subject_type === 'sector') return `/sectors?sector=${encodeURIComponent(t.subject_id)}`
  if (t.subject_type === 'country') return `/macro/country/${encodeURIComponent(t.subject_id)}`
  if (t.subject_type === 'macro_theme') {
    const th = t.subject_id.split(':').pop() ?? ''
    return ['growth', 'inflation', 'labor'].includes(th) ? `/macro/theme/${th}` : '/macro'
  }
  return '/macro/cross-asset'
}

function Body({ t }: { t: ThesisDetail }) {
  const id = t.thesis_id
  const nav = useNavigate()
  const m = useThesisMutations(id)
  const [closing, setClosing] = useState(false)
  const theme = useUi((s) => s.theme)
  const setTheme = useUi((s) => s.setTheme)
  const err = (title: string) => (e: unknown) => toast({ tone: 'error', title, message: (e as Error).message })
  const link = subjectLink(t)
  const print = () => {
    const prev = theme
    if (prev === 'dark') setTheme('light')
    const restore = () => {
      if (prev === 'dark') setTheme('dark')
      window.removeEventListener('afterprint', restore)
    }
    window.addEventListener('afterprint', restore)
    setTimeout(() => window.print(), prev === 'dark' ? 400 : 0)
  }
  const exportMd = () =>
    thesesApi
      .exportMarkdown(id)
      .then((md) => downloadText(`${slug(t.title)}.md`, md))
      .catch(err('Export failed'))
  return (
    <div className="space-y-3" data-print-root>
      <section className="rounded-md border border-border bg-surface px-4 py-3" aria-label="Thesis summary">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-[280px] flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight">{t.title}</h1>
              <HelpLink />
              <Badge tone={t.status === 'active' ? 'accent' : 'neutral'}>{t.status}</Badge>
              {t.status !== 'closed' ? <AutoStatusBadge status={t.auto_status} /> : t.outcome && <Badge tone={t.outcome === 'win' ? 'up' : t.outcome === 'loss' ? 'down' : 'neutral'}>{t.outcome}</Badge>}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>
                {SUBJECT_LABEL[t.subject_type]}:{' '}
                {link ? (
                  <Link to={link} className="font-semibold text-fg hover:underline">
                    {t.subject_label ?? t.subject_id}
                  </Link>
                ) : (
                  t.subject_label
                )}
              </span>
              <DirectionBadge direction={t.direction} />
              <span>horizon {t.horizon}{t.horizon_date ? ` (to ${fmtDate(t.horizon_date)})` : ''}</span>
              <ConvictionDots value={t.conviction} />
              <span>created {fmtDate(t.created_at)}</span>
              {t.inception_date && <span>inception {fmtDate(t.inception_date)}</span>}
              {t.closed_at && <span>closed {fmtDate(t.closed_at)} ({CLOSE_REASONS.find((c) => c.value === t.close_reason)?.label ?? t.close_reason})</span>}
              {!!t.tags.length && <span className="text-faint">{t.tags.map((x) => `#${x}`).join(' ')}</span>}
            </div>
          </div>
          <div className="no-print flex flex-wrap items-center gap-1.5">
            {moves(t.status).map((to) => (
              <Button key={to} size="sm" variant={to === 'active' ? 'primary' : 'outline'} disabled={m.update.isPending} onClick={() => (to === 'closed' ? setClosing(true) : m.update.mutate({ status: to }, { onError: err('Move failed') }))}>
                {MOVE_LABEL[to]}
              </Button>
            ))}
            <Link to={`/theses/${encodeURIComponent(id)}/edit`} className={buttonVariants({ variant: 'outline', size: 'sm' })}>
              <Pencil /> Edit
            </Link>
            <Button size="sm" variant="outline" disabled={m.evaluate.isPending} title="Run the nightly evaluator now (stores the status, fires due alerts)" onClick={() => m.evaluate.mutate(undefined, { onSuccess: (s) => toast({ tone: 'success', title: `Status: ${s.auto_status.replace('_', ' ')}`, message: `${s.events_fired ?? 0} alert events fired` }), onError: err('Evaluation failed') })}>
              <RotateCw /> Evaluate
            </Button>
            <Button size="sm" variant="ghost" title="Capture a snapshot (price, benchmarks, score, KPIs)" onClick={() => m.snapshot.mutate(undefined, { onSuccess: () => toast({ tone: 'success', title: 'Snapshot captured' }), onError: err('Snapshot failed') })}>
              <Camera /> Snapshot
            </Button>
            <Button size="sm" variant="ghost" onClick={() => void exportMd()}>
              <Download /> Markdown
            </Button>
            <Button size="sm" variant="ghost" onClick={print} title="Print / save as PDF (light theme)">
              <Printer /> Print / PDF
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => {
                if (window.confirm(`Delete “${t.title}” with its journal, reviews, snapshots and linked alert rules?`))
                  m.remove.mutate(undefined, { onSuccess: () => nav('/theses'), onError: err('Delete failed') })
              }}
            >
              <Trash2 /> Delete
            </Button>
          </div>
        </div>
        <nav aria-label="Thesis sections" className="no-print mt-3 flex flex-wrap gap-x-1 border-t border-border pt-2 text-xs">
          {SECTIONS.map(([k, l]) => (
            <a key={k} href={`#th-${k}`} className="rounded-sm px-2 py-0.5 text-muted hover:bg-surface-2 hover:text-fg">
              {l}
            </a>
          ))}
        </nav>
      </section>
      <Overview t={t} />
      <ThesisTouchPanel t={t} />
      <ThesisSetupProbPanel t={t} />
      <div className="grid gap-3 xl:grid-cols-2">
        <KpiPanel t={t} />
        <PerformancePanel id={id} />
      </div>
      <ThesisText t={t} />
      <div className="grid gap-3 xl:grid-cols-2">
        <JournalPanel id={id} />
        <ReviewsPanel t={t} />
      </div>
      <EvidencePanel id={id} />
      <div className="grid gap-3 xl:grid-cols-2">
        <ScenarioPanel t={t} />
        <AlertsPanel t={t} />
      </div>
      {closing && <CloseDialog t={t} onDone={() => setClosing(false)} />}
    </div>
  )
}

function Overview({ t }: { t: ThesisDetail }) {
  const tl = useThesisTimeline(t.thesis_id)
  const st = useThesisStatus(t.thesis_id)
  const due = daysUntil(t.next_review_date)
  const c = t.card
  return (
    <div id="th-overview" className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Panel title="Price, bands and timeline" subtitle={t.price_id ? `${t.price_id} close (${t.price_currency ?? 'local'}) - journal entries, triggered alerts, catalysts and reviews as markers` : 'No price instrument'}>
        {!t.price_id ? (
          <EmptyState title="No price instrument" message="Macro themes have no default price. Edit the thesis and set a proxy instrument (e.g. ETF:TIP) to get bands and performance." />
        ) : tl.isError ? (
          <ErrorState error={tl.error} />
        ) : !tl.data ? (
          <Skeleton className="h-80" />
        ) : !tl.data.bars.length ? (
          <PendingState detail="No prices for this instrument in the current snapshot." />
        ) : (
          <ThesisPriceChart tl={tl.data} />
        )}
      </Panel>
      {t.status === 'closed' ? (
        <ClosedPanel t={t} />
      ) : (
      <Panel title="Automatic status" subtitle="Model-based rules on the latest serving data" actions={st.data && <AutoStatusBadge status={st.data.auto_status} />}>
        <div className="mb-3 grid grid-cols-2 gap-3">
          <Stat label="Last" value={c?.last_price != null ? fmtPrice(c.last_price) : DASH} sub={c?.price_date ? fmtDate(c.price_date) : undefined} />
          <Stat label="Since inception" value={<span className={cn(c?.return_since_inception == null ? '' : c.return_since_inception >= 0 ? 'text-up' : 'text-down')}>{fmtPct(c?.return_since_inception, 1, true)}</span>} sub={t.inception_price != null ? `from ${fmtPrice(t.inception_price)}` : undefined} />
          <Stat label="Target / stop" value={`${t.target != null ? fmtPrice(t.target) : DASH} / ${t.stop != null ? fmtPrice(t.stop) : DASH}`} sub={t.entry_lo != null || t.entry_hi != null ? `entry ${fmtPrice(t.entry_lo)} - ${fmtPrice(t.entry_hi)}` : undefined} />
          <Stat label="Next review" value={t.next_review_date ? fmtDate(t.next_review_date) : DASH} sub={due == null ? undefined : due < 0 ? `${-due} days overdue` : `in ${due} days`} />
        </div>
        {st.isError ? <ErrorState error={st.error} /> : !st.data ? <SkeletonRows rows={4} /> : <ReasonList reasons={st.data.reasons} />}
        <p className="mt-2 text-2xs text-faint">Evaluated {t.evaluated_at ? fmtDateTime(t.evaluated_at) : 'never'}; the nightly thesis_eval job re-evaluates open theses.</p>
      </Panel>
      )}
    </div>
  )
}

function ClosedPanel({ t }: { t: ThesisDetail }) {
  const close = [...t.snapshots].reverse().find((s) => s.kind === 'close')
  const perf = (close?.extra?.performance ?? {}) as { thesis_return?: number | null; excess_vs_benchmark?: number | null; days?: number | null }
  const reasons = ((close?.extra?.reasons ?? []) as ThesisReason[]).filter((r) => r.level !== 'ok')
  return (
    <Panel title="Outcome" subtitle="Recorded at close (performance from the close snapshot)" actions={t.outcome && <Badge tone={t.outcome === 'win' ? 'up' : t.outcome === 'loss' ? 'down' : 'neutral'}>{t.outcome}</Badge>}>
      <div className="mb-3 grid grid-cols-2 gap-3">
        <Stat label="Close reason" value={CLOSE_REASONS.find((c) => c.value === t.close_reason)?.label ?? DASH} sub={t.closed_at ? fmtDate(t.closed_at) : undefined} />
        <Stat label="Exit / inception" value={`${t.exit_price != null ? fmtPrice(t.exit_price) : DASH} / ${t.inception_price != null ? fmtPrice(t.inception_price) : DASH}`} />
        <Stat label="Thesis return" value={<span className={cn(perf.thesis_return == null ? '' : perf.thesis_return >= 0 ? 'text-up' : 'text-down')}>{fmtPct(perf.thesis_return, 1, true)}</span>} sub={perf.days != null ? `${perf.days} days held` : undefined} />
        <Stat label="vs benchmark" value={fmtPct(perf.excess_vs_benchmark, 1, true)} />
      </div>
      <div className="label-caps mb-1">Status at close</div>
      {close?.auto_status ? (
        <div className="space-y-2">
          <AutoStatusBadge status={close.auto_status} />
          {reasons.length > 0 && <ReasonList reasons={reasons} max={5} />}
        </div>
      ) : (
        <p className="text-xs text-muted">No close snapshot.</p>
      )}
    </Panel>
  )
}

function KpiPanel({ t }: { t: ThesisDetail }) {
  const st = useThesisStatus(t.thesis_id)
  const spec = new Map(t.kpis.map((k) => [k.kpi_id, k]))
  return (
    <Panel id="th-kpis" title="KPI tracker" subtitle="Healthy condition per KPI; values from the latest snapshot (sector / country: median of members)" flush>
      {st.isError ? (
        <ErrorState error={st.error} />
      ) : !st.data ? (
        <SkeletonRows rows={4} className="p-3" />
      ) : !st.data.kpis.length ? (
        <EmptyState title="No KPIs" message="Add KPIs in the editor: each becomes a linked alert and feeds the status." className="py-6" />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-xs" aria-label="KPI tracker">
            <thead className="text-left text-muted">
              <tr className="border-b border-border">
                <th scope="col" className="px-3 py-1.5 font-medium">KPI</th>
                <th scope="col" className="px-2 py-1.5 font-medium">Source</th>
                <th scope="col" className="px-2 py-1.5 text-right font-medium">Value</th>
                <th scope="col" className="px-2 py-1.5 font-medium">Healthy if</th>
                <th scope="col" className="px-2 py-1.5 font-medium">Role</th>
                <th scope="col" className="px-3 py-1.5 font-medium">State</th>
              </tr>
            </thead>
            <tbody>
              {st.data.kpis.map((k) => {
                const s = kpiState(k.ok, k.role)
                return (
                  <tr key={k.kpi_id} className="border-b border-border/60">
                    <td className="px-3 py-1.5 font-medium text-fg">{k.label}</td>
                    <td className="num max-w-[180px] truncate px-2 py-1.5 text-muted" title={`${k.basis}${k.error ? ` - ${k.error}` : ''}`}>
                      {spec.get(k.kpi_id) ? kpiSource(spec.get(k.kpi_id) as ThesisKpi) : k.kind} <span className="text-faint">· {k.basis}</span>
                    </td>
                    <td className="num px-2 py-1.5 text-right">{k.value == null ? DASH : fmtNum(k.value, Math.abs(k.value) < 10 ? 3 : 1)}</td>
                    <td className="num px-2 py-1.5">{kpiCondition({ op: (k.op ?? 'gte') as 'gte', threshold: k.threshold ?? 0, threshold_hi: k.threshold_hi }, (v) => fmtNum(v, 3))}</td>
                    <td className="px-2 py-1.5 text-muted">{k.role}</td>
                    <td className="px-3 py-1.5">
                      <Badge tone={s.tone} title={k.error ?? undefined}>
                        {k.error ? 'Error' : s.label}
                      </Badge>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  )
}

function PerformancePanel({ id }: { id: string }) {
  const p = useThesisPerformance(id)
  const d = p.data
  return (
    <Panel id="th-performance" title="Performance since inception" subtitle={d ? `${fmtDate(d.start_date)} → ${fmtDate(d.end_date)} · ${d.days} days · ${d.basis === 'usd' ? 'in USD (official FX)' : `local currency ${d.currency ?? ''}`} · direction-signed` : undefined}>
      {p.isError ? (
        <ErrorState error={p.error} />
      ) : !d ? (
        <Skeleton className="h-64" />
      ) : d.data_status === 'pending' ? (
        <PendingState compact detail="No prices for the thesis instrument over the period." />
      ) : (
        <>
          <div className="mb-2 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Thesis return" value={<span className={cn(d.thesis_return == null ? '' : d.thesis_return >= 0 ? 'text-up' : 'text-down')}>{fmtPct(d.thesis_return, 1, true)}</span>} sub={d.subject?.id ?? undefined} />
            <Stat label="vs benchmark" value={fmtPct(d.excess_vs_benchmark, 1, true)} sub={d.benchmark ? `${d.benchmark.id} ${fmtPct(d.benchmark.return, 1, true)}` : 'none'} />
            <Stat label="vs sector" value={fmtPct(d.excess_vs_sector, 1, true)} sub={d.sector ? `${d.sector.id} ${fmtPct(d.sector.return, 1, true)}` : 'none'} />
            <Stat label="Max drawdown" value={fmtPct(d.max_drawdown, 1)} sub="of the thesis path" />
          </div>
          {d.series.length > 1 ? <ThesisPerfChart p={d} /> : <p className="text-xs text-muted">The series starts at inception; the chart appears after the next trading day.</p>}
        </>
      )}
    </Panel>
  )
}

function ThesisText({ t }: { t: ThesisDetail }) {
  const m = useThesisMutations(t.thesis_id)
  const [pm, setPm] = useState<string | null>(null)
  return (
    <div id="th-thesis" className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Panel title="Thesis">
        <Markdown text={t.thesis_md} />
        {!!t.key_drivers.length && (
          <div className="mt-3">
            <div className="label-caps mb-1">Key drivers</div>
            <ul className="list-disc space-y-0.5 pl-5 text-sm">
              {t.key_drivers.map((x) => (
                <li key={x}>{x}</li>
              ))}
            </ul>
          </div>
        )}
        {t.status === 'closed' && (
          <div className="mt-4 border-t border-border pt-3">
            <div className="mb-1 flex items-center gap-2">
              <span className="label-caps">Post-mortem</span>
              {pm === null ? (
                <Button size="xs" variant="ghost" className="no-print" onClick={() => setPm(t.post_mortem_md ?? '')}>
                  <Pencil /> Edit
                </Button>
              ) : (
                <>
                  <Button size="xs" variant="primary" onClick={() => m.update.mutate({ post_mortem_md: pm }, { onSuccess: () => setPm(null), onError: (e) => toast({ tone: 'error', title: 'Save failed', message: (e as Error).message }) })}>
                    Save
                  </Button>
                  <Button size="xs" variant="ghost" onClick={() => setPm(null)}>
                    Cancel
                  </Button>
                </>
              )}
            </div>
            {pm === null ? <Markdown text={t.post_mortem_md} /> : <textarea aria-label="Post-mortem (Markdown)" className={`${textareaCls} min-h-64 font-mono`} value={pm} onChange={(e) => setPm(e.target.value)} />}
          </div>
        )}
      </Panel>
      <div className="space-y-3">
        <Panel title="Catalysts">
          {!t.catalysts.length ? (
            <p className="text-xs text-muted">None.</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {[...t.catalysts].sort((a, b) => (a.date ?? '9').localeCompare(b.date ?? '9')).map((c) => {
                const dd = daysUntil(c.date)
                return (
                  <li key={c.catalyst_id ?? c.label} className="flex items-center gap-2">
                    <span className="num w-20 shrink-0 text-muted">{c.date ? fmtDate(c.date) : 'undated'}</span>
                    <span className="flex-1 text-fg">{c.label}</span>
                    <Badge>{c.kind}</Badge>
                    {dd != null && dd >= 0 && dd <= 14 && <Badge tone="accent">in {dd}d</Badge>}
                  </li>
                )
              })}
            </ul>
          )}
        </Panel>
        <Panel title="Invalidation conditions">
          {!t.invalidation.length ? (
            <p className="text-xs text-muted">None (the stop {t.stop != null ? fmtPrice(t.stop) : 'is not set'}).</p>
          ) : (
            <ul className="space-y-1 text-xs">
              {t.invalidation.map((c) => (
                <li key={c.condition_id ?? c.label} className="flex items-center gap-2">
                  <span className="flex-1 text-fg">{c.label}</span>
                  <Badge>{(c.kind ?? 'manual').replace('_', ' ')}</Badge>
                  {c.level != null && <span className="num text-muted">{fmtPrice(c.level)}</span>}
                  {c.date && <span className="num text-muted">{fmtDate(c.date)}</span>}
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <Panel title="Links">
          <ul className="space-y-1 text-xs">
            <li>
              Benchmark <span className="num text-fg">{t.benchmark_id ?? DASH}</span> · sector <span className="num text-fg">{t.sector_benchmark_id ?? DASH}</span>
            </li>
            {t.study_id && (
              <li>
                Studio study <Link className="num text-accent-text hover:underline" to={`/studio?study=${encodeURIComponent(t.study_id)}`}>{t.study_id}</Link>
              </li>
            )}
            {t.studio_run_id && (
              <li>
                Studio run <Link className="num text-accent-text hover:underline" to={`/studio?run=${encodeURIComponent(t.studio_run_id)}`}>{t.studio_run_id}</Link>
              </li>
            )}
            {t.backtest_strategy_id && (
              <li>
                Backtest strategy <Link className="num text-accent-text hover:underline" to={`/backtest?strategy=${encodeURIComponent(t.backtest_strategy_id)}`}>{t.backtest_strategy_id}</Link>
              </li>
            )}
            {t.backtest_run_id && (
              <li>
                Backtest run <Link className="num text-accent-text hover:underline" to={`/backtest?run=${encodeURIComponent(t.backtest_run_id)}`}>{t.backtest_run_id}</Link>
              </li>
            )}
            {t.origin?.kind && t.origin.kind !== 'manual' && <li className="text-muted">Created from {t.origin.kind.replace('_', '-')}{t.origin.ref ? ` (${t.origin.ref})` : ''}</li>}
          </ul>
        </Panel>
      </div>
    </div>
  )
}

const LINK_KINDS = ['url', 'filing', 'story', 'event', 'alert', 'earnings', 'study', 'backtest'] as const

function JournalPanel({ id }: { id: string }) {
  const j = useThesisJournal(id)
  const m = useThesisMutations(id)
  const [body, setBody] = useState('')
  const [date, setDate] = useState('')
  const [lk, setLk] = useState<{ kind: (typeof LINK_KINDS)[number]; label: string; url: string }>({ kind: 'url', label: '', url: '' })
  const add = () => {
    const links: ThesisJournalLink[] = lk.label || lk.url ? [{ kind: lk.kind, label: lk.label || null, ref: lk.kind === 'url' ? null : lk.label || null, url: lk.url || null }] : []
    m.addJournal.mutate({ body_md: body.trim(), entry_date: date || null, links }, {
      onSuccess: () => {
        setBody('')
        setLk({ kind: 'url', label: '', url: '' })
      },
      onError: (e) => toast({ tone: 'error', title: 'Could not add the note', message: (e as Error).message }),
    })
  }
  return (
    <Panel id="th-journal" title="Journal" subtitle="Dated notes (Markdown) with evidence links; board moves and reviews are logged automatically">
      <div className="no-print mb-3 space-y-2 rounded-md border border-border p-2">
        <textarea aria-label="New journal note (Markdown)" className={`${textareaCls} min-h-20`} value={body} onChange={(e) => setBody(e.target.value)} placeholder="What changed? What did you learn?" />
        <div className="flex flex-wrap items-center gap-2">
          <Input aria-label="Note date" type="date" value={date} onChange={(e) => setDate(e.target.value)} className="w-36" />
          <Select label="Link kind" value={lk.kind} onChange={(v) => setLk({ ...lk, kind: v })} options={LINK_KINDS.map((k) => ({ value: k, label: k }))} />
          <Input aria-label="Link label or id" placeholder="label / id" value={lk.label} onChange={(e) => setLk({ ...lk, label: e.target.value })} className="w-36" />
          <Input aria-label="Link URL" placeholder="https://…" value={lk.url} onChange={(e) => setLk({ ...lk, url: e.target.value })} className="min-w-0 flex-1" />
          <Button size="sm" variant="primary" disabled={!body.trim() || m.addJournal.isPending} onClick={add}>
            Add note
          </Button>
        </div>
      </div>
      {j.isError ? (
        <ErrorState error={j.error} />
      ) : !j.data ? (
        <SkeletonRows rows={4} />
      ) : !j.data.length ? (
        <p className="text-xs text-muted">No entries.</p>
      ) : (
        <ol className="space-y-3">
          {j.data.map((e) => (
            <li key={e.entry_id} className="border-l-2 border-border pl-3">
              <div className="flex items-center gap-2 text-2xs text-muted">
                <span className="num">{fmtDate(e.entry_date)}</span>
                <Badge tone={e.kind === 'note' ? 'neutral' : e.kind === 'review' ? 'info' : 'accent'}>{e.kind.replace('_', ' ')}</Badge>
                {e.kind === 'note' && (
                  <button type="button" className="no-print ml-auto text-faint hover:text-down" aria-label="Delete note" onClick={() => window.confirm('Delete this note?') && m.deleteJournal.mutate(e.entry_id)}>
                    <Trash2 className="size-3.5" />
                  </button>
                )}
              </div>
              <Markdown text={e.body_md} className="mt-1 text-sm" />
              {!!e.links.length && (
                <ul className="mt-1 flex flex-wrap gap-2 text-2xs">
                  {e.links.map((l, i) => (
                    <li key={i}>
                      {l.url && /^https?:\/\//.test(l.url) ? (
                        <a href={l.url} target="_blank" rel="noreferrer noopener" className="inline-flex items-center gap-1 text-accent-text hover:underline">
                          <ExternalLink className="size-3" /> {l.label || l.url}
                        </a>
                      ) : (
                        <span className="text-muted">
                          {l.kind}: {l.label ?? l.ref}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  )
}

function ReviewsPanel({ t }: { t: ThesisDetail }) {
  const r = useThesisReviews(t.thesis_id)
  const m = useThesisMutations(t.thesis_id)
  const [f, setF] = useState<ReviewInput>({ decision: 'hold', notes_md: '', conviction_after: null })
  const submit = () =>
    m.review.mutate({ ...f, notes_md: f.notes_md?.trim() || null }, {
      onSuccess: () => {
        setF({ decision: 'hold', notes_md: '', conviction_after: null })
        toast({ tone: 'success', title: 'Review recorded', message: 'Snapshot captured; next review scheduled.' })
      },
      onError: (e) => toast({ tone: 'error', title: 'Review failed', message: (e as Error).message }),
    })
  return (
    <Panel id="th-reviews" title="Reviews & accountability" subtitle={t.review_every_days ? `Every ${t.review_every_days} days · next ${fmtDate(t.next_review_date)}` : 'No review cadence'}>
      {t.status !== 'closed' && (
        <div className="no-print mb-3 space-y-2 rounded-md border border-border p-2">
          <div className="flex flex-wrap items-center gap-2">
            <Select label="Decision" value={f.decision} onChange={(v) => setF({ ...f, decision: v })} options={(['hold', 'add', 'trim', 'close', 'none'] as const).map((x) => ({ value: x, label: x }))} />
            <Select label="Conviction after" value={f.conviction_after ? String(f.conviction_after) : ''} onChange={(v) => setF({ ...f, conviction_after: v ? Number(v) : null })} options={[{ value: '', label: 'Conviction unchanged' }, ...[1, 2, 3, 4, 5].map((n) => ({ value: String(n), label: `Conviction ${n}/5` }))]} />
            <Button size="sm" variant="primary" disabled={m.review.isPending} onClick={submit}>
              <Activity /> Complete review
            </Button>
          </div>
          <textarea aria-label="Review notes (Markdown)" className={`${textareaCls} min-h-16`} value={f.notes_md ?? ''} onChange={(e) => setF({ ...f, notes_md: e.target.value })} placeholder="Is the thesis intact? What would change your mind?" />
        </div>
      )}
      {r.isError ? (
        <ErrorState error={r.error} />
      ) : !r.data ? (
        <SkeletonRows rows={4} />
      ) : (
        <>
          <div className="label-caps mb-1">Reviews</div>
          {!r.data.items.length ? (
            <p className="mb-3 text-xs text-muted">None yet.</p>
          ) : (
            <ul className="mb-3 space-y-1 text-xs">
              {r.data.items.map((x) => (
                <li key={x.review_id} className="flex items-center gap-2">
                  <span className="num w-20 text-muted">{fmtDate(x.completed_at ?? x.due_date)}</span>
                  <Badge tone={x.status === 'completed' ? 'up' : 'neutral'}>{x.status}</Badge>
                  {x.decision && <span className="text-fg">{x.decision}</span>}
                  {x.conviction_after && <span className="text-muted">conviction {x.conviction_after}/5</span>}
                  {x.notes_md && <span className="truncate text-muted" title={x.notes_md}>{x.notes_md}</span>}
                </li>
              ))}
            </ul>
          )}
          <div className="label-caps mb-1">Snapshots</div>
          <div className="overflow-x-auto">
            <table className="w-full text-xs" aria-label="Thesis snapshots">
              <thead className="text-left text-muted">
                <tr className="border-b border-border">
                  <th scope="col" className="py-1 pr-2 font-medium">Taken</th>
                  <th scope="col" className="px-2 py-1 font-medium">Kind</th>
                  <th scope="col" className="px-2 py-1 text-right font-medium">Price</th>
                  <th scope="col" className="px-2 py-1 text-right font-medium">Benchmark</th>
                  {/* coverage v3 A8: the composite score recorded at snapshot time, a descriptive ranking */}
                  <th scope="col" className="px-2 py-1 text-right font-medium" title={`Composite score at snapshot time. ${DESCRIPTIVE_NOTE}`}>Score (descriptive)</th>
                  <th scope="col" className="px-2 py-1 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {r.data.snapshots.map((s) => (
                  <tr key={s.snapshot_id} className="border-b border-border/60">
                    <td className="num py-1 pr-2 text-muted">{fmtDate(s.taken_at)}</td>
                    <td className="px-2 py-1">{s.kind}</td>
                    <td className="num px-2 py-1 text-right">{s.price != null ? fmtPrice(s.price) : DASH}</td>
                    <td className="num px-2 py-1 text-right">{s.benchmark_price != null ? fmtNum(s.benchmark_price, 1) : DASH}</td>
                    <td className="num px-2 py-1 text-right">{s.composite_score != null ? `${fmtNum(s.composite_score, 0)}${s.grade ? ` ${s.grade}` : ''}` : DASH}</td>
                    <td className="px-2 py-1">{s.auto_status ? <AutoStatusBadge status={s.auto_status} /> : DASH}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Panel>
  )
}

function EvidenceList({ title, children, empty }: { title: string; children: ReactNode[]; empty: string }) {
  return (
    <div className="min-w-0">
      <div className="label-caps mb-1">{title}</div>
      {children.length ? <ul className="space-y-1 text-xs">{children}</ul> : <p className="text-xs text-faint">{empty}</p>}
    </div>
  )
}

function EvidencePanel({ id }: { id: string }) {
  const e = useThesisEvidence(id)
  const d = e.data
  return (
    <Panel id="th-evidence" title="Linked evidence" subtitle="Filings, news stories, event shocks (GDELT attention / tone z, model-based), earnings and macro surprises (model-based expectations, not consensus)">
      {e.isError ? (
        <ErrorState error={e.error} />
      ) : !d ? (
        <SkeletonRows rows={5} />
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <EvidenceList title="Filings" empty="No filings in the window.">
            {d.filings.slice(0, 15).map((f) => (
              <li key={String(f.filing_id)} className="flex gap-2">
                <span className="num w-20 shrink-0 text-muted">{fmtDate(f.knowledge_date)}</span>
                {f.url ? (
                  <a href={f.url} target="_blank" rel="noreferrer noopener" className="truncate text-accent-text hover:underline">
                    {f.form_type ?? f.filing_id}
                  </a>
                ) : (
                  <span className="truncate">{f.form_type ?? f.filing_id}</span>
                )}
              </li>
            ))}
          </EvidenceList>
          <EvidenceList title="News stories" empty="No linked stories.">
            {d.stories.slice(0, 12).map((s) => (
              <li key={String(s.story_id)} className="flex gap-2">
                <span className="num w-20 shrink-0 text-muted">{fmtDate(s.last_date)}</span>
                {s.url ? (
                  <a href={s.url} target="_blank" rel="noreferrer noopener" className="line-clamp-2 text-fg hover:underline" title={s.title ?? ''}>
                    {s.title ?? 'story'}
                  </a>
                ) : (
                  <span className="line-clamp-2">{s.title ?? 'story'}</span>
                )}
              </li>
            ))}
          </EvidenceList>
          <EvidenceList title="Event shocks (model-based)" empty="No shocks above |z| 2.5.">
            {d.shocks.slice(0, 12).map((s) => (
              <li key={String(s.date)} className="flex gap-2">
                <span className="num w-20 shrink-0 text-muted">{fmtDate(s.date)}</span>
                <span>
                  attention z {fmtNum(s.attention_z, 1, true)} · tone z {fmtNum(s.tone_z, 1, true)} · {s.n} items
                </span>
              </li>
            ))}
          </EvidenceList>
          <EvidenceList title="Earnings & macro" empty="No earnings or macro surprises.">
            {[
              ...d.upcoming_earnings.map((x, i) => (
                <li key={`u${i}`} className="flex gap-2">
                  <span className="num w-20 shrink-0 text-accent-text">{fmtDate(String(x.expected_date ?? ''))}</span>
                  <span>
                    upcoming {String(x.fiscal_period ?? '')} · {String(x.source_label ?? '')}
                  </span>
                </li>
              )),
              ...d.earnings.slice(0, 6).map((x, i) => (
                <li key={`e${i}`} className="flex gap-2">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(String(x.release_date ?? ''))}</span>
                  <span>
                    {String(x.fiscal_period ?? '')} EPS surprise z {fmtNum(x.eps_surprise_z as number | null, 1, true)} · CAR[-1,+1] {fmtPct(x.car_m1_p1 as number | null, 1, true)}
                  </span>
                </li>
              )),
              ...d.macro_surprises.slice(0, 8).map((x, i) => (
                <li key={`m${i}`} className="flex gap-2">
                  <span className="num w-20 shrink-0 text-muted">{fmtDate(String(x.release_date ?? ''))}</span>
                  <span>
                    {String(x.country ?? '')} {String(x.series_id ?? '')} z {fmtNum(x.surprise_z as number | null, 1, true)}
                  </span>
                </li>
              )),
            ]}
          </EvidenceList>
        </div>
      )}
    </Panel>
  )
}

const REGION_OF: Record<string, string> = { US: 'US', CA: 'CA', GB: 'UK', JP: 'JP', KR: 'KR', CN: 'CN', HK: 'HK', BR: 'LATAM', MX: 'LATAM', CL: 'LATAM', AR: 'LATAM', PE: 'LATAM', CO: 'LATAM' }

function ScenarioPanel({ t }: { t: ThesisDetail }) {
  const sid = t.scenario_id
  const q = useQuery({
    queryKey: ['theses', 'detail', t.thesis_id, 'scenario', sid],
    queryFn: () => macroApi.runScenario({ scenario_id: sid as string, security_ids: t.security_id ? [t.security_id] : undefined, top_n: 5, universe: 'full', weighting: 'cw' }),
    enabled: !!sid,
    staleTime: 300_000,
  })
  const d = q.data
  let subj: { label: string; impact: number | null | undefined; lo?: number | null; hi?: number | null } | null = null
  if (d) {
    if (t.subject_type === 'security') {
      const r = d.securities[0]
      subj = r ? { label: t.subject_label ?? t.subject_id, impact: r.impact, lo: r.impact_lo, hi: r.impact_hi } : null
    } else if (t.subject_type === 'sector') {
      const r = d.sectors.find((x) => x.entity_id === t.subject_id)
      subj = r ? { label: r.name ?? t.subject_id, impact: r.impact, lo: r.impact_lo, hi: r.impact_hi } : null
    } else if (t.subject_type === 'country') {
      const reg = REGION_OF[t.subject_id] ?? 'EU'
      const r = d.regions.find((x) => x.entity_id === reg)
      subj = r ? { label: `Region ${reg} (proxy for ${t.subject_id})`, impact: r.impact, lo: r.impact_lo, hi: r.impact_hi } : null
    } else {
      const r = [...d.instruments_top, ...d.instruments_bottom].find((x) => x.entity_id === t.price_id)
      subj = r ? { label: r.name ?? (t.price_id as string), impact: r.impact, lo: r.impact_lo, hi: r.impact_hi } : null
    }
  }
  return (
    <Panel id="th-scenario" title="Scenario impact (W4)" subtitle="Model-based macro scenario (betas x shocks); linked in the editor" actions={<Link to="/macro/markets?tab=scenarios" className={cn(buttonVariants({ variant: 'ghost', size: 'xs' }), 'no-print')}>Scenario builder</Link>}>
      {!sid ? (
        <EmptyState title="No linked scenario" message="Link a W4 macro scenario in the editor to see its model-based impact on the subject." className="py-6" />
      ) : q.isError ? (
        <ErrorState error={q.error} />
      ) : !d ? (
        <SkeletonRows rows={4} />
      ) : d.data_status === 'pending' ? (
        <PendingState compact detail="Macro exposures are not available in this snapshot." />
      ) : (
        <div className="space-y-2 text-xs">
          <div className="text-sm font-semibold">{d.scenario.name}</div>
          {subj ? (
            <div className="flex items-baseline gap-2">
              <span className="text-muted">{subj.label}:</span>
              <span className={cn('num text-md font-semibold', (subj.impact ?? 0) >= 0 ? 'text-up' : 'text-down')}>{fmtPct(subj.impact, 1, true)}</span>
              {subj.lo != null && subj.hi != null && <span className="num text-muted">90% band {fmtPct(subj.lo, 1, true)} to {fmtPct(subj.hi, 1, true)}</span>}
            </div>
          ) : (
            <p className="text-muted">No estimate for this subject (no macro exposure fitted); universe aggregates below.</p>
          )}
          <ul className="space-y-0.5">
            {d.universe.map((u) => (
              <li key={u.universe_id} className="flex gap-2">
                <span className="w-24 text-muted">{u.universe_id}</span>
                <span className="num">{fmtPct(u.impact, 1, true)}</span>
              </li>
            ))}
          </ul>
          <p className="text-2xs text-faint">{d.model_label}</p>
        </div>
      )}
    </Panel>
  )
}

function AlertsPanel({ t }: { t: ThesisDetail }) {
  return (
    <Panel id="th-alerts" title="Linked alerts" subtitle={`${t.alerts.length} rules generated from the thesis (regenerated on every save; disabled when closed)`}>
      <ul className="mb-3 grid gap-1 text-xs sm:grid-cols-2">
        {t.alerts.map((a) => (
          <li key={a.alert_id} className="flex items-center gap-2">
            <span className={cn('size-1.5 shrink-0 rounded-full', a.enabled ? 'bg-up' : 'bg-surface-3')} aria-hidden />
            <span className="truncate text-fg" title={a.rule_type}>
              {ruleLabel(a, (v) => fmtNum(v, 2))}
            </span>
            {a.last_fired_at && <span className="ml-auto shrink-0 text-2xs text-muted">fired {fmtDate(a.last_fired_at)}</span>}
          </li>
        ))}
      </ul>
      <div className="label-caps mb-1">Recent triggered alerts</div>
      {!t.recent_events.length ? (
        <p className="text-xs text-muted">None yet. Price and data rules are evaluated nightly (alerts_eval, thesis_eval).</p>
      ) : (
        <ul className="space-y-1 text-xs">
          {t.recent_events.map((e) => (
            <li key={e.event_id} className="flex gap-2">
              <span className="num w-20 shrink-0 text-muted">{fmtDate(e.as_of_date)}</span>
              <span className="text-fg">{e.message}</span>
            </li>
          ))}
        </ul>
      )}
      <Link to="/watchlist" className="no-print mt-2 inline-flex items-center gap-1 text-2xs text-muted hover:text-fg">
        <FileText className="size-3" /> All alert events
      </Link>
    </Panel>
  )
}
