import { CheckCircle2, CircleDot, Info, XCircle } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import type { StudioCoefficient, StudioTable, StudioTestView, TestVerdict } from '@/api/studio/types'
import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { fmtCell, fmtP, fmtStat, labelOf, stars } from '../fmt'

const th = 'sticky top-0 z-[1] bg-surface-2 px-2 py-1 text-left text-2xs font-semibold tracking-wide whitespace-nowrap text-muted uppercase'
const td = 'px-2 py-[3px] whitespace-nowrap'

/* ───────────── statsmodels-like coefficient table ───────────── */

export function CoefTable({ coefs, alphaLevel = 0.05, statLabel = 't' }: { coefs: StudioCoefficient[]; alphaLevel?: number; statLabel?: string }) {
  const has = (k: keyof StudioCoefficient) => coefs.some((c) => c[k] !== null && c[k] !== undefined)
  const q = has('quantile')
  const lo = (alphaLevel / 2).toFixed(3)
  const hi = (1 - alphaLevel / 2).toFixed(3)
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm" aria-label="Coefficient estimates">
        <thead>
          <tr className="border-b border-border">
            {q && <th className={th}>Quantile</th>}
            <th className={th}>Term</th>
            <th className={cn(th, 'text-right')}>Coef.</th>
            <th className={cn(th, 'text-right')}>Std. err.</th>
            <th className={cn(th, 'text-right')}>{statLabel}</th>
            <th className={cn(th, 'text-right')}>P&gt;|{statLabel}|</th>
            {has('p_value_adj') && (
              <th className={cn(th, 'text-right')} title="Benjamini–Hochberg adjusted p-value">
                p adj.
              </th>
            )}
            <th className={cn(th, 'text-right')}>[{lo}</th>
            <th className={cn(th, 'text-right')}>{hi}]</th>
            {has('exp_estimate') && <th className={cn(th, 'text-right')}>exp(coef)</th>}
            {has('marginal_effect') && <th className={cn(th, 'text-right')} title="Average marginal effect">AME</th>}
            {has('vif') && <th className={cn(th, 'text-right')} title="Variance inflation factor (>10 = severe collinearity)">VIF</th>}
            {has('share_positive') && (
              <th className={cn(th, 'text-right')} title="Share of periods with a positive cross-sectional slope">
                λ&gt;0 share
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {coefs.map((c, i) => {
            const sig = typeof c.p_value === 'number' && c.p_value < alphaLevel
            return (
              <tr key={`${c.term}-${c.quantile ?? ''}-${i}`} className="border-b border-border/60 hover:bg-surface-2">
                {q && <td className={cn(td, 'num text-muted')}>{fmtStat(c.quantile, 2)}</td>}
                <td className={cn(td, 'font-medium text-fg')}>{c.term}</td>
                <td className={cn(td, 'num text-right', sig ? 'font-semibold text-fg' : 'text-fg')}>
                  {fmtStat(c.estimate)}
                  <span className="ml-0.5 inline-block w-5 text-left text-accent-text" aria-label={stars(c.p_value) ? `significance ${stars(c.p_value)}` : undefined}>
                    {stars(c.p_value)}
                  </span>
                </td>
                <td className={cn(td, 'num text-right text-muted')}>{fmtStat(c.std_error)}</td>
                <td className={cn(td, 'num text-right text-muted')}>{fmtStat(c.statistic, 3)}</td>
                <td className={cn(td, 'num text-right', sig ? 'text-fg' : 'text-muted')}>{fmtP(c.p_value)}</td>
                {has('p_value_adj') && <td className={cn(td, 'num text-right text-muted')}>{fmtP(c.p_value_adj)}</td>}
                <td className={cn(td, 'num text-right text-muted')}>{fmtStat(c.ci_low)}</td>
                <td className={cn(td, 'num text-right text-muted')}>{fmtStat(c.ci_high)}</td>
                {has('exp_estimate') && <td className={cn(td, 'num text-right')}>{fmtStat(c.exp_estimate)}</td>}
                {has('marginal_effect') && <td className={cn(td, 'num text-right')}>{fmtStat(c.marginal_effect)}</td>}
                {has('vif') && <td className={cn(td, 'num text-right', typeof c.vif === 'number' && c.vif > 10 ? 'text-warn' : 'text-muted')}>{fmtStat(c.vif, 3)}</td>}
                {has('share_positive') && <td className={cn(td, 'num text-right text-muted')}>{typeof c.share_positive === 'number' ? `${Math.round(c.share_positive * 100)}%` : '—'}</td>}
              </tr>
            )
          })}
        </tbody>
      </table>
      <p className="mt-1.5 text-2xs text-muted">*** p&lt;0.01 · ** p&lt;0.05 · * p&lt;0.1 (unadjusted). Bold: significant at α = {alphaLevel}.</p>
    </div>
  )
}

/* ───────────── diagnostic / hypothesis tests ───────────── */

const VERDICT: Record<TestVerdict, { label: string; cls: string; Icon: typeof CheckCircle2 }> = {
  pass: { label: 'pass', cls: 'bg-up-soft text-up', Icon: CheckCircle2 },
  fail: { label: 'fail', cls: 'bg-down-soft text-down', Icon: XCircle },
  significant: { label: 'reject H0', cls: 'bg-info-soft text-info', Icon: CircleDot },
  not_significant: { label: 'do not reject', cls: 'bg-surface-3 text-muted', Icon: CircleDot },
  info: { label: 'info', cls: 'bg-surface-3 text-muted', Icon: Info },
}

export function VerdictBadge({ v }: { v: TestVerdict }) {
  const m = VERDICT[v]
  return (
    <span className={cn('inline-flex h-5 items-center gap-1 rounded-sm px-1.5 text-2xs font-semibold tracking-wide uppercase', m.cls)}>
      <m.Icon className="size-3" aria-hidden />
      {m.label}
    </span>
  )
}

export function TestTable({ tests }: { tests: StudioTestView[] }) {
  const groups = [...new Set(tests.map((t) => t.group))]
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm" aria-label="Statistical tests">
        <thead>
          <tr className="border-b border-border">
            <th className={th}>Test</th>
            <th className={th}>H0</th>
            <th className={cn(th, 'text-right')}>Statistic</th>
            <th className={cn(th, 'text-right')}>df</th>
            <th className={cn(th, 'text-right')}>p-value</th>
            <th className={th}>Result</th>
            <th className={th}>Conclusion</th>
          </tr>
        </thead>
        {groups.map((g) => (
          <tbody key={g}>
            {groups.length > 1 && (
              <tr>
                <td colSpan={7} className="bg-surface px-2 pt-2 pb-1 text-2xs font-semibold tracking-wide text-muted uppercase">
                  {g.replace(/_/g, ' ')}
                </td>
              </tr>
            )}
            {tests
              .filter((t) => t.group === g)
              .map((t, i) => (
                <tr key={`${t.name}-${i}`} className="border-b border-border/60 hover:bg-surface-2">
                  <td className={cn(td, 'font-medium text-fg')}>{t.name}</td>
                  <td className={cn(td, 'max-w-56 truncate text-muted')} title={t.null_hypothesis ?? undefined}>
                    {t.null_hypothesis ?? '—'}
                  </td>
                  <td className={cn(td, 'num text-right')}>{fmtStat(t.statistic)}</td>
                  <td className={cn(td, 'num text-right text-muted')}>{Array.isArray(t.df) ? t.df.map((x) => fmtStat(x)).join(', ') : fmtStat(t.df)}</td>
                  <td className={cn(td, 'num text-right')}>{fmtP(t.pValue)}</td>
                  <td className={td}>
                    <VerdictBadge v={t.verdict} />
                  </td>
                  <td className={cn(td, 'max-w-80 truncate text-muted')} title={[t.conclusion, t.note].filter(Boolean).join(' — ') || undefined}>
                    {t.conclusion ?? '—'}
                    {t.note ? ` — ${t.note}` : ''}
                  </td>
                </tr>
              ))}
          </tbody>
        ))}
      </table>
      <p className="mt-1.5 text-2xs text-muted">
        <b className="text-up">pass</b> / <b className="text-down">fail</b> = the server's reading of whether the model passes the check (e.g. residual tests, first-stage F ≥ 10, Durbin–Watson in 1.5–2.5). Tests without a pass/fail reading show reject / do not reject at 5%.
      </p>
    </div>
  )
}

/* ───────────── generic result table ───────────── */

export function ResultTable({ table, maxRows = 50, className, highlightFirst = false }: { table: StudioTable; maxRows?: number; className?: string; highlightFirst?: boolean }) {
  const [all, setAll] = useState(false)
  const rows = all ? table.rows : table.rows.slice(0, maxRows)
  const wide = table.columns.length > 14
  return (
    <div className={className}>
      <div className={cn('overflow-auto rounded-sm border border-border', rows.length > 18 && 'max-h-[420px]')}>
        <table className="w-full border-collapse text-sm" aria-label={table.title}>
          <thead>
            <tr className="border-b border-border">
              {table.columns.map((c, i) => (
                <th key={`${c}-${i}`} className={cn(th, i > 0 && 'text-right', i === 0 && wide && 'sticky left-0 z-[2]')}>
                  {fmtCell(c)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r, ri) => (
              <tr key={ri} className={cn('border-b border-border/60 hover:bg-surface-2', highlightFirst && ri === 0 && 'bg-accent-soft/40 font-semibold')}>
                {r.map((v, ci) => {
                  const col = table.columns[ci]
                  const isP = /(^|_)p(_value)?$|^p$|_p$/.test(col ?? '')
                  return (
                    <td key={ci} className={cn(td, ci > 0 ? 'num text-right' : 'font-medium text-fg', ci === 0 && wide && 'sticky left-0 bg-surface')}>
                      {col === 'ticker' && typeof v === 'string' ? (
                        <Link to={`/company/${encodeURIComponent(v)}`} className="font-medium text-fg hover:text-accent-text hover:underline">
                          {v}
                        </Link>
                      ) : isP && typeof v === 'number' ? (
                        fmtP(v)
                      ) : (
                        fmtCell(v)
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-1 flex items-center gap-2 text-2xs text-muted">
        {table.note && <span>{table.note}</span>}
        {table.rows.length > maxRows && (
          <Button size="xs" variant="ghost" onClick={() => setAll(!all)}>
            {all ? `Show first ${maxRows}` : `Show all ${table.rows.length} rows`}
          </Button>
        )}
      </div>
    </div>
  )
}

/* ───────────── key / value block (summary, fit stats) ───────────── */

export function KeyValues({ entries, className }: { entries: [string, unknown][]; className?: string }) {
  return (
    <dl className={cn('grid grid-cols-[minmax(0,auto)_minmax(0,1fr)] gap-x-4 gap-y-0.5 text-sm', className)}>
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="truncate text-muted">{labelOf(k)}</dt>
          <dd className="num truncate text-right text-fg" title={typeof v === 'string' ? v : undefined}>
            {fmtCell(v)}
          </dd>
        </div>
      ))}
    </dl>
  )
}

export function HelpTip({ text }: { text: string }) {
  return (
    <Tip content={<div className="max-w-xs">{text}</div>}>
      <button type="button" aria-label={text} className="inline-flex text-faint hover:text-fg">
        <Info className="size-3.5" />
      </button>
    </Tip>
  )
}
