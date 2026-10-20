"""Arguments and edges of the training helpers (``banking_dataops.ml.train``) that the report tests cannot see.

``tests/test_ml_training_contract.py`` recomputes the report and compares the journal with it. These tests
pin what only a spy can see (the exact arguments given to the grid search), the exact boundary of the
selection margin, the refusals of the helpers, and the metrics that must stay silent on a degenerate input.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline

from banking_dataops.ml import features as F
from banking_dataops.ml import train as T
from banking_dataops.ml.train import _build_estimator, _choose_threshold, out_of_fold_threshold, select_model

pytestmark = pytest.mark.limit(120)


class _StubSearch:
    """Stands in for ``GridSearchCV``: records how it was built and pretends to have found a best score."""

    built: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    best_score = 0.5

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        type(self).built.append((args, kwargs))
        self.best_estimator_ = args[0]
        self.best_score_ = type(self).best_score
        self.best_params_ = {"model__n_estimators": 15}

    def fit(self, X: pd.DataFrame, y: pd.Series) -> _StubSearch:
        return self


@pytest.fixture
def stub_search(monkeypatch: pytest.MonkeyPatch) -> type[_StubSearch]:
    _StubSearch.built = []
    _StubSearch.best_score = 0.5
    monkeypatch.setattr(T, "GridSearchCV", _StubSearch)
    return _StubSearch


@pytest.fixture
def training_rows(raw_frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    return F.split_x_y(F.build(raw_frame))


def test_the_two_kinds_build_their_models_the_selection_margin_is_exact_and_an_unknown_kind_is_refused(
    monkeypatch: pytest.MonkeyPatch, stub_search: type[_StubSearch], training_rows: tuple[pd.DataFrame, pd.Series]
) -> None:
    X, y = training_rows

    assert isinstance(_build_estimator("baseline").named_steps["model"], LogisticRegression)
    assert isinstance(_build_estimator("forest").named_steps["model"], RandomForestClassifier)
    # The preprocessing drops every column that is not a declared feature.
    assert _build_estimator("baseline").named_steps["prep"].remainder == "drop"

    # The grid search gets a forest, the grid, the ROC AUC, the shared folds and every core.
    select_model(X, y)
    ((args, kwargs),) = stub_search.built
    assert len(args) == 1
    assert isinstance(args[0], Pipeline)
    assert isinstance(args[0].named_steps["model"], RandomForestClassifier)
    assert kwargs["param_grid"] is T.FOREST_PARAM_GRID
    assert kwargs["scoring"] == "roc_auc"
    assert kwargs["n_jobs"] == -1
    assert kwargs["refit"] is True
    folds = kwargs["cv"]
    assert isinstance(folds, StratifiedKFold)
    assert (folds.n_splits, folds.shuffle, folds.random_state) == (4, True, 42)
    assert sorted(kwargs) == ["cv", "n_jobs", "param_grid", "refit", "scoring"]

    # The search result is reported as text parameters.
    assert select_model(X, y).best_params == {"model__n_estimators": "15"}

    # The forest is chosen when its lead equals the margin exactly...
    monkeypatch.setattr(T, "cross_val_score", lambda *a, **k: np.zeros(4))
    stub_search.best_score = 0.01
    selection = select_model(X, y)
    assert (selection.baseline_cv_roc_auc, selection.forest_cv_roc_auc) == (0.0, 0.01)
    assert selection.family == "random_forest"
    assert selection.rejected == "logistic_regression"
    assert selection.reason == (
        "the tuned forest leads the baseline by 0.0100 cross-validated AUC on the training rows, above the 0.01 "
        "margin that justifies the added opacity"
    )

    # ...and the baseline is kept when the forest lead is just under the margin.
    stub_search.best_score = 0.0099
    kept = select_model(X, y)
    assert kept.family == "logistic_regression"
    assert kept.estimator is kept.baseline
    assert kept.reason.startswith("the tuned forest gains only +0.0099 cross-validated AUC")

    # FORBIDDEN PATH: a typo in the kind silently selecting the heavy, opaque model.
    for kind in ("neural_net", "Forest", "FOREST", "XXforestXX", ""):
        with pytest.raises(ValueError, match="Unknown estimator kind .*: expected 'baseline' or 'forest'."):
            _build_estimator(kind)


class _ZoneMinusFiveClock(datetime):
    """A clock on a machine set to UTC-5: ``now()`` without a zone is the local time, ``now(UTC)`` the real one."""

    @classmethod
    def now(cls, tz: Any = None) -> Any:
        instant = datetime(2026, 7, 27, 14, 14, 5, tzinfo=UTC)
        if tz is None:
            return instant.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
        return instant.astimezone(tz)


def _stub_selection(X_train: pd.DataFrame, y_train: pd.Series) -> T.Selection:
    fitted = _build_estimator("baseline").fit(X_train, y_train)
    return T.Selection(
        family="logistic_regression",
        rejected="random_forest",
        estimator=fitted,
        baseline=fitted,
        forest=fitted,
        best_params={},
        baseline_cv_roc_auc=0.9,
        forest_cv_roc_auc=0.9,
        reason="stub",
    )


def _stub_evaluation(roc_auc: float) -> T.FinalEvaluation:
    return T.FinalEvaluation(
        roc_auc=roc_auc,
        average_precision=0.5,
        precision=0.5,
        recall=0.5,
        f1=0.5,
        confusion={"true_negative": 1, "false_positive": 1, "false_negative": 1, "true_positive": 1},
        baseline_test_roc_auc=roc_auc,
        forest_test_roc_auc=roc_auc,
    )


def test_the_metrics_stay_silent_on_degenerate_input_and_a_model_below_the_floor_is_refused_leaving_the_files_unchanged(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No positive row and no flagged row: the F1 is undefined; the code says 0 and says it silently.
    with monkeypatch.context() as patch:
        patch.setattr(T, "cross_val_predict", lambda *a, **k: np.zeros((6, 2)))
        patch.setattr(T, "_choose_threshold", lambda y_true, scores: 0.5)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            degenerate = out_of_fold_threshold(
                SimpleNamespace(estimator=LogisticRegression()),  # type: ignore[arg-type]
                pd.DataFrame({"x": range(6)}),
                pd.Series([False] * 6),
            )
    assert degenerate == (0.5, 0.0)

    # The only warnings left are about the ROC AUC, which no one can compute on a single class. Recall and F1 are
    # undefined when no row is positive; the code gives them the value 0 on purpose (``zero_division=0``), so that
    # no warning mentions them and no figure of 1 flatters an empty case.
    class Scorer:
        def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
            return np.tile([0.9, 0.1], (len(X), 1))

    selection = SimpleNamespace(estimator=Scorer(), baseline=Scorer(), forest=Scorer())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        evaluation = T.evaluate_once(selection, 0.5, pd.DataFrame({"x": range(4)}), pd.Series([False] * 4))  # type: ignore[arg-type]
    assert (evaluation.precision, evaluation.recall, evaluation.f1) == (0.0, 0.0, 0.0)
    assert evaluation.confusion == {
        "true_negative": 4,
        "false_positive": 0,
        "false_negative": 0,
        "true_positive": 0,
    }
    mentioned = [str(w.message) for w in caught if any(k in str(w.message) for k in ("Recall", "F-score", "Precision"))]
    assert mentioned == []

    # Scores 0.1 ... 0.9: the best F1 is at 0.5, in the middle of the curve.
    y_true = np.array([False, False, False, False, True, True, True, False, True])
    scores = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9])
    assert _choose_threshold(y_true, scores) == 0.5

    # Scores 0.1, 0.2, 0.6, 0.9 for rows F, F, T, T: the best threshold is 0.6, and 0.6 itself must be flagged.
    out_of_fold = np.array([[0.9, 0.1], [0.8, 0.2], [0.4, 0.6], [0.1, 0.9]])
    with monkeypatch.context() as patch:
        patch.setattr(T, "cross_val_predict", lambda *a, **k: out_of_fold)
        flagged = out_of_fold_threshold(
            SimpleNamespace(estimator=LogisticRegression()),  # type: ignore[arg-type]
            pd.DataFrame({"x": range(4)}),
            pd.Series([False, False, True, True]),
        )
    assert flagged == (0.6, 1.0)

    # The version number and the training time are stamped in UTC whatever the machine zone, and the metrics file
    # is written explicitly as UTF-8 (the platform default is not UTF-8 everywhere).
    monkeypatch.setattr(T, "datetime", _ZoneMinusFiveClock)
    monkeypatch.setattr(T, "select_model", _stub_selection)
    monkeypatch.setattr(T, "out_of_fold_threshold", lambda selection, X, y: (0.5, 0.5))
    monkeypatch.setattr(T, "evaluate_once", lambda selection, threshold, X, y: _stub_evaluation(0.9))
    extract_returns(raw_frame)
    written: list[tuple[str, str | None]] = []
    original = Path.write_text

    def spy(
        self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None
    ) -> int:
        written.append((self.name, encoding))
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", spy)
        report = T.train()
    assert report.model_version == "20260727-141405"
    assert report.trained_at == "2026-07-27T14:14:05+00:00"
    assert written == [("metrics.json", "utf-8")]

    # FORBIDDEN PATH: a model at chance level must not be published: shipping one would send analysts to
    # investigate randomly while displaying a confidence score. The refusal comes before any file is written, so
    # the artefact and its metrics are exactly what the last accepted run left.
    assert T.MIN_ACCEPTABLE_AUC > 0.5
    assert T.RANDOM_STATE == 42
    artefact_before = T.MODEL_PATH.read_bytes()
    metrics_before = T.METRICS_PATH.read_bytes()
    monkeypatch.setattr(T, "evaluate_once", lambda selection, threshold, X, y: _stub_evaluation(0.5))
    with pytest.raises(AssertionError, match="Invariant violated: roc_auc >= 0.7"):
        T.train()
    assert T.MODEL_PATH.read_bytes() == artefact_before
    assert T.METRICS_PATH.read_bytes() == metrics_before
