# Software Requirements Specification

**Project:** Trader — automated cryptocurrency spot trading application
**Version:** 0.1 (Phase 0 baseline)
**Status:** Approved

---

## 1. Purpose and scope

The system analyses cryptocurrency **spot** markets, collects live and
historical market data, evaluates trading opportunities, scores them, applies
strict risk-management rules, and executes spot orders through a real exchange
API only when live mode is explicitly enabled.

**In scope:** spot markets only, Binance Spot as the initial exchange, BTC and
ETH as the initial instruments, a 15-minute primary timeframe.

**Out of scope:** futures, margin, leverage, lending, staking, options,
derivatives of any kind, and any strategy that shorts an asset.

Because the system is spot-only, it can only take **long** positions. During a
sustained downtrend the correct system output is `NO_TRADE`, potentially for
days or weeks.

---

## 2. Glossary

| Term | Meaning |
|------|---------|
| `reportingCurrency` | Currency the operator reads results in. Fixed to **RON**. |
| `exchangeQuoteCurrency` | Currency the exchange actually quotes and settles in. Fixed to **USDT**. |
| `referenceCapital` | Fixed accounting base for every percentage. **1,000.00 RON**. Never changes with profit or loss. |
| Account equity | Real total value of the exchange account at a point in time. |
| Available balance | Account balance not locked by open orders. |
| Locked balance | Balance reserved by open orders. |
| Realised P&L | Profit or loss from closed trades. |
| Unrealised P&L | Mark-to-market profit or loss of open positions. |
| Gross P&L | P&L before fees and slippage. |
| Net P&L | Gross P&L minus fees and slippage. |
| `R` | One unit of risk. Initially 5.00 RON (0.5% of reference capital). |
| Session | A bounded sequence of 1..n trades inside one trading day. |
| Trading day | A calendar day in `Europe/Bucharest`, containing 0..n sessions. |
| `NO_TRADE` | A valid, expected outcome meaning no opportunity met the mandatory criteria. |

---

## 3. Confirmed requirements

### 3.1 Market and instruments

| ID | Requirement | Value |
|----|-------------|-------|
| CR-01 | Market type | Cryptocurrency spot only |
| CR-02 | Initial exchange | Binance Spot, behind an `ExchangeAdapter` abstraction |
| CR-03 | Future exchange | Crypto.com — **not implemented now**; adding it must not change strategies, risk engine, session engine or application services |
| CR-04 | Initial instruments | BTC, ETH |
| CR-05 | Execution pairs | `BTCUSDT`, `ETHUSDT` — availability and filters verified against `exchangeInfo` in Phase 3, never assumed |
| CR-06 | Primary timeframe | 15 minutes. Higher timeframes may be added later as filters. No 1-minute scalping. |

### 3.2 Capital model — Variant A (fixed capital)

| ID | Requirement | Value |
|----|-------------|-------|
| CR-07 | Reporting currency | RON |
| CR-08 | Reference capital | `referenceCapitalRon = 1000.00`, **fixed** |
| CR-09 | Compounding | **Forbidden.** Profit increases the real balance but never increases position sizing. |
| CR-10 | Session target | `sessionTargetPercent = 2.00` → 20.00 RON |
| CR-11 | Session restart threshold | `sessionRestartThresholdPercent = 4.00` → 40.00 RON |
| CR-12 | Daily maximum loss | `dailyMaximumLossPercent = 4.00` → 40.00 RON, **per day across all sessions** |
| CR-13 | Risk per trade | `maximumRiskPerTradePercent = 0.50` → 5.00 RON |
| CR-14 | Simultaneous positions | `maximumOpenPositions = 1` |
| CR-15 | Trades per day | `maximumTradesPerDay = 50` |
| CR-16 | Consecutive-loss stop | `maximumConsecutiveLosses = 3` |

All monetary values use `Decimal` in Python and `NUMERIC` in PostgreSQL.
Binary floating point is forbidden for money, prices, quantities, fees and
percentages.

### 3.3 Time

| ID | Requirement | Value |
|----|-------------|-------|
| CR-17 | Trading-day timezone | `Europe/Bucharest` |
| CR-18 | Persistence timezone | **UTC** for every stored timestamp |
| CR-19 | Trading window | Configurable. Initially 24/7, with statistics collected per hour and per weekday so weak periods can be excluded later on evidence, not on guesswork. |

---

## 4. Capital mapping: RON to exchange balance

The reference capital is denominated in RON, but the exchange settles in USDT.
The system keeps these strictly separate and never silently converts one into
the other.

**Decision OD-02 — locked funding rate.**

1. The operator funds the exchange account manually, once.
2. The system records a funding event containing the amount, the effective
   `fundingRate` (RON per USDT) and the timestamp.
3. All risk limits (20.00 / 40.00 / 5.00 RON) are converted into USDT **once**,
   at that locked rate, and then remain fixed.

Consequence: risk limits are deterministic and testable. They do not move with
the exchange rate, so a test written today still passes next month. The
trade-off is that if RON/USDT moves substantially, the RON value of a limit
drifts slightly from its nominal figure. This is accepted and documented.

---

## 5. FX and reporting

**Decision OD-05 — BNR reference rate.**

Reporting into RON uses the official **BNR** reference rate, persisted as an
`FxRateSnapshot` entity with `rate`, `source`, `rateDate` and `fetchedAt`.

**Why an entity and not a column:** historical reports must be reproducible.
Recomputing a past day's P&L with today's rate would produce a different number
than the one originally reported. Binding an `FxRateSnapshot` to each
`TradingDay` makes reports stable forever.

**USDT is not USD.** BNR publishes RON/USD, not RON/USDT. The conversion is
therefore explicit:

```text
usdtRonRate = bnrUsdRonRate * usdtUsdPeg
```

`usdtUsdPeg` defaults to `1.0000` and is a **stored field, not a hidden
constant**. It appears in the audit trail and in reports, can be replaced with a
real market rate later without a schema migration, and keeps historical reports
correct if the peg ever breaks.

**BNR publishes on working days only.** On weekends and holidays the last
published rate is reused, carrying its original `rateDate`. Crypto markets run
24/7; the FX source does not. The system is transparent about this rather than
inventing a value.

---

## 6. Trading day and trading session

### 6.1 Trading day

At the start of each `Europe/Bucharest` day a `TradingDay` record is created
containing at least: identifier, calendar date, timezone, fixed reference
capital, exchange equity snapshot, initial balances, status, realised P&L,
unrealised P&L, fees, trade count, session count, consecutive losses, stop
reason, and creation/update timestamps.

**Daily loss evaluation basis (decision OD-06, rule R-26):** the daily loss
limit is evaluated on

```text
dayNetPnl = realisedPnlOfDay + unrealisedPnlOfOpenPositions
```

This is the conservative choice. An open position sitting at -41 RON stops the
day immediately; the system does not wait for the position to close before
recognising the loss.

### 6.2 Day boundary (decision OD-04)

Open positions **may** cross midnight. New entries are blocked during the last
`noNewEntryMinutesBeforeDayEnd = 30` minutes of the day.

Rationale: force-closing a position at midnight is an exit with no strategic
justification, frequently at a loss. Opening a position at 23:58 on the closing
day's risk budget is equally wrong. Blocking new entries solves the second
problem without creating the first.

**Attribution rules:**

| Item | Attributed to |
|------|---------------|
| `tradeCount` (rule R-05) | The day the position was **opened** — the rule limits new entries |
| `realisedPnl` | The day the trade was **closed** |
| `consecutiveLosses` | Updated at close, therefore on the closing day |
| Unrealised P&L of a carried position | The **current** day, whichever it is (rule R-26) |
| The single position slot (rule R-04) | **Occupied** at the start of the new day by a carried position |

The `Trade` entity therefore carries both `openedTradingDayId` and
`closedTradingDayId`. Without this separation a position opened at 23:40 and
closed at 01:15 would make both days impossible to reconstruct correctly.

Operational consequence: a new day may legitimately begin with its position
slot already occupied and part of its risk budget already consumed by
mark-to-market. This is correct behaviour, not a defect.

### 6.3 Session start conditions

A session begins **only** when all of the following hold:

- the application is active;
- no daily stop condition has been triggered;
- market data is healthy and current;
- the exchange connection is healthy;
- the risk engine permits trading;
- at least one valid opportunity satisfies **every** mandatory criterion.

### 6.4 Session end conditions

A session ends when any of the following occurs:

- session net realised profit reaches 2% without exceeding the 4% threshold;
- no further valid opportunity exists;
- the daily loss limit is reached;
- another risk control triggers;
- the maximum trade count is reached;
- the maximum consecutive-loss count is reached;
- market data becomes unhealthy;
- exchange execution becomes unreliable;
- the operator activates the emergency stop;
- the configured trading window closes.

### 6.5 Restart rule

When a completed session's net realised profit satisfies
`2% <= profit <= 4%`, the application normally stops trading for that day.

When it satisfies `profit > 4%`, the application must:

1. close or reconcile all orders and positions;
2. record the completed session;
3. retain the fixed 1,000 RON reference capital;
4. perform a completely new market assessment;
5. start another session **only if** a new opportunity independently satisfies
   every strategy and risk criterion;
6. preserve the 40 RON maximum loss for the whole day;
7. **never** restart merely because the previous session exceeded 4%.

`CLOSED_RESTART_ELIGIBLE` makes a new session *possible*. It never *causes* one.

**Decision OD-03 — no profit protection.** Later sessions may give back profit
earned earlier in the day. The only floor is -40 RON for the day. A day can
therefore move from +45 RON to -40 RON, an 85 RON swing on a 1,000 RON
reference capital. This was chosen deliberately.

The `dailyProfitGivebackPercent` parameter exists in `RiskConfiguration` as a
**nullable field, `NULL` meaning disabled**, so the protection can be switched
on later without a schema migration. Rule R-23 and reason code
`DAILY_PROFIT_FLOOR_REACHED` are defined but inactive.

---

## 7. Autonomy modes

Three explicit, mutually exclusive modes.

### 7.1 `SIGNAL_ONLY`

Collects data, analyses opportunities, computes position size, stop-loss and
take-profit, displays the proposed action, **submits no exchange order**, lets
the operator approve or reject, and records the decision.

### 7.2 `PAPER_AUTOMATIC`

Decides automatically, submits **no live-money order**, uses Binance's currently
supported official demo/test environment where appropriate and otherwise a
clearly separated paper-execution adapter. Models fees, spread, partial fills
and slippage. Marks every order as simulated. **Never uses live API credentials
to create orders.** May consume real live public market data, but balances and
fills stay entirely separate from the live account.

### 7.3 `LIVE_AUTOMATIC`

Uses a real Binance account and submits real spot orders. **Disabled by
default.** Requires all four independent conditions:

1. `AUTONOMY_MODE=LIVE_AUTOMATIC`
2. `LIVE_TRADING_ENABLED=true`
3. `LIVE_TRADING_CONFIRMATION_PHRASE` matching the exact expected phrase
4. A runtime operator confirmation through the API

Changing one environment variable is never sufficient — by design. If any
condition is missing, the application refuses to start in live mode.

Live mode additionally requires a permanent and obvious `LIVE` indicator in the
UI, server-side enforcement of every risk rule, an immediate emergency stop,
cancellation of applicable open orders when safely required, and reconciliation
of local state against the exchange before and after order submission.

### 7.4 Activation preconditions

Live mode is not enabled until **all** of the following are complete: unit
tests pass, integration tests pass, backtesting is complete, out-of-sample
testing is complete, paper trading has run successfully, reconciliation is
tested, failure scenarios are tested, and the operator explicitly requests the
live activation phase.

---

## 8. The no-trade rule

Supported normal outcomes:

```text
ACTIVE_TRADING
NO_TRADE
TRADING_SUSPENDED
DAILY_TARGET_REACHED
DAILY_STOP_REACHED
MANUALLY_STOPPED
TECHNICAL_FAILURE
```

`NO_TRADE` is **not an error**.

The application must **never**:

- lower signal-quality thresholds because no trades have occurred;
- trade merely to reach the 2% objective;
- enter a trade merely because the day is close to ending;
- perform revenge trading;
- increase position size to recover losses;
- use martingale strategies;
- use grid averaging without an explicitly approved and tested strategy;
- average down automatically;
- expose the entire balance to one position;
- trade with stale or incomplete data;
- continue after risk controls fail;
- continue because an AI model recommends bypassing a deterministic safety rule.

---

## 9. Order execution requirements

Before every order the system validates: trading mode, trading-day state,
trading-session state, daily P&L, remaining daily risk, strategy status, signal
validity and expiry, current market price, spread, liquidity, symbol trading
status, minimum quantity, minimum notional, price step, quantity step,
available balance, maximum position size, existing orders, existing positions,
API health and data freshness.

**Rounding.** Prices and quantities are rounded according to the exchange
filters retrieved from `exchangeInfo`, never by an assumed number of decimal
places. Rounding always moves in the direction that **reduces** risk. After
rounding, the effective risk is recomputed; if it exceeds the cap, the order is
rejected rather than rounded up.

**Persistence.** The order intent is persisted **before** submission. The
exchange response is persisted on arrival.

**States handled:** accepted, rejected, partially filled, fully filled,
cancelled, expired, timeout with unknown exchange state, duplicate responses,
reconnects, WebSocket disconnections, REST fallback, rate-limit responses.

**Uncertain state.** A timeout or network error never leads to a blind retry.
It leads to reconciliation against the exchange. This is the single most
important safety rule in the execution path.

---

## 10. Economic reality of the configuration

These are consequences of the agreed numbers, recorded so they are not
rediscovered as surprises during backtesting.

**10.1 What 2% actually requires.** Risk per trade is 5.00 RON (1R). The
session target of 20.00 RON is **4R net**. Reaching it demands roughly +1R net
per executed trade over a small number of trades — a high standard. The correct
engineering conclusion is that the target will be reached rarely and
`NO_TRADE` will be the dominant outcome. The system is designed for that to be
normal.

**10.2 Which limit actually binds.** With `maximumTradesPerDay = 50`, the
theoretical maximum loss from stops alone is 250 RON, far above the 40 RON cap.
The daily loss limit (R-03) is therefore the constraint that really stops the
day, together with the consecutive-loss stop (R-06), which halts at 15 RON of
consecutive losses.

**10.3 Fees are material.** Position notional is `risk / stopDistance`. With
5.00 RON of risk and a 1% stop, notional is about 500 RON. *If* the effective
fee is on the order of 0.1% per side — a value that is read from the official
API in Phase 3 and never assumed — a round trip costs roughly 1 RON, about 20%
of the per-trade risk budget. Backtesting includes fees from the start;
omitting them produces optimistic and false results.

Because rule R-25 (a separate daily fee budget) was rejected in Phase 0, fee
bleed is bounded only indirectly: fees reduce net P&L, and the daily loss limit
is evaluated on net P&L, so the day halts once fees and losses together reach
40 RON.

**10.4 Reward-to-risk is measured net.** `minRewardRiskRatio` is evaluated on
P&L net of estimated fees and slippage on both sides. A setup with a gross R:R
of 1.8 may have a net R:R of 1.4.

**10.5 Rounding changes real risk.** See section 9.

---

## 11. Phase 0 decision log

| ID | Decision | Resolution |
|----|----------|------------|
| OD-01 | Execution quote currency | USDT — `BTCUSDT`, `ETHUSDT` |
| OD-02 | RON to exchange balance mapping | Locked funding rate |
| OD-03 | Profit protection between sessions | None; only the -40 RON daily floor |
| OD-04 | Positions crossing the day boundary | Allowed; no new entries in the last 30 minutes |
| OD-05 | FX source | BNR reference rate |
| OD-06 | Daily loss evaluation basis | Realised + unrealised (conservative) |
| OD-07 | Trading window | 24/7 initially, with per-hour and per-weekday statistics |
| OD-08 | Entry order type | Limit with cancellation timeout — re-confirmed in Phase 7 |
| OD-09 | Stop-loss placement | Local, application-monitored in paper; live decision deferred to Phase 11 |
| OD-10 | Python version | 3.12 in the container (the container is the source of truth) |
| OD-11 | Scheduler | APScheduler, in-process |
| OD-12 | Redis | Absent in Phases 1-2; added only on a real need |
| OD-13 | Charting library | lightweight-charts — re-confirmed in Phase 9 |
| OD-14 | Dashboard auth | Single operator, password + role, RBAC ready for dangerous actions |
| R-25 | Daily fee budget | **Rejected** by the operator in Phase 0 |

---

## 12. Items still open

| ID | Item | Needed by |
|----|------|-----------|
| — | Funded amount and effective `fundingRate` | Phase 8 (paper trading with realistic balances) |
| — | Calibration of R-09 to R-15 thresholds against real data | Phase 4-6 |
| — | Baseline strategy design, to be explained and justified before implementation | Phase 4 |
| — | Live activation approval | Phase 11 |
