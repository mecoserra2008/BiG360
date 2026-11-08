import { useNavigate } from 'react-router'
import { newUid, variableName } from '@/api/studio/adapters'
import type { CTransform, StudyDraft } from '@/api/studio/types'
import { studioFrequency } from '@/api/macro/adapters'
import type { MSeriesData } from '@/api/macro/types'
import { EMPTY_DRAFT, useStudio } from '@/pages/studio/store'
import { isoDaysFromNow } from './fmt'

/** "Open in Model Studio": a new time-series study with this official macro series as the target (ARIMA by default). */
export function useOpenInStudio(): (d: MSeriesData) => void {
  const navigate = useNavigate()
  const setDraft = useStudio((s) => s.setDraft)
  const setTab = useStudio((s) => s.setTab)
  const setRun = useStudio((s) => s.setRun)
  return (d) => {
    const freq = studioFrequency(d.meta.frequency)
    const yoy = d.meta.default_transform === 'yoy'
    const transforms: CTransform[] = yoy ? [{ op: 'pct_change', k: freq === 'Q' ? 4 : freq === 'W' ? 52 : 12 }, { op: 'scale', factor: 100 }] : []
    const start = `${Number(isoDaysFromNow(0).slice(0, 4)) - 20}-01-01`
    const draft: StudyDraft = {
      ...EMPTY_DRAFT,
      name: `${d.meta.title ?? d.meta.series_id}`.slice(0, 80),
      kind: 'timeseries',
      frequency: freq,
      start,
      macroAlignment: 'period',
      method: 'arima',
      params: {},
      variables: [
        {
          uid: newUid(),
          name: variableName(d.meta.series_id, []),
          source: 'macro',
          field: d.meta.series_id,
          title: d.meta.title ?? null,
          role: 'y',
          agg: null,
          entityAgg: null,
          entities: null,
          preTransforms: [],
          transforms,
          extra: {},
        },
      ],
    }
    setDraft(draft)
    setRun(null)
    setTab('builder')
    navigate('/studio')
  }
}
