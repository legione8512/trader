# State Machines

Six state machines govern the system. Every transition is persisted as an
audit event; no state ever changes silently.

---

## 1. TradingDay

```mermaid
stateDiagram-v2
    [*] --> PENDING: 00:00 Europe/Bucharest
    PENDING --> ACTIVE: preflight checks OK
    PENDING --> TECHNICAL_FAILURE: preflight failed

    ACTIVE --> ACTIVE: session opened / closed
    ACTIVE --> DAILY_TARGET_REACHED: session net >= 2% and <= 4%
    ACTIVE --> DAILY_STOP_REACHED: daily net <= -40 RON<br/>or 3 consecutive losses<br/>or 50 trades executed
    ACTIVE --> TRADING_SUSPENDED: data unhealthy<br/>or exchange unhealthy
    ACTIVE --> MANUALLY_STOPPED: operator emergency stop
    ACTIVE --> TECHNICAL_FAILURE: unrecoverable error
    ACTIVE --> CLOSED: day boundary, no trades (NO_TRADE day)

    TRADING_SUSPENDED --> ACTIVE: health restored
    TRADING_SUSPENDED --> CLOSED: day boundary

    DAILY_TARGET_REACHED --> CLOSED
    DAILY_STOP_REACHED --> CLOSED
    MANUALLY_STOPPED --> CLOSED
    TECHNICAL_FAILURE --> CLOSED

    CLOSED --> [*]
```

`NO_TRADE` is not a state. It is the **outcome** of a day that reaches `CLOSED`
with `tradeCount = 0`, and also the per-scan decision when no opportunity
qualifies.

Any halted state still manages existing positions to exit. Only new entries are
blocked.

---

## 2. TradingSession

```mermaid
stateDiagram-v2
    [*] --> EVALUATING: day ACTIVE and risk engine permits
    EVALUATING --> EVALUATING: no valid opportunity (NO_TRADE)
    EVALUATING --> OPEN: opportunity approved by the risk engine
    EVALUATING --> ABORTED: risk or health condition before any trade

    OPEN --> OPEN: trade closed, session target not reached
    OPEN --> CLOSING: stop condition triggered

    CLOSING --> CLOSED_TARGET_REACHED: 2% <= net <= 4%
    CLOSING --> CLOSED_RESTART_ELIGIBLE: net > 4%
    CLOSING --> CLOSED_STOPPED: loss limit / consecutive losses /<br/>max trades / health / emergency
    CLOSING --> CLOSED_NO_OPPORTUNITY: no further valid opportunity

    CLOSED_RESTART_ELIGIBLE --> [*]: new independent evaluation required
    CLOSED_TARGET_REACHED --> [*]
    CLOSED_STOPPED --> [*]
    CLOSED_NO_OPPORTUNITY --> [*]
    ABORTED --> [*]
```

`CLOSED_RESTART_ELIGIBLE` makes a new session **possible**. It never **causes**
one. A new session starts only when a fresh opportunity independently satisfies
every strategy and risk criterion, and the -40 RON daily floor still applies.

---

## 3. Signal

```mermaid
stateDiagram-v2
    [*] --> GENERATED
    GENERATED --> RISK_REJECTED: risk engine reason codes
    GENERATED --> RISK_APPROVED

    RISK_APPROVED --> EXPIRED: signal age > maxSignalAgeSeconds
    RISK_APPROVED --> AWAITING_OPERATOR: SIGNAL_ONLY mode
    RISK_APPROVED --> ACCEPTED: automatic modes

    AWAITING_OPERATOR --> ACCEPTED: operator approves
    AWAITING_OPERATOR --> OPERATOR_REJECTED: operator rejects
    AWAITING_OPERATOR --> EXPIRED: timeout

    ACCEPTED --> EXECUTED: order submitted
    ACCEPTED --> EXECUTION_FAILED

    EXECUTED --> [*]
    RISK_REJECTED --> [*]
    OPERATOR_REJECTED --> [*]
    EXPIRED --> [*]
    EXECUTION_FAILED --> [*]
```

---

## 4. Order

```mermaid
stateDiagram-v2
    [*] --> INTENT_RECORDED: persisted BEFORE submission
    INTENT_RECORDED --> SUBMITTING

    SUBMITTING --> ACCEPTED: exchange acknowledgement
    SUBMITTING --> REJECTED: exchange rejection
    SUBMITTING --> UNKNOWN: timeout or network error

    UNKNOWN --> RECONCILING: MANDATORY - never a blind retry
    RECONCILING --> ACCEPTED
    RECONCILING --> REJECTED
    RECONCILING --> FILLED
    RECONCILING --> CANCELED
    RECONCILING --> UNRESOLVED: escalate and suspend trading

    ACCEPTED --> PARTIALLY_FILLED
    ACCEPTED --> FILLED
    ACCEPTED --> CANCELED
    ACCEPTED --> EXPIRED

    PARTIALLY_FILLED --> FILLED
    PARTIALLY_FILLED --> CANCELED

    FILLED --> [*]
    CANCELED --> [*]
    EXPIRED --> [*]
    REJECTED --> [*]
    UNRESOLVED --> [*]
```

The `UNKNOWN -> RECONCILING` edge is the most important transition in the
system. It is where most trading bots lose real money: they resubmit an order
that the exchange had in fact already accepted.

`INTENT_RECORDED` exists so that a crash between the decision and the exchange
acknowledgement is always recoverable. The intent carries a locally generated
`clientOrderId`, which makes reconciliation and idempotency possible.

---

## 5. Position

```mermaid
stateDiagram-v2
    [*] --> OPENING
    OPENING --> OPEN: entry filled
    OPENING --> ABANDONED: entry cancelled with zero fill

    OPEN --> CLOSING: stop-loss / take-profit / manual / session end
    OPEN --> DESYNCED: exchange balance mismatch

    CLOSING --> CLOSED

    DESYNCED --> OPEN: reconciled as still open
    DESYNCED --> CLOSED: reconciled as closed

    CLOSED --> [*]
    ABANDONED --> [*]
```

`DESYNCED` blocks all new entries until reconciliation completes.

---

## 6. SystemHealth

```mermaid
stateDiagram-v2
    [*] --> STARTING
    STARTING --> HEALTHY: all checks pass
    STARTING --> UNHEALTHY: a check fails

    HEALTHY --> DEGRADED: stale data / WebSocket reconnect /<br/>rate limit / clock drift
    DEGRADED --> HEALTHY: recovered and validated
    DEGRADED --> UNHEALTHY: threshold exceeded

    UNHEALTHY --> DEGRADED: partial recovery
    UNHEALTHY --> HEALTHY: full recovery
```

**Safety rule:** `DEGRADED` and `UNHEALTHY` block opening new positions.
Management of existing positions, including stop-loss monitoring, continues.
Abandoning an open position is more dangerous than declining a new one.

---

## 7. Day-boundary attribution

Because positions may cross midnight (decision OD-04), attribution must be
unambiguous.

| Item | Attributed to |
|------|---------------|
| `tradeCount` (R-05) | The day the position was **opened** |
| `realisedPnl` | The day the trade was **closed** |
| `consecutiveLosses` | Updated at close, so the closing day |
| Unrealised P&L of a carried position | The **current** day (R-26) |
| Position slot (R-04) | **Occupied** at the start of the new day |

`Trade` therefore carries both `openedTradingDayId` and `closedTradingDayId`.

A new day may legitimately start with its position slot already taken and part
of its risk budget already consumed by mark-to-market. This is correct
behaviour.
