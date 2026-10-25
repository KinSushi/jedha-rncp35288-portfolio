#!/usr/bin/env python3
"""Streamlit dashboard: can this batch of data be released to downstream systems?

The first thing on screen is the answer, a coloured banner with four indicators; everything below is the evidence
for it, then the investigation queue of the model, a demonstration scenario, and risk analytics.

CHOIX
-----
Retained:

- One script, top to bottom (Streamlit re-runs it on each interaction). The SQL lives in
  ``banking_dataops.monitoring``, so this file holds layout and wording only.
- The verdict first, in three seconds: green when every control passed, orange when a control only warned, red
  when one failed. The same rule as the API (``banking_dataops.quality_checks``, ``monitoring.QualitySummary``).
- Each section is guarded by its own ``try`` so that a missing table or layer shows a message with the command to
  run, not a stack trace, and the sections above it stay visible.
- The model section says in so many words that the classifier orders analyst attention and does not decide
  publication (ADR-0002, ADR-0008). It shows the cross-validation selection, the threshold with its source, and the
  priority bands with their rule (ADR-0009).
- Read-only, except the two demo buttons (inject, reset), which are marked and reversible.
- Analytics read the Gold layer (heatmap, volume trend, cohorts, control trend), where the derived columns live.

Rejected:

- A multi-page application: the page is short enough for one script. Source : inconnue for any measurement.
- Computing in the dashboard: window functions and rates are computed in SQL.
- Hiding the model's limits: the metrics shown are those of the test rows, scored once after the choices were fixed.

OÙ
---
Role: the human interface of the system, port 8501. Callers: ``docker/entrypoint.sh`` (``streamlit run
dashboard/streamlit_app.py``), ``make dashboard`` locally. Depends on ``banking_dataops.monitoring`` (every query),
``banking_dataops.demo_scenario`` (the buttons), ``banking_dataops.quality_checks`` (re-run after a button) and
``banking_dataops.ml.predict`` (the band floor, ``MEDIUM_BAND_FLOOR``). Reads the ``metrics.json`` file of the
``models`` directory through ``monitoring.load_model_metrics``. The Hugging Face Space
(``huggingface-space/app.py``) is a separate, smaller application over a cloud database.

COMMENT
-------
The script loads the six first datasets in one ``try``; if it fails it stops with a hint. It then draws the
banner and indicators, the control table with a triage box per failing control, the volume, reconciliation,
status, channel and suspicious-transaction panels, the model queue, the demo expander, and the Gold analytics.
After a demo button it calls ``st.rerun()`` so the page reflects the new verdict.
Tests that prove it: no unit test runs this script (it is a Streamlit runtime). The functions it calls are
tested through tests/test_monitoring.py and tests/test_analytics_queries.py, and ``run.ps1 smoke`` asks the
running dashboard for its health endpoint.

POURQUOI
--------
- Verdict first: docs/project/07-indicateurs-de-pilotage.md describes the three-second reading, the banner.
- A warning is not a block: CTRL-007 warns when the newest event is more than 60 days old, and the banner then reads
  "Release under reservation". That control uses the wall clock, so a batch generated with the default anchor of
  2026-07-27 shows the orange banner from 25 September 2026 (ADR-0009).
- "Control pass rate over time" currently shows one point per control: the table it reads is replaced on each run
  (see ``monitoring.load_control_trend``).
- No authentication: the dashboard is for local use on synthetic data (ADR-0005, ADR-0006). If exposed beyond a local
  network, authentication should be added.

JOURNALISATION
--------------
This script writes no log of its own. Streamlit's server logs go to the container output. The demo buttons call
``demo_scenario`` and ``quality_checks``, which journal their events to ``audit_events`` and the structured log.
Database errors are caught per section and shown with ``st.error``, ``st.info`` or ``st.caption``; nothing is
swallowed silently.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

try:
    import psycopg  # noqa: F401  l'import *est* le test de disponibilité
except ModuleNotFoundError:
    st.error("**Dépendance manquante : psycopg**\nExécute : `pip install -e .`")
    st.stop()
try:
    from banking_dataops import demo_scenario
except ImportError as e:
    st.error(f"""
Dépendance manquante :{e}

Exécute :

pip install -e .
""")

    st.stop()  # noqa: E402
from banking_dataops.ml.predict import MEDIUM_BAND_FLOOR  # noqa: E402
from banking_dataops.monitoring import (  # noqa: E402
    load_channel_summary,
    load_control_trend,
    load_latest_quality_results,
    load_model_metrics,
    load_prediction_summary,
    load_reconciliation_summary,
    load_risk_heatmap,
    load_segment_cohorts,
    load_status_summary,
    load_suspicious_transactions,
    load_top_predictions,
    load_transaction_volume,
    load_volume_trend,
    summarize_quality_status,
)
from banking_dataops.quality_checks import run_quality_checks  # noqa: E402

st.set_page_config(page_title="Banking DataOps Monitoring", layout="wide")

st.title("Banking DataOps Monitoring")
st.caption(
    "Can this batch of data be released to downstream systems? "
    "Synthetic regulated-data monitoring - public technical evidence only."
)

try:
    quality = load_latest_quality_results()
    reconciliation = load_reconciliation_summary()
    volume = load_transaction_volume()
    status = load_status_summary()
    channel = load_channel_summary()
    suspicious = load_suspicious_transactions()
except Exception as exc:  # pragma: no cover - dashboard runtime path
    st.error("Database is not ready or schema is missing.")
    st.markdown(
        """
        Run the local setup first:

        ```bash
        make up && make generate && make ingest && make quality && make reconcile
        ```

        Or start everything in one command:

        ```bash
        make stack-up
        ```
        """
    )
    st.code(str(exc))
    st.stop()

if quality.empty:
    st.warning("No quality checks have been executed yet. Run `make quality`.")
else:
    summary = summarize_quality_status(quality["status"].tolist())
    failing = quality[quality["status"] == "FAIL"]
    rows_in_violation = int(failing["failed_rows"].sum()) if not failing.empty else 0
    total_transactions = int(volume["transaction_count"].sum()) if not volume.empty else 0
    total_amount = float(volume["total_amount_chf"].sum()) if not volume.empty else 0.0
    last_run = quality["executed_at"].max()
    kpi_1, kpi_2, kpi_3, kpi_4 = st.columns(4)
    # The delta is negative ("-3 failed"): fewer controls passed is bad news. "normal" shows a negative delta in red;
    # "inverse", used before, showed it in green, a reassuring colour on a blocked batch.
    kpi_1.metric(
        "Controls passed",
        f"{summary.passed_checks}/{summary.total_checks}",
        delta=None if summary.failed_checks == 0 else f"-{summary.failed_checks} failed",
        delta_color="normal",
    )
    kpi_2.metric("Rows in violation", f"{rows_in_violation:,}".replace(",", " "))
    kpi_3.metric(
        "Transactions under control",
        f"{total_transactions:,}".replace(",", " "),
        help=f"Total booked amount: CHF {total_amount:,.2f}".replace(",", " "),
    )
    kpi_4.metric(
        "Last control run",
        last_run.strftime("%d/%m %H:%M") if last_run is not None else "n/a",
    )
    if summary.overall_status == "PASS":
        st.success("**Batch cleared for release** - all controls satisfied. Data can flow to downstream systems.")
    elif summary.overall_status == "WARN":
        st.warning(f"**Release under reservation** - {summary.warning_checks} warning(s). Review before proceeding.")
    else:
        failed_ids = ", ".join(failing["control_id"].tolist())
        st.error(
            f"**Release blocked** - {summary.failed_checks} control(s) in failure "
            f"({failed_ids}), {rows_in_violation} row(s) affected. "
            "See the failing controls and the associated runbook below."
        )
st.divider()

st.subheader("Data quality status")
if not quality.empty:
    st.dataframe(quality, use_container_width=True)
    failing = quality[quality["status"] == "FAIL"]
    if not failing.empty:
        st.markdown("**Failing controls - triage**")
        for _, row in failing.iterrows():
            with st.expander(
                f"{row['control_id']} - {row['check_name']} - {row['failed_rows']} row(s) - severity {row['severity']}",
                expanded=True,
            ):
                st.markdown(
                    "1. Identify the failed control and its row count.\n"
                    "2. Isolate the affected booking date, source system or channel.\n"
                    "3. Decide: quarantine the batch, correct upstream, or document "
                    "a known issue.\n\n"
                    "Full procedure: `docs/incident_runbook.md`"
                )

st.subheader("Transaction volume by booking date")
if not volume.empty:
    st.line_chart(volume.set_index("booking_date")["transaction_count"])
    st.dataframe(volume, use_container_width=True)

st.subheader("Reconciliation results")
if reconciliation.empty:
    st.warning("No reconciliation results found. Run `make reconcile`.")
else:
    st.dataframe(reconciliation, use_container_width=True)

col_a, col_b = st.columns(2)
with col_a:
    st.subheader("Status distribution")
    st.dataframe(status, use_container_width=True)
with col_b:
    st.subheader("Channel summary")
    st.dataframe(channel, use_container_width=True)

st.subheader("Suspicious / high-risk transaction sample")
st.dataframe(suspicious, use_container_width=True)

st.divider()
st.subheader("Investigation queue - model output")
st.caption(
    "The classifier orders analyst attention. It does not decide publication: "
    "that verdict comes from the deterministic controls above."
)
try:
    bands = load_prediction_summary()
    top = load_top_predictions()
    metrics = load_model_metrics()
except Exception as exc:  # pragma: no cover - dashboard runtime path
    st.info(
        "No predictions yet. Train the model, then score the batch: "
        "`python -m banking_dataops.ml.train`, then `python -m banking_dataops.ml.predict` "
        "(or `make ml-train`, then `make ml-predict`). `run_pipeline.py --train` trains but does not score."
    )
    st.caption(str(exc))
else:
    if metrics:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Model", cast("str", metrics.get("model_family", "n/a")))
        m2.metric("ROC AUC (test)", cast("float | str", metrics.get("roc_auc", "n/a")))
        m3.metric(
            "Precision / Recall",
            f"{metrics.get('precision', 0):.2f} / {metrics.get('recall', 0):.2f}",
        )
        m4.metric("Threshold", cast("float | str", metrics.get("decision_threshold", "n/a")))
        if metrics.get("selection_reason"):
            st.caption(f"Model selection (cross-validation on the training rows): {metrics['selection_reason']}")
        if metrics.get("threshold_source"):
            st.caption(
                f"Threshold {metrics.get('decision_threshold')}: {metrics.get('threshold_criterion')} on "
                f"{metrics['threshold_source']}. Precision, recall and ROC AUC come from the test rows, "
                "scored once after the model and the threshold were fixed."
            )
    if not bands.empty:
        left, right = st.columns([1, 2])
        with left:
            st.markdown("**Queue by priority**")
            st.dataframe(bands, use_container_width=True, hide_index=True)
            threshold = metrics.get("decision_threshold") if metrics else None
            if threshold:
                floor = MEDIUM_BAND_FLOOR * float(cast("float", threshold))
                st.caption(
                    f"high: score of at least {threshold} (predicted suspicious, investigate first). "
                    f"medium: from {floor:.3f} to the threshold (grey zone). "
                    "low: below."
                )
        with right:
            st.markdown("**Head of the queue**")
            st.dataframe(top, use_container_width=True, hide_index=True)
    if metrics.get("top_features"):
        st.markdown("**What drives the score**")
        st.caption(
            "An analyst receiving a flagged transaction must be told which "
            "factors drove it, otherwise the queue is a black box and gets ignored."
        )
        st.dataframe(
            pd.DataFrame(cast("list[dict[str, object]]", metrics["top_features"])).head(8),
            use_container_width=True,
            hide_index=True,
        )

st.divider()

with st.expander("Demo scenario - inject a degraded batch", expanded=False):
    st.markdown(
        "Injects a small batch that deliberately breaks three business-rule "
        "controls, so the detection path can be observed live. Injected rows are "
        "tagged `source_system = demo_scenario` and are removed exactly, without "
        "touching reference data.\n\n"
        "`CTRL-001`, `CTRL-002` and `CTRL-004` cannot be broken this way: the "
        "schema enforces them structurally through `NOT NULL`, a primary key and "
        "a foreign key. The control layer exists to catch what the database "
        "cannot see - business rules."
    )
    injected: bool | None
    try:
        injected = demo_scenario.is_injected()
    except Exception as exc:  # pragma: no cover - dashboard runtime path
        st.error(f"Demo scenario unavailable: {exc}")
        injected = None
    if injected is not None:
        left, right = st.columns(2)

        if left.button(
            "Inject degraded batch",
            type="primary",
            disabled=injected,
            use_container_width=True,
        ):
            try:
                with st.spinner("Injecting and re-running controls..."):
                    violations = demo_scenario.inject()
                    run_quality_checks()
            except Exception as exc:  # pragma: no cover - dashboard runtime path
                st.error(
                    f"Injection failed. The pipeline must have run at least once so that accounts exist. Details: {exc}"
                )
            else:
                for violation in violations:
                    st.write(f"**{violation.control_id}** - {violation.narrative}")
                st.rerun()
        if right.button(
            "Reset to clean state",
            disabled=not injected,
            use_container_width=True,
        ):
            try:
                with st.spinner("Removing batch and re-running controls..."):
                    demo_scenario.reset()
                    run_quality_checks()
            except Exception as exc:  # pragma: no cover - dashboard runtime path
                st.error(f"Reset failed: {exc}")
            else:
                st.rerun()

st.divider()
st.subheader("Where the risk concentrates")
st.caption(
    "Risk is a joint effect. Night activity is risky, API activity is risky, and "
    "night API activity is riskier than either alone. Two separate charts would "
    "hide that interaction."
)
try:
    heatmap = load_risk_heatmap()
    trend = load_volume_trend()
    cohorts = load_segment_cohorts()
    control_trend = load_control_trend()
except Exception as exc:  # pragma: no cover - dashboard runtime path
    st.info("Gold layer not built yet. Run: `python run_pipeline.py`")
    st.caption(str(exc))
else:
    if not heatmap.empty:
        pivot = heatmap.pivot_table(
            index="channel",
            columns="booking_hour",
            values="suspicion_rate_pct",
            aggfunc="mean",
        ).fillna(0)
        st.markdown("**Suspicion rate by hour and channel (%)**")
        st.dataframe(
            pivot.style.background_gradient(cmap="Reds", axis=None).format("{:.1f}"),
            use_container_width=True,
        )
        st.caption(
            "Read the top-left block: the early hours on the API channel. That is "
            "the cell that justifies tightening controls by window rather than "
            "uniformly."
        )
    if not trend.empty:
        st.markdown("**Volume trend and 7-day rolling average**")
        chart_data = trend.set_index("date_key")[["transaction_count", "rolling_7d_avg"]]
        st.line_chart(chart_data)

        latest = trend.iloc[-1]
        change = latest.get("change_vs_previous_day")
        if change is not None and not pd.isna(change):
            direction = "up" if change > 0 else "down"
            st.caption(
                f"Last day: {int(latest['transaction_count'])} transactions, "
                f"{direction} {abs(int(change))} versus the previous day. "
                "A feed that silently halves its delivery looks like an ordinary "
                "Monday until compared to its own trailing week."
            )
    left, right = st.columns(2)
    with left:
        if not cohorts.empty:
            st.markdown("**Risk cohorts: segment by category**")
            st.dataframe(cohorts.head(12), use_container_width=True, hide_index=True)
            st.caption("Sorted by suspicion rate: where to tighten controls first.")
    with right:
        if not control_trend.empty:
            st.markdown("**Control pass rate over time**")
            pivot_controls = control_trend.pivot_table(index="date_key", columns="control_id", values="pass_rate_pct")
            st.line_chart(pivot_controls)
            st.caption(
                "A control that degrades slowly is invisible in a snapshot. "
                "This is the view that catches it before it becomes an incident."
            )

st.caption(
    "Synthetic data only. Do not load real banking, insurance, health, client or "
    "employer data. Demonstration on synthetic data, not connected to production systems."
)
