import { Download } from 'lucide-react'
import { useMemo, useState } from 'react'
import type { CompanyFinancials, FinancialStatement, Periodicity, StatementType } from '@/api/types'
import { useCompanyFinancials } from '@/api/hooks'
import { Sparkline } from '@/components/charts/Sparkline'
import { Button } from '@/components/ui/button'
import { QueryView, SkeletonRows } from '@/components/ui/feedback'
import { Segmented } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { downloadText, toCsv } from '@/lib/csv'
import { DASH, fmtMillions, fmtNum, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'

type View = 'values' | 'common' | 'growth'
type Ccy = 'reporting' | 'usd'

const STATEMENT_LABEL: Record<StatementType, string> = { income: 'Income statement', balance: 'Balance sheet', cashflow: 'Cash flow' }

export default function FinancialsTab({ id }: { id: string }) {
  const [periodicity, setPeriodicity] = useState<Periodicity>('annual')
  const [type, setType] = useState<StatementType>('income')
  const [view, setView] = useState<View>('values')
  const [ccy, setCcy] = useState<Ccy>('reporting')
  const q = useCompanyFinancials(id, periodicity)

  return (
    <Panel
      flush
      title={STATEMENT_LABEL[type]}
      subtitle={q.data ? `${periodicity === 'annual' ? 'Annual' : 'Quarterly'} · ${ccy === 'usd' ? 'USD' : q.data.reportingCurrency} millions (per-share in units) · point-in-time as filed` : undefined}
      actions={
        <>
          <Segmented label="Statement" value={type} onChange={setType} options={[{ value: 'income', label: 'IS' , title: 'Income statement'}, { value: 'balance', label: 'BS', title: 'Balance sheet' }, { value: 'cashflow', label: 'CF', title: 'Cash flow' }]} />
          <Segmented label="Periodicity" value={periodicity} onChange={setPeriodicity} options={[{ value: 'annual', label: 'Annual' }, { value: 'quarterly', label: 'Quarterly' }]} />
          {q.data && q.data.reportingCurrency !== 'USD' && (
            <Segmented label="Currency" value={ccy} onChange={setCcy} options={[{ value: 'reporting', label: q.data.reportingCurrency }, { value: 'usd', label: 'USD' }]} />
          )}
          <Segmented label="View" value={view} onChange={setView} options={[{ value: 'values', label: 'Values' }, { value: 'common', label: 'Common-size %' }, { value: 'growth', label: 'Growth %' }]} />
        </>
      }
    >
      <QueryView query={q} skeleton={<SkeletonRows rows={14} className="p-3" />}>
        {(d) => <StatementTable fin={d} type={type} view={view} ccy={ccy} id={id} />}
      </QueryView>
    </Panel>
  )
}

function StatementTable({ fin, type, view, ccy, id }: { fin: CompanyFinancials; type: StatementType; view: View; ccy: Ccy; id: string }) {
  const st = fin.statements.find((s) => s.type === type) as FinancialStatement
  const income = fin.statements.find((s) => s.type === 'income') as FinancialStatement
  const lag = fin.periodicity === 'quarterly' ? 4 : 1

  const rows = useMemo(() => {
    const fx = (i: number) => (ccy === 'usd' ? st.fxToUsd[i] : 1)
    const base: (number | null)[] =
      type === 'balance'
        ? (st.items.find((x) => x.id === 'totalAssets')?.values ?? [])
        : (income.items.find((x) => x.id === 'revenue')?.values ?? [])
    return st.items.map((item) => {
      const amounts = item.values.map((v, i) => (v === null ? null : item.kind === 'shares' ? v : v * fx(i)))
      let shown: (number | null)[]
      if (view === 'values') shown = amounts
      else if (view === 'common') shown = item.kind !== 'amount' ? item.values.map(() => null) : item.values.map((v, i) => (v === null || !base[i] ? null : v / (base[i] as number)))
      else shown = amounts.map((v, i) => (i < lag || v === null || amounts[i - lag] === null || amounts[i - lag] === 0 ? null : v / Math.abs(amounts[i - lag] as number) - (amounts[i - lag] as number) / Math.abs(amounts[i - lag] as number)))
      return { item, shown, spark: amounts }
    })
  }, [st, income, type, view, ccy, lag])

  const fmt = (v: number | null, kind: string) => {
    if (v === null) return DASH
    if (view !== 'values') return fmtPct(v, 1, view === 'growth')
    if (kind === 'perShare') return fmtNum(v, 2)
    return fmtMillions(v, Math.abs(v) < 100 ? 1 : 0)
  }

  const exportCsv = () => {
    const headers = ['line_item', ...st.periods]
    downloadText(`${id}-${type}-${fin.periodicity}-${view}.csv`, toCsv(headers, rows.map((r) => [r.item.label, ...r.shown.map((v) => (v === null ? '' : v))])))
  }

  return (
    <div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="text-2xs tracking-wide text-muted uppercase">
              <th scope="col" className="sticky left-0 z-[1] h-8 min-w-[220px] border-b border-border-strong bg-surface-2 px-3 text-left font-semibold">Line item</th>
              {st.periods.map((p, i) => (
                <th key={p} scope="col" className={cn('border-b border-border-strong bg-surface-2 px-2 text-right font-semibold', i === st.periods.length - 1 && 'text-fg')}>
                  <div className="num">{p}</div>
                  <div className="num font-normal normal-case text-faint">{st.periodEnds[i].slice(0, 7)}</div>
                </th>
              ))}
              <th scope="col" className="border-b border-border-strong bg-surface-2 px-3 text-right font-semibold">Trend</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ item, shown, spark }) => (
              <tr key={item.id} className={cn('hover:bg-surface-2', item.isTotal && 'bg-surface-2/50')}>
                <th scope="row" className={cn('sticky left-0 z-[1] h-[26px] border-b border-border bg-surface px-3 text-left font-normal whitespace-nowrap', item.isTotal ? 'font-semibold text-fg' : 'text-muted')} style={{ paddingLeft: 12 + item.level * 14 }}>
                  {item.label}
                </th>
                {shown.map((v, i) => (
                  <td key={i} className={cn('num border-b border-border px-2 text-right whitespace-nowrap', item.isTotal ? 'font-medium text-fg' : 'text-fg/90', view !== 'values' && v !== null && v < 0 && 'text-down', view === 'values' && v !== null && v < 0 && 'text-muted')}>
                    {fmt(v, item.kind)}
                  </td>
                ))}
                <td className="border-b border-border px-3 text-right">
                  <Sparkline data={spark} width={76} height={18} ariaLabel={`${item.label} trend`} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between border-t border-border px-3 py-1.5 text-2xs text-faint">
        <span>Negative values in parentheses. {ccy === 'usd' ? 'Converted at period-end FX.' : `Reported in ${fin.reportingCurrency}.`} Growth = y/y{fin.periodicity === 'quarterly' ? ' (same quarter prior year)' : ''}.</span>
        <Button size="xs" variant="ghost" onClick={exportCsv}>
          <Download /> CSV
        </Button>
      </div>
    </div>
  )
}
