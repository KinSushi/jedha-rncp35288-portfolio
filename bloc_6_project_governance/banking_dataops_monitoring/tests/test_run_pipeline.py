"""The pipeline entry point (``run_pipeline.py``): order of the steps, exit codes, what a failed step stops.

The four steps are replaced by recorders, except ``step_bronze`` which runs for real on a small batch in a
temporary folder. The tests read the exit code (0 success, 1 a step failed, 2 a configuration error) and
check that a failure stops everything after it.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from banking_dataops import audit
from banking_dataops.generate_synthetic_data import GENERATED_AT_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]
ANCHOR = datetime(2026, 7, 27, tzinfo=UTC)


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """``run_pipeline`` loaded from the repository root, without reconfiguring the logging of the session."""
    spec = importlib.util.spec_from_file_location("run_pipeline_steps", REPO_ROOT / "run_pipeline.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_pipeline_steps"] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(module, "new_run_id", lambda: "test-run")
    monkeypatch.delenv(GENERATED_AT_ENV, raising=False)
    return module


@pytest.fixture
def steps(pipeline: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the four steps by recorders returning their own name; the list shows the order they ran in."""
    ran: list[str] = []

    def make(name: str, rows: int):  # type: ignore[no-untyped-def]
        def step(*args: object, **kwargs: object) -> object:
            ran.append(name)
            return pipeline.StepResult(name, rows, 5)

        return step

    monkeypatch.setattr(pipeline, "step_bronze", make("bronze", 1500))
    monkeypatch.setattr(pipeline, "step_cleaning", make("cleaning", 1490))
    monkeypatch.setattr(pipeline, "step_silver", make("silver", 1490))
    monkeypatch.setattr(pipeline, "step_gold", make("gold", 1490))
    monkeypatch.setattr(pipeline, "load_settings", lambda: object())
    return ran


# ---------------------------------------------------------------- the command line


def test_the_defaults_are_the_reference_batch_without_training(pipeline: ModuleType) -> None:
    args = pipeline.parse_args([])

    assert (args.seed, args.transactions, args.customers) == (42, 1500, 100)
    assert (args.generated_at, args.train, args.skip_ml, args.verify, args.log_format) == (
        None,
        False,
        False,
        False,
        None,
    )


def test_every_option_is_read(pipeline: ModuleType) -> None:
    args = pipeline.parse_args(
        ["--seed", "7", "--transactions", "20", "--customers", "4", "--generated-at", "today", "--train",
         "--skip-ml", "--verify", "--log-format", "json"]
    )  # fmt: skip

    assert (args.seed, args.transactions, args.customers, args.generated_at) == (7, 20, 4, "today")
    assert (args.train, args.skip_ml, args.verify, args.log_format) == (True, True, True, "json")


@pytest.mark.parametrize(
    "argv", [["--seed", "forty-two"], ["--log-format", "xml"], ["--transactions", "1.5"], ["--unknown"], ["--seed"]]
)
def test_an_option_that_cannot_be_read_stops_the_parser_with_exit_code_2(pipeline: ModuleType, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        pipeline.parse_args(argv)

    assert raised.value.code == 2


# ---------------------------------------------------------------- the order of the steps


def test_the_four_steps_run_in_dependency_order_and_main_exits_with_zero(
    pipeline: ModuleType, steps: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = pipeline.main(["--generated-at", "2026-07-27"])

    assert exit_code == 0
    assert steps == ["bronze", "cleaning", "silver", "gold"]
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Pipeline completed successfully (anchor 2026-07-27T00:00:00Z)."
    assert out[1:] == [
        "  bronze           1500 rows       5 ms",
        "  cleaning         1490 rows       5 ms",
        "  silver           1490 rows       5 ms",
        "  gold             1490 rows       5 ms",
    ]


def test_the_bronze_step_receives_the_seed_the_sizes_and_the_resolved_anchor(
    pipeline: ModuleType, monkeypatch: pytest.MonkeyPatch, steps: list[str]
) -> None:
    received: list[tuple[object, ...]] = []

    def bronze(seed: int, transactions: int, customers: int, generated_at: datetime) -> object:
        received.append((seed, transactions, customers, generated_at))
        return pipeline.StepResult("bronze", transactions, 1)

    monkeypatch.setattr(pipeline, "step_bronze", bronze)

    pipeline.main(["--seed", "7", "--transactions", "20", "--customers", "4", "--generated-at", "2026-08-03T06:45:00Z"])

    assert received == [(7, 20, 4, datetime(2026, 8, 3, 6, 45, tzinfo=UTC))]


@pytest.mark.parametrize(
    ("argv", "trains"),
    [([], False), (["--train"], True), (["--train", "--skip-ml"], False), (["--skip-ml"], False)],
)
def test_the_model_is_trained_only_when_asked_and_not_skipped(
    pipeline: ModuleType, steps: list[str], monkeypatch: pytest.MonkeyPatch, argv: list[str], trains: bool
) -> None:
    from types import SimpleNamespace

    from banking_dataops.ml import train as train_module

    report = SimpleNamespace(
        rows_total=1500, model_name="suspicion_classifier", model_family="random_forest", roc_auc=0.97654
    )
    monkeypatch.setattr(train_module, "train", lambda: report)

    results = pipeline.run(pipeline.parse_args(argv), ANCHOR)

    assert [r.name for r in results] == (["bronze", "cleaning", "silver", "gold"] + (["ml_train"] if trains else []))
    if trains:
        assert results[-1].rows == 1500
        assert results[-1].detail == "suspicion_classifier random_forest test_roc_auc=0.9765"


def test_the_whole_pipeline_runs_under_one_journal_with_the_seed(
    pipeline: ModuleType, steps: list[str], audit_log: list[tuple[audit.AuditEvent, object]]
) -> None:
    pipeline.run(pipeline.parse_args(["--seed", "7"]), ANCHOR)

    started, succeeded = (event for event, _ in audit_log)
    assert (started.action, started.reason, started.details["seed"]) == ("run_started", "step pipeline began", 7)
    assert (succeeded.action, succeeded.reason) == ("run_succeeded", "step pipeline completed without error")


# ---------------------------------------------------------------- what stops the run


def test_a_failing_step_stops_the_steps_after_it_and_main_exits_with_one(
    pipeline: ModuleType, steps: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FORBIDDEN PATH: building Gold on a Silver layer that failed to load."""

    def silver_fails() -> object:
        steps.append("silver")
        raise RuntimeError("connection refused")

    monkeypatch.setattr(pipeline, "step_silver", silver_fails)

    exit_code = pipeline.main(["--generated-at", "2026-07-27"])

    assert exit_code == 1
    assert steps == ["bronze", "cleaning", "silver"]
    out = capsys.readouterr().out
    assert out == "FAILED: RuntimeError: connection refused\n"


def test_a_failing_step_is_journalled_as_a_failed_run(
    pipeline: ModuleType,
    steps: list[str],
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[tuple[audit.AuditEvent, object]],
) -> None:
    def cleaning_fails() -> object:
        raise ValueError("bad row")

    monkeypatch.setattr(pipeline, "step_cleaning", cleaning_fails)

    assert pipeline.main(["--generated-at", "2026-07-27"]) == 1

    assert audit_log[-1][0].reason == "step pipeline raised ValueError"
    assert steps == ["bronze"]


def test_an_invalid_anchor_exits_with_two_before_any_step_and_prints_the_reason(
    pipeline: ModuleType, steps: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = pipeline.main(["--generated-at", "not a date"])

    assert exit_code == 2
    assert steps == []
    assert capsys.readouterr().out.startswith("Configuration error: Invalid generation anchor 'not a date'")


def test_unreadable_settings_exit_with_two_before_any_step(
    pipeline: ModuleType, steps: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise ValueError("database_port: not a number")

    monkeypatch.setattr(pipeline, "load_settings", broken)

    assert pipeline.main(["--generated-at", "2026-07-27"]) == 2
    assert steps == []
    assert capsys.readouterr().out == "Configuration error: database_port: not a number\n"


# ---------------------------------------------------------------- the steps themselves


def test_the_bronze_step_writes_the_batch_and_journals_its_fingerprint_and_anchor(
    pipeline: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[tuple[audit.AuditEvent, object]],
) -> None:
    monkeypatch.chdir(tmp_path)

    result = pipeline.step_bronze(7, 30, 4, ANCHOR)

    assert (result.name, result.rows) == ("bronze", 30)
    assert result.duration_ms >= 0
    assert (tmp_path / "data" / "synthetic_transactions.csv").is_file()
    event = next(e for e, _ in audit_log if e.action == "bronze_generated")
    assert (event.category, event.row_count) == ("data", 30)
    assert event.reason == "raw layer produced deterministically from the seed and the date anchor"
    assert event.details["seed"] == 7
    assert event.details["generated_at"] == "2026-07-27T00:00:00+00:00"
    assert event.details["fingerprint"] == audit.fingerprint_directory(tmp_path / "data", "synthetic_*.csv")[:16]


def test_the_cleaning_silver_and_gold_steps_report_the_rows_they_are_accountable_for(
    pipeline: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from banking_dataops import ingest, quality_checks
    from banking_dataops.pipeline import cleaning, gold

    called: list[str] = []
    monkeypatch.setattr(cleaning, "clean_bronze_to_silver", lambda bronze, silver: SimpleNamespace(rows_out=1490))
    monkeypatch.setattr(ingest, "ingest_all", lambda: called.append("ingest"))
    monkeypatch.setattr(quality_checks, "run_quality_checks", lambda: called.append("controls"))
    monkeypatch.setattr(pipeline, "fetch_rows", lambda query: [{"n": 1490}])
    monkeypatch.setattr(gold, "build", lambda: {"gold.dim_date": 31, "gold.fact_transactions": 1490})

    assert pipeline.step_cleaning().rows == 1490
    assert pipeline.step_silver().rows == 1490
    assert called == ["ingest", "controls"]
    assert pipeline.step_gold().rows == 1490


def test_the_gold_step_reports_zero_rows_when_the_fact_table_is_missing_from_the_counts(
    pipeline: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from banking_dataops.pipeline import gold

    monkeypatch.setattr(gold, "build", lambda: {"gold.dim_date": 31})

    assert pipeline.step_gold().rows == 0
