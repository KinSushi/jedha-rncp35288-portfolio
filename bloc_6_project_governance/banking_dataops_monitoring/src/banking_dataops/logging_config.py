"""Structured logging with run correlation.

A control that fails without leaving a trace is worse than no control: it produces a
decision nobody can justify afterwards. Every step must say, in a machine-readable form,
what it did, on how many rows, and under which run.

CHOIX
-----
Retained:

- Two output formats over one record structure: ``human`` (one readable line per event, for a
  terminal and a live demonstration) and ``json`` (one object per line, for audit and log
  shipping). Chosen by ``BANKING_DATAOPS_LOG_FORMAT`` or by the ``fmt`` argument.
- A run identifier carried in a ``contextvars`` variable instead of a parameter threaded
  through every function: the correlation must cross modules that never call each other (the
  generator does not call the quality checks, yet both belong to one run).
- The correlation is added by a filter on the handler (``RunContextFilter``), so no caller has
  to remember to use a special logger object.
- ``SafeHandler`` swallows logging errors: a broken pipe or an unserialisable object must not
  abort a data pipeline.
- Logging is configured once, by the entry point (``main()``); a library function never
  configures it.

Rejected:

- A new identifier from a timestamp: two runs can start within the same second (a retry
  after a failure) and would merge two audit trails; ``new_run_id`` uses 16 characters of a
  UUID4.
- A ``LoggerAdapter``: it depends on the caller using the right object.
- Letting a logging error propagate: it would make the observability layer able to break the
  work it observes (docs/CODE_STANDARD.md, section 2).

OÙ
---
Role: cross-cutting infrastructure. Imported by every module through ``get_logger``. Entry
points call ``configure_logging``: ``run_pipeline.py`` and the ``main()`` of ``ml.train``,
``ml.predict``, ``pipeline.cleaning``, ``pipeline.gold``, ``demo_scenario`` and
``incident_report``. ``api.py`` calls it when the module is imported, because uvicorn imports
that module as the application entry point. ``banking_dataops.audit`` reads the run identifier
from here. Depends on the standard library only.

COMMENT
-------
``new_run_id`` sets the identifier in the context variable; ``RunContextFilter`` copies it,
and the current step, onto every record that reaches the handler. The formatters render the
standard fields, then the extra fields the caller attached with ``extra={...}``.
``configure_logging`` replaces the handlers of the project logger each time it is called, so
calling it twice does not duplicate the output, and sets ``propagate`` to False so records
are not printed a second time by the root logger.
Tests that prove it: tests/test_reverse_paths.py
(``test_logging_never_raises_when_the_sink_is_broken_and_configuring_it_twice_does_not_duplicate_output``,
``test_a_run_identifier_is_sixteen_hexadecimal_characters_unique_per_run_and_anything_else_reaches_no_query``).

POURQUOI
--------
- "A control that fails without leaving a trace is worse than no control":
  docs/CODE_STANDARD.md, section 2.
- ``SafeHandler`` swallowing errors is a deliberate exception to "never swallow an exception":
  docs/CODE_STANDARD.md, section 2, "Pourquoi le SafeHandler avale les exceptions".
- The run identifier is cut to eight characters in the human format: enough to correlate by
  eye in a demonstration; the full value stays in the JSON output. Source : inconnue for the
  choice of eight.
- Line-delimited JSON, not one document: a log is appended to and must stay parseable even if
  the process is killed mid-write.

JOURNALISATION
--------------
This module is the logging layer, so it logs nothing itself. Its only direct output is the
single line ``[logging] dropped a record from <logger>`` that ``SafeHandler.handleError``
writes to stderr when a record cannot be emitted; if even that fails the error is dropped.
The level comes from ``BANKING_DATAOPS_LOG_LEVEL`` (default INFO).
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import TextIO

#: Correlation identifier of the current run.
#: Empty until :func:`new_run_id` is called, which every entry point does.
_RUN_ID: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="")
#: Name of the pipeline step currently executing. Purely informational.
_STEP: contextvars.ContextVar[str] = contextvars.ContextVar("step", default="")
LOGGER_NAME = "banking_dataops"

# ---------------------------------------------------------------------------
# RUN CORRELATION
# ---------------------------------------------------------------------------


def new_run_id() -> str:
    """Start a new run and return its identifier.

    WHY a UUID rather than a timestamp: two runs can start within the same
    second — a retry after a failure, typically — and a collision would merge
    two distinct audit trails into one.
    """
    run_id = uuid.uuid4().hex[:16]
    _RUN_ID.set(run_id)
    return run_id


def current_run_id() -> str:
    """Return the identifier of the current run, or an empty string."""
    return _RUN_ID.get()


def set_run_id(run_id: str) -> None:
    """Adopt an existing run identifier.

    WHERE this is used: a child process, or a step executed separately, that
    must attach its events to a run started elsewhere.
    """
    _RUN_ID.set(run_id)


def set_step(step: str) -> None:
    """Declare the pipeline step now executing."""
    _STEP.set(step)


def current_step() -> str:
    """Return the current pipeline step, or an empty string."""
    return _STEP.get()


# ---------------------------------------------------------------------------
# FORMATTERS
# ---------------------------------------------------------------------------


class HumanFormatter(logging.Formatter):
    """One readable line per event.

    Format: ``HH:MM:SS  LEVEL  [run:abcd1234] step  message  key=value``

    The run identifier is truncated to eight characters: enough to correlate
    by eye during a demonstration, and the full value remains in the JSON
    output for audit.
    """

    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        run_id = getattr(record, "run_id", "") or "-" * 8
        step = getattr(record, "step", "") or "-"
        extras = _extra_fields(record)
        suffix = "  " + " ".join(f"{k}={v}" for k, v in extras.items()) if extras else ""
        return f"{stamp}  {record.levelname:<7} [run:{run_id[:8]}] {step:<12} {record.getMessage()}{suffix}"


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    WHY line-delimited JSON rather than a single document: a log file is
    appended to, never rewritten, and must stay parseable even when the process
    is killed mid-write.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", ""),
            "step": getattr(record, "step", ""),
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


#: Attributes present on every LogRecord, excluded from the structured extras.
_STANDARD_ATTRS = frozenset(
    [
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "run_id",
        "step",
    ]
)


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    """Return only the fields explicitly attached by the caller."""
    return {k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS}


# ---------------------------------------------------------------------------
# CORRELATION FILTER AND SAFE HANDLER
# ---------------------------------------------------------------------------


class RunContextFilter(logging.Filter):
    """Attach the current run identifier and step to every record.

    A filter rather than an adapter: it applies to every record that reaches the handler,
    whichever child logger emitted it, so correlation does not depend on the caller
    remembering to use the right object.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = current_run_id()
        record.step = current_step()
        return True


class SafeHandler(logging.StreamHandler[TextIO]):
    """A handler that never propagates an exception.

    WHY this exists: a broken pipe, a full disk or an unserialisable object must
    not abort a data pipeline. Observability is subordinate to the work it
    observes. The failure is signalled on stderr, once, and swallowed.

    This is a deliberate exception to the rule "never swallow an exception". It
    is covered by a reverse test that asserts the swallowing actually happens.
    """

    def handleError(self, record: logging.LogRecord) -> None:
        # noqa SIM105 : contextlib.suppress serait equivalent, mais chaque
        # branche porte ici son commentaire explicatif. Garantie couverte par
        # test_logging_never_raises_when_the_sink_is_broken_and_configuring_it_twice_does_not_duplicate_output.
        try:  # noqa: SIM105
            sys.stderr.write(f"[logging] dropped a record from {record.name}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------


def configure_logging(level: str | None = None, fmt: str | None = None) -> logging.Logger:
    """Configure the project logger and return it."""
    resolved_level = (level or os.getenv("BANKING_DATAOPS_LOG_LEVEL") or "INFO").upper()
    resolved_fmt = (fmt or os.getenv("BANKING_DATAOPS_LOG_FORMAT") or "human").lower()
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(resolved_level)
    logger.handlers.clear()
    logger.propagate = False
    handler = SafeHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if resolved_fmt == "json" else HumanFormatter())
    handler.addFilter(RunContextFilter())
    logger.addHandler(handler)
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child of the project logger.

    Always use this rather than ``logging.getLogger(__name__)``: it guarantees
    the record passes through the correlation filter.
    """
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
