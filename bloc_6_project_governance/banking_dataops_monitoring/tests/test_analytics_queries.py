#!/usr/bin/env python3
"""Tests for the analytical queries and the Gold builder.

WHY THESE EXIST
---------------
These modules were added without tests in an earlier pass. That is precisely
the failure mode `docs/CODE_STANDARD.md` forbids: code shipped under a standard
it does not meet, in a repository whose documentation claims every guarantee is
mechanically verified.

WHAT IS TESTED WITHOUT A DATABASE
---------------------------------
The queries cannot be executed here: no PostgreSQL. What *can* be verified,
and matters most, is that every column and table they reference actually exists
in the schema. A query naming a dropped column fails at runtime, in front of an
audience, and never at lint time.

The rules are written as check functions that raise ``AssertionError`` naming the rule that is broken. A check
that never fires proves nothing, so each one is also given a hostile example and must refuse it.

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify query structure, they don't execute queries
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

from __future__ import annotations

import inspect
import logging
import re
from pathlib import Path

import pandas as pd
import pytest

from banking_dataops import monitoring
from banking_dataops.logging_config import get_logger
from banking_dataops.monitoring import _traced
from banking_dataops.pipeline import gold
from banking_dataops.pipeline.gold import _LOADS

ROOT = Path(__file__).resolve().parents[1]
GOLD_SQL = (ROOT / "sql" / "08_gold_layer.sql").read_text(encoding="utf-8")
STAR_SQL = (ROOT / "sql" / "06_powerbi_star_schema.sql").read_text(encoding="utf-8")
#: The four analytical queries added for the advanced-KPI deliverable.
ANALYTICS = (
    "load_risk_heatmap",
    "load_volume_trend",
    "load_segment_cohorts",
    "load_control_trend",
)
#: What a page render must never contain: it would change the data the dashboard displays, on every refresh.
WRITING_STATEMENTS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER")


def _table_columns(sql: str, table: str) -> set[str]:
    """Extract declared column names for one CREATE TABLE block."""

    match = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\);", sql, re.S)
    assert match, f"table {table} not declared"
    return set(re.findall(r"^\s+(\w+)\s+[A-Z]", match.group(1), re.M))


def _check_query_source(name: str, source: str) -> None:
    """The rules every analytical query follows. Raises ``AssertionError`` naming the broken one.

    Gold only: derived columns such as ``booking_hour`` and ``is_night`` are computed once, in Gold, and a query
    that bypassed it would drift away from the model feature that shares the definition. Demo rows excluded: the
    demo scenario injects a CHF 1 850 000 transaction that would distort every average. Read-only. Traced: an
    empty panel has two causes, no data or a query that matched nothing after a schema change, and without a
    trace they are indistinguishable.
    """
    assert "gold." in source, f"{name} does not read the Gold layer"
    assert "FROM transactions" not in source, f"{name} bypasses Gold and reads Silver"
    if "gold.fact_transactions" in source:
        assert "is_demo_row" in source, f"{name} counts demo rows in its aggregates"
    for statement in WRITING_STATEMENTS:
        assert statement not in source.upper(), f"{name} contains a {statement} statement"
    assert "_traced(" in source, f"{name} is not traced"
    assert "reason=" in source, f"{name} is traced without a reason"


def _check_gold_build(loads: list[tuple[str, str]], build_source: str) -> None:
    """The rules of the Gold builder. Raises ``AssertionError`` naming the broken one."""
    tables = [table for table, _ in loads]
    dimensions = [i for i, table in enumerate(tables) if "dim_" in table]
    facts = [i for i, table in enumerate(tables) if "fact_" in table]
    assert dimensions, "no dimension declared"
    assert max(dimensions) < min(facts), "a fact table is loaded before a dimension"
    for table in tables:
        bare = table.replace("gold.", "")
        assert f"CREATE TABLE IF NOT EXISTS gold.{bare}" in GOLD_SQL, f"{table} is loaded but never declared"
    for _, statement in loads:
        for view in re.findall(r"FROM (vw_\w+)", statement):
            assert f"CREATE OR REPLACE VIEW {view}" in STAR_SQL, f"{view} is read but never created"
    assert "TRUNCATE" in build_source, "the Gold load does not empty its tables first"
    assert "assert_invariant" in build_source, "no completeness invariant on the Gold load"


# ---------------------------------------------------------------------------
# Schema conformance: the failure that only appears at runtime
# ---------------------------------------------------------------------------


def test_the_columns_the_queries_use_exist_in_gold_and_an_undeclared_table_is_refused() -> None:
    fact = _table_columns(GOLD_SQL, "gold.fact_transactions")
    account = _table_columns(GOLD_SQL, "gold.dim_account")
    quality = _table_columns(GOLD_SQL, "gold.fact_quality_results")

    for column in ("booking_hour", "channel", "is_suspicious", "is_demo_row"):  # the heatmap
        assert column in fact, f"{column} missing from gold.fact_transactions"
    for column in ("date_key", "amount_chf", "is_demo_row"):  # the volume trend
        assert column in fact, f"{column} missing from gold.fact_transactions"
    for column in ("account_id", "merchant_category"):  # the cohorts: the join between the fact and the account
        assert column in fact, f"{column} missing from gold.fact_transactions"
    assert {"account_id", "customer_segment"} <= account
    for column in ("date_key", "control_id", "is_pass", "is_fail"):  # the control trend
        assert column in quality, f"{column} missing from gold.fact_quality_results"
    assert "invented_column" not in fact

    # FORBIDDEN PATH: a check that cannot fail. A table that Gold does not declare is reported, not read as empty.
    with pytest.raises(AssertionError, match="gold.fact_no_such_table not declared"):
        _table_columns(GOLD_SQL, "gold.fact_no_such_table")


# ---------------------------------------------------------------------------
# Query safety
# ---------------------------------------------------------------------------


def test_every_analytics_query_follows_the_four_rules_and_the_check_refuses_a_query_that_breaks_one() -> None:
    for name in ANALYTICS:
        _check_query_source(name, inspect.getsource(getattr(monitoring, name)))
    # Before the first run the denominator is zero: a division error in a panel looks like missing data.
    assert "NULLIF" in inspect.getsource(monitoring.load_control_trend)

    # FORBIDDEN PATHS: each rule is shown to fire on a query that breaks it (and only it).
    traced = " _traced(x, reason='r') "
    hostile = [
        ("SELECT 1 FROM somewhere" + traced, "does not read the Gold layer"),
        ("SELECT 1 FROM gold.x JOIN transactions" + traced + "FROM transactions", "bypasses Gold and reads Silver"),
        ("SELECT 1 FROM gold.fact_transactions" + traced, "counts demo rows in its aggregates"),
        ("SELECT 1 FROM gold.x INSERT INTO gold.x" + traced, "contains a INSERT statement"),
        ("SELECT 1 FROM gold.x -- drop it" + traced, "contains a DROP statement"),
        ("SELECT 1 FROM gold.x", "is not traced"),
        ("SELECT 1 FROM gold.x _traced(x)", "is traced without a reason"),
    ]
    for source, broken_rule in hostile:
        with pytest.raises(AssertionError, match=re.escape(broken_rule)):
            _check_query_source("hostile_query", source)


def test_the_tracer_warns_on_an_empty_result_and_returns_the_frame_unchanged() -> None:
    """The tracer must escalate an empty frame to WARNING, not stay silent, and must not alter what it observes.

    WHY caplog is not used here: the project logger sets ``propagate = False`` on purpose: the root logger is
    not ours to pollute, and ``api.py`` calls ``configure_logging()`` at import. Together they make caplog blind
    to these records. Attaching a handler directly tests the real logger rather than a convenient stand-in.
    """
    captured: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    logger = get_logger("monitoring")
    handler = Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        empty = pd.DataFrame()
        assert _traced("test_query", empty, reason="unit test") is empty
        assert any("empty" in record.getMessage() for record in captured), (
            "an empty result did not raise a WARNING; a blank dashboard panel would "
            "then be indistinguishable from a query that silently matched nothing"
        )
        captured.clear()
        frame = pd.DataFrame({"a": [1, 2, 3]})
        assert _traced("test_query", frame, reason="unit test") is frame
        assert [record for record in captured if record.levelno >= logging.WARNING] == []
    finally:
        logger.removeHandler(handler)


# ---------------------------------------------------------------------------
# Gold builder
# ---------------------------------------------------------------------------


def test_the_gold_builder_follows_its_rules_and_the_check_refuses_a_broken_load() -> None:
    """Load order is a convention of the star schema: no foreign key enforces it (sql/08_gold_layer.sql declares
    none), so a test is the only thing that keeps it. A typo in a table name would TRUNCATE nothing and INSERT
    into nothing, silently producing an empty Gold layer. TRUNCATE-then-INSERT is what makes running the
    pipeline twice leave the same database.
    """
    build_source = inspect.getsource(gold.build)
    _check_gold_build(list(_LOADS), build_source)

    # FORBIDDEN PATHS: a fact row referencing a dimension key that does not exist yet, a table that is not declared,
    # a view that does not exist, accumulating rows on a second run, a load nobody counts.
    fact = ("gold.fact_transactions", "INSERT INTO gold.fact_transactions SELECT 1")
    dimension = ("gold.dim_account", "INSERT INTO gold.dim_account SELECT 1")
    cases = [
        ([fact, dimension], build_source, "a fact table is loaded before a dimension"),
        ([fact], build_source, "no dimension declared"),
        ([("gold.dim_acount", "SELECT 1"), fact], build_source, "gold.dim_acount is loaded but never declared"),
        (
            [("gold.dim_account", "SELECT 1 FROM vw_nothing"), fact],
            build_source,
            "vw_nothing is read but never created",
        ),
        ([dimension, fact], "assert_invariant()", "does not empty its tables first"),
        ([dimension, fact], "TRUNCATE gold.x", "no completeness invariant"),
    ]
    for loads, source, broken_rule in cases:
        with pytest.raises(AssertionError, match=re.escape(broken_rule)):
            _check_gold_build(loads, source)
