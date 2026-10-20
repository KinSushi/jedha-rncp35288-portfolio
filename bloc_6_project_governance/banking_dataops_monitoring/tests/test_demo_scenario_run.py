"""Demo scenario (``banking_dataops.demo_scenario``): injection, reset, and what both refuse to do.

The database is the recording fake of ``tests/fakes.py``. The clock is frozen, so the timestamps written to
the injected rows can be compared exactly. Every refusal is checked from the database's point of view: no
row inserted, no row deleted, nothing committed. For each action the accepted call and the refused one are read
in the same test.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fakes import FakeDatabase

from banking_dataops import audit, demo_scenario, logging_config
from banking_dataops.config import Settings
from banking_dataops.demo_scenario import (
    _INSERT_SQL,
    DEMO_CONTROL_IDS,
    DEMO_SOURCE_SYSTEM,
    DEMO_VIOLATIONS,
    count_injected,
    inject,
    is_injected,
    reset,
)

pytestmark = pytest.mark.usefixtures("no_audit_database")

NOW = datetime(2026, 7, 27, 14, 14, 5)
COUNT_DEMO_SQL = "SELECT COUNT(*) FROM transactions WHERE source_system = %s"
COUNT_REAL_SQL = "SELECT COUNT(*) FROM transactions WHERE source_system <> %s"
PICK_ACCOUNT_SQL = "SELECT account_id FROM accounts ORDER BY account_id LIMIT 1"
AuditLog = list[tuple[audit.AuditEvent, object]]


class _FrozenDatetime(datetime):
    """A clock on a machine set to UTC-5: ``now()`` without a zone is the local time, ``now(UTC)`` the real one."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def,override]
        instant = datetime(2026, 7, 27, 14, 14, 5, tzinfo=UTC)
        if tz is None:
            return instant.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
        return instant.astimezone(tz)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(demo_scenario, "datetime", _FrozenDatetime)


def _fresh_database(database: FakeDatabase, *, landed: int = 3) -> None:
    """No demo row yet, one account to attach to, then ``landed`` demo rows once the batch is in."""
    database.answer_each(COUNT_DEMO_SQL, [[(0,)], [(landed,)]])
    database.answer(PICK_ACCOUNT_SQL, rows=[("ACC-0001",)])


def _populated_database(database: FakeDatabase, *, before: int = 1000, after: int = 1000, deleted: int = 3) -> None:
    database.answer_each(COUNT_REAL_SQL, [[(before,)], [(after,)]])
    database.answer("DELETE FROM transactions", rowcount=deleted)
    database.answer("DELETE FROM incident_reports", rowcount=2)


def _blank_page(database: FakeDatabase, audit_log: AuditLog) -> None:
    """Forget what the fake database recorded, scripted and journalled, to judge the next call on its own."""
    database.forget()
    database.answers.clear()
    audit_log.clear()


# ---------------------------------------------------------------- counting


def test_the_marked_rows_are_counted_with_the_marker_as_a_bound_parameter_and_a_database_with_no_answer_is_refused(
    monkeypatch: pytest.MonkeyPatch, fake_database: FakeDatabase, settings: Settings
) -> None:
    fake_database.answer(COUNT_DEMO_SQL, rows=[(3,)])
    assert count_injected(settings) == 3
    assert [(s.sql, s.params) for s in fake_database.statements] == [(COUNT_DEMO_SQL, (DEMO_SOURCE_SYSTEM,))]
    assert fake_database.dsns == [settings.dsn]

    # The settings given are the ones used: the environment is not read when they are given. The environment is
    # replaced, in this block only, by other settings, so that using the wrong ones would be seen.
    other = settings.model_copy(update={"database_host": "elsewhere.test"})
    with monkeypatch.context() as patch:
        patch.setattr(demo_scenario, "load_settings", lambda: other)
        fake_database.forget()
        assert is_injected(settings) is True
        assert fake_database.dsns == [settings.dsn]

        # Without settings the environment is read; the demo is "injected" as soon as one marked row exists.
        for present, expected in [(0, False), (1, True), (3, True)]:
            fake_database.forget()
            fake_database.answers.clear()
            fake_database.answer(COUNT_DEMO_SQL, rows=[(present,)])
            assert count_injected() == present
            assert is_injected() is expected
            assert fake_database.dsns == [other.dsn] * 2

    # FORBIDDEN PATH: a COUNT(*) that answers no row is a broken database, not "zero demo rows".
    fake_database.forget()
    fake_database.answers.clear()
    with pytest.raises(AssertionError) as refused:
        count_injected()
    assert str(refused.value) == "COUNT(*) always returns one row"


# ---------------------------------------------------------------- injection


def test_inject_writes_the_three_violations_in_one_transaction_journals_each_and_a_second_injection_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: AuditLog,
    log_records: list,
    settings: Settings,
) -> None:
    # Without settings the environment is read; the first account by identifier is the one the rows attach to,
    # so that the foreign key holds.
    other = settings.model_copy(update={"database_host": "elsewhere.test"})
    with monkeypatch.context() as patch:
        patch.setattr(demo_scenario, "load_settings", lambda: other)
        _fresh_database(fake_database)
        inject()
    assert set(fake_database.dsns) == {other.dsn}
    assert {s.params["account_id"] for s in fake_database.find("INSERT INTO transactions")} == {"ACC-0001"}
    chosen = next(r for r in log_records if r.getMessage() == "anchor_account_selected")
    assert (chosen.levelname, chosen.account_id, chosen.reason) == ("DEBUG", "ACC-0001", "satisfies the foreign key")

    # The three violations: one transaction, a frozen clock, exact decimals (a float here would turn CHF 742.50 into
    # 742.5 in the control's WHERE clause comparison).
    _blank_page(fake_database, audit_log)
    _fresh_database(fake_database)
    applied = inject(settings)
    assert applied == list(DEMO_VIOLATIONS)
    inserts = fake_database.find("INSERT INTO transactions")
    assert [s.sql for s in inserts] == [_INSERT_SQL] * 3
    assert [s.params for s in inserts] == [
        {
            "transaction_id": v.transaction_id,
            "account_id": "ACC-0001",
            "source_system": DEMO_SOURCE_SYSTEM,
            "event_timestamp": NOW,
            "booking_date": date(2026, 7, 26),
            "amount_chf": v.amount_chf,
            "risk_score": v.risk_score,
            "status": v.status,
            "created_at": NOW,
        }
        for v in DEMO_VIOLATIONS
    ]
    assert fake_database.commits == 1
    assert [s.sql for s in fake_database.statements].count(PICK_ACCOUNT_SQL) == 1
    assert fake_database.dsns == [settings.dsn] * 3
    assert [v.amount_chf for v in DEMO_VIOLATIONS] == [Decimal("1850000.00"), Decimal("742.50"), Decimal("318.90")]
    assert [v.risk_score for v in DEMO_VIOLATIONS] == [Decimal("0.4200"), Decimal("1.4200"), Decimal("0.1700")]

    # Each violation is journalled with its business reason, then the count is checked.
    events = [event for event, _ in audit_log]
    assert events[0].reason == "step demo_inject began"
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("demo", "violation_injected", 1),
        ("demo", "violation_injected", 1),
        ("demo", "violation_injected", 1),
        ("integrity", "invariant_checked", None),
        ("data", "demo_batch_injected", 3),
        ("pipeline", "run_succeeded", None),
    ]
    assert [(e.reason, e.details["control_id"], e.details["transaction_id"]) for e in events[1:4]] == [
        (v.business_reason, v.control_id, v.transaction_id) for v in DEMO_VIOLATIONS
    ]
    invariant = events[4]
    assert invariant.details["invariant"] == "injected_row_count == len(DEMO_VIOLATIONS)"
    assert (invariant.details["expected"], invariant.details["observed"]) == (3, 3)
    assert events[5].reason == "the degraded batch is in place; controls can now be re-evaluated"
    assert all(given is settings for event, given in audit_log if event.category != "integrity")

    # FORBIDDEN PATH: injecting twice, which would make the verdict impossible to attribute to one act.
    _blank_page(fake_database, audit_log)
    fake_database.answer(COUNT_DEMO_SQL, rows=[(3,)])
    returned = inject(settings)
    assert returned == list(DEMO_VIOLATIONS)
    assert fake_database.find("INSERT") == []
    assert fake_database.commits == 0
    skipped = next(event for event, _ in audit_log if event.action == "injection_skipped")
    assert (skipped.category, skipped.row_count) == ("demo", 3)
    assert skipped.reason == (
        "3 demo row(s) already present; a second injection would create an ambiguous state "
        "where the verdict could not be attributed to a single act"
    )
    assert skipped.run_id == audit_log[0][0].run_id
    assert audit_log[1][1] is settings
    assert "demo_batch_injected" not in [event.action for event, _ in audit_log]


def test_inject_without_an_account_or_when_a_row_is_lost_is_refused_and_writes_nothing_more(
    fake_database: FakeDatabase, audit_log: AuditLog
) -> None:
    # FORBIDDEN PATH: creating an account to satisfy the foreign key, which the reset would not remove.
    fake_database.answer(COUNT_DEMO_SQL, rows=[(0,)])
    fake_database.answer(PICK_ACCOUNT_SQL, rows=[])
    with pytest.raises(RuntimeError) as refused:
        inject()
    assert str(refused.value) == (
        "No account found. The demo scenario needs reference data to attach its rows to. "
        "Run the ingestion pipeline first: `docker compose exec app python -m banking_dataops.ingest`."
    )
    assert "docker compose exec app python -m banking_dataops.ingest" in str(refused.value)
    assert fake_database.find("INSERT") == []
    assert fake_database.commits == 0
    assert audit_log[-1][0].action == "run_failed"
    assert audit_log[-1][0].reason == "step demo_inject raised RuntimeError"

    # FORBIDDEN PATH: an ON CONFLICT that swallowed one row, making the reset inexact.
    _blank_page(fake_database, audit_log)
    _fresh_database(fake_database, landed=2)
    with pytest.raises(AssertionError) as violated_invariant:
        inject()
    assert str(violated_invariant.value) == (
        "Invariant violated: injected_row_count == len(DEMO_VIOLATIONS). a mismatch means either a silent conflict "
        "or a collision on the source_system marker, both of which would make the reset inexact"
    )
    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert (violated.details["expected"], violated.details["observed"]) == (3, 2)
    assert "demo_batch_injected" not in [event.action for event, _ in audit_log]


# ---------------------------------------------------------------- reset


def test_reset_deletes_only_the_marked_rows_journals_it_and_stops_when_a_real_row_disappeared(
    monkeypatch: pytest.MonkeyPatch, fake_database: FakeDatabase, audit_log: AuditLog, settings: Settings
) -> None:
    _populated_database(fake_database)
    deleted = reset(settings)
    assert deleted == 3
    deletes = [s for s in fake_database.statements if s.sql.lstrip().startswith("DELETE")]
    assert [(" ".join(s.sql.split()), s.params) for s in deletes] == [
        ("DELETE FROM transactions WHERE source_system = %s", (DEMO_SOURCE_SYSTEM,)),
        ("DELETE FROM incident_reports WHERE status = 'open' AND failed_control = ANY(%s)", (list(DEMO_CONTROL_IDS),)),
    ]
    assert DEMO_CONTROL_IDS == ("CTRL-003", "CTRL-005", "CTRL-006")
    counts = [s for s in fake_database.statements if s.sql == COUNT_REAL_SQL]
    assert [s.params for s in counts] == [(DEMO_SOURCE_SYSTEM,)] * 2
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn] * 3
    # The real rows are counted before deleting and again after.
    order = [
        "count" if s.sql == COUNT_REAL_SQL else "delete" if s.sql.lstrip().startswith("DELETE") else "other"
        for s in fake_database.statements
    ]
    assert order == ["count", "delete", "delete", "count"]

    # The removal is journalled with the untouched reference rows.
    events = [event for event, _ in audit_log]
    assert events[0].reason == "step demo_reset began"
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("integrity", "invariant_checked", None),
        ("data", "demo_batch_removed", 3),
        ("pipeline", "run_succeeded", None),
    ]
    invariant, removed = events[1], events[2]
    assert invariant.details["invariant"] == "reset touches only rows marked demo_scenario"
    assert (invariant.details["before"], invariant.details["after"]) == (1000, 1000)
    assert removed.reason == "the database is back to its pre-demonstration state"
    assert (removed.details["incidents_purged"], removed.details["reference_rows_untouched"]) == (2, 1000)
    assert all(given is settings for event, given in audit_log if event.category != "integrity")

    # Without the incident purge the incident table is never touched, and the journal says zero were purged.
    _blank_page(fake_database, audit_log)
    _populated_database(fake_database)
    assert reset(purge_incidents=False) == 3
    assert fake_database.find("incident_reports") == []
    removed_without_purge = next(event for event, _ in audit_log if event.action == "demo_batch_removed")
    assert removed_without_purge.details["incidents_purged"] == 0

    # Without settings the environment is read.
    _blank_page(fake_database, audit_log)
    monkeypatch.setattr(demo_scenario, "load_settings", lambda: settings)
    _populated_database(fake_database)
    reset()
    assert set(fake_database.dsns) == {settings.dsn}

    # FORBIDDEN PATH: a reset that deletes reference data and still reports success.
    _blank_page(fake_database, audit_log)
    _populated_database(fake_database, before=1000, after=999)
    with pytest.raises(AssertionError) as refused:
        reset()
    assert str(refused.value) == (
        "Invariant violated: reset touches only rows marked demo_scenario. deleting reference data would corrupt "
        "every subsequent control result and make the demonstration destructive"
    )
    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert (violated.details["before"], violated.details["after"]) == (1000, 999)
    assert "demo_batch_removed" not in [event.action for event, _ in audit_log]
    assert audit_log[-1][0].action == "run_failed"


@pytest.mark.parametrize("count_that_answers_nothing", [0, 1])
def test_a_reset_whose_row_count_query_answers_nothing_is_refused_before_it_deletes_or_commits_more(
    fake_database: FakeDatabase, count_that_answers_nothing: int
) -> None:
    """FORBIDDEN PATH: comparing reference rows before and after a reset when one of the two counts is missing."""
    results = [[(1000,)], [(1000,)]]
    results[count_that_answers_nothing] = []
    fake_database.answer_each(COUNT_REAL_SQL, results)
    fake_database.answer("DELETE FROM transactions", rowcount=3)

    with pytest.raises(AssertionError) as refused:
        reset()

    assert str(refused.value) == "COUNT(*) always returns one row"
    assert fake_database.commits == (0 if count_that_answers_nothing == 0 else 1)


# ---------------------------------------------------------------- command line


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the logging set-up and the three actions by recorders; return the calls in order."""
    calls: list[str] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "0" * 16)
    monkeypatch.setattr(demo_scenario, "reset", lambda: calls.append("reset") or 3)
    monkeypatch.setattr(demo_scenario, "count_injected", lambda: calls.append("count") or 3)
    monkeypatch.setattr(demo_scenario, "inject", lambda: calls.append("inject") or list(DEMO_VIOLATIONS))
    return calls


def test_main_runs_the_one_action_asked_and_prints_what_it_did_and_a_mistyped_action_runs_none_of_the_three(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], cli: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["demo_scenario"])
    demo_scenario.main()
    assert cli == ["configure_logging", "new_run_id", "inject"]
    assert capsys.readouterr().out.splitlines() == [
        f"{v.control_id} | {v.control_name} | {v.narrative}" for v in DEMO_VIOLATIONS
    ]

    cli.clear()
    monkeypatch.setattr(sys, "argv", ["demo_scenario", "reset"])
    demo_scenario.main()
    assert cli[-1] == "reset"
    assert capsys.readouterr().out == "Demo batch removed (3 transaction(s)).\n"

    cli.clear()
    monkeypatch.setattr(sys, "argv", ["demo_scenario", "status"])
    demo_scenario.main()
    assert cli[2:] == ["count"]
    assert capsys.readouterr().out == "Demo rows present: 3\n"

    # FORBIDDEN PATH: a typo read as a destructive action.
    for action in ["reste", "RESET", "delete", ""]:
        cli.clear()
        monkeypatch.setattr(sys, "argv", ["demo_scenario", action])
        with pytest.raises(SystemExit) as raised:
            demo_scenario.main()
        assert str(raised.value) == f"Unknown action {action!r}. Expected: inject, reset or status."
        assert cli == ["configure_logging", "new_run_id"]
