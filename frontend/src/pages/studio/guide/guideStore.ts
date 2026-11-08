import { create } from 'zustand'
import type { CGuideQuestion, CGuideVar, GuideGoal, GuideOutcomeKind, GuideSuggestionView } from '@/api/studio/types'

/** Guided-builder question (kept across tab switches; not persisted). */
export interface GuideForm {
  goal: GuideGoal
  outcome: CGuideVar | null
  outcomeKind: GuideOutcomeKind
  driver: CGuideVar | null
  controls: CGuideVar[]
  suggestControls: boolean
  horizon: number
  frequency: NonNullable<CGuideQuestion['frequency']>
  /** '' = no universe: macro time series */
  universe: string
  sectors: string[]
  kind: NonNullable<CGuideQuestion['kind']>
  start: string
  end: string
}

export const GUIDE_DEFAULT: GuideForm = {
  goal: 'effect',
  outcome: null,
  outcomeKind: 'return',
  driver: null,
  controls: [],
  suggestControls: true,
  horizon: 1,
  frequency: 'M',
  universe: 'mvp_us',
  sectors: [],
  kind: 'auto',
  start: '',
  end: '',
}

/** Form → StudioGuideQuestion (null when the outcome is still missing). */
export function toQuestion(f: GuideForm): CGuideQuestion | null {
  if (!f.outcome) return null
  return {
    goal: f.goal,
    outcome: f.outcome,
    outcome_kind: f.outcomeKind,
    driver: f.driver,
    controls: f.controls,
    suggest_controls: f.suggestControls,
    horizon: Math.max(0, Math.min(60, Math.trunc(f.horizon) || 0)),
    frequency: f.frequency,
    universe: f.universe || null,
    sectors: f.sectors.length ? f.sectors : null,
    kind: f.kind,
    start: f.start || null,
    end: f.end || null,
  }
}

interface GuideState {
  form: GuideForm
  suggestion: GuideSuggestionView | null
  patch: (p: Partial<GuideForm>) => void
  setSuggestion: (s: GuideSuggestionView | null) => void
  reset: () => void
}

export const useGuide = create<GuideState>()((set, get) => ({
  form: GUIDE_DEFAULT,
  suggestion: null,
  patch: (p) => set({ form: { ...get().form, ...p } }),
  setSuggestion: (suggestion) => set({ suggestion }),
  reset: () => set({ form: GUIDE_DEFAULT, suggestion: null }),
}))
