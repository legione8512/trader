# Acceptance Criteria

Every criterion must be provable by an automated test. A criterion without a
test is not a criterion — it is a hope.

Status legend: `PENDING` = not implemented yet, `PASS` = covered by a passing
test, `WITHDRAWN` = removed by an explicit Phase 0 decision.

---

## Safety and risk

| ID | Criterion | Verification | Status |
|----|-----------|--------------|--------|
| AC-01 | `NO_TRADE` is a valid outcome over any horizon | A full day with no qualifying signal ends `CLOSED` with `tradeCount = 0` and no error | PENDING |
| AC-02 | The 2% target never forces a trade | At 23:50 with 0 profit and a below-threshold signal, the result is `NO_TRADE` | PENDING |
| AC-03 | Reference capital stays 1,000 RON | Property test: after any sequence of profitable trades, `referenceCapitalRon == 1000.00` | PENDING |
| AC-04 | The daily limit spans all sessions | Two sessions totalling -40 RON produce `DAILY_STOP_REACHED` | PENDING |
| AC-05 | A restart never resets the daily loss allowance | After a +45 RON session and a restart, the day floor is still -40 RON absolute | PENDING |
| AC-06 | Risk per trade stays at or below 5.00 RON after rounding | Property test over randomised exchange filters | PENDING |
| AC-07 | A fourth consecutive loss is impossible | After 3 consecutive losses, any signal is rejected with `MAX_CONSECUTIVE_LOSSES_REACHED` | PENDING |
| AC-08 | A 51st daily trade is impossible with the initial configuration | After 50 trades, rejection with `MAX_TRADES_PER_DAY_REACHED` | PENDING |
| AC-09 | Only one position may be open | A second signal while a position is open is rejected with `MAX_OPEN_POSITIONS_REACHED` | PENDING |
| AC-10 | Stale data blocks trading | A candle older than the threshold produces `STALE_MARKET_DATA` | PENDING |
| AC-16 | Emergency stop blocks new trades | With the flag active, every signal is rejected | PENDING |
| AC-21 | The daily limit uses realised **plus** unrealised P&L | An open position at -41 RON unrealised halts the day immediately, without waiting for it to close | PENDING |

---

## Mode isolation

| ID | Criterion | Verification | Status |
|----|-----------|--------------|--------|
| AC-11 | `PAPER_AUTOMATIC` cannot reach live code | Architecture test plus a runtime test asserting the live adapter is never instantiated | PENDING |
| AC-12 | `SIGNAL_ONLY` submits no order | With a mock exchange, zero submit calls are made | PENDING |
| AC-13 | Live mode is disabled by default | An empty configuration makes `LIVE_AUTOMATIC` unavailable | PENDING |
| AC-24 | Live mode needs all four guards | Any three of the four conditions leave live mode unavailable | PENDING |

---

## Security

| ID | Criterion | Verification | Status |
|----|-----------|--------------|--------|
| AC-14 | Secrets never appear in API responses or logs | Automated scan of all API responses and captured log output | PENDING |
| AC-25 | `.env` is never tracked by Git | CI check that `.env` is absent from the index and from history | PENDING |

---

## Correctness and auditability

| ID | Criterion | Verification | Status |
|----|-----------|--------------|--------|
| AC-15 | An uncertain order triggers reconciliation, not a retry | On a simulated timeout the adapter receives `getOrder`, never a second `submit` | PENDING |
| AC-17 | Money is `Decimal` / `NUMERIC` only | Architecture test: no `float` on the monetary path; every monetary column is `NUMERIC` | PENDING |
| AC-18 | Every trade is reconstructable | From a `Trade`, the signal, strategy inputs, risk assessment, order intent, exchange response and fills are all retrievable | PENDING |
| AC-19 | Strategies cannot bypass the risk engine | Architecture test on the import graph | PENDING |
| AC-20 | Backtests are reproducible | Same period, same strategy version, same seed produce bit-identical results | PENDING |
| AC-22 | FX rates are historically stable | A past day's RON report uses the `FxRateSnapshot` of that day, not the current rate | PENDING |
| AC-26 | Day-boundary attribution is correct | A position opened at 23:40 and closed at 01:15 counts its trade on the opening day and its realised P&L on the closing day | PENDING |

---

## Withdrawn

| ID | Criterion | Reason |
|----|-----------|--------|
| AC-23 | Daily fee budget blocks entries but not exits | Rule R-25 was rejected by the operator in Phase 0 | WITHDRAWN |

---

## Phase 1 exit criteria

| # | Criterion |
|---|-----------|
| 1 | `.gitignore` is present in the very first commit and ignores `.env` |
| 2 | `docs/` contains the SRS, risk rules, acceptance criteria, state machines, architecture and ADR-0001 |
| 3 | `GET /api/health` reports application and database status |
| 4 | Configuration is validated by Pydantic and live mode cannot be enabled by a single variable |
| 5 | PostgreSQL and Alembic run, with a baseline migration applied |
| 6 | The frontend renders the health status |
| 7 | `docker compose up --build` brings the whole stack up |
| 8 | `ruff check`, `mypy`, `pytest`, `npm run lint` and `tsc --noEmit` all pass |
| 9 | GitHub Actions runs lint, type checks and tests for backend and frontend |
| 10 | `.env.example` contains placeholders only |
