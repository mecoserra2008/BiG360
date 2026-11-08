import { CalendarClock, Plus } from 'lucide-react'
import { useState, type DragEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { AUTO_META, BOARD, CLOSE_REASONS, daysUntil, DIRECTION_META, groupBoard, MOVE_LABEL, moves, SUBJECT_LABEL } from '@/api/theses/adapters'
import { useMoveThesis, useTheses, useThesisStats } from '@/api/theses/hooks'
import type { AutoStatus, BoardStatus, CloseReason, Direction, SubjectType, Thesis, ThesisFilters, ThesisStats } from '@/api/theses/types'
import { Badge } from '@/components/ui/badge'
import { Button, buttonVariants } from '@/components/ui/button'
import { EmptyState, ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Select } from '@/components/ui/inputs'
import { PageHeader, Panel, Stat } from '@/components/ui/panel'
import { toast } from '@/components/ui/toast'
import { DASH, fmtDate, fmtInt, fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { AutoStatusBadge, ConvictionDots } from './shared'

const opt = <V extends string>(all: string, m: Record<V, { label: string } | string>) => [{ value: '' as V | '', label: all }, ...(Object.keys(m) as V[]).map((k) => ({ value: k, label: typeof m[k] === 'string' ? (m[k] as string) : (m[k] as { label: string }).label }))]

export default function ThesisBoard() {
  const [sp, setSp] = useSearchParams()
  const f: ThesisFilters = {
    subjectType: (sp.get('subject_type') as SubjectType) || '',
    direction: (sp.get('direction') as Direction) || '',
    autoStatus: (sp.get('auto_status') as AutoStatus) || '',
    q: sp.get('q') ?? '',
    tag: sp.get('tag') ?? '',
  }
  const set = (k: string, v: string) => setSp((p) => { if (v) p.set(k, v); else p.delete(k); return p }, { replace: true })
  const q = useTheses(f)
  const stats = useThesisStats()
  return (
    <div className="space-y-3">
      <PageHeader title="Thesis tracker" subtitle="Decision log with accountability: linked alerts, automatic status, performance vs benchmark, reviews and hit rates">
        <Link to="/theses/new" className={buttonVariants({ variant: 'primary', size: 'sm' })}>
          <Plus /> New thesis
        </Link>
      </PageHeader>
      {stats.data && <StatsStrip s={stats.data} />}
      <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Filters">
        <Input aria-label="Search theses" placeholder="Search title / subject / text…" value={f.q} onChange={(e) => set('q', e.target.value)} className="w-60" />
        <Select label="Subject type" value={f.subjectType ?? ''} onChange={(v) => set('subject_type', v)} options={opt('All subjects', SUBJECT_LABEL)} />
        <Select label="Direction" value={f.direction ?? ''} onChange={(v) => set('direction', v)} options={opt('All directions', DIRECTION_META)} />
        <Select label="Automatic status" value={f.autoStatus ?? ''} onChange={(v) => set('auto_status', v)} options={opt('Any status', AUTO_META)} />
        {!!q.data?.tags.length && (
          <Select label="Tag" value={f.tag ?? ''} onChange={(v) => set('tag', v)} options={[{ value: '', label: 'All tags' }, ...q.data.tags.map((t) => ({ value: t, label: `#${t}` }))]} />
        )}
        {(f.q || f.subjectType || f.direction || f.autoStatus || f.tag) && (
          <Button size="sm" variant="ghost" onClick={() => setSp(new URLSearchParams(), { replace: true })}>
            Clear filters
          </Button>
        )}
      </div>
      {q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} className="rounded-md border border-border bg-surface" />
      ) : !q.data ? (
        <SkeletonRows rows={10} />
      ) : (
        <Kanban items={q.data.items} />
      )}
    </div>
  )
}

function StatsStrip({ s }: { s: ThesisStats }) {
  return (
    <Panel>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-8">
        <Stat label="Ideas" value={fmtInt(s.n_idea)} />
        <Stat label="Active" value={fmtInt(s.n_active)} />
        <Stat label="Closed" value={fmtInt(s.n_closed)} />
        <Stat label="At risk / invalidated" value={`${fmtInt(s.auto_status.at_risk ?? 0)} / ${fmtInt(s.auto_status.invalidated ?? 0)}`} sub="open theses" />
        <Stat label="Hit rate" value={s.overall.hit_rate != null ? fmtPct(s.overall.hit_rate, 0) : DASH} sub={`${fmtInt(s.overall.wins ?? 0)} wins · ${fmtInt(s.overall.losses ?? 0)} losses`} />
        <Stat label="Avg return" value={fmtPct(s.overall.avg_return, 1, true)} sub="direction-signed" />
        <Stat label="Avg excess" value={fmtPct(s.overall.avg_excess, 1, true)} sub="vs benchmark" />
        <Stat label="Avg holding" value={s.avg_days_held != null ? `${fmtNum(s.avg_days_held, 0)} d` : DASH} sub={s.target_hit_rate != null ? `${fmtPct(s.target_hit_rate, 0)} hit target` : undefined} />
      </div>
    </Panel>
  )
}

function Kanban({ items }: { items: Thesis[] }) {
  const cols = groupBoard(items)
  const move = useMoveThesis()
  const [closing, setClosing] = useState<Thesis | null>(null)
  const [over, setOver] = useState<BoardStatus | null>(null)
  const doMove = (t: Thesis, to: BoardStatus) => {
    if (to === t.status) return
    if (!moves(t.status).includes(to)) {
      toast({ tone: 'info', title: `A ${t.status} thesis cannot move to ${to}` })
      return
    }
    if (to === 'closed') {
      setClosing(t)
      return
    }
    move.mutate({ id: t.thesis_id, patch: { status: to } }, { onError: (e) => toast({ tone: 'error', title: 'Move failed', message: (e as Error).message }) })
  }
  if (!items.length)
    return (
      <EmptyState
        className="rounded-md border border-border bg-surface"
        title="No theses yet"
        message="Create one here or from Company 360, the Screener, Model Studio, the Cross-asset dashboard or the Backtest Lab."
        action={
          <Link to="/theses/new" className={buttonVariants({ variant: 'primary', size: 'sm' })}>
            <Plus /> New thesis
          </Link>
        }
      />
    )
  const onDrop = (e: DragEvent, col: BoardStatus) => {
    e.preventDefault()
    setOver(null)
    const t = items.find((x) => x.thesis_id === e.dataTransfer.getData('text/thesis-id'))
    if (t) doMove(t, col)
  }
  return (
    <>
      <div className="grid gap-3 lg:grid-cols-3">
        {BOARD.map((c) => (
          <section
            key={c.id}
            aria-label={`${c.label} column`}
            className={cn('flex min-h-40 flex-col rounded-md border border-border bg-surface', over === c.id && 'border-accent')}
            onDragOver={(e) => {
              e.preventDefault()
              setOver(c.id)
            }}
            onDragLeave={() => setOver(null)}
            onDrop={(e) => onDrop(e, c.id)}
          >
            <header className="flex items-center gap-2 border-b border-border px-3 py-2">
              <h2 className="text-sm font-semibold">{c.label}</h2>
              <Badge>{cols[c.id].length}</Badge>
              <span className="truncate text-2xs text-muted">{c.hint}</span>
            </header>
            <ul className="flex flex-col gap-2 p-2">
              {cols[c.id].map((t) => (
                <Card key={t.thesis_id} t={t} onMove={doMove} />
              ))}
              {!cols[c.id].length && <li className="px-2 py-6 text-center text-xs text-faint">Drop a thesis here</li>}
            </ul>
          </section>
        ))}
      </div>
      {closing && <CloseDialog t={closing} onDone={() => setClosing(null)} />}
    </>
  )
}

function Card({ t, onMove }: { t: Thesis; onMove: (t: Thesis, to: BoardStatus) => void }) {
  const r = t.card?.return_since_inception
  const due = daysUntil(t.next_review_date)
  const top = (t.status_reasons ?? []).find((x) => x.level === 'invalid' || x.level === 'risk')
  return (
    <li
      draggable
      onDragStart={(e) => e.dataTransfer.setData('text/thesis-id', t.thesis_id)}
      className="group rounded-md border border-border bg-surface-2/40 p-2.5 hover:border-border-strong"
    >
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <Link to={`/theses/${encodeURIComponent(t.thesis_id)}`} className="line-clamp-2 text-sm font-semibold text-fg hover:text-accent-text hover:underline">
            {t.title}
          </Link>
          <div className="mt-0.5 truncate text-xs text-muted" title={t.subject_label ?? t.subject_id}>
            {SUBJECT_LABEL[t.subject_type]} · {t.subject_label ?? t.subject_id}
          </div>
        </div>
        {t.status !== 'closed' ? <AutoStatusBadge status={t.auto_status} /> : t.outcome ? <Badge tone={t.outcome === 'win' ? 'up' : t.outcome === 'loss' ? 'down' : 'neutral'}>{t.outcome}</Badge> : null}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <Badge tone={DIRECTION_META[t.direction].tone}>{DIRECTION_META[t.direction].label}</Badge>
        <span className="text-muted">{t.horizon}</span>
        <ConvictionDots value={t.conviction} />
        <span className={cn('num', r == null ? 'text-muted' : r >= 0 ? 'text-up' : 'text-down')} title="Direction-signed return since inception (closes)">
          {r == null ? DASH : fmtPct(r, 1, true)}
        </span>
        {t.status !== 'closed' && due != null && (
          <span className={cn('inline-flex items-center gap-1', due < 0 ? 'text-warn' : 'text-muted')} title={`Next review ${fmtDate(t.next_review_date)}`}>
            <CalendarClock className="size-3" aria-hidden />
            {due < 0 ? `review ${-due}d overdue` : `review in ${due}d`}
          </span>
        )}
      </div>
      {top && t.status !== 'closed' && <p className="mt-1.5 line-clamp-2 text-2xs text-muted">{top.message}</p>}
      <div className="mt-2 flex flex-wrap gap-1 opacity-80 group-hover:opacity-100">
        {moves(t.status).map((to) => (
          <Button key={to} size="xs" variant="ghost" onClick={() => onMove(t, to)} aria-label={`${MOVE_LABEL[to]}: ${t.title}`}>
            {MOVE_LABEL[to]}
          </Button>
        ))}
        {!!t.tags?.length && <span className="ml-auto truncate text-2xs text-faint">{t.tags.map((x) => `#${x}`).join(' ')}</span>}
      </div>
    </li>
  )
}

/** Close with a reason (the server records exit price, outcome, performance snapshot and a post-mortem template). */
export function CloseDialog({ t, onDone }: { t: Thesis; onDone: (closed: boolean) => void }) {
  const move = useMoveThesis()
  const [reason, setReason] = useState<CloseReason>('other')
  const [outcome, setOutcome] = useState<'' | 'win' | 'loss' | 'flat'>('')
  return (
    <div role="dialog" aria-modal="true" aria-label={`Close thesis ${t.title}`} className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-md border border-border bg-surface p-4 shadow-lg">
        <h2 className="text-md font-semibold">Close “{t.title}”</h2>
        <p className="mt-1 text-xs text-muted">The exit price, the performance since inception and a post-mortem template are recorded; linked alert rules are disabled.</p>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <label className="text-xs text-muted">
            Reason
            <Select label="Close reason" value={reason} onChange={setReason} options={CLOSE_REASONS} className="mt-1 w-full" />
          </label>
          <label className="text-xs text-muted">
            Outcome
            <Select label="Outcome" value={outcome} onChange={setOutcome} options={[{ value: '', label: 'From the return' }, { value: 'win', label: 'Win' }, { value: 'loss', label: 'Loss' }, { value: 'flat', label: 'Flat' }]} className="mt-1 w-full" />
          </label>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={() => onDone(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            disabled={move.isPending}
            onClick={() =>
              move.mutate(
                { id: t.thesis_id, patch: { status: 'closed', close_reason: reason, ...(outcome ? { outcome } : {}) } },
                {
                  onSuccess: () => {
                    toast({ tone: 'success', title: 'Thesis closed', message: 'A post-mortem template was added to the thesis.' })
                    onDone(true)
                  },
                  onError: (e) => toast({ tone: 'error', title: 'Close failed', message: (e as Error).message }),
                },
              )
            }
          >
            Close thesis
          </Button>
        </div>
      </div>
    </div>
  )
}
