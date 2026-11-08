import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement as h, type ReactElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { GENERIC_REASON, makeReason, notPublishedUntil } from '@/lib/availability'
import { Avail, Missing, MissingContent, NoDataState } from '../missing'
import { TooltipProvider } from '../tooltip'

const wrap = (el: ReactElement) => renderToStaticMarkup(h(QueryClientProvider, { client: new QueryClient() }, h(TooltipProvider, null, el)))

describe('<Missing> / <Avail>', () => {
  it('renders an explained dash: code, accessible reason text, dotted underline', () => {
    const html = wrap(h(Missing, { reason: notPublishedUntil('2026-11-05') }))
    expect(html).toContain('data-reason="not_yet_published"')
    expect(html).toContain('Not available. Not published yet: next release 2026-11-05')
    expect(html).toContain('—')
    expect(html).toContain('decoration-dotted')
  })

  it('falls back to the generic reason without catalog or scope', () => {
    const html = wrap(h(Missing, { field: 'roic', region: 'US' }))
    expect(html).toContain('data-reason="not_applicable"')
  })

  it('shows the value when present and the reason when missing', () => {
    expect(wrap(h(Avail, { value: 12.5, field: 'pe_ttm' }, '12.5x'))).toBe('12.5x')
    const nan = wrap(h(Avail, { value: Number.NaN, reason: makeReason('needs_key', 'Needs OPENDART_APIKEY') }, 'x'))
    expect(nan).toContain('data-reason="needs_key"')
    expect(nan).not.toContain('>x<')
    expect(wrap(h(Avail, { value: null, reason: 'Fewer peers' }, 'x'))).toContain('Fewer peers')
  })

  it('an empty panel is "not applicable" only when that is documented (Ownership tab of 7203.T)', () => {
    const na = renderToStaticMarkup(h(NoDataState, { reason: makeReason('not_applicable', 'US-listed securities only (FINRA short interest)'), title: 'No short-interest data' }))
    expect(na).toContain('data-testid="na-state"')
    expect(na).not.toContain('empty-state')
    expect(na).toContain('Not applicable to this listing')
    const gap = renderToStaticMarkup(h(NoDataState, { reason: makeReason('due', 'Pending refresh'), title: 'No short-interest data' }))
    expect(gap).toContain('data-testid="empty-state"')
    expect(gap).toContain('No short-interest data')
    const generic = renderToStaticMarkup(h(NoDataState, { reason: GENERIC_REASON, title: 'No 13F history' }))
    expect(generic).toContain('data-testid="empty-state"') // no documented reason: still an empty-data panel
  })

  it('tooltip content shows label, text and owner, but no owner line for by-design rows', () => {
    const html = renderToStaticMarkup(h(MissingContent, { reason: { ...makeReason('pipeline_bug', 'TDnet actuals not mapped'), detail: 'earnings owner' } }))
    expect(html).toContain('Known data gap')
    expect(html).toContain('TDnet actuals not mapped')
    expect(html).toContain('Fix / owner: earnings owner')
    const byDesign = renderToStaticMarkup(h(MissingContent, { reason: { ...makeReason('not_applicable'), detail: 'by design' } }))
    expect(byDesign).not.toContain('Fix / owner')
  })
})
