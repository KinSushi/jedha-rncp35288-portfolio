#!/usr/bin/env python3
"""Demonstration scenario: inject a degraded batch, watch the controls catch it, remove it exactly.

A monitoring system that is never seen failing cannot be trusted. A green dashboard proves nothing: it is
indistinguishable from a dashboard whose controls do not run at all. This module makes the detection path
observable on demand, in front of an audience, and reproducibly.

CHOIX
-----
Retained:

- Three deliberate violations, one per business rule the database cannot express: an amount of CHF 1,850,000
  (CTRL-003), a risk score of 1.42 (CTRL-005) and a status ``settled`` outside the reference list (CTRL-006).
  Enough to tell a story in about ninety seconds.
- The other three structural controls cannot be broken this way. CTRL-001 (nulls), CTRL-002 (duplicates) and
  CTRL-004 (orphans) are guaranteed by ``NOT NULL``, the primary key and a foreign key, so the schema refuses
  such rows. That is the design thesis of ADR-0004: the database guarantees structure, the control layer
  guarantees meaning.
- Every injected row carries ``source_system = 'demo_scenario'`` and a ``DEMO-TXN-`` identifier. The column
  already exists and feeds the summaries, so no migration is needed, and the removal deletes on that marker
  alone: reference data is never in scope. An invariant asserts it after each reset.
- The whole batch is inserted in one transaction: a half-degraded state during a live presentation would be
  unmanageable and its verdict impossible to explain.
- Refusals are clean, not tracebacks: injecting twice is skipped and journalled, resetting a clean database
  deletes nothing, injecting with no account raises with the command to run, an unknown command-line action
  stops instead of defaulting to a destructive one.

Rejected:

- Creating an account when none exists: the created account would survive the reset and make it inexact.
- Weakening the schema to let every control fail: it would contradict ADR-0004.
- Calling the scenario from the pipeline: it is demonstration apparatus, not production behaviour.

OÙ
---
Role: demonstration support, outside the production path. Callers: the dashboard buttons
(``dashboard/streamlit_app.py``), ``python -m banking_dataops.demo_scenario inject|reset|status`` (Makefile
targets ``demo-inject``, ``demo-reset``, ``demo-status`` and their ``-app`` variants) and ``run.ps1``. Never
``run_pipeline.py``. Depends on ``banking_dataops.db``, ``banking_dataops.audit`` and
``banking_dataops.config``. Writes to and deletes from ``transactions`` and deletes open rows of
``incident_reports``. The model excludes these rows from its training set (``ml.features``).

COMMENT
-------
``inject`` refuses to run twice, picks an existing account, inserts the three violations with ``ON CONFLICT DO
NOTHING``, journals each one, then checks that exactly three marked rows exist. ``reset`` counts the unmarked
rows, deletes the marked ones (and, by default, the open incidents of the three controls), counts the unmarked
rows again and asserts the two counts are equal. After either, the caller re-runs the controls
(``quality_checks``) to see the verdict change. The injected rows are stamped with the current time, not the
batch anchor of ADR-0009.
Tests that prove it: tests/test_demo_scenario.py and tests/test_reverse_paths.py
(``test_the_demo_reset_is_scoped_to_its_marker_and_a_mistyped_action_is_refused_instead_of_read_as_reset``,
``test_every_demo_violation_breaks_its_control_fits_the_schema_carries_a_reason_and_cannot_be_edited``).

POURQUOI
--------
- Three of seven controls cannot fail on inserted rows: ADR-0004, section "Justification".
- A marker that never collides with a generated source system (``core_banking``, ``card_processor``,
  ``payments_gateway``): otherwise a reset would delete real rows. A test asserts the absence of collision.
- The ``DELETE`` of incidents is limited to open incidents whose failed control is one of the three demo
  controls, so incidents raised by real failures survive.
- The reset invariant compares counts before and after: the guarantee is checked, not trusted.

JOURNALISATION
--------------
Under ``RunJournal("demo_inject")`` and ``RunJournal("demo_reset")``: the events ``violation_injected`` (one per
row, category ``demo``, with the business reason of the violation), ``injection_skipped`` when rows are already
present, ``demo_batch_injected`` and ``demo_batch_removed`` (with the number of incidents purged), and one
invariant each (category ``integrity``). A DEBUG record names the account chosen. Events go to the structured
log at INFO and, best effort, to ``audit_events``. ``main`` prints the result of each action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from banking_dataops.audit import RunJournal, assert_invariant, record_event
from banking_dataops.config import Settings, load_settings
from banking_dataops.db import connect
from banking_dataops.logging_config import get_logger

logger = get_logger("demo_scenario")
#: Marker written to ``transactions.source_system`` for every injected row. It must never collide with
#: a generated source system, otherwise a reset would delete real rows.
DEMO_SOURCE_SYSTEM = "demo_scenario"
#: Prefix of every injected transaction identifier: an injected row is recognisable by eye, not only by query.
DEMO_ID_PREFIX = "DEMO-TXN-"


@dataclass(frozen=True)
class DemoViolation:
    """A deliberate anomaly, and the sentence that explains it to an audience."""

    transaction_id: str
    control_id: str
    control_name: str
    narrative: str
    business_reason: str
    amount_chf: Decimal
    risk_score: Decimal
    status: str


#: One violation per business rule the schema cannot express: an amount, a score and a status.
DEMO_VIOLATIONS: tuple[DemoViolation, ...] = (
    DemoViolation(
        transaction_id=f"{DEMO_ID_PREFIX}0001",
        control_id="CTRL-003",
        control_name="invalid_amounts",
        narrative="A transaction of CHF 1,850,000 — far above the plausibility ceiling.",
        business_reason=(
            "An amount above the ceiling usually means a decimal shift or a "
            "currency applied twice upstream. Publishing it distorts every "
            "aggregate it enters."
        ),
        amount_chf=Decimal("1850000.00"),
        risk_score=Decimal("0.4200"),
        status="posted",
    ),
    DemoViolation(
        transaction_id=f"{DEMO_ID_PREFIX}0002",
        control_id="CTRL-005",
        control_name="invalid_risk_score",
        narrative="A risk score of 1.42, outside the [0, 1] range the engine guarantees.",
        business_reason=(
            "A score outside its range means the scoring engine returned a raw "
            "value instead of a normalised one. Downstream thresholds become "
            "meaningless without anyone noticing."
        ),
        amount_chf=Decimal("742.50"),
        risk_score=Decimal("1.4200"),
        status="posted",
    ),
    DemoViolation(
        transaction_id=f"{DEMO_ID_PREFIX}0003",
        control_id="CTRL-006",
        control_name="invalid_status",
        narrative="A lifecycle status of 'settled', which is not in the reference list.",
        business_reason=(
            "A status outside the reference list means an upstream system was "
            "upgraded without the referential being aligned. Reconciliation by "
            "status silently stops balancing."
        ),
        amount_chf=Decimal("318.90"),
        risk_score=Decimal("0.1700"),
        status="settled",
    ),
)

#: Controls the demo batch is expected to break. Used to limit the incident purge in ``reset`` to these
#: three controls.
DEMO_CONTROL_IDS: tuple[str, ...] = tuple(v.control_id for v in DEMO_VIOLATIONS)

_INSERT_SQL = """
INSERT INTO transactions (
    transaction_id, account_id, source_system, event_timestamp, booking_date,
    amount_chf, currency, channel, merchant_category, country,
    risk_score, status, is_suspicious, created_at
)
VALUES (
    %(transaction_id)s, %(account_id)s, %(source_system)s, %(event_timestamp)s,
    %(booking_date)s, %(amount_chf)s, 'CHF', 'api', 'services', 'CH',
    %(risk_score)s, %(status)s, FALSE, %(created_at)s
)
ON CONFLICT (transaction_id) DO NOTHING
"""


def count_injected(settings: Settings | None = None) -> int:
    """Return how many demo rows are currently present.

    Used by the dashboard to disable the wrong button, and by ``make demo-status``.
    """

    with connect(settings or load_settings()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM transactions WHERE source_system = %s",
                (DEMO_SOURCE_SYSTEM,),
            )
            row = cursor.fetchone()
            assert row is not None, "COUNT(*) always returns one row"
            return int(row[0])


def is_injected(settings: Settings | None = None) -> bool:
    """Return True when the demo batch is present."""
    return count_injected(settings) > 0


def _pick_account_id(cursor: psycopg.Cursor[Any]) -> str:
    """Return an existing account identifier, so the foreign key is satisfied.

    It fails rather than creating an account: reference data created by a demonstration module would
    survive the reset and make it inexact. The message carries the command that loads the data.
    """

    cursor.execute("SELECT account_id FROM accounts ORDER BY account_id LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError(
            "No account found. The demo scenario needs reference data to attach "
            "its rows to. Run the ingestion pipeline first: "
            "`docker compose exec app python -m banking_dataops.ingest`."
        )
    return str(row[0])


def inject(settings: Settings | None = None) -> list[DemoViolation]:
    """Insert the degraded batch and return the violations applied.

    All rows go in one transaction: the whole batch lands or none of it does. Calling it twice is a
    no-op, journalled as such, and does not raise.
    """

    runtime = settings or load_settings()
    with RunJournal("demo_inject", settings=runtime) as run:
        already = count_injected(runtime)
        if already:
            # Refused, not raised: the caller is usually a button, and a traceback in front of an
            # audience is the worst possible outcome.
            record_event(
                "demo",
                "injection_skipped",
                reason=(
                    f"{already} demo row(s) already present; a second injection "
                    "would create an ambiguous state where the verdict could not "
                    "be attributed to a single act"
                ),
                row_count=already,
                settings=runtime,
            )

            return list(DEMO_VIOLATIONS)
        now = datetime.now(UTC).replace(tzinfo=None)
        booking_date = (now - timedelta(days=1)).date()
        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                account_id = _pick_account_id(cursor)
                logger.debug(
                    "anchor_account_selected",
                    extra={"account_id": account_id, "reason": "satisfies the foreign key"},
                )
                for violation in DEMO_VIOLATIONS:
                    cursor.execute(
                        _INSERT_SQL,
                        {
                            "transaction_id": violation.transaction_id,
                            "account_id": account_id,
                            "source_system": DEMO_SOURCE_SYSTEM,
                            "event_timestamp": now,
                            "booking_date": booking_date,
                            "amount_chf": violation.amount_chf,
                            "risk_score": violation.risk_score,
                            "status": violation.status,
                            "created_at": now,
                        },
                    )
                    record_event(
                        "demo",
                        "violation_injected",
                        reason=violation.business_reason,
                        row_count=1,
                        settings=runtime,
                        control_id=violation.control_id,
                        transaction_id=violation.transaction_id,
                    )

            connection.commit()
        landed = count_injected(runtime)
        # Fewer rows than violations means an ON CONFLICT swallowed one; more means rows from
        # another source carry the marker.
        assert_invariant(
            landed == len(DEMO_VIOLATIONS),
            invariant="injected_row_count == len(DEMO_VIOLATIONS)",
            reason=(
                "a mismatch means either a silent conflict or a collision on the "
                "source_system marker, both of which would make the reset inexact"
            ),
            expected=len(DEMO_VIOLATIONS),
            observed=landed,
        )

        run.note(
            "demo_batch_injected",
            reason="the degraded batch is in place; controls can now be re-evaluated",
            row_count=landed,
        )

        return list(DEMO_VIOLATIONS)


def reset(settings: Settings | None = None, purge_incidents: bool = True) -> int:
    """Remove the demo batch and return the number of deleted transactions.

    The deletion targets ``source_system = 'demo_scenario'`` and nothing else. An invariant verifies
    afterwards that the number of unmarked rows is unchanged: the guarantee that makes this module safe
    to run against a populated database.
    """

    runtime = settings or load_settings()
    with RunJournal("demo_reset", settings=runtime) as run:
        # Snapshot before, so the invariant below proves that reference data was untouched.
        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM transactions WHERE source_system <> %s",
                    (DEMO_SOURCE_SYSTEM,),
                )
                row = cursor.fetchone()
                assert row is not None, "COUNT(*) always returns one row"
                real_rows_before = int(row[0])
        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM transactions WHERE source_system = %s",
                    (DEMO_SOURCE_SYSTEM,),
                )
                deleted = cursor.rowcount
                if purge_incidents:
                    cursor.execute(
                        """
                        DELETE FROM incident_reports
                        WHERE status = 'open' AND failed_control = ANY(%s)
                        """,
                        (list(DEMO_CONTROL_IDS),),
                    )
                    purged = cursor.rowcount
                else:
                    purged = 0
            connection.commit()
        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM transactions WHERE source_system <> %s",
                    (DEMO_SOURCE_SYSTEM,),
                )
                row = cursor.fetchone()
                assert row is not None, "COUNT(*) always returns one row"
                real_rows_after = int(row[0])
        assert_invariant(
            real_rows_after == real_rows_before,
            invariant="reset touches only rows marked demo_scenario",
            reason=(
                "deleting reference data would corrupt every subsequent control "
                "result and make the demonstration destructive"
            ),
            before=real_rows_before,
            after=real_rows_after,
        )

        run.note(
            "demo_batch_removed",
            reason="the database is back to its pre-demonstration state",
            row_count=deleted,
            incidents_purged=purged,
            reference_rows_untouched=real_rows_after,
        )

        return deleted


def main() -> None:
    """CLI: ``python -m banking_dataops.demo_scenario [inject|reset|status]``."""
    import sys

    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    action = sys.argv[1] if len(sys.argv) > 1 else "inject"
    if action == "reset":
        deleted = reset()
        print(f"Demo batch removed ({deleted} transaction(s)).")
    elif action == "status":
        print(f"Demo rows present: {count_injected()}")
    elif action == "inject":
        for violation in inject():
            print(f"{violation.control_id} | {violation.control_name} | {violation.narrative}")
    else:
        # Refuse an unknown action rather than default to a destructive one.
        raise SystemExit(f"Unknown action {action!r}. Expected: inject, reset or status.")


if __name__ == "__main__":
    main()
