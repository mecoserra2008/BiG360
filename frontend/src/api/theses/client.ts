import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type {
  JournalInput,
  ReviewInput,
  SubjectType,
  ThesisCreate,
  ThesisDetail,
  ThesisEvidence,
  ThesisFilters,
  ThesisJournalEntry,
  ThesisList,
  ThesisPerformance,
  ThesisReview,
  ThesisReviewList,
  ThesisSnapshot,
  ThesisStats,
  ThesisStatus,
  ThesisSubjectDefaults,
  ThesisTimeline,
  ThesisUpdate,
} from './types'

/** The thesis tracker keeps its state in the backend app_state: there is no mock implementation. */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/theses`, 'operation Thesis tracker needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

const enc = encodeURIComponent
const T = (id: string) => `/theses/${enc(id)}`

export const thesesApi = {
  async list(f: ThesisFilters = {}): Promise<ThesisList> {
    live()
    return http.get('/theses', {
      status: f.status || undefined,
      subject_type: f.subjectType || undefined,
      direction: f.direction || undefined,
      auto_status: f.autoStatus || undefined,
      q: f.q?.trim() || undefined,
      tag: f.tag || undefined,
      min_conviction: f.minConviction || undefined,
    })
  },
  async stats(): Promise<ThesisStats> {
    live()
    return http.get('/theses/stats')
  },
  async defaults(subjectType: SubjectType, subjectId: string): Promise<ThesisSubjectDefaults> {
    live()
    return http.get('/theses/subject-defaults', { subject_type: subjectType, subject_id: subjectId })
  },
  async create(body: ThesisCreate): Promise<ThesisDetail> {
    live()
    return http.post('/theses', body)
  },
  async get(id: string): Promise<ThesisDetail> {
    live()
    return http.get(T(id))
  },
  async update(id: string, patch: ThesisUpdate): Promise<ThesisDetail> {
    live()
    return http.patch(T(id), patch)
  },
  async remove(id: string): Promise<void> {
    live()
    await http.del(T(id))
  },
  async journal(id: string): Promise<ThesisJournalEntry[]> {
    live()
    return http.get(`${T(id)}/journal`)
  },
  async addJournal(id: string, body: JournalInput): Promise<ThesisJournalEntry> {
    live()
    return http.post(`${T(id)}/journal`, body)
  },
  async updateJournal(id: string, entryId: string, body: Partial<JournalInput>): Promise<ThesisJournalEntry> {
    live()
    return http.patch(`${T(id)}/journal/${enc(entryId)}`, body)
  },
  async deleteJournal(id: string, entryId: string): Promise<void> {
    live()
    await http.del(`${T(id)}/journal/${enc(entryId)}`)
  },
  async reviews(id: string): Promise<ThesisReviewList> {
    live()
    return http.get(`${T(id)}/reviews`)
  },
  async completeReview(id: string, body: ReviewInput): Promise<ThesisReview> {
    live()
    return http.post(`${T(id)}/reviews`, body)
  },
  async snapshot(id: string, note?: string): Promise<ThesisSnapshot> {
    live()
    return http.post(`${T(id)}/snapshots`, { note: note || null })
  },
  async status(id: string): Promise<ThesisStatus> {
    live()
    return http.get(`${T(id)}/status`)
  },
  async evaluate(id: string): Promise<ThesisStatus> {
    live()
    return http.post(`${T(id)}/evaluate`)
  },
  async performance(id: string): Promise<ThesisPerformance> {
    live()
    return http.get(`${T(id)}/performance`)
  },
  async timeline(id: string): Promise<ThesisTimeline> {
    live()
    return http.get(`${T(id)}/timeline`)
  },
  async evidence(id: string, days = 365): Promise<ThesisEvidence> {
    live()
    return http.get(`${T(id)}/evidence`, { days })
  },
  /** Markdown document (text/markdown). */
  async exportMarkdown(id: string): Promise<string> {
    live()
    return http.get<string>(`${T(id)}/export.md`)
  },
}
