"""Tests for the date anchor of the generated batch (ADR-0009, decision 3).

The anchor decides every timestamp, therefore ``weekday_name`` and ``is_weekend``, therefore
two inputs of the model. These tests pin the reference value, the precedence between the
command line, the environment and the default, and the refusal of anything that is not a date.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from banking_dataops import generate_synthetic_data as dsd
from banking_dataops.audit import fingerprint_directory
from banking_dataops.generate_synthetic_data import (
    DEFAULT_GENERATED_AT,
    GENERATED_AT_ENV,
    SyntheticConfig,
    generate,
    parse_anchor,
    resolve_anchor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_FINGERPRINT = "89ac138fb9943f00"  # evidence/pipeline_full.txt, line "bronze_generated"


def _load_run_pipeline():
    """Import ``run_pipeline.py`` from the repository root, which is not on the import path."""
    spec = importlib.util.spec_from_file_location("run_pipeline_under_test", REPO_ROOT / "run_pipeline.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_pipeline_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _quiet_pipeline_module(monkeypatch: pytest.MonkeyPatch):
    """Load ``run_pipeline`` without letting ``main`` reconfigure the logging of the whole test session."""
    module = _load_run_pipeline()
    monkeypatch.setattr(module, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(module, "new_run_id", lambda: "test-run")
    return module


# ---------------------------------------------------------------------------
# The reference anchor, its resolution, and the refusal of anything that is not a date
# ---------------------------------------------------------------------------


def test_the_anchor_is_the_command_line_then_the_environment_then_the_reference_and_a_non_date_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reference: a Monday, and what applies when nothing is given.
    assert datetime(2026, 7, 27, 0, 0, tzinfo=UTC) == DEFAULT_GENERATED_AT
    assert DEFAULT_GENERATED_AT.strftime("%A") == "Monday"
    assert resolve_anchor(None, {}) == DEFAULT_GENERATED_AT

    # The command line beats the environment, which beats the default unless it is empty.
    assert resolve_anchor("2026-08-03", {GENERATED_AT_ENV: "2026-09-09"}) == datetime(2026, 8, 3, tzinfo=UTC)
    for environment, expected in [
        ({GENERATED_AT_ENV: "2026-09-09T00:00:00Z"}, datetime(2026, 9, 9, tzinfo=UTC)),
        # An unset ``${VAR:-}`` in a compose file arrives as an empty string: it counts as absent.
        ({GENERATED_AT_ENV: "  "}, DEFAULT_GENERATED_AT),
        ({}, DEFAULT_GENERATED_AT),
    ]:
        assert resolve_anchor(None, environment) == expected

    # Naive means UTC and offsets are converted: one instant, one set of bytes.
    for text in ["2026-07-27", "2026-07-27T00:00:00", "2026-07-27T00:00:00Z", "2026-07-27T02:00:00+02:00"]:
        assert parse_anchor(text) == DEFAULT_GENERATED_AT
        assert parse_anchor(text).utcoffset().total_seconds() == 0

    # Today is midnight UTC of the current day in UTC. On a machine whose zone is behind UTC the local day can still
    # be the previous one: at 23:30 on the 27th at UTC-5 it is already 04:30 on the 28th in UTC, and the anchor is
    # the 28th. A clock that reads the local time instead of UTC would answer the 27th, so the test does not wait for
    # the real clock to be in that situation: it sets the situation.
    class LateEveningAtMinusFive(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = datetime(2026, 7, 28, 4, 30, 45, 123456, tzinfo=UTC)
            if tz is None:
                return instant.astimezone(timezone(timedelta(hours=-5))).replace(tzinfo=None)
            return instant.astimezone(tz)

    with monkeypatch.context() as patch:
        patch.setattr(dsd, "datetime", LateEveningAtMinusFive)
        assert parse_anchor("today") == datetime(2026, 7, 28, 0, 0, tzinfo=UTC)

    # FORBIDDEN PATH: a malformed anchor silently replaced by the clock would reopen the defect.
    for text in ["", "not-a-date", "2026-13-45", "yesterday", "27/07/2026", "2026-07-27 25:61"]:
        with pytest.raises(ValueError, match="Invalid generation anchor"):
            parse_anchor(text)
    with pytest.raises(ValueError, match="Invalid generation anchor"):
        resolve_anchor(None, {GENERATED_AT_ENV: "garbage"})  # refused, not ignored
    with pytest.raises(ValueError, match="Invalid generation anchor"):
        resolve_anchor("garbage", {GENERATED_AT_ENV: "2026-09-09"})  # the stronger source is never ignored


# ---------------------------------------------------------------------------
# The generator command line
# ---------------------------------------------------------------------------


def test_the_generator_reproduces_the_reference_batch_never_reads_the_clock_and_writes_nothing_for_a_bad_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The anchor exists to reproduce evidence/pipeline_full.txt: same seed, same bytes.
    reference = tmp_path / "reference"
    generate(
        SyntheticConfig(
            customers=100,
            accounts_per_customer=2,
            transactions=1500,
            seed=42,
            generated_at=resolve_anchor(None, {}),
        ),
        reference,
    )
    assert fingerprint_directory(reference, "synthetic_*.csv")[:16] == REFERENCE_FINGERPRINT

    # With no flag and no environment variable the files carry the reference stamp, clock untouched.
    class NoClock(datetime):
        @classmethod
        def now(cls, tz=None):
            raise AssertionError("the generator read the wall clock")

    default_run = tmp_path / "default"
    monkeypatch.delenv(GENERATED_AT_ENV, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(dsd, "datetime", NoClock)
        dsd.main(["--output-dir", str(default_run), "--customers", "5", "--transactions", "20"])
    assert sorted(p.name for p in default_run.glob("*_20260727_000000.csv")) == [
        "accounts_20260727_000000.csv",
        "customers_20260727_000000.csv",
        "transactions_20260727_000000.csv",
    ]

    # FORBIDDEN PATH: an invalid anchor, from the command line or from the environment, stops the run before
    # anything is written.
    refused = tmp_path / "refused"
    refused.mkdir()
    with pytest.raises(ValueError, match="Invalid generation anchor"):
        dsd.main(["--output-dir", str(refused), "--generated-at", "garbage"])
    assert list(refused.iterdir()) == []
    monkeypatch.setenv(GENERATED_AT_ENV, "garbage")
    with pytest.raises(ValueError, match="Invalid generation anchor"):
        dsd.main(["--output-dir", str(refused)])
    assert list(refused.iterdir()) == []


# ---------------------------------------------------------------------------
# The pipeline command line
# ---------------------------------------------------------------------------


def test_run_pipeline_exits_2_for_an_invalid_anchor_before_any_step_and_before_it_reads_the_settings(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: running the pipeline on an unknown date, or reaching the database configuration with it.

    Exit code 2, no step started. ``main`` reports a configuration error whatever raised it, so the message tells
    the two causes apart: if the settings were read first, the message would be theirs and not the anchor's.
    """
    module = _quiet_pipeline_module(monkeypatch)
    started: list[str] = []
    monkeypatch.setattr(module, "run", lambda *args, **kwargs: started.append("run"))

    def settings_must_not_be_read() -> None:
        raise AssertionError("settings were read before the anchor was validated")

    monkeypatch.setattr(module, "load_settings", settings_must_not_be_read)
    monkeypatch.delenv(GENERATED_AT_ENV, raising=False)

    exit_code = module.main(["--generated-at", "garbage"])
    printed = capsys.readouterr().out
    assert exit_code == 2
    assert started == []
    assert "Configuration error" in printed
    assert "Invalid generation anchor" in printed
    assert "settings were read" not in printed

    # The same refusal for an invalid anchor that comes from the environment.
    monkeypatch.setenv(GENERATED_AT_ENV, "garbage")
    exit_code = module.main([])
    assert exit_code == 2
    assert started == []
    assert "Invalid generation anchor" in capsys.readouterr().out
