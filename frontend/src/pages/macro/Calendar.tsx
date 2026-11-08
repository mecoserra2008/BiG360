import { useMemo, useState } from 'react'
import { useSearchParams } from 'react-router'
import { groupCalendar } from '@/api/macro/adapters'
import { useMacroCalendar, useMacroCountries } from '@/api/macro/hooks'
import { Badge } from '@/components/ui/badge'
import { QueryView, Skeleton } from '@/components/ui/feedback'
import { Segmented, Select, Switch } from '@/components/ui/inputs'
import { PageHeader, Panel } from '@/components/ui/panel'
import { Tip } from '@/components/ui/tooltip'
import { calendarCountryParam } from './calendarParams'
import { isoDaysFromNow } from './fmt'
import { CountryReleaseHint, ReleaseList } from './releases'
import { MacroNav, ModelBasedTag } from './shared'


export default function Calendar() {
  const today = isoDaysFromNow(0)
  const [when, setWhen] = useState<'upcoming' | 'past' | 'all'>('upcoming')
  const [tier, setTier] = useState('2')
  // the country filter lives in the URL so that `ECO <cc>` (and shared links) open the calendar filtered
  const [params, setParams] = useSearchParams()
  const country = calendarCountryParam(params.get('country'))
  const setCountry = (v: string) =>
    setParams(
      (p) => {
        const n = new URLSearchParams(p)
        if (v) n.set('country', v)
        else n.delete('country')
        return n
      },
      { replace: true },
    )
  const [meetings, setMeetings] = useState(true)
  const from = when === 'upcoming' ? today : isoDaysFromNow(-30)
  const to = when === 'past' ? today : isoDaysFromNow(45)
  const q = useMacroCalendar(from, to)
  const countries = useMacroCountries()
  const days = useMemo(
    () =>
      q.data
        ? groupCalendar(q.data.items, {
            countries: country ? [country] : undefined,
            maxTier: Number(tier),
            when,
            today,
            kinds: meetings ? ['data', 'policy_meeting'] : ['data'],
          })
        : [],
    [q.data, country, tier, when, today, meetings],
  )
  return (
    <>
      <PageHeader title="Release calendar" subtitle="Official statistical-office and central-bank calendars · actual vs our model-based expectation · surprise z" />
      <MacroNav />
      <Panel
        title="Releases"
        subtitle={
          <span className="inline-flex flex-wrap items-center gap-2">
            <ModelBasedTag /> <CountryReleaseHint />
          </span>
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <Segmented
              label="Period"
              size="xs"
              value={when}
              onChange={setWhen}
              options={[
                { value: 'upcoming', label: 'Upcoming' },
                { value: 'past', label: 'Past 30 days' },
                { value: 'all', label: 'All' },
              ]}
            />
            <Select
              label="Country"
              value={country}
              onChange={setCountry}
              options={[{ value: '', label: 'All countries' }, ...(countries.data?.countries ?? []).map((c) => ({ value: c.country, label: `${c.country} · ${c.name}` }))]}
            />
            <Select
              label="Maximum tier"
              value={tier}
              onChange={setTier}
              options={(q.data?.importance_tiers ?? [{ tier: 1, label: 'high' }, { tier: 2, label: 'medium' }, { tier: 3, label: 'low' }]).map((t) => ({ value: String(t.tier), label: t.tier === 1 ? `Tier 1 (${t.label})` : `Tier ≤ ${t.tier} (+ ${t.label})` }))}
            />
            <label className="flex items-center gap-1.5 text-xs text-muted">
              <Switch label="Include policy meetings" checked={meetings} onCheckedChange={setMeetings} /> Meetings
            </label>
          </div>
        }
        flush
      >
        <QueryView query={q} skeleton={<Skeleton className="m-3 h-96" />}>
          {(cal) => (
            <>
              <div className="space-y-1 border-b border-border px-3 py-1.5 text-2xs text-muted">
                <ul className="flex flex-wrap gap-x-4 gap-y-0.5" aria-label="Importance tiers">
                  {cal.importance_tiers.map((t) => (
                    <li key={t.tier} className="inline-flex flex-wrap items-center gap-1">
                      <Badge tone={t.tier === 1 ? 'accent' : 'neutral'}>T{t.tier}</Badge>
                      <span className="font-medium text-fg">{t.label}</span>
                      <span>{t.includes.map((x) => x.replace(/_/g, ' ')).join(', ')}</span>
                    </li>
                  ))}
                </ul>
                <div className="flex flex-wrap items-center gap-2">
                  <Tip content={<div className="max-w-sm">{cal.sources.join(' · ')}</div>}>
                    <span tabIndex={0} className="cursor-help underline decoration-dotted">
                      {cal.sources.length} official calendars
                    </span>
                  </Tip>
                  · <Badge tone="up">official</Badge> = date from the publisher&apos;s calendar · <Badge tone="warn">inferred</Badge> = from the usual release lag
                </div>
              </div>
              <ReleaseList days={days} />
            </>
          )}
        </QueryView>
      </Panel>
    </>
  )
}
