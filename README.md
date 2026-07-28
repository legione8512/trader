# Trader

An automated cryptocurrency **spot** trading application: it collects market data,
evaluates opportunities against a deterministic strategy, scores them, applies
strict risk rules, and — only when every mandatory condition is satisfied —
executes spot orders on Binance.

---

## Important disclaimer

This software **does not guarantee profit**. The 2% session objective is a
*stopping rule*, not a promise and not a target the system is allowed to chase.

A day with zero trades and zero profit is a **valid and expected outcome**. The
application is explicitly built to refuse to trade when the market does not
offer a sufficiently strong opportunity. `NO_TRADE` is a normal result, not an
error and not a defect.

Trading cryptocurrency carries real risk of losing real money. Use at your own
risk.

---

## Core principles

| # | Principle |
|---|-----------|
| 1 | **Not trading is a correct outcome.** Quality thresholds are never lowered to produce activity. |
| 2 | **The risk engine is authoritative.** Strategies propose; the risk engine approves or rejects, deterministically, with machine-readable reason codes. |
| 3 | **Fixed capital.** All percentages are computed against a fixed 1,000 RON reference. Profit never increases position size. No compounding. |
| 4 | **Live trading is disabled by default** and cannot be enabled by changing a single environment variable. |
| 5 | **Everything is auditable.** Every trade must be fully reconstructable after the fact from the database. |
| 6 | **Money is `Decimal` / `NUMERIC`, never `float`.** |
| 7 | **Uncertain order state triggers reconciliation, never a blind retry.** |

---

## Documentation

| Document | Contents |
|----------|----------|
| [docs/SRS.md](docs/SRS.md) | Software requirements specification |
| [docs/RISK_RULES.md](docs/RISK_RULES.md) | Every risk rule, parameter and reason code |
| [docs/ACCEPTANCE_CRITERIA.md](docs/ACCEPTANCE_CRITERIA.md) | Verifiable acceptance criteria |
| [docs/STATE_MACHINES.md](docs/STATE_MACHINES.md) | State machines for day, session, signal, order, position, health |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System architecture and module boundaries |
| [docs/adr/](docs/adr/) | Architecture Decision Records |

---

## Status

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Requirements finalisation | Done |
| 1 | Repository and local environment | In progress |
| 2 | Domain model and persistence | Not started |
| 3 | Public Binance market data | Not started |
| 4 | Strategy research framework | Not started |
| 5 | Risk engine | Not started |
| 6 | Backtesting | Not started |
| 7 | Signal-only mode | Not started |
| 8 | Automatic paper trading | Not started |
| 9 | React dashboard | Not started |
| 10 | Forward testing and shadow mode | Not started |
| 11 | Live Binance integration | Not started |
| 12 | Deployment | Not started |
| 13 | Validation and production-readiness review | Not started |

---

## Technology

**Backend** — Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 (async),
Alembic, PostgreSQL, APScheduler, pytest, Ruff, mypy.

**Frontend** — React, TypeScript, Vite, React Router, TanStack Query.

**Infrastructure** — Docker, Docker Compose, GitHub Actions.

Architecture: **modular monolith**. See
[ADR-0001](docs/adr/0001-modular-monolith.md).

---

## Getting started

### 1. Configuration

```bash
cp .env.example .env
```

Then edit `.env` and set a real `POSTGRES_PASSWORD`. The same password must
appear inside `DATABASE_URL`. `.env` is git-ignored — keep it that way.

### 2. Run the whole stack in Docker

```bash
docker compose up --build
```

| Service | URL |
|---------|-----|
| Health endpoint | http://localhost:8000/api/health |
| Interactive API docs | http://localhost:8000/docs |
| PostgreSQL | localhost:5432 |

### 3. Apply database migrations

Migrations are **not** run automatically at startup. In an application that
moves money, a schema change must be an explicit operator action, never a side
effect of a restart.

```bash
docker compose run --rm backend alembic upgrade head
```

### 4. Run the backend without Docker

Requires a PostgreSQL reachable at the `DATABASE_URL` in `.env`.

```bash
cd backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
```

```bash
cd backend && .venv/Scripts/python.exe -m uvicorn app.main:create_app --factory --reload
```

### 5. Checks

```bash
cd backend && .venv/Scripts/python.exe -m pytest
```

```bash
cd backend && .venv/Scripts/python.exe -m ruff check . && .venv/Scripts/python.exe -m mypy
```

### Useful Docker commands

```bash
docker compose down
```

```bash
docker compose down -v
```

The second one also destroys the database volume.

---

## Security

- Never commit `.env`. It is git-ignored; keep it that way.
- Never paste API keys into chat, issues, screenshots or logs.
- No exchange credentials are required before Phase 11. Public market data is
  unauthenticated.
- API keys must have **spot trading + read** permission only. Withdrawal,
  futures and margin permissions are forbidden.
- Exchange requests are signed **only in the backend**. The frontend never
  talks to the exchange.
