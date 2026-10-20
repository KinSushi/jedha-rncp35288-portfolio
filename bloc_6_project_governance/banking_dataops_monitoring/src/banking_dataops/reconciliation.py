#!/usr/bin/env python3
"""Aggregate summaries of the batch, and a coherence check recorded in ``reconciliation_results``.

A batch can pass every row-level control and still be wrong as a whole: a feed that delivers half
its rows produces perfectly valid transactions and a totally invalid total. This module looks at the
aggregates, which no per-row control can see.

CHOIX
-----
Retained:

- Five aggregations of the Silver ``transactions`` table, each answering a different question: by
  source system (is one feed missing?), by booking date (is one day empty?), by channel (does one
  channel carry an unusual risk?), by status, and by suspicious flag. A single aggregation can hide a
  problem another one shows.
- Amounts are summed in SQL, rounded to two decimals, and carried as ``Decimal`` in Python so that
  money is not turned into a binary float.
- What is written down is a coherence check, not a reconciliation. ``amount_delta`` and ``count_delta``
  are always zero because both sides come from the same table. The audit event is called
  ``coherence_summary_written`` rather than ``reconciled`` so that the journal claims no more than the code
  does. The limit is stated here instead of being discovered by a reader.
- One invariant: the reconciled batch is not empty.

Rejected:

- A real reconciliation against a second source of truth (a core-banking extract): there is no such
  source in the project; the limitation is recorded as risk R-09 (docs/project/05-registre-des-risques.md).
- Calling the empty-delta result "reconciled": it would read as a guarantee the data cannot give.

OÙ
---
Role: reporting on the batch, after the Silver load. Callers: ``python -m banking_dataops.reconciliation``
(Makefile target ``reconcile``) and ``docker/entrypoint.sh`` at start-up. ``run_pipeline.py`` does NOT call
it. Depends on ``banking_dataops.db``, ``banking_dataops.audit`` and ``banking_dataops.config``. Reads
``transactions``; writes one row to ``reconciliation_results`` per run (``sql/00_schema.sql``). The
dashboard reads the aggregates through ``banking_dataops.monitoring``. ``sql/03_reconciliation_queries.sql``
holds a readable copy of the queries, run by nothing.

COMMENT
-------
``run_reconciliation`` runs the five queries, logs each at DEBUG, sums the source-system counts and
amounts, checks that the total count is positive, inserts one summary row with the same figure on both
sides, and journals it. ``source_system_summary`` returns the first aggregation as typed objects. The queries
are module constants, with no parameter: nothing external reaches the SQL text.
Tests that prove it: tests/test_reconciliation.py
(``test_the_queries_are_selects_over_transactions_and_a_summary_has_its_fields_and_cannot_be_edited``). The
queries themselves need PostgreSQL and run in the smoke check
(``scripts/smoke_check.py``).

POURQUOI
--------
- Several aggregations rather than one: a total can be right while one source system is missing or one
  day has no transaction.
- ``Decimal`` for amounts: floating point cannot represent most decimal amounts exactly.
- The structurally zero delta is kept explicit rather than computed, so that nobody reads a real
  reconciliation into a coherence check (the comment at the insert, and docs/data_dictionary.md on
  ``amount_delta``).
- An empty batch is refused: a zero total is indistinguishable from a legitimately empty day, and the summary
  would prove nothing.

JOURNALISATION
--------------
Under ``RunJournal("reconciliation")``: the run start and end, one DEBUG record ``summary_computed`` per
aggregation (category ``data``, with the row count), the invariant ``the reconciled batch is not empty``
(category ``integrity``) and the event ``coherence_summary_written`` (category ``data``, with the batch
total and the id of the summary row). The DEBUG records are not shown at the default INFO level. A violated
invariant raises. ``main`` prints each aggregate as a table.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

import pandas as pd

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings, load_settings
from banking_dataops.db import connect, fetch_dataframe
from banking_dataops.logging_config import get_logger

logger = get_logger("reconciliation")


@dataclass(frozen=True)
class SourceSystemSummary:
    """Transaction count and CHF total of one source system."""

    source_system: str
    transaction_count: int
    total_amount_chf: Decimal


# Count, amount (rounded to 2 decimals) and first/last event per source system.
SOURCE_SYSTEM_QUERY = """
SELECT
    source_system,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    MIN(event_timestamp) AS first_event_timestamp,
    MAX(event_timestamp) AS last_event_timestamp
FROM transactions
GROUP BY source_system
ORDER BY source_system
"""

# Count, amount and suspicious count per booking date. FILTER counts only the suspicious rows.
DAILY_RECONCILIATION_QUERY = """
SELECT
    booking_date,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    COUNT(*) FILTER (WHERE is_suspicious) AS suspicious_count
FROM transactions
GROUP BY booking_date
ORDER BY booking_date
"""

# Count, average risk score and number of non-posted transactions per channel.
CHANNEL_SUMMARY_QUERY = """
SELECT
    channel,
    COUNT(*) AS transaction_count,
    ROUND(AVG(risk_score), 4) AS avg_risk_score,
    COUNT(*) FILTER (WHERE status <> 'posted') AS non_posted_count
FROM transactions
GROUP BY channel
ORDER BY transaction_count DESC
"""

STATUS_SUMMARY_QUERY = """
SELECT
    status,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf
FROM transactions
GROUP BY status
ORDER BY status
"""

# is_suspicious comes from the generator's rule (risk_score above 0.75), so the suspicious group has a
# higher average score by construction.
SUSPICIOUS_SUMMARY_QUERY = """
SELECT
    is_suspicious,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    ROUND(AVG(risk_score), 4) AS avg_risk_score
FROM transactions
GROUP BY is_suspicious
ORDER BY is_suspicious DESC
"""


def source_system_summary(settings: Settings | None = None) -> list[SourceSystemSummary]:
    """Return transaction counts and totals by source system, as typed objects."""
    frame = fetch_dataframe(SOURCE_SYSTEM_QUERY, settings=settings)
    return [
        SourceSystemSummary(
            source_system=str(row["source_system"]),
            transaction_count=int(row["transaction_count"]),
            total_amount_chf=Decimal(str(row["total_amount_chf"])),
        )
        for _, row in frame.iterrows()
    ]


def run_reconciliation(settings: Settings | None = None) -> dict[str, pd.DataFrame]:
    """Aggregate the batch five ways and persist one global coherence summary.

    A batch can pass every row-level control and still be wrong as a whole; reconciliation looks at
    the aggregate, which no per-row control can see.

    Known limitation, stated here rather than discovered by a reader: ``amount_delta`` is always
    zero. The aggregates are compared with themselves, so this is a coherence check, not a
    reconciliation between two independent systems. A real one needs a second source of truth, which
    is outside the current scope. It is journalled as ``coherence_summary_written`` for that reason.
    """
    runtime = settings or load_settings()
    with RunJournal("reconciliation", settings=runtime) as run:
        summaries = {
            "source_system": fetch_dataframe(SOURCE_SYSTEM_QUERY, runtime),
            "daily": fetch_dataframe(DAILY_RECONCILIATION_QUERY, runtime),
            "channel": fetch_dataframe(CHANNEL_SUMMARY_QUERY, runtime),
            "status": fetch_dataframe(STATUS_SUMMARY_QUERY, runtime),
            "suspicious": fetch_dataframe(SUSPICIOUS_SUMMARY_QUERY, runtime),
        }
        for name, frame in summaries.items():
            logger.debug(
                "summary_computed",
                extra={
                    "category": "data",
                    "reason": f"{name} aggregate built for the reconciliation report",
                    "rows": len(frame),
                },
            )
        total_count = int(summaries["source_system"]["transaction_count"].sum())
        total_amount = Decimal(str(summaries["source_system"]["total_amount_chf"].sum()))
        # An empty aggregate means the batch never reached Silver, or that every row was filtered
        # out. Either way the totals below would be zero and look like a legitimately empty day.
        assert_invariant(
            total_count > 0,
            invariant="the reconciled batch is not empty",
            reason=(
                "a zero total is indistinguishable from a legitimately empty day; "
                "continuing would publish a summary that proves nothing"
            ),
            total_count=total_count,
        )

        with connect(runtime) as connection:
            with connection.cursor() as cursor:
                reconciliation_id = f"REC-{uuid4().hex[:12].upper()}"
                cursor.execute(
                    """
                    INSERT INTO reconciliation_results
                        (reconciliation_id, reconciliation_name, source_count, target_count,
                         count_delta, source_total, target_total, amount_delta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        reconciliation_id,
                        "source_system_total",
                        total_count,
                        total_count,
                        # count_delta and amount_delta are structurally zero: both sides of this
                        # comparison come from the same table. Kept explicit rather than computed,
                        # so nobody reads a real reconciliation into a coherence check.
                        0,
                        total_amount,
                        total_amount,
                        Decimal("0.00"),
                    ),
                )
            connection.commit()
        run.note(
            "coherence_summary_written",
            reason=(
                "aggregate coherence check across source systems; NOT a "
                "reconciliation between two independent systems - both sides come "
                "from the same table, so the delta is structurally zero"
            ),
            row_count=total_count,
            reconciliation_id=reconciliation_id,
            total_amount_chf=str(total_amount),
            summaries=list(summaries),
        )

        return summaries


def print_reconciliation_summary(summary: dict[str, pd.DataFrame]) -> None:
    """Print each aggregate as a text table, for the command line."""
    for name, frame in summary.items():
        print(f"\n## {name}")
        print(frame.to_string(index=False))


def main() -> None:
    """Command-line entry point: run the reconciliation and print the aggregates."""
    print_reconciliation_summary(run_reconciliation())


if __name__ == "__main__":
    main()
