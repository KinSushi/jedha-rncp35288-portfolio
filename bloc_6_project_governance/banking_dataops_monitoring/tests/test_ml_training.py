"""Tests for model selection, threshold choice and the single final evaluation.

What they pin down is ADR-0009: the model and the decision threshold are chosen from
the training rows only, and the test rows are scored once, afterwards. The strongest
test is the first one, ``test_the_choices_use_the_training_rows_only_...``: it corrupts the features of the test
rows and checks that nothing but the final figures moves.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.base import clone
from sklearn.model_selection import StratifiedKFold, cross_val_predict, train_test_split

from banking_dataops.ml import features as F
from banking_dataops.ml import train as T
from banking_dataops.ml.train import _choose_threshold

pytestmark = [
    pytest.mark.limit(120),
    pytest.mark.usefixtures("no_audit_database", "small_forest_grid"),
]


def _split(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """Split a raw frame exactly as ``train()`` does."""
    X, y = F.split_x_y(F.build(raw))
    return train_test_split(X, y, test_size=0.25, random_state=T.RANDOM_STATE, stratify=y)


# ---------------------------------------------------------------------------
# Selection and threshold ignore the test rows
# ---------------------------------------------------------------------------


DECISION_FIELDS = (
    "model_family",
    "cv_folds",
    "baseline_cv_roc_auc",
    "forest_cv_roc_auc",
    "selection_reason",
    "decision_threshold",
    "threshold_oof_f1",
    "best_params",
)


def test_the_choices_use_the_training_rows_only_follow_the_margin_rule_and_the_threshold_is_out_of_fold(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: the test rows leaking into the choice of model or threshold.

    The features of the test rows are replaced by absurd values. The split depends only on the labels, so the
    same rows stay in the test set. Every decision must be unchanged; only the figures measured on those rows
    may move. By signature too: a function that receives no test rows cannot read them.
    """
    assert list(inspect.signature(T.select_model).parameters) == ["X_train", "y_train"]
    assert list(inspect.signature(T.out_of_fold_threshold).parameters) == ["selection", "X_train", "y_train"]
    assert list(inspect.signature(T.evaluate_once).parameters) == ["selection", "threshold", "X_test", "y_test"]

    _, X_test, _, _ = _split(raw_frame)
    corrupted = raw_frame.copy()
    corrupted.loc[X_test.index, "channel"] = "api"
    corrupted.loc[X_test.index, "amount_chf"] = 3.0
    corrupted.loc[X_test.index, "transaction_country"] = "AT"

    extract_returns(raw_frame)
    reference = T.train()
    extract_returns(corrupted)
    perturbed = T.train()

    for field in DECISION_FIELDS:
        assert getattr(reference, field) == getattr(perturbed, field), field
    assert reference.roc_auc != perturbed.roc_auc, "the final evaluation must see the test rows"

    # The forest is chosen only if it leads the baseline by the margin: impossible, then certain.
    X_train, _, y_train, _ = _split(raw_frame)
    for margin, family in [(10.0, "logistic_regression"), (-10.0, "random_forest")]:
        with monkeypatch.context() as patch:
            patch.setattr(T, "SELECTION_MARGIN", margin)
            selection = T.select_model(X_train, y_train)
        assert selection.family == family
        assert selection.rejected != family
        expected_gap = selection.forest_cv_roc_auc - selection.baseline_cv_roc_auc
        assert (expected_gap >= margin) == (family == "random_forest")

    # The comparison is fair only if both models see the same folds and the same rows.
    selection = T.select_model(X_train, y_train)
    assert 0.5 < selection.baseline_cv_roc_auc <= 1.0
    assert 0.5 < selection.forest_cv_roc_auc <= 1.0
    assert selection.best_params.keys() == T.FOREST_PARAM_GRID.keys()

    # On a hand-checked example the F1-optimal threshold is 0.35 (F1 = 0.8); on the training rows it is the
    # F1-optimal one on out-of-fold scores, rounded to 3 decimals.
    assert _choose_threshold(np.array([0, 0, 1, 1]), np.array([0.1, 0.4, 0.35, 0.8])) == pytest.approx(0.35)
    threshold, f1_at_threshold = T.out_of_fold_threshold(selection, X_train, y_train)
    folds = StratifiedKFold(n_splits=T.CV_FOLDS, shuffle=True, random_state=T.RANDOM_STATE)
    out_of_fold = cross_val_predict(clone(selection.estimator), X_train, y_train, cv=folds, method="predict_proba")[
        :, 1
    ]
    assert threshold == round(_choose_threshold(y_train.to_numpy(), out_of_fold), 3)
    assert 0.0 < threshold <= 1.0
    assert 0.0 < f1_at_threshold <= 1.0


def test_train_writes_a_consistent_artefact_and_publishes_nothing_for_a_bad_label_a_small_set_or_a_weak_model(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The report, ``metrics.json`` and the joblib artefact tell the same story; nothing is published otherwise."""
    extract_returns(raw_frame)
    report = T.train()

    metrics = json.loads(T.METRICS_PATH.read_text(encoding="utf-8"))
    artefact = joblib.load(T.MODEL_PATH)
    assert metrics == artefact["report"]
    assert artefact["threshold"] == report.decision_threshold
    assert report.rows_train + report.rows_test == report.rows_total
    assert sum(report.confusion.values()) == report.rows_test
    assert sorted(report.confusion) == sorted(T.CONFUSION_LABELS)
    assert report.threshold_criterion == "max_f1"
    assert report.threshold_source == T.THRESHOLD_SOURCE
    assert report.model_family in {"logistic_regression", "random_forest"}

    # The refusals leave the published files exactly as the accepted run left them.
    artefact_before = T.MODEL_PATH.read_bytes()
    metrics_before = T.METRICS_PATH.read_bytes()

    # FORBIDDEN PATH: training when no row is suspicious. A broken label must stop the run.
    no_positive = raw_frame.copy()
    no_positive["is_suspicious"] = False
    extract_returns(no_positive)
    with pytest.raises(AssertionError, match="positive rate"):
        T.train()

    # FORBIDDEN PATH: too few rows for a stratified split to leave a usable positive count.
    extract_returns(raw_frame.head(150))
    with pytest.raises(AssertionError, match="at least 200 rows"):
        T.train()

    # FORBIDDEN PATH: publishing a model that fails the AUC floor.
    extract_returns(raw_frame)
    monkeypatch.setattr(T, "MIN_ACCEPTABLE_AUC", 1.01)
    with pytest.raises(AssertionError, match="roc_auc"):
        T.train()

    assert T.MODEL_PATH.read_bytes() == artefact_before
    assert T.METRICS_PATH.read_bytes() == metrics_before
