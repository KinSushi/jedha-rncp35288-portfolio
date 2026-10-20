"""The decision API (``banking_dataops.api``): the exact bodies, the SQL of the two direct reads, the request trace.

``tests/test_api.py`` checks the verdict logic and ``tests/test_api_refusals.py`` what the API refuses. These
tests pin the rest of the contract a downstream pipeline relies on: every field of every body, the log record
of each request (what an operator reads when a consumer reports a halt), and the queries the API sends itself.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from fakes import FakeDatabase
from fastapi.testclient import TestClient

from banking_dataops import api

EXECUTED_AT = datetime(2026, 7, 27, 9, 14, 5, tzinfo=UTC).replace(tzinfo=None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app)


def _results(*rows: tuple[str, str, int, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "control_id": control,
                "check_name": control.lower().replace("-", "_"),
                "status": status,
                "failed_rows": failed,
                "severity": severity,
                "executed_at": EXECUTED_AT,
            }
            for control, status, failed, severity in rows
        ]
    )


# ---------------------------------------------------------------- the bodies


def test_the_five_routes_return_their_exact_bodies_and_leave_the_database_unchanged(
    client: TestClient, fake_database: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GET reports and never writes: whatever the route, nothing is committed and only reads are sent."""
    monkeypatch.setattr(
        api,
        "load_latest_quality_results",
        lambda: _results(
            ("CTRL-006", "FAIL", 2, "medium"),
            ("CTRL-001", "PASS", 0, "high"),
            ("CTRL-003", "FAIL", 3, "high"),
            ("CTRL-007", "WARN", 9, "low"),
        ),
    )
    fake_database.answer(
        "FROM incident_reports",
        rows=[
            ("INC-1", "Failed data control: invalid_amounts", "high", "CTRL-003", EXECUTED_AT),
            ("INC-2", "Manual incident", "low", None, EXECUTED_AT),
        ],
        columns=["incident_id", "title", "severity", "failed_control", "created_at"],
    )

    verdict = client.get("/verdict")
    assert verdict.status_code == 200
    assert verdict.json() == {
        "can_publish": False,
        "status": "FAIL",
        "controls_total": 4,
        "controls_passed": 1,
        "controls_failed": 2,
        "controls_warning": 1,
        "rows_in_violation": 5,
        "failed_control_ids": ["CTRL-003", "CTRL-006"],
        "evaluated_at": "2026-07-27T09:14:05",
    }
    assert [row["control_id"] for row in client.get("/controls").json()] == [
        "CTRL-006",
        "CTRL-001",
        "CTRL-003",
        "CTRL-007",
    ]
    assert client.get("/controls").json()[1] == {
        "control_id": "CTRL-001",
        "check_name": "ctrl_001",
        "status": "PASS",
        "failed_rows": 0,
        "severity": "high",
        "executed_at": "2026-07-27T09:14:05",
    }
    incidents = client.get("/incidents").json()
    assert incidents[0] == {
        "incident_id": "INC-1",
        "title": "Failed data control: invalid_amounts",
        "severity": "high",
        "failed_control": "CTRL-003",
        "created_at": "2026-07-27T09:14:05",
    }
    assert incidents[1]["failed_control"] is None
    assert client.get("/ready").json() == {"status": "ready"}
    assert client.get("/health").json() == {"status": "ok"}

    # The incidents query: the fifty newest open incidents, nothing bound. The probe asks for one row.
    sent = [(" ".join(s.sql.split()), s.params) for s in fake_database.statements]
    assert sent == [
        (
            "SELECT incident_id, title, severity, failed_control, created_at FROM incident_reports "
            "WHERE status = 'open' ORDER BY created_at DESC LIMIT 50",
            None,
        ),
        ("SELECT 1 AS ok", None),
    ]
    assert fake_database.commits == 0
    assert len(fake_database.dsns) == len(sent)  # /health opened no connection

    # Rows of a warning are not rows in violation; the evaluation time is the latest execution.
    frame = _results(("CTRL-001", "PASS", 0, "high"), ("CTRL-007", "WARN", 9, "low"))
    frame.loc[1, "executed_at"] = datetime(2026, 7, 28, 1, 2, 3)
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: frame)
    body = client.get("/verdict").json()
    assert (body["can_publish"], body["status"], body["rows_in_violation"]) == (True, "WARN", 0)
    assert body["failed_control_ids"] == []
    assert body["evaluated_at"] == "2026-07-28T01:02:03"

    # The application declares what it is.
    assert api.app.title == "Banking DataOps Monitoring API"
    assert api.app.version == "1.0.0"
    assert api.app.description == (
        "Read-only publication verdict for downstream systems. "
        "Synthetic data only, not connected to production systems."
    )
    assert {route.path for route in api.app.routes if "GET" in getattr(route, "methods", set())} >= {
        "/health",
        "/ready",
        "/verdict",
        "/controls",
        "/incidents",
    }


# ---------------------------------------------------------------- the request trace


def test_every_request_is_logged_with_its_method_path_status_and_duration_and_the_decision_with_its_run(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, log_records: list
) -> None:
    client.get("/health")
    (record,) = [r for r in log_records if r.getMessage() == "http_request"]
    assert (record.levelname, record.name) == ("INFO", "banking_dataops.api")
    assert (record.method, record.path, record.status) == ("GET", "/health", 200)
    assert record.category == "data"
    assert record.reason == "read-only query on /health"
    assert isinstance(record.duration_ms, int)
    assert 0 <= record.duration_ms < 5000

    # A verdict request is a decision; what was served carries the run identifier of that exact call.
    log_records.clear()
    monkeypatch.setattr(
        api,
        "load_latest_quality_results",
        lambda: _results(("CTRL-001", "PASS", 0, "high"), ("CTRL-003", "FAIL", 1, "high")),
    )
    response = client.get("/verdict")
    (decision,) = [r for r in log_records if r.getMessage() == "http_request"]
    assert (decision.category, decision.path) == ("decision", "/verdict")
    assert decision.reason == "a downstream pipeline queried the publication decision"
    (served,) = [r for r in log_records if r.getMessage() == "verdict_served"]
    assert (served.levelname, served.category) == ("INFO", "decision")
    assert (served.can_publish, served.status) == (False, "FAIL")
    assert served.run_id == response.headers["X-Run-Id"]
    assert served.reason == (
        "publication decision returned to a machine consumer; it reports the controls' verdict "
        "and never a model score (ADR-0002)"
    )

    # A server error is logged at warning, a client error at info.
    log_records.clear()
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: pd.DataFrame())
    client.get("/verdict")
    client.get("/nowhere")
    levels = {r.status: r.levelname for r in log_records if r.getMessage() == "http_request"}
    assert levels == {503: "WARNING", 404: "INFO"}


# ---------------------------------------------------------------- the middleware, called directly


class _Response:
    """A response with a plain dictionary for headers: the exact spelling of a header name matters here."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}


def _call_middleware(
    monkeypatch: pytest.MonkeyPatch, *, status: int = 200, readings: tuple[float, float] = (0.0, 0.0)
) -> tuple[Any, Any, _Response]:
    """Run ``trace_requests`` on a fake request; return (request given, request seen by call_next, response)."""
    readings_left = list(readings)
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: readings_left.pop(0)))
    request = SimpleNamespace(url=SimpleNamespace(path="/health"), method="GET")
    seen: list[Any] = []
    response = _Response(status)

    async def call_next(received: Any) -> _Response:
        seen.append(received)
        return response

    returned = asyncio.run(api.trace_requests(request, call_next))
    assert returned is response
    return request, (seen[0] if seen else None), response


def test_the_middleware_hands_the_request_on_stamps_the_run_header_measures_milliseconds_and_warns_from_500(
    monkeypatch: pytest.MonkeyPatch, log_records: list
) -> None:
    request, seen, response = _call_middleware(monkeypatch, readings=(100.0, 101.5))

    assert seen is request
    assert list(response.headers) == ["X-Run-Id"]
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["X-Run-Id"])
    (record,) = [r for r in log_records if r.getMessage() == "http_request"]
    assert record.duration_ms == 1500

    for status, level in ((200, "INFO"), (404, "INFO"), (499, "INFO"), (500, "WARNING"), (503, "WARNING")):
        log_records.clear()
        _call_middleware(monkeypatch, status=status)
        (record,) = [r for r in log_records if r.getMessage() == "http_request"]
        assert (record.levelname, record.status, record.method) == (level, status, "GET")
