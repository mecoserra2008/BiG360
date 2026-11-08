import { VERDICT, headlineCoverage } from '@/api/conformal/adapters'
import { useConformalCoverage } from '@/api/conformal/hooks'
import { CoverageTable } from '@/components/conformal/CoveragePanel'
import { Badge } from '@/components/ui/badge'
import { ErrorState, PendingState, SkeletonRows } from '@/components/ui/feedback'
import { PageHeader, Panel } from '@/components/ui/panel'
import { fmtDate } from '@/lib/format'
import { Markdown } from '../theses/shared'

/** Markdown tables are not in the safe subset: render them as fenced (monospace) blocks. */
function fenceTables(md: string): string {
  const out: string[] = []
  let inTable = false
  for (const line of md.split(/\r?\n/)) {
    const isRow = line.trimStart().startsWith('|')
    if (isRow && !inTable) {
      out.push('```')
      inTable = true
    } else if (!isRow && inTable) {
      out.push('```')
      inTable = false
    }
    out.push(line)
  }
  if (inTable) out.push('```')
  return out.join('\n')
}

export default function ConformalValidation() {
  const q = useConformalCoverage(true)
  const d = q.data
  return (
    <>
      <PageHeader
        title="Conformal ellipsoids · validation & coverage"
        subtitle="What passed the P4 gate is shown in the app (labelled model-based); what failed stays experimental (Model Studio / Backtest Lab)"
      />
      {q.isPending ? (
        <Panel><SkeletonRows rows={10} /></Panel>
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !d ? (
        <PendingState />
      ) : (
        <div className="grid min-w-0 grid-cols-12 gap-3">
          <Panel title="Validation gate (2010–2026 walk-forward)" subtitle="docs/CONFORMAL_VALIDATION.md" className="col-span-12 min-w-0 xl:col-span-7" flush>
            <div className="overflow-x-auto"><table className="w-full text-sm">
              <thead>
                <tr className="text-2xs tracking-wide text-muted uppercase">
                  <th scope="col" className="h-7 border-b border-border px-3 text-left font-semibold">Application</th>
                  <th scope="col" className="border-b border-border px-2 text-left font-semibold">Verdict</th>
                  <th scope="col" className="border-b border-border px-2 text-left font-semibold">Shown as</th>
                  <th scope="col" className="border-b border-border px-3 text-left font-semibold">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {d.gates.map((g) => (
                  <tr key={g.application} className="align-top hover:bg-surface-2">
                    <td className="border-b border-border px-3 py-1.5 font-medium">{g.title}</td>
                    <td className="border-b border-border px-2 py-1.5"><Badge tone={VERDICT[g.verdict]?.tone ?? 'neutral'}>{VERDICT[g.verdict]?.label ?? g.verdict}</Badge></td>
                    <td className="border-b border-border px-2 py-1.5 text-xs">{g.shown_as}</td>
                    <td className="border-b border-border px-3 py-1.5 text-xs text-muted">{g.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </Panel>
          <Panel title="Coverage at 90%" subtitle={`Rolling audit ${fmtDate(d.as_of)} and the walk-forward rows`} className="col-span-12 min-w-0 xl:col-span-5" flush>
            {d.data_status === 'pending' ? <PendingState compact /> : <CoverageTable rows={headlineCoverage(d.rows)} />}
            <p className="border-t border-border px-3 py-2 text-2xs text-muted">
              Coverage is marginal over time and securities: "calibrated to X% over history", never "X% chance today". {d.flags.length} rolling-audit segment(s) flagged under target.
            </p>
          </Panel>
          {d.production && (
            <Panel title="Production configuration" className="col-span-12 min-w-0">
              <pre className="overflow-x-auto text-2xs text-muted">{JSON.stringify(d.production, null, 1)}</pre>
            </Panel>
          )}
          <Panel title="Validation report" subtitle={d.validation_doc} className="col-span-12 min-w-0">
            {d.validation_md ? <Markdown text={fenceTables(d.validation_md)} className="min-w-0 [&_pre]:max-w-full [&_pre]:overflow-x-auto [&_pre]:text-2xs" /> : <p className="text-xs text-muted">The report file is not available on this server ({d.validation_doc}).</p>}
          </Panel>
        </div>
      )}
    </>
  )
}
