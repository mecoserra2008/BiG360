import type { ColumnDef } from '@tanstack/react-table'
import { useMemo, useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Users } from 'lucide-react'
import { Link } from 'react-router'
import { studioApi } from '@/api/studio/client'
import { sk } from '@/api/studio/hooks'
import { Button } from '@/components/ui/button'
import type { StudioClusters, StudioPlot, StudioResultView, StudioRunView, StudioTable } from '@/api/studio/types'
import { DataTable } from '@/components/tables/DataTable'
import { Badge } from '@/components/ui/badge'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { toast } from '@/components/ui/toast'
import { DASH, fmtDate, fmtPct } from '@/lib/format'
import { cn } from '@/lib/utils'
import { fmtCell, fmtP, fmtSeconds, fmtStat, labelOf } from '../fmt'
import { useStudio } from '../store'
import type { Assigned } from './assign'
import { ClusterSizes, ClusterSwatch, DendrogramPlot, FanChart, ForestChart, ImportanceBars, OosChart, PlotView, PointsPlot, WeightsBar } from './charts'
import { CoefTable, KeyValues, ResultTable, TestTable, VerdictBadge } from './tables'

/* ───────────── layout helpers ───────────── */

function Card({ title, sub, children, className, actions }: { title?: ReactNode; sub?: ReactNode; children: ReactNode; className?: string; actions?: ReactNode }) {
  return (
    <section className={cn('min-w-0 rounded-md border border-border bg-surface p-2.5', className)}>
      {(title || actions) && (
        <header className="mb-1.5 flex items-start gap-2">
          <div className="min-w-0 flex-1">
            {title && <h3 className="truncate text-sm font-semibold text-fg">{title}</h3>}
            {sub && <p className="truncate text-xs text-muted">{sub}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  )
}

function PlotCard({ plot, plotKey, height = 240, className }: { plot: StudioPlot; plotKey: string; height?: number; className?: string }) {
  return (
    <Card title={plot.title} className={className}>
      <PlotView plot={plot} plotKey={plotKey} height={height} />
    </Card>
  )
}

const alphaOf = (run: StudioRunView): number => {
  const a = (run.study?.params as Record<string, unknown> | undefined)?.alpha
  return typeof a === 'number' && a > 0 && a < 0.5 ? a : 0.05
}

/* ───────────── Summary ───────────── */

const KEY_GROUPS = ['specification', 'instruments', 'inference', 'cointegration']

export function SummaryTab({ run, result, a, onGoto }: { run: StudioRunView; result: StudioResultView; a: Assigned; onGoto: (tab: string) => void }) {
  const summary = Object.entries(result.summary).filter(([k]) => k !== 'linkage_matrix')
  const fit = Object.entries(result.fitStats)
  const extra = Object.entries(result.metrics).filter(([k]) => !(k in result.fitStats) && !(k in result.summary))
  const statLabel = result.method === 'glm' || result.family === 'time_series' ? 'z' : 't'
  const glance = result.tests.filter((t) => !KEY_GROUPS.includes(t.group))
  return (
    <div className="space-y-3">
      <div className="grid gap-3 @3xl:grid-cols-2">
        <Card title={result.title} sub="Model">
          <KeyValues entries={[['method', result.method], ...summary]} />
        </Card>
        <div className="space-y-3">
          {fit.length > 0 && (
            <Card title="Fit statistics">
              <KeyValues entries={fit} />
            </Card>
          )}
          {extra.length > 0 && (
            <Card title="Other metrics">
              <KeyValues entries={extra} />
            </Card>
          )}
        </div>
      </div>
      {result.coefficients.length > 0 && (
        <Card title="Coefficients" sub={result.summary.cov_type ? `Covariance: ${fmtCell(result.summary.cov_type)}` : undefined}>
          <CoefTable coefs={result.coefficients} alphaLevel={alphaOf(run)} statLabel={statLabel} />
        </Card>
      )}
      <KeyTests result={result} />
      {glance.length > 0 && (
        <Card
          title="Tests at a glance"
          actions={
            <button type="button" className="text-xs text-accent-text hover:underline" onClick={() => onGoto('diagnostics')}>
              Diagnostics →
            </button>
          }
        >
          <ul className="flex flex-wrap gap-x-4 gap-y-1.5">
            {glance.slice(0, 16).map((t, i) => (
              <li key={`${t.name}-${i}`} className="flex items-center gap-1.5 text-xs">
                <VerdictBadge v={t.verdict} />
                <span className="text-fg">{t.name}</span>
                <span className="num text-muted">p {fmtP(t.pValue)}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {a.summaryTables.map((t) => (
        <Card key={t.id} title={t.title}>
          <ResultTable table={t} maxRows={12} />
        </Card>
      ))}
    </div>
  )
}

/* ───────────── Coefficients ───────────── */

export function CoefficientsTab({ result, a }: { result: StudioResultView; a: Assigned }) {
  const lagPlots = a.coefPlots.filter(([k]) => k.startsWith('lag_profile_'))
  const procPlots = a.coefPlots.filter(([k]) => k.startsWith('process_'))
  const otherPlots = a.coefPlots.filter(([k]) => !k.startsWith('lag_profile_') && !k.startsWith('process_'))
  const lagBlocks = a.coefTables.find((t) => t.id === 'lag_blocks')
  return (
    <div className="space-y-3">
      {result.coefficients.length > 0 && !a.quantile && (
        <Card title="Forest plot" sub="Point estimates with confidence intervals">
          <ForestChart coefs={result.coefficients} />
        </Card>
      )}
      {lagPlots.length > 0 && (
        <div className="grid gap-3 @3xl:grid-cols-2">
          {lagPlots.map(([k, p]) => (
            <PlotCard key={k} plotKey={k} plot={p} height={220} />
          ))}
        </div>
      )}
      {lagBlocks && (
        <Card title={lagBlocks.title} sub="Long-run multiplier = sum of the lag coefficients; F tests all lags of a variable jointly.">
          <ResultTable table={lagBlocks} />
        </Card>
      )}
      {procPlots.length > 0 && (
        <div className="grid gap-3 @3xl:grid-cols-2">
          {procPlots.map(([k, p]) => (
            <PlotCard key={k} plotKey={k} plot={p} height={220} />
          ))}
        </div>
      )}
      {otherPlots.map(([k, p]) => (
        <PlotCard key={k} plotKey={k} plot={p} />
      ))}
      {a.coefTables
        .filter((t) => t !== lagBlocks)
        .map((t) => (
          <Card key={t.id} title={t.title}>
            <ResultTable table={t} />
          </Card>
        ))}
    </div>
  )
}

/* ───────────── Diagnostics ───────────── */

export function DiagnosticsTab({ result, a }: { result: StudioResultView; a: Assigned }) {
  const wide = new Set(['fit', 'residuals_time'])
  return (
    <div className="space-y-3">
      {result.tests.length > 0 && (
        <Card title="Tests">
          <TestTable tests={result.tests} />
        </Card>
      )}
      {a.diagTables.map((t) => (
        <Card key={t.id} title={t.title}>
          <ResultTable table={t} />
        </Card>
      ))}
      <div className="grid gap-3 @3xl:grid-cols-2">
        {a.diagPlots.map(([k, p]) => (
          <PlotCard key={k} plotKey={k} plot={p} className={wide.has(k) || k.startsWith('series_') ? '@3xl:col-span-2' : undefined} height={wide.has(k) ? 260 : 240} />
        ))}
      </div>
    </div>
  )
}

/* ───────────── Forecast ───────────── */

const METRIC_LABEL: Record<string, string> = { rmse: 'RMSE', mae: 'MAE', mape: 'MAPE %', r2_oos: 'OOS R²', corr: 'Corr.', auc: 'AUC', logloss: 'Log loss', brier: 'Brier', accuracy: 'Accuracy', n: 'n' }

const BASELINE_LABEL: Record<string, string> = {
  ols: 'OLS (unpenalized)',
  model: 'Model',
  naive: 'Naive (no change)',
  random_walk_drift: 'Random walk + drift',
  historical_mean: 'Historical mean',
  seasonal_naive: 'Seasonal naive',
}

type ByVariable = { variable: string; mean: (number | null)[]; lower: (number | null)[]; upper: (number | null)[] }

export function ForecastTab({ result }: { result: StudioResultView; a: Assigned }) {
  const f0 = result.forecast
  const byVar = (f0?.by_variable ?? []) as ByVariable[]
  const [pick, setPick] = useState<string | null>(null)
  const sel = byVar.find((b) => b.variable === pick)
  // VAR / VECM: other variables have no history in the payload, only their forecast path
  const f = f0 && sel && sel.variable !== f0.variable ? { ...f0, variable: sel.variable, mean: sel.mean, lower: sel.lower, upper: sel.upper, lower_80: undefined, upper_80: undefined, history: undefined } : f0
  const isSigma = f?.variable === 'sigma'
  const noBand = !!f && f.dates.length > 0 && f.dates.every((_, i) => f.upper?.[i] === f.mean[i])
  const e = result.evaluation
  const rows = e ? Object.entries(e.metrics ?? {}) : []
  const modelRmse = e?.metrics?.model?.rmse ?? null
  const metricCols = (() => {
    const keys = new Set<string>()
    for (const [, m] of rows) Object.keys(m ?? {}).forEach((k) => keys.add(k))
    const order = ['rmse', 'mae', 'mape', 'r2_oos', 'corr', 'auc', 'logloss', 'brier', 'accuracy']
    return [...keys].sort((x, y) => (x === 'n' ? 1 : y === 'n' ? -1 : (order.indexOf(x) + 1 || 50) - (order.indexOf(y) + 1 || 50)))
  })()
  const bestKey = metricCols.includes('rmse') ? 'rmse' : metricCols.includes('auc') ? 'auc' : metricCols.includes('logloss') ? 'logloss' : null
  const higherBetter = bestKey === 'auc'
  const val = (m: unknown) => (bestKey && m && typeof (m as Record<string, unknown>)[bestKey] === 'number' ? ((m as Record<string, number>)[bestKey] as number) : null)
  const best = rows.reduce<string | null>((b, [k, m]) => {
    const v = val(m)
    if (v === null) return b
    const bv = b === null ? null : val(e?.metrics?.[b])
    return bv === null || (higherBetter ? v > bv : v < bv) ? k : b
  }, null)
  const showRel = metricCols.includes('rmse') && rows.some(([k]) => k === 'model') && rows.length > 1
  const rel = e?.relative_rmse_vs_naive ?? null
  return (
    <div className="space-y-3">
      {f && (
        <Card
          title={isSigma ? 'Volatility forecast (sigma)' : `Forecast${f.variable ? `: ${f.variable}` : ''}`}
          sub={
            isSigma
              ? `${f.steps} steps ahead · conditional volatility per period${f.interval ? ` · band: ${f.interval}` : ''}${typeof f.var_1 === 'number' ? ` · next-period VaR ${fmtStat(f.var_1, 3)}` : ''}`
              : `${f.steps} steps ahead · ${Math.round((1 - (f.alpha ?? 0.05)) * 100)}%${f.lower_80 ? ' and 80%' : ''} intervals${f.exog_future ? ` · future regressors: ${f.exog_future}` : ''}`
          }
          actions={
            byVar.length > 1 ? (
              <Segmented
                label="Forecast variable"
                size="xs"
                value={f.variable ?? byVar[0].variable}
                onChange={setPick}
                options={byVar.map((b) => ({ value: b.variable, label: b.variable }))}
              />
            ) : undefined
          }
        >
          <FanChart forecast={f} yLabel={f.variable ?? (typeof result.summary.dependent === 'string' ? result.summary.dependent : null)} />
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-xs" aria-label="Forecast values">
              <thead>
                <tr className="text-left text-2xs tracking-wide text-muted uppercase">
                  <th className="px-2 py-1">Date</th>
                  <th className="px-2 py-1 text-right">Mean</th>
                  {f.lower_80 && <th className="px-2 py-1 text-right">80% low</th>}
                  {f.lower_80 && <th className="px-2 py-1 text-right">80% high</th>}
                  {!noBand && <th className="px-2 py-1 text-right">{Math.round((1 - (f.alpha ?? 0.05)) * 100)}% low</th>}
                  {!noBand && <th className="px-2 py-1 text-right">{Math.round((1 - (f.alpha ?? 0.05)) * 100)}% high</th>}
                </tr>
              </thead>
              <tbody>
                {f.dates.slice(0, 12).map((d, i) => (
                  <tr key={d} className="border-t border-border/60">
                    <td className="num px-2 py-0.5 text-muted">{fmtDate(d.slice(0, 10))}</td>
                    <td className="num px-2 py-0.5 text-right font-medium text-fg">{fmtStat(f.mean[i])}</td>
                    {f.lower_80 && <td className="num px-2 py-0.5 text-right">{fmtStat(f.lower_80?.[i])}</td>}
                    {f.lower_80 && <td className="num px-2 py-0.5 text-right">{fmtStat(f.upper_80?.[i])}</td>}
                    {!noBand && <td className="num px-2 py-0.5 text-right text-muted">{fmtStat(f.lower?.[i])}</td>}
                    {!noBand && <td className="num px-2 py-0.5 text-right text-muted">{fmtStat(f.upper?.[i])}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
            {f.dates.length > 12 && <p className="mt-1 text-2xs text-muted">First 12 of {f.dates.length} steps shown; the chart shows all.</p>}
          </div>
        </Card>
      )}
      {e && (
        <Card
          title="Out-of-sample evaluation"
          sub={[
            e.scheme,
            e.initial_train != null ? `initial train ${fmtStat(e.initial_train)}` : null,
            e.horizon != null ? `horizon ${fmtStat(e.horizon)}` : null,
            e.n_folds != null ? `${fmtStat(e.n_folds)} folds` : null,
            e.purge ? `purge ${fmtStat(e.purge)}` : null,
          ]
            .filter(Boolean)
            .join(' · ')}
        >
          {e.error && <p className="mb-2 rounded-sm bg-warn-soft px-2 py-1 text-xs text-warn">{e.error}</p>}
          {(rel !== null || e.oos_r2_vs_mean != null) && (
            <div className="mb-2 flex flex-wrap gap-x-6 gap-y-1 text-sm">
              {rel !== null && (
                <div>
                  <span className="text-muted">RMSE vs naive </span>
                  <span className={cn('num font-semibold', rel < 1 ? 'text-up' : 'text-down')}>{fmtStat(rel, 3)}</span>
                  <span className="ml-1 text-xs text-muted">{rel < 1 ? '(beats the no-change forecast)' : '(does not beat the no-change forecast)'}</span>
                </div>
              )}
              {e.oos_r2_vs_mean != null && (
                <div>
                  <span className="text-muted">OOS R² vs historical mean </span>
                  <span className="num font-semibold text-fg">{fmtStat(e.oos_r2_vs_mean, 3)}</span>
                </div>
              )}
            </div>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-sm" aria-label="Out-of-sample metrics against baselines">
              <thead>
                <tr className="border-b border-border text-left text-2xs tracking-wide text-muted uppercase">
                  <th className="px-2 py-1">Forecaster</th>
                  {metricCols.map((c) => (
                    <th key={c} className="px-2 py-1 text-right">
                      {METRIC_LABEL[c] ?? labelOf(c)}
                    </th>
                  ))}
                  {showRel && (
                    <th className="px-2 py-1 text-right" title="RMSE relative to the model (above 1 = worse than the model)">
                      RMSE vs model
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map(([k, m]) => {
                  const mm = (m ?? {}) as Record<string, unknown>
                  return (
                    <tr key={k} className={cn('border-b border-border/60', k === 'model' && 'bg-accent-soft/40')}>
                      <td className="px-2 py-1 font-medium text-fg">
                        {k === 'model' && e.model ? `Model (${e.model})` : (BASELINE_LABEL[k] ?? (/^[a-z_]+$/.test(k) ? labelOf(k) : k))}
                        {k === best && rows.length > 1 && (
                          <Badge tone="up" className="ml-1.5">
                            best {bestKey}
                          </Badge>
                        )}
                      </td>
                      {metricCols.map((c) => (
                        <td key={c} className={cn('num px-2 py-1 text-right', c === 'n' && 'text-muted')}>
                          {fmtStat(mm[c])}
                        </td>
                      ))}
                      {showRel && (
                        <td className="num px-2 py-1 text-right text-muted">{typeof mm.rmse === 'number' && typeof modelRmse === 'number' && modelRmse > 0 ? fmtStat(mm.rmse / modelRmse, 3) : DASH}</td>
                      )}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {(e.points?.length ?? 0) > 0 && (
            <div className="mt-3">
              <OosChart evaluation={e} />
            </div>
          )}
          {e.note && <p className="mt-1.5 text-xs text-muted">{e.note}</p>}
        </Card>
      )}
    </div>
  )
}

/* ───────────── Clusters ───────────── */

type Member = StudioClusters['members'][number]

export function ClustersTab({ run, result, a, sectorNames }: { run: StudioRunView; result: StudioResultView; a: Assigned; sectorNames: Record<string, string> }) {
  const c = result.clusters as StudioClusters
  const p = a.clusterPlots
  const linkage = Array.isArray(result.summary.linkage_matrix) ? (result.summary.linkage_matrix as number[][]) : null
  const patchDraft = useStudio((s) => s.patchDraft)
  const draftMethod = useStudio((s) => s.draft.method)
  const draftParams = useStudio((s) => s.draft.params)
  const securityLevel = (run.study?.dataset.entity_level ?? 'security') === 'security' && c.members.some((m) => m.sector_id)
  const [cl, setCl] = useState<string>('all')
  const [q, setQ] = useState('')
  const members = useMemo(() => {
    const s = q.trim().toLowerCase()
    return c.members.filter((m) => (cl === 'all' || String(m.cluster) === cl) && (!s || String(m.label ?? '').toLowerCase().includes(s) || String(m.entity_id).toLowerCase().includes(s)))
  }, [c.members, cl, q])
  const cols = useMemo<ColumnDef<Member>[]>(
    () => [
      {
        id: 'cluster',
        header: 'Cluster',
        size: 80,
        accessorFn: (m) => m.cluster,
        cell: (x) => (
          <span className="inline-flex items-center gap-1.5">
            <ClusterSwatch cluster={x.row.original.cluster} />
            <span className="num">{x.row.original.cluster === -1 ? 'noise' : x.row.original.cluster}</span>
          </span>
        ),
      },
      {
        id: 'label',
        header: 'Member',
        size: 160,
        accessorFn: (m) => m.label ?? m.entity_id,
        cell: (x) => {
          const m = x.row.original
          const lbl = String(m.label ?? m.entity_id)
          return securityLevel ? (
            <Link to={`/company/${encodeURIComponent(lbl)}`} className="font-medium text-fg hover:text-accent-text hover:underline">
              {lbl}
            </Link>
          ) : (
            <span className="font-medium text-fg">{lbl}</span>
          )
        },
      },
      { id: 'sector', header: 'Sector', size: 180, accessorFn: (m) => (m.sector_id ? (sectorNames[m.sector_id] ?? m.sector_id) : DASH), cell: (x) => <span className="text-muted">{x.getValue<string>()}</span> },
      { id: 'dist', header: 'Distance to centre', size: 140, accessorFn: (m) => m.distance_to_center ?? null, cell: (x) => <span className="num">{fmtStat(x.getValue<number | null>(), 3)}</span>, meta: { align: 'right' } },
      ...(c.soft
        ? [
            {
              id: 'prob',
              header: 'Membership prob.',
              size: 140,
              accessorFn: (m) => m.probability ?? null,
              cell: (x) => <span className="num">{fmtPct(x.getValue<number | null>(), 0)}</span>,
              meta: { align: 'right' },
            } as ColumnDef<Member>,
          ]
        : []),
    ],
    [securityLevel, sectorNames, c.soft],
  )
  const fitEntries = Object.entries(result.fitStats)
  const probabilities = useMemo(
    () => (c.soft ? Object.fromEntries(c.members.filter((m) => typeof m.probability === 'number').map((m) => [String(m.label), m.probability as number])) : undefined),
    [c.soft, c.members],
  )
  const canPeerSet = securityLevel && c.members.some((m) => m.entity_id)
  return (
    <div className="space-y-3">
      {canPeerSet && <PeerSetBar runId={run.status.runId} defaultName={run.status.name ?? result.title} n={c.members.filter((m) => m.cluster >= 0).length} k={c.n_clusters} />}
      <div className="grid gap-3 @4xl:grid-cols-3">
        <Card title={`${c.n_clusters} clusters`} sub={fmtCell(result.summary.cut ?? result.summary.k_selected_by ?? '')}>
          <ClusterSizes sizes={c.sizes} />
          {(c.noise_count ?? 0) > 0 && (
            <div className="mt-1 flex items-center gap-2 text-xs">
              <span aria-hidden className="inline-block size-2.5 rounded-full border border-faint" />
              <span className="text-muted">noise (unassigned)</span>
              <span className="num ml-auto text-fg">{c.noise_count}</span>
            </div>
          )}
          {c.soft && typeof c.mean_max_probability === 'number' && (
            <p className="mt-1 text-xs text-muted">
              Soft clustering: average membership probability <b className="num text-fg">{fmtPct(c.mean_max_probability, 0)}</b>. Faded markers are uncertain members.
            </p>
          )}
          {fitEntries.length > 0 && <KeyValues className="mt-3" entries={fitEntries} />}
        </Card>
        {p.pca_scatter && (
          <Card title={p.pca_scatter.title} className="@4xl:col-span-2">
            <PointsPlot plot={p.pca_scatter} height={300} sectorNames={sectorNames} probabilities={probabilities} />
          </Card>
        )}
      </div>
      {p.dendrogram && (
        <Card title={p.dendrogram.title}>
          <DendrogramPlot
            plot={p.dendrogram}
            linkage={linkage}
            height={280}
            memberClusters={Object.fromEntries(c.members.map((m) => [String(m.label ?? m.entity_id), m.cluster]))}
            onUseCut={
              draftMethod === 'hierarchical'
                ? (h) => {
                    patchDraft({ params: { ...draftParams, distance_threshold: Number(h.toPrecision(4)), n_clusters: null } })
                    toast({ tone: 'info', title: 'Cut copied to the builder', message: `distance_threshold = ${fmtStat(h, 4)} — run again to refit` })
                  }
                : undefined
            }
          />
        </Card>
      )}
      {(p.elbow || p.silhouette || p.bic) && (
        <div className="grid gap-3 @3xl:grid-cols-2">
          {p.bic && <PlotCard plotKey="bic" plot={p.bic} height={200} />}
          {p.elbow && <PlotCard plotKey="elbow" plot={p.elbow} height={200} />}
          {p.silhouette && <PlotCard plotKey="silhouette" plot={p.silhouette} height={200} />}
        </div>
      )}
      {p.correlation_heatmap && <PlotCard plotKey="correlation_heatmap" plot={p.correlation_heatmap} height={Math.min(520, 120 + 24 * (p.correlation_heatmap.y?.length ?? 10))} />}
      {p.profile_heatmap && <PlotCard plotKey="profile_heatmap" plot={p.profile_heatmap} height={Math.max(160, 90 + 30 * (p.profile_heatmap.y?.length ?? 3))} />}
      <Card title="Cluster profiles" sub="Most distinctive features (mean z-score vs the whole sample) and dominant sectors">
        <div className="grid gap-2 @xl:grid-cols-2 @4xl:grid-cols-3">
          {c.profiles.map((pr) => {
            const z = Object.entries(pr.z_mean ?? {})
              .filter(([, v]) => typeof v === 'number' && Number.isFinite(v))
              .sort((x, y) => Math.abs(y[1] as number) - Math.abs(x[1] as number))
              .slice(0, 4)
            return (
              <div key={pr.cluster} className="rounded-sm border border-border bg-surface-2 p-2">
                <div className="mb-1 flex items-center gap-1.5 text-sm font-semibold text-fg">
                  <ClusterSwatch cluster={pr.cluster} /> Cluster {pr.cluster}
                  <span className="num ml-auto text-xs font-normal text-muted">{pr.size} members</span>
                </div>
                <ul className="space-y-0.5 text-xs">
                  {z.map(([k, v]) => (
                    <li key={k} className="flex justify-between gap-2">
                      <span className="truncate text-muted">{k.length > 10 && /^\d{4}-/.test(k) ? fmtDate(k.slice(0, 10)) : k}</span>
                      <span className={cn('num', (v as number) > 0 ? 'text-up' : 'text-down')}>
                        {(v as number) > 0 ? '▲' : '▼'} {fmtStat(v, 2)}σ
                      </span>
                    </li>
                  ))}
                </ul>
                {pr.top_sectors && pr.top_sectors.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {pr.top_sectors.slice(0, 4).map((s, si) => (
                      <Badge key={`${s.sector_id ?? 'na'}-${si}`} tone="neutral" className="normal-case">
                        {s.sector_id ? (sectorNames[s.sector_id] ?? s.sector_id) : 'unclassified'} · {fmtStat(s.count)}
                      </Badge>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </Card>
      <Card
        title="Members"
        sub={`${members.length} of ${c.members.length}`}
        actions={
          <div className="flex items-center gap-1.5">
            <Input placeholder="Filter members" aria-label="Filter members" value={q} onChange={(e) => setQ(e.target.value)} className="w-40" />
            <Select
              label="Cluster"
              value={cl}
              onChange={setCl}
              options={[{ value: 'all', label: 'All clusters' }, ...c.sizes.map((_, i) => ({ value: String(i), label: `Cluster ${i}` })), ...((c.noise_count ?? 0) > 0 ? [{ value: '-1', label: 'Noise' }] : [])]}
            />
          </div>
        }
      >
        <DataTable data={members} columns={cols} ariaLabel="Cluster members" height={Math.min(380, 34 + members.length * 26)} initialSorting={[{ id: 'cluster', desc: false }]} getRowId={(m, i) => `${m.entity_id}-${i}`} />
      </Card>
      {a.clusterTables
        .filter((t) => t.id !== 'profiles' || c.features.length <= 30)
        .map((t) => (
          <Card key={t.id} title={t.title}>
            <ResultTable table={t} maxRows={20} />
          </Card>
        ))}
    </div>
  )
}

/* ───────────── PCA ───────────── */

export function PcaTab({ result, a }: { result: StudioResultView; a: Assigned }) {
  const p = a.pcaPlots
  const ev = a.pcaTables.find((t) => t.id === 'explained_variance')
  const load = a.pcaTables.find((t) => t.id === 'loadings')
  const nFeat = p.loadings_heatmap?.y?.length ?? 10
  return (
    <div className="space-y-3">
      <div className="grid gap-3 @3xl:grid-cols-2">
        {p.scree && <PlotCard plotKey="scree" plot={p.scree} height={240} />}
        {ev && (
          <Card title={ev.title} sub={`${fmtCell(result.summary.components_for_80pct)} components explain ≥ 80%`}>
            <ResultTable table={{ ...ev, rows: ev.rows.map((r) => r.map((v, i) => (i >= 2 && typeof v === 'number' ? fmtPct(v, 1) : v))) }} />
          </Card>
        )}
      </div>
      {p.loadings_heatmap && <PlotCard plotKey="loadings_heatmap" plot={p.loadings_heatmap} height={Math.min(560, 70 + 24 * nFeat)} />}
      <div className="grid gap-3 @3xl:grid-cols-2">
        {p.biplot && <PlotCard plotKey="biplot" plot={p.biplot} height={320} />}
        {p.pca_scatter && (
          <Card title={p.pca_scatter.title}>
            <PointsPlot plot={p.pca_scatter} height={320} />
          </Card>
        )}
      </div>
      {p.scores_time && <PlotCard plotKey="scores_time" plot={p.scores_time} height={260} />}
      {load && (
        <Card title={load.title}>
          <ResultTable table={load} />
        </Card>
      )}
    </div>
  )
}

/* ───────────── Other output ───────────── */

export function OtherTab({ a }: { result: StudioResultView; a: Assigned }) {
  return (
    <div className="space-y-3">
      <div className="grid gap-3 @3xl:grid-cols-2">
        {a.otherPlots.map(([k, p]) => (
          <PlotCard key={k} plotKey={k} plot={p} />
        ))}
      </div>
      {a.otherTables.map((t) => (
        <Card key={t.id} title={t.title}>
          <ResultTable table={t} />
        </Card>
      ))}
    </div>
  )
}

/* ───────────── Data & reproducibility ───────────── */

export function DataTab({ run }: { run: StudioRunView }) {
  const d = run.dataset
  const r = run.reproducibility
  return (
    <div className="space-y-3">
      {d && (
        <div className="grid gap-3 @3xl:grid-cols-2">
          <Card title="Dataset">
            <KeyValues
              entries={[
                ['kind', d.kind],
                ['frequency', d.frequency],
                ['period', `${fmtDate(d.start.slice(0, 10))} → ${fmtDate(d.end.slice(0, 10))}`],
                ['rows', d.n_rows],
                ['entities', d.n_entities ?? null],
                ['periods', d.n_periods ?? null],
                ['macro_alignment', d.macro_alignment ?? null],
                ['build_time', fmtSeconds(d.build_seconds)],
                ['cached', d.cached ?? null],
                ['dataset_key', d.key],
                ['snapshot', d.snapshot_id],
              ]}
            />
          </Card>
          {r && (
            <Card title="Reproducibility" sub="Same spec + snapshot + seed ⇒ same numbers">
              <KeyValues
                entries={[
                  ['spec_hash', r.spec_hash],
                  ['seed', r.seed],
                  ['snapshot', r.snapshot_id],
                  ...Object.entries(r.timings ?? {}).map(([k, v]) => [k, typeof v === 'number' ? fmtSeconds(v) : v] as [string, unknown]),
                  ...Object.entries(r.versions ?? {}),
                ]}
              />
            </Card>
          )}
        </div>
      )}
      {d && d.coverage.length > 0 && (
        <Card title="Variable coverage">
          <div className="overflow-x-auto">
            <table className="w-full text-sm" aria-label="Variable coverage">
              <thead>
                <tr className="border-b border-border text-left text-2xs tracking-wide text-muted uppercase">
                  <th className="px-2 py-1">Variable</th>
                  <th className="px-2 py-1 text-right">Non-null</th>
                  <th className="px-2 py-1">Coverage</th>
                  <th className="px-2 py-1">First</th>
                  <th className="px-2 py-1">Last</th>
                  <th className="px-2 py-1 text-right">Entities</th>
                  <th className="px-2 py-1">Look-ahead</th>
                </tr>
              </thead>
              <tbody>
                {d.coverage.map((c) => (
                  <tr key={c.column} className="border-b border-border/60">
                    <td className="px-2 py-1 font-medium text-fg">{c.column}</td>
                    <td className="num px-2 py-1 text-right">{fmtStat(c.nNonNull)}</td>
                    <td className="px-2 py-1">
                      <CoverageBar pct={c.pctNonNull} />
                    </td>
                    <td className="num px-2 py-1 text-muted">{c.firstDate ? fmtDate(c.firstDate.slice(0, 10)) : DASH}</td>
                    <td className="num px-2 py-1 text-muted">{c.lastDate ? fmtDate(c.lastDate.slice(0, 10)) : DASH}</td>
                    <td className="num px-2 py-1 text-right text-muted">{fmtStat(c.nEntities)}</td>
                    <td className="px-2 py-1">{c.usesFuture ? <Badge tone="warn">uses future</Badge> : <span className="text-xs text-muted">no</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {run.study && (
        <Card title="Study spec (JSON)">
          <details>
            <summary className="cursor-pointer text-xs text-muted hover:text-fg">Show the exact request body</summary>
            <pre className="num mt-2 max-h-80 overflow-auto rounded-sm bg-surface-2 p-2 text-2xs text-fg">{JSON.stringify(run.study, null, 2)}</pre>
          </details>
        </Card>
      )}
    </div>
  )
}

export function CoverageBar({ pct }: { pct: number }) {
  const v = Math.max(0, Math.min(1, pct))
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="relative h-1.5 w-20 overflow-hidden rounded-full bg-surface-3">
        <span className={cn('absolute inset-y-0 left-0 rounded-full', v >= 0.8 ? 'bg-up' : v >= 0.5 ? 'bg-warn' : 'bg-down')} style={{ width: `${v * 100}%` }} />
      </span>
      <span className="num w-10 text-xs text-muted">{fmtPct(v, 0)}</span>
    </span>
  )
}

/* ───────────── method-specific tab (v1.6: causal, multivariate, volatility, panel premia) ───────────── */

const PLOT_HEIGHT: Record<string, number> = { event_path: 300, ts_band: 260, regime_prob: 360 }

function num(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Group-time ATT(g, t) table → heatmap (rows: adoption cohort, columns: calendar time). */
function groupTimePlot(t: StudioTable): StudioPlot | null {
  const gi = t.columns.indexOf('group')
  const ti = t.columns.indexOf('time')
  const ai = t.columns.indexOf('att')
  if (gi < 0 || ti < 0 || ai < 0) return null
  const groups = [...new Set(t.rows.map((r) => String(r[gi])))].sort()
  const times = [...new Set(t.rows.map((r) => String(r[ti])))].sort()
  const z = groups.map(() => times.map(() => null as number | null))
  for (const r of t.rows) z[groups.indexOf(String(r[gi]))][times.indexOf(String(r[ti]))] = num(r[ai])
  return { kind: 'heatmap', title: 'Group-time ATT(g, t)', x: times, y: groups.map((g) => `cohort ${g}`), z }
}

export function SpecialTab({ result, a }: { result: StudioResultView; a: Assigned }) {
  const sp = a.special
  if (!sp) return null
  const pretrend = result.tests.filter((t) => /pre-?trend|pre-period/i.test(t.name))
  const weights = sp.tables.find((t) => t.id === 'weights')
  const regimes = sp.tables.find((t) => t.id === 'regimes')
  const gt = sp.tables.find((t) => t.id === 'group_time_att')
  const gtPlot = gt ? groupTimePlot(gt) : null
  const grs = result.tests.filter((t) => /\bGRS\b/.test(t.name))
  const impTable = sp.tables.find((t) => t.id === 'importance')
  const impRows = impTable
    ? impTable.rows.map((r) => ({ feature: String(r[0]), impurity: num(r[impTable.columns.indexOf('impurity')]), permutation: num(r[impTable.columns.indexOf('permutation')]) }))
    : null
  const selected = result.coefficients.filter((c) => c.selected === true)
  const unselected = result.coefficients.filter((c) => c.selected === false)
  const plots = sp.plots.filter(([k]) => !(k === 'importance' && impRows))
  const fullWidth = (p: StudioPlot) =>
    ['irf', 'fevd', 'regime_prob', 'event_path', 'coef_path', 'shap_beeswarm'].includes(p.kind) || plots.length === 1 || ((p.x as unknown[] | undefined)?.length ?? p.series?.[0]?.x.length ?? 0) > 400 || (p.series?.length ?? 0) > 4
  return (
    <div className="space-y-3">
      {pretrend.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm">
          {pretrend.map((t, i) => (
            <span key={i} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
              <VerdictBadge v={t.verdict} />
              <span className="text-fg">{t.name}</span>
              <span className="num text-muted">
                stat {fmtStat(t.statistic, 3)} · p {fmtP(t.pValue)}
              </span>
              <span className="text-xs text-muted">{t.verdict === 'fail' ? '— pre-treatment effects are not zero: parallel trends is doubtful' : t.verdict === 'pass' ? '— no evidence against parallel trends' : ''}</span>
            </span>
          ))}
        </div>
      )}
      {grs.map((t, i) => (
        <div key={i} className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm">
          <span className="font-semibold text-fg">{t.name}</span>
          <span className="num text-muted">
            F {fmtStat(t.statistic, 3)}
            {t.df !== null ? ` ${Array.isArray(t.df) ? fmtCell(t.df) : `(${fmtCell(t.df)})`}` : ''} · p {fmtP(t.pValue)}
          </span>
          <span className="font-medium text-fg">{t.conclusion}</span>
          {t.null_hypothesis && <span className="text-xs text-muted">H0: {t.null_hypothesis}</span>}
        </div>
      ))}
      {(selected.length > 0 || unselected.length > 0) && (
        <Card title={`Selected features (${selected.length} of ${selected.length + unselected.length})`} sub="Non-zero coefficients at the chosen penalty; standardized size in brackets">
          <div className="flex flex-wrap gap-1.5">
            {[...selected]
              .sort((x, y) => Math.abs(y.std_estimate ?? 0) - Math.abs(x.std_estimate ?? 0))
              .map((c) => (
                <Badge key={c.term} tone={(c.std_estimate ?? c.estimate ?? 0) >= 0 ? 'up' : 'down'} className="normal-case">
                  {(c.std_estimate ?? c.estimate ?? 0) >= 0 ? '▲' : '▼'} {c.term} ({fmtStat(c.std_estimate ?? c.estimate, 3)})
                </Badge>
              ))}
            {unselected.map((c) => (
              <Badge key={c.term} tone="neutral" className="normal-case line-through opacity-70">
                {c.term}
              </Badge>
            ))}
          </div>
        </Card>
      )}
      {impRows && (
        <Card title="Feature importance" sub="Permutation importance on held-out folds vs impurity / gain in-sample">
          <ImportanceBars rows={impRows} />
        </Card>
      )}
      {regimes && (
        <div className="flex flex-wrap gap-2">
          {regimes.rows.map((r, i) => {
            const c = (k: string) => r[regimes.columns.indexOf(k)]
            return (
              <div key={i} className="rounded-md border border-border bg-surface px-3 py-2 text-sm">
                <div className="font-semibold text-fg">{String(c('label') ?? `regime ${c('regime')}`)}</div>
                <div className="num text-xs text-muted">
                  expected duration <b className="text-fg">{fmtStat(c('expected_duration'), 3)}</b> periods · share of time {fmtPct(num(c('share_of_time')), 0)}
                </div>
              </div>
            )
          })}
        </div>
      )}
      <div className="grid gap-3 @3xl:grid-cols-2">
        {plots.map(([k, p]) => (
          <Card key={k} title={p.title} className={fullWidth(p) ? '@3xl:col-span-2' : undefined}>
            <PlotView plot={p} plotKey={k} height={PLOT_HEIGHT[p.kind] ?? 240} />
          </Card>
        ))}
      </div>
      {weights && (
        <Card title={weights.title} sub="Convex weights of the donors that make up the synthetic unit">
          <WeightsBar
            title={weights.title}
            items={weights.rows.map((r) => ({ label: String(r[weights.columns.indexOf('label')] ?? r[0]), value: num(r[weights.columns.indexOf('weight')]) ?? 0 }))}
          />
        </Card>
      )}
      {gtPlot && (
        <Card title="Group-time ATT(g, t)" sub="Callaway–Sant'Anna: effect for each adoption cohort at each date">
          <PlotView plot={gtPlot} plotKey="group_time" height={Math.max(150, 80 + 34 * (gtPlot.y?.length ?? 1))} />
        </Card>
      )}
      {result.method === 'factor_model' && result.coefficients.length > 0 && (
        <Card title="Risk premia (second pass)" sub="Cross-sectional regression of average test-asset returns on their betas">
          <CoefTable coefs={result.coefficients} statLabel="t" />
        </Card>
      )}
      {sp.tables
        .filter((t) => t !== weights && t !== impTable)
        .map((t) => (
          <Card key={t.id} title={t.title}>
            <ResultTable table={t} maxRows={t.id === 'group_time_att' || t.id === 'dynamic_att' ? 15 : 30} highlightFirst={t.id === 'leaderboard'} />
          </Card>
        ))}
    </div>
  )
}

/** Tests whose result is the answer rather than a residual check (Hausman, first-stage F, Johansen, Patell …). */
export function KeyTests({ result }: { result: StudioResultView }) {
  const key = result.tests.filter((t) => KEY_GROUPS.includes(t.group))
  if (!key.length) return null
  return (
    <Card title="Key tests" sub="Specification, identification and inference">
      <ul className="divide-y divide-border/60">
        {key.map((t, i) => (
          <li key={`${t.name}-${i}`} className="flex flex-wrap items-center gap-x-3 gap-y-0.5 py-1.5 text-sm">
            <VerdictBadge v={t.verdict} />
            <span className="font-medium text-fg">{t.name}</span>
            <span className="num text-xs text-muted">
              stat {fmtStat(t.statistic, 3)}
              {t.pValue !== null ? ` · p ${fmtP(t.pValue)}` : ''}
              {t.df !== null ? ` · df ${fmtCell(t.df)}` : ''}
            </span>
            {t.conclusion && <span className="ml-auto text-xs font-medium text-fg">{t.conclusion}</span>}
          </li>
        ))}
      </ul>
    </Card>
  )
}

/** Save the security-level cluster membership of a run as a reusable peer set (Company 360 → Peers). */
function PeerSetBar({ runId, defaultName, n, k }: { runId: string; defaultName: string; n: number; k: number }) {
  const qc = useQueryClient()
  const [name, setName] = useState(defaultName)
  const [saved, setSaved] = useState<string | null>(null)
  const m = useMutation({
    mutationFn: () => studioApi.createPeerSet(runId, { name: name.trim() || defaultName }),
    onSuccess: (ps) => {
      setSaved(ps.peer_set_id)
      void qc.invalidateQueries({ queryKey: sk.peerSets() })
      toast({ tone: 'success', title: 'Peer set saved', message: `${ps.name} · ${ps.n_members} companies in ${ps.n_clusters} groups — pick it in Company 360 → Peers` })
    },
    onError: (e) => toast({ tone: 'error', title: 'Could not save the peer set', message: e instanceof Error ? e.message : String(e) }),
  })
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-surface-2 px-3 py-2 text-sm">
      <span className="text-fg">
        Use these {k} clusters ({n} companies) as peer groups
      </span>
      <Input aria-label="Peer set name" className="h-7 w-64" value={name} onChange={(e) => setName(e.target.value)} />
      <Button size="sm" variant="outline" disabled={m.isPending} onClick={() => m.mutate()}>
        <Users /> Save as peer set
      </Button>
      {saved && <span className="num text-xs text-muted">saved {saved}</span>}
    </div>
  )
}
