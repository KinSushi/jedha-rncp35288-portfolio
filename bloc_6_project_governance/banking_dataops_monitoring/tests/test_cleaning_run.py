"""The cleaning step as a whole (``clean_bronze_to_silver``): files read and written, invariants, journal.

``tests/test_cleaning.py`` proves the row-level rules. These tests run the step on real CSV files in a
temporary folder and read what it wrote, what it refused to write, and what it journalled.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from banking_dataops import audit, logging_config
from banking_dataops.pipeline import cleaning
from banking_dataops.pipeline.cleaning import (
    CleaningReport,
    _trim_object_columns,
    clean_bronze_to_silver,
    clean_transactions,
)

pytestmark = pytest.mark.usefixtures("no_audit_database")


def _transactions(rows: int, *, duplicates: int = 0, bad_dates: int = 0) -> pd.DataFrame:
    """``rows`` valid transactions, then ``duplicates`` repeated identifiers, then ``bad_dates`` impossible dates."""
    frame = pd.DataFrame(
        {
            "transaction_id": [f"T{i:04d}" for i in range(rows)],
            "amount_chf": [100.0 + i for i in range(rows)],
            "risk_score": [0.2] * rows,
            "status": ["posted"] * rows,
            "currency": ["CHF"] * rows,
            "country": ["CH"] * rows,
            "booking_date": ["2026-07-01"] * rows,
        }
    )
    repeated = frame.head(duplicates)
    broken = frame.tail(bad_dates).assign(
        transaction_id=[f"B{i:04d}" for i in range(bad_dates)], booking_date="1850-01-01"
    )
    return pd.concat([frame, repeated, broken], ignore_index=True)


def _bronze(directory: Path, transactions: pd.DataFrame, *, customers: bool = True, accounts: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    transactions.to_csv(directory / "synthetic_transactions.csv", index=False)
    if customers:
        pd.DataFrame({"customer_id": ["C1 ", " C2"], "country": ["CH", " FR "]}).to_csv(
            directory / "synthetic_customers.csv", index=False
        )
    if accounts:
        pd.DataFrame({"account_id": ["A1", "A2"], "customer_id": ["C1", "C2 "]}).to_csv(
            directory / "synthetic_accounts.csv", index=False
        )
    return directory


def _rows(**columns: list[object]) -> pd.DataFrame:
    size = len(next(iter(columns.values())))
    frame = pd.DataFrame({"transaction_id": [f"T{i}" for i in range(size)]})
    for name, values in columns.items():
        frame[name] = values
    return frame


# ---------------------------------------------------------------- the files written


def test_the_step_writes_the_clean_files_without_an_index_and_never_deletes_what_an_earlier_run_left(
    tmp_path: Path,
) -> None:
    # A clean batch: three clean files, no quarantine file; the folder is created with its parents.
    clean_silver = tmp_path / "a" / "b" / "silver"
    report = clean_bronze_to_silver(_bronze(tmp_path / "bronze", _transactions(20)), clean_silver)
    assert sorted(p.name for p in clean_silver.iterdir()) == [
        "accounts_clean.csv",
        "customers_clean.csv",
        "transactions_clean.csv",
    ]
    assert pd.read_csv(clean_silver / "transactions_clean.csv")["transaction_id"].tolist() == [
        f"T{i:04d}" for i in range(20)
    ]
    assert (report.rows_in, report.rows_out, report.rows_quarantined) == (20, 20, 0)
    # Customers and accounts are only trimmed; their blanks count in the report (three cells and one cell).
    customers = pd.read_csv(clean_silver / "customers_clean.csv")
    accounts = pd.read_csv(clean_silver / "accounts_clean.csv")
    assert customers.to_dict(orient="list") == {"customer_id": ["C1", "C2"], "country": ["CH", "FR"]}
    assert accounts.to_dict(orient="list") == {"account_id": ["A1", "A2"], "customer_id": ["C1", "C2"]}
    assert report.whitespace_trimmed == 4

    # Customers and accounts files that are missing are skipped, not invented.
    skipped = tmp_path / "skipped"
    clean_bronze_to_silver(
        _bronze(tmp_path / "bronze_only", _transactions(20), customers=False, accounts=False), skipped
    )
    assert sorted(p.name for p in skipped.iterdir()) == ["transactions_clean.csv"]

    # A quarantined row is written with its identifier and reason; the files carry no index column.
    silver = tmp_path / "silver"
    transactions = _transactions(40, duplicates=1, bad_dates=1)
    report = clean_bronze_to_silver(_bronze(tmp_path / "bronze_quarantine", transactions), silver)
    assert pd.read_csv(silver / "quarantine.csv").to_dict(orient="records") == [
        {"transaction_id": "T0000", "reason": "duplicate id"},
        {"transaction_id": "B0000", "reason": "booking_date outside [2000, 2100]"},
    ]
    assert (report.rows_in, report.rows_out, report.duplicates_removed) == (42, 40, 1)
    assert report.impossible_dates_quarantined == 1
    assert len(pd.read_csv(silver / "transactions_clean.csv")) == report.rows_out
    assert (silver / "transactions_clean.csv").read_text(encoding="utf-8").splitlines()[0] == ",".join(
        transactions.columns
    )
    assert (silver / "quarantine.csv").read_text(encoding="utf-8") == (
        'transaction_id,reason\nT0000,duplicate id\nB0000,"booking_date outside [2000, 2100]"\n'
    )
    assert (silver / "customers_clean.csv").read_text(encoding="utf-8") == "customer_id,country\nC1,CH\nC2,FR\n"
    assert (silver / "accounts_clean.csv").read_text(encoding="utf-8") == "account_id,customer_id\nA1,C1\nA2,C2\n"

    # Documented behaviour: a stale quarantine file stays for the analyst to see when nothing is quarantined now.
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "quarantine.csv").write_text("transaction_id,reason\nOLD,duplicate id\n", encoding="utf-8")
    clean_bronze_to_silver(_bronze(tmp_path / "bronze_stale", _transactions(20)), stale)
    assert (stale / "quarantine.csv").read_text(encoding="utf-8") == "transaction_id,reason\nOLD,duplicate id\n"

    # A batch of a single row is cleaned.
    one = clean_bronze_to_silver(_bronze(tmp_path / "bronze_one", _transactions(1)), tmp_path / "silver_one")
    assert (one.rows_in, one.rows_out) == (1, 1)


def test_the_step_is_journalled_with_both_invariants_the_cleaning_figures_and_the_settings_it_was_given(
    tmp_path: Path, audit_log: list[tuple[audit.AuditEvent, object]], settings
) -> None:
    bronze = _bronze(tmp_path / "bronze", _transactions(40, duplicates=1, bad_dates=1))

    clean_bronze_to_silver(bronze, tmp_path / "silver", settings)

    events = [event for event, _ in audit_log]
    assert events[0].reason == "step cleaning began"
    assert [(e.category, e.action, e.row_count) for e in events] == [
        ("pipeline", "run_started", None),
        ("integrity", "invariant_checked", None),
        ("integrity", "invariant_checked", None),
        ("data", "bronze_cleaned_to_silver", 40),
        ("pipeline", "run_succeeded", None),
    ]
    not_empty, rate, cleaned = events[1], events[2], events[3]
    assert not_empty.details["invariant"] == "cleaning leaves at least one row"
    assert (not_empty.details["rows_in"], not_empty.details["rows_out"]) == (42, 40)
    assert rate.details["invariant"] == "quarantine rate below 5%"
    assert (rate.details["rate"], rate.details["quarantined"]) == (0.0476, 2)
    assert cleaned.reason == (
        "types fixed, formats normalised, duplicates and impossible dates quarantined; "
        "business anomalies deliberately left for the control layer"
    )
    assert cleaned.details == {
        "rows_in": 42,
        "duplicates": 1,
        "impossible_dates": 1,
        "whitespace_trimmed": 4,
        "quarantined": 2,
    }
    assert all(given is settings for event, given in audit_log if event.category != "integrity")


# ---------------------------------------------------------------- what the step refuses


def test_the_step_refuses_a_missing_source_a_batch_that_vanishes_and_a_quarantine_rate_of_five_percent(
    tmp_path: Path,
) -> None:
    """FORBIDDEN PATH: cleaning nothing, or cleaning so hard that the batch a control must judge shrinks in silence."""
    # A missing Bronze file: the message carries the command that generates it, and nothing is written.
    empty_bronze, silver = tmp_path / "empty_bronze", tmp_path / "silver"
    empty_bronze.mkdir()
    with pytest.raises(RuntimeError) as missing:
        clean_bronze_to_silver(empty_bronze, silver)
    assert str(missing.value) == (
        f"No Bronze transactions at {empty_bronze / 'synthetic_transactions.csv'}. "
        "Generate the raw layer first: `python run_pipeline.py --skip-ml`."
    )
    assert list(silver.iterdir()) == []

    # No surviving row.
    vanished = _bronze(tmp_path / "vanished", _transactions(20).assign(booking_date="1850-01-01"))
    with pytest.raises(AssertionError) as nothing_left:
        clean_bronze_to_silver(vanished, tmp_path / "silver_none")
    assert str(nothing_left.value) == (
        "Invariant violated: cleaning leaves at least one row. an empty silver layer means the cleaning rules "
        "rejected the whole batch; every downstream control would then pass on nothing"
    )
    assert list((tmp_path / "silver_none").iterdir()) == []

    # The quarantine rate is a strict limit: four in a hundred passes, five stops.
    assert (
        clean_bronze_to_silver(
            _bronze(tmp_path / "four", _transactions(96, duplicates=4)), tmp_path / "silver_four"
        ).rows_quarantined
        == 4
    )
    with pytest.raises(AssertionError) as too_many:
        clean_bronze_to_silver(_bronze(tmp_path / "five", _transactions(95, duplicates=5)), tmp_path / "silver_five")
    assert str(too_many.value) == (
        "Invariant violated: quarantine rate below 5%. a high quarantine rate means the cleaning rules are "
        "miscalibrated; continuing would silently shrink the batch a control is meant to judge"
    )
    assert not (tmp_path / "silver_five" / "transactions_clean.csv").exists()


# ---------------------------------------------------------------- row rules, one input at a time


def test_row_rules_read_every_date_format_numbers_given_as_text_and_count_each_changed_cell_once() -> None:
    # The three date columns are parsed whatever the format of each cell (a first-cell guess would lose the others).
    mixed = ["2026-07-01", "July 2, 2026", "2026-07-03T10:00:00"]
    clean, report = clean_transactions(_rows(event_timestamp=mixed, booking_date=mixed, created_at=mixed))
    for column in ("event_timestamp", "booking_date", "created_at"):
        assert clean[column].tolist() == [
            pd.Timestamp("2026-07-01"),
            pd.Timestamp("2026-07-02"),
            pd.Timestamp("2026-07-03 10:00:00"),
        ]
    assert (report.dates_parsed, report.rows_out) == (9, 3)

    # Amounts and scores given as text are read as numbers; unreadable ones become missing.
    clean, _ = clean_transactions(_rows(amount_chf=["100.5", "abc", "7"], risk_score=["0.25", "", "x"]))
    assert clean["amount_chf"].tolist()[0] == 100.5
    assert clean["amount_chf"].isna().tolist() == [False, True, False]
    assert clean["risk_score"].isna().tolist() == [False, True, True]
    assert clean["risk_score"].tolist()[0] == 0.25

    # Currency and status: each changed cell is counted once.
    clean, report = clean_transactions(_rows(currency=["chf", "CHF", "Eur"], status=["posted", "PENDING", "Rejected"]))
    assert clean["currency"].tolist() == ["CHF", "CHF", "EUR"]
    assert clean["status"].tolist() == ["posted", "pending", "rejected"]
    assert (report.currency_normalised, report.status_normalised) == (2, 2)

    # A column that mixes numbers and text is read as text and trimmed.
    clean, report = clean_transactions(_rows(note=[1, "a "]))
    assert clean["note"].tolist() == ["1", "a"]
    assert report.whitespace_trimmed == 1

    # The first of two rows with one identifier is kept.
    clean, report = clean_transactions(_rows(amount_chf=[1.0, 2.0]).assign(transaction_id=["T", "T"]))
    assert clean["amount_chf"].tolist() == [1.0]
    assert report.quarantined_rows == [{"transaction_id": "T", "reason": "duplicate id"}]


@pytest.mark.parametrize(
    ("booking_dates", "kept_ids", "reasons"),
    [
        (["2026-07-01", "not a date", "2026-07-03"], ["T0", "T2"], ["T1"]),
        (["1999-12-31", "2026-07-01"], ["T1"], ["T0"]),
        (["2000-01-01", "2026-07-01"], ["T0", "T1"], []),
        (["2100-12-31", "2026-07-01"], ["T0", "T1"], []),
        (["2101-01-01", "2026-07-01"], ["T1"], ["T0"]),
    ],
)
def test_a_date_that_cannot_be_read_or_lies_outside_2000_2100_is_quarantined_with_its_reason_and_the_ends_are_kept(
    booking_dates: list[str], kept_ids: list[str], reasons: list[str]
) -> None:
    """FORBIDDEN PATH: one unreadable cell aborting the whole batch, or slipping into Silver as text."""
    frame = _rows(booking_date=booking_dates)
    before = frame.copy(deep=True)

    clean, report = clean_transactions(frame)

    assert clean["transaction_id"].tolist() == kept_ids
    assert report.impossible_dates_quarantined == len(reasons)
    assert report.quarantined_rows == [
        {"transaction_id": identifier, "reason": "booking_date outside [2000, 2100]"} for identifier in reasons
    ]
    assert frame.equals(before)  # the caller's frame is left unchanged


# ---------------------------------------------------------------- the report and the trimming helper


def test_the_report_counts_what_left_silver_and_the_trimming_helper_counts_the_cells_it_changed() -> None:
    assert CleaningReport(rows_in=10, rows_out=7).rows_quarantined == 3
    assert CleaningReport().rows_quarantined == 0
    first, second = CleaningReport(), CleaningReport()
    first.quarantined_rows.append({"transaction_id": "T1", "reason": "duplicate id"})
    assert second.quarantined_rows == []

    frame = pd.DataFrame({"name": ["a ", " b", "c"], "n": [1, 2, 3]})
    report = CleaningReport()
    trimmed = _trim_object_columns(frame, report)
    assert trimmed["name"].tolist() == ["a", "b", "c"]
    assert trimmed["n"].tolist() == [1, 2, 3]
    assert report.whitespace_trimmed == 2
    assert frame["name"].tolist() == ["a ", " b", "c"]

    untouched = pd.DataFrame({"name": ["a", "b"]})
    other = CleaningReport()
    assert _trim_object_columns(untouched, other).equals(untouched)
    assert other.whitespace_trimmed == 0

    anything = pd.DataFrame({"anything": [1, 2, 3]})
    cleaned, row_report = clean_transactions(anything)
    assert cleaned.equals(anything)
    assert (row_report.rows_in, row_report.rows_out, row_report.dates_parsed) == (3, 3, 0)


# ---------------------------------------------------------------- the command line


def test_main_cleans_the_data_folder_of_the_repository_and_prints_five_counters(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "0" * 16)

    def fake_clean(bronze: Path, silver: Path) -> CleaningReport:
        calls.append((bronze, silver))
        return CleaningReport(
            rows_in=100, rows_out=97, duplicates_removed=2, whitespace_trimmed=5, impossible_dates_quarantined=1
        )

    monkeypatch.setattr(cleaning, "clean_bronze_to_silver", fake_clean)

    cleaning.main()

    root = Path(cleaning.__file__).resolve().parents[3]
    assert calls == ["configure_logging", "new_run_id", (root / "data", root / "data" / "silver")]
    assert capsys.readouterr().out.splitlines() == [
        "  rows in          100",
        "  rows out         97",
        "  duplicates       2",
        "  impossible dates 1",
        "  whitespace       5",
    ]
