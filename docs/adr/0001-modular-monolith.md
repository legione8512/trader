# ADR-0001 — Modular monolith instead of microservices

- **Status:** Accepted
- **Date:** 2026-07-28
- **Phase:** 0

---

## Context

The system has clearly separable concerns: market data collection, strategy
evaluation, risk assessment, order execution, portfolio accounting,
backtesting, reporting and auditing. That separation invites a microservice
decomposition.

The actual operating profile, however, is:

- one operator;
- two instruments (BTC, ETH);
- a 15-minute primary timeframe;
- at most one open position;
- a fixed reference capital of 1,000 RON;
- a single deployment target.

The dominant risks in this project are **correctness and safety** — a wrong
order, a duplicated order, a bypassed risk rule, an inconsistent P&L — not
throughput or independent team scaling.

---

## Decision

Build a **modular monolith**: one deployable backend process with strictly
enforced internal module boundaries.

Boundaries are enforced by an automated architecture test over the import
graph, not by convention. The critical invariant is that no path from
`strategies/` reaches `execution/` without passing through `risk/`.

---

## Rationale

**Correctness is easier in one process.** A trading decision spans strategy,
risk, execution and portfolio state. In one process this is a single database
transaction. Split across services it becomes a distributed transaction, and
the failure modes multiply exactly where money is at stake.

**Reconciliation is already the hard problem.** Reconciling local state against
the exchange is unavoidable and difficult. Adding reconciliation *between
internal services* would multiply that difficulty for no benefit at this scale.

**Auditability.** "Every trade must be fully reconstructable" is far simpler
when every event is written to one database with one clock and one ordering.

**Backtesting reuses production code.** The backtester must run the same
strategy and the same risk engine as live trading. With in-process modules this
is an import. With microservices it becomes either a network call in a
backtest loop — unusably slow — or a duplicated implementation, which
guarantees divergence between what is tested and what trades.

**Microservices would not solve any problem we actually have.** There is no
scaling bottleneck, no independent deployment need, and no team-boundary
pressure.

---

## Consequences

**Positive**

- One transaction boundary around a trading decision.
- One database, one clock, one event ordering for the audit trail.
- Backtest, paper and live share the same code, not a copy of it.
- Dramatically simpler local development and deployment.
- Failure modes are fewer and easier to enumerate and test.

**Negative**

- Module boundaries can erode without discipline. Mitigated by the automated
  architecture test.
- The whole process restarts together. Acceptable: the safe default on restart
  is to stop opening new positions and reconcile before resuming.
- Horizontal scaling would require refactoring. Accepted; there is no
  foreseeable need at this scale.

**Neutral**

- If a genuine isolation requirement appears later — a heavy machine-learning
  research module, or a market data collector needing its own uptime profile —
  it can be extracted. Clean module boundaries make extraction a refactor
  rather than a rewrite. That is precisely why the boundaries are enforced from
  day one.

---

## Alternatives considered

| Alternative | Why rejected |
|-------------|--------------|
| Microservices per bounded context | Distributed transactions and cross-service reconciliation on the money path, for no scaling benefit |
| Separate market-data service | Real but premature. Can be extracted later if uptime profiles genuinely diverge |
| Serverless functions | Incompatible with long-lived WebSocket market data streams and in-memory strategy state |
| Unstructured single module | Would allow strategies to reach execution directly, voiding every risk guarantee |
