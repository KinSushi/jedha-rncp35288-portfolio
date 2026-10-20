"""Reconciliation (``banking_dataops.reconciliation``): five aggregates, one coherence row, empty batch refused.

The database is the recording fake of ``tests/fakes.py``. Each aggregate query is answered from a script, so
the tests read what was asked (the five queries, in order), what was written (one row of
``reconciliation_results``) and what was journalled.
"""

from __future__ import annotations

import dataclasses
import re
from decimal import Decimal

import pandas as pd
import pytest
from fakes import FakeDatabase

from banking_dataops import audit, reconciliation
from banking_dataops.config import Settings
from banking_dataops.reconciliation import (
    CHANNEL_SUMMARY_QUERY,
    DAILY_RECONCILIATION_QUERY,
    SOURCE_SYSTEM_QUERY,
    STATUS_SUMMARY_QUERY,
    SUSPICIOUS_SUMMARY_QUERY,
    SourceSystemSummary,
    print_reconciliation_summary,
    run_reconciliation,
    source_system_summary,
)

pytestmark = pytest.mark.usefixtures("no_audit_database")

SOURCE_COLUMNS = [
    "source_system",
    "transaction_count",
    "total_amount_chf",
    "first_event_timestamp",
    "last_event_timestamp",
]
SOURCE_ROWS = [
    ("card_processor", 300, Decimal("1500.25"), "2026-07-01", "2026-07-27"),
    ("core_banking", 400, Decimal("2000.50"), "2026-07-01", "2026-07-27"),
    ("payments_gateway", 100, Decimal("499.25"), "2026-07-02", "2026-07-26"),
]


def _script(database: FakeDatabase, *, source_rows: list[tuple[object, ...]] | None = None) -> None:
    rows = SOURCE_ROWS if source_rows is None else source_rows
    database.answer("GROUP BY source_system", rows=rows, columns=SOURCE_COLUMNS)
    database.answer(
        "GROUP BY booking_date",
        rows=[("2026-07-26", 800, Decimal("4000.00"), 12)],
        columns=["booking_date", "transaction_count", "total_amount_chf", "suspicious_count"],
    )
    database.answer(
        "GROUP BY channel",
        rows=[("api", 500, Decimal("0.2500"), 20)],
        columns=["channel", "transaction_count", "avg_risk_score", "non_posted_count"],
    )
    database.answer(
        "GROUP BY status",
        rows=[("posted", 790, Decimal("3990.00"))],
        columns=["status", "transaction_count", "total_amount_chf"],
    )
    database.answer(
        "GROUP BY is_suspicious",
        rows=[(True, 12, Decimal("90.00"), Decimal("0.8100"))],
        columns=["is_suspicious", "transaction_count", "total_amount_chf", "avg_risk_score"],
    )


def _blank_page(database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]]) -> None:
    """Forget what the fake database recorded and scripted, and what was journalled."""
    database.forget()
    database.answers.clear()
    audit_log.clear()


def test_a_summary_is_a_frozen_typed_record_with_exact_decimal_totals_and_an_empty_table_is_an_empty_list(
    fake_database: FakeDatabase, settings: Settings
) -> None:
    _script(fake_database)
    summaries = source_system_summary(settings)
    assert summaries == [
        SourceSystemSummary("card_processor", 300, Decimal("1500.25")),
        SourceSystemSummary("core_banking", 400, Decimal("2000.50")),
        SourceSystemSummary("payments_gateway", 100, Decimal("499.25")),
    ]
    assert all(isinstance(s.total_amount_chf, Decimal) for s in summaries)
    assert fake_database.sql_sent() == [SOURCE_SYSTEM_QUERY]
    assert fake_database.dsns == [settings.dsn]

    fake_database.forget()
    fake_database.answers.clear()
    fake_database.answer("GROUP BY source_system", rows=[], columns=SOURCE_COLUMNS)
    assert source_system_summary() == []

    # FORBIDDEN PATH: a figure edited once it was computed.
    with pytest.raises(dataclasses.FrozenInstanceError):
        summaries[1].transaction_count = 0  # type: ignore[misc]
    assert summaries[1].transaction_count == 400


def test_the_reconciliation_asks_five_aggregates_writes_one_coherence_row_journals_it_and_refuses_an_empty_batch(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    log_records: list,
    settings: Settings,
) -> None:
    _script(fake_database)
    summaries = run_reconciliation(settings)

    # The five aggregates, in order, by name.
    assert list(summaries) == ["source_system", "daily", "channel", "status", "suspicious"]
    assert fake_database.sql_sent()[:5] == [
        SOURCE_SYSTEM_QUERY,
        DAILY_RECONCILIATION_QUERY,
        CHANNEL_SUMMARY_QUERY,
        STATUS_SUMMARY_QUERY,
        SUSPICIOUS_SUMMARY_QUERY,
    ]
    assert list(summaries["source_system"].columns) == SOURCE_COLUMNS
    assert summaries["source_system"]["transaction_count"].tolist() == [300, 400, 100]
    assert summaries["status"]["status"].tolist() == ["posted"]
    assert fake_database.dsns == [settings.dsn] * 6

    # One coherence row with the batch totals and structural zero deltas.
    inserts = fake_database.find("INSERT INTO reconciliation_results")
    assert len(inserts) == 1
    assert " ".join(inserts[0].sql.split()) == (
        "INSERT INTO reconciliation_results (reconciliation_id, reconciliation_name, source_count, target_count, "
        "count_delta, source_total, target_total, amount_delta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    )
    identifier, name, source_count, target_count, count_delta, source_total, target_total, amount_delta = inserts[
        0
    ].params
    assert re.fullmatch(r"REC-[0-9A-F]{12}", identifier)
    assert (name, source_count, target_count, count_delta) == ("source_system_total", 800, 800, 0)
    assert (source_total, target_total, amount_delta) == (Decimal("4000.00"), Decimal("4000.00"), Decimal("0.00"))
    assert isinstance(source_total, Decimal)
    assert fake_database.commits == 1

    # The run is journalled with the honest description of what it proves, and named for the reconciliation.
    events = [event for event, _ in audit_log]
    assert events[0].reason == "step reconciliation began"
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("integrity", "invariant_checked", None),
        ("data", "coherence_summary_written", 800),
        ("pipeline", "run_succeeded", None),
    ]
    invariant, written = events[1], events[2]
    assert invariant.details["invariant"] == "the reconciled batch is not empty"
    assert invariant.details["total_count"] == 800
    assert written.reason == (
        "aggregate coherence check across source systems; NOT a reconciliation between two independent "
        "systems - both sides come from the same table, so the delta is structurally zero"
    )
    assert written.details["total_amount_chf"] == "4000.00"
    assert written.details["summaries"] == ["source_system", "daily", "channel", "status", "suspicious"]
    assert written.details["reconciliation_id"] == inserts[0].params[0]
    assert all(given is settings for event, given in audit_log if event.category != "integrity")

    # Each aggregate leaves a debug record with its row count.
    computed = [r for r in log_records if r.getMessage() == "summary_computed"]
    assert [(r.reason, r.rows, r.category) for r in computed] == [
        ("source_system aggregate built for the reconciliation report", 3, "data"),
        ("daily aggregate built for the reconciliation report", 1, "data"),
        ("channel aggregate built for the reconciliation report", 1, "data"),
        ("status aggregate built for the reconciliation report", 1, "data"),
        ("suspicious aggregate built for the reconciliation report", 1, "data"),
    ]
    assert {r.levelname for r in computed} == {"DEBUG"}

    # Two runs write two distinct reconciliation identifiers; without settings the environment is read.
    _blank_page(fake_database, audit_log)
    monkeypatch.setattr(reconciliation, "load_settings", lambda: settings)
    _script(fake_database)
    run_reconciliation()
    run_reconciliation()
    identifiers = [s.params[0] for s in fake_database.find("INSERT INTO reconciliation_results")]
    assert len(set(identifiers)) == len(identifiers)
    assert set(fake_database.dsns) == {settings.dsn}

    # FORBIDDEN PATH: a zero total read as a legitimately empty day, and persisted as a coherence proof.
    _blank_page(fake_database, audit_log)
    _script(fake_database, source_rows=[])
    with pytest.raises(AssertionError) as refused:
        run_reconciliation(settings)
    assert str(refused.value) == (
        "Invariant violated: the reconciled batch is not empty. a zero total is indistinguishable from a "
        "legitimately empty day; continuing would publish a summary that proves nothing"
    )
    assert fake_database.find("INSERT INTO reconciliation_results") == []
    assert fake_database.commits == 0
    actions = [event.action for event, _ in audit_log]
    assert actions[-2:] == ["invariant_violated", "run_failed"]
    assert "coherence_summary_written" not in actions


def test_the_text_report_and_main_print_a_titled_table_and_a_batch_of_one_is_reconciled_but_zero_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_database: FakeDatabase, settings: Settings
) -> None:
    frame = pd.DataFrame({"status": ["posted", "pending"], "transaction_count": [790, 10]})
    print_reconciliation_summary({"status": frame})
    assert capsys.readouterr().out == "\n## status\n" + frame.to_string(index=False) + "\n"

    channel = pd.DataFrame({"channel": ["api"], "transaction_count": [5]})
    with monkeypatch.context() as patch:
        patch.setattr(reconciliation, "run_reconciliation", lambda: {"channel": channel})
        reconciliation.main()
    assert capsys.readouterr().out == "\n## channel\n" + channel.to_string(index=False) + "\n"

    # The boundary: a batch of a single transaction is reconciled...
    _script(fake_database, source_rows=[("core_banking", 1, Decimal("10.00"), "2026-07-01", "2026-07-01")])
    run_reconciliation(settings)
    assert fake_database.find("INSERT INTO reconciliation_results")[0].params[2] == 1

    # ...and a batch whose systems all report zero transactions is refused, with nothing written.
    fake_database.forget()
    fake_database.answers.clear()
    _script(fake_database, source_rows=[("core_banking", 0, Decimal("0.00"), "2026-07-01", "2026-07-01")])
    with pytest.raises(AssertionError, match="the reconciled batch is not empty"):
        run_reconciliation(settings)
    assert fake_database.find("INSERT INTO reconciliation_results") == []
