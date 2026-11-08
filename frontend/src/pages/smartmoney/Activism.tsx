import { useSearchParams } from 'react-router'
import { EVENT_META } from '@/api/smartmoney/adapters'
import { useActivism } from '@/api/smartmoney/hooks'
import type { SmActivism } from '@/api/smartmoney/types'
import { Badge } from '@/components/ui/badge'
import { EmptyState, ErrorState, PendingState, Skeleton } from '@/components/ui/feedback'
import { Checkbox, Select } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { fmtDate, fmtInt } from '@/lib/format'
import { CompanyLink, EventBadge, OfficialTag, SmartMoneyNav } from './shared'

const DAYS = [30, 90, 180, 365, 1095].map((d) => ({ value: String(d), label: d < 365 ? `${d} days` : d === 365 ? '1 year' : '3 years' }))
const DEFAULT_TYPES = ['new_13d', 'switch_13g_to_13d', 'amend_13d']

export default function Activism() {
  const [sp, setSp] = useSearchParams()
  const days = Number(sp.get('days') ?? '90')
  const types = sp.get('types')?.split(',').filter(Boolean) ?? DEFAULT_TYPES
  const q = useActivism({ days, eventTypes: types })
  const toggle = (t: string, on: boolean) => {
    const next = on ? [...new Set([...types, t])] : types.filter((x) => x !== t)
    setSp((p) => { p.set('types', (next.length ? next : DEFAULT_TYPES).join(',')); return p }, { replace: true })
  }
  return (
    <>
      <PageHeader title="Activism feed" subtitle="SEC Schedule 13D (active / control intent above 5%) and 13G (passive) filings, with 13G → 13D switches">
        <div className="flex flex-wrap items-center gap-3">
          <Select label="Window" value={String(days)} onChange={(v) => setSp((p) => { p.set('days', v); return p }, { replace: true })} options={DAYS} />
          {Object.entries(EVENT_META).map(([k, m]) => (
            <Checkbox key={k} label={m.label} checked={types.includes(k)} onCheckedChange={(c) => toggle(k, c === true)} />
          ))}
        </div>
      </PageHeader>
      <SmartMoneyNav />
      {q.isError ? <ErrorState error={q.error} onRetry={() => void q.refetch()} /> : !q.data ? <Skeleton className="h-96" /> : q.data.data_status === 'pending' ? <PendingState detail="The 13D / 13G job has not run for this snapshot yet." /> : <Body d={q.data} />}
    </>
  )
}

function Body({ d }: { d: SmActivism }) {
  return (
    <div className="grid gap-3 xl:grid-cols-[minmax(0,3fr)_minmax(0,1fr)]">
      <Panel title={`${fmtInt(d.items.length)} filings since ${fmtDate(d.from)}`} subtitle={<OfficialTag label="SEC EDGAR (subject / filer roles from the EDGAR index; ambiguous 13Ds resolved from the filing header)" />} flush>
        {d.items.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs" aria-label="13D and 13G filings">
              <thead className="text-left text-muted">
                <tr className="border-b border-border">
                  {['Filed', 'Event', 'Filer', 'Subject company', 'Form', ''].map((h) => (
                    <th key={h || 'link'} scope="col" className="px-2 py-1.5 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.items.map((e) => (
                  <tr key={e.filing_id} className="border-b border-border/60">
                    <td className="num px-2 py-1 whitespace-nowrap text-muted">{fmtDate(e.event_date)}</td>
                    <td className="px-2 py-1"><EventBadge type={e.event_type} />{e.prior_13g_date && e.event_type === 'switch_13g_to_13d' ? <span className="ml-1 text-2xs text-faint">13G {fmtDate(e.prior_13g_date)}</span> : null}</td>
                    <td className="max-w-[240px] truncate px-2 py-1 font-medium" title={e.filer_name ?? ''}>{e.filer_name ?? '—'}{e.n_filers > 1 ? <span className="ml-1 text-2xs text-faint">+{e.n_filers - 1}</span> : null}</td>
                    <td className="max-w-[260px] truncate px-2 py-1">{e.security ? <CompanyLink ticker={e.security.ticker} name={e.security.name} id={e.security.security_id} /> : <span className="text-muted">{e.subject_name}</span>}</td>
                    <td className="px-2 py-1 whitespace-nowrap text-muted">
                      {e.form_type}
                      {e.role_source === 'unresolved' || e.role_source === 'name_heuristic' ? (
                        <Tip content="Subject / filer roles inferred (no ticker match and no filing-header check)"><span tabIndex={0} className="ml-1"><Badge tone="neutral">inferred</Badge></span></Tip>
                      ) : null}
                    </td>
                    <td className="px-2 py-1"><a href={e.url} target="_blank" rel="noreferrer" className="text-accent-text hover:underline">EDGAR</a></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <EmptyState title="No filings in this window" className="py-8" />
        )}
      </Panel>
      <div className="space-y-3">
        <Panel title="Counts" subtitle="all event types in the window" flush>
          <ul className="divide-y divide-border/60 text-xs">
            {d.counts.map((c) => (
              <li key={c.event_type} className="flex items-center gap-2 px-2 py-1"><EventBadge type={c.event_type} /><span className="num ml-auto">{fmtInt(c.n)}</span></li>
            ))}
          </ul>
        </Panel>
        <Panel title="Most active 13D filers" flush>
          {d.top_filers.length ? (
            <ul className="divide-y divide-border/60 text-xs">
              {d.top_filers.map((f) => (
                <li key={`${f.filer_cik}${f.filer_name}`} className="flex items-center gap-2 px-2 py-1"><span className="min-w-0 flex-1 truncate" title={f.filer_name}>{f.filer_name}</span><span className="num text-muted">{f.n}</span></li>
              ))}
            </ul>
          ) : (
            <EmptyState className="py-6" />
          )}
        </Panel>
      </div>
    </div>
  )
}
