"""Bronze to Silver load (``banking_dataops.ingest``): what is sent to PostgreSQL, what is journalled, what is refused.

The database is a recording fake (``tests/fakes.py``): the tests read the exact ``COPY`` statements and
payloads. The two SQL files that create the schema and the ``TRUNCATE`` are replaced by recorders, because
their own contents are tested elsewhere (tests/test_sql_splitter.py, tests/test_db.py).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fakes import FakeDatabase

from banking_dataops import audit, ingest
from banking_dataops.config import Settings
from banking_dataops.ingest import _load_csv

pytestmark = pytest.mark.usefixtures("no_audit_database")

CUSTOMERS = "customer_id,name\nC1,Ada\nC2,Bob\n"
ACCOUNTS = "account_id,customer_id\nA1,C1\nA2,C1\nA3,C2\n"
TRANSACTIONS = "transaction_id,account_id\nT1,A1\nT2,A2\nT3,A3\nT4,A3\n"


def _write_bronze(directory: Path, *, transactions: str = TRANSACTIONS) -> None:
    (directory / "synthetic_customers.csv").write_text(CUSTOMERS, encoding="utf-8")
    (directory / "synthetic_accounts.csv").write_text(ACCOUNTS, encoding="utf-8")
    (directory / "synthetic_transactions.csv").write_text(transactions, encoding="utf-8")


@pytest.fixture
def silver_steps(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Record the schema files applied and the reset, in order, instead of executing them."""
    steps: list[tuple[str, object]] = []
    monkeypatch.setattr(
        ingest, "execute_sql_file", lambda path, settings=None: steps.append(("sql_file", (path.name, settings)))
    )
    monkeypatch.setattr(ingest, "reset_tables", lambda settings=None: steps.append(("reset", settings)))
    return steps


def test_load_csv_copies_the_whole_file_with_its_header_and_counts_the_data_rows(
    tmp_path: Path, fake_database: FakeDatabase, settings: Settings
) -> None:
    path = tmp_path / "synthetic_customers.csv"
    path.write_text(CUSTOMERS, encoding="utf-8")

    loaded = _load_csv("customers", path, settings)

    assert loaded == 2
    assert fake_database.copies == [("COPY customers FROM STDIN WITH CSV HEADER", CUSTOMERS)]
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn]


def test_load_csv_counts_zero_rows_for_a_file_that_holds_only_its_header(
    tmp_path: Path, fake_database: FakeDatabase
) -> None:
    path = tmp_path / "synthetic_accounts.csv"
    path.write_text("account_id,customer_id\n", encoding="utf-8")

    assert _load_csv("accounts", path) == 0
    assert fake_database.copies[0][1] == "account_id,customer_id\n"


def test_load_csv_of_a_missing_file_is_refused_before_any_connection_is_opened(
    tmp_path: Path, fake_database: FakeDatabase
) -> None:
    """FORBIDDEN PATH: a load that starts, finds nothing, and leaves a half-loaded table."""
    missing = tmp_path / "synthetic_customers.csv"

    with pytest.raises(FileNotFoundError, match="CSV file not found: .*synthetic_customers.csv"):
        _load_csv("customers", missing)

    assert fake_database.dsns == []
    assert fake_database.statements == []
    assert fake_database.commits == 0


@pytest.mark.parametrize(
    ("loader", "table", "file_name"),
    [
        (ingest.load_customers, "customers", "synthetic_customers.csv"),
        (ingest.load_accounts, "accounts", "synthetic_accounts.csv"),
        (ingest.load_transactions, "transactions", "synthetic_transactions.csv"),
    ],
)
def test_each_loader_reads_its_own_file_into_its_own_table(
    tmp_path: Path, fake_database: FakeDatabase, loader, table: str, file_name: str
) -> None:
    _write_bronze(tmp_path)
    expected = (tmp_path / file_name).read_text(encoding="utf-8")

    rows = loader(tmp_path)

    assert rows == expected.count("\n") - 1
    assert fake_database.copies == [(f"COPY {table} FROM STDIN WITH CSV HEADER", expected)]


def test_a_loader_does_not_fall_back_on_another_files_content(tmp_path: Path, fake_database: FakeDatabase) -> None:
    """FORBIDDEN PATH: loading transactions when only the customers file exists."""
    (tmp_path / "synthetic_customers.csv").write_text(CUSTOMERS, encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="synthetic_transactions.csv"):
        ingest.load_transactions(tmp_path)

    assert fake_database.copies == []


def test_create_schema_applies_the_tables_then_the_audit_journal(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The journal is created with the schema: the first event of the run must find its table."""
    applied: list[tuple[Path, Settings | None]] = []
    monkeypatch.setattr(ingest, "execute_sql_file", lambda path, settings=None: applied.append((path, settings)))

    ingest.create_schema(settings)

    assert applied == [
        (ingest.SQL_DIR / "00_schema.sql", settings),
        (ingest.SQL_DIR / "07_audit_journal.sql", settings),
    ]
    assert all(path.is_file() for path, _ in applied)


def test_ingest_all_resets_then_loads_customers_accounts_and_transactions_in_order(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    _write_bronze(tmp_path)
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    loaded = ingest.ingest_all(runtime)

    assert loaded == {"customers": 2, "accounts": 3, "transactions": 4}
    assert silver_steps == [
        ("sql_file", ("00_schema.sql", runtime)),
        ("sql_file", ("07_audit_journal.sql", runtime)),
        ("reset", runtime),
    ]
    assert [sql for sql, _ in fake_database.copies] == [
        "COPY customers FROM STDIN WITH CSV HEADER",
        "COPY accounts FROM STDIN WITH CSV HEADER",
        "COPY transactions FROM STDIN WITH CSV HEADER",
    ]
    assert fake_database.dsns == [runtime.dsn] * 3


def test_ingest_all_journals_each_step_with_its_reason_and_row_count(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    _write_bronze(tmp_path)
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    ingest.ingest_all(runtime)

    events = [event for event, _ in audit_log]
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("data", "tables_reset", None),
        ("data", "table_loaded", 2),
        ("data", "table_loaded", 3),
        ("data", "table_loaded", 4),
        ("integrity", "invariant_checked", None),
        ("pipeline", "run_succeeded", None),
    ]
    assert events[0].reason == "step ingest began"
    assert events[1].reason == (
        "silver is derived from bronze and holds no state of its own; "
        "truncate-then-insert is what makes the step idempotent"
    )
    assert [(e.reason, e.details["table"]) for e in events[2:5]] == [
        ("customers copied from bronze into silver, typed and constrained", "customers"),
        ("accounts copied from bronze into silver, typed and constrained", "accounts"),
        ("transactions copied from bronze into silver, typed and constrained", "transactions"),
    ]
    assert events[5].details["invariant"] == "at least one transaction reached silver"
    assert events[5].details["holds"] is True
    assert events[5].details["loaded"] == {"customers": 2, "accounts": 3, "transactions": 4}
    # ``assert_invariant`` takes no settings: its event is persisted with the environment's, not the run's.
    assert all(given is runtime for event, given in audit_log if event.category != "integrity")


def test_ingest_all_stops_when_no_transaction_reached_silver(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: a green verdict over an empty batch.

    A transactions file that holds only its header loads zero rows; the invariant refuses to go on, and the
    run is closed as failed, not as succeeded.
    """
    _write_bronze(tmp_path, transactions="transaction_id,account_id\n")
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    with pytest.raises(AssertionError, match="Invariant violated: at least one transaction reached silver"):
        ingest.ingest_all(runtime)

    actions = [event.action for event, _ in audit_log]
    assert "invariant_violated" in actions
    assert actions[-1] == "run_failed"
    assert "run_succeeded" not in actions


def test_ingest_all_with_a_missing_bronze_file_fails_the_run_and_reports_the_file(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    (tmp_path / "synthetic_customers.csv").write_text(CUSTOMERS, encoding="utf-8")
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    with pytest.raises(FileNotFoundError):
        ingest.ingest_all(runtime)

    last = audit_log[-1][0]
    assert last.action == "run_failed"
    assert last.reason == "step ingest raised FileNotFoundError"
    assert "synthetic_accounts.csv" in str(last.details["error"])


def test_ingest_all_reads_the_settings_of_the_environment_when_none_are_given(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    _write_bronze(tmp_path)
    environment = settings.model_copy(update={"data_dir": tmp_path})
    monkeypatch.setattr(ingest, "load_settings", lambda: environment)

    ingest.ingest_all()

    assert ("reset", environment) in silver_steps
    assert fake_database.dsns == [environment.dsn] * 3


def test_print_ingestion_summary_prints_one_line_per_table(capsys: pytest.CaptureFixture[str]) -> None:
    ingest.print_ingestion_summary({"customers": 2, "accounts": 3, "transactions": 4})

    assert capsys.readouterr().out == "customers: loaded=2\naccounts: loaded=3\ntransactions: loaded=4\n"


def test_main_runs_the_ingestion_and_prints_its_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(ingest, "ingest_all", lambda: {"customers": 1, "accounts": 1, "transactions": 1})

    ingest.main()

    assert capsys.readouterr().out == "customers: loaded=1\naccounts: loaded=1\ntransactions: loaded=1\n"


def test_the_csv_is_read_twice_in_text_mode_and_in_utf_8(
    tmp_path: Path, fake_database: FakeDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The platform default encoding is not UTF-8 everywhere (Windows uses a legacy code page): it is stated."""
    path = tmp_path / "synthetic_customers.csv"
    path.write_text(CUSTOMERS, encoding="utf-8")
    opened: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    original = Path.open

    def spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)

    _load_csv("customers", path)

    assert opened == [(("r",), {"encoding": "utf-8"})] * 2


def test_ingest_all_accepts_a_batch_of_exactly_one_transaction(
    tmp_path: Path,
    fake_database: FakeDatabase,
    silver_steps: list[tuple[str, object]],
    settings: Settings,
) -> None:
    _write_bronze(tmp_path, transactions="transaction_id,account_id\nT1,A1\n")
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    assert ingest.ingest_all(runtime)["transactions"] == 1


def test_an_empty_silver_layer_is_refused_with_the_rule_and_the_reason(
    tmp_path: Path, fake_database: FakeDatabase, silver_steps: list[tuple[str, object]], settings: Settings
) -> None:
    _write_bronze(tmp_path, transactions="transaction_id,account_id\n")
    runtime = settings.model_copy(update={"data_dir": tmp_path})

    with pytest.raises(AssertionError) as refused:
        ingest.ingest_all(runtime)

    assert str(refused.value) == (
        "Invariant violated: at least one transaction reached silver. an empty silver layer makes every control "
        "pass on nothing; the dashboard would show a green verdict over an empty batch"
    )
