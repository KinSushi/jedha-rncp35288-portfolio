"""The logging layer (``banking_dataops.logging_config``): run correlation, the two formats, the safe handler.

``tests/test_reverse_paths.py`` already proves that a broken sink never raises and that configuring twice does
not duplicate lines. These tests pin the exact shape of what is written, the way the level and the format are
chosen, and what the configuration refuses.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from banking_dataops import logging_config
from banking_dataops.logging_config import (
    HumanFormatter,
    JsonFormatter,
    RunContextFilter,
    SafeHandler,
    _extra_fields,
    configure_logging,
    current_run_id,
    current_step,
    get_logger,
    new_run_id,
    set_run_id,
    set_step,
)

CREATED = datetime(2026, 7, 27, 9, 14, 5, tzinfo=UTC).timestamp()


@pytest.fixture
def restored_logger() -> Iterator[logging.Logger]:
    """Give the test the project logger and put its level, handlers and propagation back afterwards."""
    logger = logging.getLogger("banking_dataops")
    saved = (logger.level, list(logger.handlers), logger.propagate)
    try:
        yield logger
    finally:
        logger.setLevel(saved[0])
        logger.handlers[:] = saved[1]
        logger.propagate = saved[2]


@pytest.fixture
def no_logging_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BANKING_DATAOPS_LOG_LEVEL", raising=False)
    monkeypatch.delenv("BANKING_DATAOPS_LOG_FORMAT", raising=False)


def _record(message: str = "hello", level: int = logging.INFO, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("banking_dataops.audit", level, "/src/audit.py", 42, message, None, None)
    record.created = CREATED
    record.run_id = "0123456789abcdef"
    record.step = "ingest"
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _padded() -> logging.LogRecord:
    record = _record("started", level=logging.WARNING)
    record.run_id = ""
    record.step = ""
    return record


def _with_arguments() -> logging.LogRecord:
    record = _record("%d rows loaded")
    record.args = (12,)
    return record


def _bare_record() -> logging.LogRecord:
    """A record that never went through the correlation filter: it has no ``run_id`` and no ``step``."""
    record = logging.LogRecord("banking_dataops.audit", logging.INFO, "/src/audit.py", 42, "hello", None, None)
    record.created = CREATED
    return record


# ---------------------------------------------------------------- run correlation


def test_a_run_gets_a_sixteen_hex_identifier_a_step_and_a_filter_stamps_both_on_every_record() -> None:
    first = new_run_id()
    assert current_run_id() == first
    second = new_run_id()
    assert first != second
    assert current_run_id() == second
    for run_id in (first, second):
        assert re.fullmatch(r"[0-9a-f]{16}", run_id)

    set_run_id("feedfacefeedface")
    assert current_run_id() == "feedfacefeedface"
    set_step("cleaning")
    assert current_step() == "cleaning"
    set_step("ml_train")
    assert current_step() == "ml_train"
    record = logging.LogRecord("x", logging.INFO, "f.py", 1, "m", None, None)
    assert RunContextFilter().filter(record) is True
    assert (record.run_id, record.step) == ("feedfacefeedface", "ml_train")  # type: ignore[attr-defined]

    assert get_logger().name == "banking_dataops"
    assert get_logger("audit").name == "banking_dataops.audit"
    assert get_logger("audit").parent is logging.getLogger("banking_dataops")


# ---------------------------------------------------------------- formats


@pytest.mark.parametrize(
    ("make_record", "expected"),
    [
        (
            lambda: _record("tables_reset", category="data", rows=3),
            "09:14:05  INFO    [run:01234567] ingest       tables_reset  category=data rows=3",
        ),
        (_padded, "09:14:05  WARNING [run:--------] -            started"),
        (_bare_record, "09:14:05  INFO    [run:--------] -            hello"),
        (_with_arguments, "09:14:05  INFO    [run:01234567] ingest       12 rows loaded"),
    ],
)
def test_the_human_line_shows_time_level_short_run_step_message_and_extras_padded_when_absent(
    make_record: Callable[[], logging.LogRecord], expected: str
) -> None:
    assert HumanFormatter().format(make_record()) == expected


def test_the_json_line_is_one_object_with_the_fixed_keys_in_order_then_the_extras_and_the_exception_text() -> None:
    line = JsonFormatter().format(_record("tables_reset", category="data", rows=3, name_fr="Zürich"))
    payload = json.loads(line)
    assert list(payload) == [
        "timestamp",
        "level",
        "logger",
        "run_id",
        "step",
        "message",
        "module",
        "line",
        "category",
        "rows",
        "name_fr",
    ]
    assert payload["timestamp"] == "2026-07-27T09:14:05+00:00"
    assert (payload["level"], payload["logger"]) == ("INFO", "banking_dataops.audit")
    assert (payload["run_id"], payload["step"], payload["message"]) == ("0123456789abcdef", "ingest", "tables_reset")
    assert (payload["module"], payload["line"]) == ("audit", 42)
    assert "Zürich" in line
    assert "\n" not in line

    # Extras: only what the caller attached; an unserialisable value is kept as text.
    assert _extra_fields(_record(category="data", rows=3)) == {"category": "data", "rows": 3}
    exotic = json.loads(JsonFormatter().format(_record("x", when=datetime(2026, 7, 27, tzinfo=UTC))))
    assert exotic["when"] == "2026-07-27 00:00:00+00:00"

    # A record that never met the correlation filter carries empty strings, not an error.
    bare = json.loads(JsonFormatter().format(_bare_record()))
    assert (bare["run_id"], bare["step"]) == ("", "")

    # The exception text appears only when there is one.
    try:
        raise ValueError("boom")
    except ValueError:
        failing = _record("failed")
        failing.exc_info = sys.exc_info()
    with_exception = json.loads(JsonFormatter().format(failing))
    assert with_exception["exception"].startswith("Traceback (most recent call last):")
    assert with_exception["exception"].rstrip().endswith("ValueError: boom")
    assert "exception" not in json.loads(JsonFormatter().format(_record("fine")))


# ---------------------------------------------------------------- the safe handler


def test_a_dropped_record_leaves_one_line_on_stderr_and_a_broken_stderr_does_not_stop_the_handler(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """FORBIDDEN PATH: the report of a logging failure becoming a second failure."""
    SafeHandler(stream=io.StringIO()).handleError(_record())
    assert capsys.readouterr().err == "[logging] dropped a record from banking_dataops.audit\n"

    class Broken:
        def write(self, _text: str) -> None:
            raise OSError("closed")

    monkeypatch.setattr(sys, "stderr", Broken())
    SafeHandler(stream=io.StringIO()).handleError(_record())


# ---------------------------------------------------------------- configuration


def test_the_level_and_the_format_come_from_the_arguments_then_the_environment_and_an_unknown_level_is_refused(
    restored_logger: logging.Logger,
    monkeypatch: pytest.MonkeyPatch,
    no_logging_environment: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FORBIDDEN PATH: a typo in the level silently turning logging off or to a default nobody chose."""
    # By default: INFO, human readable, on standard output, no propagation, one filter.
    logger = configure_logging()
    assert logger is restored_logger
    assert logger.name == "banking_dataops"
    assert logger.level == logging.INFO
    assert logger.propagate is False
    (handler,) = logger.handlers
    assert isinstance(handler, SafeHandler)
    assert handler.stream is sys.stdout
    assert isinstance(handler.formatter, HumanFormatter)
    assert [type(f) for f in handler.filters] == [RunContextFilter]

    # Configuring again replaces the handler instead of adding one.
    configure_logging()
    assert restored_logger.handlers != [handler]
    assert [type(h) for h in restored_logger.handlers] == [SafeHandler]

    # The environment, then the arguments.
    monkeypatch.setenv("BANKING_DATAOPS_LOG_LEVEL", "debug")
    monkeypatch.setenv("BANKING_DATAOPS_LOG_FORMAT", "JSON")
    from_environment = configure_logging()
    assert from_environment.level == logging.DEBUG
    assert isinstance(from_environment.handlers[0].formatter, JsonFormatter)
    from_arguments = configure_logging(level="warning", fmt="Human")
    assert from_arguments.level == logging.WARNING
    assert isinstance(from_arguments.handlers[0].formatter, HumanFormatter)
    assert isinstance(configure_logging(fmt="yaml").handlers[0].formatter, HumanFormatter)

    # A record logged afterwards reaches standard output with the current run.
    monkeypatch.delenv("BANKING_DATAOPS_LOG_LEVEL")
    monkeypatch.delenv("BANKING_DATAOPS_LOG_FORMAT")
    set_run_id("0123456789abcdef")
    set_step("ingest")
    configure_logging()
    get_logger("ingest").info("tables_reset", extra={"rows": 3})
    assert "[run:01234567] ingest       tables_reset  rows=3" in capsys.readouterr().out
    assert logging_config.LOGGER_NAME == "banking_dataops"

    # An unknown level is refused and the logger keeps its previous handlers.
    before = list(restored_logger.handlers)
    with pytest.raises(ValueError, match="Unknown level: 'LOUD'"):
        configure_logging(level="loud")
    assert restored_logger.handlers == before
    assert restored_logger.level == logging.INFO
