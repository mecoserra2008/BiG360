import { ApiError } from '../errors'
import { API_BASE_URL, API_PREFIX, USE_MOCK, http } from '../http'
import type { BqlExplain, BqlFieldPage, BqlResolve, BqlResult, BqlSaved, BqlSavedInput } from './types'

/** BQL needs the live API (the query engine runs on the serving snapshot): no mock implementation. */
function live(): void {
  if (USE_MOCK) throw new ApiError(501, `${API_BASE_URL}${API_PREFIX}/bql`, 'the BQL console needs the live API (set VITE_USE_MOCK=false)', 'Not implemented')
}

export const bqlApi = {
  async run(query: string, maxRows = 5000): Promise<BqlResult> {
    live()
    return http.post('/bql', { query, max_rows: maxRows })
  },
  async explain(query: string): Promise<BqlExplain> {
    live()
    return http.post('/bql/explain', { query })
  },
  async fields(q: string, group?: string, limit = 50): Promise<BqlFieldPage> {
    live()
    return http.get('/bql/fields', { q: q.trim() || undefined, group: group || undefined, limit })
  },
  async resolve(q: string): Promise<BqlResolve> {
    live()
    return http.get('/bql/resolve', { q })
  },
  async saved(): Promise<BqlSaved[]> {
    live()
    return http.get('/bql/saved')
  },
  async save(body: BqlSavedInput): Promise<BqlSaved> {
    live()
    return http.post('/bql/saved', body)
  },
  async update(id: string, body: Partial<BqlSavedInput>): Promise<BqlSaved> {
    live()
    return http.put(`/bql/saved/${encodeURIComponent(id)}`, body)
  },
  async remove(id: string): Promise<void> {
    live()
    return http.del(`/bql/saved/${encodeURIComponent(id)}`)
  },
  /** Export as a file (binary-safe: XLSX); returns the blob and the server's file name. */
  async export(query: string, format: 'csv' | 'xlsx' | 'json'): Promise<{ blob: Blob; filename: string }> {
    live()
    const url = `${API_BASE_URL}${API_PREFIX}/bql/export`
    const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ query, format }) })
    if (!res.ok) {
      let detail = res.statusText
      try {
        const b = (await res.json()) as { detail?: string; title?: string }
        detail = b.detail || b.title || detail
      } catch {
        /* non-JSON body */
      }
      throw new ApiError(res.status, url, detail, res.statusText)
    }
    const cd = res.headers.get('content-disposition') ?? ''
    const m = /filename="([^"]+)"/.exec(cd)
    return { blob: await res.blob(), filename: m?.[1] ?? `bql.${format}` }
  },
}
