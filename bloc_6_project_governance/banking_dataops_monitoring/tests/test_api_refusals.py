"""The decision API says no: writes, unknown routes, hostile parameters, cross-origin calls, database outages.

The API is read-only by design (ADR-0006). A forward test shows what it returns; these tests show what it
refuses, and that a refusal costs nothing: the database is not asked, the verdict does not move, the failure
is a 503 with a sentence, never a stack trace.

The database is replaced by ``fake_database`` (tests/fakes.py). The control results are replaced by a frame
built in the test, so no test depends on a PostgreSQL that may or may not be running.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import psycopg
import pytest
from fakes import FakeDatabase
from fastapi.testclient import TestClient

from banking_dataops import api

READ_ONLY_ROUTES = ["/health", "/ready", "/verdict", "/controls", "/incidents"]


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False)


def _results(*rows: tuple[str, str, int]) -> pd.DataFrame:
    now = datetime(2026, 7, 27, 9, 14, tzinfo=UTC).replace(tzinfo=None)
    return pd.DataFrame(
        [
            {
                "control_id": control,
                "check_name": control.lower().replace("-", "_"),
                "status": status,
                "failed_rows": failed,
                "severity": "high",
                "executed_at": now,
            }
            for control, status, failed in rows
        ]
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("route", READ_ONLY_ROUTES)
def test_write_methods_are_refused_with_405_and_name_the_allowed_method(
    client: TestClient, fake_database: FakeDatabase, method: str, route: str
) -> None:
    """FORBIDDEN PATH: a consumer changing state through a route that only reports.

    There is no POST, PUT, PATCH or DELETE route (ADR-0006). The framework answers 405 and says which method
    is allowed; the handler is never entered, so the database is never asked.
    """
    response = client.request(method, route, json={"can_publish": True})

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert fake_database.statements == []


def test_a_refused_write_leaves_the_published_verdict_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: forcing ``can_publish`` to true by posting a verdict.

    The verdict read before and after a rejected POST is identical, byte for byte.
    """
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: _results(("CTRL-003", "FAIL", 2)))

    before = client.get("/verdict").json()
    refused = client.post("/verdict", json={"can_publish": True, "status": "PASS"})
    after = client.get("/verdict").json()

    assert refused.status_code == 405
    assert before["can_publish"] is False
    assert after == before


@pytest.mark.parametrize(
    "path", ["/", "/admin", "/metrics", "/verdict/extra", "/controls/CTRL-001", "/incidents/INC-1", "/Health"]
)
def test_unknown_routes_answer_404_without_touching_the_database(
    client: TestClient, fake_database: FakeDatabase, path: str
) -> None:
    """FORBIDDEN PATH: a route nobody declared answering anything but 'not found'."""
    response = client.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert fake_database.statements == []


def test_query_parameters_leave_the_verdict_unchanged(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """FORBIDDEN PATH: a caller steering the decision from the URL.

    ``/verdict?can_publish=true&status=PASS`` is ignored: the answer is the one the persisted controls give.
    """
    monkeypatch.setattr(
        api, "load_latest_quality_results", lambda: _results(("CTRL-001", "PASS", 0), ("CTRL-005", "FAIL", 4))
    )

    plain = client.get("/verdict").json()
    steered = client.get("/verdict", params={"can_publish": "true", "status": "PASS", "failed_control_ids": ""}).json()

    assert steered == plain
    assert plain["can_publish"] is False
    assert plain["failed_control_ids"] == ["CTRL-005"]


def test_a_browser_origin_gets_no_cors_authorisation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """FORBIDDEN PATH: a web page on another origin reading the verdict.

    CORS is rejected on purpose (module docstring): the consumers are machines. No ``Access-Control-*``
    header is sent, so a browser blocks the call, and the preflight is refused.
    """
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: _results(("CTRL-001", "PASS", 0)))

    simple = client.get("/verdict", headers={"Origin": "https://evil.example"})
    preflight = client.options(
        "/verdict", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"}
    )

    assert simple.status_code == 200
    assert not [h for h in simple.headers if h.lower().startswith("access-control-")]
    assert preflight.status_code == 405
    assert not [h for h in preflight.headers if h.lower().startswith("access-control-")]


@pytest.mark.parametrize(
    ("route", "patched"),
    [
        ("/verdict", "load_latest_quality_results"),
        ("/controls", "load_latest_quality_results"),
        ("/ready", "fetch_rows"),
        ("/incidents", "fetch_rows"),
    ],
)
def test_a_database_outage_is_a_503_with_the_reason_and_no_stack_trace(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str, patched: str
) -> None:
    """FORBIDDEN PATH: a 500 and a Python traceback where the truth is 'the database is down'."""

    def down(*_args: Any, **_kwargs: Any) -> Any:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(api, patched, down)

    response = client.get(route)

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable: connection refused"}
    assert "Traceback" not in response.text


def test_verdict_without_any_executed_control_is_refused_with_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: reading an empty result as 'nothing failed' and releasing an unevaluated batch."""
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: pd.DataFrame())

    response = client.get("/verdict")

    assert response.status_code == 503
    assert response.json() == {"detail": "no control has been executed yet - run the quality pipeline first"}


def test_every_request_gets_its_own_run_identifier(client: TestClient) -> None:
    """FORBIDDEN PATH: two calls sharing one identifier, so a halt cannot be traced to one exact call."""
    first = client.get("/health").headers["X-Run-Id"]
    second = client.get("/health").headers["X-Run-Id"]
    refused = client.post("/health").headers["X-Run-Id"]

    assert re.fullmatch(r"[0-9a-f]{16}", first)
    identifiers = [first, second, refused]
    assert len(set(identifiers)) == len(identifiers)
