/**
 * Types mirroring the backend health DTO.
 *
 * The backend serialises camelCase (see app/api/base.py), so these names match
 * the wire format exactly. They are hand-written for now; generating them from
 * the OpenAPI schema becomes worthwhile once there are many endpoints.
 */

export const HEALTH_STATUSES = ['STARTING', 'HEALTHY', 'DEGRADED', 'UNHEALTHY'] as const
export type HealthStatus = (typeof HEALTH_STATUSES)[number]

export const AUTONOMY_MODES = ['SIGNAL_ONLY', 'PAPER_AUTOMATIC', 'LIVE_AUTOMATIC'] as const
export type AutonomyMode = (typeof AUTONOMY_MODES)[number]

export interface HealthCheck {
  name: string
  status: HealthStatus
  durationMs: number
  detail: string | null
}

export interface HealthResponse {
  status: HealthStatus
  timestamp: string
  version: string
  environment: string
  autonomyMode: AutonomyMode
  liveTradingEnabled: boolean
  checks: HealthCheck[]
}

/**
 * Whether a status forbids opening new positions.
 *
 * Mirrors HealthStatus.blocks_new_positions in the backend. This is a *display*
 * helper only: the rule itself is enforced server-side by the risk engine. The
 * frontend must never be the only place a risk rule lives.
 */
export function blocksNewPositions(status: HealthStatus): boolean {
  return status !== 'HEALTHY'
}
