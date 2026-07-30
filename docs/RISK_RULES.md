# Risk Rules

**Version:** 1 (Phase 0 baseline)

The risk engine is **authoritative**. Strategies propose; the risk engine
approves or rejects. There is no code path from a strategy directly to order
execution, and this is enforced by an automated architecture test.

The engine is **deterministic**. No machine-learning or LLM component may
override, weaken or bypass any rule below.

Every value lives in a single versioned, audited `RiskConfiguration` entity.
No magic numbers scattered in code.

---

## 1. Rule table

| ID | Rule | Parameter | Initial value | Enforcement | Reason code |
|----|------|-----------|---------------|-------------|-------------|
| R-01 | Fixed reference capital | `referenceCapitalRon` | 1000.00 | Basis for all percentages | — |
| R-02 | Maximum risk per trade | `maximumRiskPerTradePercent` | 0.50% → 5.00 RON | REJECT ORDER | `RISK_PER_TRADE_EXCEEDED` |
| R-03 | Maximum daily loss | `dailyMaximumLossPercent` | 4.00% → 40.00 RON | HALT DAY | `DAILY_LOSS_LIMIT_REACHED` |
| R-04 | Simultaneous positions | `maximumOpenPositions` | 1 | REJECT ORDER | `MAX_OPEN_POSITIONS_REACHED` |
| R-05 | Trades per day | `maximumTradesPerDay` | 50 | HALT DAY | `MAX_TRADES_PER_DAY_REACHED` |
| R-06 | Consecutive losses | `maximumConsecutiveLosses` | 3 | HALT DAY | `MAX_CONSECUTIVE_LOSSES_REACHED` |
| R-07 | Session target | `sessionTargetPercent` | 2.00% | CLOSE SESSION | `SESSION_TARGET_REACHED` |
| R-08 | Session restart threshold | `sessionRestartThresholdPercent` | 4.00% | ALLOW RE-EVALUATION | `SESSION_RESTART_ELIGIBLE` |
| R-09 | Market data freshness | `maxCandleAgeSeconds` | **1800 s** = 2 intervals on 15m | REJECT ORDER | `STALE_MARKET_DATA` |
| R-10 | Signal age | `maxSignalAgeSeconds` | TBD in Phase 4 | REJECT ORDER | `SIGNAL_EXPIRED` |
| R-11 | Maximum spread | `maxSpreadBps` | Calibrated on real data | REJECT ORDER | `SPREAD_TOO_WIDE` |
| R-12 | Minimum liquidity | `minOrderBookDepthQuote` | Calibrated on real data | REJECT ORDER | `LIQUIDITY_TOO_LOW` |
| R-13 | Volatility band | `minAtrPercent` / `maxAtrPercent` | Calibrated on real data | REJECT ORDER | `VOLATILITY_OUT_OF_RANGE` |
| R-14 | Minimum reward-to-risk | `minRewardRiskRatio` | 1.8, **net of fees and slippage** | REJECT ORDER | `REWARD_RISK_TOO_LOW` |
| R-15 | Maximum estimated slippage | `maxEstimatedSlippageBps` | Calibrated on real data | REJECT ORDER | `ESTIMATED_SLIPPAGE_TOO_HIGH` |
| R-16 | Exchange filters | From `exchangeInfo` | Read live, never assumed | REJECT ORDER | `EXCHANGE_FILTER_VIOLATION`, `MIN_NOTIONAL_NOT_MET` |
| R-17 | Available balance | — | Checked before every order | REJECT ORDER | `INSUFFICIENT_BALANCE` |
| R-18 | System health | `SystemHealth` | `HEALTHY` required | REJECT ORDER | `SYSTEM_HEALTH_DEGRADED` |
| R-19 | Exchange health | — | Checked before every order | REJECT ORDER | `EXCHANGE_UNHEALTHY` |
| R-20 | Emergency stop | `emergencyStopActive` | `false` | REJECT ALL | `EMERGENCY_STOP_ACTIVE` |
| R-21 | Trading window | `tradingWindows` | 24/7 initially | REJECT ORDER | `TRADING_WINDOW_CLOSED` |
| R-22 | Clock drift | `maxClockDriftMs` | TBD in Phase 3 | REJECT ORDER | `CLOCK_DRIFT_EXCEEDED` |
| R-23 | Daily profit floor | `dailyProfitGivebackPercent` | `NULL` = **disabled** (OD-03) | HALT DAY | `DAILY_PROFIT_FLOOR_REACHED` |
| R-24 | Day-boundary entry block | `noNewEntryMinutesBeforeDayEnd` | 30 | REJECT ORDER | `DAY_BOUNDARY_NO_ENTRY_WINDOW` |
| R-25 | *Daily fee budget* | — | **REJECTED in Phase 0** | Not implemented | — |
| R-26 | Daily P&L evaluation basis | `dailyPnlBasis` | `REALISED_PLUS_UNREALISED` | Modifies R-03 | `DAILY_LOSS_LIMIT_REACHED` |
| R-27 | Strategy enabled | `strategy.isEnabled` | `false` by default | REJECT ORDER | `STRATEGY_DISABLED` |

R-25 is kept in the table as a reserved, rejected identifier so that rule IDs
remain stable and the decision stays visible in the audit history.

R-27 was added in Phase 5.2. `STRATEGY_DISABLED` was in the reason-code list
from Phase 0 but had no rule identifier, which meant a documented protection
with nothing enumerable behind it. Enabling a strategy is an operator decision
and refusing a proposal from a disabled one is a gate like any other, so it now
has a row.

**R-01, R-25 and R-26 have no rule function of their own.** R-01 is the basis
every percentage is computed from, R-25 was rejected in Phase 0, and R-26
selects which P&L figure R-03 compares — it modifies a rule rather than being
one. A test asserts that every other identifier in this table is implemented.

---

## 2. Enforcement semantics

| Enforcement | Meaning |
|-------------|---------|
| `REJECT ORDER` | This specific proposal is refused. The day and session continue. |
| `HALT DAY` | The trading day transitions to `DAILY_STOP_REACHED`. No further entries today. Existing positions are still managed to exit. |
| `CLOSE SESSION` | The current session closes. A new one may start only if the restart rule allows it. |
| `REJECT ALL` | Every proposal is refused until the condition is cleared by the operator. |
| `ALLOW RE-EVALUATION` | Makes a new session possible. Never causes one. |

**A halted day still manages open positions.** Abandoning an open position is
more dangerous than declining to open a new one. Stop-loss and take-profit
monitoring continue after any halt.

---

## 3. Risk assessment record

Every `RiskAssessment` persists, for every rule evaluated:

- the rule identifier and the parameter values used;
- the actual input values at evaluation time;
- the per-rule verdict;
- the machine-readable reason codes;
- a human-readable explanation;
- the final verdict.

Not only the final verdict. A rejected trade must be as explainable as an
accepted one.

---

## 4. Complete reason code list

```text
RISK_PER_TRADE_EXCEEDED
DAILY_LOSS_LIMIT_REACHED
MAX_OPEN_POSITIONS_REACHED
MAX_TRADES_PER_DAY_REACHED
MAX_CONSECUTIVE_LOSSES_REACHED
SESSION_TARGET_REACHED
SESSION_RESTART_ELIGIBLE
STALE_MARKET_DATA
SIGNAL_EXPIRED
SPREAD_TOO_WIDE
LIQUIDITY_TOO_LOW
VOLATILITY_OUT_OF_RANGE
REWARD_RISK_TOO_LOW
ESTIMATED_SLIPPAGE_TOO_HIGH
EXCHANGE_FILTER_VIOLATION
MIN_NOTIONAL_NOT_MET
INSUFFICIENT_BALANCE
SYSTEM_HEALTH_DEGRADED
EXCHANGE_UNHEALTHY
EMERGENCY_STOP_ACTIVE
TRADING_WINDOW_CLOSED
CLOCK_DRIFT_EXCEEDED
DAILY_PROFIT_FLOOR_REACHED
DAY_BOUNDARY_NO_ENTRY_WINDOW
NO_VALID_OPPORTUNITY
STRATEGY_DISABLED
RISK_CONFIGURATION_INCOMPLETE
```

`RISK_CONFIGURATION_INCOMPLETE` was added in Phase 5.2. Several gates in the
table above are deliberately uncalibrated until Phases 4-6 supply real data, and
the engine refuses rather than passing them. Reporting such a refusal under the
gate's own code — `SPREAD_TOO_WIDE` when `maxSpreadBps` was never set — would
tell an operator the market was bad when in fact the configuration is missing.
It is a refusal that needs its own name.

An uncalibrated gate is not the same as a disabled one. `dailyProfitGivebackPercent`
is `NULL` because decision OD-03 switched R-23 off; that rule reports
`NOT_APPLICABLE` and does not block. A `NULL` threshold on a mandatory gate
reports `NOT_CALIBRATED` and does block.

---

## 5. Position sizing

```text
riskAmount   = referenceCapital * maximumRiskPerTradePercent
stopDistance = abs(entryPrice - stopLossPrice)
rawQuantity  = riskAmount / stopDistance
quantity     = round_down_to_step(rawQuantity, stepSize)
actualRisk   = quantity * stopDistance
```

Then, mandatorily:

1. If `actualRisk > riskAmount`, reject with `RISK_PER_TRADE_EXCEEDED`.
   Never round up.
2. If `quantity * entryPrice < minNotional`, reject with
   `MIN_NOTIONAL_NOT_MET`. Never inflate the quantity to satisfy the minimum —
   that would silently exceed the risk budget.
3. If `quantity <= 0`, reject.

Sizing never references account equity or accumulated profit. It references
only the fixed reference capital. This is what makes the model non-compounding.
