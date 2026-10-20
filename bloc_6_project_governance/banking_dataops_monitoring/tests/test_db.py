"""Database plumbing (``banking_dataops.db``): connections, SQL files in one transaction, reads, the reset.

The splitter itself is covered by tests/test_sql_splitter.py and tests/test_sql_splitter_edges.py. These tests
use the recording fake of ``tests/fakes.py`` to check what the other functions send to PostgreSQL, and that a
failure leaves the transaction uncommitted: for each function the accepted call and the refused one are read
side by side.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import psycopg
import pytest
from fakes import FakeDatabase

from banking_dataops import db
from banking_dataops.config import Settings


def _write_sql(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_connection_uses_the_dsn_of_the_given_settings_or_of_the_environment_and_an_unreachable_server_is_an_error(
    fake_database: FakeDatabase, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.connect(settings)
    assert fake_database.dsns == ["host=db.test port=6543 dbname=bank user=alice password=s3cret"]

    fake_database.forget()
    monkeypatch.setattr(db, "load_settings", lambda: settings)
    db.connect()
    assert fake_database.dsns == [settings.dsn]

    # FORBIDDEN PATH: a server that cannot be reached is an error for the caller, never a connection-less success.
    def unreachable(dsn: str, **_options: object) -> None:
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", unreachable)
    with pytest.raises(psycopg.OperationalError, match="connection refused"):
        db.connect(settings)


def test_a_sql_file_runs_its_statements_in_order_in_one_transaction_and_a_failure_or_a_missing_file_commits_nothing(
    tmp_path: Path, fake_database: FakeDatabase, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = _write_sql(
        tmp_path, "schema.sql", "CREATE TABLE a (x int);\n-- note; with a semicolon\nCREATE TABLE b (y int);\n"
    )
    db.execute_sql_file(schema, settings)
    assert fake_database.sql_sent() == ["CREATE TABLE a (x int)", "-- note; with a semicolon\nCREATE TABLE b (y int)"]
    assert fake_database.commits == 1
    assert fake_database.order == ["commit"]
    assert fake_database.dsns == [settings.dsn]

    # A file that holds only a comment sends that comment as its single statement; a blank one sends nothing.
    fake_database.forget()
    db.execute_sql_file(_write_sql(tmp_path, "comment.sql", "-- nothing but a comment\n"))
    assert fake_database.sql_sent() == ["-- nothing but a comment"]
    assert fake_database.commits == 1
    fake_database.forget()
    db.execute_sql_file(_write_sql(tmp_path, "blank.sql", "  \n\n"))
    assert fake_database.statements == []

    # The file is read as UTF-8, whatever the platform default.
    fake_database.forget()
    accents = _write_sql(tmp_path, "accents.sql", "-- Zürich\nSELECT 1;\n")
    read: list[str | None] = []
    original = Path.read_text

    def spy(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        read.append(encoding)
        return original(self, encoding=encoding, errors=errors)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", spy)
        db.execute_sql_file(accents)
    assert read == ["utf-8"]
    assert fake_database.sql_sent() == ["-- Zürich\nSELECT 1"]

    # FORBIDDEN PATH: half a schema. All or nothing: the statements after the failure are never sent.
    fake_database.forget()
    three = _write_sql(
        tmp_path, "three.sql", "CREATE TABLE a (x int);\nCREATE TABLE b (y int);\nCREATE TABLE c (z int);\n"
    )
    fake_database.fail_on("TABLE b", psycopg.errors.SyntaxError("boom"))
    with pytest.raises(psycopg.errors.SyntaxError, match="boom"):
        db.execute_sql_file(three)
    assert fake_database.sql_sent() == ["CREATE TABLE a (x int)", "CREATE TABLE b (y int)"]
    assert fake_database.commits == 0

    # A missing file is refused before any connection is opened.
    fake_database.forget()
    with pytest.raises(FileNotFoundError):
        db.execute_sql_file(tmp_path / "absent.sql")
    assert fake_database.dsns == []
    assert fake_database.commits == 0


def test_sql_files_apply_in_the_order_given_each_in_its_own_transaction_and_the_first_failing_one_stops_the_rest(
    tmp_path: Path, fake_database: FakeDatabase, settings: Settings
) -> None:
    first = _write_sql(tmp_path, "00.sql", "CREATE TABLE a (x int);")
    second = _write_sql(tmp_path, "01.sql", "INSERT INTO a VALUES (1);")

    db.execute_sql_files([second, first], settings)
    assert fake_database.sql_sent() == ["INSERT INTO a VALUES (1)", "CREATE TABLE a (x int)"]
    assert fake_database.commits == 2
    assert fake_database.dsns == [settings.dsn] * 2

    # FORBIDDEN PATH: carrying on after a failure, which would apply files that depend on the one that failed.
    fake_database.forget()
    one = _write_sql(tmp_path, "10.sql", "CREATE TABLE a (x int);")
    two = _write_sql(tmp_path, "11.sql", "CREATE TABLE b (y int);")
    three = _write_sql(tmp_path, "12.sql", "CREATE TABLE c (z int);")
    fake_database.fail_on("TABLE b", RuntimeError("second file fails"))
    with pytest.raises(RuntimeError, match="second file fails"):
        db.execute_sql_files([one, two, three])
    assert fake_database.sql_sent() == ["CREATE TABLE a (x int)", "CREATE TABLE b (y int)"]
    assert fake_database.commits == 1


def test_a_read_builds_a_frame_or_dictionaries_binds_its_parameters_commits_nothing_and_a_failing_read_is_raised(
    fake_database: FakeDatabase, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_database.answer("FROM t", rows=[(1, "a"), (2, "b")], columns=["id", "label"])
    frame = db.fetch_dataframe("SELECT id, label FROM t", settings=settings)
    assert list(frame.columns) == ["id", "label"]
    assert frame.to_dict(orient="records") == [{"id": 1, "label": "a"}, {"id": 2, "label": "b"}]
    assert fake_database.dsns == [settings.dsn]

    # No row still gives the column names; a statement without result set gives an empty frame.
    fake_database.answers.clear()
    fake_database.answer("FROM t", rows=[], columns=["id", "label"])
    empty = db.fetch_dataframe("SELECT id, label FROM t")
    assert empty.empty
    assert list(empty.columns) == ["id", "label"]
    nothing = db.fetch_dataframe("CREATE TABLE t (id int)")
    assert isinstance(nothing, pd.DataFrame)
    assert nothing.empty
    assert list(nothing.columns) == []

    # Dictionaries, with the parameters bound rather than written into the text.
    fake_database.forget()
    fake_database.answers.clear()
    fake_database.answer("FROM t", rows=[(1, "a")], columns=["id", "label"])
    rows = db.fetch_rows("SELECT id, label FROM t WHERE id = %s", settings=settings, params=(1,))
    assert rows == [{"id": 1, "label": "a"}]
    (statement,) = fake_database.statements
    assert (statement.sql, statement.params) == ("SELECT id, label FROM t WHERE id = %s", (1,))
    assert fake_database.dsns == [settings.dsn]

    fake_database.forget()
    monkeypatch.setattr(db, "load_settings", lambda: settings)
    assert db.fetch_rows("SELECT 1 AS ok") == []
    assert fake_database.statements[0].params is None
    assert fake_database.dsns == [settings.dsn]

    # FORBIDDEN PATH: a read that fails is raised, not turned into an empty answer that looks like "no data".
    fake_database.forget()
    fake_database.fail_on("FROM broken", RuntimeError("read failed"))
    with pytest.raises(RuntimeError, match="read failed"):
        db.fetch_rows("SELECT * FROM broken")
    with pytest.raises(RuntimeError, match="read failed"):
        db.fetch_dataframe("SELECT * FROM broken")
    assert fake_database.commits == 0  # a read never commits


def test_a_statement_is_committed_once_the_reset_is_one_cascading_truncate_and_a_failure_is_never_committed(
    fake_database: FakeDatabase, settings: Settings
) -> None:
    db.execute_query("DELETE FROM t", settings)
    assert fake_database.sql_sent() == ["DELETE FROM t"]
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn]

    fake_database.forget()
    db.reset_tables(settings)
    (sql,) = fake_database.sql_sent()
    assert " ".join(sql.split()) == (
        "TRUNCATE TABLE incident_reports, reconciliation_results, quality_check_results, transactions, "
        "accounts, customers RESTART IDENTITY CASCADE"
    )
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn]

    # FORBIDDEN PATH: a statement that failed (a lock, a missing table) must not be committed, reset included.
    fake_database.forget()
    fake_database.fail_on("DELETE", RuntimeError("locked"))
    fake_database.fail_on("TRUNCATE", RuntimeError("table is locked"))
    with pytest.raises(RuntimeError, match="locked"):
        db.execute_query("DELETE FROM t")
    with pytest.raises(RuntimeError, match="table is locked"):
        db.reset_tables()
    assert fake_database.commits == 0
