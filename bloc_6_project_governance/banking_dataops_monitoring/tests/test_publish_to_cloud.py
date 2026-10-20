"""Tests for ``scripts/publish_to_cloud.py``: the copy of a batch to a managed PostgreSQL.

The two databases are replaced by recording fakes, so the tests check what the script would send: which statements, in
which order, to which side, and what it prints. The main safeguards are tested as refusals: no target configured, a
password that must never reach the screen, a target that does not match the source, a table that is missing.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import psycopg
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "publish_to_cloud.py"
TARGET_URL = "postgresql://cloud_user:cloud_secret@cloud.example.org:5432/banking?sslmode=require"


@pytest.fixture(scope="module")
def publish() -> ModuleType:
    """Import the script as a module (``scripts/`` is not a package); its ``sys.path`` tweak is undone afterwards."""
    saved = list(sys.path)
    spec = importlib.util.spec_from_file_location("publish_to_cloud", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path[:] = saved
    return module


class FakeCopy:
    """Either side of a ``COPY``: a context manager that yields blocks to read, or takes a payload to write."""

    def __init__(self, blocks: list[bytes], sink: list[tuple[str, str]], sql: str) -> None:
        self.blocks = blocks
        self.sink = sink
        self.sql = sql

    def __enter__(self) -> FakeCopy:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def __iter__(self) -> Iterator[bytes]:
        return iter(self.blocks)

    def write(self, payload: str) -> None:
        self.sink.append((self.sql, payload))


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.row: tuple[object, ...] | None = None

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str) -> None:
        statement = " ".join(sql.split())
        self.connection.statements.append(statement)
        counted = re.fullmatch(r"SELECT COUNT\(\*\) FROM (\S+)", statement)
        if counted:
            table = counted.group(1)
            if table not in self.connection.counts:
                raise psycopg.errors.UndefinedTable(f'relation "{table}" does not exist\nLINE 1: ...')
            self.row = (self.connection.counts[table],)
        elif statement == "SELECT version()":
            self.row = ("PostgreSQL 16.4, compiled by gcc",)
        elif statement.startswith("SELECT verdict, can_publish FROM vw_publication_verdict"):
            self.row = ("WARN", True)

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    def copy(self, sql: str) -> FakeCopy:
        return FakeCopy([], self.connection.copies_in, sql)


class FakeConnection:
    """Stands in for a psycopg connection and records everything it is asked."""

    def __init__(self, counts: dict[str, int], copy_out: dict[str, list[bytes]] | None = None) -> None:
        self.counts = counts
        self.copy_out = copy_out or {}
        self.statements: list[str] = []
        self.copies_in: list[tuple[str, str]] = []
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class SourceCursor(FakeCursor):
    """A source cursor: ``copy`` reads the rows of the table named in the statement."""

    def copy(self, sql: str) -> FakeCopy:
        table = re.fullmatch(r"COPY (\S+) TO STDOUT \(FORMAT CSV\)", sql)
        assert table is not None, sql
        return FakeCopy(self.connection.copy_out[table.group(1)], self.connection.copies_in, sql)


class FakeSource(FakeConnection):
    def cursor(self) -> SourceCursor:
        return SourceCursor(self)


def _tables(publish: ModuleType) -> list[str]:
    return list(publish.SILVER_TABLES) + list(publish.GOLD_TABLES)


@pytest.fixture
def run_main(publish: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Run ``main`` with fake databases; returns ``(exit_code, printed text, source, target)``."""
    monkeypatch.setattr(
        publish,
        "load_settings",
        lambda: SimpleNamespace(database_name="banking", database_host="db", database_port=5432, dsn="source-dsn"),
    )
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", TARGET_URL)

    def _run(
        *arguments: str, source: FakeSource, target: FakeConnection
    ) -> tuple[int, str, FakeSource, FakeConnection]:
        by_dsn = {"source-dsn": source, TARGET_URL: target}
        monkeypatch.setattr(publish.psycopg, "connect", lambda dsn, **kwargs: by_dsn[dsn])
        monkeypatch.setattr(sys, "argv", ["publish_to_cloud.py", *arguments])
        exit_code = publish.main()
        return exit_code, capsys.readouterr().out, source, target

    return _run


# ---------------------------------------------------------------------------
# Small functions
# ---------------------------------------------------------------------------


def test_mask_hides_the_password_of_a_url(publish: ModuleType) -> None:
    assert publish.mask("postgresql://user:secret@host:5432/db") == "postgresql://user:***@host:5432/db"


def test_mask_hides_the_password_of_a_keyword_dsn(publish: ModuleType) -> None:
    assert publish.mask("host=h user=u password=secret dbname=d") == "host=h user=u password=*** dbname=d"


def test_mask_leaves_a_dsn_without_password_unchanged(publish: ModuleType) -> None:
    assert publish.mask("postgresql://host/db") == "postgresql://host/db"


def test_target_dsn_refuses_a_missing_variable(publish: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """FORBIDDEN PATH: no target configured. The script stops, and the message gives an example for each provider."""
    monkeypatch.delenv("BANKING_DATAOPS_CLOUD_DSN", raising=False)
    with pytest.raises(SystemExit) as stop:
        publish.target_dsn()
    message = str(stop.value)
    assert message.startswith("BANKING_DATAOPS_CLOUD_DSN n'est pas défini.\n")
    assert "Exemple Neon     : postgresql://user:pwd@ep-xxx.eu-central-1.aws.neon.tech/banking_dataops" in message
    assert "Exemple Supabase : postgresql://postgres.xxxx:pwd@aws-0-eu-central-1.pooler.supabase.com:5432" in message


def test_target_dsn_refuses_a_blank_variable(publish: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """FORBIDDEN PATH: a variable holding only spaces is no target."""
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", "   ")
    with pytest.raises(SystemExit, match="n'est pas défini"):
        publish.target_dsn()


def test_target_dsn_adds_sslmode_to_a_url_without_query(
    publish: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", "postgresql://u:p@h/db")
    assert publish.target_dsn() == "postgresql://u:p@h/db?sslmode=require"
    assert capsys.readouterr().out == "  note : sslmode=require ajouté automatiquement\n"


def test_target_dsn_adds_sslmode_after_an_existing_query(publish: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", "postgresql://u:p@h/db?connect_timeout=5")
    assert publish.target_dsn() == "postgresql://u:p@h/db?connect_timeout=5&sslmode=require"


def test_target_dsn_keeps_an_explicit_sslmode_and_says_nothing(
    publish: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", "postgresql://u:p@h/db?sslmode=verify-full")
    assert publish.target_dsn() == "postgresql://u:p@h/db?sslmode=verify-full"
    assert capsys.readouterr().out == ""


def test_target_dsn_strips_the_surrounding_spaces(publish: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BANKING_DATAOPS_CLOUD_DSN", "  postgresql://u:p@h/db?sslmode=require \n")
    assert publish.target_dsn() == "postgresql://u:p@h/db?sslmode=require"


def test_apply_sql_file_runs_each_statement_in_order_then_commits_once(
    publish: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sql_file = tmp_path / "ddl.sql"
    sql_file.write_text("CREATE TABLE a (id INT);\nCREATE TABLE b (id INT);\n", encoding="utf-8")
    connection = FakeConnection({})

    publish.apply_sql_file(connection, sql_file, "ddl.sql")

    assert connection.statements == ["CREATE TABLE a (id INT)", "CREATE TABLE b (id INT)"]
    assert connection.commits == 1
    assert capsys.readouterr().out == "  ok   ddl.sql                      (2 instructions)\n"


def test_apply_sql_file_with_a_missing_file_leaves_the_target_unchanged(publish: ModuleType, tmp_path: Path) -> None:
    """FORBIDDEN PATH: a DDL file that is not there. Nothing is sent, nothing is committed."""
    connection = FakeConnection({})
    with pytest.raises(FileNotFoundError):
        publish.apply_sql_file(connection, tmp_path / "absent.sql", "absent.sql")
    assert (connection.statements, connection.commits) == ([], 0)


def test_copy_table_truncates_the_target_then_copies_the_csv_and_counts_the_lines(publish: ModuleType) -> None:
    source = FakeSource({}, copy_out={"customers": [b"c1,Ana\n", b"c2,Ben\nc3,Cy\n"]})
    target = FakeConnection({})

    copied = publish.copy_table(source, target, "customers")

    assert copied == 3
    assert target.statements == ["TRUNCATE customers CASCADE"]
    assert target.copies_in == [("COPY customers FROM STDIN (FORMAT CSV)", "c1,Ana\nc2,Ben\nc3,Cy\n")]
    assert target.commits == 1


def test_copy_table_leaves_the_source_unchanged(publish: ModuleType) -> None:
    """The source is only read: no statement, no commit on that side."""
    source = FakeSource({}, copy_out={"accounts": [b"a1\n"]})
    publish.copy_table(source, FakeConnection({}), "accounts")
    assert (source.statements, source.commits, source.rollbacks) == ([], 0, 0)


def test_count_rows_returns_an_integer(publish: ModuleType) -> None:
    assert publish.count_rows(FakeConnection({"customers": 100}), "customers") == 100


# ---------------------------------------------------------------------------
# main: three modes and their refusals
# ---------------------------------------------------------------------------


def _same_counts(publish: ModuleType, rows: int = 10) -> dict[str, int]:
    return {table: rows for table in _tables(publish)}


def test_verify_reports_identical_volumes_and_exits_zero(publish: ModuleType, run_main) -> None:
    exit_code, printed, _, target = run_main(
        "--verify", source=FakeSource(_same_counts(publish)), target=FakeConnection(_same_counts(publish))
    )
    assert exit_code == 0
    assert "  volumes identiques.\n" in printed
    assert "ÉCART DÉTECTÉ." not in printed
    assert target.statements[0] == "SELECT version()"
    assert all(statement.startswith("SELECT") for statement in target.statements)


def test_verify_exits_with_a_failure_when_one_table_differs(publish: ModuleType, run_main) -> None:
    """FORBIDDEN PATH: a target that does not hold what the source holds is reported, never accepted."""
    counts = _same_counts(publish)
    target_counts = {**counts, "transactions": 9}
    exit_code, printed, _, _ = run_main("--verify", source=FakeSource(counts), target=FakeConnection(target_counts))
    assert exit_code == 1
    assert f"  ERR  {'transactions':<30} source={10:<8} cible=9\n" in printed
    assert "  ÉCART DÉTECTÉ.\n" in printed


def test_verify_reports_a_missing_target_table_and_rolls_back(publish: ModuleType, run_main) -> None:
    """FORBIDDEN PATH: a table missing from the target is an error line and a rollback, and the run fails."""
    counts = _same_counts(publish)
    target_counts = {name: rows for name, rows in counts.items() if name != "gold.predictions"}
    exit_code, printed, _, target = run_main(
        "--verify", source=FakeSource(counts), target=FakeConnection(target_counts)
    )
    assert exit_code == 1
    assert f'  ERR  {"gold.predictions":<30} relation "gold.predictions" does not exist\n' in printed
    assert target.rollbacks == 1


def test_dry_run_lists_what_would_be_copied_and_writes_nothing(publish: ModuleType, run_main) -> None:
    exit_code, printed, _, target = run_main(
        "--dry-run", source=FakeSource(_same_counts(publish, 7)), target=FakeConnection({})
    )
    assert exit_code == 0
    assert "MODE DRY-RUN — aucune écriture\n" in printed
    assert f"  copierait  {'customers':<30} {7:>8} lignes\n" in printed
    assert target.statements == ["SELECT version()"]
    assert (target.copies_in, target.commits) == ([], 0)


def _copy_out(publish: ModuleType) -> dict[str, list[bytes]]:
    return {table: [b"row1\nrow2\n"] for table in _tables(publish)}


def test_publication_copies_every_table_in_the_documented_order_and_reports_the_verdict(
    publish: ModuleType, run_main
) -> None:
    counts = _same_counts(publish)
    source = FakeSource(counts, copy_out=_copy_out(publish))
    target = FakeConnection(counts)

    exit_code, printed, _, _ = run_main(source=source, target=target)

    assert exit_code == 0
    truncated = [s for s in target.statements if s.startswith("TRUNCATE ")]
    assert truncated == [f"TRUNCATE {table} CASCADE" for table in _tables(publish)]
    assert [sql for sql, _ in target.copies_in] == [
        f"COPY {table} FROM STDIN (FORMAT CSV)" for table in _tables(publish)
    ]
    first_view = next(i for i, s in enumerate(target.statements) if "CREATE OR REPLACE VIEW vw_dim_control AS" in s)
    last_truncate = max(i for i, s in enumerate(target.statements) if s.startswith("TRUNCATE "))
    assert last_truncate < first_view, "the star-schema views are recreated after the data is loaded"
    assert f"  PUBLIÉ — {2 * len(_tables(publish))} lignes, 8 vues, verdict WARN\n" in printed
    assert "  ok   verdict publié sur la cible : WARN (can_publish=True)\n" in printed


def test_publication_never_prints_the_password_of_the_target(publish: ModuleType, run_main) -> None:
    """The target is shown masked; the password it holds appears nowhere in the output."""
    counts = _same_counts(publish)
    _, printed, _, _ = run_main(source=FakeSource(counts, copy_out=_copy_out(publish)), target=FakeConnection(counts))
    assert "cloud_secret" not in printed
    assert "  cible  : postgresql://cloud_user:***@cloud.example.org:5432/banking?sslmode=require\n" in printed
    assert "  source : banking@db:5432\n" in printed


def test_publication_exits_with_a_failure_when_a_copied_table_does_not_match(publish: ModuleType, run_main) -> None:
    """FORBIDDEN PATH: after the copy, a target that disagrees with the source makes the whole publication fail."""
    counts = _same_counts(publish)
    target = FakeConnection({**counts, "customers": 3})
    exit_code, printed, _, _ = run_main(source=FakeSource(counts, copy_out=_copy_out(publish)), target=target)
    assert exit_code == 1
    assert f"  ERR  {'customers':<30} source=10 cible=3\n" in printed
    assert "  ÉCHEC — 1 table(s) en écart\n" in printed
    assert "PUBLIÉ" not in printed
