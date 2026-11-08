import { useQuery } from '@tanstack/react-query'
import { instrumentsApi } from './client'

const H = 30 * 60_000
export const useInstruments = (type?: string) => useQuery({ queryKey: ['instruments', type ?? 'all'], queryFn: () => instrumentsApi.list(type), staleTime: H })
export const useInstrument = (id: string) => useQuery({ queryKey: ['instrument', id], queryFn: () => instrumentsApi.detail(id), staleTime: H })
