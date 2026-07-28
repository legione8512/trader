/**
 * HTTP client for the backend API.
 *
 * The frontend talks ONLY to this backend. It never calls an exchange directly
 * and never holds an API credential: exchange requests are signed server-side.
 */

/** Base URL. Empty in development, where Vite proxies /api to the backend. */
const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  readonly status: number
  readonly path: string

  constructor(message: string, status: number, path: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'ApiError'
    this.status = status
    this.path = path
  }

  /** True when the request never reached the backend at all. */
  get isNetworkFailure(): boolean {
    return this.status === 0
  }
}

interface GetOptions {
  signal?: AbortSignal
  /**
   * Non-2xx statuses to treat as successful responses.
   *
   * The health endpoint answers 503 when the system is unhealthy, and that body
   * is exactly what the dashboard needs to display. Treating it as an error
   * would hide the diagnosis at the moment it matters most.
   */
  allowedStatuses?: readonly number[]
}

export async function apiGet<T>(path: string, options: GetOptions = {}): Promise<T> {
  const { signal, allowedStatuses = [] } = options

  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      method: 'GET',
      headers: { Accept: 'application/json' },
      signal,
    })
  } catch (cause) {
    // Network-level failure: the backend is unreachable, not merely unhappy.
    throw new ApiError('Backend unreachable', 0, path, { cause })
  }

  if (!response.ok && !allowedStatuses.includes(response.status)) {
    throw new ApiError(`Request failed with status ${response.status}`, response.status, path)
  }

  return (await response.json()) as T
}
