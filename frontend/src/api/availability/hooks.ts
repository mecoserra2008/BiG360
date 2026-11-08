import { useQuery } from '@tanstack/react-query'
import { availabilityApi } from './client'

/** Field-availability catalog: changes with the completeness audit, so it is cached for the session. */
export const useFieldAvailability = () =>
  useQuery({ queryKey: ['availability', 'catalog'] as const, queryFn: () => availabilityApi.catalog(), staleTime: 60 * 60_000, gcTime: 2 * 60 * 60_000, retry: 0 })

/** Per-security reasons for the Company 360 key fields (null value -> why). */
export const useCompanyAvailability = (id: string | null | undefined, fields?: readonly string[]) =>
  useQuery({
    queryKey: ['availability', 'company', id ?? '', (fields ?? []).join(',')] as const,
    queryFn: () => availabilityApi.company(id as string, fields),
    enabled: !!id,
    staleTime: 10 * 60_000,
    retry: 0,
  })
