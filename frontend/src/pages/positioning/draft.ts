import { studioSeeds } from '@/api/positioning/adapters'
import type { PosDetail } from '@/api/positioning/types'
import { newUid } from '@/api/studio/adapters'
import type { StudyDraft } from '@/api/studio/types'
import { EMPTY_DRAFT } from '@/pages/studio/store'

/** Model Studio draft: weekly OLS of the mapped instrument's log return on CFTC positioning (source `cftc`). */
export function positioningDraft(d: Pick<PosDetail, 'market' | 'report_type' | 'headline_category' | 'price_instrument'>): StudyDraft {
  const seeds = studioSeeds(d)
  return {
    ...EMPTY_DRAFT,
    name: `${d.market.name} · CFTC positioning`.slice(0, 80),
    kind: 'timeseries',
    frequency: 'W',
    start: '2008-01-01',
    entities: d.price_instrument ? { tickers: [d.price_instrument.ticker], primary_only: false } : EMPTY_DRAFT.entities,
    method: 'ols',
    params: {},
    variables: seeds.map((s) => ({
      uid: newUid(),
      name: s.name,
      source: s.source,
      field: s.field,
      title: s.title,
      role: s.role,
      agg: null,
      entityAgg: null,
      entities: s.ticker ? { tickers: [s.ticker], primary_only: false } : null,
      preTransforms: [],
      transforms: s.source === 'cftc' && s.role === 'x' ? [{ op: 'lag', k: 1 }] : [],
      extra: {},
    })),
  }
}
