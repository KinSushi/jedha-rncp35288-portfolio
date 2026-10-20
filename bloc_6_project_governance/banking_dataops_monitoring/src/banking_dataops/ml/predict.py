#!/usr/bin/env python3
"""Score the current batch and write the investigation queue to ``gold.predictions``.

A prediction never changes the publication decision. Blocking belongs to the
deterministic control layer (ADR-0002, refined by ADR-0008): a decision that must
stand before an auditor has to name the rule it applied. The model only orders the
work of the analyst who investigates.

CHOIX
-----
Retained:

- Three priority bands defined from the decision threshold stored in the model
  artefact: ``high`` from the threshold upwards (the transaction is predicted
  suspicious, investigate first), ``medium`` from ``MEDIUM_BAND_FLOOR`` times the
  threshold up to the threshold (grey zone), ``low`` below. A retrain that moves
  the threshold moves both boundaries with it (ADR-0009).
- Every run truncates ``gold.predictions`` and reinserts the whole queue in one
  transaction. The table is derived, it holds no state worth preserving.
- The row carries the model version, the exact decision threshold in force and the
  run identifier, so a score read months later can be traced to its artefact.

Rejected:

- The previous rule, ``high`` at 1.30 times the threshold. With a threshold of
  about 0.83 the boundary exceeded 1, so no transaction could ever be ``high``
  (evidence/rejeu_modele_2026-07-27.json: 0 high, 227 medium, 1,273 low). A
  multiplier cannot be right for every threshold; a rule anchored on the threshold
  itself is.
- Bands defined on ranks (top 10 %, next 20 %). They would answer an analyst's
  capacity, not the model's confidence, and would label 10 % of transactions
  ``high`` even in a batch where nothing looks suspicious.
- UPSERT instead of truncate: it would keep stale scores for transactions that left
  the batch. Source : inconnue (no measurement of that case).

OÙ
---
Role: the scoring step that follows ``banking_dataops.ml.train``. Callers:
``python -m banking_dataops.ml.predict`` (Makefile target ``ml-predict``) and the
alias ``src/ml/predict.py``. ``run_pipeline.py`` does NOT call it: ``--train`` trains
but leaves ``gold.predictions`` untouched (README, "make ml-predict").
Depends on: ``banking_dataops.ml.features`` (same transformation as training, to avoid
training/serving skew), ``banking_dataops.db`` (connection), ``banking_dataops.audit``,
``banking_dataops.logging_config``.
Read by: the dashboard and ``monitoring.load_prediction_summary`` through
``gold.vw_prediction_summary``, ``scripts/smoke_check.py``, the Power BI views.

COMMENT
-------
1. ``load_model`` reads the artefact; a missing file raises with the command that
   produces it.
2. The Silver rows are extracted and turned into features by the same functions
   the training used; an empty batch and a score count different from the row count
   raise (``assert_invariant``).
3. ``priority_band`` maps each score to a band; it refuses a score outside [0, 1]
   (including NaN) and a threshold outside (0, 1].
4. One transaction truncates and refills ``gold.predictions``.
Tests that prove it: tests/test_ml.py
(``test_the_bands_follow_the_threshold_every_band_is_reachable_and_an_invalid_score_or_threshold_is_refused``),
tests/test_ml_predict.py
(``test_predict_writes_a_consistent_rounded_queue_and_refuses_what_it_cannot_score_leaving_the_table_as_it_was``).

POURQUOI
--------
- A band rule tied to the threshold, with ``high`` equal to "predicted suspicious":
  ADR-0009 (decision 2) and the measured defect of the previous rule in
  evidence/rejeu_modele_2026-07-27.json (``bandes_1500``).
- ``MEDIUM_BAND_FLOOR = 0.60``: kept from the previous version, which named it
  ``LOW_BAND_MULTIPLIER``. On the 27 July 2026 test rows a floor of 0.60 times the
  threshold gave a recall of 0.90 at a precision of 0.43
  (evidence/rejeu_modele_2026-07-27.json, ``au_seuil_bas_test``). The value 0.60
  itself was chosen by the project, source : inconnue.
- No score is allowed to fall silently into ``low``: a NaN compares false against
  every boundary, so without the check it would be labelled the lowest priority
  (docs/CODE_STANDARD.md, section 7, "Échecs").
- A prediction never blocks: ADR-0002, section "Suite donnée".

JOURNALISATION
--------------
``RunJournal("ml_predict")`` records the run start and end (category ``pipeline``)
and the event ``predictions_written`` (category ``data``) with the row count, the
model version, the threshold and the number of rows in each band, plus the two
invariants (category ``integrity``, successes included). Events go to the
structured log at INFO and, best effort, to ``audit_events``. A missing model or a
violated invariant raises and the run is closed as failed. Only ``main()`` prints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import joblib

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings
from banking_dataops.db import connect
from banking_dataops.logging_config import current_run_id, get_logger
from banking_dataops.ml import features as F

logger = get_logger("ml.predict")

ROOT = Path(__file__).resolve().parents[3]
MODEL_PATH = ROOT / "models" / "suspicion_classifier.joblib"

#: Lower edge of the ``medium`` band, as a fraction of the decision threshold. The
#: upper edge is the threshold itself. Relative to the threshold, so that a retrain
#: moves the band with it instead of leaving it stranded at an absolute value.
MEDIUM_BAND_FLOOR = 0.60

#: The three values ``gold.predictions.priority_band`` accepts (CHECK constraint in
#: sql/08_gold_layer.sql), highest priority first.
BANDS = ("high", "medium", "low")


def priority_band(score: float, threshold: float) -> str:
    """Map a probability to an investigation priority.

    - ``high``: ``score >= threshold``. The model predicts the transaction
      suspicious; investigate these first. This is exactly ``predicted_suspicious``.
    - ``medium``: ``MEDIUM_BAND_FLOOR * threshold <= score < threshold``. Grey zone,
      worth a look once the ``high`` items are done.
    - ``low``: below that.

    Raises ``ValueError`` for a score outside [0, 1] or NaN, and for a threshold
    outside (0, 1]: a NaN would otherwise compare false against both boundaries and
    be filed as ``low``, the quietest possible mistake.
    """
    if not (isinstance(score, int | float) and 0.0 <= score <= 1.0):
        raise ValueError(f"Score {score!r} is not a probability in [0, 1]; refusing to assign a priority band.")
    if not (isinstance(threshold, int | float) and 0.0 < threshold <= 1.0):
        raise ValueError(
            f"Threshold {threshold!r} is outside (0, 1]; the model artefact is corrupt, retrain with "
            "`python -m banking_dataops.ml.train`."
        )
    if score >= threshold:
        return "high"
    if score >= threshold * MEDIUM_BAND_FLOOR:
        return "medium"
    return "low"


def load_model(path: Path | None = None) -> dict[str, Any]:
    """Load the persisted artefact.

    Fails with an actionable message rather than a ``FileNotFoundError``: the caller
    is usually a pipeline, and "train it first" is more useful than a stack trace.
    """
    target = path or MODEL_PATH
    if not target.exists():
        raise RuntimeError(
            f"No model at {target}. Train one first: `docker compose exec app python -m banking_dataops.ml.train`."
        )
    artefact: dict[str, Any] = joblib.load(target)
    return artefact


def predict(settings: Settings | None = None, model_path: Path | None = None) -> int:
    """Score every transaction and persist the queue. Returns the row count.

    All Silver transactions are scored, including those the model was trained on.
    Only the held-out rows measured at training time say anything about performance
    (docs/adr/0008-classifieur-de-priorisation.md); the queue is a work list, not a
    benchmark.
    """
    with RunJournal("ml_predict", settings=settings) as run:
        artefact = load_model(model_path)
        pipeline = artefact["pipeline"]
        threshold = float(artefact["threshold"])
        report = artefact["report"]
        version = str(report["model_version"])
        family = str(report.get("model_family", "unknown"))

        raw = F.extract(settings=settings)
        assert_invariant(
            len(raw) > 0,
            invariant="there is something to score",
            reason="scoring an empty batch would silently produce an empty queue",
            rows=len(raw),
        )

        frame = F.build(raw)
        X, _ = F.split_x_y(frame)
        scores = pipeline.predict_proba(X)[:, 1]
        # The scorer must return one probability per row, in order. A length
        # mismatch would silently shift every score onto the wrong transaction.
        assert_invariant(
            len(scores) == len(frame),
            invariant="one score per transaction, in order",
            reason=(
                "a length mismatch would attach each score to the wrong "
                "transaction and send analysts to investigate innocent rows"
            ),
            rows=len(frame),
            scores=len(scores),
        )

        run_id = current_run_id()
        payload = [
            (
                str(frame.iloc[i]["transaction_id"]),
                round(float(scores[i]), 5),
                bool(scores[i] >= threshold),
                round(threshold, 3),
                priority_band(float(scores[i]), threshold),
                "suspicion_classifier",
                version,
                run_id,
            )
            for i in range(len(frame))
        ]

        with connect(settings) as connection:
            with connection.cursor() as cursor:
                # Idempotent: the table is derived, it has no state to preserve.
                cursor.execute("TRUNCATE TABLE gold.predictions")
                cursor.executemany(
                    """
                    INSERT INTO gold.predictions
                        (transaction_id, suspicion_score, predicted_suspicious,
                         decision_threshold, priority_band, model_name, model_version, run_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    payload,
                )
            connection.commit()

        flagged = sum(1 for row in payload if row[2])
        per_band = {band: sum(1 for row in payload if row[4] == band) for band in BANDS}
        run.note(
            "predictions_written",
            reason=(
                "the investigation queue is refreshed; it prioritises analyst attention and never blocks publication"
            ),
            row_count=len(payload),
            model_family=family,
            model_version=version,
            threshold=round(threshold, 3),
            flagged=flagged,
            high_priority=per_band["high"],
            medium_priority=per_band["medium"],
            low_priority=per_band["low"],
        )

        return len(payload)


def main() -> None:
    """CLI entry point: score the batch and print the row count."""
    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    count = predict()
    print(f"{count} transaction(s) scored into gold.predictions.")


if __name__ == "__main__":
    main()
