import type { HealthStatus } from '../types/health'

interface StatusBadgeProps {
  status: HealthStatus
  label?: string
}

const STATUS_SYMBOLS: Record<HealthStatus, string> = {
  HEALTHY: '●',
  DEGRADED: '▲',
  UNHEALTHY: '■',
  STARTING: '◌',
}

/**
 * Status indicator that does not rely on colour alone.
 *
 * Each status carries a distinct shape and its text label, so the badge stays
 * readable for colour-blind operators and in a greyscale screenshot.
 */
export function StatusBadge({ status, label }: StatusBadgeProps) {
  return (
    <span
      className={`status-badge status-badge--${status.toLowerCase()}`}
      data-testid="status-badge"
      data-status={status}
    >
      <span aria-hidden="true" className="status-badge__symbol">
        {STATUS_SYMBOLS[status]}
      </span>
      <span className="status-badge__text">{label ?? status}</span>
    </span>
  )
}
