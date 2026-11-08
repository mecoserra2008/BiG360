import { useQuery } from '@tanstack/react-query'
import { create } from 'zustand'
import { createJSONStorage, persist, type StateStorage } from 'zustand/middleware'
import { safeStorage } from '@/lib/utils'
import { USE_MOCK } from '../http'
import { techProbApi } from './client'

const opts = { staleTime: 10 * 60_000, retry: 1 } as const

export const useSetupProbabilities = (id: string, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'setup-prob', id] as const, queryFn: () => techProbApi.setup(id), enabled: enabled && !!id && !USE_MOCK, ...opts })

/** Custom barriers (thesis target / stop): probabilities computed at query time from the same calibrated surface. */
export const useBarrierProbabilities = (id: string, target: number | null | undefined, stop: number | null | undefined,
                                        direction: 'long' | 'short' | undefined, enabled = true) =>
  useQuery({
    queryKey: ['technicals', 'setup-prob', id, target, stop, direction] as const,
    queryFn: () => techProbApi.setup(id, { target: target as number, stop: stop as number, direction }),
    enabled: enabled && !!id && !USE_MOCK && typeof target === 'number' && typeof stop === 'number' && target > 0 && stop > 0,
    ...opts,
  })

export const useZoneHold = (id: string, horizon: number, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'zones', id, horizon] as const, queryFn: () => techProbApi.zones(id, horizon), enabled: enabled && !!id && !USE_MOCK, ...opts })

export const useScoreCalibration = (id: string, enabled = true) =>
  useQuery({ queryKey: ['technicals', 'score-cal', id] as const, queryFn: () => techProbApi.score(id), enabled: enabled && !!id && !USE_MOCK, ...opts })

/* ── "experimental" toggle: outputs that failed the T6 gate are hidden unless it is on (per viewer, persisted) ── */

const storage = createJSONStorage(
  (): StateStorage => ({ getItem: (k) => safeStorage.get(k), setItem: (k, v) => safeStorage.set(k, v), removeItem: (k) => safeStorage.remove(k) }),
)

interface ExperimentalState {
  show: boolean
  setShow: (v: boolean) => void
}

export const useTechExperimental = create<ExperimentalState>()(
  persist((set) => ({ show: false, setShow: (show) => set({ show }) }), { name: 'big360.technicals.experimental', storage }),
)
