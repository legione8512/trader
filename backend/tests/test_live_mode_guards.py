"""Live-mode guard tests.

Covers AC-13 (live mode is disabled by default) and AC-24 (all four guards are
required). These are the most important tests in the repository at this stage:
they are what makes "live trading cannot be enabled accidentally" a fact rather
than an intention.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.config.settings import LIVE_TRADING_EXPECTED_PHRASE, Settings
from app.domain.enums import AutonomyMode

VALID_LIVE_CONFIGURATION: dict[str, Any] = {
    "autonomy_mode": AutonomyMode.LIVE_AUTOMATIC,
    "live_trading_enabled": True,
    "live_trading_confirmation_phrase": LIVE_TRADING_EXPECTED_PHRASE,
    "binance_live_api_key": "live-key-value-1234567890",
    "binance_live_api_secret": "live-secret-value-1234567890",
}


class TestDefaults:
    def test_default_mode_is_signal_only(self, settings: Settings) -> None:
        """AC-13: an empty configuration must not produce a live-capable app."""
        assert settings.autonomy_mode is AutonomyMode.SIGNAL_ONLY
        assert settings.live_trading_enabled is False
        assert settings.is_live_execution_configured is False

    def test_no_credentials_are_configured_by_default(self, settings: Settings) -> None:
        assert settings.binance_live_api_key is None
        assert settings.binance_live_api_secret is None
        assert settings.binance_paper_api_key is None
        assert settings.binance_paper_api_secret is None


class TestGuardsAreAllRequired:
    """AC-24: removing any single guard must make live mode unavailable."""

    def test_mode_alone_is_not_enough(self, make_settings: Callable[..., Settings]) -> None:
        with pytest.raises(ValidationError, match="LIVE_AUTOMATIC mode refused"):
            make_settings(autonomy_mode=AutonomyMode.LIVE_AUTOMATIC)

    @pytest.mark.parametrize(
        "removed_guard",
        [
            "live_trading_enabled",
            "live_trading_confirmation_phrase",
            "binance_live_api_key",
            "binance_live_api_secret",
        ],
    )
    def test_each_missing_guard_refuses_live_mode(
        self, make_settings: Callable[..., Settings], removed_guard: str
    ) -> None:
        configuration = dict(VALID_LIVE_CONFIGURATION)
        configuration[removed_guard] = False if removed_guard == "live_trading_enabled" else None

        with pytest.raises(ValidationError, match="LIVE_AUTOMATIC mode refused"):
            make_settings(**configuration)

    def test_wrong_confirmation_phrase_refuses_live_mode(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        configuration = dict(VALID_LIVE_CONFIGURATION)
        configuration["live_trading_confirmation_phrase"] = "i understand live trading risks"

        with pytest.raises(ValidationError, match="LIVE_AUTOMATIC mode refused"):
            make_settings(**configuration)

    def test_complete_configuration_is_accepted(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(**VALID_LIVE_CONFIGURATION)
        assert settings.autonomy_mode is AutonomyMode.LIVE_AUTOMATIC
        assert settings.is_live_execution_configured is True


class TestNonLiveModesAreUnaffected:
    @pytest.mark.parametrize(
        "mode",
        [AutonomyMode.SIGNAL_ONLY, AutonomyMode.PAPER_AUTOMATIC],
    )
    def test_non_live_modes_never_report_live_execution(
        self, make_settings: Callable[..., Settings], mode: AutonomyMode
    ) -> None:
        """Even with the live flag set, a non-live mode stays non-live."""
        settings = make_settings(autonomy_mode=mode, live_trading_enabled=True)
        assert settings.is_live_execution_configured is False

    def test_paper_mode_does_not_require_live_credentials(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            autonomy_mode=AutonomyMode.PAPER_AUTOMATIC,
            binance_paper_api_key="paper-key-value-1234567890",
            binance_paper_api_secret="paper-secret-value-1234567890",
        )
        assert settings.autonomy_mode is AutonomyMode.PAPER_AUTOMATIC
        assert settings.binance_live_api_key is None


class TestConfirmationPhrase:
    def test_phrase_is_stored_as_a_secret(self, make_settings: Callable[..., Settings]) -> None:
        settings = make_settings(**VALID_LIVE_CONFIGURATION)
        phrase = settings.live_trading_confirmation_phrase
        assert isinstance(phrase, SecretStr)
        assert LIVE_TRADING_EXPECTED_PHRASE not in repr(phrase)
        assert LIVE_TRADING_EXPECTED_PHRASE not in str(phrase)

    def test_expected_phrase_is_not_a_plausible_typo(self) -> None:
        """The phrase must be deliberate, not something produced by accident."""
        assert LIVE_TRADING_EXPECTED_PHRASE == "I UNDERSTAND LIVE TRADING RISKS"
        assert len(LIVE_TRADING_EXPECTED_PHRASE) > 20
