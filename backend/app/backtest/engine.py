"""Replaying a strategy over historical candles.

The engine runs the **real** strategy, the **real** position sizing and the
**real** risk rules. Not simplified copies: a backtest that validates a
simplified engine has validated something that will never trade.

**The strategy sees the same bounded window a live run sees.** At each candle it
is handed exactly ``required_candles`` of history - not the whole prefix. That
is not only a performance decision, though it is the difference between a run
that finishes and one that does not: recursive indicators like the EMA depend on
every bar they have ever seen, so a backtest computing over all history and a
live process computing over a loaded window would disagree. Bounding both to the
same length makes them identical.

**Assumptions that cannot come from candles are declared, not hidden.** A candle
carries no spread, no order-book depth and no slippage, so rules R-11, R-12 and
R-15 cannot be evaluated from history. Feeding them ``None`` would refuse every
trade and produce an empty, meaningless run; feeding them silent optimistic
values would produce a confident, meaningless one. They are supplied explicitly,
recorded on the result, and reported alongside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from app.backtest.fills import (
    Bar,
    ExitFill,
    ExitTrigger,
    FillAssumptions,
    simulate_exit,
    simulate_limit_entry,
    simulate_trailing_exit,
)
from app.domain.candle_window import CandleWindow
from app.domain.enums import OrderSide, RiskReasonCode, Timeframe
from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION
from app.domain.position_sizing import SizingRequest, SizingResult, size_position
from app.domain.risk.context import (
    DayState,
    MarketState,
    ProposalUnderReview,
    RiskContext,
    SystemState,
)
from app.domain.risk.economics import TradingCosts, net_reward_risk_ratio
from app.domain.risk.engine import evaluate as evaluate_risk
from app.domain.risk.limits import RiskLimits
from app.domain.symbol_filters import SymbolFilters
from app.domain.trading_calendar import day_end, trading_date_for
from app.strategies.base import SignalProposal, Strategy, StrategyContext, code_fingerprint

ZERO = Decimal(0)
#: Six decimal places, matching the reward-to-risk column in the database.
R_MULTIPLE_QUANTUM = Decimal("0.000001")


class BacktestError(DomainError):
    """The run cannot be performed as configured."""


@dataclass(frozen=True, slots=True)
class MarketAssumptions:
    """What the candles cannot tell us, stated rather than assumed silently.

    Every one of these is a value a live run measures and a historical candle
    does not contain. They are inputs to the result, not properties of it.
    """

    spread_bps: Decimal
    order_book_depth_quote: Decimal
    slippage_bps: Decimal

    def describe(self) -> dict[str, str]:
        return {
            "spread_bps": (
                f"{self.spread_bps} assumed constant. Candles carry no spread; a "
                f"live run measures it and rule R-11 judges the measurement."
            ),
            "order_book_depth_quote": (
                f"{self.order_book_depth_quote} assumed constant. Historical depth "
                f"is not recoverable from candles."
            ),
            "slippage_bps": (
                f"{self.slippage_bps} assumed per side, and charged in the cost "
                f"model as well as checked by rule R-15."
            ),
        }


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    """Everything a run needs, and everything it must report."""

    symbol: str
    timeframe: Timeframe
    limits: RiskLimits
    costs: TradingCosts
    filters: SymbolFilters
    #: Reporting currency per unit of quote. Held constant across the run, and
    #: that is an assumption: RON/USDT moved over any period worth testing.
    funding_rate: Decimal
    market: MarketAssumptions
    trading_timezone: str = "Europe/Bucharest"
    entry_timeout_bars: int = 4
    max_bars_in_trade: int | None = None

    def __post_init__(self) -> None:
        if self.funding_rate <= ZERO:
            raise BacktestError("Funding rate must be positive")
        if self.entry_timeout_bars < 1:
            raise BacktestError("Entry orders must be live for at least one bar")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    """One completed round trip, with the numbers that produced it."""

    signal_index: int
    entry_index: int
    exit_index: int
    entry_time: datetime
    exit_time: datetime
    side: OrderSide
    quantity: Decimal
    entry_price: Decimal
    exit_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal | None
    trigger: ExitTrigger
    gross_pnl_quote: Decimal
    fees_quote: Decimal
    net_pnl_quote: Decimal
    net_pnl_reporting: Decimal
    #: Net result in units of the risk that was taken. The only figure that
    #: compares trades of different sizes honestly.
    r_multiple: Decimal
    risk_reporting: Decimal
    gapped: bool = False
    ambiguous_bar: bool = False

    @property
    def is_win(self) -> bool:
        return self.net_pnl_quote > ZERO


@dataclass(slots=True)
class BacktestResult:
    """What the run did, including everything it refused to do."""

    config: BacktestConfig
    fill_assumptions: FillAssumptions
    strategy_name: str
    strategy_parameters: dict[str, object]
    strategy_fingerprint: str

    bars_evaluated: int = 0
    proposals: int = 0
    #: Proposals the risk engine refused, counted per reason code. A run that
    #: produced no trades is not the same as a run that produced none because
    #: the minimum notional was never met, and this is the difference.
    rejections: dict[str, int] = field(default_factory=dict)
    entries_expired_unfilled: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)
    #: Proposals skipped because a position was already open. Not a refusal -
    #: an opportunity the position limit cost, and worth seeing.
    skipped_position_open: int = 0

    def record_rejection(self, codes: tuple[RiskReasonCode, ...]) -> None:
        for code in codes:
            self.rejections[code.value] = self.rejections.get(code.value, 0) + 1

    @property
    def assumptions(self) -> dict[str, dict[str, str]]:
        """Carried with the result. A backtest read without them is a number
        without units."""
        return {
            "fills": self.fill_assumptions.describe(),
            "market": self.config.market.describe(),
            "funding_rate": {
                "value": str(self.config.funding_rate),
                "note": (
                    "Held constant for the whole run. RON/USDT moved over any "
                    "period worth testing, so results are stated in R as well."
                ),
            },
        }


def run_backtest(
    strategy: Strategy, window: CandleWindow, config: BacktestConfig
) -> BacktestResult:
    """Replay ``strategy`` over ``window``, honouring every real rule."""
    if window.timeframe is not config.timeframe:
        raise BacktestError(
            f"Window is {window.timeframe.value} but the run is configured for "
            f"{config.timeframe.value}"
        )

    bars = [
        Bar(
            open_time=window.open_times[index],
            open=window.opens[index],
            high=window.highs[index],
            low=window.lows[index],
            close=window.closes[index],
        )
        for index in range(len(window))
    ]
    fill_assumptions = FillAssumptions(entry_timeout_bars=config.entry_timeout_bars)
    result = BacktestResult(
        config=config,
        fill_assumptions=fill_assumptions,
        strategy_name=strategy.name,
        strategy_parameters=dict(strategy.parameters),
        strategy_fingerprint=code_fingerprint(strategy),
    )

    required = strategy.required_candles
    day = _RunningDay(config.limits, config.trading_timezone)
    index = required - 1
    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        while index < len(bars):
            result.bars_evaluated += 1
            # Before anything is evaluated: a candle belonging to a new day
            # must be judged against a fresh day's counters, not yesterday's.
            day.advance_to(bars[index].open_time)
            # The same bounded history a live run would have loaded, so the two
            # take identical code paths.
            view = window.slice(index - required + 1, index + 1)
            proposal = strategy.evaluate(
                StrategyContext(window=view, evaluated_at=bars[index].open_time)
            )
            if proposal is None:
                index += 1
                continue

            result.proposals += 1
            trade = _attempt(proposal, bars, index, config, day, result)
            if trade is None:
                index += 1
                continue

            result.trades.append(trade)
            day.record(trade)
            # One position at a time: nothing is evaluated while the previous
            # trade is still running, which is what maximumOpenPositions means.
            index = max(index + 1, trade.exit_index + 1)
    return result


def _attempt(
    proposal: SignalProposal,
    bars: list[Bar],
    index: int,
    config: BacktestConfig,
    day: _RunningDay,
    result: BacktestResult,
) -> BacktestTrade | None:
    sizing = size_position(
        SizingRequest(
            side=proposal.side,
            reference_price=proposal.reference_price,
            stop_loss_price=proposal.stop_loss_price,
            take_profit_price=proposal.take_profit_price,
            risk_budget_reporting=config.limits.risk_per_trade_amount,
            funding_rate=config.funding_rate,
            filters=config.filters,
        )
    )

    decision = evaluate_risk(_context(proposal, sizing, config, day, bars[index].open_time))
    if not decision.is_approved:
        result.record_rejection(decision.reason_codes)
        return None

    entry = simulate_limit_entry(
        bars,
        signal_index=index,
        side=proposal.side,
        limit_price=sizing.entry_price,
        timeout_bars=config.entry_timeout_bars,
    )
    if not entry.filled or entry.bar_index is None or entry.price is None:
        result.entries_expired_unfilled += 1
        return None

    # A strategy that wants a trailing exit says so by publishing the levels it
    # computed at entry. Read rather than recomputed here: recomputing would
    # create a second implementation that can disagree with the one the decision
    # was taken on.
    trailing = _trailing_settings(proposal)
    if trailing is not None:
        atr, activation, distance = trailing
        exit_fill = simulate_trailing_exit(
            bars,
            entry_index=entry.bar_index,
            entry_price=entry.price,
            side=proposal.side,
            initial_stop_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
            atr_at_entry=atr,
            activation_atr=activation / atr,
            trailing_atr=distance / atr,
            max_bars=config.max_bars_in_trade,
        )
    else:
        exit_fill = simulate_exit(
            bars,
            entry_index=entry.bar_index,
            side=proposal.side,
            stop_loss_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
            max_bars=config.max_bars_in_trade,
        )
    return _settle(proposal, sizing, entry.bar_index, entry.price, exit_fill, index, bars, config)


def _context(
    proposal: SignalProposal,
    sizing: SizingResult,
    config: BacktestConfig,
    day: _RunningDay,
    now: datetime,
) -> RiskContext:
    net_ratio: Decimal | None = None
    if sizing.is_viable and sizing.take_profit_price is not None:
        net_ratio = net_reward_risk_ratio(
            entry_price=sizing.entry_price,
            stop_loss_price=sizing.stop_loss_price,
            take_profit_price=sizing.take_profit_price,
            costs=config.costs,
        )
    return RiskContext(
        evaluated_at=now,
        limits=config.limits,
        day=day.state(now),
        market=MarketState(
            # Zero, and true: the decision is taken at this candle's close, so
            # the data is exactly as fresh as it can be.
            candle_age=timedelta(0),
            spread_bps=config.market.spread_bps,
            order_book_depth_quote=config.market.order_book_depth_quote,
            atr_percent=_atr_percent(proposal),
            estimated_slippage_bps=config.market.slippage_bps,
            clock_drift_ms=ZERO,
        ),
        system=SystemState(
            sizing_is_viable=sizing.is_viable,
            sizing_reason_codes=tuple(code.value for code in sizing.reason_codes),
        ),
        proposal=ProposalUnderReview(
            side=proposal.side,
            entry_price=sizing.entry_price if sizing.is_viable else proposal.reference_price,
            stop_loss_price=(
                sizing.stop_loss_price if sizing.is_viable else proposal.stop_loss_price
            ),
            take_profit_price=sizing.take_profit_price,
            net_reward_risk_ratio=net_ratio,
            quantity=sizing.quantity if sizing.is_viable else None,
            risk_amount_reporting=sizing.risk_reporting if sizing.is_viable else None,
            notional_quote=sizing.notional_quote if sizing.is_viable else None,
            # Zero: the proposal is judged on the candle that produced it.
            signal_age=timedelta(0),
        ),
    )


def _trailing_settings(
    proposal: SignalProposal,
) -> tuple[Decimal, Decimal, Decimal] | None:
    """The trailing levels a strategy published, or ``None`` for a fixed exit."""
    required = ("atr_at_entry", "trailing_activation", "trailing_atr")
    if not all(key in proposal.inputs for key in required):
        return None
    atr = Decimal(str(proposal.inputs["atr_at_entry"]))
    if atr <= ZERO:
        return None
    return (
        atr,
        Decimal(str(proposal.inputs["trailing_activation"])),
        Decimal(str(proposal.inputs["trailing_atr"])),
    )


def _atr_percent(proposal: SignalProposal) -> Decimal | None:
    """Read from what the strategy recorded, never recomputed.

    Recomputing it here would create a second implementation that can disagree
    with the one the decision was actually taken on.
    """
    raw = proposal.inputs.get("atr_fraction")
    if raw is None:
        return None
    return Decimal(str(raw)) * Decimal(100)


def _settle(
    proposal: SignalProposal,
    sizing: SizingResult,
    entry_index: int,
    entry_price: Decimal,
    exit_fill: ExitFill,
    signal_index: int,
    bars: list[Bar],
    config: BacktestConfig,
) -> BacktestTrade:
    quantity = sizing.quantity
    direction = Decimal(1) if proposal.side is OrderSide.BUY else Decimal(-1)
    gross = (exit_fill.price - entry_price) * quantity * direction

    # Charged on both legs, on the notional actually traded. A losing trade pays
    # its fees too.
    fees = (entry_price * quantity + exit_fill.price * quantity) * config.costs.fee_rate_per_side
    net_quote = gross - fees
    net_reporting = net_quote * config.funding_rate
    risk = sizing.risk_reporting

    return BacktestTrade(
        signal_index=signal_index,
        entry_index=entry_index,
        exit_index=exit_fill.bar_index,
        entry_time=bars[entry_index].open_time,
        exit_time=bars[exit_fill.bar_index].open_time,
        side=proposal.side,
        quantity=quantity,
        entry_price=entry_price,
        exit_price=exit_fill.price,
        stop_loss_price=sizing.stop_loss_price,
        take_profit_price=sizing.take_profit_price,
        trigger=exit_fill.trigger,
        gross_pnl_quote=gross,
        fees_quote=fees,
        net_pnl_quote=net_quote,
        net_pnl_reporting=net_reporting,
        # Quantised to six places: an R multiple is a reporting figure, and
        # carrying sixty digits of it would make two runs that agree look like
        # two that do not.
        r_multiple=((net_reporting / risk).quantize(R_MULTIPLE_QUANTUM) if risk > ZERO else ZERO),
        risk_reporting=risk,
        gapped=exit_fill.gapped,
        ambiguous_bar=exit_fill.ambiguous,
    )


class _RunningDay:
    """The day state the risk rules read, rolling over at the day boundary.

    **The rollover is not a detail.** Without it a whole run is one endless day:
    three losing trades anywhere in two years trip R-06 and nothing can ever
    trade again. A first version of this class had no rollover and produced
    exactly that - 3 trades out of 574 proposals, with 571 refusals blaming the
    consecutive-loss rule. The numbers looked like a verdict on the strategy and
    were an artefact of the engine.

    Days are resolved through the same trading calendar the live system uses, so
    a 23-hour and a 25-hour day are the ones the operator's timezone actually
    has rather than fixed 24-hour blocks.
    """

    def __init__(self, limits: RiskLimits, timezone: str) -> None:
        self._limits = limits
        self._timezone = ZoneInfo(timezone)
        self._date: date | None = None
        self._realised = ZERO
        self._trades = 0
        self._consecutive_losses = 0
        self._day_end: datetime | None = None

    def advance_to(self, instant: datetime) -> None:
        """Roll the day over if this candle belongs to a new one."""
        today = trading_date_for(instant, self._timezone)
        if today == self._date:
            return
        self._date = today
        self._realised = ZERO
        self._trades = 0
        # The streak resets too. R-06 halts a DAY; carrying the count across the
        # boundary would halt every following day as well.
        self._consecutive_losses = 0
        self._day_end = day_end(today, self._timezone)

    def state(self, now: datetime) -> DayState:
        remaining = self._day_end - now if self._day_end is not None else None
        return DayState(
            realised_pnl=self._realised,
            trades_today=self._trades,
            consecutive_losses=self._consecutive_losses,
            # One session per day here. Sessions are modelled in the paper
            # trading phase, where a session can actually be closed and
            # reopened; pretending to have them now would make R-07 fire on a
            # boundary the engine cannot honour.
            session_pnl=self._realised,
            time_remaining_in_day=remaining,
        )

    def record(self, trade: BacktestTrade) -> None:
        self._realised += trade.net_pnl_reporting
        self._trades += 1
        self._consecutive_losses = 0 if trade.is_win else self._consecutive_losses + 1
