#!/usr/bin/env python3
"""Gold layer: rebuild the star schema tables that the dashboard, Power BI and the model read.

Silver is the validated source, normalised for integrity. Gold is shaped for reading: dimension and
fact tables that the dashboard, the Power BI report and the scoring step query several times per
session.

CHOIX
-----
Retained:

- Materialised tables, not views. The readers query Gold repeatedly; views would recompute on every
  read and make latency unpredictable, and a view could shift between the moment features are built
  and the moment predictions are written, while a table is a snapshot of the last build.
- Truncate then insert, in a single transaction, in a fixed order: dimensions first, then facts.
  Gold is derived and holds no state worth preserving, so rebuilding is always safe, and an UPSERT
  would be more complex and would keep rows whose transaction disappeared upstream.
- The Gold tables are filled from the views of ``sql/06_powerbi_star_schema.sql`` (dimensions and
  quality facts) and by a direct query for ``fact_transactions``, which adds the derived columns
  ``booking_hour``, ``is_night``, ``is_cross_border`` and ``is_high_amount`` (and a flag for demo rows).
- Two invariants after the load: the transaction fact table is not empty, and it has exactly as many
  rows as Silver.

Rejected:

- Reading the views directly from the dashboard: unpredictable latency, no snapshot (above).
- UPSERT: see above.
- A single definition of the derived columns for dashboard and model: they are computed here in SQL
  and again in ``banking_dataops.ml.features`` in Python, so the rules exist twice and no automated
  test compares them. Source : inconnue for why the model does not read these columns.

OÙ
---
Role: the Gold step, after the Silver load and the quality controls. Callers: ``run_pipeline.py`` (step
``gold``, which runs whether or not the controls passed: a failed control does not stop the
pipeline, it changes the verdict), ``python -m banking_dataops.pipeline.gold``, and the alias
``src/transformations/build_gold.py``. Depends on ``banking_dataops.db``, ``banking_dataops.audit`` and,
for the project root path only, ``banking_dataops.powerbi_views``. Reads the Silver tables and the
views of ``sql/06_powerbi_star_schema.sql``; writes the tables of ``sql/08_gold_layer.sql``. Read by
the dashboard (``monitoring``), by Power BI and by ``scripts/smoke_check.py``;
``gold.predictions`` is filled by ``banking_dataops.ml.predict``, not here.

COMMENT
-------
``build`` runs ``sql/06`` and ``sql/08`` (idempotent definitions of the views and the tables), then, in
one connection, truncates every Gold table in reverse load order and inserts them in load order,
recording the row count of each statement. After the commit it journals one event per table and checks
the two invariants. The table list ``_LOADS`` is the single place the order is written down.
Tests that prove it: no unit test runs ``build`` (it needs PostgreSQL). The invariants are exercised by
``scripts/smoke_check.py`` on a running stack, and tests/test_sql_files_exist.py checks that the SQL
files exist and tests/test_sql_splitter.py that every one splits into valid statements.

POURQUOI
--------
- Gold as tables, truncate then insert: the reasons recorded in the journal event ``gold_table_loaded``
  and ADR-0001 (replayable batch).
- The row-count invariant: a gap means the join to accounts or customers dropped rows, so every later
  aggregate and every model score would be computed on a subset nobody declared (the text of the
  ``assert_invariant`` call below).
- Night is before 06:00 and a high amount is above CHF 10,000: the same values as the generator that
  builds the risk score (src/banking_dataops/generate_synthetic_data.py:275
  and src/banking_dataops/generate_synthetic_data.py:277). The origin of 10,000 is
  a project choice, source : inconnue; it is not a regulatory threshold that the repository cites.
- ``TRUNCATE ... CASCADE`` on each table, facts first. No foreign key is declared in Gold today, so the
  cascade has nothing to follow; it is kept so that adding a key later does not make the truncate fail.

JOURNALISATION
--------------
One ``RunJournal("gold_build")``: the run start and end, one ``gold_table_loaded`` event per table
(category ``data``, with the row count) and the two invariants (category ``integrity``). They go to the
structured log at INFO and, best effort, to ``audit_events``. A violated invariant raises
``AssertionError`` and closes the run as failed. ``main`` prints one line per table.
"""

from __future__ import annotations

from typing import cast

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings
from banking_dataops.db import connect, execute_sql_file, fetch_rows
from banking_dataops.logging_config import get_logger
from banking_dataops.powerbi_views import ROOT

logger = get_logger("pipeline.gold")

GOLD_SQL = ROOT / "sql" / "08_gold_layer.sql"

#: (table, statement) pairs in load order: dimensions first, then facts, as in any star schema. No foreign
#: key enforces the order (sql/08_gold_layer.sql declares none), so it is a convention that keeps the
#: dimensions in place when a fact is loaded. The same list, reversed, gives the truncate order.
_LOADS: tuple[tuple[str, str], ...] = (
    (
        "gold.dim_date",
        """
        INSERT INTO gold.dim_date
        SELECT date_key, year, quarter, month_number, month_name, year_month,
               week_number, weekday_number, weekday_name, is_weekend
        FROM vw_dim_date
        """,
    ),
    (
        "gold.dim_customer",
        "INSERT INTO gold.dim_customer SELECT * FROM vw_dim_customer",
    ),
    (
        "gold.dim_account",
        "INSERT INTO gold.dim_account SELECT * FROM vw_dim_account",
    ),
    (
        "gold.dim_control",
        """
        INSERT INTO gold.dim_control
        SELECT control_id, check_name, severity, severity_rank, control_family
        FROM vw_dim_control
        """,
    ),
    # The transaction fact adds the columns the dashboard and the model share: booking_hour,
    # is_night (before 06:00), is_cross_border (transaction country differs from the customer's) and
    # is_high_amount (above CHF 10,000). is_demo_row marks the rows injected by the demo scenario.
    (
        "gold.fact_transactions",
        """
        INSERT INTO gold.fact_transactions (
            transaction_id, account_id, date_key, event_timestamp, source_system,
            channel, merchant_category, transaction_country, status, amount_chf,
            risk_score, is_suspicious, is_demo_row,
            booking_hour, is_night, is_cross_border, is_high_amount
        )
        SELECT
            t.transaction_id,
            t.account_id,
            t.booking_date,
            t.event_timestamp,
            t.source_system,
            t.channel,
            t.merchant_category,
            t.country,
            t.status,
            t.amount_chf,
            t.risk_score,
            t.is_suspicious,
            (t.source_system = 'demo_scenario'),  -- Flag for demo scenario rows
            EXTRACT(HOUR FROM t.event_timestamp)::int,  -- booking_hour (0-23)
            EXTRACT(HOUR FROM t.event_timestamp) < 6,  -- is_night (midnight to 6 AM)
            (t.country <> c.country),  -- is_cross_border (different country than customer)
            (t.amount_chf > 10000)  -- is_high_amount (above CHF 10,000)
        FROM transactions t
        JOIN accounts  a ON a.account_id  = t.account_id
        JOIN customers c ON c.customer_id = a.customer_id
        """,
    ),
    (
        "gold.fact_quality_results",
        """
        INSERT INTO gold.fact_quality_results
        SELECT check_id, control_id, date_key, executed_at, status,
               failed_rows, is_pass, is_fail, is_warn
        FROM vw_fact_quality_results
        """,
    ),
)


def build(settings: Settings | None = None) -> dict[str, int]:
    """Rebuild every Gold table from Silver and return the row count of each.

    The SQL definitions are (re)applied first, so a fresh database needs no separate step. The
    truncate and the inserts share one connection and one commit: either Gold is fully rebuilt or
    it keeps its previous content.

    Raises ``AssertionError`` if the transaction fact is empty (a join lost every row) or if its
    row count differs from the Silver ``transactions`` table (a join lost some rows).
    """
    with RunJournal("gold_build", settings=settings) as run:
        execute_sql_file(ROOT / "sql" / "06_powerbi_star_schema.sql", settings=settings)

        execute_sql_file(GOLD_SQL, settings=settings)

        counts: dict[str, int] = {}
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                for table, _ in reversed(_LOADS):
                    cursor.execute(f"TRUNCATE TABLE {table} CASCADE")

                for table, statement in _LOADS:
                    cursor.execute(statement)
                    counts[table] = cursor.rowcount

            connection.commit()

        for table, count in counts.items():
            run.note(
                "gold_table_loaded",
                reason=f"{table} rebuilt from silver, truncate-then-insert keeps the step idempotent",
                row_count=count,
                table=table,
            )

        # A fact table left empty means the load matched nothing, usually a join that lost every
        # row. It must stop the pipeline.
        assert_invariant(
            counts.get("gold.fact_transactions", 0) > 0,
            invariant="gold.fact_transactions is not empty",
            reason=(
                "an empty fact table means the join to accounts or customers "
                "dropped every row; the dashboard would show a clean but false "
                "picture"
            ),
            rows=counts.get("gold.fact_transactions", 0),
        )

        silver = cast("int", fetch_rows("SELECT COUNT(*) AS n FROM transactions", settings=settings)[0]["n"])
        assert_invariant(
            int(silver) == counts["gold.fact_transactions"],
            invariant="gold.fact_transactions row count == silver transactions",
            reason=(
                "a gap means the join dropped rows; every downstream aggregate "
                "and every model score would then be computed on a subset "
                "nobody declared"
            ),
            silver=int(silver),
            gold=counts["gold.fact_transactions"],
        )

        return counts


def main() -> None:
    """Command-line entry point: rebuild Gold and print the row count of each table."""
    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    for table, count in build().items():
        print(f"{count:>8}  {table}")


if __name__ == "__main__":
    main()
