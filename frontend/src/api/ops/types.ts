/**
 * Data Ops (contract v1.28.0): schedule profiles (/system/schedule), the Data Health page (/system/data-health) and the
 * scheduler / data-as-of / ops-alert fields of /system/status. Payloads keep the contract shape; ./adapters derives
 * the header summary, heatmap tones and labels.
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type OpsSchedule = S['Schedule']
export type OpsScheduleProfile = S['ScheduleProfile']
export type OpsScheduleEntry = S['ScheduleEntry']
export type OpsSchedulerStatus = S['SchedulerStatus']
export type OpsServingSection = S['ServingSection']
export type OpsAlert = S['OpsAlert']
export type DataHealth = S['DataHealth']
export type DataHealthDataset = S['DataHealthDataset']
export type DataGap = S['DataGap']
export type DataHealthCampaign = S['DataHealthCampaign']
export type DataHealthSource = S['DataHealthSource']
export type ScheduleProfileId = 'weekly' | 'daily' | 'intraday'

/** Header freshness chip (F01 / F03 / F13). */
export interface HeaderFreshness {
  /** data as-of shown in the header (most recent core-region as-of) */
  asOf: string | null
  /** per-region as-of for the tooltip */
  regions: Record<string, string>
  /** snapshot rebuild time (shown as secondary info only) */
  builtAt: string | null
  status: 'fresh' | 'stale' | 'error'
  schedulerOn: boolean | null
  profile: string | null
  /** short chip text when something needs attention ('' = no chip) */
  chip: string
  chipTone: 'warn' | 'down' | null
  alerts: OpsAlert[]
  staleSources: string[]
  /** v1.28 serving_sections of the published snapshot (empty on older backends) */
  sections?: { section: string; group: string | null; status: string; dataAsOf: string | null; detail: string | null }[]
  /** coverage v3 A7: the background status cache behind this summary (null on older backends) */
  statusCache?: StatusCacheView | null
}

/**
 * Coverage v3 A7: /system/status is computed in the background. `partial` = some sections failed and serve their last
 * good value (at most ~10 min old, see sectionAsOf) or nothing; `warming` / `error` = no full payload yet.
 */
export interface StatusCacheView {
  state: 'fresh' | 'stale' | 'partial' | 'warming' | 'error'
  computedAt: string | null
  /** failed section names (data_freshness, last_dag_runs, universe_counts, ops_status, runtime) */
  failedSections: string[]
  sectionErrors: string[]
  sectionAsOf: Record<string, string>
  /** the freshness rows / ops alerts are not current: never read them as "fresh" / "all clear" */
  freshnessUnknown: boolean
}

/** Startup sync (contract v1.41.0, GET /system/sync): reason codes per dataset, jobs, publishes, header chip. */
export type SyncStatus = S['SyncStatus']
export type SyncDataset = S['SyncDataset']
export type SyncEntity = S['SyncEntity']
export type SyncJob = S['SyncJob']
export type SyncReason = S['SyncReason']
export type SyncChip = S['SyncChip']
export type SyncRequestAccepted = S['SyncRequestAccepted']
