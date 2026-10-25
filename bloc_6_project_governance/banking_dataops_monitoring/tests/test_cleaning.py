#!/usr/bin/env python3
"""Tests for the Bronze-to-Silver cleaning step.

WHY THIS MODULE EXISTS
----------------------
The cleaning step transforms raw Bronze data into clean Silver data. But cleaning
must be careful: it should repair *form* (whitespace, case, date formats) without
removing *business anomalies* (large amounts, invalid statuses, impossible dates).

WHAT THIS MODULE TESTS
----------------------
1. Form repairs: whitespace trimming, case normalization, date parsing
2. Quarantine logic: duplicates, impossible dates, invalid data
3. Business anomaly preservation: large amounts, invalid statuses, out-of-range scores

WHY THESE TESTS MATTER
----------------------
The decisive test is the last one. If cleaning removes a CHF 1 850 000 transaction,
CTRL-003 can't fail on it. The dashboard stays green while invalid data flows
downstream: the exact silent failure this project exists to prevent.

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify cleaning logic, they don't modify data
- NO SIDE EFFECTS: Tests are pure functions that verify state
- DETERMINISTIC: Same input always produces same output
"""

from __future__ import annotations

import pandas as pd

from banking_dataops.pipeline.cleaning import (
    KNOWN_STATUSES,
    MAX_YEAR,
    MIN_YEAR,
    CleaningReport,
    clean_transactions,
)


def _frame(**overrides) -> pd.DataFrame:
    """Create a DataFrame with default test data."""

    base = {
        "transaction_id": ["T1", "T2"],
        "amount_chf": [100.0, 200.0],
        "risk_score": [0.2, 0.3],
        "status": ["posted", "pending"],
        "currency": ["CHF", "CHF"],
        "country": ["CH", "FR"],
        "booking_date": ["2026-07-01", "2026-07-02"],
    }
    base.update(overrides)
    return pd.DataFrame(base)


# ---------------------------------------------------------------------------
# Form is repaired
# ---------------------------------------------------------------------------


def test_whitespace_is_trimmed() -> None:
    """`"CH "` and `"CH"` are two different countries to a GROUP BY.

    A single trailing space splits an aggregate in two, and nobody notices
    until the totals stop matching.
    """

    frame = _frame(country=["CH ", " FR"])
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert list(clean["country"]) == ["CH", "FR"]
    assert report.whitespace_trimmed >= 2
    assert frame.equals(before)  # the caller's frame is left unchanged


def test_currency_is_upper_cased() -> None:
    """Currency is upper-cased in the clean frame; the caller's frame is left unchanged."""

    frame = _frame(currency=["chf", "Chf"])
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert set(clean["currency"]) == {"CHF"}
    assert report.currency_normalised == 2
    assert frame.equals(before)


def test_status_is_lower_cased() -> None:
    """Status is lower-cased in the clean frame; the caller's frame is left unchanged."""

    frame = _frame(status=["Posted", "PENDING"])
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert set(clean["status"]) <= KNOWN_STATUSES
    assert report.status_normalised == 2
    assert frame.equals(before)


def test_dates_are_parsed() -> None:
    """Dates are parsed in the clean frame; the caller's text dates are left unchanged."""

    frame = _frame()
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert pd.api.types.is_datetime64_any_dtype(clean["booking_date"])
    assert report.dates_parsed == 2
    assert frame.equals(before)


# ---------------------------------------------------------------------------
# The unusable is quarantined, never dropped in silence
# ---------------------------------------------------------------------------


def test_duplicate_ids_are_quarantined_keeping_the_first() -> None:
    """Keeping the first is arbitrary but deterministic.

    Keeping both would break the load on the primary key; dropping both would
    lose a real transaction.
    """

    frame = _frame(
        transaction_id=["T1", "T1"],
        amount_chf=[100.0, 999.0],
    )
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert list(clean["transaction_id"]) == ["T1"]
    assert clean.iloc[0]["amount_chf"] == 100.0
    assert report.duplicates_removed == 1
    assert len(frame) == len(before) and frame.equals(before)  # nothing is removed from the caller's frame


def test_impossible_dates_are_quarantined() -> None:
    """An impossible date is quarantined out of the clean frame; the caller's frame is left unchanged."""

    frame = _frame(booking_date=["1899-01-01", "2026-07-02"])
    before = frame.copy(deep=True)
    clean, report = clean_transactions(frame)
    assert list(clean["transaction_id"]) == [frame["transaction_id"].iloc[1]]
    assert report.impossible_dates_quarantined == 1
    assert frame.equals(before)


def test_quarantined_rows_carry_a_reason() -> None:
    """A quarantined row with no reason is a row nobody can investigate."""

    _, report = clean_transactions(_frame(transaction_id=["T1", "T1"]))
    assert report.quarantined_rows
    for row in report.quarantined_rows:
        assert row["reason"]


def test_report_counts_are_consistent() -> None:
    """Test that report counts are consistent."""

    _, report = clean_transactions(_frame(transaction_id=["T1", "T1"]))
    assert report.rows_quarantined == report.rows_in - report.rows_out


# ---------------------------------------------------------------------------
# Business anomalies survive: the decisive property
# ---------------------------------------------------------------------------


def test_business_anomalies_reach_silver_untouched() -> None:
    """FORBIDDEN PATH: cleaning away what the controls are meant to catch.

    Each of these three rows breaks one control. If cleaning removed them, the
    dashboard would stay green while invalid data flowed downstream: the exact
    silent failure this project exists to prevent.
    """

    frame = pd.DataFrame(
        {
            "transaction_id": ["DEMO-TXN-0001", "DEMO-TXN-0002", "DEMO-TXN-0003"],
            "amount_chf": [1_850_000.0, 742.50, 318.90],  # CTRL-003
            "risk_score": [0.42, 1.42, 0.17],  # CTRL-005
            "status": ["posted", "posted", "settled"],  # CTRL-006
            "currency": ["CHF", "CHF", "CHF"],
            "country": ["CH", "CH", "CH"],
            "booking_date": ["2026-08-02"] * 3,
        }
    )
    clean, report = clean_transactions(frame)
    assert report.rows_out == 3, "cleaning removed a row a control must judge"
    assert (clean["amount_chf"] > 1_000_000).any()
    assert (clean["risk_score"] > 1).any()
    assert (~clean["status"].isin(KNOWN_STATUSES)).any()


def test_unknown_status_is_normalised_but_not_removed() -> None:
    """`settled` is not in the reference list, and that is CTRL-006's job.

    Cleaning lower-cases it; it must not delete it.
    """

    clean, _ = clean_transactions(_frame(status=["SETTLED", "posted"]))
    assert sorted(clean["status"]) == ["posted", "settled"]


def test_year_bounds_are_wide_enough_to_be_uncontroversial() -> None:
    """The window must catch parsing accidents, not business events."""

    assert MIN_YEAR <= 2000
    assert MAX_YEAR >= 2100


def test_empty_report_is_coherent() -> None:
    """Test that empty report is coherent."""

    report = CleaningReport()
    assert report.rows_quarantined == 0
