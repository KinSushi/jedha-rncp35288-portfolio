#!/usr/bin/env python3

"""Tests for the feature layer and the priority bands.

The most important test in this file is the target-leak guard. A model trained
on ``risk_score`` would score a perfect AUC and prove nothing, because the label
is thresholded from that very column. That failure mode is silent: the metrics
look excellent, which is exactly why it needs a test rather than a comment.

The bands are tested by their boundaries and by the property that failed in the
first version: whatever the threshold, a score of 1.0 must be reachable as ``high``.
Training and prediction have their own files (test_ml_training.py, test_ml_predict.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from banking_dataops.ml import features as F
from banking_dataops.ml.predict import BANDS, MEDIUM_BAND_FLOOR, priority_band

pytestmark = pytest.mark.limit(120)

# ---------------------------------------------------------------------------
# Test data factory
# ---------------------------------------------------------------------------


def _frame(rows: int = 40) -> pd.DataFrame:
    """Build a raw frame shaped like the SQL extract."""

    rng = np.random.default_rng(0)
    base = pd.Timestamp("2026-07-15 03:00:00")
    return pd.DataFrame(
        {
            "transaction_id": [f"TX-{i:05d}" for i in range(rows)],
            "amount_chf": rng.lognormal(4, 0.8, rows).round(2),
            "channel": rng.choice(["web", "api", "mobile", "branch"], rows),
            "merchant_category": rng.choice(["grocery", "travel", "electronics"], rows),
            "transaction_country": rng.choice(["CH", "FR", "DE"], rows),
            "source_system": rng.choice(["core_banking", "card_processor"], rows),
            "event_timestamp": [base + pd.Timedelta(hours=int(h)) for h in rng.integers(0, 48, rows)],
            "is_suspicious": rng.random(rows) > 0.9,
            "customer_segment": rng.choice(["retail", "premium"], rows),
            "customer_country": rng.choice(["CH", "FR"], rows),
        }
    )


# ---------------------------------------------------------------------------
# Target leak: the failure mode that looks like success
# ---------------------------------------------------------------------------


def test_no_forbidden_column_is_a_feature_the_derivations_follow_the_business_and_a_leak_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORBIDDEN PATHS: training on the column the label is derived from, a tree memorising rows by identifier.

    ``is_suspicious = risk_score > threshold``. Feeding ``risk_score`` would give a perfect model that has learnt
    nothing. Identifiers carry no signal but perfect separability: a model using them scores flawlessly in test and
    collapses on unseen data. The guard must raise, not warn: if someone adds ``risk_score`` to the feature list,
    the pipeline must stop rather than produce an impressive and meaningless model.
    """
    assert "risk_score" in F.FORBIDDEN_FEATURES
    assert "risk_score" not in F.feature_columns()
    assert F.TARGET in F.FORBIDDEN_FEATURES
    assert F.TARGET not in F.feature_columns()
    for column in ("transaction_id", "account_id", "customer_id"):
        assert column in F.FORBIDDEN_FEATURES
        assert column not in F.feature_columns()

    # Feature engineering. Night is midnight to 6 a.m.: a drift here silently changes both the KPI shown on screen
    # and the feature fed to the model. Cross-border compares the right two columns.
    built = F.build(_frame())
    for column in ("booking_hour", "is_night", "is_cross_border", "is_high_amount", "amount_log"):
        assert column in built.columns
    assert (built.loc[built["booking_hour"] < 6, "is_night"]).all()
    assert not (built.loc[built["booking_hour"] >= 6, "is_night"]).any()
    expected = built["transaction_country"] != built["customer_country"]
    assert (built["is_cross_border"] == expected).all()

    # A frame that was not built is refused, and so is a feature set that leaks.
    with pytest.raises(ValueError, match="Missing feature columns"):
        F.split_x_y(_frame())

    monkeypatch.setattr(F, "NUMERIC_FEATURES", ("amount_chf", "risk_score"))
    leaking = F.build(_frame())
    leaking["risk_score"] = 0.5
    with pytest.raises(ValueError, match="Target leak"):
        F.split_x_y(leaking)


# ---------------------------------------------------------------------------
# Scoring and triage
# ---------------------------------------------------------------------------


def test_the_bands_follow_the_threshold_every_band_is_reachable_and_an_invalid_score_or_threshold_is_refused() -> None:
    # ``high`` starts at the threshold, ``medium`` at 0.60 of it, both ends inclusive below.
    threshold = 0.80
    floor = threshold * MEDIUM_BAND_FLOOR
    assert priority_band(threshold, threshold) == "high"
    assert priority_band(0.95, threshold) == "high"
    assert priority_band(threshold - 1e-9, threshold) == "medium"
    assert priority_band(floor, threshold) == "medium"
    assert priority_band(floor - 1e-9, threshold) == "low"
    assert priority_band(0.0, threshold) == "low"

    # The same score changes band when the threshold is retuned: bands are relative.
    assert priority_band(0.40, 0.30) == "high"
    assert priority_band(0.40, 0.50) == "medium"
    assert priority_band(0.40, 0.90) == "low"

    # FORBIDDEN STATE: a band nobody can reach. Regression of the first version, where ``high`` began at 1.30 x the
    # threshold: with a threshold above 0.77 that boundary exceeded 1, so no probability could ever be ``high``. The
    # three bands must each be reachable for every legal threshold.
    for legal in [0.05, 0.3, 0.5, 0.833, 0.9, 0.99, 1.0]:
        reached = {
            priority_band(1.0, legal),
            priority_band(legal * MEDIUM_BAND_FLOOR, legal),
            priority_band(0.0, legal),
        }
        assert reached == set(BANDS)

    # FORBIDDEN PATH: a NaN compares false against every boundary and would be filed ``low``.
    for score in [float("nan"), -0.01, 1.01, float("inf"), "0.5", None]:
        with pytest.raises(ValueError, match="not a probability"):
            priority_band(score, 0.8)  # type: ignore[arg-type]

    # FORBIDDEN PATH: a corrupt artefact must stop the queue, not silently misclassify it.
    for corrupt in [0.0, -0.5, 1.01, float("nan"), "0.8", None]:
        with pytest.raises(ValueError, match="Threshold"):
            priority_band(0.5, corrupt)  # type: ignore[arg-type]
