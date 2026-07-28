"""Structured logging configuration.

Logs are events with fields, not sentences. ``event="order_filled",
order_id=42`` can be filtered, correlated and replayed; ``"Order 42 filled"``
cannot. This is what makes the audit requirement achievable from logs as well
as from the database.

Every log record passes through a masking processor before rendering. See
``app.core.secrets``.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

from app.core.secrets import mask_mapping, secret_registry
from app.domain.enums import AppEnvironment


def mask_secrets_processor(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor that redacts secrets from every log event."""
    masked: dict[str, Any] = mask_mapping(dict(event_dict))
    return masked


def configure_logging(
    *,
    log_level: str = "INFO",
    environment: AppEnvironment = AppEnvironment.DEVELOPMENT,
) -> None:
    """Configure structlog and the standard library logging bridge.

    Development renders colourised, human-readable lines. Every other
    environment renders JSON, which is what log aggregators need.
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Masking runs last, so it also covers fields added by the processors
        # above, including formatted exception text.
        mask_secrets_processor,
    ]

    renderer: structlog.typing.Processor
    if environment is AppEnvironment.DEVELOPMENT:
        renderer = structlog.dev.ConsoleRenderer(colors=True)
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route uvicorn / third-party stdlib logging through the same stream so a
    # single log pipeline covers everything.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )


def register_configured_secrets(secret_values: list[str]) -> int:
    """Load the configured secrets into the masking registry.

    Returns how many values were accepted. Called once at startup, before the
    first log line that could contain a credential.
    """
    return secret_registry.register_all(secret_values)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
