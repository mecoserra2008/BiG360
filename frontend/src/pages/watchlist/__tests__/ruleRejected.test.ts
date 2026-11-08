import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/remote', () => {
  class AlertRejected extends Error {
    status = 400
  }
  return {
    AlertRejected,
    watchlistNames: {},
    createAlert: vi.fn(async () => {
      throw new AlertRejected('price_change_pct_1d: param \'pct\' (number) is required')
    }),
    updateAlert: vi.fn(async () => undefined),
    deleteAlert: vi.fn(async () => undefined),
    addWatchItem: vi.fn(async () => undefined),
    removeWatchItem: vi.fn(async () => undefined),
    syncWatchlist: vi.fn(async () => null),
    loadAlerts: vi.fn(async () => null),
  }
})

import { useToasts } from '@/components/ui/toast'
import { useWatchlist } from '@/store'

describe('rejected alert rules (guide 13: the HTTP 400 was swallowed)', () => {
  beforeEach(() => useWatchlist.setState({ rules: [] }))
  it('removes the local rule again and shows the server message', async () => {
    useWatchlist.getState().addRule({ companyId: 'S1', ticker: 'MSFT', metric: 'change1dPct', operator: '>', threshold: 5, enabled: true })
    expect(useWatchlist.getState().rules).toHaveLength(1)
    await vi.waitFor(() => expect(useWatchlist.getState().rules).toHaveLength(0))
    const t = useToasts.getState().toasts.at(-1)
    expect(t).toMatchObject({ tone: 'error', title: 'Alert rule not saved (MSFT)' })
    expect(t?.message).toContain("param 'pct'")
  })
})
