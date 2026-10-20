#!/usr/bin/env python3
"""Test suite for reconciliation queries and data structures.

WHY THIS MODULE EXISTS
----------------------
The reconciliation module defines SQL queries and data structures that are critical
for verifying data consistency. If these queries are malformed or the data structures
are incorrect, the reconciliation process fails silently.

WHAT THIS MODULE TESTS
----------------------
1. That all reconciliation queries are SELECT statements over the transactions table
2. That SourceSystemSummary has the correct structure and cannot be edited once built

WHY THESE TESTS MATTER
----------------------
- SQL queries: If a query doesn't contain SELECT or doesn't reference the transactions table,
  it's clearly wrong and would cause runtime errors
- Data structures: If SourceSystemSummary doesn't have the expected fields, the reconciliation
  process can't aggregate results correctly

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify structure, they don't execute queries
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

import dataclasses

import pytest

from banking_dataops.reconciliation import (
    CHANNEL_SUMMARY_QUERY,
    DAILY_RECONCILIATION_QUERY,
    SOURCE_SYSTEM_QUERY,
    STATUS_SUMMARY_QUERY,
    SUSPICIOUS_SUMMARY_QUERY,
    SourceSystemSummary,
)


def test_the_queries_are_selects_over_transactions_and_a_summary_has_its_fields_and_cannot_be_edited() -> None:
    """Every reconciliation query reads the transactions table, and a summary row is frozen once built.

    A reconciliation figure that could be edited after it was computed would no longer be what the database said.
    """

    for query in [
        SOURCE_SYSTEM_QUERY,
        DAILY_RECONCILIATION_QUERY,
        CHANNEL_SUMMARY_QUERY,
        STATUS_SUMMARY_QUERY,
        SUSPICIOUS_SUMMARY_QUERY,
    ]:
        assert "SELECT" in query.upper()
        assert "transactions" in query.lower()

    row = SourceSystemSummary("core_banking", 10, 1234)
    assert row.source_system == "core_banking"
    assert row.transaction_count == 10

    with pytest.raises(dataclasses.FrozenInstanceError):
        row.transaction_count = 11  # type: ignore[misc]
    assert row.transaction_count == 10
