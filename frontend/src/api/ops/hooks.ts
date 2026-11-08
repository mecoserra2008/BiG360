import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { opsApi } from './client'
import type { ScheduleProfileId } from './types'

export const opk = {
  schedule: () => ['ops', 'schedule'] as const,
  dataHealth: () => ['ops', 'data-health'] as const,
  sync: () => ['ops', 'sync'] as const,
}

export const useSchedule = () => useQuery({ queryKey: opk.schedule(), queryFn: () => opsApi.schedule(), refetchInterval: 60_000, retry: 1 })
export const useDataHealth = () => useQuery({ queryKey: opk.dataHealth(), queryFn: () => opsApi.dataHealth(), refetchInterval: 30_000, retry: 1 })

/** Startup sync status: polled every 5 s while a sync runs, every 60 s otherwise; a backend without the
 * endpoint (< 1.41) answers 404 -> no chip, no panel. */
export const useSyncStatus = (enabled = true) =>
  useQuery({
    queryKey: opk.sync(),
    queryFn: () => opsApi.sync(),
    enabled,
    retry: false,
    refetchInterval: (q) => (q.state.error ? false : q.state.data?.chip?.running ? 5_000 : 60_000),
  })

export function useRequestSync() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (datasets?: string[]) => opsApi.requestSync(datasets),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: opk.sync() })
    },
  })
}

export function useSetSchedule() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (p: ScheduleProfileId) => opsApi.setSchedule(p),
    onSuccess: (data) => {
      qc.setQueryData(opk.schedule(), data)
      void qc.invalidateQueries({ queryKey: ['system'] })
      void qc.invalidateQueries({ queryKey: ['freshness'] })
      void qc.invalidateQueries({ queryKey: opk.dataHealth() })
    },
  })
}
