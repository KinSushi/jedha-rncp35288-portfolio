"""The dashboard read side (`banking_dataops.monitoring`): exact SQL, limits, traces, verdict counts.

The twelve queries are compared word for word with ``tests/golden/monitoring_queries.json``: a dashboard panel
that silently reads another column or another table is a wrong number in front of an audience, and a
review of the JSON file is the only place such a change should pass. The database is the recording fake of
``tests/fakes.py``.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pytest
from fakes import FakeDatabase

from banking_dataops import monitoring
from banking_dataops.config import Settings
from banking_dataops.monitoring import QualitySummary, _traced, summarize_quality_status

GOLDEN = json.loads((Path(__file__).parent / "golden" / "monitoring_queries.json").read_text(encoding="utf-8"))

#: Every loader, the call that reaches it, and whether it takes the settings.
LOADERS: dict[str, tuple[Callable[[Settings], pd.DataFrame], bool]] = {
    "load_latest_quality_results": (lambda s: monitoring.load_latest_quality_results(s), True),
    "load_reconciliation_summary": (lambda s: monitoring.load_reconciliation_summary(s), True),
    "load_transaction_volume": (lambda s: monitoring.load_transaction_volume(s), True),
    "load_status_summary": (lambda s: monitoring.load_status_summary(s), True),
    "load_channel_summary": (lambda s: monitoring.load_channel_summary(s), True),
    "load_risk_heatmap": (lambda s: monitoring.load_risk_heatmap(s), True),
    "load_volume_trend": (lambda s: monitoring.load_volume_trend(s), True),
    "load_segment_cohorts": (lambda s: monitoring.load_segment_cohorts(s), True),
    "load_control_trend": (lambda s: monitoring.load_control_trend(s), True),
    "load_prediction_summary": (lambda s: monitoring.load_prediction_summary(), False),
    "load_suspicious_transactions": (lambda s: monitoring.load_suspicious_transactions(7, s), True),
    "load_top_predictions": (lambda s: monitoring.load_top_predictions(9), False),
}
TRACED = {
    "load_risk_heatmap": "risk concentration by hour and channel, read by the dashboard heatmap",
    "load_volume_trend": "daily volume and rolling average, used to spot a partially delivered feed",
    "load_segment_cohorts": "risk cohorts by customer segment and merchant category",
    "load_control_trend": "control pass rate over time, used to catch slow degradation",
}


def _flat(sql: str) -> str:
    return " ".join(sql.split())


# ---------------------------------------------------------------- the queries, word for word


@pytest.mark.parametrize("name", sorted(LOADERS))
def test_each_loader_sends_its_golden_query_once_reads_only_and_leaves_the_database_unchanged(
    fake_database: FakeDatabase, settings: Settings, name: str
) -> None:
    """A dashboard render never writes: one SELECT, no commit, nothing for the next render to find changed."""
    call, takes_settings = LOADERS[name]

    call(settings)

    assert [_flat(sql) for sql in fake_database.sql_sent()] == [GOLDEN[name]]
    assert fake_database.sql_sent()[0].lstrip().upper().startswith(("SELECT", "WITH"))
    assert fake_database.commits == 0
    if takes_settings:
        assert fake_database.dsns == [settings.dsn]


def test_the_limits_are_written_as_numbers_and_anything_that_is_not_a_number_is_refused_before_the_sql_text(
    fake_database: FakeDatabase, settings: Settings
) -> None:
    """FORBIDDEN PATH: an injection through ``LIMIT``, which is interpolated into the text instead of bound.

    The ``int(...)`` cast refuses anything that is not a number; no statement reaches the database.
    """
    monitoring.load_suspicious_transactions()
    monitoring.load_suspicious_transactions("12")  # type: ignore[arg-type]
    monitoring.load_top_predictions()
    sent = [_flat(sql) for sql in fake_database.sql_sent()]
    assert sent == [
        GOLDEN["load_suspicious_transactions"].replace("LIMIT 7", "LIMIT 50"),
        GOLDEN["load_suspicious_transactions"].replace("LIMIT 7", "LIMIT 12"),
        GOLDEN["load_top_predictions"].replace("LIMIT 9", "LIMIT 15"),
    ]

    for loader in (monitoring.load_suspicious_transactions, monitoring.load_top_predictions):
        for hostile in ("5; DROP TABLE transactions", "1 OR 1=1", "ten", None):
            with pytest.raises((ValueError, TypeError)):
                loader(hostile)  # type: ignore[arg-type]

    assert len(fake_database.statements) == len(sent)


# ---------------------------------------------------------------- the traces


def test_a_traced_query_logs_its_row_count_at_debug_and_an_empty_one_at_warning_and_returns_the_same_frame(
    fake_database: FakeDatabase, log_records: list, settings: Settings
) -> None:
    frame = pd.DataFrame({"x": [1, 2, 3]})
    assert _traced("load_demo", frame, reason="a demonstration panel") is frame
    (record,) = [r for r in log_records if r.name == "banking_dataops.monitoring"]
    assert (record.levelname, record.getMessage()) == ("DEBUG", "load_demo")
    assert (record.category, record.reason, record.rows) == ("data", "a demonstration panel", 3)

    log_records.clear()
    empty = pd.DataFrame({"x": []})
    assert _traced("load_demo", empty, reason="a demonstration panel") is empty
    warning = next(r for r in log_records if r.levelname == "WARNING")
    assert warning.getMessage() == "load_demo_empty"
    assert (warning.category, warning.rows) == ("data", 0)
    assert warning.reason == (
        "query returned no rows; either the layer is not built yet or the query no longer matches the schema"
    )

    for name, reason in TRACED.items():
        log_records.clear()
        fake_database.answer("FROM gold.", rows=[(1, 2)], columns=["a", "b"])
        assert LOADERS[name][0](settings).to_dict(orient="records") == [{"a": 1, "b": 2}]
        (traced,) = [r for r in log_records if r.name == "banking_dataops.monitoring"]
        assert (traced.getMessage(), traced.reason, traced.rows) == (name, reason, 1)
        assert [r for r in log_records if r.levelname == "WARNING"] == []

        log_records.clear()
        fake_database.answers.clear()
        fake_database.answer("FROM gold.", rows=[], columns=["a", "b"])
        assert LOADERS[name][0](settings).empty
        assert [r.getMessage() for r in log_records if r.levelname == "WARNING"] == [f"{name}_empty"]
        fake_database.answers.clear()


# ---------------------------------------------------------------- the verdict counts


def test_the_counts_ignore_case_a_failure_outranks_a_warning_and_a_summary_cannot_be_edited() -> None:
    summary = summarize_quality_status(["pass", "Pass", "WARN", "fail", "SKIPPED"])
    assert summary == QualitySummary(total_checks=5, passed_checks=2, failed_checks=1, warning_checks=1)
    empty = summarize_quality_status([])
    assert (empty.total_checks, empty.overall_status) == (0, "PASS")
    assert QualitySummary(total_checks=2, passed_checks=2, failed_checks=0).warning_checks == 0

    for statuses, expected in (
        (["PASS", "WARN", "FAIL"], "FAIL"),
        (["WARN", "WARN"], "WARN"),
        (["PASS", "WARN"], "WARN"),
        (["PASS"], "PASS"),
        (["UNKNOWN"], "PASS"),
    ):
        assert summarize_quality_status(statuses).overall_status == expected

    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.failed_checks = 0  # type: ignore[misc]


# ---------------------------------------------------------------- the model metrics


def test_the_model_metrics_are_read_as_utf_8_from_the_models_folder_and_a_missing_or_unreadable_file_gives_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module_file = tmp_path / "src" / "banking_dataops" / "monitoring.py"
    module_file.parent.mkdir(parents=True)
    monkeypatch.setattr(monitoring, "__file__", str(module_file))
    models = tmp_path / "models"
    models.mkdir()

    assert monitoring.load_model_metrics() == {}

    (models / "metrics.json").write_text('{"model_version": "v1", "roc_auc": 0.97}', encoding="utf-8")
    read: list[tuple[str, str | None]] = []
    original = Path.read_text

    def spy(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        read.append((self.name, encoding))
        return original(self, encoding=encoding, errors=errors)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", spy)
        assert monitoring.load_model_metrics() == {"model_version": "v1", "roc_auc": 0.97}
    assert read == [("metrics.json", "utf-8")]

    for content in ("", "{not json", "\x00\x01"):
        (models / "metrics.json").write_text(content, encoding="utf-8")
        assert monitoring.load_model_metrics() == {}
