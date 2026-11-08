import { ApiError } from './errors'

export const USE_MOCK: boolean = (import.meta.env.VITE_USE_MOCK ?? 'true').toString().toLowerCase() !== 'false'
const RAW_API_URL: string = (import.meta.env.VITE_API_URL ?? 'http://localhost:8000').toString().trim()
/**
 * VITE_API_URL = "same-origin" (or empty / "/") -> the page's own origin. Used by the app-mode build
 * (`npm run build:app`, frontend/.env.app): FastAPI serves the built UI and /api/v1 from one origin (no CORS).
 */
export const API_BASE_URL: string = (
  RAW_API_URL === '' || RAW_API_URL === '/' || RAW_API_URL.toLowerCase() === 'same-origin'
    ? (globalThis.location?.origin ?? 'http://localhost:8000')
    : RAW_API_URL
).replace(/\/$/, '')
export const API_PREFIX = '/api/v1'

export type Query = Record<string, string | number | boolean | null | undefined | readonly string[]>

function buildUrl(path: string, query?: Query): URL {
  const full = path.startsWith('/health') ? `${API_BASE_URL}${path}` : `${API_BASE_URL}${API_PREFIX}${path}`
  const url = new URL(full)
  for (const [k, v] of Object.entries(query ?? {})) {
    if (Array.isArray(v)) for (const x of v) url.searchParams.append(k, String(x))
    else if (v !== null && v !== undefined && v !== '') url.searchParams.set(k, String(v))
  }
  return url
}

/**
 * Capability discovery: FastAPI publishes implemented operations in /openapi.json while contract stubs
 * (501) are hidden from it. Knowing this up-front lets the client answer "pipeline pending" locally
 * instead of firing requests the browser would log as failed resources. If /openapi.json is unavailable
 * every request simply goes to the network.
 */
interface Op {
  method: string
  re: RegExp
}
interface SpecParam {
  name: string
  in: string
  schema?: { enum?: unknown[] }
}
type Spec = { paths?: Record<string, Record<string, { parameters?: SpecParam[] }>> }
let specPromise: Promise<Spec | null> | null = null
function openapiSpec(): Promise<Spec | null> {
  specPromise ??= fetch(`${API_BASE_URL}/openapi.json`, { headers: { Accept: 'application/json' } })
    .then((r) => (r.ok ? (r.json() as Promise<Spec>) : null))
    .catch(() => null)
  return specPromise
}
let opsPromise: Promise<Op[] | null> | null = null
function implementedOps(): Promise<Op[] | null> {
  opsPromise ??= openapiSpec()
    .then((spec) => {
      if (!spec?.paths) return null
      const ops: Op[] = []
      for (const [p, methods] of Object.entries(spec.paths)) {
        const re = new RegExp(`^${p.replace(/[.*+?^$()|[\]\\]/g, '\\$&').replace(/\{[^}]+\}/g, '[^/]+')}$`)
        for (const m of Object.keys(methods)) ops.push({ method: m.toUpperCase(), re })
      }
      return ops
    })
    .catch(() => null)
  return opsPromise
}

async function request<T>(method: string, path: string, opts: { query?: Query; body?: unknown } = {}): Promise<T> {
  const url = buildUrl(path, opts.query)
  const ops = await implementedOps()
  if (ops && !ops.some((o) => o.method === method && o.re.test(url.pathname))) {
    throw new ApiError(501, url.toString(), `operation ${method} ${url.pathname} is defined in contracts/api.yaml but not implemented yet`, 'Not implemented')
  }
  let res: Response
  try {
    res = await fetch(url, {
      method,
      headers: { Accept: 'application/json', ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}) },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    })
  } catch (err) {
    throw new ApiError(0, url.toString(), `Network error — is the API running at ${API_BASE_URL}? (${(err as Error).message})`)
  }
  if (!res.ok) {
    // contract errors are application/problem+json {title, status, detail}
    let title = res.statusText
    let detail = ''
    let type = ''
    try {
      const body = (await res.json()) as { title?: string; detail?: unknown; type?: string; errors?: { loc?: string[]; msg?: string }[] | null }
      if (body?.title) title = body.title
      if (body?.type) type = body.type
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
      else if (body?.errors?.length) detail = body.errors.map((e) => `${(e.loc ?? []).slice(1).join('.')}: ${e.msg ?? ''}`).join('; ')
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, url.toString(), detail || title, title, type)
  }
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') ?? ''
  return (ct.includes('json') ? await res.json() : await res.text()) as T
}

export const http = {
  get: <T>(path: string, query?: Query) => request<T>('GET', path, { query }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>('POST', path, { body: body ?? {}, query }),
  patch: <T>(path: string, body: unknown) => request<T>('PATCH', path, { body }),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, { body }),
  del: (path: string) => request<void>('DELETE', path),
  /** DELETE that returns a JSON body (e.g. cancelled run status) */
  delete: <T>(path: string) => request<T>('DELETE', path),
}

/**
 * Enum of a query parameter as published by the running API's /openapi.json (null = unknown / spec unavailable).
 * Lets the client avoid sending values an older backend would reject with 422 (e.g. a newly added regime scope).
 */
export async function queryParamEnum(method: string, path: string, name: string): Promise<string[] | null> {
  const spec = await openapiSpec()
  const op = spec?.paths?.[`${API_PREFIX}${path}`]?.[method.toLowerCase()]
  const p = op?.parameters?.find((x) => x.name === name && x.in === 'query')
  const e = p?.schema?.enum
  return Array.isArray(e) ? e.map(String) : null
}

/** Resolve the first fulfilled value or null (optional enrichment calls must never fail the page). */
export async function optional<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p
  } catch {
    return null
  }
}
