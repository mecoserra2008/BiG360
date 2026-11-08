import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement as h } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { ek } from '@/api/earnings/hooks'
import type { ECompany } from '@/api/earnings/types'
import { TooltipProvider } from '@/components/ui/tooltip'
import { makeReason } from '@/lib/availability'
import EarningsTab from '../EarningsTab'

// COVERAGE_V2 W1 (2026-10-31): every empty section of Company 360 > Earnings & Events says WHY for this company
// (no releases recorded, no statements, nothing announced) - never the old universe-wide "Not covered" note.
const base = {
  security: { security_id: 'S1', ticker: '0001.HK', name: 'Test Co', currency: 'HKD' },
  as_of: '2026-10-30',
  next: [],
  history: [],
  reaction_stats: { n_events: 0, n_reactions: 0 },
  expectations: [],
  corporate_events: [],
  pead: null,
  labels: {},
  model_note: 'model-based',
  data_status: 'ok',
} as unknown as ECompany

// node environment: the chart-token hook reads CSS variables (empty here -> the built-in defaults)
const g = globalThis as unknown as Record<string, unknown>
g.document ??= { documentElement: {} }
g.getComputedStyle ??= () => ({ getPropertyValue: () => '' })

const render = (d: ECompany) => {
  const qc = new QueryClient()
  qc.setQueryData(ek.company('S1'), d)
  return renderToStaticMarkup(h(QueryClientProvider, { client: qc }, h(TooltipProvider, null, h(EarningsTab, { id: 'S1' }))))
}

describe('EarningsTab reasons', () => {
  it('shows the specific reason of each empty section', () => {
    const html = render({
      ...base,
      availability: {
        next: makeReason('insufficient_data', 'No date announced and no release history to project the reporting cadence from'),
        history: makeReason('insufficient_data', 'No earnings release recorded for this company yet'),
        reactions: null,
        expectations: makeReason('no_source', 'No free structured quarterly results feed for this market'),
      },
    } as unknown as ECompany)
    expect(html).toContain('no release history to project the reporting cadence')
    expect(html).toContain('No earnings release recorded for this company yet')
    expect(html).toContain('No free structured quarterly results feed for this market')
    expect(html).not.toContain('Not covered')
    expect(html).not.toContain('mvp_us')
  })

  it('keeps the generic empty states for an older API without availability', () => {
    const html = render(base)
    expect(html).toContain('No upcoming date')
    expect(html).toContain('No expectations')
  })
})
