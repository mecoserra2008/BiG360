import { ChevronDown, Plus, Trash2 } from 'lucide-react'
import { ExperimentalNote } from '@/components/conformal/notes'
import { type ReactNode, useMemo, useState } from 'react'
import { parseSweepValues, signalVar, sweepSize } from '@/api/backtest/adapters'
import type { BtCatalog, BtDraft, BtEvent, BtScreenRule, BtSignalVar } from '@/api/backtest/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox, Input, Segmented, Select, Switch } from '@/components/ui/inputs'
import { cn } from '@/lib/utils'

type Patch = (fn: (s: BtDraft) => BtDraft) => void

function Section({ title, badge, children, defaultOpen = true }: { title: string; badge?: ReactNode; children: ReactNode; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="border-b border-border">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm font-semibold text-fg hover:bg-surface-2" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        <ChevronDown className={cn('size-3.5 text-muted transition-transform', !open && '-rotate-90')} aria-hidden />
        <span className="flex-1">{title}</span>
        {badge}
      </button>
      {open && <div className="space-y-2 px-3 pb-3">{children}</div>}
    </section>
  )
}

function Row({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-2">
      <span className="truncate text-xs text-muted" title={hint ?? label}>
        {label}
      </span>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">{children}</div>
    </div>
  )
}

/** Checkbox with a visible text label (the shared Checkbox only carries an aria-label). */
function LabeledCheck({ label, checked, onCheckedChange }: { label: string; checked: boolean; onCheckedChange: (on: boolean) => void }) {
  return (
    <label className="inline-flex cursor-pointer items-center gap-1 text-xs text-fg">
      <Checkbox label={label} checked={checked} onCheckedChange={(v) => onCheckedChange(v === true)} />
      {label}
    </label>
  )
}

function Num({ value, onChange, label, step = 1, min, max, placeholder, className, pct = false }: { value: number | null | undefined; onChange: (v: number | null) => void; label: string; step?: number; min?: number; max?: number; placeholder?: string; className?: string; pct?: boolean }) {
  const shown = value === null || value === undefined ? '' : pct ? String(Math.round(value * 10000) / 100) : String(value)
  return (
    <Input
      type="number"
      aria-label={label}
      className={cn('w-20', className)}
      value={shown}
      step={step}
      min={min}
      max={max}
      placeholder={placeholder}
      onChange={(e) => {
        const t = e.target.value
        if (t === '') return onChange(null)
        const v = Number(t)
        if (Number.isFinite(v)) onChange(pct ? v / 100 : v)
      }}
    />
  )
}

const listText = (xs: string[] | undefined) => (xs ?? []).join(', ')
const textList = (t: string) =>
  t
    .split(',')
    .map((x) => x.trim())
    .filter(Boolean)

const OPS: { value: BtScreenRule['op']; label: string }[] = [
  { value: 'gte', label: '≥' },
  { value: 'gt', label: '>' },
  { value: 'lte', label: '≤' },
  { value: 'lt', label: '<' },
  { value: 'eq', label: '=' },
  { value: 'neq', label: '≠' },
  { value: 'in', label: 'in' },
  { value: 'not_null', label: 'has value' },
]

const SWEEP_PATHS = [
  'construction.quantiles',
  'construction.top_n',
  'construction.weighting',
  'construction.holding_periods',
  'construction.rebalance',
  'construction.side',
  'construction.neutralize',
  'construction.max_weight',
  'signal.lag_periods',
  'event.hold_days',
  'event.stop_loss',
  'event.take_profit',
  'costs.commission_bps',
]

export function Builder({ catalog, spec, patch, running, onRun, onValidate, onSave, validating }: { catalog: BtCatalog | undefined; spec: BtDraft; patch: Patch; running: boolean; onRun: (mode: 'auto' | 'async') => void; onValidate: () => void; onSave: () => void; validating: boolean }) {
  const u = spec.universe
  const assets = u.kind === 'assets'
  const ev = spec.mode === 'event'
  const sources = useMemo(() => (catalog?.sources ?? []).filter((s) => s.universe === 'both' || s.universe === (assets ? 'assets' : 'securities')), [catalog, assets])
  const triggers = useMemo(() => (catalog?.event_triggers ?? []).filter((t) => t.universe === 'both' || t.universe === (assets ? 'assets' : 'securities')), [catalog, assets])
  const regimes = catalog?.regimes ?? []
  const setU = (p: Partial<BtDraft['universe']>) => patch((s) => ({ ...s, universe: { ...s.universe, ...p } }))
  const setC = (p: Partial<BtDraft['construction']>) => patch((s) => ({ ...s, construction: { ...s.construction, ...p } }))
  const setCost = (p: Partial<BtDraft['costs']>) => patch((s) => ({ ...s, costs: { ...s.costs, ...p } }))
  const setP = (p: Partial<BtDraft['period']>) => patch((s) => ({ ...s, period: { ...s.period, ...p } }))
  const setR = (p: Partial<BtDraft['robustness']>) => patch((s) => ({ ...s, robustness: { ...s.robustness, ...p } }))
  const setE = (p: Partial<BtEvent>) => patch((s) => ({ ...s, event: { ...(s.event as BtEvent), ...p } }))
  const setVar = (i: number, p: Partial<BtSignalVar>) => patch((s) => ({ ...s, signal: { ...s.signal, variables: s.signal.variables.map((v, j) => (j === i ? { ...v, ...p } : v)) } }))
  const c = spec.construction
  const cost = spec.costs
  const per = spec.period
  const rb = spec.robustness
  const evb = spec.event
  const nSweep = sweepSize(spec)
  const fieldsOf = (src: string) => sources.find((s) => s.source === src)?.fields ?? []
  const screenFields = catalog?.screen_fields ?? []
  return (
    <div className="text-sm">
      <div className="space-y-2 border-b border-border p-3">
        <Row label="Name">
          <Input aria-label="Strategy name" className="w-full" value={spec.name} onChange={(e) => patch((s) => ({ ...s, name: e.target.value }))} />
        </Row>
        <Row label="Mode" hint="Rank: quantile portfolios rebalanced on a calendar. Event: enter on an event, exit on time / stop / target.">
          <Segmented
            label="Backtest mode"
            value={spec.mode}
            onChange={(m) =>
              patch((s) => ({
                ...s,
                mode: m,
                event: m === 'event' ? (s.event ?? { trigger: assets ? 'cftc_extreme' : 'earnings_surprise', params: {}, hold_days: 20, max_positions: 20, allow_short: false, entry_lag_days: 1 }) : s.event,
              }))
            }
            options={[
              { value: 'rank', label: 'Rank / quantile' },
              { value: 'event', label: 'Event-driven' },
            ]}
          />
        </Row>
      </div>

      <Section title="1 · Universe" badge={<Badge tone="neutral">{assets ? 'assets' : (u.universe_id ?? 'custom')}</Badge>}>
        <Row label="Kind">
          <Segmented
            label="Universe kind"
            value={u.kind}
            onChange={(k) =>
              patch((s) => ({
                ...s,
                universe: { ...s.universe, kind: k, universe_id: k === 'assets' ? null : (s.universe.universe_id ?? 'mvp_us') },
                signal: { ...s.signal, variables: [signalVar(k === 'assets' ? 'cross_asset' : 'factor_scores', k === 'assets' ? 'trend_signal' : 'value')], combine: 'single' },
                event: s.event ? { ...s.event, trigger: k === 'assets' ? 'cftc_extreme' : 'earnings_surprise' } : s.event,
                period: { ...s.period, benchmark: k === 'assets' ? 'universe_ew' : null },
              }))
            }
            options={[
              { value: 'securities', label: 'Securities' },
              { value: 'assets', label: 'Cross-asset' },
            ]}
          />
        </Row>
        {!assets ? (
          <>
            <Row label="Universe" hint="Point-in-time membership (history starts 2026-10-10; earlier dates use the initial list)">
              <Select label="Universe" value={u.universe_id ?? ''} onChange={(v) => setU({ universe_id: v || null })} options={[{ value: '', label: 'none (filters only)' }, ...(catalog?.universes ?? ['mvp_us', 'mvp_global', 'full', 'eu_broad']).map((x) => ({ value: x, label: x }))]} />
            </Row>
            <Row label="Regions">
              <Input aria-label="Regions (comma separated)" className="w-full" placeholder="US, EU, JP …" value={listText(u.regions)} onChange={(e) => setU({ regions: textList(e.target.value) })} />
            </Row>
            <Row label="Sectors">
              <Input aria-label="Sector ids (comma separated)" className="w-full" placeholder="all" value={listText(u.sectors)} onChange={(e) => setU({ sectors: textList(e.target.value) })} />
            </Row>
            <Row label="Delisted names" hint="Inactive US names (Polygon, ~2 years) while they traded">
              <Select
                label="Include delisted"
                value={u.include_delisted === null || u.include_delisted === undefined ? 'auto' : u.include_delisted ? 'on' : 'off'}
                onChange={(v) => setU({ include_delisted: v === 'auto' ? null : v === 'on' })}
                options={[
                  { value: 'auto', label: 'auto' },
                  { value: 'on', label: 'include' },
                  { value: 'off', label: 'exclude' },
                ]}
              />
            </Row>
            <Row label="Liquidity" hint="Point-in-time minimum market cap / 20-day ADV (USD)">
              <Num label="Min market cap (USD bn)" value={u.min_market_cap_usd ? u.min_market_cap_usd / 1e9 : null} onChange={(v) => setU({ min_market_cap_usd: v ? v * 1e9 : null })} step={0.5} placeholder="cap bn" />
              <Num label="Min ADV (USD m)" value={u.min_adv_usd ? u.min_adv_usd / 1e6 : null} onChange={(v) => setU({ min_adv_usd: v ? v * 1e6 : null })} step={1} placeholder="ADV m" />
            </Row>
            <div className="space-y-1">
              <div className="flex items-center justify-between">
                <span className="text-xs text-muted">Screen rules (evaluated point-in-time each rebalance)</span>
                <Button size="xs" variant="ghost" onClick={() => setU({ screen: [...(u.screen ?? []), { field: 'quality', op: 'gte', value: 50 }] })}>
                  <Plus /> Rule
                </Button>
              </div>
              {(u.screen ?? []).map((r, i) => (
                <div key={i} className="flex items-center gap-1">
                  <Select label="Screen field" className="min-w-0 flex-1" value={r.field} onChange={(f) => setU({ screen: (u.screen ?? []).map((x, j) => (j === i ? { ...x, field: f } : x)) })} options={screenFields.map((f) => ({ value: f.field, label: f.label }))} />
                  <Select label="Operator" className="w-16" value={r.op} onChange={(op) => setU({ screen: (u.screen ?? []).map((x, j) => (j === i ? { ...x, op } : x)) })} options={OPS} />
                  {r.op !== 'not_null' && (
                    <Input
                      aria-label="Screen value"
                      className="w-20"
                      value={Array.isArray(r.value) ? (r.value as unknown[]).join(',') : String(r.value ?? '')}
                      onChange={(e) => {
                        const t = e.target.value
                        const n = Number(t)
                        const v = r.op === 'in' ? textList(t) : t !== '' && Number.isFinite(n) ? n : t
                        setU({ screen: (u.screen ?? []).map((x, j) => (j === i ? { ...x, value: v } : x)) })
                      }}
                    />
                  )}
                  <Button size="icon-sm" variant="ghost" aria-label="Remove rule" onClick={() => setU({ screen: (u.screen ?? []).filter((_, j) => j !== i) })}>
                    <Trash2 />
                  </Button>
                </div>
              ))}
            </div>
          </>
        ) : (
          <>
            <Row label="Asset classes">
              {(catalog?.asset_classes ?? ['commodity', 'fx', 'equity_index', 'rates', 'credit', 'crypto']).map((ac) => (
                <LabeledCheck key={ac} label={ac.replace('_', ' ')} checked={(u.asset_classes ?? []).includes(ac)} onCheckedChange={(on) => setU({ asset_classes: on ? [...(u.asset_classes ?? []), ac] : (u.asset_classes ?? []).filter((x) => x !== ac) })} />
              ))}
            </Row>
            <Row label="Assets">
              <Input aria-label="Asset ids (comma separated)" className="w-full" placeholder="all in the classes (CL, GC, SPX, EURUSD …)" value={listText(u.asset_ids)} onChange={(e) => setU({ asset_ids: textList(e.target.value) })} />
            </Row>
          </>
        )}
      </Section>

      <Section title={ev ? '2 · Signal & event' : '2 · Signal'} badge={<Badge tone="neutral">{spec.signal.variables.length} var</Badge>}>
        {spec.signal.variables.map((v, i) => {
          const fields = fieldsOf(v.source)
          return (
            <div key={i} className="space-y-1 rounded-md border border-border p-2">
              <div className="flex items-center gap-1">
                <Input aria-label="Variable name" className="w-24" value={v.name} onChange={(e) => setVar(i, { name: e.target.value.replace(/[^A-Za-z0-9_]/g, '') })} />
                <Select label="Signal source" className="min-w-0 flex-1" value={v.source} onChange={(src) => setVar(i, { source: src, field: fieldsOf(src)[0]?.field ?? '', higher_is_better: fieldsOf(src)[0]?.higher_is_better !== false })} options={sources.map((s) => ({ value: s.source, label: s.title }))} />
                {spec.signal.variables.length > 1 && (
                  <Button size="icon-sm" variant="ghost" aria-label="Remove variable" onClick={() => patch((s) => ({ ...s, signal: { ...s.signal, variables: s.signal.variables.filter((_, j) => j !== i) } }))}>
                    <Trash2 />
                  </Button>
                )}
              </div>
              <div className="flex items-center gap-1">
                {fields.length ? (
                  <Select label="Signal field" className="min-w-0 flex-1" value={v.field} onChange={(f) => setVar(i, { field: f, higher_is_better: fields.find((x) => x.field === f)?.higher_is_better !== false })} options={fields.map((f) => ({ value: f.field, label: `${f.label}${f.model_based ? ' (model)' : ''}` }))} />
                ) : (
                  <Input aria-label="Signal field" className="min-w-0 flex-1" placeholder={v.source === 'studio_run' ? 'Studio run id (SR…)' : v.source === 'bql' ? 'BQL expression, e.g. cs_zscore(chg_pct(px, 3M)) - cs_zscore(pe_ttm)' : 'field'} title={v.source === 'bql' ? 'Evaluated point-in-time at every rebalance date (BQL-lite; see the BQL console for fields and functions)' : undefined} value={v.field} onChange={(e) => setVar(i, { field: v.source === 'bql' ? e.target.value : e.target.value.trim() })} />
                )}
                <Segmented
                  label="Direction"
                  size="xs"
                  value={v.higher_is_better === false ? 'low' : 'high'}
                  onChange={(d) => setVar(i, { higher_is_better: d === 'high' })}
                  options={[
                    { value: 'high', label: 'high ↑', title: 'higher values are better' },
                    { value: 'low', label: 'low ↓', title: 'lower values are better' },
                  ]}
                />
                {spec.signal.variables.length > 1 && spec.signal.combine !== 'formula' && <Num label="Weight" className="w-14" value={v.weight ?? 1} step={0.25} onChange={(w) => setVar(i, { weight: w ?? 1 })} />}
              </div>
            </div>
          )
        })}
        <div className="flex flex-wrap items-center gap-1.5">
          <Button size="xs" variant="ghost" disabled={spec.signal.variables.length >= 12} onClick={() => patch((s) => ({ ...s, signal: { ...s.signal, variables: [...s.signal.variables, signalVar(sources[0]?.source ?? 'factor_scores', sources[0]?.fields[0]?.field ?? 'value', s.signal.variables.map((x) => x.name))], combine: s.signal.combine === 'single' ? 'zscore' : s.signal.combine } }))}>
            <Plus /> Variable
          </Button>
          {spec.signal.variables.length > 1 && (
            <Segmented
              label="Combine"
              size="xs"
              value={spec.signal.combine}
              onChange={(cb) => patch((s) => ({ ...s, signal: { ...s.signal, combine: cb, formula: cb === 'formula' ? (s.signal.formula ?? s.signal.variables.map((x) => x.name).join(' + ')) : s.signal.formula } }))}
              options={[
                { value: 'zscore', label: 'z blend' },
                { value: 'rank', label: 'rank blend' },
                { value: 'formula', label: 'formula' },
              ]}
            />
          )}
        </div>
        {spec.signal.combine === 'formula' && (
          <Row label="Formula" hint="Formula engine syntax over the variable names (e.g. 0.5*value + 0.5*mom - abs(vol)); no panel functions">
            <Input aria-label="Signal formula" className="w-full font-mono" value={spec.signal.formula ?? ''} onChange={(e) => patch((s) => ({ ...s, signal: { ...s.signal, formula: e.target.value } }))} />
          </Row>
        )}
        <Row label="Extra lag" hint="Lag the signal by n rebalance periods (on top of the t+1 execution)">
          <Num label="Signal lag periods" value={spec.signal.lag_periods} min={0} max={24} onChange={(v) => patch((s) => ({ ...s, signal: { ...s.signal, lag_periods: v ?? 0 } }))} />
        </Row>
        {ev && evb && (
          <div className="space-y-2 rounded-md border border-border bg-surface-2/40 p-2">
            <Row label="Trigger">
              <Select label="Event trigger" className="w-full" value={evb.trigger} onChange={(t) => setE({ trigger: t, params: {} })} options={triggers.map((t) => ({ value: t.trigger as BtEvent['trigger'], label: `${t.title}${t.model_based && !t.experimental ? ' (model)' : ''}` }))} />
            </Row>
            {triggers.find((t) => t.trigger === evb.trigger)?.experimental && (
              <ExperimentalNote>{triggers.find((t) => t.trigger === evb.trigger)?.validation ?? ''}</ExperimentalNote>
            )}
            <EventParams trigger={evb.trigger} params={(evb.params ?? {}) as Record<string, unknown>} onChange={(p) => setE({ params: p })} />
            <Row label="Exit" hint="Stop / target: fixed fractions, or the EXPERIMENTAL calibrated band (stop at the band edge emitted on the event date)">
              <Num label="Hold days" value={evb.hold_days} min={1} onChange={(v) => setE({ hold_days: v ?? 20 })} placeholder="days" />
              <Segmented
                label="Stop / target mode"
                size="xs"
                value={evb.stop_mode ?? 'fixed'}
                onChange={(m) => setE({ stop_mode: m })}
                options={[{ value: 'fixed', label: 'Fixed %' }, { value: 'calibrated_band', label: 'Calibrated band (α)' }]}
              />
              {(evb.stop_mode ?? 'fixed') === 'fixed' ? (
                <>
                  <Num label="Stop loss %" pct value={evb.stop_loss ?? null} onChange={(v) => setE({ stop_loss: v })} placeholder="stop %" />
                  <Num label="Take profit %" pct value={evb.take_profit ?? null} onChange={(v) => setE({ take_profit: v })} placeholder="target %" />
                </>
              ) : (
                <>
                  <Select label="Band level" value={String(evb.band_level ?? 90) as '80' | '90' | '95'} onChange={(v) => setE({ band_level: Number(v) as 80 | 90 | 95 })} options={[{ value: '80', label: '80% (α 0.20)' }, { value: '90', label: '90% (α 0.10)' }, { value: '95', label: '95% (α 0.05)' }]} />
                  <Select label="Band horizon" value={String(evb.band_horizon ?? 20) as '1' | '5' | '10' | '20'} onChange={(v) => setE({ band_horizon: Number(v) as 1 | 5 | 10 | 20 })} options={[{ value: '1', label: '1 session' }, { value: '5', label: '5 sessions' }, { value: '10', label: '10 sessions' }, { value: '20', label: '20 sessions' }]} />
                  <Switch label="Also take profit at the opposite band edge" checked={!!evb.band_target} onCheckedChange={(on) => setE({ band_target: on })} />
                </>
              )}
            </Row>
            {evb.stop_mode === 'calibrated_band' && (
              <ExperimentalNote>{catalog?.stop_modes?.find((m) => m.mode === 'calibrated_band')?.validation ?? "Band stops beat ATR stops, but no stop beat 'no stop' in validation."}</ExperimentalNote>
            )}
            <Row label="Positions">
              <Num label="Max positions" value={evb.max_positions} min={1} onChange={(v) => setE({ max_positions: v ?? 20 })} />
              <Switch label="Allow shorts" checked={!!evb.allow_short} onCheckedChange={(on) => setE({ allow_short: on })} />
            </Row>
          </div>
        )}
      </Section>

      {!ev && c && (
        <Section title="3 · Construction" badge={<Badge tone="neutral">{c.side === 'long_short' ? 'L/S' : 'long'} · {c.rebalance}</Badge>}>
          <Row label="Rebalance">
            <Segmented label="Rebalance frequency" size="xs" value={c.rebalance} onChange={(r) => setC({ rebalance: r })} options={[{ value: 'D', label: 'Daily' }, { value: 'W', label: 'Weekly' }, { value: 'M', label: 'Monthly' }]} />
          </Row>
          <Row label="Side">
            <Segmented label="Side" size="xs" value={c.side} onChange={(v) => setC({ side: v })} options={[{ value: 'long_only', label: 'Long only' }, { value: 'long_short', label: 'Long-short' }]} />
          </Row>
          <Row label="Selection">
            <Select label="Selection" value={c.selection} onChange={(v) => setC({ selection: v })} options={[{ value: 'quantile', label: 'Quantiles' }, { value: 'top_n', label: 'Top N' }, { value: 'all', label: 'All (signal-weighted)' }]} />
            {c.selection === 'quantile' && <Num label="Quantiles" value={c.quantiles} min={2} max={20} onChange={(v) => setC({ quantiles: v ?? 5 })} />}
            {c.selection === 'top_n' && <Num label="Top N" value={c.top_n ?? 20} min={1} onChange={(v) => setC({ top_n: v })} />}
          </Row>
          <Row label="Weighting">
            <Select label="Weighting" value={c.weighting} onChange={(v) => setC({ weighting: v })} options={[{ value: 'equal', label: 'Equal' }, { value: 'cap', label: 'Market cap' }, { value: 'signal', label: 'Signal' }, { value: 'inverse_vol', label: 'Inverse volatility' }, { value: 'risk_parity', label: 'Risk parity (ERC)' }]} />
          </Row>
          <Row label="Neutralize">
            <Select label="Neutralize" value={c.neutralize} onChange={(v) => setC({ neutralize: v })} options={[{ value: 'none', label: 'None' }, { value: 'sector', label: assets ? 'Asset class' : 'Sector' }, { value: 'country', label: 'Country' }]} />
          </Row>
          <Row label="Limits" hint="Max weight per name and max one-way turnover per rebalance (%)">
            <Num label="Max weight %" pct value={c.max_weight ?? null} onChange={(v) => setC({ max_weight: v })} placeholder="cap %" />
            <Num label="Max turnover %" pct value={c.max_turnover ?? null} onChange={(v) => setC({ max_turnover: v })} placeholder="turn %" />
          </Row>
          <Row label="Holding" hint="Overlapping tranches: each rebalance re-forms 1/K of the book (Jegadeesh-Titman)">
            <Num label="Holding periods" value={c.holding_periods} min={1} max={24} onChange={(v) => setC({ holding_periods: v ?? 1 })} />
            <span className="text-xs text-muted">exec. lag</span>
            <Num label="Execution lag days" value={c.execution_lag_days} min={0} max={10} onChange={(v) => setC({ execution_lag_days: v ?? 1 })} className="w-14" />
          </Row>
          <Row label="Regime filter" hint="Go to cash (or half) when the macro regime known at t is not in the selected states (model-based states labelled)">
            <Select
              label="Regime filter"
              value={c.regime_filter ? `${c.regime_filter.regime_type}:${c.regime_filter.scope}` : ''}
              onChange={(v) => {
                if (!v) return setC({ regime_filter: null })
                const [rt, sc] = v.split(':')
                const r = regimes.find((x) => x.regime_type === rt && x.scope === sc)
                setC({ regime_filter: { regime_type: rt as 'cycle_quadrant', scope: sc, states: r?.states.slice(0, 2) ?? [], action: 'cash' } })
              }}
              options={[{ value: '', label: 'none' }, ...regimes.filter((r) => ['US', 'GLOBAL', 'fed', 'ecb'].includes(r.scope)).map((r) => ({ value: `${r.regime_type}:${r.scope}`, label: `${r.regime_type.replace('_', ' ')} · ${r.scope}${r.model_based ? ' (model)' : ''}` }))]}
            />
          </Row>
          {c.regime_filter && (
            <Row label="Invest in">
              {(regimes.find((r) => r.regime_type === c.regime_filter?.regime_type && r.scope === c.regime_filter?.scope)?.states ?? []).map((st) => (
                <LabeledCheck key={st} label={st.replace(/_/g, ' ')} checked={c.regime_filter?.states.includes(st) ?? false} onCheckedChange={(on) => setC({ regime_filter: { ...(c.regime_filter as NonNullable<typeof c.regime_filter>), states: on ? [...(c.regime_filter?.states ?? []), st] : (c.regime_filter?.states ?? []).filter((x) => x !== st) } })} />
              ))}
            </Row>
          )}
        </Section>
      )}

      {cost && (
        <Section title={ev ? '3 · Costs' : '4 · Costs'} defaultOpen={false} badge={<Badge tone={cost.enabled ? 'neutral' : 'warn'}>{cost.enabled ? 'on' : 'off'}</Badge>}>
          <Row label="Costs">
            <Switch label="Apply trading costs" checked={cost.enabled} onCheckedChange={(on) => setCost({ enabled: on })} />
          </Row>
          <Row label="Commission bp">
            <Num label="Commission bps" value={cost.commission_bps} step={0.5} onChange={(v) => setCost({ commission_bps: v ?? 0 })} />
          </Row>
          <Row label="Spread" hint="Half-spread by 20-day ADV bucket (≥1bn 1bp · ≥100m 3bp · ≥20m 8bp · ≥5m 20bp · else 50bp) or fixed">
            <Select label="Spread model" value={cost.spread_model} onChange={(v) => setCost({ spread_model: v })} options={[{ value: 'adv_buckets', label: 'ADV buckets' }, { value: 'fixed', label: 'Fixed' }]} />
            {cost.spread_model === 'fixed' && <Num label="Fixed half-spread bps" value={cost.fixed_spread_bps} onChange={(v) => setCost({ fixed_spread_bps: v ?? 0 })} />}
          </Row>
          <Row label="Impact" hint="Square-root impact: coef × daily vol × √(trade / ADV), with the capital below">
            <Num label="Impact coefficient" value={cost.impact_coef} step={0.05} onChange={(v) => setCost({ impact_coef: v ?? 0 })} />
            <Num label="Capital (USD m)" value={cost.capital_usd / 1e6} step={5} onChange={(v) => setCost({ capital_usd: (v ?? 10) * 1e6 })} />
          </Row>
          <Row label="Borrow bp/yr" hint="General collateral / hard to borrow (ADV < 5m)">
            <Num label="Borrow bps" value={cost.borrow_bps} onChange={(v) => setCost({ borrow_bps: v ?? 0 })} />
            <Num label="Hard-to-borrow bps" value={cost.htb_borrow_bps} onChange={(v) => setCost({ htb_borrow_bps: v ?? 0 })} />
          </Row>
          <Row label="FX" hint="Returns converted with official fx_rates (adj_close × usd_per_unit); FX conversion cost on non-USD trades">
            <Select label="Base currency" value={cost.currency} onChange={(v) => setCost({ currency: v })} options={[{ value: 'USD', label: 'USD (official FX)' }, { value: 'local', label: 'Local currency' }]} />
            <Num label="FX bps" value={cost.fx_bps} step={0.5} onChange={(v) => setCost({ fx_bps: v ?? 0 })} />
          </Row>
        </Section>
      )}

      {per && (
        <Section title={ev ? '4 · Period & holdout' : '5 · Period & holdout'} badge={<Badge tone="accent">OOS {per.oos_start ?? `last ${Math.round((per.oos_fraction ?? 0.3) * 100)}%`}</Badge>}>
          <Row label="Period">
            <Input type="date" aria-label="Start date" className="w-36" value={per.start ?? ''} onChange={(e) => setP({ start: e.target.value || null })} />
            <Input type="date" aria-label="End date" className="w-36" value={per.end ?? ''} onChange={(e) => setP({ end: e.target.value || null })} />
          </Row>
          <Row label="Holdout start" hint="Mandatory out-of-sample holdout: never used for parameter selection">
            <Input type="date" aria-label="Out-of-sample start" className="w-36" value={per.oos_start ?? ''} onChange={(e) => setP({ oos_start: e.target.value || null })} />
            {!per.oos_start && <Num label="Holdout fraction %" pct value={per.oos_fraction} onChange={(v) => setP({ oos_fraction: v ?? 0.3 })} />}
          </Row>
          <Row label="Benchmark">
            <Select label="Benchmark" value={per.benchmark ?? ''} onChange={(v) => setP({ benchmark: v || null })} options={[{ value: '', label: 'auto (SPY / ACWI / assets EW)' }, ...(catalog?.benchmarks ?? []).map((b) => ({ value: b.id, label: b.label }))]} />
          </Row>
          <Row label="Risk-free">
            <Segmented label="Risk-free rate" size="xs" value={per.rf} onChange={(v) => setP({ rf: v })} options={[{ value: 'cash', label: '3m T-bill' }, { value: 'zero', label: 'zero' }]} />
          </Row>
        </Section>
      )}

      {rb && (
        <Section title={ev ? '5 · Robustness' : '6 · Robustness'} defaultOpen={false} badge={nSweep ? <Badge tone="warn">{nSweep} configs</Badge> : undefined}>
          <Row label="Permutations" hint="Label permutation test: the signal shuffled across names within each date (should give ~0 Sharpe). Opt-in heavy extra: each permutation costs about one full simulation (Validate shows the cost); 20 is typical">
            <Num label="Permutations" value={rb.permutations} min={0} max={200} onChange={(v) => setR({ permutations: v ?? 0 })} />
            <Switch label="Stale-signal check" checked={rb.stale_check} onCheckedChange={(on) => setR({ stale_check: on })} />
          </Row>
          <Row label="Attribution">
            <Switch label="Factor attribution" checked={rb.attribution} onCheckedChange={(on) => setR({ attribution: on })} />
          </Row>
          <SweepEditor sweep={(rb.sweep ?? {}) as Record<string, unknown[]>} onChange={(sw) => setR({ sweep: sw })} />
          {nSweep > 0 && (
            <Row label="WF / CSCV" hint="Walk-forward folds on the in-sample window; CSCV blocks for the probability of backtest overfitting">
              <Num label="Walk-forward folds" value={rb.wf_folds} min={2} max={12} onChange={(v) => setR({ wf_folds: v ?? 4 })} />
              <Num label="CSCV blocks" value={rb.cscv_blocks} min={4} max={16} step={2} onChange={(v) => setR({ cscv_blocks: v ?? 10 })} />
            </Row>
          )}
        </Section>
      )}

      <div className="sticky bottom-0 flex flex-wrap items-center gap-1.5 border-t border-border bg-surface p-3">
        <Button variant="primary" disabled={running} onClick={() => onRun('auto')}>
          {running ? 'Running…' : 'Run backtest'}
        </Button>
        <Button variant="outline" disabled={running} onClick={() => onRun('async')} title="Queue on the worker (long sweeps / big universes)">
          Run on worker
        </Button>
        <Button variant="ghost" disabled={validating} onClick={onValidate} title="Check the spec and estimate the run time">
          Check
        </Button>
        <Button variant="ghost" onClick={onSave} title="Save as a strategy (multiple-testing counter, paper schedule)">
          Save
        </Button>
      </div>
    </div>
  )
}

function EventParams({ trigger, params, onChange }: { trigger: string; params: Record<string, unknown>; onChange: (p: Record<string, unknown>) => void }) {
  const num = (k: string, label: string, dflt: number, step = 0.1) => <Num key={k} label={label} value={(params[k] as number | undefined) ?? dflt} step={step} onChange={(v) => onChange({ ...params, [k]: v ?? dflt })} />
  if (trigger === 'earnings_surprise')
    return (
      <Row label="Surprise" hint="|field| ≥ threshold vs MODEL-BASED expectations (not consensus)">
        <Select label="Surprise field" value={(params.field as string) ?? 'sue'} onChange={(f) => onChange({ ...params, field: f })} options={[{ value: 'sue', label: 'SUE (EPS)' }, { value: 'pead_score', label: 'PEAD score' }, { value: 'rev_surprise_z', label: 'Revenue z' }, { value: 'ear_z', label: 'Announcement return z' }]} />
        {num('threshold', 'Threshold', 2)}
      </Row>
    )
  if (trigger === 'cftc_extreme')
    return (
      <Row label="COT index" hint="Crossing above high / below low of the 3-year COT index">
        {num('high', 'High threshold', 0.9, 0.05)}
        {num('low', 'Low threshold', 0.1, 0.05)}
        <Select label="Mode" value={(params.mode as string) ?? 'contrarian'} onChange={(m) => onChange({ ...params, mode: m })} options={[{ value: 'contrarian', label: 'contrarian' }, { value: 'trend', label: 'with the crowd' }]} />
      </Row>
    )
  if (trigger === 'macro_surprise')
    return (
      <>
        <Row label="Surprise" hint="|signed z| ≥ threshold vs model-based expectations">
          {num('threshold', 'Threshold z', 1.5)}
          <Input aria-label="Country" className="w-14" placeholder="US" value={(params.country as string) ?? ''} onChange={(e) => onChange({ ...params, country: e.target.value.toUpperCase() || undefined })} />
          <Select label="Theme" value={(params.theme as string) ?? ''} onChange={(t) => onChange({ ...params, theme: t || undefined })} options={[{ value: '', label: 'any theme' }, { value: 'growth', label: 'growth' }, { value: 'inflation', label: 'inflation' }, { value: 'labor', label: 'labor' }]} />
        </Row>
        <Row label="Trade">
          <Input aria-label="Target instruments" className="w-full" placeholder="ETF:SPY (comma separated, in the universe)" value={((params.targets as string[]) ?? []).join(', ')} onChange={(e) => onChange({ ...params, targets: textList(e.target.value) })} />
        </Row>
      </>
    )
  if (trigger === 'confluence_touch') return <Row label="Distance">{num('max_distance_atr', 'Max distance (ATR)', 0.25, 0.05)}</Row>
  if (trigger === 'signal_threshold') return <Row label="Threshold">{num('threshold', 'Signal threshold', 0)}</Row>
  if (trigger === 'calibrated_band_breakout')
    return (
      <Row label="Band" hint="The h-session move leaves the marginal conformal band emitted h sessions earlier (walk-forward on the panel prices)">
        <Select label="Breakout horizon" value={String(params.horizon ?? 5) as '1' | '5' | '10' | '20'} onChange={(v) => onChange({ ...params, horizon: Number(v) })} options={[{ value: '1', label: '1 session' }, { value: '5', label: '5 sessions' }, { value: '10', label: '10 sessions' }, { value: '20', label: '20 sessions' }]} />
        <Select label="Breakout band level" value={String(params.level ?? 90) as '80' | '90' | '95'} onChange={(v) => onChange({ ...params, level: Number(v) })} options={[{ value: '80', label: '80%' }, { value: '90', label: '90%' }, { value: '95', label: '95%' }]} />
        <Select label="Breakout direction" value={(params.direction as 'up' | 'both') ?? 'up'} onChange={(v) => onChange({ ...params, direction: v })} options={[{ value: 'up', label: 'upside (long)' }, { value: 'both', label: 'both (needs shorts)' }]} />
      </Row>
    )
  if (trigger === 'peer_breakaway')
    return (
      <Row label="Break-away" hint="Fade the member with |z | group| ≥ z on days the group's joint score exceeded its 90% radius (needs shorts for down moves)">
        {num('z', '|z| threshold', 2.5, 0.25)}
        <Select label="Signal source" value={(params.source as 'auto' | 'walk_forward' | 'live') ?? 'auto'} onChange={(v) => onChange({ ...params, source: v })} options={[{ value: 'auto', label: 'auto' }, { value: 'walk_forward', label: 'walk-forward 2010–2026' }, { value: 'live', label: 'live table (~1 year)' }]} />
      </Row>
    )
  if (trigger === 'meta_setups')
    return (
      <>
        <Row label="Meta setups" hint="v2 weekly setups (stop at the invalidation, target T1 per trade): all of them, only those the meta-labelling model takes, or taken and sized by its quarter-Kelly fraction. The run notes carry the gate verdict.">
          <Select label="Meta horizon" value={String(params.horizon ?? 20) as '10' | '20' | '40'} onChange={(v) => onChange({ ...params, horizon: Number(v) })} options={[{ value: '10', label: '10 sessions' }, { value: '20', label: '20 sessions' }, { value: '40', label: '40 sessions' }]} />
          <Select label="Meta mode" value={(params.mode as 'all' | 'take' | 'sized') ?? 'take'} onChange={(v) => onChange({ ...params, mode: v })} options={[{ value: 'all', label: 'all setups' }, { value: 'take', label: 'meta: take only' }, { value: 'sized', label: 'meta: take + Kelly size' }]} />
        </Row>
        <Row label="Source">
          <Select label="Meta source" value={(params.source as 'auto' | 'walk_forward' | 'live') ?? 'auto'} onChange={(v) => onChange({ ...params, source: v })} options={[{ value: 'auto', label: 'auto' }, { value: 'walk_forward', label: 'walk-forward 2013–2026' }, { value: 'live', label: 'live table' }]} />
          <Select label="Meta direction" value={(params.direction as 'long' | 'both') ?? 'long'} onChange={(v) => onChange({ ...params, direction: v })} options={[{ value: 'long', label: 'long setups' }, { value: 'both', label: 'long + short (needs shorts)' }]} />
        </Row>
      </>
    )
  return null
}

function SweepEditor({ sweep, onChange }: { sweep: Record<string, unknown[]>; onChange: (s: Record<string, unknown[]>) => void }) {
  const keys = Object.keys(sweep)
  const [text, setText] = useState<Record<string, string>>(() => Object.fromEntries(keys.map((k) => [k, sweep[k].join(', ')])))
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted" title="Every combination is simulated; selection uses the in-sample window only (walk-forward), PBO via CSCV, all configurations count as trials for the deflated Sharpe">
          Parameter sweep (walk-forward)
        </span>
        <Button
          size="xs"
          variant="ghost"
          disabled={keys.length >= 4}
          onClick={() => {
            const k = SWEEP_PATHS.find((p) => !keys.includes(p)) ?? 'construction.quantiles'
            const v = k === 'construction.quantiles' ? [3, 5, 10] : k === 'construction.weighting' ? ['equal', 'inverse_vol'] : [1, 3]
            setText((t) => ({ ...t, [k]: v.join(', ') }))
            onChange({ ...sweep, [k]: v })
          }}
        >
          <Plus /> Parameter
        </Button>
      </div>
      {keys.map((k) => (
        <div key={k} className="flex items-center gap-1">
          <Select
            label="Sweep parameter"
            className="min-w-0 flex-1"
            value={k}
            onChange={(nk) => {
              if (nk === k || keys.includes(nk)) return
              const { [k]: vals, ...rest } = sweep
              onChange({ ...rest, [nk]: vals })
              setText((t) => ({ ...t, [nk]: t[k] ?? '' }))
            }}
            options={SWEEP_PATHS.map((p) => ({ value: p, label: p }))}
          />
          <Input
            aria-label={`Values for ${k}`}
            className="w-32 font-mono"
            value={text[k] ?? sweep[k].join(', ')}
            onChange={(e) => {
              setText((t) => ({ ...t, [k]: e.target.value }))
              onChange({ ...sweep, [k]: parseSweepValues(e.target.value) })
            }}
          />
          <Button
            size="icon-sm"
            variant="ghost"
            aria-label="Remove sweep parameter"
            onClick={() => {
              const { [k]: _drop, ...rest } = sweep
              onChange(rest)
            }}
          >
            <Trash2 />
          </Button>
        </div>
      ))}
    </div>
  )
}
