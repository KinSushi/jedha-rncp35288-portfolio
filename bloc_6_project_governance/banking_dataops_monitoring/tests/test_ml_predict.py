"""Tests for ``ml.predict``: the queue written to ``gold.predictions``.

The database is replaced by a recording fake, so the tests check what would be sent to
PostgreSQL: the statements, their order, and every row. The refusals check the other
half: when the inputs are unacceptable nothing at all is sent, the table is left as it was.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest

from banking_dataops import logging_config
from banking_dataops.audit import AuditEvent
from banking_dataops.ml import features as F
from banking_dataops.ml import predict as P
from banking_dataops.ml.train import _build_estimator

pytestmark = [
    pytest.mark.limit(120),
    pytest.mark.usefixtures("no_audit_database", "model_dir"),
]


class RecordingCursor:
    """Records every statement it receives."""

    def __init__(self, log: list[tuple]) -> None:
        self.log = log

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.log.append(("execute", sql, params))

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.log.append(("executemany", sql, list(rows)))


class RecordingConnection:
    """Stands in for a psycopg connection: a context manager with a cursor and a commit."""

    def __init__(self, log: list[tuple]) -> None:
        self.log = log

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.log)

    def commit(self) -> None:
        self.log.append(("commit",))


class NanModel:
    """A scorer that returns NaN for every row (module level so that joblib can pickle it)."""

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        return np.full((len(X), 2), np.nan)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Replace ``predict.connect`` and return the log of what the code sent."""
    log: list[tuple] = []
    monkeypatch.setattr(P, "connect", lambda settings=None: RecordingConnection(log))
    return log


def _write_artefact(raw: pd.DataFrame, path: Path, threshold: float) -> None:
    """Fit the baseline on the whole frame and save it the way ``train()`` does."""
    X, y = F.split_x_y(F.build(raw))
    pipeline = _build_estimator("baseline").fit(X, y)
    joblib.dump(
        {
            "pipeline": pipeline,
            "threshold": threshold,
            "report": {"model_version": "test-1", "model_family": "logistic_regression"},
        },
        path,
    )


def _expected_band(score: float, threshold: float) -> str:
    """The rule of ADR-0009, restated independently of the implementation."""
    if score >= threshold:
        return "high"
    if score >= 0.6 * threshold:
        return "medium"
    return "low"


INSERT_STATEMENT = (
    "INSERT INTO gold.predictions (transaction_id, suspicion_score, predicted_suspicious, decision_threshold, "
    "priority_band, model_name, model_version, run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


class FixedScores:
    """A scorer that answers the given probabilities in order, whatever the features (picklable)."""

    def __init__(self, scores: list[float]) -> None:
        self.scores = scores

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        probabilities = np.array(self.scores[: len(X)])
        return np.column_stack([1 - probabilities, probabilities])


def _write_fixed_artefact(path: Path, scores: list[float], threshold: float, report: dict[str, str]) -> None:
    joblib.dump({"pipeline": FixedScores(scores), "threshold": threshold, "report": report}, path)


def test_predict_writes_a_consistent_rounded_queue_and_refuses_what_it_cannot_score_leaving_the_table_as_it_was(
    raw_frame: pd.DataFrame, extract_returns, database: list[tuple], tmp_path: Path
) -> None:
    # The table is truncated first, refilled in one statement, and every row follows the rule.
    artefact = tmp_path / "model.joblib"
    _write_artefact(raw_frame, artefact, threshold=0.7)
    extract_returns(raw_frame)
    written = P.predict(model_path=artefact)
    assert [entry[0] for entry in database] == ["execute", "executemany", "commit"]
    assert database[0][1] == "TRUNCATE TABLE gold.predictions"
    rows = database[1][2]
    assert written == len(rows)
    for transaction_id, score, flagged, threshold, band, model_name, version, run_id in rows:
        assert threshold == 0.7
        assert band == _expected_band(score, threshold), transaction_id
        assert flagged == (band == "high"), transaction_id
        assert band in P.BANDS
        assert model_name == "suspicion_classifier"
        assert version == "test-1"
        assert run_id
    # The column list is the contract with sql/08_gold_layer.sql: its order decides which value lands where.
    assert " ".join(database[1][1].split()) == INSERT_STATEMENT
    # Every transaction is scored exactly once.
    assert sorted(row[0] for row in rows) == sorted(raw_frame["transaction_id"])

    # Regression of the first version, where no row could ever be ``high``.
    database.clear()
    _write_artefact(raw_frame, artefact, threshold=0.85)
    P.predict(model_path=artefact)
    assert "high" in {row[4] for row in database[1][2]}

    # A score equal to the threshold is flagged; stored figures are rounded to 5 and 3 decimals.
    database.clear()
    threshold = 0.85243
    scores = [threshold, 0.85242, 0.5, 0.511459, 0.123456789, 1.0]
    batch = raw_frame.iloc[: len(scores)]
    _write_fixed_artefact(artefact, scores, threshold, {"model_version": "v-7", "model_family": "stub"})
    extract_returns(batch)
    P.predict(model_path=artefact)
    stored = database[1][2]
    name = "suspicion_classifier"
    assert [row[:7] for row in stored] == [
        (batch.iloc[0]["transaction_id"], 0.85243, True, 0.852, "high", name, "v-7"),
        (batch.iloc[1]["transaction_id"], 0.85242, False, 0.852, "medium", name, "v-7"),
        (batch.iloc[2]["transaction_id"], 0.5, False, 0.852, "low", name, "v-7"),
        (batch.iloc[3]["transaction_id"], 0.51146, False, 0.852, "medium", name, "v-7"),
        (batch.iloc[4]["transaction_id"], 0.12346, False, 0.852, "low", name, "v-7"),
        (batch.iloc[5]["transaction_id"], 1.0, True, 0.852, "high", name, "v-7"),
    ]
    assert {row[7] for row in stored} != {""}

    # FORBIDDEN PATH: scoring without a model. The message carries the command that trains one.
    database.clear()
    with pytest.raises(RuntimeError, match=r"ml\.train"):
        P.predict(model_path=tmp_path / "absent.joblib")
    assert database == []

    # FORBIDDEN PATH: an empty batch would silently replace the queue with an empty one.
    _write_artefact(raw_frame, artefact, threshold=0.5)
    extract_returns(raw_frame.iloc[0:0])
    with pytest.raises(AssertionError, match="something to score"):
        P.predict(model_path=artefact)
    assert database == []

    # FORBIDDEN PATH: a threshold of 0 would label everything ``high``. Nothing is truncated.
    _write_artefact(raw_frame, artefact, threshold=0.0)
    extract_returns(raw_frame)
    with pytest.raises(ValueError, match="Threshold"):
        P.predict(model_path=artefact)
    assert database == []

    # FORBIDDEN PATH: a model that returns NaN must stop the run, not hide in the ``low`` band.
    joblib.dump({"pipeline": NanModel(), "threshold": 0.5, "report": {"model_version": "test-1"}}, artefact)
    with pytest.raises(ValueError, match="not a probability"):
        P.predict(model_path=artefact)
    assert database == []


def test_predict_journals_the_run_and_its_figures_hands_the_settings_down_and_leaves_the_table_when_scores_are_missing(
    raw_frame: pd.DataFrame,
    extract_returns,
    database: list[tuple],
    audit_log: list[tuple[AuditEvent, object]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An auditor reads the journal, not the code: every event carries its reason and its figures."""
    artefact = tmp_path / "model.joblib"
    _write_artefact(raw_frame, artefact, threshold=0.72349)
    extract_returns(raw_frame)

    written = P.predict(model_path=artefact)

    events = [event for event, _ in audit_log]
    assert [(e.category, e.action) for e in events] == [
        ("pipeline", "run_started"),
        ("integrity", "invariant_checked"),
        ("integrity", "invariant_checked"),
        ("data", "predictions_written"),
        ("pipeline", "run_succeeded"),
    ]
    started, rows_check, scores_check, queue, finished = events
    assert started.reason == "step ml_predict began"
    assert rows_check.reason == "scoring an empty batch would silently produce an empty queue"
    assert rows_check.details == {"invariant": "there is something to score", "holds": True, "rows": len(raw_frame)}
    assert scores_check.reason == (
        "a length mismatch would attach each score to the wrong transaction and send analysts to investigate "
        "innocent rows"
    )
    assert scores_check.details == {
        "invariant": "one score per transaction, in order",
        "holds": True,
        "rows": len(raw_frame),
        "scores": len(raw_frame),
    }
    stored = database[1][2]
    per_band = Counter(row[4] for row in stored)
    assert queue.reason == (
        "the investigation queue is refreshed; it prioritises analyst attention and never blocks publication"
    )
    assert queue.row_count == written
    assert queue.details == {
        "model_family": "logistic_regression",
        "model_version": "test-1",
        "threshold": 0.723,
        "flagged": sum(row[2] for row in stored),
        "high_priority": per_band["high"],
        "medium_priority": per_band["medium"],
        "low_priority": per_band["low"],
    }
    assert queue.details["flagged"] == per_band["high"]
    assert finished.reason == "step ml_predict completed without error"
    # The threshold printed next to a score is the rounded one, in the table and in the journal alike.
    assert {row[3] for row in stored} == {0.723}

    # A single transaction is a batch like any other; an artefact without a family is reported as unknown.
    database.clear()
    audit_log.clear()
    _write_fixed_artefact(artefact, [0.9], 0.5, {"model_version": "v-1"})
    extract_returns(raw_frame.iloc[:1])
    assert P.predict(model_path=artefact) == 1
    single = next(event for event, _ in audit_log if event.action == "predictions_written")
    assert single.details["model_family"] == "unknown"
    assert single.details["high_priority"] == 1

    # A caller that points the step at another database must not be silently sent to the default one.
    database.clear()
    audit_log.clear()
    _write_artefact(raw_frame, artefact, threshold=0.5)
    seen: dict[str, object] = {}

    def fake_extract(settings: object = None) -> pd.DataFrame:
        seen["extract"] = settings
        return raw_frame

    def fake_connect(settings: object = None) -> RecordingConnection:
        seen["connect"] = settings
        return RecordingConnection(database)

    marker = object()
    with monkeypatch.context() as patch:
        patch.setattr(F, "extract", fake_extract)
        patch.setattr(P, "connect", fake_connect)
        P.predict(settings=marker, model_path=artefact)  # type: ignore[arg-type]
    assert seen == {"extract": marker, "connect": marker}
    # The two invariant events are journalled without settings (``assert_invariant`` takes none): they go to the
    # default database. The events of the run itself must follow the caller's settings.
    run_events = [(event.action, settings) for event, settings in audit_log if event.category != "integrity"]
    assert run_events == [
        ("run_started", marker),
        ("predictions_written", marker),
        ("run_succeeded", marker),
    ]

    # FORBIDDEN PATH: scores shifted onto the wrong transactions. Nothing is written, the run is closed as failed.
    database.clear()
    audit_log.clear()
    _write_fixed_artefact(artefact, [0.9, 0.8, 0.7], 0.5, {"model_version": "v-1"})
    extract_returns(raw_frame.iloc[:4])
    with pytest.raises(AssertionError, match=r"one score per transaction, in order"):
        P.predict(model_path=artefact)
    assert database == []
    violation = next(event for event, _ in audit_log if event.action == "invariant_violated")
    assert violation.details == {
        "invariant": "one score per transaction, in order",
        "holds": False,
        "rows": 4,
        "scores": 3,
    }
    failed = audit_log[-1][0]
    assert (failed.action, failed.reason) == ("run_failed", "step ml_predict raised AssertionError")


def test_main_configures_logging_then_scores_and_prints_the_count_and_a_bad_threshold_names_the_retraining(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "run")
    monkeypatch.setattr(P, "predict", lambda: calls.append("predict") or 12)

    P.main()

    assert calls == ["configure_logging", "new_run_id", "predict"]
    assert capsys.readouterr().out == "12 transaction(s) scored into gold.predictions.\n"

    # FORBIDDEN PATH: a threshold outside zero and one is refused with the command that retrains.
    with pytest.raises(ValueError, match=re.escape("retrain with `python -m banking_dataops.ml.train`.")):
        P.priority_band(0.5, 1.5)
