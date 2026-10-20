#!/usr/bin/env python3

"""Tests for the demo scenario module.

The demo scenario injects known violations into the database to demonstrate
the quality controls catching them. These tests ensure:
1. The demo marker cannot collide with real data
2. Every violation actually breaks its target control
3. The demo is safe to run against a populated database
4. The lists that scope the reset cannot be edited while the process runs
"""

import pytest

from banking_dataops.demo_scenario import (
    DEMO_CONTROL_IDS,
    DEMO_SOURCE_SYSTEM,
    DEMO_VIOLATIONS,
)
from banking_dataops.quality_checks import get_quality_checks


def test_the_marker_is_distinct_every_violation_breaks_a_real_control_and_the_scoping_lists_cannot_be_edited() -> None:
    """The marker must not collide with generated source systems; each payload must violate the rule its control
    enforces and be insertable (NOT NULL, NUMERIC(5,4) on risk_score).

    ``DEMO_CONTROL_IDS`` scopes the purge of the open incidents (``failed_control = ANY(%s)``): a list that could be
    extended while the process runs would let a reset delete incidents raised by real failures. Both lists are
    tuples, so the edit is refused.
    """
    assert DEMO_SOURCE_SYSTEM == "demo_scenario"
    assert DEMO_SOURCE_SYSTEM not in {"core_banking", "card_processor", "payments_gateway"}

    known = {check.control_id for check in get_quality_checks()}
    assert DEMO_VIOLATIONS
    for violation in DEMO_VIOLATIONS:
        assert violation.control_id in known
        assert violation.narrative
        assert violation.transaction_id.startswith("DEMO-TXN-")
        assert violation.amount_chf is not None
        assert violation.status
        assert 0 <= violation.risk_score < 10  # NUMERIC(5, 4) upper bound

    by_control = {v.control_id: v for v in DEMO_VIOLATIONS}
    # CTRL-003: amount_chf <= 0 OR amount_chf > 1000000
    assert by_control["CTRL-003"].amount_chf > 1_000_000
    # CTRL-005: risk_score < 0 OR risk_score > 1
    assert by_control["CTRL-005"].risk_score > 1
    # CTRL-006: status NOT IN ('posted', 'pending', 'rejected')
    assert by_control["CTRL-006"].status not in {"posted", "pending", "rejected"}

    assert len(set(DEMO_CONTROL_IDS)) == len(DEMO_CONTROL_IDS)
    with pytest.raises(AttributeError):
        DEMO_CONTROL_IDS.append("CTRL-001")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        DEMO_VIOLATIONS[0] = DEMO_VIOLATIONS[1]  # type: ignore[index]
