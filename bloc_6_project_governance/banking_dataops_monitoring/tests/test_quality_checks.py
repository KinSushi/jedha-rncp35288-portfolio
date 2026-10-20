#!/usr/bin/env python3
"""Test suite for quality checks configuration and status logic.

WHY THIS MODULE EXISTS
----------------------
The quality_checks module defines SQL-based data quality checks and a status
determination function. If these are misconfigured, the pipeline could pass
invalid data or fail on valid data. These tests verify that:
1. The status_from_failed_rows() function correctly maps failure counts to statuses
2. All quality checks are properly configured with unique IDs, names, severities, and valid SQL

WHAT THIS MODULE TESTS
----------------------
1. The status determination logic (failure count to PASS, WARN or FAIL)
2. That all checks are properly configured, and that a definition cannot be downgraded in place

WHY THESE TESTS MATTER
----------------------
- Status logic: If status_from_failed_rows() is inverted, the pipeline could publish
  corrupted data (0 failures → FAIL) or block valid data (1 failure → PASS)
- Check configuration: If checks have duplicate IDs, the audit trail becomes ambiguous.
  If checks have invalid SQL, they fail at runtime. If checks have invalid severities,
  the decision gate logic breaks.

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify configuration, they don't execute SQL
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

import dataclasses

import pytest

from banking_dataops.quality_checks import get_quality_checks, status_from_failed_rows


def test_the_status_follows_the_failed_rows_and_the_controls_are_well_formed_and_cannot_be_downgraded() -> None:
    """Failure counts map to statuses, every control is configured, and a definition cannot be edited in place.

    A control that could be downgraded at run time (``severity = "low"``, ``warn_only = True``) would let a
    batch through that the declared rules refuse; the definitions are frozen so that the edit is refused.
    """

    assert status_from_failed_rows(0) == "PASS"
    assert status_from_failed_rows(1) == "FAIL"
    assert status_from_failed_rows(1, warn_only=True) == "WARN"

    checks = get_quality_checks()
    assert len(checks) >= 7
    assert len({check.control_id for check in checks}) == len(checks)
    for check in checks:
        assert check.control_id.startswith("CTRL-")
        assert check.check_name
        assert check.severity in {"high", "medium", "low"}
        assert "SELECT" in check.query.upper()

    with pytest.raises(dataclasses.FrozenInstanceError):
        checks[0].severity = "low"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        checks[0].warn_only = True  # type: ignore[misc]
