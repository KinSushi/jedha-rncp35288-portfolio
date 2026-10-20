#!/usr/bin/env python3
"""Tests for the read-only decision API.

WHY THIS MODULE EXISTS
----------------------
The API exposes the publication decision to downstream systems. If the contract
changes (response shape, status codes, error handling), pipelines break silently.
These tests verify the contract without requiring a database.

WHAT THIS MODULE TESTS
----------------------
1. test_health_does_not_touch_the_database() - Verifies /health endpoint works
2. test_verdict_allows_publication_when_everything_passes() - Verifies PASS scenario
3. test_verdict_blocks_publication_on_failure() - Verifies FAIL scenario
4. test_warning_still_allows_publication() - Verifies WARN scenario
5. test_verdict_returns_503_when_no_control_ran() - Verifies 503 when no data
6. test_controls_expose_every_row() - Verifies /controls endpoint

WHY THESE TESTS MATTER
----------------------
- Contract verification: Downstream systems depend on the exact response shape
- Status codes: 200 vs 503 determines whether a pipeline proceeds or halts
- Decision logic: can_publish must be True only when all checks pass

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify API responses, they don't modify data
- NO DATABASE: Tests use monkeypatch to stub the database layer
- DETERMINISTIC: Same input always produces same output
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from banking_dataops import api


@pytest.fixture
def client() -> TestClient:
    """Create a test client for the API."""

    return TestClient(api.app)


def _frame(statuses: list[tuple[str, str, int]]) -> pd.DataFrame:
    """Create a DataFrame that mimics database query results."""

    now = datetime.now(UTC).replace(tzinfo=None)
    return pd.DataFrame(
        [
            {
                "control_id": cid,
                "check_name": cid.lower().replace("-", "_"),
                "status": status,
                "failed_rows": rows,
                "severity": "high",
                "executed_at": now,
            }
            for cid, status, rows in statuses
        ]
    )


def test_health_does_not_touch_the_database(client: TestClient) -> None:
    """Test that /health endpoint works without database access."""

    assert client.get("/health").json() == {"status": "ok"}


def test_verdict_allows_publication_when_everything_passes(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that publication is allowed when all checks pass."""

    monkeypatch.setattr(api, "load_latest_quality_results", lambda: _frame([("CTRL-001", "PASS", 0)]))
    body = client.get("/verdict").json()
    assert body["can_publish"] is True
    assert body["status"] == "PASS"
    assert body["failed_control_ids"] == []
    assert body["rows_in_violation"] == 0


def test_verdict_blocks_publication_on_failure(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that publication is blocked when any check fails."""

    monkeypatch.setattr(
        api,
        "load_latest_quality_results",
        lambda: _frame([("CTRL-001", "PASS", 0), ("CTRL-003", "FAIL", 1), ("CTRL-006", "FAIL", 2)]),
    )
    body = client.get("/verdict").json()
    assert body["can_publish"] is False
    assert body["status"] == "FAIL"
    assert body["failed_control_ids"] == ["CTRL-003", "CTRL-006"]
    assert body["rows_in_violation"] == 3


def test_warning_still_allows_publication(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A warning is a reservation, not a block. The distinction matters
    downstream: WARN must not halt a pipeline."""

    monkeypatch.setattr(
        api,
        "load_latest_quality_results",
        lambda: _frame([("CTRL-001", "PASS", 0), ("CTRL-007", "WARN", 1)]),
    )
    body = client.get("/verdict").json()
    assert body["can_publish"] is True
    assert body["status"] == "WARN"


def test_verdict_returns_503_when_no_control_ran(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that 503 is returned when no controls ran."""

    monkeypatch.setattr(api, "load_latest_quality_results", lambda: pd.DataFrame())
    assert client.get("/verdict").status_code == 503


def test_controls_expose_every_row(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that /controls endpoint exposes all control results."""

    monkeypatch.setattr(
        api,
        "load_latest_quality_results",
        lambda: _frame([("CTRL-001", "PASS", 0), ("CTRL-003", "FAIL", 4)]),
    )
    body = client.get("/controls").json()
    assert [row["control_id"] for row in body] == ["CTRL-001", "CTRL-003"]
    assert [row["status"] for row in body] == ["PASS", "FAIL"]
