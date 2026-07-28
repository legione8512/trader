"""Secret redaction for logs and diagnostics.

Pydantic's ``SecretStr`` prevents accidental printing of a secret *object*. It
does not help once someone has called ``.get_secret_value()`` and put the raw
string into a log line, an exception message or an HTTP error body.

This module is the second layer of defence: a registry of known secret values
and a masker that redacts them wherever they appear.

Two independent strategies are used, because either one alone has a gap:

* **By key name** - any field whose name looks sensitive is redacted, even if
  its value was never registered.
* **By value** - any registered secret value is redacted, even if it appears
  inside an innocuous-looking field such as a URL or an error message.
"""

from __future__ import annotations

from typing import Any

REDACTED: str = "***REDACTED***"

#: Substrings that make a field name sensitive. Matched case-insensitively.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "secret",
    "api_key",
    "apikey",
    "token",
    "password",
    "passwd",
    "passphrase",
    "confirmation_phrase",
    "authorization",
    "credential",
    "signature",
    "private",
    "cookie",
)

#: Values shorter than this are never registered. Registering a short or empty
#: string would redact unrelated text all over the logs.
MINIMUM_SECRET_LENGTH: int = 8


class SecretRegistry:
    """Holds the secret values that must never appear in output."""

    def __init__(self) -> None:
        self._values: set[str] = set()

    def register(self, value: str | None) -> bool:
        """Register one secret value. Returns whether it was accepted."""
        if value is None:
            return False
        stripped = value.strip()
        if len(stripped) < MINIMUM_SECRET_LENGTH:
            return False
        self._values.add(stripped)
        return True

    def register_all(self, values: object) -> int:
        """Register several values. Returns how many were accepted."""
        if not isinstance(values, (list, tuple, set, frozenset)):
            return 0
        return sum(1 for value in values if isinstance(value, str) and self.register(value))

    def clear(self) -> None:
        self._values.clear()

    @property
    def size(self) -> int:
        return len(self._values)

    def mask_text(self, text: str) -> str:
        """Replace every registered secret value found inside ``text``."""
        masked = text
        # Longest first, so that a secret containing another secret is fully
        # replaced instead of being partially mangled.
        for value in sorted(self._values, key=len, reverse=True):
            if value in masked:
                masked = masked.replace(value, REDACTED)
        return masked


#: Process-wide registry. Populated at startup from the loaded settings.
secret_registry = SecretRegistry()


def is_sensitive_key(key: str) -> bool:
    """Whether a field name looks like it holds a secret."""
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def mask_value(key: str, value: Any, registry: SecretRegistry | None = None) -> Any:
    """Redact one key/value pair, recursing into containers.

    A sensitive key name redacts the value outright. Otherwise the value is
    scanned for registered secret substrings.
    """
    active = secret_registry if registry is None else registry

    if is_sensitive_key(key):
        return REDACTED

    if isinstance(value, str):
        return active.mask_text(value)

    if isinstance(value, dict):
        return {
            inner_key: mask_value(str(inner_key), inner_value, active)
            for inner_key, inner_value in value.items()
        }

    if isinstance(value, (list, tuple)):
        masked = [mask_value(key, item, active) for item in value]
        return tuple(masked) if isinstance(value, tuple) else masked

    return value


def mask_mapping(data: dict[str, Any], registry: SecretRegistry | None = None) -> dict[str, Any]:
    """Redact an entire mapping. Used by the structlog processor."""
    return {key: mask_value(key, value, registry) for key, value in data.items()}
