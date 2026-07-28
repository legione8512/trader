import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import { apiGet } from './client'
import type { HealthResponse } from '../types/health'

export const HEALTH_QUERY_KEY = ['health'] as const

/** How often the dashboard re-checks system health, in milliseconds. */
export const HEALTH_POLL_INTERVAL_MS = 5_000

export function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  // 503 carries the unhealthy report itself, so it is an expected response.
  return apiGet<HealthResponse>('/api/health', { signal, allowedStatuses: [503] })
}

export function useHealth(): UseQueryResult<HealthResponse, Error> {
  return useQuery({
    queryKey: HEALTH_QUERY_KEY,
    queryFn: ({ signal }) => fetchHealth(signal),
    refetchInterval: HEALTH_POLL_INTERVAL_MS,
    // Keep polling while the tab is hidden: an operator watching an open
    // position needs the alert when they come back, not a stale snapshot.
    refetchIntervalInBackground: true,
    // No retry. The poll interval IS the retry, and it is only 5 seconds away.
    // Retrying silently would delay showing a backend outage to an operator who
    // may have a position open - exactly the moment the truth matters most.
    retry: false,
  })
}
