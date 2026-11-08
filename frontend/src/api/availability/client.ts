import { resolveId } from '../client'
import { USE_MOCK, http } from '../http'
import type { AvailabilityReason, FieldAvailabilityCatalog } from '@/lib/availability'

export interface CompanyAvailability {
  security_id: string
  region?: string | null
  as_of?: string | null
  px_date?: string | null
  has_fundamentals?: boolean
  reasons: Record<string, AvailabilityReason>
}

const EMPTY: FieldAvailabilityCatalog = { version: 1, fields: {} }

/** v1.42.0 data-availability reasons (docs/DATA_COMPLETENESS.md). Mock mode / older API: empty (generic tooltips). */
export const availabilityApi = {
  async catalog(): Promise<FieldAvailabilityCatalog> {
    if (USE_MOCK) return EMPTY
    try {
      return await http.get<FieldAvailabilityCatalog>('/meta/field-availability')
    } catch {
      return EMPTY
    }
  },
  async company(id: string, fields?: readonly string[]): Promise<CompanyAvailability | null> {
    if (USE_MOCK) return null
    try {
      const sid = await resolveId(id)
      return await http.get<CompanyAvailability>(`/companies/${encodeURIComponent(sid)}/availability`, fields?.length ? { fields } : undefined)
    } catch {
      return null
    }
  },
}
