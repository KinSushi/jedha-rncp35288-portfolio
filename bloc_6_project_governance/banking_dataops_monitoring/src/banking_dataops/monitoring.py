#!/usr/bin/env python3
"""Read-only data loaders for the dashboard: one function per panel, each returning a DataFrame.

The dashboard answers one question, "can this batch be released?", and every panel under the verdict is
a query. They are kept here so that the dashboard script contains no SQL and the queries can be read,
tested and reused in one place.

CHOIX
-----
Retained:

- One loader per panel, each a ``SELECT`` that returns a pandas DataFrame. Nothing here writes to the
  database.
- Two sources, chosen per panel. The first panels (verdict, reconciliation, volume, status, channel,
  suspicious transactions) read the Silver tables and ``quality_check_results``. The analytics panels
  (heatmap, volume trend, cohorts, control trend) and the model queue read the Gold tables of
  ``sql/08_gold_layer.sql``, where the derived columns live.
- Small cells are suppressed in the analytics queries (``HAVING COUNT(*) >= 3`` for the heatmap, ``>= 5``
  for the cohorts): a cell of one or two rows would display a single suspicious transaction as a 100 %
  rate.
- Window functions (rolling average, lag) are computed in SQL, next to the data, so that the dashboard
  does not recompute them on each render.
- The analytics loaders log what they returned (``_traced``), and warn when a result is empty, so that
  "no data" can be told from "the query no longer matches the schema". Reads are not written to the audit
  journal: page renders would drown the events that matter.
- The model metrics are read from the ``metrics.json`` file of the ``models`` directory, next to the artefact
  they describe, rather than from the database.

Rejected:

- SQL inside the dashboard script: untestable and repeated.
- An ORM or a query builder: the queries are few, fixed and readable as SQL.
- Writing read queries to ``audit_events``: reading changes nothing in the system.

OÙ
---
Role: the data access layer of the presentation. Callers: ``dashboard/streamlit_app.py`` (every loader),
``api.py`` and the scheduled workflow (``.github/workflows/scheduled-pipeline.yml``, the final verdict
step).
Depends on ``banking_dataops.db.fetch_dataframe`` and ``banking_dataops.logging_config``. Reads
``transactions``, ``quality_check_results``, ``reconciliation_results``, ``gold.fact_transactions``,
``gold.dim_account``, ``gold.fact_quality_results``, ``gold.predictions`` and the view
``gold.vw_prediction_summary``, plus the ``metrics.json`` file of the ``models`` directory.

COMMENT
-------
``QualitySummary`` and ``summarize_quality_status`` turn a list of control statuses into the verdict
counts: FAIL if any control failed, otherwise WARN if any warned, otherwise PASS (the same rule as
``quality_checks.run_quality_checks``). The ``load_*`` functions run one query each; the ``limit`` of
``load_suspicious_transactions`` and ``load_top_predictions`` is cast to ``int`` before it enters the SQL
text. ``load_model_metrics`` returns an empty dictionary if the file is missing or unreadable, so the
dashboard shows no metrics rather than failing.
Tests that prove it: tests/test_monitoring.py, tests/test_analytics_queries.py (the queries and the
tracing), tests/test_reverse_paths.py (the verdict is derived only from persisted control results).

POURQUOI
--------
- Cells under 3 and cohorts under 5 rows are suppressed: a rate computed on a handful of rows is a number,
  not a signal. The two thresholds are project choices, source : inconnue.
- ``NULLIF(COUNT(*), 0)`` in ``load_control_trend``: before the first run the denominator is zero, and a
  division error in a panel looks like missing data; NULL gives an empty cell.
- Limits are cast with ``int(...)`` because they are interpolated into the SQL text instead of bound; the
  callers pass numbers, and the cast refuses anything else (docs/CODE_STANDARD.md, section 5).
- ``quality_check_results`` keeps one row per control (it is replaced on each run), so
  ``load_control_trend`` shows one point per control with the current writer. A history needs a writer
  that keeps the earlier rows: not implemented today.
- The ``reconciliation`` panel shows a coherence summary, not a comparison between two systems
  (banking_dataops/reconciliation.py).

JOURNALISATION
--------------
The ``_traced`` helper logs at DEBUG what a query returned (name, reason, row count), and at WARNING
``<name>_empty`` when the result is empty. The loaders that do not use it log nothing. No audit event is
written: this module only reads. Errors from the database propagate to the dashboard, which catches them and
shows a message. No ``print``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from banking_dataops.config import Settings
from banking_dataops.db import fetch_dataframe
from banking_dataops.logging_config import get_logger

logger = get_logger("monitoring")


def _traced(name: str, frame: pd.DataFrame, reason: str) -> pd.DataFrame:
    """Log what an analytics query returned, and warn if it returned nothing.

    An empty panel has two very different causes: there is no data yet, or the query silently matches
    nothing after a schema change. Without a trace they are indistinguishable at a glance, and the
    second is the dangerous one. The frame is returned unchanged.
    """
    logger.debug(
        name,
        extra={"category": "data", "reason": reason, "rows": len(frame)},
    )
    if frame.empty:
        logger.warning(
            f"{name}_empty",
            extra={
                "category": "data",
                "reason": (
                    "query returned no rows; either the layer is not built yet "
                    "or the query no longer matches the schema"
                ),
                "rows": 0,
            },
        )
    return frame


@dataclass(frozen=True)
class QualitySummary:
    """Counts of control statuses, and the batch verdict they imply.

    ``overall_status`` is FAIL if any control failed, WARN if none failed but one warned, else PASS.
    """

    total_checks: int
    passed_checks: int
    failed_checks: int
    warning_checks: int = 0

    @property
    def overall_status(self) -> str:
        """Return ``FAIL`` if any check failed, ``WARN`` if only warnings, otherwise ``PASS``."""
        if self.failed_checks > 0:
            return "FAIL"
        if self.warning_checks > 0:
            return "WARN"
        return "PASS"


def summarize_quality_status(statuses: list[str]) -> QualitySummary:
    """Count PASS, WARN and FAIL in a list of statuses (case-insensitive) into a ``QualitySummary``."""
    normalized = [status.upper() for status in statuses]
    return QualitySummary(
        total_checks=len(normalized),
        passed_checks=normalized.count("PASS"),
        failed_checks=normalized.count("FAIL"),
        warning_checks=normalized.count("WARN"),
    )


def load_latest_quality_results(settings: Settings | None = None) -> pd.DataFrame:
    """Return the persisted result of each control, ordered by control identifier.

    There is one row per control: the table is replaced on each run, so these are the latest results.
    """
    return fetch_dataframe(
        """
        SELECT control_id, check_name, status, failed_rows, severity, executed_at
        FROM quality_check_results
        ORDER BY control_id
        """,
        settings=settings,
    )


def load_reconciliation_summary(settings: Settings | None = None) -> pd.DataFrame:
    """Return the last 20 reconciliation rows (counts and amounts on both sides, and the deltas).

    Both sides come from the same table, so the deltas are zero: this is a coherence summary, not a
    comparison between two systems (see ``banking_dataops.reconciliation``).
    """
    return fetch_dataframe(
        """
        SELECT reconciliation_name, source_count, target_count, count_delta,
               source_total, target_total, amount_delta, executed_at
        FROM reconciliation_results
        ORDER BY executed_at DESC
        LIMIT 20
        """,
        settings=settings,
    )


def load_transaction_volume(settings: Settings | None = None) -> pd.DataFrame:
    """Return the daily count and CHF total of transactions, by booking date."""
    return fetch_dataframe(
        """
        SELECT booking_date, COUNT(*) AS transaction_count, ROUND(SUM(amount_chf), 2) AS total_amount_chf
        FROM transactions
        GROUP BY booking_date
        ORDER BY booking_date
        """,
        settings=settings,
    )


def load_status_summary(settings: Settings | None = None) -> pd.DataFrame:
    """Return the count and CHF total of transactions per status."""
    return fetch_dataframe(
        """
        SELECT status, COUNT(*) AS transaction_count, ROUND(SUM(amount_chf), 2) AS total_amount_chf
        FROM transactions
        GROUP BY status
        ORDER BY status
        """,
        settings=settings,
    )


def load_channel_summary(settings: Settings | None = None) -> pd.DataFrame:
    """Return the transaction count and average risk score per channel, largest channel first."""
    return fetch_dataframe(
        """
        SELECT channel, COUNT(*) AS transaction_count, ROUND(AVG(risk_score), 4) AS avg_risk_score
        FROM transactions
        GROUP BY channel
        ORDER BY transaction_count DESC
        """,
        settings=settings,
    )


def load_suspicious_transactions(limit: int = 50, settings: Settings | None = None) -> pd.DataFrame:
    """Return the transactions labelled suspicious, highest risk score first.

    "Suspicious" here is the label ``is_suspicious`` (``risk_score`` above 0.75 in the generator), not
    a model output; the model's queue is ``load_top_predictions``.
    """
    return fetch_dataframe(
        f"""
        SELECT transaction_id, account_id, event_timestamp, amount_chf, channel,
               country, risk_score, status, is_suspicious
        FROM transactions
        WHERE is_suspicious
        ORDER BY risk_score DESC, event_timestamp DESC
        LIMIT {int(limit)}
        """,
        settings=settings,
    )


def load_prediction_summary() -> pd.DataFrame:
    """Return the investigation queue grouped by priority band, ``high`` first.

    An analyst opens the dashboard to decide where to start, not to read 1,500 scores: the summary gives
    each band's count, average score, CHF total and number of transactions labelled suspicious. It reads
    the view ``gold.vw_prediction_summary``. The band rule is in ``banking_dataops.ml.predict`` (ADR-0009).
    """
    return fetch_dataframe(
        """
        SELECT priority_band, transaction_count, avg_score,
               total_amount_chf, labelled_suspicious, last_scored_at
        FROM gold.vw_prediction_summary
        ORDER BY CASE priority_band
                     WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END
        """
    )


def load_top_predictions(limit: int = 15) -> pd.DataFrame:
    """Return the highest-scored transactions with their context: the head of the investigation queue."""
    return fetch_dataframe(
        f"""
        SELECT p.transaction_id, p.suspicion_score, p.priority_band,
               f.amount_chf, f.channel, f.merchant_category,
               f.booking_hour, f.is_cross_border, f.is_suspicious AS labelled,
               p.model_version
        FROM gold.predictions p
        JOIN gold.fact_transactions f USING (transaction_id)
        ORDER BY p.suspicion_score DESC
        LIMIT {int(limit)}
        """
    )


def load_model_metrics() -> dict[str, object]:
    """Return the training report saved next to the model, or an empty dict if it is missing or unreadable.

    Read from ``models/metrics.json`` and not from the database: the figures belong to the artefact, and
    keeping them in the same directory lowers the risk of showing the metrics of another model. Both
    files are written by ``ml.train`` one after the other, not atomically.
    """
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "models" / "metrics.json"
    if not path.exists():
        return {}
    try:
        metrics: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return metrics
    except Exception:
        return {}


def load_risk_heatmap(settings: Settings | None = None) -> pd.DataFrame:
    """Return the suspicion rate by booking hour and channel, the source of the heatmap.

    A matrix rather than two bar charts because risk concentration is a joint effect: night activity is
    risky, API activity is risky, and night API activity is riskier than either alone. Demo rows are
    excluded. Cells with fewer than three transactions are left out (``HAVING``).
    """
    query = """
        SELECT
            booking_hour,
            channel,
            COUNT(*)                                              AS transaction_count,
            ROUND(AVG(CASE WHEN is_suspicious THEN 1.0 ELSE 0.0 END) * 100, 1)
                                                                  AS suspicion_rate_pct
        FROM gold.fact_transactions
        WHERE NOT is_demo_row
        GROUP BY booking_hour, channel
        HAVING COUNT(*) >= 3
        ORDER BY booking_hour, channel
    """
    return _traced(
        "load_risk_heatmap",
        fetch_dataframe(query, settings=settings),
        reason="risk concentration by hour and channel, read by the dashboard heatmap",
    )


def load_volume_trend(settings: Settings | None = None) -> pd.DataFrame:
    """Return the daily volume with a 7-day rolling average and the change from the previous day.

    Raw daily counts swing with the weekday. A feed that silently stops delivering half its rows looks
    like an ordinary Monday until it is compared with its own trailing week. The window functions run in
    SQL so the dashboard does not recompute them on each render.
    """
    query = """
        WITH daily AS (
            SELECT
                date_key,
                COUNT(*)                     AS transaction_count,
                ROUND(SUM(amount_chf), 2)    AS total_amount_chf
            FROM gold.fact_transactions
            WHERE NOT is_demo_row
            GROUP BY date_key
        )
        SELECT
            date_key,
            transaction_count,
            total_amount_chf,
            ROUND(AVG(transaction_count) OVER (
                ORDER BY date_key ROWS BETWEEN 6 PRECEDING AND CURRENT ROW
            ), 1)                                                AS rolling_7d_avg,
            transaction_count - LAG(transaction_count) OVER (ORDER BY date_key)
                                                                 AS change_vs_previous_day
        FROM daily
        ORDER BY date_key
    """
    return _traced(
        "load_volume_trend",
        fetch_dataframe(query, settings=settings),
        reason="daily volume and rolling average, used to spot a partially delivered feed",
    )


def load_segment_cohorts(settings: Settings | None = None) -> pd.DataFrame:
    """Return risk and volume by customer segment and merchant category, riskiest cohort first.

    A cohort view answers "where do we tighten controls first", which a total cannot. Cohorts with fewer
    than five transactions are left out: a rate on two transactions is not a signal.
    """
    query = """
        SELECT
            a.customer_segment,
            f.merchant_category,
            COUNT(*)                                              AS transaction_count,
            ROUND(SUM(f.amount_chf), 2)                           AS total_amount_chf,
            ROUND(AVG(CASE WHEN f.is_suspicious THEN 1.0 ELSE 0.0 END) * 100, 1)
                                                                  AS suspicion_rate_pct,
            ROUND(AVG(f.amount_chf), 2)                           AS avg_amount_chf
        FROM gold.fact_transactions f
        JOIN gold.dim_account a ON a.account_id = f.account_id
        WHERE NOT f.is_demo_row
        GROUP BY a.customer_segment, f.merchant_category
        HAVING COUNT(*) >= 5
        ORDER BY suspicion_rate_pct DESC
    """
    return _traced(
        "load_segment_cohorts",
        fetch_dataframe(query, settings=settings),
        reason="risk cohorts by customer segment and merchant category",
    )


def load_control_trend(settings: Settings | None = None) -> pd.DataFrame:
    """Return the pass rate of each control by date, meant to show a slow degradation.

    ``NULLIF`` guards the division: before the first run the denominator is zero, and NULL gives an
    empty cell instead of a division error. As ``quality_check_results`` is replaced on each run, the
    result holds one point per control today (see POURQUOI in the module docstring).
    """
    query = """
        SELECT
            date_key,
            control_id,
            SUM(is_pass)                                          AS passes,
            SUM(is_fail)                                          AS failures,
            ROUND(100.0 * SUM(is_pass) / NULLIF(COUNT(*), 0), 1)   AS pass_rate_pct
        FROM gold.fact_quality_results
        GROUP BY date_key, control_id
        ORDER BY date_key, control_id
    """
    return _traced(
        "load_control_trend",
        fetch_dataframe(query, settings=settings),
        reason="control pass rate over time, used to catch slow degradation",
    )
