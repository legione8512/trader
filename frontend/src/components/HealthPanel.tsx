import { StatusBadge } from './StatusBadge'
import { blocksNewPositions, type HealthResponse } from '../types/health'

interface HealthPanelProps {
  health: HealthResponse
}

export function HealthPanel({ health }: HealthPanelProps) {
  const blocked = blocksNewPositions(health.status)

  return (
    <section className="panel" aria-labelledby="health-heading">
      <header className="panel__header">
        <h2 id="health-heading">System health</h2>
        <StatusBadge status={health.status} />
      </header>

      {blocked && (
        <p className="panel__notice" data-testid="blocked-notice">
          New positions are blocked. Existing positions are still managed to exit.
        </p>
      )}

      <table className="checks">
        <caption className="visually-hidden">Individual health checks</caption>
        <thead>
          <tr>
            <th scope="col">Check</th>
            <th scope="col">Status</th>
            <th scope="col">Duration</th>
            <th scope="col">Detail</th>
          </tr>
        </thead>
        <tbody>
          {health.checks.map((check) => (
            <tr key={check.name} data-testid={`check-${check.name}`}>
              <td className="checks__name">{check.name}</td>
              <td>
                <StatusBadge status={check.status} />
              </td>
              <td className="checks__duration">{check.durationMs.toFixed(1)} ms</td>
              <td className="checks__detail">{check.detail ?? '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <dl className="meta">
        <div>
          <dt>Version</dt>
          <dd>{health.version}</dd>
        </div>
        <div>
          <dt>Environment</dt>
          <dd>{health.environment}</dd>
        </div>
        <div>
          <dt>Last checked</dt>
          <dd>
            <time dateTime={health.timestamp}>
              {new Date(health.timestamp).toLocaleTimeString()}
            </time>
          </dd>
        </div>
      </dl>
    </section>
  )
}
