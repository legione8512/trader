"""Architecture tests.

Module boundaries are enforced by an automated check on the import graph, not
by convention or code review. See docs/ARCHITECTURE.md section 2.

The rules grow as modules appear. Today the domain-purity rule is enforceable;
the strategies-cannot-reach-execution rule (AC-19) is added in Phase 4, when
those modules exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

#: The pure domain layer may import only these first-party modules.
DOMAIN_ALLOWED_FIRST_PARTY = {"app.domain"}

#: Modules that must never appear anywhere under app/domain.
FORBIDDEN_IN_DOMAIN = {
    "sqlalchemy",
    "fastapi",
    "starlette",
    "alembic",
    "asyncpg",
    "httpx",
    "requests",
    "pydantic_settings",
    "structlog",
}


def python_files(package: str) -> list[Path]:
    return sorted((APP_ROOT / package).rglob("*.py"))


def imported_modules(path: Path) -> set[str]:
    """Every top-level module name imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


class TestDomainPurity:
    """app/domain performs no I/O and knows nothing about infrastructure.

    This is what makes the risk engine and the state machines testable without
    a database, an exchange or a network.
    """

    def test_the_domain_package_is_not_empty(self) -> None:
        assert python_files("domain"), "No domain modules found"

    @pytest.mark.parametrize("path", python_files("domain"), ids=lambda p: p.name)
    def test_no_infrastructure_imports(self, path: Path) -> None:
        for module in imported_modules(path):
            root = module.split(".")[0]
            assert root not in FORBIDDEN_IN_DOMAIN, (
                f"{path.name} imports {module}. The domain layer must stay pure."
            )

    @pytest.mark.parametrize("path", python_files("domain"), ids=lambda p: p.name)
    def test_no_first_party_imports_outside_the_domain(self, path: Path) -> None:
        for module in imported_modules(path):
            if not module.startswith("app."):
                continue
            package = ".".join(module.split(".")[:2])
            assert package in DOMAIN_ALLOWED_FIRST_PARTY, (
                f"{path.name} imports {module}. The domain layer may only import from app.domain."
            )


class TestMoneyIsNeverFloat:
    """AC-17: no float on the monetary path.

    A blanket ban on the token ``float`` would be crude; what matters is that
    no module converts a value to float. ``float(...)`` in a monetary context
    silently discards exactness.
    """

    @pytest.mark.parametrize(
        "path",
        [*python_files("domain"), *python_files("config"), *python_files("persistence")],
        ids=lambda p: f"{p.parent.name}/{p.name}",
    )
    def test_no_float_conversions(self, path: Path) -> None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "float", (
                    f"{path.name} line {node.lineno} calls float(). Monetary values "
                    f"must stay Decimal end to end."
                )
