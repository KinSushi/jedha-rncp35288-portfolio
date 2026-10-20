"""A recording stand-in for PostgreSQL, shared by the tests that must see what the code sends.

WHY IT EXISTS
-------------
Most modules of ``banking_dataops`` talk to PostgreSQL through ``psycopg``. The suite runs offline (no test
may write to a database that happens to be running), so the tests replace ``psycopg.connect`` with
:class:`FakeDatabase`. It answers each statement from a small script and writes down everything the code
sent: the connection string, the statements with their parameters, the commits, the ``COPY`` payloads.

A test can then check both halves of a contract: what the code asked of the database (the exact SQL, the
bound parameters, the order) and what it did with the answer (the returned counts, the events journalled,
the exception raised).

HOW A SCRIPT WORKS
------------------
``database.answer("FROM accounts", rows=[("ACC-1",)])`` makes every statement whose text contains
``FROM accounts`` return that row. The first matching script wins; a statement that matches nothing returns
no row and a row count of 0, which is what an empty table would do.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg.rows import dict_row


@dataclass
class Answer:
    """What the fake database returns for a statement that contains ``fragment``."""

    fragment: str
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rowcount: int = 0
    #: When set, the n-th execution of a matching statement returns the n-th list of rows (the last one repeats).
    sequence: list[list[tuple[Any, ...]]] = field(default_factory=list)
    calls: int = 0


@dataclass(frozen=True)
class Statement:
    """One statement as the code sent it: ``execute`` or ``executemany``, its SQL and its parameters."""

    kind: str
    sql: str
    params: Any


class _Column:
    """Stands in for ``cursor.description[i]``: the code only reads ``.name``."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeCopy:
    """Stands in for ``cursor.copy(...)``: collects what is written to it."""

    def __init__(self, database: FakeDatabase, sql: str) -> None:
        self._database = database
        self._sql = sql
        self.written: list[str] = []

    def __enter__(self) -> FakeCopy:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self._database.copies.append((self._sql, "".join(self.written)))
        return False

    def write(self, data: str) -> None:
        self.written.append(data)


class FakeCursor:
    """The part of a psycopg cursor the project uses."""

    def __init__(self, database: FakeDatabase, dict_rows: bool) -> None:
        self._database = database
        self._dict_rows = dict_rows
        self._answer = Answer("")
        self._current: list[tuple[Any, ...]] = []
        self._position = 0
        self.rowcount = 0

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    @property
    def description(self) -> list[_Column] | None:
        return [_Column(name) for name in self._answer.columns] if self._answer.columns else None

    def execute(self, sql: str, params: Any = None) -> None:
        self._database.statements.append(Statement("execute", sql, params))
        self._select(sql)

    def executemany(self, sql: str, rows: Sequence[Any]) -> None:
        self._database.statements.append(Statement("executemany", sql, [tuple(row) for row in rows]))
        self._select(sql)

    def copy(self, sql: str) -> FakeCopy:
        for fragment, error in self._database.failures:
            if fragment in sql:
                raise error
        return FakeCopy(self._database, sql)

    def fetchone(self) -> Any:
        rows = self._rows()
        if self._position >= len(rows):
            return None
        row = rows[self._position]
        self._position += 1
        return row

    def fetchall(self) -> list[Any]:
        rows = self._rows()[self._position :]
        self._position = len(self._rows())
        return rows

    def _select(self, sql: str) -> None:
        self._position = 0
        for fragment, error in self._database.failures:
            if fragment in sql:
                raise error
        self._answer = next((a for a in self._database.answers if a.fragment in sql), Answer(""))
        if self._answer.sequence:
            self._current = self._answer.sequence[min(self._answer.calls, len(self._answer.sequence) - 1)]
            self._answer.calls += 1
        else:
            self._current = self._answer.rows
        self.rowcount = self._answer.rowcount

    def _rows(self) -> list[Any]:
        if self._dict_rows:
            return [dict(zip(self._answer.columns, row, strict=True)) for row in self._current]
        return list(self._current)


class FakeConnection:
    """The part of a psycopg connection the project uses: a context manager with cursors and a commit."""

    def __init__(self, database: FakeDatabase, dict_rows: bool) -> None:
        self._database = database
        self._dict_rows = dict_rows

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self._database, self._dict_rows)

    def commit(self) -> None:
        self._database.commits += 1
        self._database.order.append("commit")


class FakeDatabase:
    """Records every connection, statement, commit and ``COPY`` the code performs, and answers from a script."""

    def __init__(self) -> None:
        self.answers: list[Answer] = []
        self.failures: list[tuple[str, Exception]] = []
        self.statements: list[Statement] = []
        self.copies: list[tuple[str, str]] = []
        self.dsns: list[str] = []
        self.commits = 0
        self.order: list[str] = []

    def answer(
        self,
        fragment: str,
        rows: Sequence[tuple[Any, ...]] = (),
        columns: Sequence[str] = (),
        rowcount: int = 0,
    ) -> None:
        """Make every statement containing ``fragment`` return ``rows`` (and report ``rowcount``)."""
        self.answers.append(Answer(fragment, [tuple(r) for r in rows], list(columns), rowcount))

    def fail_on(self, fragment: str, error: Exception) -> None:
        """Make every statement containing ``fragment`` raise ``error`` (after being recorded)."""
        self.failures.append((fragment, error))

    def answer_each(self, fragment: str, results: Sequence[Sequence[tuple[Any, ...]]], rowcount: int = 0) -> None:
        """Successive executions of a statement containing ``fragment`` return ``results[0]``, ``results[1]``, ..."""
        sequence = [[tuple(r) for r in rows] for rows in results]
        self.answers.append(Answer(fragment, rowcount=rowcount, sequence=sequence))

    def connect(self, dsn: str = "", **kwargs: Any) -> FakeConnection:
        """Replacement for ``psycopg.connect``; ``row_factory`` given means rows come back as dictionaries."""
        self.dsns.append(dsn)
        return FakeConnection(self, dict_rows=kwargs.get("row_factory") is dict_row)

    def forget(self) -> None:
        """Empty the record and the scripted failures (not the scripted answers).

        For a test that checks several calls in turn and wants each one judged from a blank page.
        """
        self.statements.clear()
        self.copies.clear()
        self.dsns.clear()
        self.order.clear()
        self.failures.clear()
        self.commits = 0

    def sql_sent(self) -> list[str]:
        """The text of every statement, in the order it was sent."""
        return [s.sql for s in self.statements]

    def find(self, fragment: str) -> list[Statement]:
        """Every statement whose text contains ``fragment``."""
        return [s for s in self.statements if fragment in s.sql]
