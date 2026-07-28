"""Shared test fixtures.

Tests must never depend on the developer's local ``.env`` or on ambient
environment variables. A test that passes on one machine and fails on another
because of a stray variable is worse than no test at all.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import SettingsConfigDict

from app.config.settings import Settings
from app.core.secrets import secret_registry
from app.domain.enums import AppEnvironment
from app.main import create_app


class IsolatedSettings(Settings):
    """Settings that ignore ``.env`` entirely.

    Only explicit keyword arguments and (cleared) environment variables apply.
    """

    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every settings variable from the ambient environment."""
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
        monkeypatch.delenv(field_name.lower(), raising=False)


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    """Ensure secrets registered by one test never leak into the next."""
    secret_registry.clear()
    yield
    secret_registry.clear()


@pytest.fixture
def make_settings() -> Callable[..., Settings]:
    """Factory producing isolated settings with test-friendly defaults."""

    def _factory(**overrides: Any) -> Settings:
        values: dict[str, Any] = {"app_env": AppEnvironment.TEST}
        values.update(overrides)
        return IsolatedSettings(**values)

    return _factory


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
