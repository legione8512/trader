"""Configuration tests.

Covers AC-03 (the reference capital is fixed) and AC-17 (money is Decimal).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.domain.enums import AppEnvironment, AutonomyMode


class TestPhaseZeroDefaults:
    """The defaults must be exactly the Phase 0 agreed values."""

    def test_reference_capital_is_one_thousand_ron(self, settings: Settings) -> None:
        assert settings.bootstrap_reference_capital_ron == Decimal("1000.00")

    def test_risk_parameter_defaults(self, settings: Settings) -> None:
        assert settings.bootstrap_session_target_percent == Decimal("2.00")
        assert settings.bootstrap_session_restart_threshold_percent == Decimal("4.00")
        assert settings.bootstrap_daily_maximum_loss_percent == Decimal("4.00")
        assert settings.bootstrap_maximum_risk_per_trade_percent == Decimal("0.50")
        assert settings.bootstrap_maximum_open_positions == 1
        assert settings.bootstrap_maximum_trades_per_day == 50
        assert settings.bootstrap_maximum_consecutive_losses == 3
        assert settings.bootstrap_no_new_entry_minutes_before_day_end == 30

    def test_daily_profit_floor_is_disabled_by_default(self, settings: Settings) -> None:
        """Phase 0 decision OD-03: no profit protection between sessions."""
        assert settings.bootstrap_daily_profit_giveback_percent is None

    def test_trading_identity_defaults(self, settings: Settings) -> None:
        assert settings.reporting_currency == "RON"
        assert settings.exchange_quote_currency == "USDT"
        assert settings.trading_timezone == "Europe/Bucharest"
        assert settings.fx_rate_source == "BNR"
        assert settings.usdt_usd_peg == Decimal("1.0000")


class TestDerivedRiskAmounts:
    """Derived RON amounts must be exact Decimals, never floats."""

    def test_derived_amounts(self, settings: Settings) -> None:
        assert settings.bootstrap_session_target_ron == Decimal("20.00")
        assert settings.bootstrap_session_restart_threshold_ron == Decimal("40.00")
        assert settings.bootstrap_daily_maximum_loss_ron == Decimal("40.00")
        assert settings.bootstrap_maximum_risk_per_trade_ron == Decimal("5.00")

    def test_derived_amounts_are_decimal_not_float(self, settings: Settings) -> None:
        # mypy proves the "not a float" half statically: Decimal and float are
        # disjoint types, so an isinstance(amount, float) check here would be
        # flagged as unreachable code. The runtime assertion below covers the
        # remaining risk, that a property silently returns something else.
        for amount in (
            settings.bootstrap_session_target_ron,
            settings.bootstrap_daily_maximum_loss_ron,
            settings.bootstrap_maximum_risk_per_trade_ron,
        ):
            assert isinstance(amount, Decimal)
            assert amount == amount.quantize(Decimal("0.01"))

    def test_percent_of_reference_rounds_down(self, make_settings: Callable[..., Settings]) -> None:
        """Rounding a risk limit up would permit more risk than configured."""
        settings = make_settings(
            bootstrap_reference_capital_ron=Decimal("1000.00"),
            bootstrap_maximum_risk_per_trade_percent=Decimal("0.333"),
        )
        # 1000.00 * 0.333 / 100 = 3.33 exactly, so nothing is lost here...
        assert settings.bootstrap_maximum_risk_per_trade_ron == Decimal("3.33")

        settings = make_settings(
            bootstrap_reference_capital_ron=Decimal("1000.00"),
            bootstrap_maximum_risk_per_trade_percent=Decimal("0.3339"),
        )
        # ...but 3.339 must become 3.33, never 3.34.
        assert settings.bootstrap_maximum_risk_per_trade_ron == Decimal("3.33")

    def test_reference_capital_does_not_change_with_percentages(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        """AC-03: the reference capital is fixed. Nothing derives back into it."""
        settings = make_settings()
        original = settings.bootstrap_reference_capital_ron
        _ = settings.bootstrap_session_target_ron
        _ = settings.bootstrap_daily_maximum_loss_ron
        _ = settings.bootstrap_maximum_risk_per_trade_ron
        assert settings.bootstrap_reference_capital_ron == original == Decimal("1000.00")


class TestValidation:
    def test_unknown_timezone_is_rejected(self, make_settings: Callable[..., Settings]) -> None:
        with pytest.raises(ValidationError, match="Unknown timezone"):
            make_settings(trading_timezone="Europe/Atlantis")

    def test_log_level_is_normalised_and_validated(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        assert make_settings(log_level="debug").log_level == "DEBUG"
        with pytest.raises(ValidationError):
            make_settings(log_level="VERBOSE")

    def test_currency_codes_are_uppercased(self, make_settings: Callable[..., Settings]) -> None:
        settings = make_settings(reporting_currency="ron", exchange_quote_currency="usdt")
        assert settings.reporting_currency == "RON"
        assert settings.exchange_quote_currency == "USDT"

    @pytest.mark.parametrize(
        "field_name",
        [
            "bootstrap_reference_capital_ron",
            "bootstrap_session_target_percent",
            "bootstrap_daily_maximum_loss_percent",
            "bootstrap_maximum_risk_per_trade_percent",
        ],
    )
    def test_non_positive_risk_values_are_rejected(
        self, make_settings: Callable[..., Settings], field_name: str
    ) -> None:
        with pytest.raises(ValidationError):
            make_settings(**{field_name: Decimal("0")})

    def test_zero_open_positions_is_rejected(self, make_settings: Callable[..., Settings]) -> None:
        with pytest.raises(ValidationError):
            make_settings(bootstrap_maximum_open_positions=0)

    def test_empty_string_means_disabled_not_zero(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(bootstrap_daily_profit_giveback_percent="")
        assert settings.bootstrap_daily_profit_giveback_percent is None

    def test_cors_origins_are_parsed(self, make_settings: Callable[..., Settings]) -> None:
        settings = make_settings(
            cors_allowed_origins="http://localhost:5173, http://127.0.0.1:5173 ,"
        )
        assert settings.cors_origins == ["http://localhost:5173", "http://127.0.0.1:5173"]


class TestProductionHardening:
    def test_placeholder_password_is_rejected_in_production(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        with pytest.raises(ValidationError, match="placeholder password"):
            make_settings(
                app_env=AppEnvironment.PRODUCTION,
                database_url="postgresql+asyncpg://trader:CHANGE_ME@db:5432/trader",
            )

    def test_debug_logging_is_rejected_in_production(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        with pytest.raises(ValidationError, match="DEBUG"):
            make_settings(
                app_env=AppEnvironment.PRODUCTION,
                database_url="postgresql+asyncpg://trader:realpassword@db:5432/trader",
                database_url_sync="postgresql+psycopg://trader:realpassword@db:5432/trader",
                log_level="DEBUG",
            )

    def test_valid_production_configuration_is_accepted(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            app_env=AppEnvironment.PRODUCTION,
            database_url="postgresql+asyncpg://trader:realpassword@db:5432/trader",
            database_url_sync="postgresql+psycopg://trader:realpassword@db:5432/trader",
            log_level="INFO",
        )
        assert settings.app_env is AppEnvironment.PRODUCTION
        assert settings.autonomy_mode is AutonomyMode.SIGNAL_ONLY
