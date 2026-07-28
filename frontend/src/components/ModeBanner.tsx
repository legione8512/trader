import type { AutonomyMode } from '../types/health'

interface ModeBannerProps {
  mode: AutonomyMode
  liveTradingEnabled: boolean
}

const MODE_LABELS: Record<AutonomyMode, string> = {
  SIGNAL_ONLY: 'SIGNAL ONLY',
  PAPER_AUTOMATIC: 'PAPER TRADING',
  LIVE_AUTOMATIC: 'LIVE TRADING',
}

const MODE_DESCRIPTIONS: Record<AutonomyMode, string> = {
  SIGNAL_ONLY: 'Proposals only. No order is ever submitted.',
  PAPER_AUTOMATIC: 'Simulated execution. No real money is at risk.',
  LIVE_AUTOMATIC: 'REAL MONEY. Real orders are submitted to the exchange.',
}

/**
 * Permanent, unmissable indicator of the active autonomy mode.
 *
 * The live variant is deliberately loud. An operator must never be able to
 * mistake a live session for a paper one, and the cost of an over-obvious
 * banner is nothing compared to the cost of that mistake.
 *
 * This is a display element only. Mode enforcement lives server-side.
 */
export function ModeBanner({ mode, liveTradingEnabled }: ModeBannerProps) {
  const isLive = mode === 'LIVE_AUTOMATIC'

  return (
    <div
      className={`mode-banner mode-banner--${isLive ? 'live' : 'safe'}`}
      role="status"
      aria-live="polite"
      data-testid="mode-banner"
      data-mode={mode}
    >
      <span className="mode-banner__label">{MODE_LABELS[mode]}</span>
      <span className="mode-banner__description">{MODE_DESCRIPTIONS[mode]}</span>
      {isLive && liveTradingEnabled && (
        <span className="mode-banner__warning" data-testid="live-warning">
          ⚠ LIVE ORDERS ARMED
        </span>
      )}
    </div>
  )
}
