#!/usr/bin/env python3
"""Load the generated CSV files into PostgreSQL: the Silver tables.

The Bronze layer is a set of CSV files; the controls, the reconciliation and the Gold layer are
SQL. This module is the bridge. Loading into constrained tables also makes the database reject
anything structurally invalid (ADR-0004).

CHOIX
-----
Retained:

- ``COPY ... FROM STDIN``: the client streams the file to the server, which works when the
  database runs in another container and cannot see the host's files (ADR-0005).
- Truncate then insert, not UPSERT. Silver is derived from Bronze and holds no state worth
  preserving; an UPSERT would leave orphan rows whenever a transaction disappears upstream.
  Running the step twice leaves the same tables.
- A fixed load order, customers then accounts then transactions, because the foreign keys refuse a
  transaction whose account does not exist yet.
- The schema and the audit journal are created here, in ``create_schema``, because ingestion emits
  the first audit event and the journal table must exist by then.
- One invariant after the load: at least one transaction reached Silver.

Rejected:

- Row-by-row ``INSERT``: slower and more code. Source : inconnue for the size of the gain on 1,500
  rows; it was not measured.
- Letting the load run on an empty batch: every control would pass on nothing and the dashboard
  would show a green verdict over no data.
- A single transaction for the three tables: not what the code does (see COMMENT). Source :
  inconnue for why the three loads were not grouped.

OÙ
---
Role: the Silver load, between the generator and the controls. Callers: ``run_pipeline.py``
(step ``silver``), ``python -m banking_dataops.ingest`` (Makefile targets ``ingest`` and ``reset``),
``docker/entrypoint.sh`` at start-up, and the alias ``src/ingestion/ingest.py``. Depends on
``banking_dataops.db`` (connection, SQL files, reset), ``banking_dataops.audit`` and
``banking_dataops.config`` (the ``data_dir`` setting). Reads ``synthetic_customers.csv``,
``synthetic_accounts.csv`` and ``synthetic_transactions.csv`` from that directory, writes the tables created by
``sql/00_schema.sql``.
Important: it reads the raw Bronze files. The files cleaned by ``pipeline.cleaning``
(``data/silver/*_clean.csv``) are not read here, so the cleaning step does not currently feed this load.

COMMENT
-------
``ingest_all`` opens a ``RunJournal``, creates the schema and the journal, empties the Silver tables
(``db.reset_tables``), loads the three files in order, journals each load, then checks the
invariant. ``_load_csv`` counts the data lines of the file, then opens its own connection, streams the
file with ``COPY`` and commits. Each table is therefore committed on its own: if the transactions
load fails, customers and accounts stay loaded until the next run truncates them.
No test runs the load against a real database; the continuous integration has no PostgreSQL.
Tests that prove it: tests/test_reverse_paths.py
(``test_every_state_changing_module_is_journalled_and_the_scan_refuses_a_module_that_leaves_no_trace``
checks that this module journals). The smoke check ``scripts/smoke_check.py`` inspects the loaded tables
on a running stack.

POURQUOI
--------
- Truncate then insert: ADR-0001 (replayable batch) and the reason recorded in the journal event
  ``tables_reset``.
- Constraints in the schema, business rules in the controls: ADR-0004.
- The empty-load invariant: the dashboard must not show a green verdict over an empty batch
  (the reason written in the ``assert_invariant`` call below).
- ``f"COPY {table_name} ..."`` builds SQL text from a variable. The three callers pass literal
  table names, so no external value reaches the statement; ``COPY`` cannot take the table name as a
  bound parameter.

JOURNALISATION
--------------
Under one ``RunJournal("ingest")``: the events ``tables_reset`` and ``table_loaded`` (one per table,
with the row count), the invariant ``at least one transaction reached silver`` (category
``integrity``), and the run start and end. They go to the structured log at INFO and, best effort, to
``audit_events``. A missing CSV raises ``FileNotFoundError``; a failed invariant raises
``AssertionError``. The module logger ``ingest`` is created but not used directly. ``main`` prints the
row counts.
"""

from __future__ import annotations

from pathlib import Path

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings, load_settings
from banking_dataops.db import connect, execute_sql_file, reset_tables
from banking_dataops.logging_config import get_logger

logger = get_logger("ingest")

ROOT = Path(__file__).resolve().parents[2]
SQL_DIR = ROOT / "sql"


def create_schema(settings: Settings | None = None) -> None:
    """Create the tables, then the audit journal.

    The journal is created here and not in a later step: the first event is emitted by the ingestion
    itself, and a journal created afterwards would leave the earliest, often most interesting,
    events unpersisted.
    """
    execute_sql_file(SQL_DIR / "00_schema.sql", settings=settings)
    execute_sql_file(SQL_DIR / "07_audit_journal.sql", settings=settings)


def _load_csv(table_name: str, csv_path: Path, settings: Settings | None = None) -> int:
    """Load one CSV file into a table with ``COPY`` and return the number of data rows.

    The rows are counted from the file before loading, because the audit event needs the count and
    the number of lines in the file is what the load is supposed to reproduce.

    ``COPY FROM STDIN`` rather than ``COPY FROM '<path>'``: the file is on the client, and the
    server may not be able to see it (a database in a container, the file on the host). The whole
    file is read into memory and written in one call, which is fine for these volumes.

    Raises ``FileNotFoundError`` if the file does not exist.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8") as f:
        row_count = sum(1 for _ in f) - 1  # minus the header line
    with connect(settings) as conn:
        with conn.cursor() as cur:
            with csv_path.open("r", encoding="utf-8") as f:
                with cur.copy(f"COPY {table_name} FROM STDIN WITH CSV HEADER") as _copy:
                    _copy.write(f.read())
            conn.commit()
    return row_count


def load_customers(data_dir: Path, settings: Settings | None = None) -> int:
    """Load ``synthetic_customers.csv`` into ``customers``."""
    return _load_csv("customers", data_dir / "synthetic_customers.csv", settings)


def load_accounts(data_dir: Path, settings: Settings | None = None) -> int:
    """Load ``synthetic_accounts.csv`` into ``accounts``."""
    return _load_csv("accounts", data_dir / "synthetic_accounts.csv", settings)


def load_transactions(data_dir: Path, settings: Settings | None = None) -> int:
    """Load ``synthetic_transactions.csv`` into ``transactions``."""
    return _load_csv("transactions", data_dir / "synthetic_transactions.csv", settings)


def ingest_all(settings: Settings | None = None) -> dict[str, int]:
    """Create the schema, empty the Silver tables and load the three datasets.

    It resets rather than upserts: Silver is derived from Bronze, so truncate then insert makes the
    step idempotent, where an UPSERT would keep rows whose transaction disappeared upstream. The
    order of the loads is fixed by the foreign keys.

    Warning: this wipes the Silver tables. Do not call it during a live demonstration.

    Raises ``AssertionError`` if no transaction was loaded (the Bronze files are missing or empty).
    """
    runtime = settings or load_settings()
    data_dir = runtime.data_dir
    with RunJournal("ingest", settings=runtime) as run:
        create_schema(runtime)
        reset_tables(runtime)
        run.note(
            "tables_reset",
            reason=(
                "silver is derived from bronze and holds no state of its own; "
                "truncate-then-insert is what makes the step idempotent"
            ),
        )

        # Customers before accounts before transactions: the foreign keys refuse the reverse.
        loaded = {
            "customers": load_customers(data_dir, runtime),
            "accounts": load_accounts(data_dir, runtime),
            "transactions": load_transactions(data_dir, runtime),
        }

        for table_name, row_count in loaded.items():
            run.note(
                "table_loaded",
                reason=f"{table_name} copied from bronze into silver, typed and constrained",
                row_count=row_count,
                table=table_name,
            )

        # An empty load means the Bronze files are missing or unreadable. Every downstream
        # control would then pass on nothing and report a clean batch.
        assert_invariant(
            loaded["transactions"] > 0,
            invariant="at least one transaction reached silver",
            reason=(
                "an empty silver layer makes every control pass on nothing; the "
                "dashboard would show a green verdict over an empty batch"
            ),
            loaded=loaded,
        )

        return loaded


def print_ingestion_summary(loaded: dict[str, int]) -> None:
    """Print one ``table: loaded=N`` line per table."""
    for table_name, row_count in loaded.items():
        print(f"{table_name}: loaded={row_count}")


def main() -> None:
    """Command-line entry point: run the whole ingestion and print the row counts."""
    print_ingestion_summary(ingest_all())


if __name__ == "__main__":
    main()
