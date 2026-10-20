#!/usr/bin/env python3
"""Train the suspicion classifier, choose its decision threshold and persist it.

The classifier orders an investigation queue; it never blocks a publication. The
blocking verdict stays with the deterministic controls (ADR-0002, refined by
ADR-0008). This module is where the model is built, so it is also where the
rules that keep its figures honest live: selection and threshold are decided on
the training set only, and the test set is read once, at the very end (ADR-0009).

CHOIX
-----
Retained:

- Two candidates compared on the same stratified 75 / 25 split (seed 42): a
  logistic regression, readable by an analyst, and a random forest tuned by grid
  search. The forest is shipped only if its cross-validated ROC AUC beats the
  baseline by ``SELECTION_MARGIN`` (0.01); otherwise the simpler model wins.
- The comparison uses stratified 4-fold cross-validation on the training rows
  only. The decision threshold maximises F1 on the out-of-fold predictions of
  the same training rows. The test rows are scored once, after both choices are
  frozen, to report the figures.
- Both the logistic regression and the forest are scored on the test rows in
  that final step. The forest's test AUC is reported for information only; no
  decision reads it.

Rejected:

- Choosing the model and the threshold on the test split, the previous behaviour.
  The same 375 rows then picked the model, picked the threshold and announced
  the performance, so the announced AUC was not independent of the selection
  (ADR-0008, refinements of October 2026; ADR-0009).
- A separate validation split. With about 91 positives in the training rows
  (121 in total, 30 of them in the test split, per
  evidence/rejeu_modele_2026-07-27.json) a fixed validation split would hold
  fewer than 20 positives; cross-validation reuses every training row.
- Nested cross-validation. The forest's best cross-validated score is the best of
  12 combinations and is therefore slightly optimistic. The rule errs on the
  forest's side, so when the baseline still wins the decision is conservative;
  nested cross-validation was judged disproportionate for 1,125 rows. Source :
  inconnue (the size of the optimism was not measured here).

OÙ
---
Role: the model step of the pipeline (Bronze, Silver, Gold, then model).
Callers: ``python -m banking_dataops.ml.train`` (Makefile target ``ml-train``),
``run_pipeline.py --train``, and the alias ``src/ml/train.py``. It is never run
silently on every pipeline execution: retraining is a deliberate act.
Depends on: ``banking_dataops.ml.features`` (extraction, feature building, leak
guard), ``banking_dataops.audit`` (``RunJournal``, ``assert_invariant``),
``banking_dataops.logging_config``.
Produces: ``suspicion_classifier.joblib`` (fitted pipeline, threshold,
report) and ``metrics.json`` in the ``models`` directory; read by ``banking_dataops.ml.predict``, by
``banking_dataops.monitoring.load_model_metrics`` and by the dashboard.

COMMENT
-------
1. Extract the Silver rows, build the features, refuse a training set under 200
   rows or with a degenerate positive rate (``assert_invariant``).
2. Split 75 / 25, stratified, seed 42 (``train_test_split``).
3. ``select_model``: cross-validate the baseline, grid-search the forest with the
   same folds, apply the margin rule, fit both on the whole training set.
4. ``out_of_fold_threshold``: out-of-fold probabilities of the selected model on
   the training set, then the threshold with the best F1 (``_choose_threshold``).
5. ``evaluate_once``: score the test rows, report AUC, precision, recall, F1 and
   the confusion matrix at that threshold.
6. Refuse to publish a model under ``MIN_ACCEPTABLE_AUC``; write the artefact and
   the report.
Tests that prove it: tests/test_ml_training.py
(``test_the_choices_use_the_training_rows_only_follow_the_margin_rule_and_the_threshold_is_out_of_fold``,
``test_train_writes_a_consistent_artefact_and_publishes_nothing_for_a_bad_label_a_small_set_or_a_weak_model``).

POURQUOI
--------
- Selection and threshold outside the test set: ADR-0009, which supersedes the
  "Le seuil et le choix du modèle sont faits sur le jeu de test" limitation of
  docs/adr/0008-classifieur-de-priorisation.md.
- A margin of 0.01 AUC: a gain of a few thousandths on one split is noise, and an
  analyst can read coefficients but not 300 trees (ADR-0008, "Sélection du
  modèle"). The value 0.01 itself is a project choice, source : inconnue.
- Stratified folds: with 8.07 % positives a plain split can leave a fold almost
  empty of them (evidence/rejeu_modele_2026-07-27.json, ``taux_positifs``).
- F1 as the threshold criterion: neutral between a missed investigation and
  wasted analyst time; no cost matrix exists to do better (docs/KPI_DEFINITIONS.md).
- Floor of 0.70 on the test AUC: a model at chance would send analysts to
  investigate randomly while displaying a confidence score (ADR-0008, "Garde-fou").

JOURNALISATION
--------------
Through ``RunJournal("ml_train")`` and ``record_event``: the run start and end
(category ``pipeline``), then the events ``baseline_trained``, ``model_selected``,
``threshold_selected``, ``model_evaluated`` and ``model_persisted`` (category
``data``) carrying the cross-validation scores, the threshold and the test
figures, and the invariant checks (category ``integrity``, successes included).
Each event goes to the structured log at INFO and, best effort, to the
``audit_events`` table. A failed invariant raises and the run is closed as
failed. ``main()`` prints a human summary; the library functions never print.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import (
    GridSearchCV,
    StratifiedKFold,
    cross_val_predict,
    cross_val_score,
    train_test_split,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings
from banking_dataops.logging_config import get_logger
from banking_dataops.ml import features as F

logger = get_logger("ml.train")

ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = ROOT / "models"
MODEL_PATH = MODEL_DIR / "suspicion_classifier.joblib"
METRICS_PATH = MODEL_DIR / "metrics.json"

#: Fixed seed everywhere a random choice is made. Without it two training runs
#: on identical data produce different models, and no result can be compared.
RANDOM_STATE = 42

#: Minimum ROC AUC on the test rows below which the model is not published. A
#: model no better than chance must not reach gold.predictions and give false
#: confidence.
MIN_ACCEPTABLE_AUC = 0.70

#: Number of stratified folds used both to compare the candidates and to produce
#: the out-of-fold predictions the threshold is chosen on. Four folds keep about
#: 22 positives per validation fold on this dataset.
CV_FOLDS = 4

#: The forest replaces the baseline only if its cross-validated ROC AUC is at
#: least this much higher. See the module docstring, section POURQUOI.
SELECTION_MARGIN = 0.01

#: Criterion used to choose the decision threshold. Written down, and stored in
#: the report, so that the choice is explicit rather than implied by the code.
THRESHOLD_CRITERION = "max_f1"

#: Decimals kept for the decision threshold, matching ``NUMERIC(4, 3)`` of
#: ``gold.predictions.decision_threshold`` (sql/08_gold_layer.sql).
THRESHOLD_DECIMALS = 3

#: Where the threshold is computed. Stored in the report next to the criterion.
THRESHOLD_SOURCE = "out-of-fold predictions on the training set"

#: Hyper-parameter grid of the random forest (12 combinations).
FOREST_PARAM_GRID: dict[str, list[object]] = {
    "model__n_estimators": [150, 300],
    "model__max_depth": [8, 14, None],
    "model__min_samples_leaf": [1, 5],
}

CONFUSION_LABELS = ("true_negative", "false_positive", "false_negative", "true_positive")


@dataclass
class TrainingReport:
    """Everything needed to judge, reproduce and defend a training run.

    The ``baseline_cv_*`` and ``forest_cv_*`` fields are the figures the selection
    rests on (training rows only). The ``*_test_*`` fields and the fields without a
    prefix come from the single final evaluation on the test rows.
    """

    model_name: str
    model_family: str
    model_version: str
    trained_at: str
    rows_total: int
    rows_train: int
    rows_test: int
    positive_rate: float
    best_params: dict[str, object]
    cv_folds: int
    baseline_cv_roc_auc: float
    forest_cv_roc_auc: float
    selection_margin: float
    selection_reason: str
    decision_threshold: float
    threshold_criterion: str
    threshold_source: str
    threshold_oof_f1: float
    roc_auc: float
    average_precision: float
    precision: float
    recall: float
    f1: float
    confusion: dict[str, int]
    baseline_test_roc_auc: float
    forest_test_roc_auc: float
    top_features: list[dict[str, object]]
    random_state: int = RANDOM_STATE


@dataclass
class Selection:
    """Outcome of ``select_model``: the chosen family and both fitted candidates."""

    family: str
    rejected: str
    estimator: Pipeline
    baseline: Pipeline
    forest: Pipeline
    best_params: dict[str, object]
    baseline_cv_roc_auc: float
    forest_cv_roc_auc: float
    reason: str


@dataclass
class FinalEvaluation:
    """Figures of the single evaluation on the test rows."""

    roc_auc: float
    average_precision: float
    precision: float
    recall: float
    f1: float
    confusion: dict[str, int]
    baseline_test_roc_auc: float
    forest_test_roc_auc: float


def _cv() -> StratifiedKFold:
    """Return the cross-validation splitter shared by every selection step.

    One splitter, one seed: the baseline score, the forest score and the
    out-of-fold predictions are computed on identical folds, so they are
    comparable and reproducible.
    """
    return StratifiedKFold(n_splits=CV_FOLDS, shuffle=True, random_state=RANDOM_STATE)


def _build_estimator(kind: str) -> Pipeline:
    """Assemble preprocessing and estimator into a single pipeline.

    A Pipeline rather than a transformation applied beforehand: the scaler and
    the encoder are fitted on the training fold only. Fitting them on the whole
    dataset would leak the distribution of the held-out rows into training, a
    subtle and common flaw that inflates every benchmark.
    """
    preprocessor = ColumnTransformer(
        transformers=[
            ("num", StandardScaler(), list(F.NUMERIC_FEATURES)),
            (
                "cat",
                # A category unseen in training must not crash scoring in
                # production: it becomes an all-zero vector.
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                list(F.CATEGORICAL_FEATURES),
            ),
            ("bool", "passthrough", list(F.BOOLEAN_FEATURES)),
        ],
        remainder="drop",
    )

    # Class weights compensate for the 8 % positive rate without resampling, which
    # would change the data the cross-validation folds are drawn from.
    if kind == "baseline":
        estimator = LogisticRegression(max_iter=1000, class_weight="balanced", random_state=RANDOM_STATE)
    elif kind == "forest":
        estimator = RandomForestClassifier(class_weight="balanced_subsample", random_state=RANDOM_STATE, n_jobs=-1)
    else:
        raise ValueError(f"Unknown estimator kind {kind!r}: expected 'baseline' or 'forest'.")

    return Pipeline([("prep", preprocessor), ("model", estimator)])


def _choose_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    """Return the score threshold that maximises F1.

    The criterion is F1 (``THRESHOLD_CRITERION``). It is not 0.5: that default
    assumes balanced classes and equal error costs, and neither holds here. A
    false negative is a missed investigation, a false positive is wasted analyst
    time, and no cost matrix exists to weigh them, so F1 is the neutral
    compromise. Among thresholds with the same F1 the lowest one is returned.
    The threshold is stored with every prediction so that a past decision stays
    readable after a retune.

    The caller decides which scores are passed. They must never be scores of the
    test rows (ADR-0009).
    """
    precision, recall, thresholds = precision_recall_curve(y_true, scores)
    f1 = np.zeros_like(precision)
    np.divide(2 * precision * recall, precision + recall, out=f1, where=(precision + recall) > 0)
    if not len(thresholds):
        return 0.5
    # precision_recall_curve returns one more (precision, recall) point than
    # thresholds: the last point is the "predict nothing" end of the curve.
    return float(thresholds[int(np.argmax(f1[:-1]))])


def _feature_names(pipeline: Pipeline) -> list[str]:
    """Return the expanded feature names after one-hot encoding."""
    return [str(name) for name in pipeline.named_steps["prep"].get_feature_names_out()]


def _importances(pipeline: Pipeline, limit: int = 12) -> list[dict[str, object]]:
    """Return the most influential features, largest first.

    An analyst who receives a flagged transaction must be told which factors drove
    the score, otherwise the queue is a black box and gets ignored. For the
    logistic regression the weight is the absolute coefficient: it says how
    strongly a feature moves the score, not in which direction, and it is not a
    proof of causality.
    """
    model = pipeline.named_steps["model"]
    names = _feature_names(pipeline)
    # The two branches differ in substance (impurity-based importances of a
    # forest versus coefficients of a linear model), so the explicit form reads
    # better than a conditional expression.
    if hasattr(model, "feature_importances_"):  # noqa: SIM108
        weights = model.feature_importances_
    else:
        weights = np.abs(model.coef_[0])

    order = np.argsort(weights)[::-1][:limit]
    return [{"feature": names[i], "importance": round(float(weights[i]), 5)} for i in order]


def select_model(X_train: pd.DataFrame, y_train: pd.Series) -> Selection:
    """Choose between the baseline and the forest using the training rows only.

    The signature is the guarantee: this function receives no test data, so it
    cannot depend on it. Both candidates are scored by stratified cross-validation
    on identical folds. The forest is tuned by grid search inside those folds and
    replaces the baseline only if it leads by at least ``SELECTION_MARGIN``.

    The comparison is slightly tilted towards the forest: its score is the best
    of ``len(FOREST_PARAM_GRID)`` combinations, the baseline has no tuning. The
    tilt favours the complex model, so a win of the simple model is robust.

    Both candidates are then fitted on the whole training set, which the final
    evaluation needs in order to report them side by side.
    """
    cv = _cv()
    baseline = _build_estimator("baseline")
    baseline_cv = float(np.mean(cross_val_score(baseline, X_train, y_train, cv=cv, scoring="roc_auc")))

    search = GridSearchCV(
        _build_estimator("forest"),
        param_grid=FOREST_PARAM_GRID,
        scoring="roc_auc",
        cv=cv,
        n_jobs=-1,
        refit=True,
    )
    search.fit(X_train, y_train)
    forest: Pipeline = search.best_estimator_
    forest_cv = float(search.best_score_)

    baseline.fit(X_train, y_train)

    gap = forest_cv - baseline_cv
    if gap >= SELECTION_MARGIN:
        family, rejected, estimator = "random_forest", "logistic_regression", forest
        reason = (
            f"the tuned forest leads the baseline by {gap:.4f} cross-validated AUC on the "
            f"training rows, above the {SELECTION_MARGIN} margin that justifies the added opacity"
        )
    else:
        family, rejected, estimator = "logistic_regression", "random_forest", baseline
        reason = (
            f"the tuned forest gains only {gap:+.4f} cross-validated AUC over the baseline on "
            f"the training rows, below the {SELECTION_MARGIN} margin; the simpler and readable "
            "model is shipped instead"
        )

    return Selection(
        family=family,
        rejected=rejected,
        estimator=estimator,
        baseline=baseline,
        forest=forest,
        best_params={k: str(v) for k, v in search.best_params_.items()},
        baseline_cv_roc_auc=baseline_cv,
        forest_cv_roc_auc=forest_cv,
        reason=reason,
    )


def out_of_fold_threshold(selection: Selection, X_train: pd.DataFrame, y_train: pd.Series) -> tuple[float, float]:
    """Choose the decision threshold on out-of-fold predictions of the training rows.

    Each training row is scored by a copy of the selected model that never saw it,
    so the scores are honest, and the threshold is the one maximising F1 on them
    (``THRESHOLD_CRITERION``). The test rows play no part. The model is cloned and
    refitted per fold: the final artefact, fitted on the whole training set, is
    not the object that produced these scores, which is the point.

    Returns ``(threshold, f1_at_threshold)``; the second value is the F1 of the
    out-of-fold predictions at the returned (rounded) threshold, kept in the report for
    context.
    """
    unfitted = clone(selection.estimator)
    out_of_fold = cross_val_predict(unfitted, X_train, y_train, cv=_cv(), method="predict_proba")[:, 1]
    y_true = y_train.to_numpy()
    # Rounded to the three decimals the gold.predictions column stores, and used in that
    # rounded form everywhere (test evaluation, artefact, predictions): the threshold
    # printed next to a score is then exactly the one that classified it.
    threshold = round(_choose_threshold(y_true, out_of_fold), THRESHOLD_DECIMALS)
    f1_at_threshold = float(f1_score(y_true, out_of_fold >= threshold, zero_division=0))
    return threshold, f1_at_threshold


def evaluate_once(selection: Selection, threshold: float, X_test: pd.DataFrame, y_test: pd.Series) -> FinalEvaluation:
    """Score the test rows, once, with the model and threshold already frozen.

    This is the only place the test rows are used. Nothing returned here feeds back
    into a decision. The unselected candidate is scored as well, purely so the
    report can show both AUCs next to each other; it changes nothing.
    """
    scores = selection.estimator.predict_proba(X_test)[:, 1]
    predicted = scores >= threshold
    return FinalEvaluation(
        roc_auc=float(roc_auc_score(y_test, scores)),
        average_precision=float(average_precision_score(y_test, scores)),
        precision=float(precision_score(y_test, predicted, zero_division=0)),
        recall=float(recall_score(y_test, predicted, zero_division=0)),
        f1=float(f1_score(y_test, predicted, zero_division=0)),
        confusion=dict(
            zip(
                CONFUSION_LABELS,
                [int(v) for v in confusion_matrix(y_test, predicted, labels=[False, True]).ravel()],
                strict=True,
            )
        ),
        baseline_test_roc_auc=float(roc_auc_score(y_test, selection.baseline.predict_proba(X_test)[:, 1])),
        forest_test_roc_auc=float(roc_auc_score(y_test, selection.forest.predict_proba(X_test)[:, 1])),
    )


def train(settings: Settings | None = None, test_size: float = 0.25) -> TrainingReport:
    """Train, compare, evaluate and persist. Returns the report.

    The order of the steps is the contract: select, then choose the threshold, then
    evaluate on the test rows, then publish. Nothing computed from the test rows
    flows back into the first two.
    """
    with RunJournal("ml_train", seed=RANDOM_STATE, settings=settings) as run:
        raw = F.extract(settings=settings)
        assert_invariant(
            len(raw) >= 200,
            invariant="training set has at least 200 rows",
            reason=("below that, a stratified split leaves too few positives for the metrics to mean anything"),
            rows=len(raw),
        )

        frame = F.build(raw)
        X, y = F.split_x_y(frame)
        positive_rate = float(y.mean())
        assert_invariant(
            0.01 < positive_rate < 0.5,
            invariant="0.01 < positive rate < 0.5",
            reason=(
                "a degenerate class balance makes every metric misleading and "
                "usually signals a broken label rather than a hard problem"
            ),
            positive_rate=round(positive_rate, 4),
        )

        # Stratified: with 8 % positives a plain random split can leave the test
        # rows almost empty of them.
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=RANDOM_STATE, stratify=y
        )

        selection = select_model(X_train, y_train)
        run.note(
            "baseline_trained",
            reason="a linear reference everyone can read, to judge whether tuning earns its cost",
            row_count=len(X_train),
            cv_roc_auc=round(selection.baseline_cv_roc_auc, 4),
            cv_folds=CV_FOLDS,
        )
        run.note(
            "model_selected",
            reason=selection.reason,
            row_count=len(X_train),
            chosen=selection.family,
            rejected=selection.rejected,
            baseline_cv_roc_auc=round(selection.baseline_cv_roc_auc, 4),
            forest_cv_roc_auc=round(selection.forest_cv_roc_auc, 4),
            margin=SELECTION_MARGIN,
            data_used="training rows only",
        )

        threshold, threshold_f1 = out_of_fold_threshold(selection, X_train, y_train)
        run.note(
            "threshold_selected",
            reason=(
                f"{THRESHOLD_CRITERION} on {THRESHOLD_SOURCE}: the threshold is fixed before the test rows are scored"
            ),
            row_count=len(X_train),
            threshold=round(threshold, 4),
            oof_f1=round(threshold_f1, 4),
            criterion=THRESHOLD_CRITERION,
        )

        evaluation = evaluate_once(selection, threshold, X_test, y_test)
        run.note(
            "model_evaluated",
            reason="single evaluation on the test rows, after model and threshold were frozen",
            row_count=len(X_test),
            roc_auc=round(evaluation.roc_auc, 4),
            precision=round(evaluation.precision, 4),
            recall=round(evaluation.recall, 4),
            f1=round(evaluation.f1, 4),
            baseline_test_roc_auc=round(evaluation.baseline_test_roc_auc, 4),
            forest_test_roc_auc=round(evaluation.forest_test_roc_auc, 4),
        )

        report = TrainingReport(
            model_name="suspicion_classifier",
            model_family=selection.family,
            model_version=datetime.now(UTC).strftime("%Y%m%d-%H%M%S"),
            trained_at=datetime.now(UTC).isoformat(),
            rows_total=len(frame),
            rows_train=len(X_train),
            rows_test=len(X_test),
            positive_rate=round(positive_rate, 4),
            best_params=selection.best_params,
            cv_folds=CV_FOLDS,
            baseline_cv_roc_auc=round(selection.baseline_cv_roc_auc, 4),
            forest_cv_roc_auc=round(selection.forest_cv_roc_auc, 4),
            selection_margin=SELECTION_MARGIN,
            selection_reason=selection.reason,
            decision_threshold=threshold,
            threshold_criterion=THRESHOLD_CRITERION,
            threshold_source=THRESHOLD_SOURCE,
            threshold_oof_f1=round(threshold_f1, 4),
            roc_auc=round(evaluation.roc_auc, 4),
            average_precision=round(evaluation.average_precision, 4),
            precision=round(evaluation.precision, 4),
            recall=round(evaluation.recall, 4),
            f1=round(evaluation.f1, 4),
            confusion=evaluation.confusion,
            baseline_test_roc_auc=round(evaluation.baseline_test_roc_auc, 4),
            forest_test_roc_auc=round(evaluation.forest_test_roc_auc, 4),
            top_features=_importances(selection.estimator),
        )

        # A model no better than chance must not be published: it would give
        # false confidence to an analyst working the queue it produces.
        assert_invariant(
            report.roc_auc >= MIN_ACCEPTABLE_AUC,
            invariant=f"roc_auc >= {MIN_ACCEPTABLE_AUC}",
            reason=(
                "publishing a model at chance level would send analysts to "
                "investigate randomly while displaying a confidence score"
            ),
            roc_auc=report.roc_auc,
        )

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {"pipeline": selection.estimator, "threshold": threshold, "report": asdict(report)},
            MODEL_PATH,
        )
        METRICS_PATH.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")
        run.note(
            "model_persisted",
            reason="the artefact and its metrics are versioned so a verdict can be traced to a model",
            row_count=len(X_train),
            path=str(MODEL_PATH.relative_to(ROOT)),
            roc_auc=report.roc_auc,
        )

        return report


def main() -> None:
    """CLI entry point: train, then print a human summary."""
    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    report = train()
    print(f"\nModel      : {report.model_name} v{report.model_version} ({report.model_family})")
    print(f"Rows       : {report.rows_total} ({report.positive_rate:.1%} positive)")
    print(
        f"CV ROC AUC : baseline {report.baseline_cv_roc_auc}, forest {report.forest_cv_roc_auc} "
        f"({report.cv_folds}-fold on the training rows, used for the selection)"
    )
    print(f"Selection  : {report.selection_reason}")
    print(
        f"Test AUC   : {report.roc_auc}  (baseline {report.baseline_test_roc_auc}, "
        f"forest {report.forest_test_roc_auc}; reported once, not used for any decision)"
    )
    print(f"Precision  : {report.precision}   Recall: {report.recall}   F1: {report.f1}")
    print(f"Threshold  : {report.decision_threshold}  ({report.threshold_criterion} on {report.threshold_source})")
    print(f"Confusion  : {report.confusion}")
    print("\nTop factors:")
    for item in report.top_features[:6]:
        print(f"  {item['importance']:>8}  {item['feature']}")


if __name__ == "__main__":
    main()
