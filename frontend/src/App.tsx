import { Route, Routes } from 'react-router'

import { DashboardPage } from './pages/DashboardPage'

export function App() {
  return (
    <Routes>
      <Route path="/" element={<DashboardPage />} />
      {/* Later phases add /signals, /orders, /backtests, /audit. */}
      <Route path="*" element={<DashboardPage />} />
    </Routes>
  )
}
