import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { conformalApi } from './client'
import type { PairQuery, PeerGroupQuery } from './types'

const M = 10 * 60_000

export const cfk = {
  envelope: (id: string) => ['conformal', 'envelope', id] as const,
  touch: (id: string, h: number, levels: number[] | undefined) => ['conformal', 'touch', id, h, levels ?? null] as const,
  peers: (q: PeerGroupQuery) => ['conformal', 'peers', q] as const,
  pair: (q: PairQuery) => ['conformal', 'pair', q] as const,
  coverage: (doc: boolean) => ['conformal', 'coverage', doc] as const,
}

export const useConformalEnvelope = (id: string, enabled = true) =>
  useQuery({ queryKey: cfk.envelope(id), queryFn: () => conformalApi.envelope(id), staleTime: M, enabled: enabled && !!id, retry: 1 })

/** levels undefined = automatic mode (zones + technical levels). */
export const useConformalTouch = (id: string, horizon: number, levels?: number[], enabled = true) =>
  useQuery({
    queryKey: cfk.touch(id, horizon, levels),
    queryFn: () => conformalApi.touch(id, horizon, levels),
    staleTime: M,
    enabled: enabled && !!id && (levels === undefined || levels.length > 0),
    placeholderData: keepPreviousData,
    retry: 1,
  })

export const usePeerGroups = (q: PeerGroupQuery, enabled = true) =>
  useQuery({ queryKey: cfk.peers(q), queryFn: () => conformalApi.peerGroups(q), staleTime: M, enabled, placeholderData: keepPreviousData, retry: 1 })

export const usePairBand = (q: PairQuery, enabled = true) =>
  useQuery({
    queryKey: cfk.pair(q),
    queryFn: () => conformalApi.pairBand(q),
    staleTime: M,
    enabled: enabled && !!q.groupId && (!!q.weights || (!!q.i && !!q.j && q.i !== q.j)),
    retry: 0,
  })

export const useConformalCoverage = (includeDoc = false, enabled = true) =>
  useQuery({ queryKey: cfk.coverage(includeDoc), queryFn: () => conformalApi.coverage(includeDoc), staleTime: M, enabled, retry: 1 })
