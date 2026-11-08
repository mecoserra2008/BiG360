import { useQuery } from '@tanstack/react-query'
import { BellPlus, Globe2, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router'
import { useMacroCountries } from '@/api/macro/hooks'
import { loadAlertEvents } from '@/api/remote'
import type { AlertRule, MacroAlertMetric } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/feedback'
import { Input, Select, Switch } from '@/components/ui/inputs'
import { Panel } from '@/components/ui/panel'
import { DASH, fmtDateTime } from '@/lib/format'
import { useWatchlist } from '@/store'

const TYPES: { value: MacroAlertMetric; label: string; hint: string }[] = [
  { value: 'macroRelease', label: 'Macro release', hint: 'a linked official series is published' },
  { value: 'macroSurprise', label: 'Macro surprise', hint: '|surprise z| vs our model-based expectation above a threshold' },
  { value: 'regimeChange', label: 'Regime change', hint: 'cycle quadrant changes or a recession probability crosses a level' },
]

const isMacro = (r: AlertRule) => r.metric === 'macroRelease' || r.metric === 'macroSurprise' || r.metric === 'regimeChange'

function describe(r: AlertRule): string {
  const p = r.macroParams ?? {}
  if (r.metric === 'macroRelease') return `Release of a tier ≤ ${p.max_importance ?? 1} series`
  if (r.metric === 'macroSurprise')
    return `|surprise z| ≥ ${p.threshold_z ?? 2} vs model-based expectation (not consensus)${Array.isArray(p.themes) && p.themes.length ? ` · ${(p.themes as string[]).join(', ')}` : ''} · tier ≤ ${p.max_importance ?? 3}`
  const kind = String(p.kind ?? 'any')
  return kind === 'cycle_quadrant' ? 'Cycle quadrant changes' : kind === 'recession_probability' ? `Recession probability crosses ${Math.round(Number(p.level ?? 0.5) * 100)}%` : `Quadrant change or recession probability crossing ${Math.round(Number(p.level ?? 0.5) * 100)}%`
}

/** Country-level macro alert rules (macro_release / macro_surprise / regime_change) and their fired events. */
export function MacroAlerts() {
  const rules = useWatchlist((s) => s.rules).filter(isMacro)
  const addRule = useWatchlist((s) => s.addRule)
  const updateRule = useWatchlist((s) => s.updateRule)
  const removeRule = useWatchlist((s) => s.removeRule)
  const countries = useMacroCountries()
  const events = useQuery({ queryKey: ['alerts', 'events'], queryFn: () => loadAlertEvents(200), staleTime: 60_000 })
  const [type, setType] = useState<MacroAlertMetric>('macroSurprise')
  const [country, setCountry] = useState('US')
  const [tier, setTier] = useState('2')
  const [z, setZ] = useState('2')
  const [theme, setTheme] = useState('')
  const [kind, setKind] = useState('any')
  const [level, setLevel] = useState('50')

  const submit = () => {
    const countriesP = country ? { countries: [country] } : {}
    const params: Record<string, unknown> =
      type === 'macroRelease'
        ? { ...countriesP, max_importance: Number(tier) }
        : type === 'macroSurprise'
          ? { ...countriesP, max_importance: Number(tier), threshold_z: Math.abs(Number(z) || 2), ...(theme ? { themes: [theme] } : {}) }
          : { ...countriesP, kind, level: Math.min(0.99, Math.max(0.01, (Number(level) || 50) / 100)) }
    addRule({ companyId: '', ticker: country || 'All countries', metric: type, operator: '>', threshold: type === 'macroSurprise' ? Number(z) : type === 'regimeChange' ? Number(level) / 100 : Number(tier), enabled: true, macroParams: params })
  }

  const macroEvents = (events.data ?? []).filter((e) => e.target.startsWith('macro:')) // v1.11 AlertEvent.target
  return (
    <Panel title="Macro alerts" subtitle="Country-level rules evaluated nightly on official releases · surprises are measured against our model-based expectations (not consensus)" className="col-span-12" flush>
      <form
        className="flex flex-wrap items-center gap-2 border-b border-border p-2"
        onSubmit={(e) => {
          e.preventDefault()
          submit()
        }}
      >
        <Select label="Macro rule type" value={type} onChange={setType} options={TYPES.map((t) => ({ value: t.value, label: t.label }))} className="w-44" />
        <Select label="Country" value={country} onChange={setCountry} options={[{ value: '', label: 'All countries' }, ...(countries.data?.countries ?? []).map((c) => ({ value: c.country, label: `${c.country} · ${c.name}` }))]} className="w-48" />
        {type !== 'regimeChange' && <Select label="Maximum tier" value={tier} onChange={setTier} options={[{ value: '1', label: 'Tier 1' }, { value: '2', label: 'Tier ≤ 2' }, { value: '3', label: 'Any tier' }]} className="w-28" />}
        {type === 'macroSurprise' && (
          <>
            <Input aria-label="Surprise z threshold" inputMode="decimal" value={z} onChange={(e) => setZ(e.target.value)} className="w-20" placeholder="|z|" />
            <Select label="Theme" value={theme} onChange={setTheme} options={[{ value: '', label: 'All themes' }, { value: 'growth', label: 'Growth' }, { value: 'inflation', label: 'Inflation' }, { value: 'labor', label: 'Labor' }]} className="w-32" />
          </>
        )}
        {type === 'regimeChange' && (
          <>
            <Select label="Regime trigger" value={kind} onChange={setKind} options={[{ value: 'any', label: 'Quadrant or recession prob.' }, { value: 'cycle_quadrant', label: 'Cycle quadrant change' }, { value: 'recession_probability', label: 'Recession probability' }]} className="w-56" />
            {kind !== 'cycle_quadrant' && (
              <label className="flex items-center gap-1 text-xs text-muted">
                level
                <Input aria-label="Recession probability level (%)" inputMode="decimal" value={level} onChange={(e) => setLevel(e.target.value)} className="w-16" />%
              </label>
            )}
          </>
        )}
        <span className="text-2xs text-muted">{TYPES.find((t) => t.value === type)?.hint}</span>
        <Button type="submit" variant="primary" size="sm" className="ml-auto">
          <BellPlus /> Add macro rule
        </Button>
      </form>
      {rules.length === 0 ? (
        <EmptyState icon={<Globe2 />} title="No macro rules" message="Add a rule above: releases, surprises vs our model, or regime changes for a country." />
      ) : (
        <table className="w-full text-sm" aria-label="Macro alert rules">
          <thead>
            <tr className="border-b border-border text-left text-2xs font-semibold tracking-wide text-muted uppercase">
              <th className="px-3 py-1.5">On</th>
              <th className="px-3 py-1.5">Scope</th>
              <th className="px-3 py-1.5">Rule</th>
              <th className="px-3 py-1.5">Created</th>
              <th className="px-3 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {rules.map((r) => (
              <tr key={r.id} className="border-b border-border/60">
                <td className="px-3 py-1">
                  <Switch label={`Enable macro rule ${r.ticker}`} checked={r.enabled} onCheckedChange={(v) => updateRule(r.id, { enabled: v })} />
                </td>
                <td className="px-3 py-1">
                  <Badge tone="info">macro</Badge> <span className="num font-medium text-fg">{r.ticker}</span>
                </td>
                <td className="px-3 py-1 text-fg">
                  {TYPES.find((t) => t.value === r.metric)?.label}: {describe(r)}
                </td>
                <td className="num px-3 py-1 text-muted">{fmtDateTime(r.createdAt)}</td>
                <td className="px-3 py-1 text-right">
                  <Button size="icon-sm" variant="ghost" aria-label={`Delete macro rule ${r.ticker}`} onClick={() => removeRule(r.id)}>
                    <Trash2 />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="border-t border-border px-3 py-2">
        <h3 className="mb-1 text-xs font-semibold text-fg">Recent macro events</h3>
        {macroEvents.length === 0 ? (
          <p className="text-xs text-muted">{events.isPending ? 'Loading…' : 'No macro events fired yet (the scheduler evaluates rules nightly).'}</p>
        ) : (
          <ul className="max-h-60 space-y-0.5 overflow-y-auto text-xs">
            {macroEvents.slice(0, 50).map((e) => {
              const cc = e.target.replace('macro:', '')
              return (
                <li key={e.id} className="flex items-center gap-2">
                  <span className="num w-32 shrink-0 text-muted">{fmtDateTime(e.firedAt)}</span>
                  <Link to={`/macro/country/${cc}`} className="num w-10 shrink-0 font-medium text-accent-text hover:underline">
                    {cc || DASH}
                  </Link>
                  <span className={e.read ? 'text-muted' : 'text-fg'}>{e.message}</span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </Panel>
  )
}
