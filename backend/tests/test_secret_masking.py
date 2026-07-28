"""Secret masking tests.

Covers AC-14: API secrets must never appear in responses or logs.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config.settings import Settings
from app.core.logging import mask_secrets_processor, register_configured_secrets
from app.core.secrets import (
    MINIMUM_SECRET_LENGTH,
    REDACTED,
    SecretRegistry,
    is_sensitive_key,
    mask_mapping,
    secret_registry,
)

REAL_SECRET = "S3cr3t-Api-Key-abcdef1234567890"


class TestSecretRegistry:
    def test_registers_a_long_enough_value(self) -> None:
        registry = SecretRegistry()
        assert registry.register(REAL_SECRET) is True
        assert registry.size == 1

    def test_rejects_short_values(self) -> None:
        """A short registered value would redact unrelated text everywhere."""
        registry = SecretRegistry()
        assert registry.register("abc") is False
        assert registry.register("x" * (MINIMUM_SECRET_LENGTH - 1)) is False
        assert registry.register(None) is False
        assert registry.size == 0

    def test_masks_the_value_inside_arbitrary_text(self) -> None:
        registry = SecretRegistry()
        registry.register(REAL_SECRET)
        text = f"request failed with key={REAL_SECRET} at endpoint /order"
        masked = registry.mask_text(text)
        assert REAL_SECRET not in masked
        assert REDACTED in masked
        assert "/order" in masked

    def test_leaves_unrelated_text_untouched(self) -> None:
        registry = SecretRegistry()
        registry.register(REAL_SECRET)
        assert registry.mask_text("nothing sensitive here") == "nothing sensitive here"


class TestSensitiveKeyDetection:
    def test_recognises_sensitive_names(self) -> None:
        for name in (
            "api_key",
            "BINANCE_LIVE_API_SECRET",
            "password",
            "access_token",
            "live_trading_confirmation_phrase",
            "Authorization",
            "signature",
        ):
            assert is_sensitive_key(name) is True

    def test_ignores_ordinary_names(self) -> None:
        for name in ("symbol", "quantity", "order_id", "status", "reference_capital_ron"):
            assert is_sensitive_key(name) is False


class TestMaskMapping:
    def test_redacts_by_key_name_even_when_value_is_unknown(self) -> None:
        masked = mask_mapping({"api_key": "never-registered-value"})
        assert masked["api_key"] == REDACTED

    def test_redacts_by_value_even_when_key_looks_innocent(self) -> None:
        registry = SecretRegistry()
        registry.register(REAL_SECRET)
        masked = mask_mapping({"event": f"calling https://x/y?k={REAL_SECRET}"}, registry)
        assert REAL_SECRET not in str(masked["event"])

    def test_recurses_into_nested_structures(self) -> None:
        registry = SecretRegistry()
        registry.register(REAL_SECRET)
        payload = {
            "request": {"headers": {"authorization": "Bearer xyz"}, "body": REAL_SECRET},
            "items": [REAL_SECRET, "safe"],
        }
        masked = mask_mapping(payload, registry)
        assert masked["request"]["headers"]["authorization"] == REDACTED
        assert masked["request"]["body"] == REDACTED
        assert masked["items"][0] == REDACTED
        assert masked["items"][1] == "safe"

    def test_preserves_non_string_values(self) -> None:
        masked = mask_mapping({"quantity": 3, "price": None, "filled": True})
        assert masked == {"quantity": 3, "price": None, "filled": True}


class TestLogProcessor:
    def test_processor_redacts_registered_secrets(self) -> None:
        secret_registry.register(REAL_SECRET)
        event = {"event": "order_submitted", "detail": f"key {REAL_SECRET} used"}
        masked = mask_secrets_processor(None, "info", event)
        assert REAL_SECRET not in str(masked)

    def test_processor_redacts_sensitive_field_names(self) -> None:
        event = {"event": "startup", "binance_live_api_secret": "anything at all"}
        masked = mask_secrets_processor(None, "info", event)
        assert masked["binance_live_api_secret"] == REDACTED


class TestSettingsIntegration:
    def test_configured_secrets_are_registered(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            binance_paper_api_key="paper-key-abcdefghijklmnop",
            binance_paper_api_secret="paper-secret-abcdefghijklmnop",
        )
        registered = register_configured_secrets(settings.secret_values())
        assert registered == 2

        masked = mask_mapping({"event": "using paper-key-abcdefghijklmnop"})
        assert "paper-key-abcdefghijklmnop" not in str(masked["event"])

    def test_secret_values_returns_only_configured_secrets(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        assert make_settings().secret_values() == []

    def test_repr_of_settings_never_leaks_a_secret(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(binance_paper_api_key=REAL_SECRET)
        assert REAL_SECRET not in repr(settings)
        assert REAL_SECRET not in str(settings.model_dump())
