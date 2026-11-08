import { Plus, Search } from 'lucide-react'
import { useDeferredValue, useState } from 'react'
import { useStudioCatalog, useStudioVariables } from '@/api/studio/hooks'
import type { StudioVariableView, VariableRole } from '@/api/studio/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Flag } from '@/components/ui/data'
import { ErrorState, SkeletonRows } from '@/components/ui/feedback'
import { Input, Select } from '@/components/ui/inputs'
import { DASH, fmtDate } from '@/lib/format'
import { fmtStat } from '../fmt'

const FREQ_LABEL: Record<string, string> = { D: 'daily', W: 'weekly', M: 'monthly', Q: 'quarterly', A: 'annual' }
const PAGE = 40

/** Searchable catalog of every source.field with table, frequency and coverage. */
export function VariablePicker({ roles, onAdd }: { roles: { role: VariableRole; label: string }[]; onAdd: (v: StudioVariableView, role: VariableRole) => void }) {
  const cat = useStudioCatalog()
  const [q, setQ] = useState('')
  const [source, setSource] = useState('')
  const [freq, setFreq] = useState('')
  const [limit, setLimit] = useState(PAGE)
  const dq = useDeferredValue(q)
  const vars = useStudioVariables({ q: dq.trim() || undefined, source: source || undefined, frequency: freq || undefined, limit })
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        <div className="relative min-w-40 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
          <Input
            autoFocus
            className="w-full pl-7"
            placeholder="Search variables (e.g. 10y yield, roic, hicp)"
            aria-label="Search variables"
            value={q}
            onChange={(e) => {
              setQ(e.target.value)
              setLimit(PAGE)
            }}
          />
        </div>
        <Select
          label="Source table"
          value={source}
          onChange={(v) => {
            setSource(v)
            setLimit(PAGE)
          }}
          options={[{ value: '', label: 'All sources' }, ...(cat.data?.sources ?? []).map((s) => ({ value: s.source, label: `${s.title}${s.nVariables ? ` (${s.nVariables})` : ''}` }))]}
        />
        <Select
          label="Frequency"
          value={freq}
          onChange={(v) => {
            setFreq(v)
            setLimit(PAGE)
          }}
          options={[{ value: '', label: 'Any freq.' }, ...['D', 'W', 'M', 'Q', 'A'].map((f) => ({ value: f, label: FREQ_LABEL[f] }))]}
        />
      </div>
      {source === 'bql' && <BqlExpressionRow roles={roles} onAdd={onAdd} />}
      {vars.isPending ? (
        <SkeletonRows rows={6} />
      ) : vars.isError ? (
        <ErrorState error={vars.error} onRetry={() => void vars.refetch()} />
      ) : (
        <>
          <div className="text-2xs text-muted">
            {fmtStat(vars.data.total)} variable{vars.data.total === 1 ? '' : 's'}
            {vars.data.total > vars.data.items.length ? ` · showing ${vars.data.items.length}` : ''}
          </div>
          <ul className="max-h-[360px] divide-y divide-border overflow-y-auto rounded-sm border border-border" aria-label="Variables">
            {vars.data.items.map((v) => (
              <li key={v.key} className="flex items-start gap-2 px-2 py-1.5 hover:bg-surface-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5">
                    {v.country && v.country.length === 2 && <Flag country={v.country} />}
                    <span className="truncate text-sm text-fg" title={v.title}>
                      {v.title}
                    </span>
                  </div>
                  <div className="num flex flex-wrap gap-x-2 text-2xs text-muted">
                    <span className="text-fg/80">
                      {v.source}:{v.field}
                    </span>
                    <span>{FREQ_LABEL[v.frequency] ?? v.frequency}</span>
                    {v.units && <span>{v.units}</span>}
                    <span>
                      {v.firstDate ? fmtDate(v.firstDate) : DASH} → {v.lastDate ? fmtDate(v.lastDate) : DASH}
                    </span>
                    <span>n={fmtStat(v.nObs)}</span>
                    {v.nEntities !== null && <span>{fmtStat(v.nEntities)} entities</span>}
                    {v.pit && <Badge tone="info" className="h-4 px-1">PIT</Badge>}
                  </div>
                </div>
                <div className="flex shrink-0 gap-1">
                  {roles.map((r) => (
                    <Button key={r.role} size="xs" variant={r.role === 'y' ? 'outline' : 'secondary'} onClick={() => onAdd(v, r.role)} title={`Add as ${r.label}`} aria-label={`Add ${v.title} as ${r.label}`}>
                      <Plus /> {r.role === 'feature' ? 'add' : r.role}
                    </Button>
                  ))}
                </div>
              </li>
            ))}
            {vars.data.items.length === 0 && <li className="px-2 py-6 text-center text-xs text-muted">No variables match.</li>}
          </ul>
          {vars.data.total > vars.data.items.length && (
            <Button size="xs" variant="ghost" onClick={() => setLimit(limit + PAGE)} disabled={vars.isFetching}>
              Load more
            </Button>
          )}
        </>
      )}
    </div>
  )
}

/** Model Studio source `bql` (roadmap v3 B1): any BQL-lite expression, evaluated point-in-time on the dataset grid. */
function BqlExpressionRow({ roles, onAdd }: { roles: { role: VariableRole; label: string }[]; onAdd: (v: StudioVariableView, role: VariableRole) => void }) {
  const [expr, setExpr] = useState('cs_zscore(chg_pct(px, 3M))')
  const view: StudioVariableView = {
    key: `bql:${expr}`, source: 'bql', field: expr.trim(), table: 'bql', title: `BQL ${expr.trim()}`, country: null, frequency: 'D',
    units: null, entityType: 'security', nObs: 0, nEntities: null, firstDate: null, lastDate: null, pit: true,
  }
  return (
    <div className="space-y-1 rounded-sm border border-border bg-surface-2 p-2">
      <div className="text-2xs text-muted">BQL expression — evaluated point-in-time at every dataset date on the serving snapshot (cross-sectional functions run over the dataset's securities)</div>
      <div className="flex gap-1">
        <Input aria-label="BQL expression" className="num min-w-0 flex-1" value={expr} onChange={(e) => setExpr(e.target.value)} placeholder="e.g. cs_zscore(roic) - cs_zscore(pe_ttm)" />
        {roles.map((r) => (
          <Button key={r.role} size="xs" variant={r.role === 'y' ? 'outline' : 'secondary'} disabled={!expr.trim()} onClick={() => onAdd(view, r.role)} aria-label={`Add the BQL expression as ${r.label}`}>
            <Plus /> {r.role === 'feature' ? 'add' : r.role}
          </Button>
        ))}
      </div>
    </div>
  )
}
