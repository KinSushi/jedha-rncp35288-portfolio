#!/usr/bin/env python3
"""Test suite for synthetic data generation.

WHY THIS MODULE EXISTS
----------------------
The generate_synthetic_data module creates test data for development and testing.
If this module is broken, the entire pipeline can't be tested. These tests verify
that the data generation works correctly and produces the expected output.

WHAT THIS MODULE TESTS
----------------------
1. test_generate_synthetic_data_files() - Verifies that the generate() function creates
   the expected CSV files with the correct structure and content

WHY THESE TESTS MATTER
----------------------
- File creation: If the files aren't created, the pipeline can't load data
- Column structure: If the columns are wrong, the pipeline fails at runtime
- Data validity: If the data is invalid (e.g., wrong status values), the pipeline produces incorrect results

SECURITY CONSIDERATIONS
-----------------------
- READ-ONLY: These tests only verify the output of generate(), they don't modify anything
- NO SIDE EFFECTS: Tests use tmp_path to avoid polluting the filesystem
- DETERMINISTIC: Same input always produces same output (with fixed seed)
"""

from pathlib import Path

import pandas as pd
import pytest

from banking_dataops.generate_synthetic_data import SyntheticConfig, generate


def test_generate_synthetic_data_files(tmp_path: Path) -> None:
    """Test that generate() creates the expected CSV files with correct structure."""

    generate(SyntheticConfig(customers=3, accounts_per_customer=2, transactions=25), tmp_path)
    customers = tmp_path / "synthetic_customers.csv"
    accounts = tmp_path / "synthetic_accounts.csv"
    transactions = tmp_path / "synthetic_transactions.csv"
    assert customers.exists()
    assert accounts.exists()
    assert transactions.exists()
    assert pd.read_csv(customers)["customer_id"].nunique() == 3
    assert pd.read_csv(accounts)["account_id"].nunique() == 6
    frame = pd.read_csv(transactions)
    assert frame["transaction_id"].nunique() == 25
    expected_columns = {
        "transaction_id",
        "account_id",
        "source_system",
        "event_timestamp",
        "booking_date",
        "amount_chf",
        "currency",
        "channel",
        "merchant_category",
        "country",
        "risk_score",
        "status",
        "is_suspicious",
        "created_at",
    }
    assert expected_columns.issubset(frame.columns)
    assert set(frame["status"]).issubset({"posted", "pending", "rejected"})

    # FORBIDDEN PATH: a batch with no transaction is refused, not written as a header-only file that cleaning would
    # take for a valid empty batch.
    refused = tmp_path / "refused"
    with pytest.raises(ValueError, match="No rows to write"):
        generate(SyntheticConfig(customers=3, accounts_per_customer=2, transactions=0), refused)
    assert not (refused / "synthetic_transactions.csv").exists()
