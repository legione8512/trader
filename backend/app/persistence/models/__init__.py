"""SQLAlchemy models.

Importing this package registers every model with ``Base.metadata``. Alembic's
autogenerate compares the live database against that metadata, so a model that
is never imported here is invisible to it - and autogenerate would silently
propose dropping its table.
"""

from app.persistence.models.audit import AuditEvent, SystemEvent
from app.persistence.models.configuration import RiskConfiguration, TradingConfiguration
from app.persistence.models.execution import Order, OrderFill, Position, Trade
from app.persistence.models.fx import FxRateSnapshot
from app.persistence.models.market_data import Candle
from app.persistence.models.reference import Exchange, TradingPair
from app.persistence.models.signals import RiskAssessment, Signal
from app.persistence.models.snapshots import BalanceSnapshot, PnLSnapshot
from app.persistence.models.strategy import Strategy, StrategyVersion
from app.persistence.models.trading import TradingDay, TradingSession

__all__ = [
    "AuditEvent",
    "BalanceSnapshot",
    "Candle",
    "Exchange",
    "FxRateSnapshot",
    "Order",
    "OrderFill",
    "PnLSnapshot",
    "Position",
    "RiskAssessment",
    "RiskConfiguration",
    "Signal",
    "Strategy",
    "StrategyVersion",
    "SystemEvent",
    "Trade",
    "TradingConfiguration",
    "TradingDay",
    "TradingPair",
    "TradingSession",
]
