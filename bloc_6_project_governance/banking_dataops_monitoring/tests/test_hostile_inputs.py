"""Inputs the system must refuse or must not let through: improbable scores, forbidden API operations, hostile values.

Each test names one forbidden path, tries it, and checks the refusal and what stayed untouched. The database is
the recording fake of ``tests/fakes.py``; nothing reaches a real PostgreSQL.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
from fakes import FakeDatabase
from fastapi.testclient import TestClient

from banking_dataops import api, audit, db, ingest
from banking_dataops.audit import RunJournal, _persist_event, assert_invariant
from banking_dataops.incident_report import write_incident_report
from banking_dataops.ml import features as F
from banking_dataops.ml.predict import priority_band

# ---------------------------------------------------------------- the priority band


@pytest.mark.parametrize(
    "score", [-0.0001, 1.0001, 2, -1, math.nan, math.inf, -math.inf, "0.5", None, [0.5], (0.5,), {"p": 0.5}]
)
def test_a_score_that_is_not_a_probability_is_refused_instead_of_being_filed_as_low(score: object) -> None:
    """FORBIDDEN PATH: a NaN compares false against both boundaries and would land in 'low', the quietest mistake."""
    with pytest.raises(ValueError) as refused:
        priority_band(score, 0.5)  # type: ignore[arg-type]

    assert str(refused.value) == f"Score {score!r} is not a probability in [0, 1]; refusing to assign a priority band."


@pytest.mark.parametrize("threshold", [0, 0.0, -0.1, 1.0001, 2, math.nan, math.inf, "0.7", None, [0.7]])
def test_a_threshold_outside_zero_one_is_refused_with_the_command_that_retrains(threshold: object) -> None:
    with pytest.raises(ValueError, match=r"is outside \(0, 1\]; the model artefact is corrupt, retrain with"):
        priority_band(0.5, threshold)  # type: ignore[arg-type]


@pytest.mark.parametrize(("score", "threshold"), [(0.0, 0.5), (1.0, 0.5), (0.5, 1.0), (0.3, 0.5), (1, 1)])
def test_the_edges_of_the_valid_range_are_accepted_by_the_same_guards(score: float, threshold: float) -> None:
    assert priority_band(score, threshold) in {"high", "medium", "low"}


# ---------------------------------------------------------------- the audit layer


def test_a_falsy_condition_that_is_not_a_boolean_still_violates_the_invariant(
    audit_log: list[tuple[audit.AuditEvent, object]],
) -> None:
    """FORBIDDEN PATH: an empty list or a zero passed as a condition being read as 'holds'."""
    for condition in (0, None, "", []):
        with pytest.raises(AssertionError, match="Invariant violated: the batch is not empty"):
            assert_invariant(condition, invariant="the batch is not empty", reason="because")  # type: ignore[arg-type]

    assert [event.action for event, _ in audit_log] == ["invariant_violated"] * 4


def test_an_interruption_of_the_operator_is_never_swallowed_by_the_journal_table_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The write is best effort for database errors only: Ctrl-C must still stop the process."""

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(db, "connect", interrupted)

    with pytest.raises(KeyboardInterrupt):
        _persist_event(audit.AuditEvent(category="data", action="x", reason="y"))


def test_an_interruption_inside_a_run_is_journalled_as_a_failed_run_and_still_propagates(
    audit_log: list[tuple[audit.AuditEvent, object]],
) -> None:
    with pytest.raises(KeyboardInterrupt), RunJournal("ingest"):
        raise KeyboardInterrupt

    assert audit_log[-1][0].action == "run_failed"
    assert audit_log[-1][0].reason == "step ingest raised KeyboardInterrupt"


# ---------------------------------------------------------------- the read-only API


@pytest.fixture
def client() -> TestClient:
    return TestClient(api.app, raise_server_exceptions=False)


def test_nobody_can_open_an_incident_through_the_api(client: TestClient, fake_database: FakeDatabase) -> None:
    """FORBIDDEN PATH: the API becoming a control plane (ADR-0006). Incidents are opened by a failed control only."""
    response = client.post("/incidents", json={"title": "forged", "severity": "high", "failed_control": "CTRL-001"})

    assert response.status_code == 405
    assert fake_database.find("INSERT") == []


def test_nobody_can_close_an_incident_through_the_api(client: TestClient, fake_database: FakeDatabase) -> None:
    patched = client.patch("/incidents/INC-1", json={"status": "closed"})
    deleted = client.delete("/incidents/INC-1")

    assert (patched.status_code, deleted.status_code) == (404, 404)
    assert fake_database.statements == []


def test_nobody_can_rerun_the_controls_through_the_api(client: TestClient, fake_database: FakeDatabase) -> None:
    response = client.post("/controls")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert fake_database.statements == []


def test_nobody_can_overwrite_the_audit_journal_through_the_api(
    client: TestClient, fake_database: FakeDatabase
) -> None:
    attempts = [client.put("/audit_events", json=[]), client.delete("/audit_events"), client.get("/audit_events")]

    assert [r.status_code for r in attempts] == [404, 404, 404]
    assert fake_database.statements == []


def test_a_forged_publication_header_does_not_change_the_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = pd.DataFrame(
        [
            {
                "control_id": "CTRL-003",
                "check_name": "invalid_amounts",
                "status": "FAIL",
                "failed_rows": 2,
                "severity": "high",
                "executed_at": pd.Timestamp("2026-07-27 09:00"),
            }
        ]
    )
    monkeypatch.setattr(api, "load_latest_quality_results", lambda: frame)

    plain = client.get("/verdict").json()
    forged = client.get(
        "/verdict", headers={"X-Can-Publish": "true", "X-Run-Id": "0" * 16, "Authorization": "Bearer x"}
    )

    assert forged.json() == plain
    assert plain["can_publish"] is False
    assert forged.headers["X-Run-Id"] != "0" * 16


# ---------------------------------------------------------------- values that must never reach a query text


def test_a_hostile_value_travels_as_a_parameter_and_leaves_the_query_text_unchanged(
    fake_database: FakeDatabase,
) -> None:
    """FORBIDDEN PATH: SQL injection. The text of the statement is the same whatever the value."""
    hostile = "x'; DROP TABLE audit_events; --"
    query = "SELECT * FROM incident_reports WHERE incident_id = %s"

    db.fetch_rows(query, params=(hostile,))
    db.fetch_rows(query, params=("INC-1",))

    first, second = fake_database.statements
    assert first.sql == second.sql == query
    assert first.params == (hostile,)
    assert "DROP TABLE" not in first.sql


def test_a_failing_read_changes_nothing_and_commits_nothing(fake_database: FakeDatabase) -> None:
    fake_database.fail_on("FROM missing_table", RuntimeError('relation "missing_table" does not exist'))

    with pytest.raises(RuntimeError, match="missing_table"):
        db.fetch_dataframe("SELECT * FROM missing_table")

    assert fake_database.commits == 0


def test_a_directory_given_as_a_sql_file_is_refused_before_any_connection(
    tmp_path: Path, fake_database: FakeDatabase
) -> None:
    with pytest.raises((IsADirectoryError, PermissionError)):
        db.execute_sql_file(tmp_path)

    assert fake_database.dsns == []


def test_a_directory_given_as_a_csv_file_is_refused_before_any_connection(
    tmp_path: Path, fake_database: FakeDatabase
) -> None:
    with pytest.raises((IsADirectoryError, PermissionError)):
        ingest._load_csv("customers", tmp_path)  # noqa: SLF001

    assert fake_database.dsns == []
    assert fake_database.copies == []


# ---------------------------------------------------------------- files and frames


def test_a_report_cannot_be_written_under_a_path_that_is_a_file_and_the_file_is_left_unchanged(tmp_path: Path) -> None:
    blocker = tmp_path / "output"
    blocker.write_text("not a folder", encoding="utf-8")

    with pytest.raises((FileExistsError, NotADirectoryError)):
        write_incident_report("# report\n", blocker / "incident_report.md")

    assert blocker.read_text(encoding="utf-8") == "not a folder"


def test_the_leak_guard_keeps_a_forbidden_column_out_of_the_features_even_when_the_frame_carries_it() -> None:
    built = F.build(
        pd.DataFrame(
            {
                "transaction_id": ["T1"],
                "amount_chf": [100.0],
                "channel": ["api"],
                "merchant_category": ["grocery"],
                "transaction_country": ["CH"],
                "source_system": ["core_banking"],
                "event_timestamp": [pd.Timestamp("2026-07-27 12:00")],
                "is_suspicious": [True],
                "customer_segment": ["retail"],
                "customer_country": ["CH"],
                "risk_score": [0.99],
                "account_id": ["A1"],
            }
        )
    )

    features, _ = F.split_x_y(built)

    assert not set(features.columns) & F.FORBIDDEN_FEATURES
    assert "risk_score" in built.columns
