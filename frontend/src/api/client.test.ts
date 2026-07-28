import { describe, expect, it, vi } from 'vitest'

import { ApiError, apiGet } from './client'
import { stubFetchJson } from '../test/fixtures'

describe('apiGet', () => {
  it('returns the parsed body on success', async () => {
    stubFetchJson({ value: 42 })
    await expect(apiGet<{ value: number }>('/api/thing')).resolves.toEqual({ value: 42 })
  })

  it('throws ApiError on an unexpected status', async () => {
    stubFetchJson({ detail: 'nope' }, 500)
    await expect(apiGet('/api/thing')).rejects.toBeInstanceOf(ApiError)
  })

  it('returns the body for a status the caller explicitly allows', async () => {
    stubFetchJson({ status: 'UNHEALTHY' }, 503)
    await expect(apiGet('/api/health', { allowedStatuses: [503] })).resolves.toEqual({
      status: 'UNHEALTHY',
    })
  })

  it('marks a transport failure as a network failure', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(new TypeError('Failed to fetch'))

    const error = await apiGet('/api/health').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).isNetworkFailure).toBe(true)
    expect((error as ApiError).status).toBe(0)
  })

  it('records the path on the error for diagnostics', async () => {
    stubFetchJson({}, 404)
    const error = await apiGet('/api/missing').catch((caught: unknown) => caught)
    expect((error as ApiError).path).toBe('/api/missing')
  })
})
