"""The audit layer (``banking_dataops.audit``): fingerprints, events, the run frame, the invariants, the queries.

``tests/test_reverse_paths.py`` already closes the main forbidden paths of the journal (an event without a
reason, an unknown category, a violated invariant). These tests pin the rest of the contract: the exact words
of the refusals, the fields of every record, what is sent to PostgreSQL, and what the layer swallows on
purpose (a journal table that is unreachable) versus what it never swallows (the pipeline's own exception).

Each test takes one unit of the module and puts the cases it accepts and the cases it refuses side by side.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fakes import FakeDatabase
from helpers import bounded

from banking_dataops import audit, logging_config
from banking_dataops import db as db_module
from banking_dataops.audit import (
    EVENT_CATEGORIES,
    AuditEvent,
    RunJournal,
    _as_json,
    _persist_event,
    _safe_run_id,
    assert_invariant,
    audit_trail,
    code_version,
    fingerprint_bytes,
    fingerprint_directory,
    fingerprint_file,
    iter_categories,
    record_event,
    runtime_context,
    summarise_runs,
)
from banking_dataops.config import Settings
from banking_dataops.logging_config import _RUN_ID, _STEP

INSERT_EVENT_SQL = (
    "INSERT INTO audit_events (run_id, category, action, reason, row_count, details, occurred_at) "
    "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)"
)


@pytest.fixture
def no_run() -> Iterator[None]:
    """Start the test with no run identifier and no step, and restore the previous ones afterwards."""
    run_token = _RUN_ID.set("")
    step_token = _STEP.set("")
    try:
        yield
    finally:
        _RUN_ID.reset(run_token)
        _STEP.reset(step_token)


class _Stopwatch:
    """A monotonic clock that answers the values it was given, one per call."""

    def __init__(self, *readings: float) -> None:
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0)


# ---------------------------------------------------------------- fingerprints


def test_fingerprints_are_sha256_digests_read_in_64_kib_blocks_over_sorted_names_and_a_missing_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: the digest of an empty payload standing in for a file that does not exist."""
    # Bytes.
    assert fingerprint_bytes(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    assert fingerprint_bytes(b"") == hashlib.sha256(b"").hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint_bytes(b"x"))

    # A file has the digest of its bytes, whatever its size.
    small, big = tmp_path / "small.bin", tmp_path / "big.bin"
    small.write_bytes(b"abc")
    payload = bytes(range(256)) * 1000 + b"tail"
    big.write_bytes(payload)
    assert bounded(fingerprint_file, small) == fingerprint_bytes(b"abc")
    assert bounded(fingerprint_file, big) == fingerprint_bytes(payload)

    # A directory covers the sorted names and contents of the matching files only.
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "b.csv").write_bytes(b"2")
    (folder / "a.csv").write_bytes(b"1")
    (folder / "notes.txt").write_bytes(b"ignored")
    expected = hashlib.sha256()
    for name, content in (("a.csv", b"1"), ("b.csv", b"2")):
        expected.update(name.encode("utf-8"))
        expected.update(fingerprint_bytes(content).encode("ascii"))
    assert fingerprint_directory(folder) == expected.hexdigest()
    assert (
        fingerprint_directory(folder, "*.txt")
        == hashlib.sha256(b"notes.txt" + fingerprint_bytes(b"ignored").encode("ascii")).hexdigest()
    )
    assert fingerprint_directory(folder, "*.parquet") == hashlib.sha256().hexdigest()

    # Memory stays constant: no read asks for more than 65 536 bytes.
    sizes: list[int] = []
    real_open = Path.open

    class Spy:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def __enter__(self) -> Spy:
            return self

        def __exit__(self, *exc_info: object) -> None:
            self._handle.close()

        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return self._handle.read(size)

    stream = tmp_path / "stream.bin"
    stream.write_bytes(b"x" * 150_000)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", lambda self, *a, **k: Spy(real_open(self, *a, **k)))
        assert fingerprint_file(stream) == fingerprint_bytes(b"x" * 150_000)
    assert sizes == [65_536] * 4

    # A file that is not there is refused.
    with pytest.raises(FileNotFoundError):
        fingerprint_file(tmp_path / "absent.csv")


# ---------------------------------------------------------------- code version and context


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (subprocess.CompletedProcess([], 0, stdout="abc1234\n", stderr=""), "abc1234"),
        (subprocess.CompletedProcess([], 128, stdout=""), "unknown"),
        (subprocess.CompletedProcess([], 128, stdout="\n"), "unknown"),
        (FileNotFoundError("git"), "unknown"),
        (subprocess.TimeoutExpired("git", 5), "unknown"),
        (OSError("denied"), "unknown"),
    ],
)
def test_code_version_is_the_short_commit_asked_from_git_in_five_seconds_or_unknown_and_never_stops_a_run(
    monkeypatch: pytest.MonkeyPatch, outcome: Any, expected: str
) -> None:
    """A container built from an archive has no git: the version is 'unknown', the run starts all the same."""
    seen: list[tuple[list[str], dict[str, object]]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append((command, kwargs))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[no-any-return]

    monkeypatch.setattr(audit.subprocess, "run", run)

    assert code_version() == expected
    assert seen == [
        (["git", "rev-parse", "--short", "HEAD"], {"capture_output": True, "text": True, "timeout": 5, "check": False})
    ]


def test_runtime_context_records_python_platform_and_code_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "code_version", lambda: "abc1234")

    assert runtime_context() == {
        "python": sys.version.split()[0],
        "platform": audit.platform.system(),
        "code_version": "abc1234",
    }


# ---------------------------------------------------------------- the event


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"category": "finance", "action": "x", "reason": "y"},
            "Unknown audit category 'finance'. Allowed: control, data, decision, demo, integrity, pipeline",
        ),
        ({"category": "data", "action": "", "reason": "because"}, "An audit event requires a non-empty action."),
        ({"category": "data", "action": "   ", "reason": "because"}, "An audit event requires a non-empty action."),
        ({"category": "data", "action": "\t\n", "reason": "because"}, "An audit event requires a non-empty action."),
        (
            {"category": "data", "action": "x", "reason": " "},
            "An audit event requires a reason. A journal that records what without recording why "
            "is a log, not an audit trail.",
        ),
        ({"category": "data", "action": "x", "reason": "y", "row_count": -3}, "row_count cannot be negative, got -3."),
        ({"category": "data", "action": "rows_inserted", "reason": "batch loaded", "row_count": 0}, None),
        ({"category": "pipeline", "action": "run_started", "reason": "step began"}, None),
    ],
)
def test_an_event_is_built_with_its_defaults_or_refused_with_the_documented_message(
    fields: dict[str, Any], message: str | None
) -> None:
    if message is not None:
        with pytest.raises(ValueError) as refused:
            AuditEvent(**fields)
        assert str(refused.value) == message
        return

    event = AuditEvent(**fields)

    assert (event.category, event.action, event.reason, event.row_count) == (
        fields["category"],
        fields["action"],
        fields["reason"],
        fields.get("row_count"),
    )
    assert (event.details, event.run_id) == ({}, "")
    assert event.occurred_at.tzinfo is UTC
    assert abs((datetime.now(UTC) - event.occurred_at).total_seconds()) < 5


def test_an_event_is_frozen_keeps_its_own_details_and_the_categories_are_a_sorted_closed_list() -> None:
    first = AuditEvent(category="data", action="a", reason="r")
    second = AuditEvent(category="data", action="b", reason="r")
    first.details["leak"] = True

    assert second.details == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.reason = "edited"  # type: ignore[misc]
    assert iter_categories() == ["control", "data", "decision", "demo", "integrity", "pipeline"]
    assert set(iter_categories()) == EVENT_CATEGORIES


# ---------------------------------------------------------------- record_event


def test_record_event_returns_logs_and_hands_over_a_valid_event_and_writes_nothing_for_an_invalid_one(
    audit_log: list[tuple[AuditEvent, object]], log_records: list, settings: Settings
) -> None:
    """FORBIDDEN PATH: an invalid event reaching the log or the table before the refusal."""
    run_id = logging_config.new_run_id()

    event = record_event("data", "rows_inserted", "batch loaded", 12, settings, table="customers", seed=42)
    record_event("data", "no_count", "no row count given")

    assert (event.category, event.action, event.reason, event.row_count) == (
        "data",
        "rows_inserted",
        "batch loaded",
        12,
    )
    assert event.details == {"table": "customers", "seed": 42}
    assert event.run_id == run_id
    assert audit_log[0] == (event, settings)
    first, second = [r for r in log_records if r.name == "banking_dataops.audit"]
    assert (first.levelname, first.getMessage()) == ("INFO", "rows_inserted")
    assert (first.category, first.reason, first.rows, first.table) == ("data", "batch loaded", 12, "customers")
    assert second.rows == "-"

    before = (len(audit_log), len(log_records))
    with pytest.raises(ValueError, match="requires a reason"):
        record_event("data", "x", "")
    assert (len(audit_log), len(log_records)) == before


# ---------------------------------------------------------------- persistence


def test_an_event_is_persisted_with_a_json_context_and_a_naive_utc_timestamp_and_a_failure_is_reported_never_raised(
    fake_database: FakeDatabase, settings: Settings, log_records: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: the observer breaking the pipeline it observes (and its event lost silently)."""
    event = AuditEvent(
        category="data",
        action="rows_inserted",
        reason="batch loaded",
        row_count=12,
        details={"table": "clients é", "path": PurePosixPath("data/x.csv")},
        run_id="0123456789abcdef",
        occurred_at=datetime(2026, 7, 27, 9, 14, 5, tzinfo=UTC),
    )

    assert _persist_event(event, settings) is True
    (statement,) = fake_database.statements
    assert " ".join(statement.sql.split()) == INSERT_EVENT_SQL
    assert statement.params == (
        "0123456789abcdef",
        "data",
        "rows_inserted",
        "batch loaded",
        12,
        '{"table": "clients é", "path": "data/x.csv"}',
        datetime(2026, 7, 27, 9, 14, 5),
    )
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn]

    # Without settings, the environment's are read.
    monkeypatch.setattr(audit, "load_settings", lambda: settings)
    assert _persist_event(AuditEvent(category="data", action="x", reason="y")) is True
    assert fake_database.dsns == [settings.dsn] * 2

    # An unreachable journal table is a warning with the first 200 characters of the error, never an exception.
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("x" * 300)

    with monkeypatch.context() as patch:
        patch.setattr(db_module, "connect", refuse)
        assert _persist_event(AuditEvent(category="data", action="x", reason="y")) is False
    warning = next(r for r in log_records if r.getMessage() == "audit_event_not_persisted")
    assert (warning.levelname, warning.category) == ("WARNING", "integrity")
    assert warning.reason == "the journal table is unreachable; the event is kept in the log only"
    assert warning.error == "x" * 200

    # A missing database driver returns False without a warning; the operator's interruption is not swallowed.
    log_records.clear()
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "banking_dataops.db", None)
        assert _persist_event(AuditEvent(category="data", action="x", reason="y")) is False
    assert [r for r in log_records if r.getMessage() == "audit_event_not_persisted"] == []

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(db_module, "connect", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _persist_event(AuditEvent(category="data", action="x", reason="y"))


def test_the_json_context_never_raises_on_an_exotic_value_and_keeps_accents() -> None:
    text = _as_json({"when": datetime(2026, 7, 27, tzinfo=UTC), "name": "Zürich", "n": 3})

    assert json.loads(text) == {"when": "2026-07-27 00:00:00+00:00", "name": "Zürich", "n": 3}
    assert "Zürich" in text


# ---------------------------------------------------------------- the run frame


def test_a_successful_run_reuses_the_identifier_of_its_parent_declares_its_step_and_journals_start_and_end(
    no_run: None, monkeypatch: pytest.MonkeyPatch, audit_log: list[tuple[AuditEvent, object]], settings: Settings
) -> None:
    monkeypatch.setattr(audit, "code_version", lambda: "abc1234")
    monkeypatch.setattr(audit.time, "monotonic", _Stopwatch(100.0, 101.5, 200.0, 200.25))

    with RunJournal("cleaning", seed=42, settings=settings) as run:
        own = run.run_id
        assert re.fullmatch(r"[0-9a-f]{16}", own)
        assert logging_config.current_run_id() == own
        assert logging_config.current_step() == "cleaning"
        assert run.started_at == 100.0
        note = run.note("gold_table_loaded", "rebuilt from silver", 31, table="gold.dim_date")
    with RunJournal("child", settings=settings) as child:
        assert child.run_id == own

    started, noted, succeeded, _, _ = (event for event, _ in audit_log)
    assert (started.category, started.action, started.reason) == ("pipeline", "run_started", "step cleaning began")
    assert started.details == {
        "seed": 42,
        "python": sys.version.split()[0],
        "platform": audit.platform.system(),
        "code_version": "abc1234",
    }
    assert noted is note
    assert (note.category, note.action, note.reason, note.row_count) == (
        "data",
        "gold_table_loaded",
        "rebuilt from silver",
        31,
    )
    assert (note.details, note.run_id) == ({"table": "gold.dim_date"}, own)
    assert (succeeded.category, succeeded.action) == ("pipeline", "run_succeeded")
    assert succeeded.reason == "step cleaning completed without error"
    assert succeeded.details["duration_ms"] == 1500
    assert {event.run_id for event, _ in audit_log} == {own}
    assert [given for _, given in audit_log] == [settings] * 5


def test_a_failing_run_is_journalled_with_the_truncated_error_and_its_duration_and_the_exception_propagates(
    no_run: None, monkeypatch: pytest.MonkeyPatch, audit_log: list[tuple[AuditEvent, object]], settings: Settings
) -> None:
    """FORBIDDEN PATH: the audit layer absorbing the pipeline's own exception, interruptions included."""
    with monkeypatch.context() as patch:
        patch.setattr(audit.time, "monotonic", _Stopwatch(10.0, 12.25))
        with pytest.raises(ValueError, match="boom"), RunJournal("ingest", settings=settings):
            raise ValueError("boom" + "!" * 400)

    failed = audit_log[-1][0]
    assert (failed.category, failed.action) == ("pipeline", "run_failed")
    assert failed.reason == "step ingest raised ValueError"
    assert failed.details["error"] == ("boom" + "!" * 400)[:300]
    assert failed.details["duration_ms"] == 2250
    assert audit_log[-1][1] is settings
    assert "run_succeeded" not in [event.action for event, _ in audit_log]

    with pytest.raises(KeyboardInterrupt), RunJournal("ingest"):
        raise KeyboardInterrupt
    assert audit_log[-1][0].reason == "step ingest raised KeyboardInterrupt"


# ---------------------------------------------------------------- invariants


@pytest.mark.parametrize("condition", [True, False, 0, None, "", []])
def test_an_invariant_is_journalled_and_a_violated_one_raises_with_the_rule_and_the_reason(
    audit_log: list[tuple[AuditEvent, object]], condition: Any
) -> None:
    if not condition:
        with pytest.raises(AssertionError) as raised:
            assert_invariant(condition, invariant="a == b", reason="they must match", a=1, b=2)
        assert str(raised.value) == "Invariant violated: a == b. they must match"
        event = audit_log[-1][0]
        assert (event.category, event.action, event.reason) == ("integrity", "invariant_violated", "they must match")
        assert event.details == {"invariant": "a == b", "holds": condition, "a": 1, "b": 2}
        return

    assert_invariant(condition, invariant="a == a", reason="tautology", rows=5)

    event = audit_log[-1][0]
    assert (event.category, event.action, event.reason) == ("integrity", "invariant_checked", "tautology")
    assert event.details == {"invariant": "a == a", "holds": True, "rows": 5}


# ---------------------------------------------------------------- run identifier and queries


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, ""),
        ("a1b2c3d4e5f60718", "a1b2c3d4e5f60718"),
        ("  0f  ", "0f"),
        ("f" * 32, "f" * 32),
        ("0123456789", "0123456789"),
        ("' OR 1=1 --", None),
        ("ABCDEF", None),
        ("xyz", None),
        ("", None),
        ("   ", None),
        ("a" * 33, None),
        ("../etc", None),
        ("1234 5678", None),
        ("ab12X", None),
        ("X", None),
        ("ab-cd", None),
    ],
)
def test_a_run_identifier_is_accepted_and_trimmed_or_refused_with_its_value_in_the_message(
    given: str | None, expected: str | None
) -> None:
    if expected is None:
        with pytest.raises(ValueError) as raised:
            _safe_run_id(given)
        assert str(raised.value) == f"Invalid run identifier {given!r}: expected hexadecimal, at most 32 characters."
        return

    assert _safe_run_id(given) == expected


def test_the_trail_and_the_run_summaries_bind_their_parameters_and_a_hostile_value_never_reaches_the_database(
    fake_database: FakeDatabase, settings: Settings
) -> None:
    """FORBIDDEN PATH: a crafted identifier or limit reaching a query."""
    columns = ["run_id", "category", "action", "reason", "row_count", "details", "occurred_at"]
    fake_database.answer("FROM audit_events", rows=[("a1", "data", "x", "y", 1, {}, "2026-07-27")], columns=columns)

    rows = audit_trail(" a1b2 ", limit=5, settings=settings)
    audit_trail()
    audit_trail(settings=settings)
    audit_trail(limit="7")  # type: ignore[arg-type]
    summarise_runs(limit=3, settings=settings)
    summarise_runs()

    select = "SELECT run_id, category, action, reason, row_count, details, occurred_at FROM audit_events "
    assert [" ".join(s.sql.split()) for s in fake_database.statements] == [
        select + "WHERE run_id = %s ORDER BY occurred_at DESC LIMIT %s",
        select + "ORDER BY occurred_at DESC LIMIT %s",
        select + "ORDER BY occurred_at DESC LIMIT %s",
        select + "ORDER BY occurred_at DESC LIMIT %s",
        "SELECT run_id, MIN(occurred_at) AS started_at, MAX(occurred_at) AS ended_at, COUNT(*) AS event_count, "
        "COUNT(*) FILTER (WHERE action = 'run_failed') AS failures, "
        "COUNT(*) FILTER (WHERE action = 'invariant_violated') AS violations "
        "FROM audit_events GROUP BY run_id ORDER BY MIN(occurred_at) DESC LIMIT %s",
        "SELECT run_id, MIN(occurred_at) AS started_at, MAX(occurred_at) AS ended_at, COUNT(*) AS event_count, "
        "COUNT(*) FILTER (WHERE action = 'run_failed') AS failures, "
        "COUNT(*) FILTER (WHERE action = 'invariant_violated') AS violations "
        "FROM audit_events GROUP BY run_id ORDER BY MIN(occurred_at) DESC LIMIT %s",
    ]
    assert [s.params for s in fake_database.statements] == [("a1b2", 5), (200,), (200,), (7,), (3,), (20,)]
    assert rows == [dict(zip(columns, ("a1", "data", "x", "y", 1, {}, "2026-07-27"), strict=True))]
    assert fake_database.dsns[0] == settings.dsn
    assert fake_database.dsns[2] == settings.dsn
    assert fake_database.dsns[4] == settings.dsn

    sent = len(fake_database.statements)
    with pytest.raises(ValueError, match="Invalid run identifier"):
        audit_trail("0' OR '1'='1")
    with pytest.raises(ValueError):
        audit_trail(limit="7; DROP TABLE audit_events")  # type: ignore[arg-type]
    assert len(fake_database.statements) == sent
