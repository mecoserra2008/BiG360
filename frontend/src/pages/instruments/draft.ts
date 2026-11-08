import type { InstrumentDetail } from '@/api/instruments/types'
import { newUid } from '@/api/studio/adapters'
import type { StudyDraft } from '@/api/studio/types'
import { EMPTY_DRAFT } from '@/pages/studio/store'

/** Model Studio draft: weekly log returns of this instrument (time series, ARIMA by default). */
export function instrumentDraft(d: Pick<InstrumentDetail, 'ticker' | 'name' | 'type'>): StudyDraft {
  return {
    ...EMPTY_DRAFT,
    name: `${d.ticker} · ${d.name}`.slice(0, 80),
    kind: 'timeseries',
    frequency: 'W',
    start: '2000-01-01',
    entities: { tickers: [d.ticker], primary_only: false },
    method: 'arima',
    params: {},
    variables: [
      { uid: newUid(), name: 'log_ret', source: 'prices', field: 'log_ret', title: `${d.ticker} log return`, role: 'y', agg: null, entityAgg: null, entities: { tickers: [d.ticker], primary_only: false }, preTransforms: [], transforms: [], extra: {} },
    ],
  }
}

