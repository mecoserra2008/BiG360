import { ExternalLink } from 'lucide-react'
import { useState } from 'react'
import { useSearchParams } from 'react-router'
import { euChange, regulatorLabel } from '@/api/smartmoney/adapters'
import { useEuShorts } from '@/api/smartmoney/hooks'
import type { SmEuShorts } from '@/api/smartmoney/types'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate, fmtInt, fmtNum } from '@/lib/format'
import { CompanyLink, OfficialTag, Signed, SmartMoneyNav } from './shared'

const REG_OPTS = [{ value: '', label: 'All regulators' }, ...['fca', 'amf', 'cnmv', 'afm', 'cbi', 'fsma', 'fi_se', 'ftno'].map((r) => ({ value: r, label: regulatorLabel(r) }))]
const PAGE = 200

export default function EuShorts() {
  const [sp, setSp] = useSearchParams()
  const regulator = sp.get('regulator') ?? ''
  const holder = sp.get('holder') ?? ''
  const mode = (sp.get('mode') ?? 'current') as 'current' | 'history'
  const ptype = (sp.get('type') ?? '') as '' | 'individual' | 'aggregate'
  const [text, setText] = useState(sp.get('q') ?? '')
  const page = Number(sp.get('page') ?? '0')
  const q = useEuShorts({ regulator, holder, q: sp.get('q') ?? '', current: mode === 'current', positionType: ptype, limit: PAGE, offset: page * PAGE })
  const set = (k: string, v: string) => setSp((p) => { if (v) p.set(k, v); else p.delete(k); p.delete('page'); return p }, { replace: true })
  return (
    <>
      <PageHeader title="EU / UK short-seller register" subtitle="Net short positions at or above 0.5% of issued capital, as published by the competent authorities">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented<'current' | 'history'> label="Register mode" value={mode} onChange={(v) => set('mode', v === 'current' ? '' : v)} options={[{ value: 'current', label: 'Current' }, { value: 'history', label: 'All notifications' }]} />
          <Select label="Regulator" value={regulator} onChange={(v) => set('regulator', v)} options={REG_OPTS} />
          <Select label="Type" value={ptype} onChange={(v) => set('type', v)} options={[{ value: '', label: 'All types' }, { value: 'individual', label: 'Named holders' }, { value: 'aggregate', label: 'Aggregates' }]} />
          <form onSubmit={(e) => { e.preventDefault(); set('q', text.trim()) }}>
            <Input aria-label="Search holder, issuer, ISIN or ticker" placeholder="Holder, issuer, ISIN, ticker" value={text} onChange={(e) => setText(e.target.value)} className="h-8 w-56" />
          </form>
        </div>
      </PageHeader>
      <SmartMoneyNav />
      {holder && (
        <div className="mb-2 flex items-center gap-2 text-xs">
          Holder: <Badge tone="accent">{holder}</Badge>
          <button type="button" className="text-accent-text hover:underline" onClick={() => set('holder', '')}>clear</button>
        </div>
      )}
      {q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <Skeleton className="h-96" /> : q.data.data_status === 'pending' ? <PendingState detail="The EU short-position job has not run for this snapshot yet." /> : <Body d={q.data} page={page} onPage={(n) => setSp((p) => { p.set('page', String(n)); return p }, { replace: true })} onHolder={(h) => set('holder', h)} />}
    </>
  )
}

function Body({ d, page, onPage, onHolder }: { d: SmEuShorts; page: number; onPage: (n: number) => void; onHolder: (h: string) => void }) {
  const pages = Math.max(1, Math.ceil(d.total / PAGE))
  return (
    <div className="space-y-3">
      <div className="grid gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,1fr)]">
        <Panel title={d.current ? 'Current positions' : 'All notifications'} subtitle={<span className="inline-flex flex-wrap items-center gap-2"><OfficialTag label="regulator registers" /><span className="text-2xs text-muted">{fmtInt(d.total)} rows · knowledge date = publication (AMF) or position date + 1-2 business days (estimated)</span></span>} flush>
          {d.items.length ? (
            <div className="overflow-x-auto">
              <table className="w-full text-xs" aria-label="EU and UK net short positions">
                <thead className="text-left text-muted">
                  <tr className="border-b border-border">
                    {['Holder', 'Issuer', 'Regulator', 'Net short', 'Change', 'Position date', 'Published'].map((h, i) => (
                      <th key={h} scope="col" className={`px-2 py-1.5 font-medium ${i >= 3 ? 'text-right' : ''}`}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {d.items.map((r, i) => {
                    const ch = euChange(r)
                    return (
                      <tr key={`${r.regulator}${r.holder_name}${r.isin}${r.position_date}${i}`} className="border-b border-border/60">
                        <td className="max-w-[220px] px-2 py-1">
                          {r.position_type === 'aggregate' ? (
                            <Tip content="Regulator-published aggregate net short position of the issuer (no holder names)"><span tabIndex={0}><Badge tone="info">aggregate</Badge></span></Tip>
                          ) : (
                            <button type="button" className="max-w-full truncate text-left hover:underline" title={r.holder_name} onClick={() => onHolder(r.holder_name)}>{r.holder_name}</button>
                          )}
                        </td>
                        <td className="max-w-[220px] truncate px-2 py-1">
                          {r.ticker && r.security_id ? <CompanyLink ticker={r.ticker} name={r.name} id={r.security_id} /> : <span title={r.isin}>{r.issuer_name ?? r.isin}</span>}
                        </td>
                        <td className="px-2 py-1 text-muted">
                          <a href={r.source_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:underline">{r.regulator_label}<ExternalLink className="size-3" aria-hidden /></a>
                        </td>
                        <td className="num px-2 py-1 text-right">{r.below_threshold ? <span className="text-muted">&lt; 0.5%</span> : r.position_pct != null ? `${fmtNum(r.position_pct, 2)}%` : DASH}</td>
                        <td className="px-2 py-1 text-right">{d.current ? (ch == null ? <Badge tone="info">new</Badge> : <Signed v={ch / 100} invert digits={2} pp />) : <span className="text-faint">{DASH}</span>}</td>
                        <td className="num px-2 py-1 text-right">{fmtDate(r.position_date)}</td>
                        <td className="num px-2 py-1 text-right text-muted">{fmtDate(r.publication_date ?? r.knowledge_date)}{r.knowledge_date_estimated && !r.publication_date ? <span title="estimated publication date"> *</span> : ''}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState title="No positions match" className="py-8" />
          )}
          {pages > 1 && (
            <div className="flex items-center justify-end gap-2 border-t border-border px-3 py-1.5 text-xs">
              <button type="button" className="text-accent-text disabled:text-faint" disabled={page <= 0} onClick={() => onPage(page - 1)}>← previous</button>
              <span className="text-muted">page {page + 1} / {pages}</span>
              <button type="button" className="text-accent-text disabled:text-faint" disabled={page + 1 >= pages} onClick={() => onPage(page + 1)}>next →</button>
            </div>
          )}
        </Panel>
        <div className="space-y-3">
          <Panel title="Most active holders" subtitle="current named positions" flush>
            {d.top_holders.length ? (
              <ul className="divide-y divide-border/60 text-xs">
                {d.top_holders.slice(0, 15).map((h) => (
                  <li key={h.holder_name} className="flex items-center gap-2 px-2 py-1">
                    <button type="button" className="min-w-0 flex-1 truncate text-left hover:underline" title={h.holder_name} onClick={() => onHolder(h.holder_name)}>{h.holder_name}</button>
                    <span className="num text-muted">{h.n_positions}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState className="py-6" />
            )}
          </Panel>
          <Panel title="Registers" subtitle="official sources ingested" flush>
            <ul className="divide-y divide-border/60 text-xs">
              {d.regulators.map((r) => (
                <li key={r.id} className="px-2 py-1">
                  <div className="flex items-center gap-2">
                    <a href={r.url} target="_blank" rel="noreferrer" className="min-w-0 flex-1 truncate font-medium hover:underline">{r.label}</a>
                    <span className="num text-muted">{fmtInt(r.n_positions)}{r.n_aggregates ? ` + ${fmtInt(r.n_aggregates)} agg.` : ''}</span>
                  </div>
                  <div className="text-2xs text-faint">history {fmtDate(r.history_from)} → {fmtDate(r.history_to)}{r.id === 'fca' ? ' · named holders until 10 Jul 2026, aggregates since' : ''}</div>
                </li>
              ))}
            </ul>
          </Panel>
          <Panel title="Not ingested" subtitle="and why" flush>
            <ul className="divide-y divide-border/60 text-xs">
              {d.skipped.map((s) => (
                <li key={s.id} className="px-2 py-1"><span className="font-medium">{s.label}</span><div className="text-2xs text-muted">{s.reason}</div></li>
              ))}
            </ul>
          </Panel>
        </div>
      </div>
    </div>
  )
}
