import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { DataHealth, OpsSchedule, ScheduleProfileId, SyncRequestAccepted, SyncStatus } from './types'

/** Data Ops endpoints only exist on the live backend; the mock answers "not implemented" (pages show the error state). */
function live(path: string): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}${path}`, 'data ops needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const opsApi = {
  async schedule(): Promise<OpsSchedule> {
    live('/system/schedule')
    return http.get<OpsSchedule>('/system/schedule')
  },
  async setSchedule(profile: ScheduleProfileId): Promise<OpsSchedule> {
    live('/system/schedule')
    return http.put<OpsSchedule>('/system/schedule', { profile })
  },
  async dataHealth(): Promise<DataHealth> {
    live('/system/data-health')
    return http.get<DataHealth>('/system/data-health')
  },
  /** v1.41 startup sync: the running / last sync (reasons per dataset, jobs, publishes, header chip). */
  async sync(): Promise<SyncStatus> {
    live('/system/sync')
    return http.get<SyncStatus>('/system/sync')
  },
  async requestSync(datasets?: string[]): Promise<SyncRequestAccepted> {
    live('/system/sync')
    return http.post<SyncRequestAccepted>('/system/sync', datasets?.length ? { datasets } : {})
  },
}
