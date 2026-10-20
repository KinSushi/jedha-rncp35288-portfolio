#!/usr/bin/env python3
"""Test suite for configuration management.

WHY THIS MODULE EXISTS
----------------------
Configuration is the foundation of the entire application. If settings are wrong,
everything downstream fails in unpredictable ways. These tests verify that:
1. Default values are correct and consistent
2. Dependency declarations don't drift between files

WHAT THIS MODULE TESTS
----------------------
1. test_settings_defaults() - Verifies that Settings() initializes with correct defaults, and that a value
   which is not of the field's type is refused instead of replaced by a default
2. test_requirements_txt_matches_pyproject() - Ensures requirements.txt and pyproject.toml
   declare the same dependencies (prevents silent divergence), and that the check itself refuses a drift

WHY THESE TESTS MATTER
----------------------
- Default values: If database_host defaults to "localhost" instead of "postgres" in Docker,
  the application fails to connect. This test catches that immediately.
- Dependency sync: requirements.txt and pyproject.toml can drift apart silently. A developer
  might add a package to one but forget the other. This test prevents that.

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only read configuration files, they don't modify anything
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from banking_dataops.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def _check_mirror(pyproject: str, requirements: str) -> None:
    """Raise ``AssertionError`` naming the dependencies that only one of the two files declares."""
    # Match up to a line that is exactly "]". A non-greedy [^]]* would stop at
    # the first bracket inside "psycopg[binary]" and capture nothing usable —
    # which is exactly what this test caught on its first run.
    block = re.search(r"^dependencies = \[\n(.*?)^\]", pyproject, re.S | re.M)
    assert block, "no dependencies block in pyproject.toml"
    declared = set(re.findall(r'"([^"]+)"', block.group(1)))
    mirrored = {line.strip() for line in requirements.splitlines() if line.strip() and not line.startswith("#")}
    assert declared == mirrored, (
        "requirements.txt and pyproject.toml disagree.\n"
        f"  only in pyproject:    {sorted(declared - mirrored)}\n"
        f"  only in requirements: {sorted(mirrored - declared)}"
    )


def test_settings_defaults() -> None:
    """Test that Settings() initializes with correct default values."""

    settings = Settings()
    # En Docker, host = "postgres"
    assert settings.database_host in ("localhost", "postgres")
    assert settings.database_port == 5432
    assert settings.database_name == "banking_dataops"
    assert "user=dataops" in settings.dsn

    # FORBIDDEN PATH: a value that is not a port is refused, not replaced by the default.
    with pytest.raises(ValidationError, match="database_port"):
        Settings(database_port="not-a-port")  # type: ignore[arg-type]


def test_requirements_txt_matches_pyproject() -> None:
    """The two dependency declarations must not drift.

    requirements.txt exists because the brief names it and because reviewers
    type `pip install -r` first. pyproject.toml is the source of truth. Two
    files declaring the same thing diverge silently unless something checks,
    and a check that cannot fail proves nothing: it is also given a requirements
    file with a line missing and one with a line too many, and must refuse both.
    """

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    _check_mirror(pyproject, requirements)

    lines = requirements.splitlines()
    first = next(i for i, line in enumerate(lines) if line.strip() and not line.startswith("#"))
    with pytest.raises(AssertionError, match="only in pyproject"):
        _check_mirror(pyproject, "\n".join(lines[:first] + lines[first + 1 :]))
    with pytest.raises(AssertionError, match="only in requirements"):
        _check_mirror(pyproject, requirements + "\nnot-a-dependency-of-the-project==1.0\n")
