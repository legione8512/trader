"""Application configuration.

Every setting is validated at startup. The application refuses to start with an
incoherent configuration rather than starting and failing later, mid-trade.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Final
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.enums import AppEnvironment, AutonomyMode

#: The exact phrase required in LIVE_TRADING_CONFIRMATION_PHRASE. Typing it is a
#: deliberate act; it cannot be produced by a typo or a copy-pasted default.
LIVE_TRADING_EXPECTED_PHRASE: Final = "I UNDERSTAND LIVE TRADING RISKS"

#: Marker left in .env.example placeholders. Forbidden in production.
PLACEHOLDER_MARKER: Final = "CHANGE_ME"

VALID_LOG_LEVELS: Final = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_CENT: Final = Decimal("0.01")

#: .env locations resolved from this file, not from the current directory, so
#: that `uvicorn` behaves identically whether started from the repository root
#: or from backend/. Later entries win. Missing files are ignored.
_BACKEND_ROOT: Final = Path(__file__).resolve().parents[2]
_ENV_FILES: Final = (_BACKEND_ROOT.parent / ".env", _BACKEND_ROOT / ".env")

#: Fields where an empty environment variable means "not set", not "empty value".
_EMPTY_MEANS_NONE: Final = (
    "live_trading_confirmation_phrase",
    "binance_paper_api_key",
    "binance_paper_api_secret",
    "binance_live_api_key",
    "binance_live_api_secret",
    "bootstrap_daily_profit_giveback_percent",
)


class Settings(BaseSettings):
    """Validated application configuration loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        # POSTGRES_* variables in .env are consumed by docker-compose, not by
        # the application. "ignore" lets one .env serve both.
        extra="ignore",
    )

    # ---------------------------------------------------------------- app ---
    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    log_level: str = "INFO"
    api_host: str = "0.0.0.0"  # noqa: S104 - binding all interfaces is intended in a container
    api_port: int = Field(default=8000, gt=0, lt=65536)
    cors_allowed_origins: str = "http://localhost:5173"

    # ----------------------------------------------------------- database ---
    #: One async driver for both the application and Alembic. Two URLs would
    #: mean two places where the same password can diverge or leak.
    database_url: str = "postgresql+asyncpg://trader:CHANGE_ME@localhost:5432/trader"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)

    # ------------------------------------------------- trading identity ----
    reporting_currency: str = "RON"
    exchange_quote_currency: str = "USDT"
    trading_timezone: str = "Europe/Bucharest"

    # ------------------------------------------------------ autonomy mode ---
    autonomy_mode: AutonomyMode = AutonomyMode.SIGNAL_ONLY
    live_trading_enabled: bool = False
    live_trading_confirmation_phrase: SecretStr | None = None

    # ------------------------------------------------------- credentials ---
    binance_paper_api_key: SecretStr | None = None
    binance_paper_api_secret: SecretStr | None = None
    binance_live_api_key: SecretStr | None = None
    binance_live_api_secret: SecretStr | None = None

    # ------------------------------------------- risk bootstrap (seeding) ---
    # These seed the first RiskConfiguration row. After seeding, the database is
    # the single source of truth. See docs/RISK_RULES.md.
    bootstrap_reference_capital_ron: Decimal = Field(default=Decimal("1000.00"), gt=0)
    bootstrap_session_target_percent: Decimal = Field(default=Decimal("2.00"), gt=0, le=100)
    bootstrap_session_restart_threshold_percent: Decimal = Field(
        default=Decimal("4.00"), gt=0, le=100
    )
    bootstrap_daily_maximum_loss_percent: Decimal = Field(default=Decimal("4.00"), gt=0, le=100)
    bootstrap_maximum_risk_per_trade_percent: Decimal = Field(default=Decimal("0.50"), gt=0, le=100)
    bootstrap_maximum_open_positions: int = Field(default=1, ge=1)
    bootstrap_maximum_trades_per_day: int = Field(default=50, ge=1)
    bootstrap_maximum_consecutive_losses: int = Field(default=3, ge=1)
    bootstrap_no_new_entry_minutes_before_day_end: int = Field(default=30, ge=0, le=1440)
    #: None disables the daily profit floor entirely (Phase 0 decision OD-03).
    bootstrap_daily_profit_giveback_percent: Decimal | None = Field(default=None, ge=0, le=100)

    # ---------------------------------------------------------------- fx ---
    fx_rate_source: str = "BNR"
    #: Declared assumption, never a hidden constant. USDT is pegged to USD but
    #: is not USD. See docs/SRS.md section 5.
    usdt_usd_peg: Decimal = Field(default=Decimal("1.0000"), gt=0)

    # ------------------------------------------------------- validators ----

    @field_validator(*_EMPTY_MEANS_NONE, mode="before")
    @classmethod
    def _empty_string_becomes_none(cls, value: Any) -> Any:
        """Treat ``FOO=`` in .env as unset rather than as an empty value."""
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        if value not in VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(VALID_LOG_LEVELS))
            raise ValueError(f"log_level must be one of: {allowed}")
        return value

    @field_validator("reporting_currency", "exchange_quote_currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("trading_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Fail fast on an unknown timezone.

        A wrong timezone silently shifts every trading-day boundary, which would
        corrupt daily P&L accounting in a way that is very hard to notice later.
        """
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _validate_live_trading_guards(self) -> Settings:
        """Live mode requires every configuration guard, not just the mode.

        Guards 1-3 are enforced here. Guard 4, the runtime operator
        confirmation, is enforced in the execution layer in Phase 11.
        """
        if self.autonomy_mode is not AutonomyMode.LIVE_AUTOMATIC:
            return self

        missing: list[str] = []

        if not self.live_trading_enabled:
            missing.append("LIVE_TRADING_ENABLED must be true")

        phrase = self.live_trading_confirmation_phrase
        if phrase is None or phrase.get_secret_value() != LIVE_TRADING_EXPECTED_PHRASE:
            missing.append(
                "LIVE_TRADING_CONFIRMATION_PHRASE must be set to the exact expected phrase"
            )

        if self.binance_live_api_key is None or self.binance_live_api_secret is None:
            missing.append("BINANCE_LIVE_API_KEY and BINANCE_LIVE_API_SECRET must both be set")

        if missing:
            raise ValueError(
                "LIVE_AUTOMATIC mode refused. Unsatisfied guards: "
                + "; ".join(missing)
                + ". Live trading cannot be enabled by changing a single variable, by design."
            )
        return self

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> Settings:
        """Stricter rules once the environment claims to be production."""
        if self.app_env is not AppEnvironment.PRODUCTION:
            return self

        problems: list[str] = []

        if PLACEHOLDER_MARKER in self.database_url:
            problems.append("DATABASE_URL still contains a placeholder password")

        if self.log_level == "DEBUG":
            problems.append("LOG_LEVEL=DEBUG is forbidden in production (leak risk)")

        if problems:
            raise ValueError("Invalid production configuration: " + "; ".join(problems))
        return self

    # ------------------------------------------------ derived properties ---

    @property
    def cors_origins(self) -> list[str]:
        """Allowed browser origins, parsed from a comma-separated string."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def is_live_execution_configured(self) -> bool:
        """Whether configuration guards 1-3 for live trading are all satisfied.

        This is deliberately NOT called "live trading allowed". Guard 4, the
        runtime operator confirmation, is a separate gate in Phase 11.
        """
        return self.autonomy_mode is AutonomyMode.LIVE_AUTOMATIC and self.live_trading_enabled

    @property
    def bootstrap_session_target_ron(self) -> Decimal:
        return self._percent_of_reference(self.bootstrap_session_target_percent)

    @property
    def bootstrap_session_restart_threshold_ron(self) -> Decimal:
        return self._percent_of_reference(self.bootstrap_session_restart_threshold_percent)

    @property
    def bootstrap_daily_maximum_loss_ron(self) -> Decimal:
        return self._percent_of_reference(self.bootstrap_daily_maximum_loss_percent)

    @property
    def bootstrap_maximum_risk_per_trade_ron(self) -> Decimal:
        return self._percent_of_reference(self.bootstrap_maximum_risk_per_trade_percent)

    def _percent_of_reference(self, percent: Decimal) -> Decimal:
        """Convert a percentage of the fixed reference capital into RON.

        Rounds DOWN to the cent. Rounding a risk limit up, even by one cent,
        would allow slightly more risk than configured. The safe direction is
        always the one that reduces exposure.
        """
        raw = self.bootstrap_reference_capital_ron * percent / Decimal(100)
        return raw.quantize(_CENT, rounding=ROUND_DOWN)

    @property
    def database_password(self) -> str | None:
        """Password embedded in the database URL, if any.

        Driver errors frequently quote the whole DSN, password included. That
        text can end up in a log line or an error response, so the password is
        treated as a secret like any API key.
        """
        try:
            parsed = urlsplit(self.database_url)
        except ValueError:
            return None
        return unquote(parsed.password) if parsed.password else None

    def secret_values(self) -> list[str]:
        """Every secret value currently configured, for the log masker.

        Returned as plain strings on purpose: the masker needs the real values
        in order to redact them if they ever reach a log line.
        """
        candidates = (
            self.live_trading_confirmation_phrase,
            self.binance_paper_api_key,
            self.binance_paper_api_secret,
            self.binance_live_api_key,
            self.binance_live_api_secret,
        )
        values = [secret.get_secret_value() for secret in candidates if secret is not None]

        password = self.database_password
        if password is not None and password != PLACEHOLDER_MARKER:
            values.append(password)
        return values


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached so that configuration is read and validated exactly once. Tests build
    their own ``Settings`` instances instead of using this function.
    """
    return Settings()
