import { useHealth } from '../api/health'
import { HealthPanel } from '../components/HealthPanel'
import { ModeBanner } from '../components/ModeBanner'

export function DashboardPage() {
  const { data, isPending, isError, error } = useHealth()

  return (
    <main className="dashboard">
      <h1 className="dashboard__title">Trader</h1>

      {isPending && (
        <p className="dashboard__message" data-testid="loading">
          Contacting backend…
        </p>
      )}

      {isError && (
        <section className="panel panel--error" data-testid="error" role="alert">
          <h2>Backend unreachable</h2>
          <p>{error.message}</p>
          <p className="panel__notice">
            While the backend cannot be reached, this page shows nothing about the trading
            system. Absence of an alert here is not evidence that the system is fine.
          </p>
        </section>
      )}

      {data && (
        <>
          <ModeBanner mode={data.autonomyMode} liveTradingEnabled={data.liveTradingEnabled} />
          <HealthPanel health={data} />
        </>
      )}
    </main>
  )
}
