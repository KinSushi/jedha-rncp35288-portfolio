"""Contract tests for ``ml.train``: the figures it reports, what it journals and what it publishes.

``test_ml_training.py`` proves the rule of ADR-0009 (nothing computed from the test rows feeds a decision). This file
pins the rest of the behaviour, so that a changed constant, a flipped comparison or an altered message cannot go
unnoticed: the report is recomputed here from the rules (four stratified folds, seed 42, margin 0.01, F1 criterion,
three-decimal threshold), the journal is compared with the report, and the boundaries of every guard are tested on
both sides. The expected values are written as literals on purpose: a constant read from the module would move with
the code under test.
"""

from __future__ import annotations

import json
import re
import warnings
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.base import clone
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import GridSearchCV, StratifiedKFold, cross_val_predict, cross_val_score, train_test_split

from banking_dataops import audit
from banking_dataops.ml import features as F
from banking_dataops.ml import train as T
from banking_dataops.ml.train import _build_estimator, _choose_threshold, _cv, _feature_names, _importances

pytestmark = [
    pytest.mark.limit(120),
    pytest.mark.usefixtures("no_audit_database"),
]

SMALL_GRID: dict[str, list[object]] = {
    "model__n_estimators": [15, 30],
    "model__max_depth": [4],
    "model__min_samples_leaf": [1],
}


def _split_with_literals(raw: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series, pd.Series]:
    """The split of ADR-0009 written with its literal values: 25 % test rows, seed 42, stratified."""
    X, y = F.split_x_y(F.build(raw))
    return train_test_split(X, y, test_size=0.25, random_state=42, stratify=y)


def _folds() -> StratifiedKFold:
    """The four shuffled stratified folds of ADR-0009, with the literal seed."""
    return StratifiedKFold(n_splits=4, shuffle=True, random_state=42)


class Trained(SimpleNamespace):
    """One real training run: the report, the journal it wrote and the files it published."""

    report: T.TrainingReport
    events: list[audit.AuditEvent]
    raw: pd.DataFrame
    root: Path
    model_path: Path
    metrics_path: Path


@pytest.fixture
def trained(_generated_frame: pd.DataFrame, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Trained]:
    """Train, with a small forest grid, into a NESTED temporary directory (``parents=True`` is exercised).

    One run per test, on purpose: the mutation tool attributes a function to the test during which it ran, so a
    run shared by the whole module would be credited to its first test only, and the journal tests would never
    be asked to judge a mutant of ``train``.
    """
    patch = pytest.MonkeyPatch()
    events: list[audit.AuditEvent] = []
    patch.setattr(audit, "_persist_event", lambda event, settings=None: events.append(event) or False)
    root = tmp_path_factory.mktemp("trained")
    model_dir = root / "nested" / "models"
    patch.setattr(T, "ROOT", root)
    patch.setattr(T, "MODEL_DIR", model_dir)
    patch.setattr(T, "MODEL_PATH", model_dir / "suspicion_classifier.joblib")
    patch.setattr(T, "METRICS_PATH", model_dir / "metrics.json")
    patch.setattr(T, "FOREST_PARAM_GRID", SMALL_GRID)
    patch.setattr(F, "extract", lambda settings=None: _generated_frame.copy())
    report = T.train()
    yield Trained(
        report=report,
        events=events,
        raw=_generated_frame,
        root=root,
        model_path=model_dir / "suspicion_classifier.joblib",
        metrics_path=model_dir / "metrics.json",
    )
    patch.undo()


def _event(trained: Trained, action: str) -> audit.AuditEvent:
    return next(event for event in trained.events if event.action == action)


# ---------------------------------------------------------------------------
# The report is what the rules of ADR-0009 give
# ---------------------------------------------------------------------------


def test_report_describes_the_run_and_the_split(trained: Trained) -> None:
    """Model identity, split sizes and constants of the run are what the project documents."""
    report = trained.report
    X_train, X_test, _, _ = _split_with_literals(trained.raw)
    assert report.model_name == "suspicion_classifier"
    assert report.rows_total == len(trained.raw)
    assert report.rows_train == len(X_train)
    assert report.rows_test == len(X_test)
    assert report.positive_rate == round(float(trained.raw["is_suspicious"].mean()), 4)
    assert report.cv_folds == 4
    assert report.selection_margin == 0.01
    assert report.threshold_criterion == "max_f1"
    assert report.threshold_source == "out-of-fold predictions on the training set"
    assert report.random_state == 42
    assert re.fullmatch(r"\d{8}-\d{6}", report.model_version)
    offset = datetime.fromisoformat(report.trained_at).utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_selection_figures_are_the_cross_validated_ones_on_the_training_rows(trained: Trained) -> None:
    """The AUCs the choice rests on are recomputed here on the same four folds, with the same seed."""
    X_train, _, y_train, _ = _split_with_literals(trained.raw)
    baseline = round(
        float(np.mean(cross_val_score(_build_estimator("baseline"), X_train, y_train, cv=_folds(), scoring="roc_auc"))),
        4,
    )
    search = GridSearchCV(
        _build_estimator("forest"), param_grid=SMALL_GRID, scoring="roc_auc", cv=_folds(), n_jobs=1, refit=True
    ).fit(X_train, y_train)
    forest = round(float(search.best_score_), 4)

    assert trained.report.baseline_cv_roc_auc == baseline
    assert trained.report.forest_cv_roc_auc == forest
    gap = forest - baseline
    assert trained.report.model_family == ("random_forest" if gap >= 0.01 else "logistic_regression")
    assert trained.report.best_params == {k: str(v) for k, v in search.best_params_.items()}


def test_threshold_is_the_f1_optimum_of_out_of_fold_scores_rounded_to_three_decimals(trained: Trained) -> None:
    """The threshold comes from training rows only and is the one stored, to three decimals, everywhere."""
    X_train, _, y_train, _ = _split_with_literals(trained.raw)
    artefact = joblib.load(trained.model_path)
    out_of_fold = cross_val_predict(clone(artefact["pipeline"]), X_train, y_train, cv=_folds(), method="predict_proba")[
        :, 1
    ]
    expected = round(_choose_threshold(y_train.to_numpy(), out_of_fold), 3)

    assert trained.report.decision_threshold == expected
    assert artefact["threshold"] == expected
    assert trained.report.threshold_oof_f1 == round(
        float(f1_score(y_train.to_numpy(), out_of_fold >= expected, zero_division=0)), 4
    )


def test_test_figures_are_measured_once_on_the_test_rows_with_the_frozen_threshold(trained: Trained) -> None:
    """Precision, recall, F1, AUCs and the confusion matrix are recomputed from the published model."""
    X_train, X_test, y_train, y_test = _split_with_literals(trained.raw)
    artefact = joblib.load(trained.model_path)
    scores = artefact["pipeline"].predict_proba(X_test)[:, 1]
    predicted = scores >= artefact["threshold"]
    actual = y_test.to_numpy().astype(bool)
    report = trained.report

    assert report.roc_auc == round(float(roc_auc_score(y_test, scores)), 4)
    assert report.average_precision == round(float(average_precision_score(y_test, scores)), 4)
    assert report.precision == round(float(precision_score(y_test, predicted, zero_division=0)), 4)
    assert report.recall == round(float(recall_score(y_test, predicted, zero_division=0)), 4)
    assert report.f1 == round(float(f1_score(y_test, predicted, zero_division=0)), 4)
    assert report.confusion == {
        "true_negative": int(np.sum(~predicted & ~actual)),
        "false_positive": int(np.sum(predicted & ~actual)),
        "false_negative": int(np.sum(~predicted & actual)),
        "true_positive": int(np.sum(predicted & actual)),
    }
    baseline_scores = _build_estimator("baseline").fit(X_train, y_train).predict_proba(X_test)[:, 1]
    assert report.baseline_test_roc_auc == round(float(roc_auc_score(y_test, baseline_scores)), 4)
    assert 0.5 < report.forest_test_roc_auc <= 1.0


def test_top_features_are_the_twelve_heaviest_of_the_published_model(trained: Trained) -> None:
    """The analyst is told which factors drive the score: largest first, five decimals, at most twelve."""
    artefact = joblib.load(trained.model_path)
    expected = _importances(artefact["pipeline"], 12)
    top = trained.report.top_features
    assert top == expected
    weights = [float(item["importance"]) for item in top]  # type: ignore[arg-type]
    assert weights == sorted(weights, reverse=True)
    assert [set(item) for item in top][0] == {"feature", "importance"}


# ---------------------------------------------------------------------------
# What is published
# ---------------------------------------------------------------------------


def test_artefact_and_metrics_are_published_together_in_the_documented_formats(trained: Trained) -> None:
    """The joblib artefact carries pipeline, threshold and report; metrics.json is the same report, indented."""
    artefact = joblib.load(trained.model_path)
    assert set(artefact) == {"pipeline", "threshold", "report"}
    assert artefact["report"] == asdict(trained.report)
    assert trained.metrics_path.read_text(encoding="utf-8") == json.dumps(asdict(trained.report), indent=2)
    assert json.loads(trained.metrics_path.read_text(encoding="utf-8")) == artefact["report"]


def test_training_twice_into_the_same_directory_is_accepted(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns, monkeypatch: pytest.MonkeyPatch, no_audit_database: None
) -> None:
    """The directory of the model may already exist: a second training replaces the files instead of failing."""
    monkeypatch.setattr(T, "select_model", _stub_selection)
    monkeypatch.setattr(T, "out_of_fold_threshold", lambda selection, X, y: (0.5, 0.5))
    extract_returns(raw_frame)

    first = T.train()
    second = T.train()

    assert first.decision_threshold == second.decision_threshold == 0.5
    assert json.loads(T.METRICS_PATH.read_text(encoding="utf-8"))["model_version"] == second.model_version


# ---------------------------------------------------------------------------
# The journal tells the same story as the report
# ---------------------------------------------------------------------------


def test_journal_lists_the_steps_in_the_documented_order(trained: Trained) -> None:
    """Select, then threshold, then evaluate, then publish: the order of the events is the order of the contract."""
    assert [(event.category, event.action) for event in trained.events] == [
        ("pipeline", "run_started"),
        ("integrity", "invariant_checked"),
        ("integrity", "invariant_checked"),
        ("data", "baseline_trained"),
        ("data", "model_selected"),
        ("data", "threshold_selected"),
        ("data", "model_evaluated"),
        ("integrity", "invariant_checked"),
        ("data", "model_persisted"),
        ("pipeline", "run_succeeded"),
    ]


def test_journal_run_started_records_the_seed_and_names_the_step(trained: Trained) -> None:
    started = _event(trained, "run_started")
    assert started.reason == "step ml_train began"
    assert started.details["seed"] == 42
    assert _event(trained, "run_succeeded").reason == "step ml_train completed without error"


def test_journal_invariants_state_their_rule_their_reason_and_their_figures(trained: Trained) -> None:
    checks = [event for event in trained.events if event.action == "invariant_checked"]
    rows, balance, floor = checks
    assert rows.reason == "below that, a stratified split leaves too few positives for the metrics to mean anything"
    assert rows.details == {"invariant": "training set has at least 200 rows", "holds": True, "rows": len(trained.raw)}
    assert balance.reason == (
        "a degenerate class balance makes every metric misleading and usually signals a broken label rather than a "
        "hard problem"
    )
    assert balance.details == {
        "invariant": "0.01 < positive rate < 0.5",
        "holds": True,
        "positive_rate": trained.report.positive_rate,
    }
    assert floor.reason == (
        "publishing a model at chance level would send analysts to investigate randomly while displaying a "
        "confidence score"
    )
    assert floor.details == {"invariant": "roc_auc >= 0.7", "holds": True, "roc_auc": trained.report.roc_auc}


def test_journal_selection_events_carry_the_figures_of_the_report(trained: Trained) -> None:
    report = trained.report
    baseline = _event(trained, "baseline_trained")
    assert baseline.reason == "a linear reference everyone can read, to judge whether tuning earns its cost"
    assert baseline.row_count == report.rows_train
    assert baseline.details == {"cv_roc_auc": report.baseline_cv_roc_auc, "cv_folds": 4}

    selected = _event(trained, "model_selected")
    assert selected.reason == report.selection_reason
    assert selected.row_count == report.rows_train
    assert selected.details == {
        "chosen": report.model_family,
        "rejected": "random_forest" if report.model_family == "logistic_regression" else "logistic_regression",
        "baseline_cv_roc_auc": report.baseline_cv_roc_auc,
        "forest_cv_roc_auc": report.forest_cv_roc_auc,
        "margin": 0.01,
        "data_used": "training rows only",
    }

    threshold = _event(trained, "threshold_selected")
    assert threshold.reason == (
        "max_f1 on out-of-fold predictions on the training set: the threshold is fixed before the test rows are scored"
    )
    assert threshold.row_count == report.rows_train
    assert threshold.details == {
        "threshold": round(report.decision_threshold, 4),
        "oof_f1": report.threshold_oof_f1,
        "criterion": "max_f1",
    }


def test_journal_evaluation_and_publication_events_carry_the_figures_of_the_report(trained: Trained) -> None:
    report = trained.report
    evaluated = _event(trained, "model_evaluated")
    assert evaluated.reason == "single evaluation on the test rows, after model and threshold were frozen"
    assert evaluated.row_count == report.rows_test
    assert evaluated.details == {
        "roc_auc": report.roc_auc,
        "precision": report.precision,
        "recall": report.recall,
        "f1": report.f1,
        "baseline_test_roc_auc": report.baseline_test_roc_auc,
        "forest_test_roc_auc": report.forest_test_roc_auc,
    }

    persisted = _event(trained, "model_persisted")
    assert persisted.reason == "the artefact and its metrics are versioned so a verdict can be traced to a model"
    assert persisted.row_count == report.rows_train
    assert Path(str(persisted.details["path"])).parts == ("nested", "models", "suspicion_classifier.joblib")
    assert persisted.details["roc_auc"] == report.roc_auc


# ---------------------------------------------------------------------------
# Guards: both sides of every boundary
# ---------------------------------------------------------------------------


class _Reached(Exception):
    """Raised by a stand-in for ``select_model``: the guards before it were passed."""


def _stop_at_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(X_train: pd.DataFrame, y_train: pd.Series) -> T.Selection:
        raise _Reached

    monkeypatch.setattr(T, "select_model", refuse)


def test_exactly_200_rows_are_accepted(
    raw_frame: pd.DataFrame, extract_returns, monkeypatch: pytest.MonkeyPatch, no_audit_database: None
) -> None:
    _stop_at_selection(monkeypatch)
    extract_returns(raw_frame.head(200))
    with pytest.raises(_Reached):
        T.train()


def test_199_rows_are_refused_with_the_documented_message(
    raw_frame: pd.DataFrame, extract_returns, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    """FORBIDDEN PATH: one row under the minimum. The refusal names the rule and the reason, and is journalled."""
    extract_returns(raw_frame.head(199))
    with pytest.raises(AssertionError) as refusal:
        T.train()
    assert str(refusal.value) == (
        "Invariant violated: training set has at least 200 rows. below that, a stratified split leaves too few "
        "positives for the metrics to mean anything"
    )
    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert violated.details == {"invariant": "training set has at least 200 rows", "holds": False, "rows": 199}


def _with_positives(raw: pd.DataFrame, positives: int) -> pd.DataFrame:
    frame = raw.copy()
    frame["is_suspicious"] = [index < positives for index in range(len(frame))]
    return frame


@pytest.mark.parametrize("positives", [9, 399])
def test_class_balance_just_inside_the_open_interval_is_accepted(
    raw_frame: pd.DataFrame, extract_returns, monkeypatch: pytest.MonkeyPatch, no_audit_database: None, positives: int
) -> None:
    """800 rows: 9 positives is 1.1 %, 399 is 49.9 %: both are inside the open interval (1 %, 50 %)."""
    _stop_at_selection(monkeypatch)
    extract_returns(_with_positives(raw_frame.head(800), positives))
    with pytest.raises(_Reached):
        T.train()


@pytest.mark.parametrize("positives", [8, 400])
def test_class_balance_on_the_boundary_is_refused(raw_frame: pd.DataFrame, extract_returns, positives: int) -> None:
    """FORBIDDEN PATH: exactly 1 % or exactly 50 % positives is outside the open interval and stops the run."""
    extract_returns(_with_positives(raw_frame.head(800), positives))
    with pytest.raises(AssertionError, match=re.escape("Invariant violated: 0.01 < positive rate < 0.5.")):
        T.train()


def _stub_selection(X_train: pd.DataFrame, y_train: pd.Series) -> T.Selection:
    """A fast stand-in for ``select_model``: a fitted logistic baseline chosen without any cross-validation."""
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


def _stub_everything_but_the_floor(monkeypatch: pytest.MonkeyPatch, roc_auc: float) -> None:
    monkeypatch.setattr(T, "select_model", _stub_selection)
    monkeypatch.setattr(T, "out_of_fold_threshold", lambda selection, X, y: (0.5, 0.5))
    monkeypatch.setattr(T, "evaluate_once", lambda selection, threshold, X, y: _stub_evaluation(roc_auc))


def test_a_model_exactly_at_the_acceptance_floor_is_published(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns, monkeypatch: pytest.MonkeyPatch, no_audit_database: None
) -> None:
    _stub_everything_but_the_floor(monkeypatch, 0.7)
    extract_returns(raw_frame)
    report = T.train()
    assert report.roc_auc == 0.7
    assert T.MODEL_PATH.exists()


def test_a_model_just_under_the_floor_leaves_the_directory_unchanged(
    raw_frame: pd.DataFrame, model_dir: Path, extract_returns, monkeypatch: pytest.MonkeyPatch, audit_log: list
) -> None:
    """FORBIDDEN PATH: 0.6999 is under the floor. Nothing is published, the refusal names the rule and the figure."""
    _stub_everything_but_the_floor(monkeypatch, 0.6999)
    extract_returns(raw_frame)
    with pytest.raises(AssertionError) as refusal:
        T.train()
    assert str(refusal.value) == (
        "Invariant violated: roc_auc >= 0.7. publishing a model at chance level would send analysts to investigate "
        "randomly while displaying a confidence score"
    )
    assert not model_dir.exists()
    violated = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert violated.details == {"invariant": "roc_auc >= 0.7", "holds": False, "roc_auc": 0.6999}


def test_train_hands_its_settings_to_the_journal_and_the_extract(
    raw_frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch, audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    """A caller that targets another database must not be sent to the default one."""
    seen: dict[str, object] = {}

    def fake_extract(settings: object = None) -> pd.DataFrame:
        seen["extract"] = settings
        return raw_frame.head(10)

    monkeypatch.setattr(F, "extract", fake_extract)
    marker = object()
    with pytest.raises(AssertionError, match="at least 200 rows"):
        T.train(settings=marker)  # type: ignore[arg-type]
    assert seen == {"extract": marker}
    assert audit_log[0][0].action == "run_started"
    assert audit_log[0][1] is marker


def test_the_share_of_test_rows_is_a_quarter_by_default_and_can_be_changed(
    raw_frame: pd.DataFrame, extract_returns, monkeypatch: pytest.MonkeyPatch, no_audit_database: None
) -> None:
    seen: list[int] = []

    def spy(X_train: pd.DataFrame, y_train: pd.Series) -> T.Selection:
        seen.append(len(X_train))
        raise _Reached

    monkeypatch.setattr(T, "select_model", spy)
    extract_returns(raw_frame)
    with pytest.raises(_Reached):
        T.train()
    with pytest.raises(_Reached):
        T.train(test_size=0.5)
    assert seen[0] == len(_split_with_literals(raw_frame)[0])
    assert seen[1] == len(raw_frame) - round(len(raw_frame) * 0.5)


# ---------------------------------------------------------------------------
# Selection, threshold and final evaluation, one by one
# ---------------------------------------------------------------------------


def test_select_model_reports_the_cross_validated_scores_and_the_reason_of_a_forest_win(
    raw_frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(T, "FOREST_PARAM_GRID", SMALL_GRID)
    monkeypatch.setattr(T, "SELECTION_MARGIN", -10.0)
    X_train, _, y_train, _ = _split_with_literals(raw_frame)

    selection = T.select_model(X_train, y_train)

    expected_baseline = float(
        np.mean(cross_val_score(_build_estimator("baseline"), X_train, y_train, cv=_folds(), scoring="roc_auc"))
    )
    assert selection.baseline_cv_roc_auc == expected_baseline
    assert (selection.family, selection.rejected) == ("random_forest", "logistic_regression")
    assert selection.estimator is selection.forest
    gap = selection.forest_cv_roc_auc - selection.baseline_cv_roc_auc
    assert selection.reason == (
        f"the tuned forest leads the baseline by {gap:.4f} cross-validated AUC on the training rows, above the "
        "-10.0 margin that justifies the added opacity"
    )


def test_select_model_keeps_the_baseline_below_the_margin_and_says_why(
    raw_frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(T, "FOREST_PARAM_GRID", SMALL_GRID)
    monkeypatch.setattr(T, "SELECTION_MARGIN", 10.0)
    X_train, _, y_train, _ = _split_with_literals(raw_frame)

    selection = T.select_model(X_train, y_train)

    assert (selection.family, selection.rejected) == ("logistic_regression", "random_forest")
    assert selection.estimator is selection.baseline
    gap = selection.forest_cv_roc_auc - selection.baseline_cv_roc_auc
    assert selection.reason == (
        f"the tuned forest gains only {gap:+.4f} cross-validated AUC over the baseline on the training rows, below "
        "the 10.0 margin; the simpler and readable model is shipped instead"
    )
    assert selection.best_params in (
        {"model__n_estimators": "15", "model__max_depth": "4", "model__min_samples_leaf": "1"},
        {"model__n_estimators": "30", "model__max_depth": "4", "model__min_samples_leaf": "1"},
    )


def test_select_model_fits_both_candidates_on_the_whole_training_set(
    raw_frame: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(T, "FOREST_PARAM_GRID", SMALL_GRID)
    X_train, X_test, y_train, _ = _split_with_literals(raw_frame)

    selection = T.select_model(X_train, y_train)

    expected = _build_estimator("baseline").fit(X_train, y_train).predict_proba(X_test)[:, 1]
    assert np.allclose(selection.baseline.predict_proba(X_test)[:, 1], expected)
    assert selection.forest.predict_proba(X_test).shape == (len(X_test), 2)


def test_out_of_fold_threshold_returns_the_threshold_and_its_f1_and_leaves_the_estimator_unchanged(
    raw_frame: pd.DataFrame,
) -> None:
    X_train, X_test, y_train, _ = _split_with_literals(raw_frame)
    selection = _stub_selection(X_train, y_train)
    fitted = selection.estimator
    before = fitted.predict_proba(X_test)

    threshold, f1_at_threshold = T.out_of_fold_threshold(selection, X_train, y_train)

    out_of_fold = cross_val_predict(clone(fitted), X_train, y_train, cv=_folds(), method="predict_proba")[:, 1]
    expected_threshold = round(_choose_threshold(y_train.to_numpy(), out_of_fold), 3)
    assert threshold == expected_threshold
    assert f1_at_threshold == float(f1_score(y_train.to_numpy(), out_of_fold >= expected_threshold, zero_division=0))
    assert np.array_equal(fitted.predict_proba(X_test), before)


class _FixedScorer:
    """A scorer that answers fixed probabilities, whatever the features."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probabilities = np.array(self.scores[: len(X)])
        return np.column_stack([1 - probabilities, probabilities])


def _evaluation_selection(scores: list[float]) -> T.Selection:
    scorer = _FixedScorer(scores)
    return T.Selection(
        family="stub",
        rejected="stub",
        estimator=scorer,  # type: ignore[arg-type]
        baseline=_FixedScorer([0.9, 0.1, 0.9, 0.1, 0.9]),  # type: ignore[arg-type]
        forest=_FixedScorer([0.1, 0.1, 0.9, 0.9, 0.1]),  # type: ignore[arg-type]
        best_params={},
        baseline_cv_roc_auc=0.0,
        forest_cv_roc_auc=0.0,
        reason="stub",
    )


def test_evaluate_once_counts_a_score_equal_to_the_threshold_as_flagged() -> None:
    """Five test rows, hand-checked: flagged at 0.7 are rows 2, 3 and 4 (a score equal to the threshold counts)."""
    y_test = pd.Series([False, False, True, True, True])
    X_test = pd.DataFrame({"x": range(5)})
    selection = _evaluation_selection([0.2, 0.7, 0.7, 0.9, 0.1])

    result = T.evaluate_once(selection, 0.7, X_test, y_test)

    assert result.confusion == {"true_negative": 1, "false_positive": 1, "false_negative": 1, "true_positive": 2}
    assert result.precision == pytest.approx(2 / 3)
    assert result.recall == pytest.approx(2 / 3)
    assert result.f1 == pytest.approx(2 / 3)
    assert result.roc_auc == pytest.approx(roc_auc_score(y_test, [0.2, 0.7, 0.7, 0.9, 0.1]))
    assert result.average_precision == pytest.approx(average_precision_score(y_test, [0.2, 0.7, 0.7, 0.9, 0.1]))
    assert result.baseline_test_roc_auc == pytest.approx(roc_auc_score(y_test, [0.9, 0.1, 0.9, 0.1, 0.9]))
    assert result.forest_test_roc_auc == pytest.approx(roc_auc_score(y_test, [0.1, 0.1, 0.9, 0.9, 0.1]))


def test_evaluate_once_reports_zero_without_a_warning_when_nothing_is_flagged() -> None:
    """A threshold above every score flags nothing: precision, recall and F1 are 0 by rule, silently."""
    y_test = pd.Series([False, True, False, True, False])
    X_test = pd.DataFrame({"x": range(5)})
    selection = _evaluation_selection([0.1, 0.2, 0.3, 0.4, 0.5])

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = T.evaluate_once(selection, 0.99, X_test, y_test)

    assert (result.precision, result.recall, result.f1) == (0.0, 0.0, 0.0)
    assert result.confusion == {"true_negative": 3, "false_positive": 0, "false_negative": 2, "true_positive": 0}


def test_choose_threshold_prefers_the_lowest_threshold_among_equal_f1() -> None:
    """Thresholds 0.8 and 0.2 both give an F1 of two thirds: the lowest of the two is returned."""
    y_true = np.array([1, 1, 1, 1, 0, 0, 0, 0])
    scores = np.array([0.9, 0.8, 0.3, 0.2, 0.35, 0.34, 0.33, 0.32])
    assert _choose_threshold(y_true, scores) == 0.2


def test_choose_threshold_falls_back_to_one_half_when_the_curve_has_no_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(T, "precision_recall_curve", lambda y, s: (np.array([1.0]), np.array([0.0]), np.array([])))
    assert _choose_threshold(np.array([0, 1]), np.array([0.1, 0.9])) == 0.5


def test_choose_threshold_scores_zero_where_precision_and_recall_are_both_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A point of the curve with precision 0 and recall 0 has an F1 of 0, not a division by zero."""
    monkeypatch.setattr(
        T,
        "precision_recall_curve",
        lambda y, s: (np.array([0.0, 0.5, 1.0]), np.array([0.0, 0.5, 0.0]), np.array([0.2, 0.6])),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _choose_threshold(np.array([0, 1]), np.array([0.1, 0.9])) == 0.6


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def test_cross_validation_uses_four_shuffled_folds_with_seed_42() -> None:
    splitter = _cv()
    assert splitter.get_n_splits() == 4
    assert splitter.shuffle is True
    assert splitter.random_state == 42


def test_baseline_is_a_balanced_logistic_regression_with_a_fixed_seed() -> None:
    model = _build_estimator("baseline").named_steps["model"]
    assert type(model).__name__ == "LogisticRegression"
    params = model.get_params()
    assert (params["max_iter"], params["class_weight"], params["random_state"]) == (1000, "balanced", 42)


def test_forest_is_a_balanced_subsample_random_forest_with_a_fixed_seed() -> None:
    model = _build_estimator("forest").named_steps["model"]
    assert type(model).__name__ == "RandomForestClassifier"
    params = model.get_params()
    assert (params["class_weight"], params["random_state"], params["n_jobs"]) == ("balanced_subsample", 42, -1)


def test_preprocessing_scales_numbers_encodes_categories_and_keeps_booleans() -> None:
    preprocessor = _build_estimator("baseline").named_steps["prep"]
    by_name = {
        name: (type(step).__name__ if not isinstance(step, str) else step, columns)
        for name, step, columns in preprocessor.transformers
    }
    assert by_name == {
        "num": ("StandardScaler", list(F.NUMERIC_FEATURES)),
        "cat": ("OneHotEncoder", list(F.CATEGORICAL_FEATURES)),
        "bool": ("passthrough", list(F.BOOLEAN_FEATURES)),
    }
    encoder = dict((name, step) for name, step, _ in preprocessor.transformers)["cat"]
    assert (encoder.handle_unknown, encoder.sparse_output) == ("ignore", False)
    assert preprocessor.remainder == "drop"


def test_a_category_unseen_in_training_is_scored_instead_of_crashing(raw_frame: pd.DataFrame) -> None:
    """In production a new channel may appear: it becomes an all-zero vector, not an error."""
    X, y = F.split_x_y(F.build(raw_frame))
    fitted = _build_estimator("baseline").fit(X, y)
    unseen = X.head(3).copy()
    unseen["channel"] = "carrier_pigeon"
    scores = fitted.predict_proba(unseen)[:, 1]
    assert scores.shape == (3,)
    assert bool(np.all((scores >= 0) & (scores <= 1)))


def test_feature_names_are_the_expanded_names_as_text(raw_frame: pd.DataFrame) -> None:
    X, y = F.split_x_y(F.build(raw_frame))
    fitted = _build_estimator("baseline").fit(X, y)
    names = _feature_names(fitted)
    assert names[: len(F.NUMERIC_FEATURES)] == [f"num__{name}" for name in F.NUMERIC_FEATURES]
    assert all(isinstance(name, str) and "__" in name for name in names)
    assert names == [str(name) for name in fitted.named_steps["prep"].get_feature_names_out()]


class _Prep:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def get_feature_names_out(self) -> list[str]:
        return self.names


def _fake_pipeline(model: object, names: list[str]) -> SimpleNamespace:
    return SimpleNamespace(named_steps={"prep": _Prep(names), "model": model})


def test_importances_of_a_linear_model_are_its_absolute_coefficients_largest_first() -> None:
    model = SimpleNamespace(coef_=np.array([[0.5, -2.0, 1.25, 0.0]]))
    result = _importances(_fake_pipeline(model, ["a", "b", "c", "d"]))  # type: ignore[arg-type]
    assert result == [
        {"feature": "b", "importance": 2.0},
        {"feature": "c", "importance": 1.25},
        {"feature": "a", "importance": 0.5},
        {"feature": "d", "importance": 0.0},
    ]


def test_importances_of_a_forest_are_its_impurity_importances_rounded_to_five_decimals() -> None:
    model = SimpleNamespace(feature_importances_=np.array([0.1234567, 0.7, 0.1765433]))
    result = _importances(_fake_pipeline(model, ["a", "b", "c"]), limit=2)  # type: ignore[arg-type]
    assert result == [{"feature": "b", "importance": 0.7}, {"feature": "c", "importance": 0.17654}]


def test_importances_keep_twelve_features_by_default() -> None:
    model = SimpleNamespace(feature_importances_=np.arange(1, 16) / 120)
    names = [f"f{i}" for i in range(15)]
    result = _importances(_fake_pipeline(model, names))  # type: ignore[arg-type]
    assert [item["feature"] for item in result] == [f"f{i}" for i in range(14, 2, -1)]


# ---------------------------------------------------------------------------
# The command line
# ---------------------------------------------------------------------------


def test_main_prints_the_summary_of_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from banking_dataops import logging_config

    calls: list[str] = []
    report = T.TrainingReport(
        model_name="suspicion_classifier",
        model_family="logistic_regression",
        model_version="20261011-000000",
        trained_at="2026-10-11T00:00:00+00:00",
        rows_total=1500,
        rows_train=1125,
        rows_test=375,
        positive_rate=0.0807,
        best_params={},
        cv_folds=4,
        baseline_cv_roc_auc=0.9748,
        forest_cv_roc_auc=0.9661,
        selection_margin=0.01,
        selection_reason="because",
        decision_threshold=0.852,
        threshold_criterion="max_f1",
        threshold_source="out-of-fold predictions on the training set",
        threshold_oof_f1=0.7079,
        roc_auc=0.97,
        average_precision=0.77,
        precision=0.6562,
        recall=0.7,
        f1=0.6774,
        confusion={"true_negative": 334, "false_positive": 11, "false_negative": 9, "true_positive": 21},
        baseline_test_roc_auc=0.97,
        forest_test_roc_auc=0.9573,
        top_features=[{"feature": f"f{i}", "importance": i / 10} for i in range(1, 9)],
    )
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "run")
    monkeypatch.setattr(T, "train", lambda: calls.append("train") or report)

    T.main()

    assert calls == ["configure_logging", "new_run_id", "train"]
    assert capsys.readouterr().out == (
        "\n"
        "Model      : suspicion_classifier v20261011-000000 (logistic_regression)\n"
        "Rows       : 1500 (8.1% positive)\n"
        "CV ROC AUC : baseline 0.9748, forest 0.9661 (4-fold on the training rows, used for the selection)\n"
        "Selection  : because\n"
        "Test AUC   : 0.97  (baseline 0.97, forest 0.9573; reported once, not used for any decision)\n"
        "Precision  : 0.6562   Recall: 0.7   F1: 0.6774\n"
        "Threshold  : 0.852  (max_f1 on out-of-fold predictions on the training set)\n"
        "Confusion  : {'true_negative': 334, 'false_positive': 11, 'false_negative': 9, 'true_positive': 21}\n"
        "\n"
        "Top factors:\n"
        "       0.1  f1\n"
        "       0.2  f2\n"
        "       0.3  f3\n"
        "       0.4  f4\n"
        "       0.5  f5\n"
        "       0.6  f6\n"
    )
