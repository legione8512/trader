import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { ModeBanner } from './ModeBanner'

describe('ModeBanner', () => {
  it('shows the safe variant for SIGNAL_ONLY', () => {
    render(<ModeBanner mode="SIGNAL_ONLY" liveTradingEnabled={false} />)

    const banner = screen.getByTestId('mode-banner')
    expect(banner).toHaveClass('mode-banner--safe')
    expect(banner).toHaveTextContent('SIGNAL ONLY')
    expect(banner).toHaveTextContent('No order is ever submitted')
  })

  it('shows the safe variant for PAPER_AUTOMATIC', () => {
    render(<ModeBanner mode="PAPER_AUTOMATIC" liveTradingEnabled={false} />)

    const banner = screen.getByTestId('mode-banner')
    expect(banner).toHaveClass('mode-banner--safe')
    expect(banner).toHaveTextContent('PAPER TRADING')
    expect(banner).toHaveTextContent('No real money is at risk')
  })

  it('makes live mode visually unmistakable', () => {
    render(<ModeBanner mode="LIVE_AUTOMATIC" liveTradingEnabled />)

    const banner = screen.getByTestId('mode-banner')
    expect(banner).toHaveClass('mode-banner--live')
    expect(banner).toHaveTextContent('LIVE TRADING')
    expect(banner).toHaveTextContent('REAL MONEY')
    expect(screen.getByTestId('live-warning')).toHaveTextContent('LIVE ORDERS ARMED')
  })

  it('never renders the live styling for a non-live mode', () => {
    for (const mode of ['SIGNAL_ONLY', 'PAPER_AUTOMATIC'] as const) {
      const { unmount } = render(<ModeBanner mode={mode} liveTradingEnabled />)
      expect(screen.getByTestId('mode-banner')).not.toHaveClass('mode-banner--live')
      expect(screen.queryByTestId('live-warning')).not.toBeInTheDocument()
      unmount()
    }
  })

  it('is announced to assistive technology', () => {
    render(<ModeBanner mode="SIGNAL_ONLY" liveTradingEnabled={false} />)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })
})
