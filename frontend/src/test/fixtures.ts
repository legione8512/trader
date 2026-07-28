import type { HealthResponse } from '../types/health'

export function makeHealth(overrides: Partial<HealthResponse> = {}): HealthResponse {
  return {
    status: 'HEALTHY',
    timestamp: '2026-07-28T21:18:57.138974Z',
    version: '0.1.0',
    environment: 'test',
    autonomyMode: 'SIGNAL_ONLY',
    liveTradingEnabled: false,
    checks: [
      { name: 'application', status: 'HEALTHY', durationMs: 0.0, detail: 'process is running' },
      { name: 'configuration', status: 'HEALTHY', durationMs: 0.016, detail: 'mode=SIGNAL_ONLY' },
      { name: 'database', status: 'HEALTHY', durationMs: 3.078, detail: 'connection ok' },
    ],
    ...overrides,
  }
}

/** Stub the global fetch with a JSON response. */
export function stubFetchJson(body: unknown, status = 200): void {
  const fetchMock = globalThis.fetch as unknown as {
    mockResolvedValue: (value: Response) => void
  }
  fetchMock.mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response)
}
