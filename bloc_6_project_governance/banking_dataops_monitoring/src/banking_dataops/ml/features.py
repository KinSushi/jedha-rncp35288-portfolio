#!/usr/bin/env python3
"""Feature engineering for the suspicion classifier: one definition for training and scoring.

The same transformation must run when the model is trained and when it scores a batch.
Duplicating it in two files is how training/serving skew appears: a feature computed
slightly differently in production silently degrades a model that tested well.

CHOIX
-----
Retained:

- One module, two callers (``ml.train`` and ``ml.predict``), and a reading of the Silver
  tables through one SQL query, ``_EXTRACT_SQL``, ordered by ``transaction_id`` so that a
  given database always yields the same row order.
- The label is ``is_suspicious``. ``risk_score``, the value the generator thresholds to
  make the label, is forbidden as a feature by an explicit list, ``FORBIDDEN_FEATURES``,
  and an assertion in ``split_x_y`` that raises. The identifiers are forbidden too: they
  carry no signal and would let a tree memorise rows.
- The model sees the observable factors the generator's score is built from: hour of
  booking, night flag, cross-border flag, amount (raw, log and above 10,000 CHF), channel,
  merchant category, transaction country, source system, customer segment and the weekday
  and weekend flag. That is 13 columns: 3 numeric, 6 categorical, 4 boolean.
- Weekday kept categorical rather than numeric, so the model is not forced to read Monday
  to Sunday as an ordered scale.
- Demo rows (``source_system = 'demo_scenario'``) are excluded from the modelling frame.

Rejected:

- Letting ``risk_score`` through "because it helps": the model would reach a perfect score
  and prove nothing (a textbook target leak; ADR-0008, "Problème résolu en chemin").
- Reading the features from the Gold tables instead of recomputing them. Gold already
  computes ``is_night``, ``is_cross_border`` and ``is_high_amount`` in SQL for the dashboard,
  so the same rules now exist twice (here and in
  src/banking_dataops/pipeline/gold.py:145).
  Source : inconnue for why the model does not read Gold. No automated test compares the SQL
  and the Python definitions: tests/integration/test_bronze_to_features.py checks the Python
  side only.

OÙ
---
Role: the feature layer of the model step. Callers: ``banking_dataops.ml.train`` (history)
and ``banking_dataops.ml.predict`` (batch to score). Depends on ``banking_dataops.db``
(``fetch_rows``) and ``banking_dataops.logging_config``; reads the Silver tables
``transactions``, ``accounts`` and ``customers`` created by ``sql/00_schema.sql``.

COMMENT
-------
``extract`` runs ``_EXTRACT_SQL`` and returns a frame. ``build`` adds the derived columns
(hour, night flag, weekday, weekend flag, cross-border flag, log of the amount, high-amount
flag) on a copy. ``split_x_y`` selects ``feature_columns()``, refuses a forbidden column
among them and a missing column, and returns the features and the boolean label.
``weekday_name`` and ``is_weekend`` come from ``event_timestamp``, which comes from the date
anchor of the generated batch: that is why the anchor is fixed by default (ADR-0009).
Tests that prove it: tests/test_ml.py
(``test_no_forbidden_column_is_a_feature_the_derivations_follow_the_business_and_a_leak_is_refused``),
tests/test_features_contract.py (every derivation at its boundary), tests/integration/test_bronze_to_features.py.

POURQUOI
--------
- The leak guard is a raise, not a comment: ADR-0008, "Problème résolu en chemin", and the
  forbidden-path tests of tests/test_ml.py.
- Night is hours 0 to 5: the generator applies ``timestamp.hour < 6`` when it builds the risk
  score (src/banking_dataops/generate_synthetic_data.py:275),
  Gold and the anomaly queries use the same cut-off
  (sql/04_anomaly_queries.sql:71).
- 10,000 CHF for ``is_high_amount``: the generator's high-amount factor
  (src/banking_dataops/generate_synthetic_data.py:277) and
  Gold (src/banking_dataops/pipeline/gold.py:147). Origin of the value:
  a project choice, source : inconnue.
- ``np.log1p`` rather than ``np.log``: ``log(0)`` is undefined and a null amount is coerced to
  0 here. The raw amount is log-normal, so a linear model reads the log better.
- Demo rows are excluded because they are deliberate anomalies (the scenario injects a
  1,850,000 CHF transaction, src/banking_dataops/demo_scenario.py:123):
  training on them would teach the model that such an amount is normal.

JOURNALISATION
--------------
``extract`` and ``build`` each log one INFO record (``features_extracted`` and
``features_built``, category ``data``) with a reason and the row count. Errors are raised as
``ValueError`` with the repair in the message (a leaked feature, a missing column); nothing is
printed and nothing is swallowed.
"""

from __future__ import annotations

import pandas as pd

from banking_dataops.config import Settings
from banking_dataops.db import fetch_rows
from banking_dataops.logging_config import get_logger

logger = get_logger("ml.features")
#: Columns that must never reach the model.
#:
#: ``risk_score`` is the value the label is thresholded from — including it is
#: a target leak. ``is_suspicious`` is the label itself. The identifiers carry
#: no signal and would let a tree memorise rows.
FORBIDDEN_FEATURES: frozenset[str] = frozenset(
    {"risk_score", "is_suspicious", "transaction_id", "account_id", "customer_id"}
)
#: Numeric features, used as-is.
NUMERIC_FEATURES: tuple[str, ...] = (
    "amount_chf",
    "booking_hour",
    "amount_log",
)
#: Categorical features, one-hot encoded in the pipeline.
CATEGORICAL_FEATURES: tuple[str, ...] = (
    "channel",
    "merchant_category",
    "transaction_country",
    "source_system",
    "customer_segment",
    "weekday_name",
)
#: Boolean features, already binary.
BOOLEAN_FEATURES: tuple[str, ...] = (
    "is_night",
    "is_cross_border",
    "is_high_amount",
    "is_weekend",
)
TARGET = "is_suspicious"
_EXTRACT_SQL = """
SELECT
    t.transaction_id,
    t.amount_chf,
    t.channel,
    t.merchant_category,
    t.country            AS transaction_country,
    t.source_system,
    t.event_timestamp,
    t.is_suspicious,
    c.customer_segment,
    c.country            AS customer_country
FROM transactions t
JOIN accounts  a ON a.account_id  = t.account_id
JOIN customers c ON c.customer_id = a.customer_id
WHERE t.source_system <> 'demo_scenario'
ORDER BY t.transaction_id
"""


def extract(settings: Settings | None = None) -> pd.DataFrame:
    """Read the Silver layer and return the raw modelling frame.

    Demo rows are excluded: they are deliberate anomalies injected for a
    presentation, and training on them would teach the model that a CHF
    1 850 000 transaction is normal.
    """

    rows = fetch_rows(_EXTRACT_SQL, settings=settings)
    frame = pd.DataFrame(rows)
    logger.info(
        "features_extracted",
        extra={
            "category": "data",
            "reason": "silver layer read for modelling, demo rows excluded",
            "rows": len(frame),
        },
    )

    return frame


def build(frame: pd.DataFrame) -> pd.DataFrame:
    """Derive the modelling features from the raw frame.

    Every derivation is documented, because a feature nobody can explain is a
    feature nobody can defend under questioning.
    """

    import numpy as np

    out = frame.copy()
    timestamps = pd.to_datetime(out["event_timestamp"])
    # Hour of booking. Night activity is a standard AML risk factor: legitimate
    # retail traffic collapses between midnight and 6 a.m. while automated
    # abuse does not.
    out["booking_hour"] = timestamps.dt.hour
    out["is_night"] = out["booking_hour"] < 6
    # Weekday. Kept as a categorical rather than a number so the model is not
    # forced to treat Monday-to-Sunday as an ordered scale.
    out["weekday_name"] = timestamps.dt.day_name()
    out["is_weekend"] = timestamps.dt.dayofweek >= 5
    # Cross-border: booked outside the customer's own country.
    out["is_cross_border"] = out["transaction_country"] != out["customer_country"]
    # Amount band. The raw amount is log-normal, so a linear model reads it
    # badly; the log transform makes the relationship usable by both families.
    out["amount_chf"] = pd.to_numeric(out["amount_chf"], errors="coerce").fillna(0.0)
    out["amount_log"] = np.log1p(out["amount_chf"])
    out["is_high_amount"] = out["amount_chf"] > 10_000
    logger.info(
        "features_built",
        extra={
            "category": "data",
            "reason": "derived hour, weekday, cross-border and amount band from silver",
            "rows": len(out),
            "features": len(feature_columns()),
        },
    )

    return out


def feature_columns() -> list[str]:
    """Return the ordered list of columns the model consumes."""

    return [*NUMERIC_FEATURES, *CATEGORICAL_FEATURES, *BOOLEAN_FEATURES]


def split_x_y(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """Return features and label, with the leak guard enforced.

    The guard is not a comment, it is an assertion: a forbidden column reaching
    the feature matrix raises here rather than producing a model that looks
    excellent and means nothing.
    """

    columns = feature_columns()
    leaked = FORBIDDEN_FEATURES.intersection(columns)
    if leaked:
        raise ValueError(
            f"Target leak: {sorted(leaked)} must never be a feature. "
            "The label is thresholded from risk_score; including it would make "
            "the model perfect and worthless."
        )
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ValueError(f"Missing feature columns: {missing}. Run build() first.")
    return frame[columns], frame[TARGET].astype(bool)
