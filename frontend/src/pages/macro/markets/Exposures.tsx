import { useMemo, useState } from 'react'
import { useExposureHistory, useExposures } from '@/api/macro/hooks'
import { type ExposureEntity, type ExposureModel, type MxExposures, exposureMatrix } from '@/api/macro/v34'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Input, Segmented, Select } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { DASH, fmtDate } from '@/lib/format'
import { cn } from '@/lib/utils'
import { MacroLines } from '../charts'
import { fmtChange, fmtMacro } from '../fmt'

const ENTITIES: { value: ExposureEntity; label: string }[] = [
  { value: 'sector', label: 'Sectors' },
  { value: 'subsector', label: 'Subsectors' },
  { value: 'region', label: 'Regions' },
  { value: 'factor', label: 'Factors' },
  { value: 'etf', label: 'ETFs' },
  { value: 'index', label: 'Indices' },
  { value: 'future', label: 'Futures' },
]

const FREQ: Record<string, string> = { W: 'weekly', M: 'monthly', D: 'daily' }

export function Exposures() {
  const [et, setEt] = useState<ExposureEntity>('sector')
  const [model, setModel] = useState<ExposureModel>('joint')
  const [w, setW] = useState('cw')
  const [q, setQ] = useState('')
  const [pick, setPick] = useState<{ id: string; label: string } | null>(null)
  const ex = useExposures(et, model)
  return (
    <div className="space-y-3">
      <Panel
        title="Macro exposure heatmap"
        subtitle={<span className="inline-flex flex-wrap items-center gap-1.5"><Badge tone="info">model-based</Badge> rolling 156-week betas of weekly returns to official macro factors · click a row for its beta history</span>}
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Select label="Entities" value={et} onChange={(v) => { setEt(v); setPick(null) }} options={ENTITIES} />
            <Segmented
              label="Model"
              size="xs"
              value={model}
              onChange={(v) => { setModel(v); setPick(null) }}
              options={[
                { value: 'joint', label: 'Joint' },
                { value: 'univariate', label: 'Single-factor' },
                { value: 'surprise', label: 'Surprise' },
              ]}
            />
            {['sector', 'subsector', 'region'].includes(et) && <Select label="Weighting" value={w} onChange={setW} options={[{ value: 'cw', label: 'Cap-weighted' }, { value: 'ew', label: 'Equal-weighted' }]} />}
            <Input aria-label="Filter rows" placeholder="Filter" value={q} onChange={(e) => setQ(e.target.value)} className="w-32" />
          </div>
        }
        flush
      >
        <QueryView query={ex} skeleton={<Skeleton className="m-3 h-96" />}>
          {(d) => <Heat d={d} weighting={['sector', 'subsector', 'region'].includes(et) ? w : null} filter={q} selected={pick?.id ?? null} onPick={(id, label) => setPick({ id, label })} />}
        </QueryView>
      </Panel>
      {pick && <HistoryPanel et={et} model={model} id={pick.id} label={pick.label} weighting={['sector', 'subsector', 'region'].includes(et) ? w : 'none'} />}
    </div>
  )
}

function cellBg(beta: number | null, scale: number, sig: number): React.CSSProperties {
  if (beta === null || scale <= 0) return { background: 'var(--surface-2)' }
  const k = Math.min(1, Math.abs(beta) / scale)
  const pct = Math.round((sig ? 12 + 40 * k : 6 + 14 * k) * 10) / 10
  return { background: `color-mix(in srgb, ${beta >= 0 ? 'var(--c2)' : 'var(--c1)'} ${pct}%, var(--surface))` }
}

function Heat({ d, weighting, filter, selected, onPick }: { d: MxExposures; weighting: string | null; filter: string; selected: string | null; onPick: (id: string, label: string) => void }) {
  const items = useMemo(() => {
    const s = filter.trim().toLowerCase()
    return d.items.filter((i) => (!weighting || i.weighting === weighting) && (!s || `${i.name} ${i.ticker ?? ''} ${i.entity_id}`.toLowerCase().includes(s)))
  }, [d.items, weighting, filter])
  const m = useMemo(() => exposureMatrix(items, d.factors), [items, d.factors])
  const scale = useMemo(() => Object.fromEntries(d.factors.map((f, j) => [f.factor_id, Math.max(1e-9, ...m.map((r) => Math.abs(r.cells[j].beta ?? 0)))])), [m, d.factors])
  return (
    <div>
      <div className="max-h-[560px] overflow-auto">
        <table className="w-full border-separate border-spacing-0 text-xs" aria-label="Macro betas by entity and factor">
          <thead>
            <tr>
              <th className="sticky top-0 left-0 z-[3] bg-surface px-2 py-1 text-left text-2xs text-muted uppercase">Entity</th>
              {d.factors.map((f) => (
                <th key={f.factor_id} className="sticky top-0 z-[2] min-w-[86px] bg-surface px-1 py-1 text-center align-bottom text-2xs font-semibold text-fg">
                  <Tip content={<div className="max-w-xs">{f.label} · {f.per} · source {f.source}</div>}>
                    <span tabIndex={0} className="cursor-help">{f.label}</span>
                  </Tip>
                  <div className="font-normal text-muted">{f.per}</div>
                </th>
              ))}
              <th className="sticky top-0 z-[2] bg-surface px-2 py-1 text-right text-2xs text-muted uppercase">R²</th>
            </tr>
          </thead>
          <tbody>
            {m.map((r) => (
              <tr key={`${r.id}-${r.weighting}`} className={cn('cursor-pointer', selected === r.id && 'outline outline-1 outline-accent')} onClick={() => onPick(r.id, r.label)}>
                <th scope="row" className="sticky left-0 z-[1] max-w-[220px] truncate bg-surface px-2 py-0.5 text-left font-medium text-fg" title={r.label}>
                  {r.label}
                </th>
                {r.cells.map((c) => (
                  <td key={c.factor} className="border border-surface p-0">
                    <Tip content={<span className="num">β {fmtMacro(c.beta, 3)} · t {fmtChange(c.t, 2)} {c.sig === 2 ? '(1% sig.)' : c.sig === 1 ? '(5% sig.)' : '(not significant)'}</span>}>
                      <span
                        className={cn('num flex h-6 items-center justify-center gap-0.5 text-2xs', c.sig ? 'font-semibold text-fg' : 'text-muted')}
                        style={cellBg(c.beta, scale[c.factor], c.sig)}
                        aria-label={`${r.label} ${c.factor} beta ${fmtMacro(c.beta, 3)}, t ${fmtChange(c.t)}`}
                      >
                        {fmtMacro(c.beta, 2)}
                        {c.sig === 2 ? '**' : c.sig === 1 ? '*' : ''}
                      </span>
                    </Tip>
                  </td>
                ))}
                <td className="num px-2 py-0.5 text-right text-muted">{fmtMacro(r.r2, 2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="border-t border-border px-3 py-2 text-2xs text-muted">
        Blue = positive beta, orange = negative (intensity scaled within each column). Bold with ** / * = |t| ≥ 2.58 / 1.96 (HAC standard errors); faint = not significant. {String((d.method as Record<string, unknown>).label ?? '')} · as of {d.as_of ? fmtDate(d.as_of) : DASH} · frequency {FREQ[d.frequency ?? 'W'] ?? d.frequency} (available: {(d.frequencies_available ?? ['W']).map((f) => FREQ[f] ?? f).join(', ')}) · {items.length} of {d.total} rows.
      </p>
    </div>
  )
}

function HistoryPanel({ et, model, id, label: name, weighting }: { et: ExposureEntity; model: ExposureModel; id: string; label: string; weighting: string }) {
  const q = useExposureHistory(et, model, id)
  return (
    <Panel title={`Beta history: ${name}`} subtitle="Point-in-time month-end betas with 90% bands (±1.645 SE)">
      <QueryView query={q} skeleton={<Skeleton className="h-64" />}>
        {(d) => {
          const hist = d.history.filter((h) => h.weighting === weighting || weighting === 'none' || !h.weighting)
          if (!hist.length) return <p className="py-6 text-center text-xs text-muted">No beta history for this entity.</p>
          const label = (fid: string) => d.factors.find((f) => f.factor_id === fid)?.label ?? fid
          return (
            <MacroLines
              lines={hist.map((h) => ({
                name: label(h.factor_id),
                points: h.points.map((p) => ({ date: p.date, value: p.beta ?? null })),
                band: { lo: h.points.map((p) => (typeof p.se === 'number' && typeof p.beta === 'number' ? p.beta - 1.645 * p.se : null)), hi: h.points.map((p) => (typeof p.se === 'number' && typeof p.beta === 'number' ? p.beta + 1.645 * p.se : null)), label: '90% band' },
              }))}
              height={180}
              zeroLine
              ariaLabel={`Beta history for ${name}`}
            />
          )
        }}
      </QueryView>
    </Panel>
  )
}
