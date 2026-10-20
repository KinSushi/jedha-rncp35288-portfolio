#!/usr/bin/env python3
"""Test suite for SQL file existence and structure.

WHY THIS MODULE EXISTS
----------------------
The project relies on 9 SQL files to set up the database schema, seed data,
define quality checks, and create views. If any of these files are missing or empty,
the pipeline fails at runtime. These tests verify that all required files exist
and are not empty.

WHAT THIS MODULE TESTS
----------------------
1. That all 9 SQL files exist and are not empty
2. That all 8 Power BI views are declared
3. That each of these checks refuses a missing file, an empty file and a missing view

WHY THESE TESTS MATTER
----------------------
- File existence: If a SQL file is missing, the pipeline can't initialize the database
- View declarations: If a view is not declared, Power BI can't import the data
- These tests catch deployment errors early (e.g., files not committed to Git)

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only check file existence and content, they don't modify anything
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = [
    "00_schema.sql",
    "01_seed_reference_data.sql",
    "02_data_quality_checks.sql",
    "03_reconciliation_queries.sql",
    "04_anomaly_queries.sql",
    "05_performance_queries.sql",
    "06_powerbi_star_schema.sql",
    "07_audit_journal.sql",
    "08_gold_layer.sql",
]
EXPECTED_VIEWS = [
    "vw_dim_date",
    "vw_dim_customer",
    "vw_dim_account",
    "vw_dim_control",
    "vw_fact_transactions",
    "vw_fact_quality_results",
    "vw_fact_incidents",
    "vw_publication_verdict",
]


def _check_sql_file(path: Path) -> None:
    """Raise ``AssertionError`` if a SQL file is missing or empty."""
    assert path.exists(), f"Missing {path.name}"
    assert path.read_text(encoding="utf-8").strip(), f"Empty {path.name}"


def _check_views(sql: str) -> None:
    """Every view the Power BI model imports must exist in the SQL file."""
    for view in EXPECTED_VIEWS:
        assert f"CREATE OR REPLACE VIEW {view}" in sql, f"Missing view {view}"


def test_the_required_sql_files_exist_the_views_are_declared_and_the_checks_refuse_a_gap(tmp_path: Path) -> None:
    """All required SQL files exist and are not empty, every view is declared, and each check can fail.

    A check that cannot fail proves nothing: a missing file, an empty file and a view that was dropped from the
    star schema must each be refused.
    """

    for filename in REQUIRED:
        _check_sql_file(ROOT / "sql" / filename)
    star_schema = (ROOT / "sql" / "06_powerbi_star_schema.sql").read_text(encoding="utf-8")
    _check_views(star_schema)

    with pytest.raises(AssertionError, match="Missing absent.sql"):
        _check_sql_file(tmp_path / "absent.sql")
    (tmp_path / "empty.sql").write_text("  \n", encoding="utf-8")
    with pytest.raises(AssertionError, match="Empty empty.sql"):
        _check_sql_file(tmp_path / "empty.sql")
    with pytest.raises(AssertionError, match="Missing view vw_dim_date"):
        _check_views(star_schema.replace("CREATE OR REPLACE VIEW vw_dim_date", "-- view removed"))
