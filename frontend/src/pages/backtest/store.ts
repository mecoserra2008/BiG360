import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import { defaultSpec, toDraft } from '@/api/backtest/adapters'
import type { BtDraft, BtOrigin, BtSpec } from '@/api/backtest/types'
import { safeStorage } from '@/lib/utils'

const storage = createJSONStorage(
  (): StateStorage => ({
    getItem: (k) => safeStorage.get(k),
    setItem: (k, v) => safeStorage.set(k, v),
    removeItem: (k) => safeStorage.remove(k),
  }),
)

export type BtTab = 'builder' | 'strategies' | 'compare' | 'presets'
export type BtResultTab = 'performance' | 'risk' | 'attribution' | 'trades' | 'robustness' | 'events'

interface BacktestState {
  tab: BtTab
  resultTab: BtResultTab
  spec: BtDraft
  /** where the draft came from (entry points) */
  origin: BtOrigin | null
  /** saved strategy the draft was opened from (runs count toward its multiple-testing counter) */
  strategyId: string | null
  runId: string | null
  watching: string[]
  compare: string[]
  setTab: (t: BtTab) => void
  setResultTab: (t: BtResultTab) => void
  setSpec: (s: BtDraft) => void
  patchSpec: (fn: (s: BtDraft) => BtDraft) => void
  /** load a draft from an entry point / preset / saved strategy and show the builder */
  open: (s: BtSpec, opts?: { origin?: BtOrigin | null; strategyId?: string | null; runId?: string | null }) => void
  reset: () => void
  setRun: (id: string | null) => void
  watch: (id: string) => void
  unwatch: (id: string) => void
  toggleCompare: (id: string) => void
  clearCompare: () => void
}

export const useBacktest = create<BacktestState>()(
  persist(
    (set) => ({
      tab: 'builder',
      resultTab: 'performance',
      spec: defaultSpec(),
      origin: null,
      strategyId: null,
      runId: null,
      watching: [],
      compare: [],
      setTab: (tab) => set({ tab }),
      setResultTab: (resultTab) => set({ resultTab }),
      setSpec: (spec) => set({ spec }),
      patchSpec: (fn) => set((st) => ({ spec: fn(st.spec) })),
      open: (spec, opts = {}) =>
        set({ spec: toDraft(spec), origin: opts.origin ?? null, strategyId: opts.strategyId ?? null, runId: opts.runId ?? null, tab: 'builder', resultTab: 'performance' }),
      reset: () => set({ spec: defaultSpec(), origin: null, strategyId: null, runId: null }),
      setRun: (runId) => set({ runId }),
      watch: (id) => set((st) => ({ watching: st.watching.includes(id) ? st.watching : [...st.watching, id] })),
      unwatch: (id) => set((st) => ({ watching: st.watching.filter((x) => x !== id) })),
      toggleCompare: (id) => set((st) => ({ compare: st.compare.includes(id) ? st.compare.filter((x) => x !== id) : [...st.compare, id].slice(-6) })),
      clearCompare: () => set({ compare: [] }),
    }),
    { name: 'big360.backtest.v1', storage, partialize: (s) => ({ spec: s.spec, origin: s.origin, strategyId: s.strategyId, runId: s.runId, compare: s.compare, tab: s.tab }) },
  ),
)
