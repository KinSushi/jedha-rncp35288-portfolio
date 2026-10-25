#!/usr/bin/env python3
"""The seven deterministic data-quality controls and the publication verdict.

This is where a batch is judged. Every other layer reads what happens here: the dashboard, the API, the
Power BI views and the incident report all start from the rows this module writes.

CHOIX
-----
Retained:

- Seven controls, each one a named SQL query that returns a count, evaluated on the Silver tables
  (CTRL-001 to CTRL-007, listed in ``get_quality_checks``). A control is explainable in one sentence and
  gives the same answer on the same data, indefinitely (ADR-0002).
- Three statuses. ``PASS`` when nothing fails, ``FAIL`` when a blocking control finds rows, ``WARN`` when a
  ``warn_only`` control finds rows (only CTRL-007 is warn-only). The batch verdict is ``FAIL`` if any control
  fails, else ``WARN`` if any warns, else ``PASS``.
- A failed control opens an incident (``incident_reports``, status ``open``) and records a ``decision``
  event.
- Every control leaves one audit event, passing or failing, carrying a business reason from
  ``CONTROL_REASONS``: a control that runs silently cannot be told from one that never ran.
- The verdict is derived from the controls only. No model score enters it (ADR-0002, ADR-0008).
- Structure is left to the schema and meaning to the controls (ADR-0004): CTRL-001, CTRL-002 and CTRL-004
  cannot fail on rows inserted into that schema, and are kept so that a control that never fires is shown
  not to fire.

Rejected:

- A model as a blocking criterion: an auditor cannot accept "the model judged so" (ADR-0002).
- Putting the thresholds in the schema: it would freeze business choices in the DDL (ADR-0004).
- Stopping the pipeline on a failed control: the failure is a verdict to publish, not an exception; the
  dashboard shows it and the incident is raised. Source : inconnue (no ADR states this explicitly).

OÙ
---
Role: the control layer, between the Silver load and the Gold build. Callers: ``run_pipeline.py`` (step
``silver``), ``python -m banking_dataops.quality_checks`` (Makefile target ``quality``), ``demo_scenario``
after injecting or resetting its batch, and the dashboard's button. Depends on ``banking_dataops.db``,
``banking_dataops.audit`` and ``banking_dataops.config``. Reads ``transactions`` and ``accounts``; writes
``quality_check_results`` (one row per control, replaced on each run) and ``incident_reports``
(``sql/00_schema.sql``). Read by ``monitoring``, ``api`` (``/verdict``, ``/controls``, ``/incidents``) and the
views of ``sql/06_powerbi_star_schema.sql``. ``sql/02_data_quality_checks.sql`` holds a readable copy of the
same predicates; no test compares the two, so a rule change has to be made in both.

COMMENT
-------
``run_quality_checks`` opens a ``RunJournal``, runs each query in declaration order, derives the status with
``status_from_failed_rows``, journals the control, and upserts its result (the primary key is the control
identifier, so a rerun replaces the previous row rather than adding one). Then, in the same transaction, it
opens an incident for every ``FAIL``. After the commit it checks that there is exactly one result per
declared control, computes the verdict, journals ``verdict_issued`` and ``controls_completed`` and returns
the results.
Tests that prove it: tests/test_quality_checks.py, tests/test_reverse_paths.py
(``test_the_verdict_blocks_on_a_failure_not_on_a_warning_is_503_when_it_cannot_decide_and_health_stays_up``,
``test_every_control_has_a_business_reason_and_the_decision_trail_is_complete_and_the_check_refuses_a_gap``,
``test_every_demo_violation_breaks_its_control_fits_the_schema_carries_a_reason_and_cannot_be_edited``). The SQL
itself needs PostgreSQL and is exercised by ``scripts/smoke_check.py`` on a running stack.

POURQUOI
--------
- CHF 1,000,000 ceiling (CTRL-003), score range [0, 1] (CTRL-005), 60-day freshness (CTRL-007): the
  thresholds and their stated origins are in docs/KPI_DEFINITIONS.md:54 (plausibility
  bound for retail banking, to recalibrate on real data), the contract of the scoring engine for the score
  range, and two monthly closing cycles for the freshness.
- CTRL-007 is warn-only because a stale dataset signals a broken feed rather than bad data
  (``CONTROL_REASONS``). It compares the newest ``event_timestamp`` with the wall clock, not with the date
  anchor of the batch (ADR-0009): a batch generated with the default anchor 2026-07-27 therefore reads
  ``WARN`` from about 60 days after its last event, that is from 25 September 2026, whatever the pipeline did.
- One event per control, success included: docs/CODE_STANDARD.md, section 3, on journalling successes.
- An incident per failed control, status ``open``: the only status this module writes. The schema
  places no constraint on ``status`` and no code closes an incident.

JOURNALISATION
--------------
Under ``RunJournal("quality_checks")``: one ``control`` event per control named ``control_pass``,
``control_warn`` or ``control_fail`` (with the business reason, the failed row count, the control, the check
name and the severity), one ``decision`` event ``incident_opened`` per failed control, the invariant
``one result per declared control`` (category ``integrity``), the ``decision`` event ``verdict_issued``
(verdict, number of controls and of failed controls) and ``controls_completed``. Events go to the
structured log at INFO and, best effort, to ``audit_events``. A violated invariant raises. Only ``main``
prints, one line per control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from banking_dataops.audit import RunJournal, assert_invariant, record_event
from banking_dataops.config import Settings, load_settings
from banking_dataops.db import connect
from banking_dataops.logging_config import get_logger

logger = get_logger("quality_checks")

#: Why each control exists, in business terms. The audit journal refuses an event without a reason, and a
#: reason written next to the rule cannot drift away from it: an auditor reading ``audit_events.reason``
#: gets the same sentence a developer reads here.
CONTROL_REASONS: dict[str, str] = {
    "CTRL-001": (
        "a null in a mandatory field means an upstream system dropped a value; "
        "publishing it corrupts every aggregate that column feeds"
    ),
    "CTRL-002": (
        "a duplicate transaction is counted twice in every total, and the "
        "duplication becomes invisible once the batch is consolidated"
    ),
    # The 1,000,000 CHF ceiling is a retail-banking plausibility bound (docs/KPI_DEFINITIONS.md).
    "CTRL-003": (
        "an amount outside the plausibility band usually means a decimal shift or a currency applied twice upstream"
    ),
    "CTRL-004": ("a transaction attached to a non-existent account cannot be reconciled, attributed or investigated"),
    "CTRL-005": (
        "a risk score outside its contractual range means the scoring engine "
        "returned a raw value instead of a normalised one"
    ),
    "CTRL-006": (
        "a status outside the reference list means an upstream system changed without the referential being aligned"
    ),
    "CTRL-007": (
        "a stale dataset signals a broken feed rather than bad data; it warns "
        "instead of blocking, because the batch itself may still be valid"
    ),
}


@dataclass(frozen=True)
class QualityCheck:
    """Definition of one control: identifier, name, severity, the SQL returning its count, and whether it only warns.

    Frozen so that a rule cannot be changed at run time by accident.
    """

    control_id: str
    check_name: str
    severity: str
    query: str
    warn_only: bool = False


@dataclass(frozen=True)
class QualityCheckResult:
    """The outcome of one control at one execution: an audit record, frozen once computed."""

    control_id: str
    check_name: str
    status: str
    failed_rows: int
    severity: str
    executed_at: datetime


def status_from_failed_rows(failed_rows: int, warn_only: bool = False) -> str:
    """Return ``PASS`` for 0 failed rows, otherwise ``WARN`` for a warn-only control and ``FAIL`` for the rest.

    One function decides, so that no control can treat ``warn_only`` differently from another, and so that
    the rule can be tested in isolation.
    """
    if failed_rows == 0:
        return "PASS"
    return "WARN" if warn_only else "FAIL"


def get_quality_checks() -> list[QualityCheck]:
    """Return the seven control definitions, in the order they run and are reported.

    A function and not a constant so that a test can substitute its own list. Each query returns one
    number: the count of offending rows (for CTRL-007, a 1 or 0 flag). Severities: high for CTRL-001 to
    CTRL-004, medium for CTRL-005 and CTRL-006, low for CTRL-007.
    """
    return [
        # Cannot fail on rows inserted into the schema (primary key and NOT NULL columns).
        QualityCheck(
            control_id="CTRL-001",
            check_name="critical_nulls",
            severity="high",
            query="""
                SELECT COUNT(*)
                FROM transactions
                WHERE transaction_id IS NULL
                   OR account_id IS NULL
                   OR event_timestamp IS NULL
                   OR amount_chf IS NULL
            """,
            warn_only=False,
        ),
        # Cannot fail on rows inserted into the schema (transaction_id is the primary key).
        QualityCheck(
            control_id="CTRL-002",
            check_name="duplicate_transactions",
            severity="high",
            query="""
                SELECT COUNT(*)
                FROM (
                    SELECT transaction_id
                    FROM transactions
                    GROUP BY transaction_id
                    HAVING COUNT(*) > 1
                ) duplicates
            """,
            warn_only=False,
        ),
        # Amounts must be positive and not above CHF 1,000,000 (docs/KPI_DEFINITIONS.md).
        QualityCheck(
            control_id="CTRL-003",
            check_name="invalid_amounts",
            severity="high",
            query="""
                SELECT COUNT(*)
                FROM transactions
                WHERE amount_chf <= 0
                   OR amount_chf > 1000000
            """,
            warn_only=False,
        ),
        # LEFT JOIN so that a transaction without an account survives the join and can be counted.
        # Cannot fail on rows inserted into the schema (foreign key).
        QualityCheck(
            control_id="CTRL-004",
            check_name="orphan_transactions",
            severity="high",
            query="""
                SELECT COUNT(*)
                FROM transactions t
                LEFT JOIN accounts a ON t.account_id = a.account_id
                WHERE a.account_id IS NULL
            """,
            warn_only=False,
        ),
        # The score range [0, 1] is the contract of the scoring engine, not a business choice.
        QualityCheck(
            control_id="CTRL-005",
            check_name="invalid_risk_score",
            severity="medium",
            query="""
                SELECT COUNT(*)
                FROM transactions
                WHERE risk_score < 0
                   OR risk_score > 1
            """,
            warn_only=False,
        ),
        # The list is repeated in the generator and in pipeline/cleaning.py (KNOWN_STATUSES).
        QualityCheck(
            control_id="CTRL-006",
            check_name="invalid_status",
            severity="medium",
            query="""
                SELECT COUNT(*)
                FROM transactions
                WHERE status NOT IN ('posted', 'pending', 'rejected')
            """,
            warn_only=False,
        ),
        # Freshness against the WALL CLOCK, not the batch anchor (see the module docstring).
        # Warn-only, and on an empty table MAX is null so the flag is 0.
        QualityCheck(
            control_id="CTRL-007",
            check_name="stale_data",
            severity="low",
            query="""
                SELECT CASE
                    WHEN MAX(event_timestamp) < CURRENT_TIMESTAMP - INTERVAL '60 days'
                    THEN 1
                    ELSE 0
                END
                FROM transactions
            """,
            warn_only=True,
        ),
    ]


def run_quality_checks(settings: Settings | None = None) -> list[QualityCheckResult]:
    """Run every control, persist the results, open an incident per failure, issue the verdict.

    This is where the publication verdict is decided; the other layers only read it. Returns exactly
    one result per declared control, in declaration order, and an invariant enforces it: a missing
    result would silently shrink the verdict (five green controls out of five look the same as seven
    out of seven on a dashboard that only shows a ratio).

    Returns normally even when controls fail: a failure is a verdict, journalled and shown, not an
    exception.
    """
    runtime = settings or load_settings()
    results: list[QualityCheckResult] = []
    executed_at = datetime.now(UTC)
    checks = get_quality_checks()
    with RunJournal("quality_checks", settings=runtime) as run:
        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                for check in checks:
                    cursor.execute(check.query)
                    row = cursor.fetchone()
                    assert row is not None, "a COUNT or CASE query always returns one row"
                    failed_rows = int(row[0] or 0)
                    result = QualityCheckResult(
                        control_id=check.control_id,
                        check_name=check.check_name,
                        status=status_from_failed_rows(failed_rows, check.warn_only),
                        failed_rows=failed_rows,
                        severity=check.severity,
                        executed_at=executed_at,
                    )

                    results.append(result)
                    # One event per control, pass or fail. Journalling only the failures would make
                    # a silent control indistinguishable from one that never ran.
                    record_event(
                        "control",
                        f"control_{result.status.lower()}",
                        reason=CONTROL_REASONS.get(
                            check.control_id,
                            "control evaluated; no business reason declared for this identifier",
                        ),
                        row_count=failed_rows,
                        settings=runtime,
                        control_id=result.control_id,
                        check_name=result.check_name,
                        severity=result.severity,
                    )

                    # The control identifier is the key: one row per control, replaced on each run.
                    cursor.execute(
                        """
                        INSERT INTO quality_check_results
                            (check_id, control_id, check_name, status, failed_rows,
                             severity, executed_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (check_id)
                        DO UPDATE SET
                            control_id = EXCLUDED.control_id,
                            check_name = EXCLUDED.check_name,
                            status = EXCLUDED.status,
                            failed_rows = EXCLUDED.failed_rows,
                            severity = EXCLUDED.severity,
                            executed_at = EXCLUDED.executed_at
                        """,
                        (
                            check.control_id,
                            result.control_id,
                            result.check_name,
                            result.status,
                            result.failed_rows,
                            result.severity,
                            result.executed_at,
                        ),
                    )

                failed = [result for result in results if result.status == "FAIL"]
                for result in failed:
                    incident_id = f"INC-{uuid4().hex[:12].upper()}"
                    cursor.execute(
                        """
                        INSERT INTO incident_reports
                            (incident_id, title, severity, status, failed_control,
                             summary, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (incident_id) DO NOTHING
                        """,
                        (
                            incident_id,
                            f"Failed data control: {result.check_name}",
                            result.severity,
                            "open",
                            result.control_id,
                            f"{result.control_id} failed with {result.failed_rows} row(s).",
                            result.executed_at,
                        ),
                    )

                    record_event(
                        "decision",
                        "incident_opened",
                        reason=(
                            "a failed control blocks publication; the incident is the "
                            "object an analyst works from, and the trace an auditor reads"
                        ),
                        row_count=result.failed_rows,
                        settings=runtime,
                        incident_id=incident_id,
                        control_id=result.control_id,
                    )

            connection.commit()

        assert_invariant(
            len(results) == len(checks),
            invariant="one result per declared control",
            reason=(
                "a missing control result would make the verdict look complete "
                "while part of the batch was never evaluated"
            ),
            declared=len(checks),
            produced=len(results),
        )

        verdict = "FAIL" if failed else ("WARN" if any(r.status == "WARN" for r in results) else "PASS")
        record_event(
            "decision",
            "verdict_issued",
            reason=(
                "the publication decision for this batch, derived only from the "
                "deterministic controls - never from a model score (ADR-0002)"
            ),
            row_count=sum(r.failed_rows for r in results if r.status == "FAIL"),
            settings=runtime,
            verdict=verdict,
            controls_total=len(results),
            controls_failed=len(failed),
        )

        run.note(
            "controls_completed",
            reason="every declared control was evaluated and its outcome persisted",
            row_count=len(results),
            verdict=verdict,
        )

    return results


def print_quality_results(results: list[QualityCheckResult]) -> None:
    """Print one ``control | status | severity | name | failed=N`` line per result."""
    for result in results:
        print(
            f"{result.control_id} | {result.status} | {result.severity} | "
            f"{result.check_name} | failed={result.failed_rows}"
        )


def main() -> None:
    """Command-line entry point: run all controls and print the results."""
    print_quality_results(run_quality_checks())


if __name__ == "__main__":
    main()
