"""Gold layer build (``banking_dataops.pipeline.gold``): load order, one transaction, invariants, refusals.

The database is the recording fake of ``tests/fakes.py``. The two SQL files that define the views and the
Gold tables are replaced by a recorder: their contents are covered by tests/test_sql_files_exist.py and by the
smoke check on a live stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeDatabase

from banking_dataops import audit, logging_config
from banking_dataops.config import Settings
from banking_dataops.pipeline import gold
from banking_dataops.pipeline.gold import _LOADS

pytestmark = pytest.mark.usefixtures("no_audit_database")

TABLES_IN_LOAD_ORDER = [
    "gold.dim_date",
    "gold.dim_customer",
    "gold.dim_account",
    "gold.dim_control",
    "gold.fact_transactions",
    "gold.fact_quality_results",
]
ROW_COUNTS = {
    "gold.dim_date": 31,
    "gold.dim_customer": 60,
    "gold.dim_account": 120,
    "gold.dim_control": 7,
    "gold.fact_transactions": 800,
    "gold.fact_quality_results": 7,
}


@pytest.fixture
def sql_files(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Settings | None]]:
    """Record the SQL files the build applies instead of executing them."""
    applied: list[tuple[Path, Settings | None]] = []
    monkeypatch.setattr(gold, "execute_sql_file", lambda path, settings=None: applied.append((path, settings)))
    return applied


def _script(database: FakeDatabase, *, silver: int = 800, counts: dict[str, int] | None = None) -> None:
    for table, rows in (counts or ROW_COUNTS).items():
        database.answer(f"INSERT INTO {table}", rowcount=rows)
    database.answer("SELECT COUNT(*) AS n FROM transactions", rows=[(silver,)], columns=["n"])


def test_the_load_list_names_the_six_tables_dimensions_first() -> None:
    assert [table for table, _ in _LOADS] == TABLES_IN_LOAD_ORDER
    assert all(statement.lstrip().startswith(f"INSERT INTO {table}") for table, statement in _LOADS)


def test_the_transaction_fact_uses_the_thresholds_of_the_generator() -> None:
    """Night is before 06:00 and a high amount above CHF 10,000, the same values as the risk score rule."""
    statement = " ".join(dict(_LOADS)["gold.fact_transactions"].split())

    assert "EXTRACT(HOUR FROM t.event_timestamp) < 6" in statement
    assert "(t.amount_chf > 10000)" in statement
    assert "(t.country <> c.country)" in statement
    assert "(t.source_system = 'demo_scenario')" in statement


def test_build_applies_the_star_schema_then_the_gold_definitions_with_the_given_settings(
    fake_database: FakeDatabase, sql_files: list[tuple[Path, Settings | None]], settings: Settings
) -> None:
    _script(fake_database)

    gold.build(settings)

    assert sql_files == [
        (gold.ROOT / "sql" / "06_powerbi_star_schema.sql", settings),
        (gold.GOLD_SQL, settings),
    ]
    assert gold.GOLD_SQL == gold.ROOT / "sql" / "08_gold_layer.sql"
    assert all(path.is_file() for path, _ in sql_files)


def test_build_truncates_facts_first_then_reloads_dimensions_first_in_one_transaction(
    fake_database: FakeDatabase, sql_files: list[tuple[Path, Settings | None]], settings: Settings
) -> None:
    _script(fake_database)

    counts = gold.build(settings)

    truncated = [(s.kind, s.sql) for s in fake_database.statements[:6]]
    assert truncated == [("execute", f"TRUNCATE TABLE {table} CASCADE") for table in reversed(TABLES_IN_LOAD_ORDER)]
    inserted = [s.sql for s in fake_database.statements[6:12]]
    assert inserted == [statement for _, statement in _LOADS]
    assert fake_database.statements[12].sql == "SELECT COUNT(*) AS n FROM transactions"
    assert fake_database.statements[13:] == []
    # Truncate and inserts share one connection and one commit, which comes before the Silver count.
    assert fake_database.order == ["commit"]
    assert fake_database.commits == 1
    assert fake_database.dsns == [settings.dsn, settings.dsn]
    assert counts == ROW_COUNTS


def test_build_journals_each_table_then_checks_both_invariants(
    fake_database: FakeDatabase,
    sql_files: list[tuple[Path, Settings | None]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    _script(fake_database)

    gold.build(settings)

    events = [event for event, _ in audit_log]
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        *[("data", "gold_table_loaded", ROW_COUNTS[t]) for t in TABLES_IN_LOAD_ORDER],
        ("integrity", "invariant_checked", None),
        ("integrity", "invariant_checked", None),
        ("pipeline", "run_succeeded", None),
    ]
    loaded = events[1:7]
    assert [e.details["table"] for e in loaded] == TABLES_IN_LOAD_ORDER
    assert (
        loaded[4].reason == "gold.fact_transactions rebuilt from silver, truncate-then-insert keeps the step idempotent"
    )
    not_empty, same_count = events[7], events[8]
    assert not_empty.details["invariant"] == "gold.fact_transactions is not empty"
    assert not_empty.details["rows"] == 800
    assert same_count.details["invariant"] == "gold.fact_transactions row count == silver transactions"
    assert (same_count.details["silver"], same_count.details["gold"]) == (800, 800)
    assert all(given is settings for event, given in audit_log if event.category != "integrity")


def test_build_refuses_an_empty_transaction_fact(
    fake_database: FakeDatabase,
    sql_files: list[tuple[Path, Settings | None]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: a clean-looking dashboard over a join that dropped every row."""
    _script(fake_database, silver=800, counts={**ROW_COUNTS, "gold.fact_transactions": 0})

    with pytest.raises(AssertionError, match="Invariant violated: gold.fact_transactions is not empty"):
        gold.build(settings)

    actions = [event.action for event, _ in audit_log]
    assert actions[-2:] == ["invariant_violated", "run_failed"]
    assert "run_succeeded" not in actions
    assert fake_database.find("SELECT COUNT(*) AS n") == []


def test_build_refuses_a_fact_table_that_lost_some_rows(
    fake_database: FakeDatabase,
    sql_files: list[tuple[Path, Settings | None]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: every aggregate and every model score computed on a subset nobody declared."""
    _script(fake_database, silver=801)

    with pytest.raises(AssertionError, match="gold.fact_transactions row count == silver transactions"):
        gold.build(settings)

    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert (violated.details["silver"], violated.details["gold"]) == (801, 800)
    assert audit_log[-1][0].action == "run_failed"


def test_a_failing_sql_definition_stops_the_build_before_any_table_is_touched(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: truncating Gold when the definitions could not be applied; the tables stay unchanged."""

    def broken(path: Path, settings: Settings | None = None) -> None:
        raise RuntimeError(f"syntax error in {path.name}")

    monkeypatch.setattr(gold, "execute_sql_file", broken)

    with pytest.raises(RuntimeError, match="syntax error in 06_powerbi_star_schema.sql"):
        gold.build(settings)

    assert fake_database.statements == []
    assert fake_database.commits == 0
    assert audit_log[-1][0].reason == "step gold_build raised RuntimeError"


def test_main_starts_a_run_then_prints_the_row_count_right_aligned_before_each_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "0" * 16)
    monkeypatch.setattr(
        gold, "build", lambda: calls.append("build") or {"gold.dim_date": 31, "gold.fact_transactions": 800}
    )

    gold.main()

    assert calls == ["configure_logging", "new_run_id", "build"]
    assert capsys.readouterr().out == "      31  gold.dim_date\n     800  gold.fact_transactions\n"


def test_an_empty_fact_table_is_refused_with_the_rule_and_the_reason(
    fake_database: FakeDatabase, sql_files: list[tuple[Path, Settings | None]], settings: Settings
) -> None:
    _script(fake_database, counts={**ROW_COUNTS, "gold.fact_transactions": 0})

    with pytest.raises(AssertionError) as refused:
        gold.build(settings)

    assert str(refused.value) == (
        "Invariant violated: gold.fact_transactions is not empty. an empty fact table means the join to accounts "
        "or customers dropped every row; the dashboard would show a clean but false picture"
    )


def test_a_gap_between_silver_and_gold_is_refused_with_the_rule_and_the_reason(
    fake_database: FakeDatabase, sql_files: list[tuple[Path, Settings | None]], settings: Settings
) -> None:
    _script(fake_database, silver=900)

    with pytest.raises(AssertionError) as refused:
        gold.build(settings)

    assert str(refused.value) == (
        "Invariant violated: gold.fact_transactions row count == silver transactions. a gap means the join "
        "dropped rows; every downstream aggregate and every model score would then be computed on a subset "
        "nobody declared"
    )


def test_a_build_that_loaded_no_fact_table_at_all_is_refused_as_an_empty_fact(
    monkeypatch: pytest.MonkeyPatch,
    fake_database: FakeDatabase,
    sql_files: list[tuple[Path, Settings | None]],
    audit_log: list[tuple[audit.AuditEvent, object]],
    settings: Settings,
) -> None:
    """FORBIDDEN PATH: a Gold build whose load list lost the fact table passing on the strength of a default."""
    without_fact = tuple(load for load in _LOADS if load[0] != "gold.fact_transactions")
    monkeypatch.setattr(gold, "_LOADS", without_fact)
    _script(fake_database)

    with pytest.raises(AssertionError, match="Invariant violated: gold.fact_transactions is not empty"):
        gold.build(settings)

    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert violated.details["rows"] == 0


def test_a_fact_table_of_exactly_one_row_matching_silver_is_accepted(
    fake_database: FakeDatabase, sql_files: list[tuple[Path, Settings | None]], settings: Settings
) -> None:
    _script(fake_database, silver=1, counts={**ROW_COUNTS, "gold.fact_transactions": 1})

    assert gold.build(settings)["gold.fact_transactions"] == 1
