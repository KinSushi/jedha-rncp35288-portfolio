#!/usr/bin/env python3

"""Bronze → cleaning → features, chained on real generated data.

Each test crosses at least one module boundary. A test that could be written
against a single module belongs in the unit suite, not here.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from banking_dataops.generate_synthetic_data import SyntheticConfig, generate
from banking_dataops.ml import features as F
from banking_dataops.pipeline.cleaning import clean_transactions

pytestmark = pytest.mark.limit(120)

ANCHOR = datetime(2026, 8, 3, 6, 45, tzinfo=UTC)


@pytest.fixture(scope="module")
def bronze() -> Path:
    """Generate one Bronze batch, shared by every test in this module."""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        generate(
            SyntheticConfig(
                customers=60,
                accounts_per_customer=2,
                transactions=800,
                seed=42,
                generated_at=ANCHOR,
            ),
            path,
        )

        yield path


@pytest.fixture(scope="module")
def joined(bronze: Path) -> pd.DataFrame:
    """Reproduce the SQL extract shape, from files rather than from Postgres."""

    tx = pd.read_csv(bronze / "synthetic_transactions.csv")
    cu = pd.read_csv(bronze / "synthetic_customers.csv")
    ac = pd.read_csv(bronze / "synthetic_accounts.csv")
    return (
        tx.merge(ac[["account_id", "customer_id"]], on="account_id")
        .merge(
            cu[["customer_id", "customer_segment", "country"]].rename(columns={"country": "customer_country"}),
            on="customer_id",
        )
        .rename(columns={"country": "transaction_country"})
    )


# ---------------------------------------------------------------------------
# Bronze contract
# ---------------------------------------------------------------------------


def test_bronze_writes_both_a_stable_and_a_timestamped_copy(bronze: Path) -> None:
    """Raw files are archived with a timestamp; the pipeline needs a fixed name.

    Both must exist: the archive answers "which extract produced this verdict",
    the stable name gives downstream steps an entry point that does not move.
    """

    stable = sorted(p.name for p in bronze.glob("synthetic_*.csv"))
    archived = sorted(p.name for p in bronze.glob("transactions_2*.csv"))
    assert stable == ["synthetic_accounts.csv", "synthetic_customers.csv", "synthetic_transactions.csv"]
    assert archived, "no timestamped archive written"
    assert archived[0].startswith("transactions_20260803")


def test_stable_and_archived_files_hold_identical_data(bronze: Path) -> None:
    """An archive that differs from the file actually used proves nothing."""

    stable = pd.read_csv(bronze / "synthetic_transactions.csv")
    archived = pd.read_csv(next(bronze.glob("transactions_2*.csv")))
    pd.testing.assert_frame_equal(stable, archived)


# ---------------------------------------------------------------------------
# Cleaning boundary
# ---------------------------------------------------------------------------


def test_cleaning_preserves_the_whole_batch_on_clean_input(bronze: Path) -> None:
    """Generated data is well-formed; cleaning must not quarantine any of it.

    If this ever fails, the cleaning rules have drifted into rejecting valid
    business data: the failure mode that silently shrinks a batch.
    """

    raw = pd.read_csv(bronze / "synthetic_transactions.csv")
    clean, report = clean_transactions(raw)
    assert report.rows_out == report.rows_in
    assert report.rows_quarantined == 0
    assert len(clean) == len(raw)


def test_cleaning_output_still_feeds_the_feature_builder(joined: pd.DataFrame) -> None:
    """The contract that breaks first when a column is renamed.

    Cleaning and feature engineering are developed independently. This is the
    only test that fails when one of them changes a column the other expects.
    """

    clean, _ = clean_transactions(joined)
    built = F.build(clean)
    X, y = F.split_x_y(built)
    assert len(X) == len(clean)
    assert len(y) == len(clean)
    assert not X.isna().all().any(), "a feature column came out entirely empty"


# ---------------------------------------------------------------------------
# Feature boundary
# ---------------------------------------------------------------------------


def test_no_forbidden_column_survives_the_chain(joined: pd.DataFrame) -> None:
    """FORBIDDEN PATH: a leak reappearing after cleaning.

    The guard lives in `split_x_y`. This test proves it still holds at the end
    of the real chain, not only on a hand-built frame.
    """

    clean, _ = clean_transactions(joined)
    X, _ = F.split_x_y(F.build(clean))
    assert not F.FORBIDDEN_FEATURES.intersection(X.columns)
    assert "risk_score" not in X.columns


def test_derived_features_match_their_documented_definition(joined: pd.DataFrame) -> None:
    """One definition, one place.

    `is_night` and `is_cross_border` are computed in Gold for the dashboard and
    in features for the model. If the two ever diverge, a KPI on screen and a
    model input stop being the same thing.
    """

    built = F.build(joined)
    assert (built["is_night"] == (built["booking_hour"] < 6)).all()
    assert (built["is_cross_border"] == (built["transaction_country"] != built["customer_country"])).all()
    assert (built["is_high_amount"] == (built["amount_chf"] > 10_000)).all()


def test_target_remains_learnable_after_cleaning(joined: pd.DataFrame) -> None:
    """A degenerate class balance after cleaning would make every metric a lie.

    This is the integration-level counterpart of the invariant asserted inside
    `train()`: it catches the problem before a training run wastes a minute.
    """

    clean, _ = clean_transactions(joined)
    _, y = F.split_x_y(F.build(clean))
    rate = float(y.mean())
    assert 0.01 < rate < 0.5, f"positive rate {rate:.3f} is degenerate"


# ---------------------------------------------------------------------------
# End-to-end determinism
# ---------------------------------------------------------------------------


def test_the_whole_chain_is_deterministic() -> None:
    """Same seed, same anchor, same features: twice, in separate directories.

    Reproducibility is claimed by the dossier. This makes the claim mechanical
    across three modules rather than within one.
    """

    def run() -> pd.DataFrame:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            generate(
                SyntheticConfig(
                    customers=30,
                    accounts_per_customer=2,
                    transactions=200,
                    seed=7,
                    generated_at=ANCHOR,
                ),
                path,
            )

            tx = pd.read_csv(path / "synthetic_transactions.csv")
            cu = pd.read_csv(path / "synthetic_customers.csv")
            ac = pd.read_csv(path / "synthetic_accounts.csv")
            frame = (
                tx.merge(ac[["account_id", "customer_id"]], on="account_id")
                .merge(
                    cu[["customer_id", "customer_segment", "country"]].rename(columns={"country": "customer_country"}),
                    on="customer_id",
                )
                .rename(columns={"country": "transaction_country"})
            )

            clean, _ = clean_transactions(frame)
            X, _ = F.split_x_y(F.build(clean))
            return X.reset_index(drop=True)

    pd.testing.assert_frame_equal(run(), run())
