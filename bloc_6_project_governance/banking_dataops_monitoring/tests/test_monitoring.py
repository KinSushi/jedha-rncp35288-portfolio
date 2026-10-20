#!/usr/bin/env python3
"""Test suite for monitoring and quality status summarization.

WHY THIS MODULE EXISTS
----------------------
The monitoring module provides a critical function: summarize_quality_status().
This function takes a list of check statuses and produces a summary that determines
whether the pipeline can proceed. If this function is broken, the entire decision
gate logic fails silently.

WHAT THIS MODULE TESTS
----------------------
1. That all PASS results produce a PASS summary
2. That any WARN result produces a WARN summary
3. That any FAIL result produces a FAIL summary, and that a summary cannot be edited once computed

WHY THESE TESTS MATTER
----------------------
- The summarize_quality_status() function is used by the decision gate to determine
  whether to BLOCK, SERVE, or REVIEW the pipeline output
- If the logic is inverted (e.g., FAIL becomes PASS), the pipeline could publish
  corrupted data without detection
- These tests ensure the priority logic is correct: FAIL > WARN > PASS

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only call a pure function, they don't modify anything
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

import dataclasses

import pytest

from banking_dataops.monitoring import summarize_quality_status


def test_a_summary_is_pass_when_all_pass_warn_with_a_warning_fail_with_a_failure_and_cannot_be_edited() -> None:
    """All PASS gives PASS, any WARN gives WARN, any FAIL gives FAIL, and the summary is frozen.

    A verdict that could be edited after it was computed (``failed_checks = 0``) would turn a FAIL into a PASS
    without the decision gate noticing.
    """

    summary = summarize_quality_status(["PASS", "PASS"])
    assert summary.total_checks == 2
    assert summary.passed_checks == 2
    assert summary.failed_checks == 0
    assert summary.warning_checks == 0
    assert summary.overall_status == "PASS"

    assert summarize_quality_status(["PASS", "WARN"]).overall_status == "WARN"

    failing = summarize_quality_status(["PASS", "FAIL"])
    assert failing.failed_checks == 1
    assert failing.overall_status == "FAIL"

    with pytest.raises(dataclasses.FrozenInstanceError):
        failing.failed_checks = 0  # type: ignore[misc]
    assert failing.overall_status == "FAIL"
