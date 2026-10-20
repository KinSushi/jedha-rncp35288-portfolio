"""The feature layer (``banking_dataops.ml.features``): the extract, every derivation at its boundary, the leak guard.

``tests/test_ml.py`` shows that the forbidden columns are not features and that ``build`` produces the 13
columns. These tests pin every derived value at the edge where a mutated comparison would change it
(05:59 against 06:00, CHF 10 000 against 10 000.01, Friday against Saturday), the SQL of the extract, and
the exact refusals of the guard.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest
from fakes import FakeDatabase

from banking_dataops.config import Settings
from banking_dataops.ml import features as F

EXTRACT_SQL = (
    "SELECT t.transaction_id, t.amount_chf, t.channel, t.merchant_category, t.country AS transaction_country, "
    "t.source_system, t.event_timestamp, t.is_suspicious, c.customer_segment, c.country AS customer_country "
    "FROM transactions t JOIN accounts a ON a.account_id = t.account_id "
    "JOIN customers c ON c.customer_id = a.customer_id WHERE t.source_system <> 'demo_scenario' "
    "ORDER BY t.transaction_id"
)
EXTRACT_COLUMNS = [
    "transaction_id",
    "amount_chf",
    "channel",
    "merchant_category",
    "transaction_country",
    "source_system",
    "event_timestamp",
    "is_suspicious",
    "customer_segment",
    "customer_country",
]


def _raw(**columns: list[object]) -> pd.DataFrame:
    """A raw frame shaped like the extract; a column given by name replaces the default one."""
    size = len(next(iter(columns.values()))) if columns else 1
    base: dict[str, list[object]] = {
        "transaction_id": [f"T{i}" for i in range(size)],
        "amount_chf": [100.0] * size,
        "channel": ["api"] * size,
        "merchant_category": ["grocery"] * size,
        "transaction_country": ["CH"] * size,
        "source_system": ["core_banking"] * size,
        "event_timestamp": [pd.Timestamp("2026-07-27 12:00:00")] * size,
        "is_suspicious": [False] * size,
        "customer_segment": ["retail"] * size,
        "customer_country": ["CH"] * size,
    }
    base.update(columns)
    return pd.DataFrame(base)


# ---------------------------------------------------------------- the lists, the extract, the split and the guard


def test_the_lists_and_the_extract_are_exact_and_the_split_refuses_a_raw_frame_a_missing_label_and_a_leak(
    monkeypatch: pytest.MonkeyPatch, fake_database: FakeDatabase, log_records: list, settings: Settings
) -> None:
    assert F.feature_columns() == [
        "amount_chf",
        "booking_hour",
        "amount_log",
        "channel",
        "merchant_category",
        "transaction_country",
        "source_system",
        "customer_segment",
        "weekday_name",
        "is_night",
        "is_cross_border",
        "is_high_amount",
        "is_weekend",
    ]
    assert {"risk_score", "is_suspicious", "transaction_id", "account_id", "customer_id"} == F.FORBIDDEN_FEATURES
    assert F.TARGET == "is_suspicious"

    # The extract asks for the Silver join without demo rows and returns a frame; it logs the rows read.
    fake_database.answer(
        "FROM transactions t",
        rows=[("T1", 12.5, "api", "grocery", "CH", "core_banking", "2026-07-27", False, "retail", "CH")],
        columns=EXTRACT_COLUMNS,
    )
    frame = F.extract(settings)
    assert [" ".join(sql.split()) for sql in fake_database.sql_sent()] == [EXTRACT_SQL]
    assert fake_database.dsns == [settings.dsn]
    assert list(frame.columns) == EXTRACT_COLUMNS
    assert frame.loc[0, "transaction_id"] == "T1"

    fake_database.forget()
    fake_database.answers.clear()
    fake_database.answer("FROM transactions t", rows=[tuple(range(10))] * 3, columns=EXTRACT_COLUMNS)
    F.extract()
    assert [r.rows for r in log_records if r.getMessage() == "features_extracted"] == [1, 3]

    # An empty Silver layer is an empty frame, not an error.
    fake_database.forget()
    fake_database.answers.clear()
    log_records.clear()
    assert F.extract().empty
    record = next(r for r in log_records if r.getMessage() == "features_extracted")
    assert (record.levelname, record.category, record.rows) == ("INFO", "data", 0)
    assert record.reason == "silver layer read for modelling, demo rows excluded"

    # The split returns the thirteen features in order and the label as booleans.
    built = F.build(_raw(amount_chf=[1.0, 2.0], is_suspicious=[1, 0]))
    features, label = F.split_x_y(built)
    assert list(features.columns) == F.feature_columns()
    assert label.dtype == bool
    assert label.tolist() == [True, False]
    assert label.name == "is_suspicious"

    # FORBIDDEN PATH: scoring a raw frame as if it had been built.
    with pytest.raises(ValueError) as raised:
        F.split_x_y(_raw(amount_chf=[1.0]))
    assert str(raised.value) == (
        "Missing feature columns: ['booking_hour', 'amount_log', 'weekday_name', 'is_night', 'is_cross_border', "
        "'is_high_amount', 'is_weekend']. Run build() first."
    )
    with pytest.raises(KeyError, match="is_suspicious"):
        F.split_x_y(built.drop(columns=["is_suspicious"]))

    # FORBIDDEN PATH: ``risk_score`` entering the feature matrix and producing a perfect, worthless model.
    monkeypatch.setattr(F, "NUMERIC_FEATURES", ("amount_chf", "risk_score", "account_id"))
    leaking = F.build(_raw(amount_chf=[1.0]))
    with pytest.raises(ValueError) as leak:
        F.split_x_y(leaking)
    assert str(leak.value) == (
        "Target leak: ['account_id', 'risk_score'] must never be a feature. "
        "The label is thresholded from risk_score; including it would make the model perfect and worthless."
    )


# ---------------------------------------------------------------- build, boundary by boundary


def test_every_derivation_holds_at_its_boundary_and_a_missing_or_unreadable_timestamp_is_refused() -> None:
    # Night is the hours zero to five, and six o'clock is already day.
    for timestamp, hour, night in [
        ("2026-07-27 00:00:00", 0, True),
        ("2026-07-27 05:59:59", 5, True),
        ("2026-07-27 06:00:00", 6, False),
        ("2026-07-27 23:59:59", 23, False),
    ]:
        built = F.build(_raw(event_timestamp=[pd.Timestamp(timestamp)]))
        assert (built.loc[0, "booking_hour"], bool(built.loc[0, "is_night"])) == (hour, night)

    # The weekend is Saturday and Sunday, and the weekday keeps its English name.
    for day, name, weekend in [
        ("2026-07-24", "Friday", False),
        ("2026-07-25", "Saturday", True),
        ("2026-07-26", "Sunday", True),
        ("2026-07-27", "Monday", False),
    ]:
        built = F.build(_raw(event_timestamp=[pd.Timestamp(f"{day} 12:00:00")]))
        assert (built.loc[0, "weekday_name"], bool(built.loc[0, "is_weekend"])) == (name, weekend)

    # A timestamp given as text is parsed before the hour is taken.
    from_text = F.build(_raw(event_timestamp=["2026-07-27T03:30:00"]))
    assert (from_text.loc[0, "booking_hour"], bool(from_text.loc[0, "is_night"])) == (3, True)

    # A high amount is strictly above ten thousand francs.
    for amount, high in [(9_999.99, False), (10_000.0, False), (10_000.01, True), (1_850_000.0, True), (0.0, False)]:
        assert bool(F.build(_raw(amount_chf=[amount])).loc[0, "is_high_amount"]) is high

    # The log amount is log1p so that a zero amount is defined; an amount given as text is read as a number.
    assert F.build(_raw(amount_chf=[0.0, 100.0]))["amount_log"].tolist() == [0.0, pytest.approx(math.log1p(100.0))]
    text_amount = F.build(_raw(amount_chf=["250.5"]))
    assert text_amount.loc[0, "amount_chf"] == 250.5
    assert text_amount.loc[0, "amount_log"] == pytest.approx(np.log1p(250.5))

    # An amount that cannot be read becomes zero and does not stop the build.
    for unreadable in [None, "not a number", float("nan"), "12,5"]:
        built = F.build(_raw(amount_chf=[unreadable]))
        assert built.loc[0, "amount_chf"] == 0.0
        assert built.loc[0, "amount_log"] == 0.0
        assert bool(built.loc[0, "is_high_amount"]) is False

    # Cross-border means the transaction country differs from the customer's.
    for transaction_country, customer_country, cross_border in [
        ("CH", "CH", False),
        ("FR", "CH", True),
        ("CH", "FR", True),
        ("ch", "CH", True),
    ]:
        built = F.build(_raw(transaction_country=[transaction_country], customer_country=[customer_country]))
        assert bool(built.loc[0, "is_cross_border"]) is cross_border

    # FORBIDDEN PATH: features computed from a column that is not there, or from a timestamp that is not a date,
    # instead of inventing an hour.
    with pytest.raises(KeyError, match="event_timestamp"):
        F.build(_raw(amount_chf=[1.0]).drop(columns=["event_timestamp"]))
    with pytest.raises(ValueError, match="time data|Unknown datetime string|unconverted"):
        F.build(_raw(event_timestamp=["last Tuesday"]))


def test_build_keeps_the_raw_columns_adds_the_derived_ones_logs_them_and_leaves_the_raw_frame_unchanged(
    log_records: list,
) -> None:
    raw = _raw(amount_chf=["250.5", "1.0", "2.0"])
    before = raw.copy(deep=True)

    built = F.build(raw)

    assert built is not raw
    assert raw.equals(before)
    assert "booking_hour" not in raw.columns
    assert set(EXTRACT_COLUMNS) <= set(built.columns)
    assert set(F.feature_columns()) <= set(built.columns)
    assert built["transaction_id"].tolist() == ["T0", "T1", "T2"]

    record = next(r for r in log_records if r.getMessage() == "features_built")
    assert (record.levelname, record.category, record.rows, record.features) == ("INFO", "data", 3, 13)
    assert record.reason == "derived hour, weekday, cross-border and amount band from silver"
