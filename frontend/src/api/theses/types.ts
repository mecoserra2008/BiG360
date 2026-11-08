/**
 * Thesis tracker (contract v1.26.0, /api/v1/theses/*). Payloads keep the contract shape; ./adapters derives what the
 * pages need (board columns, status tones, chart bands / markers, draft <-> request bodies).
 */
import type { components } from '../generated/schema'

type S = components['schemas']

export type Thesis = S['Thesis']
export type ThesisDetail = S['ThesisDetail']
export type ThesisList = S['ThesisList']
export type ThesisKpi = S['ThesisKpi']
export type ThesisCatalyst = S['ThesisCatalyst']
export type ThesisInvalidation = S['ThesisInvalidation']
export type ThesisReason = S['ThesisReason']
export type ThesisAlertRule = S['ThesisAlertRule']
export type ThesisEvent = S['ThesisEvent']
export type ThesisSnapshot = S['ThesisSnapshot']
export type ThesisReview = S['ThesisReview']
export type ThesisReviewList = S['ThesisReviewList']
export type ThesisJournalEntry = S['ThesisJournalEntry']
export type ThesisJournalLink = S['ThesisJournalLink']
export type ThesisStatus = S['ThesisStatus']
export type ThesisKpiValue = S['ThesisKpiValue']
export type ThesisPerformance = S['ThesisPerformance']
export type ThesisTimeline = S['ThesisTimeline']
export type ThesisMarker = S['ThesisMarker']
export type ThesisEvidence = S['ThesisEvidence']
export type ThesisStats = S['ThesisStats']
export type ThesisStatsGroup = S['ThesisStatsGroup']
export type ThesisSubjectDefaults = S['ThesisSubjectDefaults']
export type ThesisCreate = S['ThesisCreate']
export type ThesisUpdate = S['ThesisUpdate']
export type SubjectType = S['ThesisSubjectType']
export type Direction = S['ThesisDirection']
export type Horizon = S['ThesisHorizon']
export type BoardStatus = S['ThesisStatusColumn']
export type AutoStatus = S['ThesisAutoStatus']
export type CloseReason = NonNullable<Thesis['close_reason']>
export type Outcome = NonNullable<Thesis['outcome']>
export type OriginKind = NonNullable<S['ThesisOrigin']['kind']>

export interface ThesisFilters {
  status?: BoardStatus | ''
  subjectType?: SubjectType | ''
  direction?: Direction | ''
  autoStatus?: AutoStatus | ''
  q?: string
  tag?: string
  minConviction?: number
}

export interface ReviewInput {
  decision: 'hold' | 'add' | 'trim' | 'close' | 'none'
  notes_md?: string | null
  conviction_after?: number | null
  next_review_date?: string | null
}

export interface JournalInput {
  body_md: string
  entry_date?: string | null
  kind?: ThesisJournalEntry['kind']
  links?: ThesisJournalLink[] | null
}
