import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import type { ReactElement } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { DashboardPage } from './DashboardPage'
import { makeHealth, stubFetchJson } from '../test/fixtures'

function renderWithQueryClient(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

describe('DashboardPage', () => {
  it('renders the mode banner and health panel once loaded', async () => {
    stubFetchJson(makeHealth())
    renderWithQueryClient(<DashboardPage />)

    await waitFor(() => expect(screen.getByTestId('mode-banner')).toBeInTheDocument())
    expect(screen.getByTestId('check-database')).toBeInTheDocument()
  })

  it('surfaces a live session unmistakably', async () => {
    stubFetchJson(makeHealth({ autonomyMode: 'LIVE_AUTOMATIC', liveTradingEnabled: true }))
    renderWithQueryClient(<DashboardPage />)

    await waitFor(() => expect(screen.getByTestId('live-warning')).toBeInTheDocument())
    expect(screen.getByTestId('mode-banner')).toHaveClass('mode-banner--live')
  })

  it('still renders the report when the backend answers 503', async () => {
    // The health endpoint returns 503 with a full body describing the failure.
    // Treating it as a transport error would hide the diagnosis.
    const unhealthy = makeHealth({
      status: 'UNHEALTHY',
      checks: [
        { name: 'application', status: 'HEALTHY', durationMs: 0, detail: 'process is running' },
        {
          name: 'database',
          status: 'UNHEALTHY',
          durationMs: 12,
          detail: 'connection failed: ConnectionRefusedError',
        },
      ],
    })
    stubFetchJson(unhealthy, 503)
    renderWithQueryClient(<DashboardPage />)

    await waitFor(() => expect(screen.getByTestId('check-database')).toBeInTheDocument())
    expect(screen.getByTestId('blocked-notice')).toBeInTheDocument()
    expect(screen.queryByTestId('error')).not.toBeInTheDocument()
  })

  it('reports an unreachable backend without pretending the system is fine', async () => {
    vi.mocked(globalThis.fetch).mockRejectedValue(new TypeError('Failed to fetch'))
    renderWithQueryClient(<DashboardPage />)

    await waitFor(() => expect(screen.getByTestId('error')).toBeInTheDocument())
    expect(screen.getByRole('alert')).toHaveTextContent('Backend unreachable')
    expect(screen.getByTestId('error')).toHaveTextContent(
      'Absence of an alert here is not evidence that the system is fine',
    )
    expect(screen.queryByTestId('mode-banner')).not.toBeInTheDocument()
  })
})
