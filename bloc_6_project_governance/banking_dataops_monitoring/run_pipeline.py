#!/usr/bin/env python3
"""Run the data pipeline end to end: Bronze, cleaning, Silver, Gold, optional model.

One command replaces five. Each step is timed, journalled under a single run
identifier, and the whole run stops at the first failure with a non-zero exit code.

CHOIX
-----
Retained:

- A plain sequential script over the existing modules, one function per step, each
  returning a ``StepResult``. All steps share one ``RunJournal`` so that every audit
  event of a run carries the same ``run_id``.
- An explicit date anchor for the generated batch, ``--generated-at``, resolved once
  by ``banking_dataops.generate_synthetic_data.resolve_anchor``: the command-line
  value, else ``BANKING_DATAOPS_GENERATED_AT``, else the fixed reference
  2026-07-27T00:00:00Z. The anchor is recorded in the ``bronze_generated`` event.
- Model training only on request (``--train``), never on every run.
- The step modules are imported inside the step functions. The original design note gave
  "avoid circular dependencies" as the reason; that was not re-checked here.

Rejected:

- Anchoring on the current day, the earlier behaviour: the batch changed with the
  weekday of execution, and so did two model inputs, the AUC and the threshold
  (ADR-0009).
- Retraining on every run: a model that is silently replaced drifts without anyone
  noticing (ADR-0008, "Conséquences négatives").
- An orchestrator such as Airflow or Prefect: oversized for a sequential chain without
  recovery (ADR-0001). Airflow stays a medium-term option.

OÙ
---
Role: the orchestrator at the root of the repository. Callers: ``docker/entrypoint.sh``
(``--skip-ml``), the Makefile targets ``pipeline``, ``pipeline-train`` and
``pipeline-verify``, ``run.ps1``, and ``.github/workflows/scheduled-pipeline.yml``.
Depends on: ``banking_dataops.generate_synthetic_data`` (Bronze),
``banking_dataops.pipeline.cleaning`` (Silver files), ``banking_dataops.ingest`` and
``banking_dataops.quality_checks`` (Silver tables and controls),
``banking_dataops.pipeline.gold`` (Gold), ``banking_dataops.ml.train`` (optional),
``banking_dataops.audit`` and ``banking_dataops.logging_config``.
It does NOT call ``banking_dataops.ml.predict``: scoring is a separate command
(``make ml-predict``).

COMMENT
-------
``main`` parses the arguments, configures logging, opens a run identifier, resolves the
anchor and loads the settings, then calls ``run``. Anchor or settings errors return 2
before anything is generated or written; a failing step is printed and returns 1.
``run`` executes the steps in dependency order inside one ``RunJournal``:
Bronze (CSV files), cleaning (Bronze to Silver files), Silver (load, then the seven
controls), Gold (star schema), then the model if ``--train`` and not ``--skip-ml``.
``--verify`` is accepted for compatibility with the Makefile, ``run.ps1`` and the
scheduled workflow, which call it "idempotence check", but it currently changes
nothing: it runs the same single pipeline. Idempotence itself comes from the load
steps (TRUNCATE then INSERT) and is not asserted here.
Tests that prove it: tests/test_anchor.py (anchor precedence, refusal of an invalid
anchor with exit code 2 before any step).

POURQUOI
--------
- A fixed default anchor: the only date that reproduces the logged batch fingerprint
  89ac138fb9943f00 (evidence/pipeline_full.txt, evidence/rejeu_modele_2026-07-27.md).
- Exit code 2 for configuration, 1 for a failed run: shell callers (entrypoint, CI) can
  tell "fix your command" from "the pipeline broke" (docs/CODE_STANDARD.md, section 7).
- The run stops at the first failing step because each step reads the output of the
  previous one; ADR-0001 describes the chain as four sequential, replayable steps. No ADR
  states the stop-on-failure rule itself: source : inconnue.
- Whether ``--verify`` should assert idempotence is an open decision: source : inconnue
  (not specified in docs/adr/).

JOURNALISATION
--------------
``RunJournal("pipeline")`` records the run start and end (category ``pipeline``); every
step module records its own events under the same ``run_id`` (``bronze_generated``,
``bronze_cleaned``, ``table_loaded``, ``verdict_issued``, ``gold_table_loaded``, and the
model events). Events go to the structured log at INFO (WARNING when the best-effort
write to ``audit_events`` fails) and, best effort, to the table. A failing step closes
the run as failed and the exception reaches ``main``. ``main`` prints the summary and
the errors: this file is an entry point.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

# Lets the script run from a fresh checkout without `pip install -e .`.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from banking_dataops.audit import RunJournal, fingerprint_directory, record_event  # noqa: E402
from banking_dataops.config import load_settings  # noqa: E402
from banking_dataops.db import fetch_rows  # noqa: E402
from banking_dataops.generate_synthetic_data import DEFAULT_GENERATED_AT, GENERATED_AT_ENV, resolve_anchor  # noqa: E402
from banking_dataops.logging_config import configure_logging, new_run_id, set_step  # noqa: E402


@dataclass
class StepResult:
    """Outcome of one pipeline step, printed in the end-of-run summary.

    ``rows`` is the row count the step is accountable for (generated, kept after
    cleaning, present in the table), not a measure of work done.
    """

    name: str
    rows: int
    duration_ms: int
    detail: str = ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line. ``argv`` is injectable so the parser can be tested."""
    parser = argparse.ArgumentParser(description="Bronze -> Silver -> Gold -> ML pipeline.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--transactions", type=int, default=1500, help="Number of transactions to generate")
    parser.add_argument("--customers", type=int, default=100, help="Number of customers to generate")
    parser.add_argument(
        "--generated-at",
        default=None,
        help=(
            "Date anchor of the generated batch, ISO 8601 (e.g. 2026-07-27T00:00:00Z) or 'today'. "
            f"Default: ${GENERATED_AT_ENV}, else {DEFAULT_GENERATED_AT:%Y-%m-%dT%H:%M:%SZ}, "
            "the anchor that reproduces the logged fingerprint."
        ),
    )
    parser.add_argument("--train", action="store_true", help="Train the ML model after the Gold step")
    parser.add_argument("--skip-ml", action="store_true", help="Skip all ML steps (overrides --train)")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Accepted for compatibility; currently performs no check beyond a normal run",
    )
    parser.add_argument("--log-format", choices=["human", "json"], default=None, help="Logging format")
    return parser.parse_args(argv)


def step_bronze(seed: int, transactions: int, customers: int, generated_at: datetime) -> StepResult:
    """Generate the synthetic raw files and journal their fingerprint.

    The fingerprint (SHA-256 over file names and contents) and the anchor are written
    to the audit event, so a later reader can tell which batch a verdict was issued on.
    """
    from banking_dataops.generate_synthetic_data import SyntheticConfig, generate

    set_step("bronze")
    started = time.monotonic()
    config = SyntheticConfig(
        customers=customers,
        accounts_per_customer=2,
        transactions=transactions,
        seed=seed,
        generated_at=generated_at,
    )
    generate(config, Path("data"))
    digest = fingerprint_directory(Path("data"), "synthetic_*.csv")
    record_event(
        "data",
        "bronze_generated",
        reason="raw layer produced deterministically from the seed and the date anchor",
        row_count=transactions,
        seed=seed,
        generated_at=generated_at.isoformat(),
        fingerprint=digest[:16],
    )
    return StepResult("bronze", transactions, int((time.monotonic() - started) * 1000))


def step_cleaning() -> StepResult:
    """Clean Bronze into Silver files; the cleaning module quarantines what it rejects."""
    from banking_dataops.pipeline.cleaning import clean_bronze_to_silver

    set_step("cleaning")
    started = time.monotonic()
    report = clean_bronze_to_silver(Path("data"), Path("data") / "silver")
    return StepResult("cleaning", report.rows_out, int((time.monotonic() - started) * 1000))


def step_silver() -> StepResult:
    """Load the raw Bronze files into PostgreSQL, then evaluate the quality controls.

    The load reads ``data/synthetic_*.csv``, not the files written by the cleaning step
    (``banking_dataops.ingest``, module docstring).

    The return value of ``run_quality_checks`` is ignored on purpose: its effects are
    what matters (controls evaluated, results persisted, verdict journalled).
    """
    from banking_dataops.ingest import ingest_all
    from banking_dataops.quality_checks import run_quality_checks

    set_step("silver")
    started = time.monotonic()
    ingest_all()
    run_quality_checks()
    rows = int(cast("int", fetch_rows("SELECT COUNT(*) AS n FROM transactions")[0]["n"]))
    return StepResult("silver", rows, int((time.monotonic() - started) * 1000))


def step_gold() -> StepResult:
    """Build the Gold tables (star schema) from Silver."""
    from banking_dataops.pipeline.gold import build

    set_step("gold")
    started = time.monotonic()
    counts = build()
    return StepResult("gold", counts.get("gold.fact_transactions", 0), int((time.monotonic() - started) * 1000))


def run(args: argparse.Namespace, generated_at: datetime) -> list[StepResult]:
    """Run every step in dependency order under one journal and return their results."""
    steps: list[StepResult] = []
    with RunJournal("pipeline", seed=args.seed):
        steps.append(step_bronze(args.seed, args.transactions, args.customers, generated_at))
        steps.append(step_cleaning())
        steps.append(step_silver())
        steps.append(step_gold())
        if not args.skip_ml and args.train:
            from banking_dataops.ml.train import train

            ml_started = time.monotonic()
            report = train()
            # train() returns a TrainingReport, not a StepResult: adapt it so the
            # end-of-run summary stays uniform. The detail carries the shipped model and
            # the AUC of the single final evaluation.
            steps.append(
                StepResult(
                    "ml_train",
                    report.rows_total,
                    int((time.monotonic() - ml_started) * 1000),
                    f"{report.model_name} {report.model_family} test_roc_auc={report.roc_auc:.4f}",
                )
            )
    return steps


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns 0 on success, 1 if a step failed, 2 on a configuration error."""
    args = parse_args(argv)
    configure_logging(fmt=args.log_format)
    new_run_id()

    # Both checks come before any file is written or any table touched: an invalid
    # anchor must not leave a half-generated batch behind.
    try:
        generated_at = resolve_anchor(args.generated_at)
        load_settings()
    except Exception as exc:
        print(f"Configuration error: {exc}")
        return 2

    try:
        steps = run(args, generated_at)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 1

    print(f"Pipeline completed successfully (anchor {generated_at:%Y-%m-%dT%H:%M:%SZ}).")
    for step in steps:
        print(f"  {step.name:<12} {step.rows:>8} rows  {step.duration_ms:>6} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
