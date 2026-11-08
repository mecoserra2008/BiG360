import { useNavigate } from 'react-router'
import type { EvEventDates } from '@/api/events/types'
import { newUid } from '@/api/studio/adapters'
import type { StudyDraft } from '@/api/studio/types'
import { EMPTY_DRAFT, useStudio } from '@/pages/studio/store'

/**
 * Build an event_car study draft from labelled event dates (GET /events/v2/event-dates).
 * country → country-level daily returns (entity = ISO2), sector → sector level, issuer → security with its
 * sector peers as the equal-weight market.
 */
export function eventStudyDraft(d: EvEventDates, opts: { sectorId?: string | null } = {}): StudyDraft {
  const lt = d.label.type
  const entity = lt === 'issuer' ? (d.label.security?.ticker ?? d.label.id) : d.label.id
  const dates = d.dates.map((x) => x.date).sort()
  const first = dates[0] ?? d.start
  const start = `${Number(first.slice(0, 4)) - 2}-01-01`
  return {
    ...EMPTY_DRAFT,
    name: `Event study · ${d.label.name} · ${d.theme === 'all' ? 'all themes' : d.theme} (${d.kind})`.slice(0, 80),
    kind: 'panel',
    frequency: 'D',
    start,
    end: null,
    entityLevel: lt === 'country' ? 'country' : lt === 'sector' ? 'sector' : 'security',
    entities:
      lt === 'issuer'
        ? {
            universe: 'mvp_global',
            sectors: opts.sectorId ? [opts.sectorId] : null,
            primary_only: true,
          }
        : { universe: 'mvp_global', primary_only: true },
    method: 'event_car',
    params: {
      model: 'market',
      est_window: [-250, -30],
      event_window: [-5, 10],
      events: dates.map((date) => ({ entity, date })),
    },
    variables: [
      {
        uid: newUid(),
        name: 'ret',
        source: 'prices',
        field: 'ret',
        title: 'Daily total return',
        role: 'y',
        agg: null,
        entityAgg: null,
        entities: null,
        preTransforms: [],
        transforms: [],
        extra: {},
      },
    ],
  }
}

/** "Send to Model Studio": open the builder prefilled with an event_car study over these event dates. */
export function useSendEventStudy(): (d: EvEventDates, opts?: { sectorId?: string | null }) => void {
  const navigate = useNavigate()
  const setDraft = useStudio((s) => s.setDraft)
  const setTab = useStudio((s) => s.setTab)
  const setRun = useStudio((s) => s.setRun)
  return (d, opts) => {
    setDraft(eventStudyDraft(d, opts))
    setRun(null)
    setTab('builder')
    navigate('/studio')
  }
}
