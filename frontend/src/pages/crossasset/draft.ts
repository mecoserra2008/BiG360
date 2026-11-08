import { studioSeeds } from '@/api/crossasset/adapters'
import { newUid } from '@/api/studio/adapters'
import type { StudyDraft } from '@/api/studio/types'
import { EMPTY_DRAFT } from '@/pages/studio/store'

/** Model Studio draft: weekly OLS of the asset's log return on its lagged cross-asset signals (source `cross_asset`). */
export function crossAssetDraft(asset: { asset_id: string; name: string }, hasPositioning: boolean, hasCarry = true): StudyDraft {
  return {
    ...EMPTY_DRAFT,
    name: `${asset.name} · cross-asset signals`.slice(0, 80),
    kind: 'timeseries',
    frequency: 'W',
    start: '2008-01-01',
    method: 'ols',
    params: {},
    variables: studioSeeds(asset, hasPositioning, hasCarry).map((s) => ({
      uid: newUid(),
      name: s.name,
      source: 'cross_asset',
      field: s.field,
      title: s.title,
      role: s.role,
      agg: null,
      entityAgg: null,
      entities: null,
      preTransforms: [],
      transforms: s.transforms,
      extra: {},
    })),
  }
}
