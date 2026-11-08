import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import { specToDraft } from '@/api/studio/adapters'
import type { CStudySpec, DraftVariable, StudyDraft } from '@/api/studio/types'
import { safeStorage } from '@/lib/utils'

const storage = createJSONStorage(
  (): StateStorage => ({
    getItem: (k) => safeStorage.get(k),
    setItem: (k, v) => safeStorage.set(k, v),
    removeItem: (k) => safeStorage.remove(k),
  }),
)

export const EMPTY_DRAFT: StudyDraft = {
  name: '',
  kind: 'timeseries',
  frequency: 'M',
  start: '2005-01-01',
  end: null,
  entities: { primary_only: true },
  entityLevel: 'security',
  macroAlignment: 'auto',
  variables: [],
  method: 'ols',
  params: {},
  seed: 42,
  timeoutS: 600,
  datasetExtra: {},
}

export type StudioTab = 'builder' | 'guide' | 'studies' | 'presets' | 'registry' | 'monitor'

interface StudioState {
  tab: StudioTab
  draft: StudyDraft
  /** saved study the draft was opened from (save = update) */
  studyId: string | null
  /** preset the draft was loaded from */
  presetId: string | null
  /** run shown in the results pane */
  runId: string | null
  /** async runs being watched for completion (toast) */
  watching: string[]
  /** run ids ticked for comparison */
  compare: string[]
  /** Model Registry entry open in the registry tab */
  modelId: string | null
  setTab: (t: StudioTab) => void
  patchDraft: (p: Partial<StudyDraft>) => void
  setDraft: (d: StudyDraft) => void
  patchVariable: (uid: string, p: Partial<DraftVariable>) => void
  addVariable: (v: DraftVariable) => void
  removeVariable: (uid: string) => void
  moveVariable: (uid: string, dir: -1 | 1) => void
  loadSpec: (spec: CStudySpec, opts: { needsY?: boolean; studyId?: string | null; presetId?: string | null; runId?: string | null }) => void
  newStudy: () => void
  setRun: (id: string | null) => void
  watch: (id: string) => void
  unwatch: (id: string) => void
  toggleCompare: (id: string) => void
  clearCompare: () => void
  setStudyId: (id: string | null) => void
  setModelId: (id: string | null) => void
}

export const useStudio = create<StudioState>()(
  persist(
    (set, get) => ({
      tab: 'builder',
      draft: EMPTY_DRAFT,
      studyId: null,
      presetId: null,
      runId: null,
      watching: [],
      compare: [],
      modelId: null,
      setTab: (tab) => set({ tab }),
      patchDraft: (p) => set({ draft: { ...get().draft, ...p } }),
      setDraft: (draft) => set({ draft }),
      patchVariable: (uid, p) => set({ draft: { ...get().draft, variables: get().draft.variables.map((v) => (v.uid === uid ? { ...v, ...p } : v)) } }),
      addVariable: (v) => set({ draft: { ...get().draft, variables: [...get().draft.variables, v] } }),
      removeVariable: (uid) => set({ draft: { ...get().draft, variables: get().draft.variables.filter((v) => v.uid !== uid) } }),
      moveVariable: (uid, dir) => {
        const vs = [...get().draft.variables]
        const i = vs.findIndex((v) => v.uid === uid)
        const j = i + dir
        if (i < 0 || j < 0 || j >= vs.length) return
        ;[vs[i], vs[j]] = [vs[j], vs[i]]
        set({ draft: { ...get().draft, variables: vs } })
      },
      loadSpec: (spec, opts) =>
        set({
          draft: specToDraft(spec, { needsY: opts.needsY }),
          studyId: opts.studyId ?? null,
          presetId: opts.presetId ?? null,
          runId: opts.runId === undefined ? get().runId : opts.runId,
          tab: 'builder',
        }),
      newStudy: () => set({ draft: { ...EMPTY_DRAFT }, studyId: null, presetId: null, runId: null }),
      setRun: (runId) => set({ runId }),
      watch: (id) => set({ watching: get().watching.includes(id) ? get().watching : [...get().watching, id] }),
      unwatch: (id) => set({ watching: get().watching.filter((x) => x !== id) }),
      toggleCompare: (id) => set({ compare: get().compare.includes(id) ? get().compare.filter((x) => x !== id) : [...get().compare, id].slice(-6) }),
      clearCompare: () => set({ compare: [] }),
      setStudyId: (studyId) => set({ studyId }),
      setModelId: (modelId) => set({ modelId }),
    }),
    {
      name: 'big360.studio',
      version: 1,
      storage,
      partialize: (s) => ({ tab: s.tab, draft: s.draft, studyId: s.studyId, presetId: s.presetId, runId: s.runId, watching: s.watching, compare: s.compare, modelId: s.modelId }),
    },
  ),
)
