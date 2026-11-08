/** HTTP error carrying the contract's Problem payload (application/problem+json). */
export class ApiError extends Error {
  readonly status: number
  readonly url: string
  readonly title: string
  /** Problem.type, e.g. "urn:studio:look_ahead" for Model Studio guardrails */
  readonly type: string
  constructor(status: number, url: string, message: string, title = '', type = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.url = url
    this.title = title
    this.type = type
  }
}

/** 501 = operation defined in contracts/api.yaml but not implemented by the backend yet. */
export function isPendingError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 501
}

export function isNetworkError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 0
}

/** Errors that will not fix themselves on retry. */
export function isPermanentError(err: unknown): boolean {
  return err instanceof ApiError && [400, 401, 403, 404, 405, 409, 422, 501].includes(err.status)
}
