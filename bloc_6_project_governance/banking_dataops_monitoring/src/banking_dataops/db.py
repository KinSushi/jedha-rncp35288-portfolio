#!/usr/bin/env python3
"""PostgreSQL helpers: connect, run SQL files, fetch rows, reset the Silver tables.

A thin layer over ``psycopg`` so that the pipeline steps do not repeat connection handling, and
so that SQL files holding several statements run the same way everywhere.

CHOIX
-----
Retained:

- A function per need (``connect``, ``execute_sql_file``, ``fetch_dataframe``, ``fetch_rows``,
  ``execute_query``, ``reset_tables``) over ``psycopg`` v3, with a connection opened per call.
  ``psycopg.Connection`` is already a context manager, so no wrapper class is added.
- A hand-written statement splitter (``split_sql_statements``) that understands line comments,
  block comments, quoted strings with doubled quotes and dollar quoting. A SQL file is executed
  statement by statement, in one transaction.
- Every external value travels as a bound parameter (``fetch_rows(..., params=...)``), never
  through string formatting (docs/CODE_STANDARD.md, section 5).
- ``reset_tables`` empties the Silver tables with one ``TRUNCATE ... RESTART IDENTITY CASCADE``
  so that loading is "truncate then insert" and a run can be repeated.

Rejected:

- ``sql_text.split(";")``: it broke the schema as soon as a semicolon appeared in a comment or a
  string literal; the splitter and its tests replace it.
- ``pandas.read_sql_query`` on a raw psycopg connection: pandas only recognises SQLAlchemy and
  sqlite3 and emits a ``UserWarning`` on each call, which flooded the container logs during a
  demonstration. The frame is built from the cursor instead.
- A connection pool: one batch per run does not need it. Source : inconnue (no measurement of the
  cost of connecting per call).
- ``DELETE`` instead of ``TRUNCATE`` for the reset: ``TRUNCATE`` also resets the identity counters,
  so the first row after a reset gets the same identifier on every run.

OÙ
---
Role: data access layer. Callers: ``ingest`` (schema creation, reset), ``pipeline.gold`` and
``powerbi_views`` (SQL files), ``monitoring`` and ``reconciliation`` (read queries),
``quality_checks`` and ``demo_scenario`` (``connect``), ``audit``, ``api`` and ``ml.features``
(``fetch_rows``). The dashboard reaches the database through ``monitoring``. Depends on
``banking_dataops.config`` (the DSN), ``psycopg`` and ``pandas``. It talks to the database
described in ``sql/00_schema.sql``.

COMMENT
-------
``split_sql_statements`` scans the text one character at a time and keeps a small state:
inside a line comment, a block comment, a single-quoted string, or a dollar-quoted block.
It cuts only on a semicolon outside all four. ``execute_sql_file`` splits a file and runs the
statements in one transaction, committed at the end; if one fails, the transaction is rolled
back when the connection closes. ``fetch_rows`` returns dictionaries, ``fetch_dataframe`` a
pandas frame built from the cursor. ``reset_tables`` truncates, in one statement, the tables
that Silver loading refills.
Tests that prove it: tests/test_sql_splitter.py (every SQL file of ``sql/``), tests/test_sql_splitter_edges.py (the
cases that used to break), tests/test_sql_string_hygiene.py, tests/test_db.py (what is sent to the database, and
that a failure is never committed), tests/test_reverse_paths.py
(``test_a_run_identifier_is_sixteen_hexadecimal_characters_unique_per_run_and_anything_else_reaches_no_query``,
the validation that precedes a bound parameter). No test exercises ``execute_sql_file`` or ``reset_tables``
against a real database: they need PostgreSQL, and the continuous integration has none.

POURQUOI
--------
- One transaction per SQL file: schema creation is all or nothing, so a failure halfway does not
  leave tables without their indexes. That PostgreSQL DDL can be rolled back is stated from
  general knowledge and was not re-checked against the documentation: source : inconnue.
- The cascade in ``reset_tables`` follows the foreign keys of ``sql/00_schema.sql``: truncating
  ``customers`` or ``accounts`` also empties the tables that reference them.
- ``audit_events`` is not in the list: the journal is the durable source of what happened
  (ADR-0007) and has to outlive the data it describes. Gold tables are reloaded by their own
  ``TRUNCATE`` (``sql/08_gold_layer.sql``, ADR-0004 for where constraints live).
- Parameters bound rather than formatted: ADR-0007 and docs/CODE_STANDARD.md, section 5.

JOURNALISATION
--------------
No logging here: the functions are plumbing and raise on failure (a ``psycopg.Error``
carries the database message). The callers journal what the SQL did. Credentials come from the
settings and are not written to any message by this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from banking_dataops.config import Settings, load_settings


def connect(settings: Settings | None = None) -> psycopg.Connection:
    """Open a PostgreSQL connection. ``settings`` is injectable for tests; default: the environment."""
    return psycopg.connect((settings or load_settings()).dsn)


def split_sql_statements(sql_text: str) -> list[str]:
    """Split a SQL file into statements, cutting only on semicolons that end a statement.

    A semicolon inside a comment, a string literal or a dollar-quoted block (used by PostgreSQL
    function bodies) does not end anything, so each of those is tracked as a state while the
    text is scanned. Comments are kept inside the statement they belong to.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    in_single = False
    in_line = False
    in_block = False
    dollar_tag = None
    while i < len(sql_text):
        char = sql_text[i]
        pair = sql_text[i : i + 2]

        if in_line:
            current.append(char)
            if char == "\n":
                in_line = False
            i += 1
            continue

        if in_block:
            current.append(char)
            if pair == "*/":
                current.append(sql_text[i + 1])
                in_block = False
                i += 2
                continue

            i += 1
            continue

        if dollar_tag is not None:
            # The closing tag is copied whole: copying only its first character would leave the block unbalanced.
            close = dollar_tag + "$"
            if sql_text.startswith(close, i):
                current.append(close)
                i += len(close)
                dollar_tag = None
            else:
                current.append(char)
                i += 1
            continue

        # A dollar quote, a comment marker or a tag inside a string literal is text, not syntax.
        if pair == "$$" and not in_single:
            dollar_tag = "$"
            current.append("$$")
            i += 2
            continue

        # A tagged opening ($name$): the tag is alphanumeric or underscore and starts with a letter, and the
        # closing dollar sign of the opening tag must be there: "$abc def" is not a tag, so "$" stays an ordinary
        # character and "$1" stays a positional parameter.
        tag_start = i + 1 < len(sql_text) and (sql_text[i + 1].isalpha() or sql_text[i + 1] == "_")
        if char == "$" and not in_single and tag_start:
            j = i + 1
            while j < len(sql_text) and (sql_text[j].isalnum() or sql_text[j] == "_"):
                j += 1
            if j < len(sql_text) and sql_text[j] == "$":
                dollar_tag = sql_text[i:j]
                current.append(dollar_tag + "$")
                i = j + 1
                continue

        # No special case for a doubled quote inside a string: the first quote closes the string, the second one
        # reopens it, and between the two nothing else is looked at, so the state after the pair is the one before.
        if char == "'":
            in_single = not in_single
            current.append(char)
            i += 1
            continue

        if pair == "--" and not in_single:
            in_line = True
            current.append(pair)
            i += 2
            continue

        if pair == "/*" and not in_single:
            in_block = True
            current.append(pair)
            i += 2
            continue

        if char == ";" and not in_single and dollar_tag is None:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(char)
        i += 1

    # A last statement without a closing semicolon is still a statement.
    if current:
        stmt = "".join(current).strip()
        if stmt:
            statements.append(stmt)
    return statements


def execute_sql_file(path: Path, settings: Settings | None = None) -> None:
    """Execute every statement of a SQL file in one transaction, committed at the end.

    All or nothing: a failure leaves the database as it was, rather than half a schema.
    Raises ``FileNotFoundError`` if the file is missing and ``psycopg.Error`` if a statement fails.
    """
    statements = split_sql_statements(path.read_text(encoding="utf-8"))
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
        connection.commit()


def execute_sql_files(paths: Iterable[Path], settings: Settings | None = None) -> None:
    """Execute SQL files one after the other, in the order given.

    Sequential because the files depend on each other (``01_seed_reference_data.sql`` needs the
    tables of ``00_schema.sql``). The order is the caller's responsibility.
    """
    for path in paths:
        execute_sql_file(path, settings=settings)


def fetch_dataframe(query: str, settings: Settings | None = None) -> pd.DataFrame:
    """Run a read query and return the result as a pandas DataFrame.

    Built from the cursor and not with ``pandas.read_sql_query``: given a raw psycopg connection,
    pandas works but emits a ``UserWarning`` on every call, which floods the container logs
    during a demonstration. This also adds no dependency.
    """
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            if cursor.description is None:
                # A statement with no result set (DDL, for instance).
                return pd.DataFrame()
            columns = [column.name for column in cursor.description]
            return pd.DataFrame(cursor.fetchall(), columns=columns)


def fetch_rows(
    query: str,
    settings: Settings | None = None,
    params: tuple[object, ...] | dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Run a query and return a list of dictionaries, one per row.

    Dictionaries because ``row["customer_id"]`` is self-documenting and JSON-serialisable, which
    suits API responses and audit details. Every external value must go through ``params``: the
    query text is never built from user input.
    """
    with psycopg.connect((settings or load_settings()).dsn, row_factory=dict_row) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return list(cursor.fetchall())


def execute_query(query: str, settings: Settings | None = None) -> None:
    """Run a statement that returns no rows (INSERT, UPDATE, DELETE, TRUNCATE) and commit it.

    For several statements in one transaction use ``execute_sql_file``.
    """
    with connect(settings) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
        connection.commit()


def reset_tables(settings: Settings | None = None) -> None:
    """Empty the Silver tables that a load refills, in one ``TRUNCATE`` statement.

    ``RESTART IDENTITY`` makes identifiers start again from 1 on each run, and ``CASCADE``
    follows the foreign keys so no table has to be listed in dependency order by hand.
    Calling it twice is harmless: truncating an empty table does nothing. This is the first half of
    the "truncate then insert" pattern that makes the load idempotent.
    """
    query = """
    TRUNCATE TABLE
        incident_reports,
        reconciliation_results,
        quality_check_results,
        transactions,
        accounts,
        customers
    RESTART IDENTITY CASCADE
    """
    execute_query(query, settings=settings)
