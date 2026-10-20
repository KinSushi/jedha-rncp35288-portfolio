"""The end-to-end acceptance script (``scripts/smoke_check.py``): fifteen checks, one exit code.

The script runs against a live stack in the real project. Here the database is replaced by a function that
answers each query from a table of counts, so that every check can be made to fail in turn and the exit code
read: 0 only when all fifteen hold, 1 as soon as one does not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from banking_dataops import db

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_check.py"

#: Each query of the script, recognised by a fragment of its text, with the value a healthy stack answers.
HEALTHY: dict[str, int] = {
    "FROM transactions": 800,
    "FROM accounts": 120,
    "FROM customers": 60,
    "FROM quality_check_results WHERE status = 'FAIL'": 0,
    "FROM quality_check_results": 7,
    "FROM gold.fact_transactions": 800,
    "FROM gold.dim_date": 4,
    "FROM gold.predictions WHERE suspicion_score": 0,
    "FROM gold.predictions": 800,
    "WHERE reason IS NULL": 0,
    "action = 'invariant_violated'": 0,
    "FROM audit_events": 1200,
}


@pytest.fixture
def smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The script as a module, with a model file present and an empty list of results."""
    saved = list(sys.path)
    spec = importlib.util.spec_from_file_location("smoke_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.path[:] = saved
    model = tmp_path / "suspicion_classifier.joblib"
    model.write_bytes(b"model")
    monkeypatch.setattr(module, "MODEL_PATH", model)
    monkeypatch.setattr(module, "_results", [])
    return module


def _answers(monkeypatch: pytest.MonkeyPatch, overrides: dict[str, int | None] | None = None) -> list[str]:
    """Replace ``fetch_rows``: the first fragment found in the query decides the answer. Return the queries seen."""
    values = {**HEALTHY, **(overrides or {})}
    seen: list[str] = []

    def fetch_rows(query: str, settings: Any = None, params: Any = None) -> list[dict[str, object]]:
        seen.append(query)
        for fragment, value in values.items():
            if fragment in query:
                return [] if value is None else [{"n": value}]
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(db, "fetch_rows", fetch_rows)
    return seen


def test_a_healthy_stack_passes_all_fifteen_checks_and_exits_with_zero(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch)

    exit_code = smoke.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "  PASS  transactions loaded  (800 rows)" in out
    assert "  PASS  gold matches silver exactly  (gold 800 vs silver 800)" in out
    assert "DEMO READY  -  15 checks passed" in out
    assert "FAIL" not in out


@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("transactions loaded", {"FROM transactions": 0}),
        ("accounts loaded", {"FROM accounts": 0}),
        ("customers loaded", {"FROM customers": 0}),
        ("7 controls persisted", {"FROM quality_check_results": 6}),
        ("no control in failure", {"FROM quality_check_results WHERE status = 'FAIL'": 1}),
        ("gold.fact_transactions populated", {"FROM gold.fact_transactions": 0}),
        ("gold matches silver exactly", {"FROM gold.fact_transactions": 799}),
        ("all 4 gold dimensions populated", {"FROM gold.dim_date": 3}),
        ("gold.predictions populated", {"FROM gold.predictions": 0}),
        ("every suspicion score within [0, 1]", {"FROM gold.predictions WHERE suspicion_score": 2}),
        ("audit journal populated", {"FROM audit_events": 0}),
        ("every event carries a business reason", {"WHERE reason IS NULL": 1}),
        ("no invariant was violated", {"action = 'invariant_violated'": 1}),
    ],
)
def test_one_broken_check_makes_the_script_exit_with_one_and_name_the_check(
    smoke: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    label: str,
    overrides: dict[str, int | None],
) -> None:
    """FORBIDDEN PATH: reporting 'demo ready' while one layer of the stack is empty, incomplete or corrupt."""
    _answers(monkeypatch, overrides)

    exit_code = smoke.main()

    out = capsys.readouterr().out
    assert exit_code == 1
    assert [line for line in out.splitlines() if line.startswith("  FAIL")][0].split("  ")[2] == label
    assert "NOT READY" in out
    assert "DEMO READY" not in out


def test_a_missing_model_artefact_is_a_failed_check_with_the_remedy(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _answers(monkeypatch)
    monkeypatch.setattr(smoke, "MODEL_PATH", tmp_path / "absent.joblib")

    exit_code = smoke.main()

    assert exit_code == 1
    assert "  FAIL  model artefact present  - train it first" in capsys.readouterr().out


def test_a_missing_table_is_one_failed_check_not_a_stack_trace_that_hides_the_others(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(query: str, settings: Any = None, params: Any = None) -> list[dict[str, object]]:
        raise RuntimeError('relation "gold.predictions" does not exist')

    monkeypatch.setattr(db, "fetch_rows", broken)

    exit_code = smoke.main()

    out = capsys.readouterr().out
    assert exit_code == 1
    assert out.count("  FAIL  ") + out.count("  PASS  ") == 15
    assert "  FAIL  transactions loaded  (-1 rows)" in out
    assert "NOT READY" in out


def test_the_summary_banner_counts_failures_and_passes(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _answers(monkeypatch, {"FROM accounts": 0, "FROM customers": 0})

    smoke.main()

    assert "NOT READY  -  2 failure(s), 13 passed" in capsys.readouterr().out


def test_the_verdict_independence_check_refuses_a_verdict_that_reads_the_model(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FORBIDDEN PATH: a model score reaching the publication decision (ADR-0002)."""
    from banking_dataops import api

    _answers(monkeypatch)

    def verdict() -> None:
        return "SELECT suspicion_score FROM gold.predictions"  # type: ignore[return-value]

    monkeypatch.setattr(api, "verdict", verdict)

    exit_code = smoke.main()

    assert exit_code == 1
    assert "  FAIL  verdict does not read model predictions (ADR-0002)" in capsys.readouterr().out


# ---------------------------------------------------------------- the helpers


def test_scalar_returns_the_first_value_of_the_first_row(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "fetch_rows", lambda query, settings=None, params=None: [{"n": "42", "other": 1}])

    assert smoke.scalar("SELECT 42 AS n") == 42


@pytest.mark.parametrize("default", [-1, 0, 99])
def test_scalar_gives_the_default_when_there_is_no_row_or_the_query_fails(
    smoke: ModuleType, monkeypatch: pytest.MonkeyPatch, default: int
) -> None:
    monkeypatch.setattr(db, "fetch_rows", lambda query, settings=None, params=None: [])
    assert smoke.scalar("SELECT 1", default) == default

    def broken(query: str, settings: Any = None, params: Any = None) -> list[dict[str, object]]:
        raise ConnectionError("down")

    monkeypatch.setattr(db, "fetch_rows", broken)
    assert smoke.scalar("SELECT 1", default) == default


def test_the_default_of_scalar_is_minus_one(smoke: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "fetch_rows", lambda query, settings=None, params=None: [])

    assert smoke.scalar("SELECT 1") == -1


def test_check_records_and_prints_its_outcome(smoke: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    assert smoke.check("holds", True) is True
    assert smoke.check("broken", False, "(0 rows)") is False

    assert capsys.readouterr().out == "  PASS  holds\n  FAIL  broken  (0 rows)\n"


def test_step_prints_a_titled_section_header(smoke: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    smoke.step("1. Silver layer")

    assert capsys.readouterr().out == "\n==> 1. Silver layer\n"


def test_colours_are_off_when_the_output_is_not_a_terminal(smoke: ModuleType) -> None:
    assert (smoke.GREEN, smoke.RED, smoke.YELLOW, smoke.OFF) == ("", "", "", "")
