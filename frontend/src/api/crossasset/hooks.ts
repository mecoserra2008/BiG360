import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { crossAssetApi } from './client'
import type { XaClass, XaWindow } from './types'

const H = 30 * 60_000
export const useXaCockpit = (assetClass?: XaClass, securityId?: string, enabled = true) =>
  useQuery({ queryKey: ['cross-asset', 'cockpit', assetClass ?? '', securityId ?? ''], queryFn: () => crossAssetApi.cockpit(assetClass, securityId), staleTime: H, placeholderData: keepPreviousData, enabled })
export const useXaAsset = (id: string | null, from: string | null) =>
  useQuery({ queryKey: ['cross-asset', 'asset', id ?? '', from ?? 'default'], queryFn: () => crossAssetApi.asset(id as string, from), staleTime: H, placeholderData: keepPreviousData, enabled: !!id })
export const useXaCurves = (enabled = true) => useQuery({ queryKey: ['cross-asset', 'curves'], queryFn: () => crossAssetApi.curves(), staleTime: H, enabled })
export const useXaCurve = (root: string | null, from: string | null = null) =>
  useQuery({ queryKey: ['cross-asset', 'curve', root ?? '', from ?? 'all'], queryFn: () => crossAssetApi.curve(root as string, from), staleTime: H, placeholderData: keepPreviousData, enabled: !!root })
export const useXaCorrelation = (window: XaWindow, from: string | null = null) =>
  useQuery({ queryKey: ['cross-asset', 'correlation', window, from ?? 'all'], queryFn: () => crossAssetApi.correlation(window, from), staleTime: H, placeholderData: keepPreviousData })
export const useXaRisk = (from: string | null = null) =>
  useQuery({ queryKey: ['cross-asset', 'risk', from ?? 'all'], queryFn: () => crossAssetApi.risk(from), staleTime: H, placeholderData: keepPreviousData })
