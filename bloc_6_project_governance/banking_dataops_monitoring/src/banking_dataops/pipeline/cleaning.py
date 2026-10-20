#!/usr/bin/env python3
"""Bronze to Silver cleaning: repair what is unambiguous, quarantine what is not, touch nothing else.

Bronze is what arrived, Silver is what is accepted. Between the two there is a decision, and it must be
visible, testable and journalled instead of buried in a loading routine. An auditor asking "what did
you change between the source file and the table" must not be told "read the ingestion code".

CHOIX
-----
Retained:

- Three classes of treatment, and only three. Repaired: types, date parsing, whitespace, currency and
  status casing. Quarantined: duplicate identifiers and impossible dates, written to ``quarantine.csv``
  with a reason. Untouched: business anomalies, which belong to the control layer (ADR-0002, ADR-0004).
- Quarantine, never silent deletion: a dropped row is a row nobody will investigate.
- The "untouched" rule is the important one. A transaction of CHF 1,850,000 is not cleaned away; it
  must reach Silver so that CTRL-003 can fail on it and produce a traceable verdict.
- A report object (``CleaningReport``) with one counter per decision, so every change can be
  questioned.
- Two invariants stop the run: no output at all, and a quarantine rate of 5 % or more.
- Duplicates are resolved by keeping the first occurrence: arbitrary but deterministic. Keeping both
  would break the load, dropping both would lose a real transaction.

Rejected:

- Removing out-of-range amounts or unknown statuses here: it would hide from the controls the very
  anomalies they exist to detect.
- Continuing when the quarantine rate is high: above 5 % the rules are wrong, not the data, and the
  batch would silently shrink under a control meant to judge it.

OÙ
---
Role: the cleaning step between Bronze generation and the Silver load. Callers: ``run_pipeline.py``
(step ``cleaning``), ``python -m banking_dataops.pipeline.cleaning``, ``make clean-bronze`` and the alias
``src/cleaning/clean.py``. Depends on ``pandas``, ``banking_dataops.audit`` and
``banking_dataops.logging_config``. Reads ``data/synthetic_*.csv``; writes, in the ``data/silver`` directory,
``transactions_clean.csv``, ``customers_clean.csv``, ``accounts_clean.csv`` and ``quarantine.csv``.
Important: ``banking_dataops.ingest`` loads the raw Bronze files, not these cleaned files, so the output of
this step does not currently reach the database. The cleaning report and its journal event are produced
and checked, but the Silver tables hold the Bronze values.

COMMENT
-------
``clean_transactions`` works on a frame: trim text columns, parse the three date columns and the numbers
with ``errors="coerce"`` (an unparseable value becomes missing instead of crashing), upper-case the
currency and lower-case the status, quarantine duplicate identifiers (first kept), quarantine booking
dates that are missing or outside [2000, 2100]. ``clean_bronze_to_silver`` reads the file, calls it,
checks the two invariants, writes the files and journals one event. ``_trim_object_columns`` counts the
cells it changes. A limit: ``astype(str)`` turns a missing text value into the text "nan"; the generated
data has none.
Tests that prove it: tests/test_cleaning.py (``test_whitespace_is_trimmed``,
``test_duplicate_ids_are_quarantined_keeping_the_first``, ``test_impossible_dates_are_quarantined``,
``test_business_anomalies_reach_silver_untouched``, ``test_report_counts_are_consistent``),
tests/integration/test_bronze_to_features.py (``test_cleaning_preserves_the_whole_batch_on_clean_input``).

POURQUOI
--------
- Cleaning kept separate from loading and from the controls: ADR-0004 (the database guarantees the
  structure, the control layer handles the meaning) and ADR-0002.
- Year bounds 2000 to 2100: wide enough to be uncontroversial, so that only parsing accidents (such as
  "2023-13-45") fall outside; a test pins the width (``test_year_bounds_are_wide_enough_to_be_uncontroversial``).
  The exact bounds are a project choice, source : inconnue.
- The 5 % ceiling is a project choice, source : inconnue; the generated data quarantines nothing
  (evidence/pipeline_full.txt, event ``bronze_cleaned_to_silver``).
- ``select_dtypes(include="object")`` is deprecated in pandas 3 because it also selected string
  dtypes; naming the dtypes explicitly keeps the same selection on pandas 2 and 3 and removes the
  ``FutureWarning`` from the run output.

JOURNALISATION
--------------
One ``RunJournal("cleaning")``: the run start and end, the two invariant checks (category ``integrity``)
and the event ``bronze_cleaned_to_silver`` (category ``data``) with the row counts, duplicates, impossible
dates, whitespace repairs and the number quarantined. Events go to the structured log at INFO and, best
effort, to ``audit_events``. A missing Bronze file raises ``RuntimeError`` with the command that
regenerates it; a failed invariant raises ``AssertionError``. ``main`` prints a five-line summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings
from banking_dataops.logging_config import get_logger

logger = get_logger("pipeline.cleaning")
#: The controlled vocabulary of the transaction status. This module does not filter on it: it
#: only lower-cases the status. A genuinely unknown status passes through to Silver, where CTRL-006
#: fails on it. The constant is the shared definition the tests use.
KNOWN_STATUSES = frozenset({"posted", "pending", "rejected"})
#: Widest plausible booking window. A date outside it is a parsing accident, not a business event.
MIN_YEAR = 2000
MAX_YEAR = 2100


@dataclass
class CleaningReport:
    """What the cleaning step changed, counted. One counter per decision an auditor could question.

    Mutable on purpose: the helpers increment the counters as they work.
    """

    rows_in: int = 0
    rows_out: int = 0
    duplicates_removed: int = 0
    whitespace_trimmed: int = 0
    currency_normalised: int = 0
    status_normalised: int = 0
    dates_parsed: int = 0
    impossible_dates_quarantined: int = 0
    quarantined_rows: list[dict[str, object]] = field(default_factory=list)

    @property
    def rows_quarantined(self) -> int:
        """Rows removed from Silver and kept for investigation: ``rows_in - rows_out``."""
        return self.rows_in - self.rows_out


def _trim_object_columns(frame: pd.DataFrame, report: CleaningReport) -> pd.DataFrame:
    """Strip leading and trailing whitespace from every text column and count the cells changed.

    ``"CH "`` and ``"CH"`` are two different countries to a GROUP BY: a single trailing space splits an
    aggregate in two and nobody notices until the totals stop matching.
    """
    out = frame.copy()
    # Both dtypes are named: select_dtypes(include="object") is deprecated in pandas 3 because it
    # also selected string dtypes. This keeps the same selection on pandas 2 and 3.
    _text_columns = [
        column
        for column in out.columns
        if pd.api.types.is_object_dtype(out[column]) or pd.api.types.is_string_dtype(out[column])
    ]
    for column in _text_columns:
        before = out[column]
        after = before.astype(str).str.strip()
        report.whitespace_trimmed += int((before.astype(str) != after).sum())
        out[column] = after
    return out


def clean_transactions(frame: pd.DataFrame) -> tuple[pd.DataFrame, CleaningReport]:
    """Clean a raw transaction frame. Return the clean frame and the report.

    Order: trim, parse types, normalise casing, then quarantine. Parsing precedes the date check so
    that an unparseable date becomes missing and is caught as impossible. Every quarantined row keeps
    its identifier and a reason in ``report.quarantined_rows``.
    """
    report = CleaningReport(rows_in=len(frame))
    out = _trim_object_columns(frame, report)
    for column in ("event_timestamp", "booking_date", "created_at"):
        if column in out.columns:
            parsed = pd.to_datetime(out[column], errors="coerce", format="mixed")
            report.dates_parsed += int(parsed.notna().sum())
            out[column] = parsed
    if "amount_chf" in out.columns:
        out["amount_chf"] = pd.to_numeric(out["amount_chf"], errors="coerce")
    if "risk_score" in out.columns:
        out["risk_score"] = pd.to_numeric(out["risk_score"], errors="coerce")
    # "chf" and "CHF" are different strings to a GROUP BY: normalise the casing.
    if "currency" in out.columns:
        before = out["currency"]
        after = before.astype(str).str.upper()
        report.currency_normalised += int((before != after).sum())
        out["currency"] = after
    if "status" in out.columns:
        before = out["status"]
        after = before.astype(str).str.lower()
        report.status_normalised += int((before != after).sum())
        out["status"] = after
    # Duplicate identifiers: keep the first, quarantine the rest. Arbitrary but deterministic;
    # keeping both would break the load, dropping both would lose a real transaction.
    if "transaction_id" in out.columns:
        duplicated = out.duplicated(subset=["transaction_id"], keep="first")
        report.duplicates_removed = int(duplicated.sum())
        for _, row in out[duplicated].iterrows():
            report.quarantined_rows.append({"transaction_id": row.get("transaction_id"), "reason": "duplicate id"})
        out = out[~duplicated]
    # A missing date (failed parse) or one outside any plausible window is a parsing accident.
    if "booking_date" in out.columns:
        years = out["booking_date"].dt.year
        impossible = years.isna() | (years < MIN_YEAR) | (years > MAX_YEAR)
        report.impossible_dates_quarantined = int(impossible.sum())
        for _, row in out[impossible].iterrows():
            report.quarantined_rows.append(
                {
                    "transaction_id": row.get("transaction_id"),
                    "reason": f"booking_date outside [{MIN_YEAR}, {MAX_YEAR}]",
                }
            )
        out = out[~impossible]
    report.rows_out = len(out)
    return out, report


def clean_bronze_to_silver(bronze_dir: Path, silver_dir: Path, settings: Settings | None = None) -> CleaningReport:
    """Read the Bronze files, clean them, write the cleaned files, and return the report.

    Writes ``transactions_clean.csv``, ``customers_clean.csv``, ``accounts_clean.csv`` and, only when
    something was quarantined, ``quarantine.csv`` (a stale one from an earlier run is not removed).
    Customers and accounts are only whitespace-trimmed.

    Raises ``RuntimeError`` if the Bronze transactions file is missing, and ``AssertionError`` if no
    row survives or if the quarantine rate is 5 % or more.
    """
    with RunJournal("cleaning", settings=settings) as run:
        silver_dir.mkdir(parents=True, exist_ok=True)
        source = bronze_dir / "synthetic_transactions.csv"
        if not source.exists():
            raise RuntimeError(
                f"No Bronze transactions at {source}. Generate the raw layer first: `python run_pipeline.py --skip-ml`."
            )
        raw = pd.read_csv(source)
        clean, report = clean_transactions(raw)
        # An empty result means the cleaning rules rejected everything: always a bug in the
        # rules, never a property of the data.
        assert_invariant(
            report.rows_out > 0,
            invariant="cleaning leaves at least one row",
            reason=(
                "an empty silver layer means the cleaning rules rejected the whole "
                "batch; every downstream control would then pass on nothing"
            ),
            rows_in=report.rows_in,
            rows_out=report.rows_out,
        )

        # Quarantine must stay marginal. From 5 % up the rules are wrong, not the data, and the
        # pipeline should stop rather than silently shrink Silver.
        quarantine_rate = report.rows_quarantined / max(report.rows_in, 1)
        assert_invariant(
            quarantine_rate < 0.05,
            invariant="quarantine rate below 5%",
            reason=(
                "a high quarantine rate means the cleaning rules are miscalibrated; "
                "continuing would silently shrink the batch a control is meant to judge"
            ),
            rate=round(quarantine_rate, 4),
            quarantined=report.rows_quarantined,
        )

        clean.to_csv(silver_dir / "transactions_clean.csv", index=False)
        # Quarantined rows are written, never discarded: a row nobody can look at is a row
        # nobody will investigate.
        if report.quarantined_rows:
            pd.DataFrame(report.quarantined_rows).to_csv(silver_dir / "quarantine.csv", index=False)
        for name in ("customers", "accounts"):
            path = bronze_dir / f"synthetic_{name}.csv"
            if path.exists():
                frame = pd.read_csv(path)
                cleaned = _trim_object_columns(frame, report)
                cleaned.to_csv(silver_dir / f"{name}_clean.csv", index=False)
        run.note(
            "bronze_cleaned_to_silver",
            reason=(
                "types fixed, formats normalised, duplicates and impossible dates "
                "quarantined; business anomalies deliberately left for the control layer"
            ),
            row_count=report.rows_out,
            rows_in=report.rows_in,
            duplicates=report.duplicates_removed,
            impossible_dates=report.impossible_dates_quarantined,
            whitespace_trimmed=report.whitespace_trimmed,
            quarantined=report.rows_quarantined,
        )

        return report


def main() -> None:
    """Command-line entry point: clean ``data/`` into ``data/silver/`` and print the main counters."""
    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    root = Path(__file__).resolve().parents[3]
    report = clean_bronze_to_silver(root / "data", root / "data" / "silver")
    print(f"  rows in          {report.rows_in}")
    print(f"  rows out         {report.rows_out}")
    print(f"  duplicates       {report.duplicates_removed}")
    print(f"  impossible dates {report.impossible_dates_quarantined}")
    print(f"  whitespace       {report.whitespace_trimmed}")


if __name__ == "__main__":
    main()
