import { keepPreviousData, useMutation, useQuery } from '@tanstack/react-query'
import { supplyChainApi } from './client'
import type { NetworkQuery, ScPortfolioRequest, ScShockRequest } from './types'

const M = 10 * 60_000

export const sck = {
  network: (id: string, q: NetworkQuery) => ['supply-chain', 'network', id, q] as const,
  geography: (id: string) => ['supply-chain', 'geography', id] as const,
  countryRisk: () => ['supply-chain', 'country-risk'] as const,
  portfolio: (b: ScPortfolioRequest) => ['supply-chain', 'portfolio', b] as const,
}

export const useCompanyNetwork = (id: string, q: NetworkQuery) =>
  useQuery({ queryKey: sck.network(id, q), queryFn: () => supplyChainApi.network(id, q), staleTime: M, placeholderData: keepPreviousData, retry: 1 })
export const useCompanyGeography = (id: string) =>
  useQuery({ queryKey: sck.geography(id), queryFn: () => supplyChainApi.geography(id), staleTime: M, retry: 1 })
export const useCountryRisk = (enabled = true) =>
  useQuery({ queryKey: sck.countryRisk(), queryFn: () => supplyChainApi.countryRisk(), staleTime: M, enabled, retry: 1 })
export const usePortfolioExposure = (b: ScPortfolioRequest, enabled = true) =>
  useQuery({ queryKey: sck.portfolio(b), queryFn: () => supplyChainApi.portfolio(b), staleTime: M, enabled, placeholderData: keepPreviousData, retry: 1 })
export const useShock = () => useMutation({ mutationFn: (b: ScShockRequest) => supplyChainApi.shock(b) })
