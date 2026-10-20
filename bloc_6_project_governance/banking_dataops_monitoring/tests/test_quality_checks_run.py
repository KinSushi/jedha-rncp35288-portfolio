"""The seven controls as they run (``banking_dataops.quality_checks``): the SQL sent, the verdict, the incidents.

``tests/test_quality_checks.py`` checks the definitions. These tests run ``run_quality_checks`` against the
recording fake database (``tests/fakes.py``) and read what it did: which statement for which control, which
rows persisted, which incident opened, which events journalled, and what it refused to do.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC

import pytest
from fakes import FakeDatabase

from banking_dataops import audit, quality_checks
from banking_dataops.config import Settings
from banking_dataops.quality_checks import (
    CONTROL_REASONS,
    QualityCheck,
    QualityCheckResult,
    get_quality_checks,
    print_quality_results,
    run_quality_checks,
    status_from_failed_rows,
)

pytestmark = pytest.mark.usefixtures("no_audit_database")

#: A fragment that appears in the SQL of one control only, so that the fake can answer it alone.
FRAGMENT = {
    "CTRL-001": "transaction_id IS NULL",
    "CTRL-002": "HAVING COUNT(*) > 1",
    "CTRL-003": "amount_chf <= 0",
    "CTRL-004": "LEFT JOIN accounts",
    "CTRL-005": "risk_score < 0",
    "CTRL-006": "status NOT IN",
    "CTRL-007": "INTERVAL '60 days'",
}

#: The declared controls: identifier, name, severity, whether the control only warns, and its SQL on one line.
DECLARED = [
    (
        "CTRL-001",
        "critical_nulls",
        "high",
        False,
        "SELECT COUNT(*) FROM transactions WHERE transaction_id IS NULL OR account_id IS NULL "
        "OR event_timestamp IS NULL OR amount_chf IS NULL",
    ),
    (
        "CTRL-002",
        "duplicate_transactions",
        "high",
        False,
        "SELECT COUNT(*) FROM ( SELECT transaction_id FROM transactions GROUP BY transaction_id "
        "HAVING COUNT(*) > 1 ) duplicates",
    ),
    (
        "CTRL-003",
        "invalid_amounts",
        "high",
        False,
        "SELECT COUNT(*) FROM transactions WHERE amount_chf <= 0 OR amount_chf > 1000000",
    ),
    (
        "CTRL-004",
        "orphan_transactions",
        "high",
        False,
        "SELECT COUNT(*) FROM transactions t LEFT JOIN accounts a ON t.account_id = a.account_id "
        "WHERE a.account_id IS NULL",
    ),
    (
        "CTRL-005",
        "invalid_risk_score",
        "medium",
        False,
        "SELECT COUNT(*) FROM transactions WHERE risk_score < 0 OR risk_score > 1",
    ),
    (
        "CTRL-006",
        "invalid_status",
        "medium",
        False,
        "SELECT COUNT(*) FROM transactions WHERE status NOT IN ('posted', 'pending', 'rejected')",
    ),
    (
        "CTRL-007",
        "stale_data",
        "low",
        True,
        "SELECT CASE WHEN MAX(event_timestamp) < CURRENT_TIMESTAMP - INTERVAL '60 days' THEN 1 ELSE 0 END "
        "FROM transactions",
    ),
]

INSERT_RESULT_SQL = (
    "INSERT INTO quality_check_results (check_id, control_id, check_name, status, failed_rows, severity, executed_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (check_id) DO UPDATE SET control_id = EXCLUDED.control_id, "
    "check_name = EXCLUDED.check_name, status = EXCLUDED.status, failed_rows = EXCLUDED.failed_rows, "
    "severity = EXCLUDED.severity, executed_at = EXCLUDED.executed_at"
)
INSERT_INCIDENT_SQL = (
    "INSERT INTO incident_reports (incident_id, title, severity, status, failed_control, summary, created_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (incident_id) DO NOTHING"
)


def _flat(sql: str) -> str:
    return " ".join(sql.split())


def _script(database: FakeDatabase, **counts: int | None) -> None:
    """Make each control return the count given for it (``CTRL_003=3``); the others return 0."""
    for control, fragment in FRAGMENT.items():
        database.answer(fragment, rows=[(counts.get(control.replace("-", "_"), 0),)])


def _actions(audit_log: list[tuple[audit.AuditEvent, object]]) -> list[str]:
    return [event.action for event, _ in audit_log]


# ---------------------------------------------------------------- the definitions, word for word


def test_the_seven_controls_are_declared_with_their_exact_sql_and_nothing_can_be_edited_after_the_fact() -> None:
    declared = [(c.control_id, c.check_name, c.severity, c.warn_only, _flat(c.query)) for c in get_quality_checks()]
    assert declared == DECLARED

    assert set(CONTROL_REASONS) == {c.control_id for c in get_quality_checks()}
    assert all(len(reason) > 60 for reason in CONTROL_REASONS.values())

    # A count maps to PASS, WARN or FAIL; a control blocks unless it was declared warn-only, so that forgetting the
    # flag must not downgrade a failure.
    for failed_rows, warn_only, expected in [
        (0, False, "PASS"),
        (0, True, "PASS"),
        (1, False, "FAIL"),
        (7, False, "FAIL"),
        (1, True, "WARN"),
        (7, True, "WARN"),
    ]:
        assert status_from_failed_rows(failed_rows, warn_only) == expected
        assert status_from_failed_rows(failed_rows, warn_only=warn_only) == expected
    assert status_from_failed_rows(3) == "FAIL"

    # FORBIDDEN PATH: a rule changed at run time by accident, or a result edited once computed (the dataclasses are
    # frozen).
    check = get_quality_checks()[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        check.severity = "low"  # type: ignore[misc]
    result = QualityCheckResult("CTRL-001", "critical_nulls", "PASS", 0, "high", executed_at=None)  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "FAIL"  # type: ignore[misc]


# ---------------------------------------------------------------- a batch where every control passes


def _blank_page(database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]]) -> None:
    """Forget what the fake database recorded and scripted, and what was journalled."""
    database.forget()
    database.answers.clear()
    audit_log.clear()


def test_a_clean_batch_passes_persists_and_journals_every_control_and_a_control_that_answers_nothing_stops_the_run(
    fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]], settings: Settings
) -> None:
    _script(fake_database)
    results = run_quality_checks(settings)

    assert [(r.control_id, r.status, r.failed_rows) for r in results] == [
        (control, "PASS", 0) for control, *_ in DECLARED
    ]
    assert [_flat(s.sql) for s in fake_database.statements if s.sql.lstrip().startswith("SELECT")] == [
        sql for *_, sql in DECLARED
    ]
    assert fake_database.find("incident_reports") == []
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn]

    # Every result is persisted under its control identifier, with one shared timestamp.
    inserts = fake_database.find("INSERT INTO quality_check_results")
    assert [_flat(s.sql) for s in inserts] == [INSERT_RESULT_SQL] * 7
    assert [s.params for s in inserts] == [
        (r.control_id, r.control_id, r.check_name, "PASS", 0, r.severity, r.executed_at) for r in results
    ]
    assert len({r.executed_at for r in results}) == 1
    assert results[0].executed_at.tzinfo is UTC

    # One event per control, then the verdict.
    events = [event for event, _ in audit_log]
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        *[("control", "control_pass", 0)] * 7,
        ("integrity", "invariant_checked", None),
        ("decision", "verdict_issued", 0),
        ("data", "controls_completed", 7),
        ("pipeline", "run_succeeded", None),
    ]
    controls = events[1:8]
    assert [e.reason for e in controls] == [CONTROL_REASONS[c] for c, *_ in DECLARED]
    assert [(e.details["control_id"], e.details["check_name"], e.details["severity"]) for e in controls] == [
        (c, name, severity) for c, name, severity, *_ in DECLARED
    ]
    invariant, verdict, completed = events[8], events[9], events[10]
    assert invariant.details["invariant"] == "one result per declared control"
    assert (invariant.details["declared"], invariant.details["produced"]) == (7, 7)
    assert verdict.reason == (
        "the publication decision for this batch, derived only from the deterministic controls "
        "— never from a model score (ADR-0002)"
    )
    assert (verdict.details["verdict"], verdict.details["controls_total"], verdict.details["controls_failed"]) == (
        "PASS",
        7,
        0,
    )
    assert completed.reason == "every declared control was evaluated and its outcome persisted"
    assert completed.details["verdict"] == "PASS"
    assert all(given is settings for event, given in audit_log if event.category != "integrity")

    # A null count is read as zero failed rows.
    _blank_page(fake_database, audit_log)
    for fragment in FRAGMENT.values():
        fake_database.answer(fragment, rows=[(None,)])
    assert {(r.status, r.failed_rows) for r in run_quality_checks(settings)} == {("PASS", 0)}

    # FORBIDDEN PATH: a control that answered nothing being read as 'zero failures'. The run stops before anything is
    # committed, and no verdict is issued.
    _blank_page(fake_database, audit_log)
    fake_database.answer("transaction_id IS NULL", rows=[])
    with pytest.raises(AssertionError) as refused:
        run_quality_checks(settings)
    assert str(refused.value) == "a COUNT or CASE query always returns one row"
    assert fake_database.commits == 0
    assert fake_database.find("INSERT INTO quality_check_results") == []
    assert _actions(audit_log)[-1] == "run_failed"
    assert "verdict_issued" not in _actions(audit_log)


# ---------------------------------------------------------------- a batch where controls fail or warn


def test_a_failure_is_a_verdict_with_an_incident_each_a_warning_opens_none_and_a_down_database_leaves_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """A failure is a verdict, journalled and shown: it does not raise. A database that is down raises."""
    _script(fake_database, CTRL_003=3, CTRL_005=1)
    results = run_quality_checks(settings)

    assert {r.control_id: (r.status, r.failed_rows) for r in results if r.status != "PASS"} == {
        "CTRL-003": ("FAIL", 3),
        "CTRL-005": ("FAIL", 1),
    }
    incidents = fake_database.find("INSERT INTO incident_reports")
    assert [_flat(s.sql) for s in incidents] == [INSERT_INCIDENT_SQL] * 2
    assert [(p[1], p[2], p[3], p[4], p[5]) for p in (s.params for s in incidents)] == [
        ("Failed data control: invalid_amounts", "high", "open", "CTRL-003", "CTRL-003 failed with 3 row(s)."),
        ("Failed data control: invalid_risk_score", "medium", "open", "CTRL-005", "CTRL-005 failed with 1 row(s)."),
    ]
    assert all(re.fullmatch(r"INC-[0-9A-F]{12}", s.params[0]) for s in incidents)
    assert incidents[0].params[0] != incidents[1].params[0]
    assert incidents[0].params[6] == results[2].executed_at
    assert fake_database.commits == 1

    # The failed batch is journalled with its incidents and a FAIL verdict counting the failed rows; the incidents
    # follow the settings of the run.
    _blank_page(fake_database, audit_log)
    _script(fake_database, CTRL_003=3, CTRL_005=1, CTRL_007=9)
    run_quality_checks(settings)
    events = [event for event, _ in audit_log]
    opened = [e for e in events if e.action == "incident_opened"]
    assert [(e.category, e.row_count, e.details["control_id"]) for e in opened] == [
        ("decision", 3, "CTRL-003"),
        ("decision", 1, "CTRL-005"),
    ]
    assert opened[0].reason == (
        "a failed control blocks publication; the incident is the object an analyst works from, "
        "and the trace an auditor reads"
    )
    assert re.fullmatch(r"INC-[0-9A-F]{12}", str(opened[0].details["incident_id"]))
    verdict = next(e for e in events if e.action == "verdict_issued")
    # The 9 rows of the warn-only control do not count: only FAIL rows are in violation.
    assert verdict.row_count == 4
    assert (verdict.details["verdict"], verdict.details["controls_failed"]) == ("FAIL", 2)
    assert [e.action for e in events if e.category == "control"] == [
        "control_pass",
        "control_pass",
        "control_fail",
        "control_pass",
        "control_fail",
        "control_pass",
        "control_warn",
    ]
    assert [given for event, given in audit_log if event.action == "incident_opened"] == [settings, settings]

    # FORBIDDEN PATH: a stale-data warning raising an incident that stops a valid batch.
    _blank_page(fake_database, audit_log)
    _script(fake_database, CTRL_007=1)
    warned = run_quality_checks(settings)
    assert warned[-1].status == "WARN"
    assert fake_database.find("incident_reports") == []
    warning_verdict = next(event for event, _ in audit_log if event.action == "verdict_issued")
    assert warning_verdict.details["verdict"] == "WARN"
    assert warning_verdict.row_count == 0

    # An undeclared control is journalled with the fallback reason.
    _blank_page(fake_database, audit_log)
    extra = QualityCheck("CTRL-099", "extra_rule", "low", "SELECT COUNT(*) FROM extra_table", warn_only=False)
    fake_database.answer("extra_table", rows=[(2,)])
    with monkeypatch.context() as patch:
        patch.setattr(quality_checks, "get_quality_checks", lambda: [extra])
        undeclared = run_quality_checks(settings)
    assert [(r.control_id, r.status, r.failed_rows) for r in undeclared] == [("CTRL-099", "FAIL", 2)]
    control_event = next(event for event, _ in audit_log if event.category == "control")
    assert control_event.reason == "control evaluated; no business reason declared for this identifier"
    assert control_event.action == "control_fail"

    # Without explicit settings the environment settings are used.
    _blank_page(fake_database, audit_log)
    _script(fake_database)
    with monkeypatch.context() as patch:
        patch.setattr(quality_checks, "load_settings", lambda: settings)
        run_quality_checks()
    assert fake_database.dsns == [settings.dsn]
    assert all(given is settings for event, given in audit_log if event.category != "integrity")

    # FORBIDDEN PATH: issuing a verdict when the controls could not run.
    _blank_page(fake_database, audit_log)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("database is down")

    monkeypatch.setattr(quality_checks, "connect", refuse)
    with pytest.raises(ConnectionError, match="database is down"):
        run_quality_checks(settings)
    failed = audit_log[-1][0]
    assert failed.action == "run_failed"
    assert failed.reason == "step quality_checks raised ConnectionError"
    assert "verdict_issued" not in _actions(audit_log)


# ---------------------------------------------------------------- the command line and the completeness rule


class _ShrunkList(list):  # type: ignore[type-arg]
    """A list of controls that claims to hold one more control than it yields."""

    def __len__(self) -> int:
        return super().__len__() + 1


def test_the_results_are_printed_one_aligned_line_each_and_a_missing_control_result_is_refused_with_its_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_database: FakeDatabase, settings: Settings
) -> None:
    results = [
        QualityCheckResult("CTRL-001", "critical_nulls", "PASS", 0, "high", executed_at=None),  # type: ignore[arg-type]
        QualityCheckResult("CTRL-003", "invalid_amounts", "FAIL", 3, "high", executed_at=None),  # type: ignore[arg-type]
    ]
    print_quality_results(results)
    assert capsys.readouterr().out == (
        "CTRL-001 | PASS | high | critical_nulls | failed=0\nCTRL-003 | FAIL | high | invalid_amounts | failed=3\n"
    )

    single = QualityCheckResult("CTRL-006", "invalid_status", "PASS", 0, "medium", executed_at=None)  # type: ignore[arg-type]
    with monkeypatch.context() as patch:
        patch.setattr(quality_checks, "run_quality_checks", lambda: [single])
        quality_checks.main()
    assert capsys.readouterr().out == "CTRL-006 | PASS | medium | invalid_status | failed=0\n"

    # FORBIDDEN PATH: a verdict that looks complete while part of the batch was never evaluated.
    monkeypatch.setattr(quality_checks, "get_quality_checks", lambda: _ShrunkList(get_quality_checks()))
    _script(fake_database)
    with pytest.raises(AssertionError) as refused:
        run_quality_checks(settings)
    assert str(refused.value) == (
        "Invariant violated: one result per declared control. a missing control result would make the verdict "
        "look complete while part of the batch was never evaluated"
    )
