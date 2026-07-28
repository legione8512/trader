import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { HealthPanel } from './HealthPanel'
import { makeHealth } from '../test/fixtures'

describe('HealthPanel', () => {
  it('renders one row per check', () => {
    render(<HealthPanel health={makeHealth()} />)

    expect(screen.getByTestId('check-application')).toBeInTheDocument()
    expect(screen.getByTestId('check-configuration')).toBeInTheDocument()
    expect(screen.getByTestId('check-database')).toBeInTheDocument()
  })

  it('does not warn about blocked positions while healthy', () => {
    render(<HealthPanel health={makeHealth()} />)
    expect(screen.queryByTestId('blocked-notice')).not.toBeInTheDocument()
  })

  it('warns that new positions are blocked when degraded', () => {
    render(<HealthPanel health={makeHealth({ status: 'DEGRADED' })} />)

    const notice = screen.getByTestId('blocked-notice')
    expect(notice).toHaveTextContent('New positions are blocked')
    // Existing positions must still be managed; abandoning one is worse.
    expect(notice).toHaveTextContent('still managed to exit')
  })

  it('warns that new positions are blocked when unhealthy', () => {
    render(<HealthPanel health={makeHealth({ status: 'UNHEALTHY' })} />)
    expect(screen.getByTestId('blocked-notice')).toBeInTheDocument()
  })

  it('shows a placeholder when a check has no detail', () => {
    const health = makeHealth({
      checks: [{ name: 'exchange', status: 'STARTING', durationMs: 0, detail: null }],
    })
    render(<HealthPanel health={health} />)
    expect(screen.getByTestId('check-exchange')).toHaveTextContent('—')
  })

  it('shows version and environment', () => {
    render(<HealthPanel health={makeHealth({ version: '9.9.9', environment: 'production' })} />)
    expect(screen.getByText('9.9.9')).toBeInTheDocument()
    expect(screen.getByText('production')).toBeInTheDocument()
  })
})
