import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { isTerminal } from './adapters'
import { backtestApi } from './client'

export const bk = {
  catalog: () => ['backtest', 'catalog'] as const,
  runs: () => ['backtest', 'runs'] as const,
  run: (id: string) => ['backtest', 'run', id] as const,
  holdings: (id: string, date: string | null) => ['backtest', 'holdings', id, date ?? 'latest'] as const,
  trades: (id: string, offset: number, entity: string | null) => ['backtest', 'trades', id, offset, entity ?? ''] as const,
  compare: (ids: string[]) => ['backtest', 'compare', ...ids] as const,
  strategies: () => ['backtest', 'strategies'] as const,
  strategy: (id: string) => ['backtest', 'strategy', id] as const,
}

const HOUR = 3600_000

export const useBtCatalog = () => useQuery({ queryKey: bk.catalog(), queryFn: () => backtestApi.catalog(), staleTime: HOUR })
export const useBtRuns = () => useQuery({ queryKey: bk.runs(), queryFn: () => backtestApi.runs(), staleTime: 15_000 })
export const useBtStrategies = () => useQuery({ queryKey: bk.strategies(), queryFn: () => backtestApi.strategies(), staleTime: 15_000 })
export const useBtStrategy = (id: string | null) =>
  useQuery({ queryKey: bk.strategy(id ?? ''), queryFn: () => backtestApi.strategy(id as string), enabled: !!id, staleTime: 15_000 })

/** Polls every 1.5 s while the run is queued / running; stops once terminal. */
export const useBtRun = (runId: string | null) =>
  useQuery({
    queryKey: bk.run(runId ?? ''),
    queryFn: () => backtestApi.run(runId as string),
    enabled: !!runId,
    staleTime: Infinity,
    refetchInterval: (q) => {
      const s = q.state.data?.run.status
      return s && !isTerminal(s) ? 1000 : false
    },
  })

export const useBtHoldings = (runId: string | null, date: string | null, enabled = true) =>
  useQuery({ queryKey: bk.holdings(runId ?? '', date), queryFn: () => backtestApi.holdings(runId as string, date), enabled: !!runId && enabled, staleTime: Infinity, placeholderData: keepPreviousData })

export const useBtTrades = (runId: string | null, offset: number, entity: string | null, enabled = true) =>
  useQuery({
    queryKey: bk.trades(runId ?? '', offset, entity),
    queryFn: () => backtestApi.trades(runId as string, { offset, limit: 200, entityId: entity }),
    enabled: !!runId && enabled,
    staleTime: Infinity,
    placeholderData: keepPreviousData,
  })

export const useBtCompare = (ids: string[]) =>
  useQuery({ queryKey: bk.compare(ids), queryFn: () => backtestApi.compare(ids), enabled: ids.length > 0, staleTime: 60_000, placeholderData: keepPreviousData })
