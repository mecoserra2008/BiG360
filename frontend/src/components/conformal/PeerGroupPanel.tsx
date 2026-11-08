import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { memberRows, pctTone, scoreHistory, weightsParam } from '@/api/conformal/adapters'
import { usePairBand, usePeerGroups } from '@/api/conformal/hooks'
import type { CfPeerGroupDetail, CfPeerGroupSummary, PeerGroupQuery } from '@/api/conformal/types'
import { Sparkline } from '@/components/charts/Sparkline'
import { Badge } from '@/components/ui/badge'
import { ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { Panel, Stat } from '@/components/ui/panel'
import { fmtDate, fmtNum, fmtPct } from '@/lib/format'
import { securityRoute } from '@/lib/routes'
import { cn } from '@/lib/utils'
import { ModelBasedNote } from './notes'

const pctLabel = (p: number | null | undefined) => (p == null ? '—' : `${(p * 100).toFixed(1)}th pct`)

/**
 * Peer-group joint anomaly (GCN-GRU + ACI ellipsoid; passed the validation gate as a DESCRIPTIVE measure): today's
 * calibrated percentile, its history, member contributions, the break-away member and a pair / spread band tool.
 * Never framed as a trading signal (the break-away fade had no economic value in validation).
 */
export function PeerGroupPanel({ query, title = 'Peer-group joint anomaly', className }: { query: PeerGroupQuery; title?: string; className?: string }) {
  const list = usePeerGroups(query)
  const [picked, setPicked] = useState<string | null>(null)
  const groups = list.data?.groups ?? []
  const defaultId = list.data?.detail?.group_id ?? groups[0]?.group_id ?? null
  const gid = picked && groups.some((g) => g.group_id === picked) ? picked : defaultId
  const needDetail = !!gid && gid !== list.data?.detail?.group_id
  const det = usePeerGroups({ groupId: gid ?? undefined }, needDetail)
  const detail: CfPeerGroupDetail | null | undefined = needDetail ? det.data?.detail : list.data?.detail

  const body = () => {
    if (list.isPending) return <SkeletonRows className="p-3" rows={6} />
    if (list.isError) return <ErrorState error={list.error} onRetry={() => void list.refetch()} />
    if (list.data?.data_status === 'pending') return <PendingState compact detail="Peer-group scores are not in the serving snapshot yet." />
    if (!groups.length)
      return <p className="px-3 py-4 text-xs text-muted">This selection is not part of a scored peer group (groups hold 20–50 liquid names of one subsector or supply-chain cluster per listing region).</p>
    return (
      <div className="space-y-3 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select
            label="Peer group"
            value={gid ?? ''}
            onChange={(v) => setPicked(v)}
            options={groups.map((g) => ({ value: g.group_id, label: `${g.label} · ${g.kind === 'industry' ? 'industry' : 'supply chain'} · ${pctLabel(g.score_pct)}` }))}
            className="w-[360px] max-w-full"
          />
          <span className="text-2xs text-muted">{groups.length} group{groups.length > 1 ? 's' : ''} · scored {fmtDate(list.data?.as_of)} · {list.data?.filter ?? ''} + {list.data?.radius_mode ?? ''}</span>
        </div>
        {needDetail && det.isPending ? <SkeletonRows rows={5} /> : detail ? <GroupDetail d={detail} focus={query.securityId} /> : null}
        {groups.length > 1 && <GroupList groups={groups} current={gid} onPick={setPicked} />}
      </div>
    )
  }
  return (
    <Panel
      title={title}
      subtitle="How unusual today's joint move of the peer group was vs a calibrated model of its co-movement"
      className={className}
      flush
      actions={<Badge tone="info" title="Descriptive, model-based; not a trading signal">descriptive · model-based · not a trading signal</Badge>}
    >
      {body()}
      <ModelBasedNote className="border-t border-border px-3 py-2">{list.data?.labels?.peer ?? 'Descriptive, model-based; not a trading signal.'}</ModelBasedNote>
    </Panel>
  )
}

function GroupDetail({ d, focus }: { d: CfPeerGroupDetail; focus?: string }) {
  const rows = useMemo(() => memberRows(d), [d])
  const hist = useMemo(() => scoreHistory(d), [d])
  const maxShare = Math.max(0.0001, ...rows.map((r) => Math.abs(r.share ?? 0)))
  const tone = pctTone(d.score_pct)
  const anomalies = (d.history ?? []).filter((h) => h.anomaly_90).length
  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <Stat
          label="Joint-move percentile"
          value={<span className={cn(tone === 'down' ? 'text-down' : tone === 'warn' ? 'text-warn' : 'text-fg')}>{d.score_pct == null ? '—' : `${(d.score_pct * 100).toFixed(1)}%`}</span>}
          sub={d.anomaly_95 ? 'beyond the 95% radius' : d.anomaly_90 ? 'beyond the 90% radius' : 'inside the 90% radius'}
        />
        <Stat label="Group move" value={fmtPct(d.group_move == null ? null : Math.expm1(d.group_move), 2, true)} sub={`${d.n_scored ?? d.n_members} members · ${fmtDate(d.as_of)}`} />
        <Stat
          label="Break-away member"
          value={d.breakaway_member ? <Link className="hover:text-accent-text hover:underline" to={securityRoute(d.breakaway_member.security_id)}>{d.breakaway_member.ticker}</Link> : '—'}
          sub={d.breakaway_z == null ? undefined : `z | rest of group ${fmtNum(d.breakaway_z, 2)}`}
        />
        <Stat
          label="Top contributor"
          value={d.top_member ? <Link className="hover:text-accent-text hover:underline" to={securityRoute(d.top_member.security_id)}>{d.top_member.ticker}</Link> : '—'}
          sub={d.top_share == null ? undefined : `${fmtPct(d.top_share, 0)} of the score`}
        />
      </dl>
      <div className="flex flex-wrap items-center gap-3 rounded-md border border-border bg-surface-2 px-2 py-1.5">
        <span className="label-caps">Percentile, last year</span>
        <Sparkline data={hist} width={260} height={34} tone="accent" area ariaLabel="Peer-group joint-anomaly percentile history" />
        <span className="text-2xs text-muted">{anomalies} session{anomalies === 1 ? '' : 's'} beyond the 90% radius of {d.history.length} (≈10% expected when calibrated)</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm" aria-label="Member contributions">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="h-7 border-b border-border px-2 text-left font-semibold">Member</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold">Move</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold" title="Own standardised move">z own</th>
              <th scope="col" className="border-b border-border px-2 text-right font-semibold" title="Move given the other members (break-away measure)">z | group</th>
              <th scope="col" className="border-b border-border px-2 text-left font-semibold" title="Exact additive share of the joint score; negative = offsets the group">Share of score</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 12).map((r) => (
              <tr key={r.securityId} className={cn('hover:bg-surface-2', r.securityId === focus && 'bg-accent-soft')}>
                <td className="h-[26px] border-b border-border px-2">
                  <Link to={securityRoute(r.securityId)} className="num font-semibold hover:text-accent-text hover:underline">{r.ticker}</Link>
                  {r.isBreakaway && <Badge tone="warn" className="ml-1.5">break-away</Badge>}
                  {r.isTop && !r.isBreakaway && <Badge tone="neutral" className="ml-1.5">top</Badge>}
                </td>
                <td className={cn('num border-b border-border px-2 text-right', (r.r ?? 0) > 0 ? 'text-up' : (r.r ?? 0) < 0 ? 'text-down' : 'text-muted')}>{r.r == null ? '—' : fmtPct(Math.expm1(r.r), 1, true)}</td>
                <td className="num border-b border-border px-2 text-right">{fmtNum(r.zMarg, 2)}</td>
                <td className={cn('num border-b border-border px-2 text-right', Math.abs(r.zCond ?? 0) >= 2.5 && 'font-semibold text-warn')}>{fmtNum(r.zCond, 2)}</td>
                <td className="border-b border-border px-2">
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-1.5 w-20 overflow-hidden rounded-full bg-surface-3">
                      <span className={cn('block h-full rounded-full', (r.share ?? 0) < 0 ? 'bg-faint' : 'bg-accent')} style={{ width: `${Math.min(100, (Math.abs(r.share ?? 0) / maxShare) * 100)}%` }} />
                    </span>
                    <span className="num text-2xs text-muted">{r.share == null ? '—' : fmtPct(r.share, 0)}</span>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length > 12 && <p className="px-2 pt-1 text-2xs text-faint">Top 12 of {rows.length} members by contribution.</p>}
      </div>
      <PairBandTool d={d} focus={focus} />
    </div>
  )
}

function GroupList({ groups, current, onPick }: { groups: CfPeerGroupSummary[]; current: string | null; onPick: (g: string) => void }) {
  return (
    <details className="rounded-md border border-border">
      <summary className="cursor-pointer px-2 py-1.5 text-xs text-muted hover:text-fg">All groups in this selection ({groups.length}), by today's percentile</summary>
      <ul className="max-h-56 overflow-y-auto">
        {groups.map((g) => (
          <li key={g.group_id}>
            <button type="button" onClick={() => onPick(g.group_id)} className={cn('flex h-7 w-full items-center gap-2 px-2 text-left text-xs hover:bg-surface-3', g.group_id === current && 'bg-accent-soft')}>
              <span className="min-w-0 flex-1 truncate">{g.label}</span>
              <span className="text-2xs text-faint">{g.kind === 'industry' ? 'industry' : 'supply chain'} · {g.n_members}</span>
              <Badge tone={pctTone(g.score_pct)}>{g.score_pct == null ? '—' : `${Math.round(g.score_pct * 100)}`}</Badge>
            </button>
          </li>
        ))}
      </ul>
    </details>
  )
}

function PairBandTool({ d, focus }: { d: CfPeerGroupDetail; focus?: string }) {
  const members = d.members
  const firstLeg = focus && members.some((m) => m.security_id === focus) ? focus : (members[0]?.security_id ?? '')
  const other = d.breakaway_member?.security_id && d.breakaway_member.security_id !== firstLeg ? d.breakaway_member.security_id : members.find((m) => m.security_id !== firstLeg)?.security_id ?? ''
  const [mode, setMode] = useState<'pair' | 'basket'>('pair')
  const [i, setI] = useState(firstLeg)
  const [j, setJ] = useState(other)
  const [beta, setBeta] = useState('')
  const [wtxt, setWtxt] = useState('')
  const iSel = members.some((m) => m.security_id === i) ? i : firstLeg
  const jSel = members.some((m) => m.security_id === j) && j !== iSel ? j : members.find((m) => m.security_id !== iSel)?.security_id ?? ''
  const byTicker = useMemo(() => new Map(members.map((m) => [m.ticker.toUpperCase(), m.security_id])), [members])
  const weights = useMemo(() => {
    if (mode !== 'basket') return undefined
    const rows = wtxt
      .split(/[,;\n]/)
      .map((x) => x.trim())
      .filter(Boolean)
      .map((x) => {
        const [k, w] = x.split(/[:=\s]+/)
        return { securityId: byTicker.get((k ?? '').toUpperCase()) ?? k ?? '', weight: Number((w ?? '').replace(',', '.')) }
      })
    return weightsParam(rows) || undefined
  }, [mode, wtxt, byTicker])
  const b = beta.trim() === '' ? null : Number(beta.replace(',', '.'))
  const q = usePairBand({ groupId: d.group_id, i: iSel, j: jSel, beta: b, weights }, mode === 'pair' || !!weights)
  const opts = members.map((m) => ({ value: m.security_id, label: m.ticker }))
  const r = q.data
  return (
    <div className="rounded-md border border-border p-2">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="label-caps">Pair / spread band (next session)</span>
        <Segmented label="Band tool mode" size="xs" value={mode} onChange={setMode} options={[{ value: 'pair', label: 'Pair' }, { value: 'basket', label: 'Basket' }]} />
        {mode === 'pair' ? (
          <>
            <Select label="Long leg" value={iSel} onChange={setI} options={opts} className="w-28" />
            <span className="text-xs text-muted">−β ×</span>
            <Select label="Short leg" value={jSel} onChange={setJ} options={opts.filter((o) => o.value !== iSel)} className="w-28" />
            <Input aria-label="Hedge ratio beta (blank = minimum variance)" placeholder="β auto" inputMode="decimal" value={beta} onChange={(e) => setBeta(e.target.value)} className="w-20" />
          </>
        ) : (
          <Input aria-label="Basket weights (TICKER:weight, comma-separated)" placeholder="AAA:0.5, BBB:0.5, CCC:-1" value={wtxt} onChange={(e) => setWtxt(e.target.value)} className="w-72" />
        )}
      </div>
      {q.isError ? (
        <p className="text-xs text-down">{(q.error as Error)?.message ?? 'Could not compute the band'}</p>
      ) : !r ? (
        <p className="text-xs text-muted">{mode === 'basket' && !weights ? 'Enter member weights, e.g. AAA:0.5, BBB:0.5.' : 'Computing…'}</p>
      ) : (
        <div className="space-y-1 text-xs" data-testid="pair-band">
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
            {r.beta != null && <span>β = <span className="num font-semibold">{fmtNum(r.beta, 3)}</span>{beta.trim() === '' ? ' (min-variance)' : ''}</span>}
            <span className="text-muted">model sd {fmtPct(r.sd, 2)}</span>
            {r.realised && (
              <span>
                today {fmtPct(r.realised.value == null ? null : Math.expm1(r.realised.value), 2, true)}
                {r.realised.z != null ? ` (z ${fmtNum(r.realised.z, 2)})` : ''}
              </span>
            )}
          </div>
          {(r.marginal?.bands.length ?? 0) > 0 && (
            <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
              <span className="label-caps" title={r.labels?.marginal}>This combination</span>
              {r.marginal!.bands.map((x) => (
                <span key={x.level}>
                  {x.level}%: <span className="num font-semibold">{fmtPct(x.lo_pct, 1, true)} … {fmtPct(x.hi_pct, 1, true)}</span>
                </span>
              ))}
              <span className="text-2xs text-faint">calibrated on its last {r.marginal!.n_cal} sessions</span>
            </div>
          )}
          <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-muted">
            <span className="label-caps" title={r.labels?.pair}>Simultaneous (joint set)</span>
            {r.bands.map((x) => (
              <span key={x.level}>
                {x.level}%: <span className="num">{fmtPct(x.lo_pct, 1, true)} … {fmtPct(x.hi_pct, 1, true)}</span>
              </span>
            ))}
            {r.realised && (
              <Badge tone={r.realised.inside_90 === false ? 'warn' : 'neutral'}>today {r.realised.inside_90 === false ? (r.realised.inside_95 === false ? 'outside 95%' : 'outside 90%') : 'inside'}</Badge>
            )}
          </div>
        </div>
      )}
      <p className="mt-1.5 text-2xs text-faint">
        Next-session bands of the combination (log return), law as of {fmtDate(r?.as_of)}. "This combination" = split-conformal band calibrated on the combination's own recent moves (short window, not separately validated);
        "simultaneous" = projection of the group's validated joint set (conservative: holds for all combinations at once). Descriptive, not a trade signal.
      </p>
    </div>
  )
}
