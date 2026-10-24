"""When the database refuses a statement, the step stops, journals a failed run, and commits nothing it should not.

A pipeline that carries on after a refused statement builds its verdict, its queue or its reports on a state
nobody declared. For each step that writes, the recording fake database (``tests/fakes.py``) is told to refuse
one statement; the tests read the exception, the commits and the journal.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import pandas as pd
import pytest
from fakes import FakeDatabase

from banking_dataops import audit, demo_scenario, ingest, powerbi_views, reconciliation
from banking_dataops.config import Settings
from banking_dataops.ml import features as F
from banking_dataops.ml import predict as P
from banking_dataops.ml.train import _build_estimator
from banking_dataops.pipeline import cleaning, gold
from banking_dataops.quality_checks import run_quality_checks

#: No ``limit(120)`` here (removed on 20/10/2026): these tests take at most 0.12 s, so the default limit of
#: ``tests/conftest.py`` applies. One of them is the only test with a 120 s limit that reaches
#: ``db.split_sql_statements``; when mutmut ran it first, a mutant that resets the index of the splitter exhausted
#: its CPU budget (33 s) before the limit fired and was recorded as a time-out instead of a detection.
pytestmark = [
    pytest.mark.usefixtures("no_audit_database"),
]


class Refused(Exception):
    """Stands for the error PostgreSQL raises when it refuses a statement."""


def _actions(audit_log: list[tuple[audit.AuditEvent, object]]) -> list[str]:
    return [event.action for event, _ in audit_log]


def test_a_refused_copy_stops_the_ingestion_before_the_next_table_is_touched(
    tmp_path: Path, fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]], settings: Settings
) -> None:
    for name, content in (
        ("customers", "customer_id\nC1\n"),
        ("accounts", "account_id\nA1\n"),
        ("transactions", "transaction_id\nT1\n"),
    ):
        (tmp_path / f"synthetic_{name}.csv").write_text(content, encoding="utf-8")
    runtime = settings.model_copy(update={"data_dir": tmp_path})
    fake_database.fail_on("COPY accounts", Refused("foreign key violation"))

    with pytest.raises(Refused, match="foreign key violation"):
        ingest.ingest_all(runtime)

    assert [sql for sql, _ in fake_database.copies] == ["COPY customers FROM STDIN WITH CSV HEADER"]
    assert fake_database.commits == 4  # two schema files, the reset, and the customers loaded before the refusal
    assert audit_log[-1][0].action == "run_failed"
    assert audit_log[-1][0].reason == "step ingest raised Refused"
    assert "table_loaded" not in _actions(audit_log)[3:]


def test_a_refused_insert_in_gold_leaves_gold_with_its_previous_content(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """The truncate and the inserts share one transaction: a refusal means no commit, so nothing was emptied."""
    monkeypatch.setattr(gold, "execute_sql_file", lambda path, settings=None: None)
    fake_database.fail_on("INSERT INTO gold.fact_transactions", Refused("column does not exist"))

    with pytest.raises(Refused, match="column does not exist"):
        gold.build(settings)

    assert fake_database.commits == 0
    assert "gold_table_loaded" not in _actions(audit_log)
    assert audit_log[-1][0].reason == "step gold_build raised Refused"


def test_a_refused_aggregate_writes_no_reconciliation_row(
    fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]], settings: Settings
) -> None:
    fake_database.fail_on("GROUP BY channel", Refused("relation does not exist"))

    with pytest.raises(Refused):
        reconciliation.run_reconciliation(settings)

    assert fake_database.find("INSERT INTO reconciliation_results") == []
    assert fake_database.commits == 0
    assert "coherence_summary_written" not in _actions(audit_log)


def test_a_refused_control_query_issues_no_verdict_and_commits_no_result(
    fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]], settings: Settings
) -> None:
    """FORBIDDEN PATH: a verdict computed from six controls out of seven and persisted as if complete."""
    for fragment in ("transaction_id IS NULL", "HAVING COUNT(*) > 1", "amount_chf <= 0", "LEFT JOIN accounts"):
        fake_database.answer(fragment, rows=[(0,)])
    fake_database.fail_on("risk_score < 0", Refused("column risk_score does not exist"))

    with pytest.raises(Refused):
        run_quality_checks(settings)

    assert fake_database.commits == 0
    assert "verdict_issued" not in _actions(audit_log)
    assert "controls_completed" not in _actions(audit_log)
    assert audit_log[-1][0].action == "run_failed"


def test_a_refused_injection_commits_no_demo_row_and_claims_no_success(
    fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    fake_database.answer("SELECT COUNT(*) FROM transactions WHERE source_system = %s", rows=[(0,)])
    fake_database.answer("SELECT account_id FROM accounts", rows=[("ACC-1",)])
    fake_database.fail_on("INSERT INTO transactions", Refused("duplicate key"))

    with pytest.raises(Refused):
        demo_scenario.inject()

    assert fake_database.commits == 0
    assert "demo_batch_injected" not in _actions(audit_log)
    assert "violation_injected" not in _actions(audit_log)


def test_a_refused_reset_deletes_nothing_and_claims_no_removal(
    fake_database: FakeDatabase, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    fake_database.answer("SELECT COUNT(*) FROM transactions WHERE source_system <> %s", rows=[(1000,)])
    fake_database.fail_on("DELETE FROM transactions", Refused("lock timeout"))

    with pytest.raises(Refused, match="lock timeout"):
        demo_scenario.reset()

    assert fake_database.commits == 0
    assert "demo_batch_removed" not in _actions(audit_log)
    assert fake_database.find("DELETE FROM incident_reports") == []


def test_a_refused_insert_of_the_queue_leaves_the_previous_queue_in_place(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    raw_frame: pd.DataFrame,
    model_dir: Path,
    extract_returns,
) -> None:
    """The ``TRUNCATE`` of ``gold.predictions`` is sent first; without a commit it does not take effect."""
    X, y = F.split_x_y(F.build(raw_frame))
    pipeline = _build_estimator("baseline").fit(X, y)
    model_dir.mkdir(exist_ok=True)
    joblib.dump(
        {"pipeline": pipeline, "threshold": 0.5, "report": {"model_version": "v1", "model_family": "logistic"}},
        model_dir / "suspicion_classifier.joblib",
    )
    extract_returns(raw_frame)
    fake_database.fail_on("INSERT INTO gold.predictions", Refused("check constraint priority_band"))

    with pytest.raises(Refused):
        P.predict()

    assert fake_database.commits == 0
    assert fake_database.sql_sent()[0] == "TRUNCATE TABLE gold.predictions"
    assert "predictions_written" not in _actions(audit_log)


def test_a_transactions_file_with_no_content_at_all_stops_the_cleaning_before_any_silver_file(
    tmp_path: Path, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    bronze, silver = tmp_path / "bronze", tmp_path / "silver"
    bronze.mkdir()
    (bronze / "synthetic_transactions.csv").write_text("", encoding="utf-8")

    with pytest.raises(pd.errors.EmptyDataError):
        cleaning.clean_bronze_to_silver(bronze, silver)

    assert list(silver.iterdir()) == []
    assert audit_log[-1][0].action == "run_failed"


def test_a_sql_file_that_cannot_be_applied_creates_no_star_schema_view_and_claims_none(
    monkeypatch: pytest.MonkeyPatch, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    def refuse(path: Path, settings: Settings | None = None) -> None:
        raise Refused(f"syntax error in {path.name}")

    monkeypatch.setattr(powerbi_views, "execute_sql_file", refuse)

    with pytest.raises(Refused, match="06_powerbi_star_schema.sql"):
        powerbi_views.create_views()

    assert "star_schema_views_created" not in _actions(audit_log)
    assert audit_log[-1][0].reason == "step powerbi_views raised Refused"
