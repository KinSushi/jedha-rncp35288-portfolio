#!/usr/bin/env python3
"""Audit trail: what ran, on which data, and why the result is what it is.

In a regulated setting a verdict must be explainable months later by someone who was not
there. This module gives every consequential step three things: a run identifier shared by
all its events, an event that always says why, and a SHA-256 fingerprint of the data it
read or wrote.

CHOIX
-----
Retained:

- Double write. Every event goes to the structured log, always, and to the ``audit_events``
  table, best effort (ADR-0007). The log survives a database outage, the table survives a
  container restart and can be queried in SQL.
- ``reason`` is mandatory. ``AuditEvent`` refuses an empty reason at construction and the
  table refuses it again with a ``CHECK`` (sql/07_audit_journal.sql): a journal that records
  what without why is a log, not an audit trail.
- The event is a frozen dataclass, so an event cannot be edited once it is built.
- ``assert_invariant`` journals a satisfied invariant as well as a violated one, and raises
  on violation: a check performed in silence cannot be told from a check never run.
- A closed list of six categories (``EVENT_CATEGORIES``), repeated in the table's ``CHECK``.
- A run identifier is 16 hexadecimal characters (``logging_config.new_run_id``). Before it
  enters a query it is validated against a whitelist, and then bound as a parameter.
- Data fingerprints cover file names and contents, with file names sorted first.

Rejected:

- Logs only: volatile, not queryable, no imposed structure (ADR-0007).
- A table only: unavailable exactly when the database is, and the most interesting incidents
  happen during an outage (ADR-0007).
- Raising when the table write fails: observability would break the work it observes
  (docs/CODE_STANDARD.md, section 3). The failure is logged at WARNING instead.
- Escaping the run identifier instead of validating it: a missed edge case is an injection,
  a whitelist rejects everything not explicitly allowed (docs/CODE_STANDARD.md, section 5).

OÙ
---
Role: cross-cutting. Every state-changing module opens a ``RunJournal`` and records events
here: ``ingest``, ``quality_checks``, ``reconciliation``, ``pipeline.cleaning``,
``pipeline.gold``, ``powerbi_views``, ``demo_scenario``, ``ml.train``, ``ml.predict``, and
``run_pipeline.py``, which also uses ``fingerprint_directory``. Depends on
``banking_dataops.config`` (settings), ``banking_dataops.logging_config`` (run identifier,
logger) and, lazily, ``banking_dataops.db``. Reads and writes the table ``audit_events``
(``sql/07_audit_journal.sql``). ``audit_trail``, ``summarise_runs`` and ``iter_categories``
are not called by the application: the Makefile targets ``audit-trail`` and ``audit-runs`` query
the table and the view ``vw_run_summary`` directly with psql.

COMMENT
-------
``record_event`` builds an ``AuditEvent`` (validation happens there), logs it at INFO, then
calls ``_persist_event``, which inserts one row and returns False, after a WARNING, if
anything goes wrong. ``RunJournal`` is a context manager: ``__enter__`` records ``run_started``
with the Python version, platform and commit, ``__exit__`` records ``run_succeeded`` or
``run_failed`` and returns False so the exception continues to the caller. ``audit_trail``
reads the table, filtered by a validated run identifier when one is given.
Tests that prove it: tests/test_reverse_paths.py (``test_audit_event_without_a_reason_is_refused``,
``test_a_run_identifier_is_sixteen_hexadecimal_characters_unique_per_run_and_anything_else_reaches_no_query``,
``test_a_violated_invariant_stops_the_run_and_a_satisfied_one_is_journalled_too``).

POURQUOI
--------
- Double write and best effort: ADR-0007, "Décision".
- Mandatory reason: ADR-0007 and docs/CODE_STANDARD.md, section 3.
- Journalling successes: docs/CODE_STANDARD.md, section 3, "Invariants".
- SHA-256 rather than a weaker digest: a fingerprint is used as proof that two runs produced
  the same bytes, so collisions must not be producible. Source : inconnue for the choice of
  SHA-256 itself in this project; the standard library's ``hashlib`` documents the algorithm.
- Timestamps stored without time zone (``occurred_at.replace(tzinfo=None)``): the column is
  ``TIMESTAMP`` (sql/07_audit_journal.sql:49) and the
  values are UTC; the convention is not stated elsewhere, source : inconnue.

JOURNALISATION
--------------
This module is the journal. ``record_event`` logs every event at INFO under the logger
``audit``, with the category, the reason, the row count and the details as structured
fields. ``_persist_event`` logs ``audit_event_not_persisted`` at WARNING when the table write
fails. ``RunJournal.__exit__`` records the failure with the exception name and the first 300
characters of its message before re-raising. No ``print``, no bare ``except``. The broad
``except Exception`` blocks of ``_persist_event`` and ``code_version`` return a fallback value:
the first after a WARNING, the second deliberately silent because a missing git history in a
container built from an archive is expected.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Literal

from banking_dataops.config import Settings, load_settings
from banking_dataops.logging_config import current_run_id, get_logger, new_run_id, set_step

logger = get_logger("audit")

#: Closed list of event categories. Repeated in the CHECK constraint of audit_events
#: (sql/07_audit_journal.sql): a new category needs both changes.
EVENT_CATEGORIES = frozenset(
    {
        "pipeline",
        "data",
        "control",
        "decision",
        "demo",
        "integrity",
    }
)


def fingerprint_bytes(payload: bytes) -> str:
    """Return the SHA-256 digest of a byte payload as 64 hexadecimal characters."""
    return hashlib.sha256(payload).hexdigest()


def fingerprint_file(path: Path) -> str:
    """Return the SHA-256 digest of a file, read in 64 KiB chunks.

    Chunked so that memory stays constant whatever the file size.

    Raises ``FileNotFoundError`` or ``PermissionError`` if the file cannot be read.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint_directory(directory: Path, pattern: str = "*.csv") -> str:
    """Return one digest covering every file of a directory that matches ``pattern``.

    File names are sorted before hashing: a filesystem does not promise any particular
    order, and the same directory must give the same fingerprint everywhere. The name of
    each file is hashed with its content, so renaming a file changes the fingerprint even
    if the bytes are identical (structural drift is a change too).
    """
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.name.encode("utf-8"))
        digest.update(fingerprint_file(path).encode("ascii"))
    return digest.hexdigest()


def code_version() -> str:
    """Return the short git commit hash of the running code, or ``"unknown"``.

    The commit is the mechanical answer to "which code produced this verdict". ``"unknown"``
    rather than an exception, because the application container is built from an archive
    that may carry no git history, and a missing hash is not worth stopping a run for.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def runtime_context() -> dict[str, str]:
    """Return the execution environment recorded with the ``run_started`` event.

    Reproducibility depends on the Python version and the platform as well as on the code.
    """
    return {
        "python": sys.version.split()[0],
        "platform": platform.system(),
        "code_version": code_version(),
    }


@dataclass(frozen=True)
class AuditEvent:
    """One traceable fact in the audit trail.

    Frozen: an event is a record, not a draft, and cannot be edited once built.
    ``details`` is free structured context, stored as JSONB.

    Raises ``ValueError`` at construction if the category is unknown, the action or the
    reason is empty, or the row count is negative. The refusal is loud and early so that an
    invalid event never reaches the journal.
    """

    category: str
    action: str
    reason: str
    row_count: int | None = None
    details: dict[str, object] = field(default_factory=dict)
    run_id: str = ""
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.category not in EVENT_CATEGORIES:
            raise ValueError(
                f"Unknown audit category {self.category!r}. Allowed: {', '.join(sorted(EVENT_CATEGORIES))}"
            )

        if not self.action.strip():
            raise ValueError("An audit event requires a non-empty action.")

        if not self.reason.strip():
            raise ValueError(
                "An audit event requires a reason. A journal that records what "
                "without recording why is a log, not an audit trail."
            )

        if self.row_count is not None and self.row_count < 0:
            raise ValueError(f"row_count cannot be negative, got {self.row_count}.")


def record_event(
    category: str,
    action: str,
    reason: str,
    row_count: int | None = None,
    settings: Settings | None = None,
    **details: object,
) -> AuditEvent:
    """Build one event, write it to the log, then to the ``audit_events`` table.

    The log write always succeeds and is the primary record; the table write is best effort
    (see ``_persist_event``). The event is returned so that tests can assert on it.
    """
    event = AuditEvent(
        category=category,
        action=action,
        reason=reason,
        row_count=row_count,
        details=details,
        run_id=current_run_id(),
    )

    logger.info(
        event.action,
        extra={
            "category": event.category,
            "reason": event.reason,
            "rows": event.row_count if event.row_count is not None else "-",
            **event.details,
        },
    )

    _persist_event(event, settings)
    return event


def _persist_event(event: AuditEvent, settings: Settings | None = None) -> bool:
    """Insert the event into ``audit_events``. Return False, after a WARNING, on any failure.

    The exception is swallowed on purpose: the audit layer observes the pipeline and must
    never break it, for instance during a database migration. The event is not lost, it is
    in the log, and the WARNING makes the gap visible to an operator.
    """
    try:
        from banking_dataops.db import connect
    except Exception:
        return False
    try:
        with connect(settings or load_settings()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO audit_events
                        (run_id, category, action, reason, row_count, details, occurred_at)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                    """,
                    (
                        event.run_id,
                        event.category,
                        event.action,
                        event.reason,
                        event.row_count,
                        _as_json(event.details),
                        # The column is a naive TIMESTAMP; the values are UTC.
                        event.occurred_at.replace(tzinfo=None),
                    ),
                )

            connection.commit()
        return True
    except Exception as exc:
        logger.warning(
            "audit_event_not_persisted",
            extra={
                "category": "integrity",
                "reason": "the journal table is unreachable; the event is kept in the log only",
                "error": str(exc)[:200],
            },
        )

        return False


def _as_json(details: dict[str, object]) -> str:
    """Serialize event details as JSON without ever raising on an exotic value.

    ``default=str`` turns a ``Path`` or a ``datetime`` into text instead of raising a
    ``TypeError``, so an event is persisted even when its details hold an unexpected type.
    """
    import json

    return json.dumps(details, default=str, ensure_ascii=False)


@dataclass
class RunJournal:
    """Frame one pipeline step as a run with a start and an end.

    A context manager so that a run is always closed: ``__exit__`` records either
    ``run_succeeded`` or ``run_failed``, which guarantees every run has a terminal event. It
    returns False, so an exception is journalled and then re-raised: the audit layer
    observes, it never absorbs.

    If a run identifier already exists (a parent step opened it) it is reused, so the events
    of nested steps share one ``run_id``; otherwise a new one is created.
    """

    step: str
    seed: int | None = None
    settings: Settings | None = None
    run_id: str = ""
    started_at: float = 0.0

    def __enter__(self) -> RunJournal:
        self.run_id = current_run_id() or new_run_id()
        self.started_at = time.monotonic()
        set_step(self.step)
        record_event(
            "pipeline",
            "run_started",
            reason=f"step {self.step} began",
            settings=self.settings,
            seed=self.seed,
            **runtime_context(),  # type: ignore[arg-type]  # keys never collide with row_count or settings
        )

        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        duration_ms = int((time.monotonic() - self.started_at) * 1000)
        if exc_type is None:
            record_event(
                "pipeline",
                "run_succeeded",
                reason=f"step {self.step} completed without error",
                settings=self.settings,
                duration_ms=duration_ms,
            )

        else:
            record_event(
                "pipeline",
                "run_failed",
                reason=f"step {self.step} raised {exc_type.__name__}",
                settings=self.settings,
                duration_ms=duration_ms,
                error=str(exc)[:300],
            )

        return False

    def note(
        self,
        action: str,
        reason: str,
        row_count: int | None = None,
        **details: object,
    ) -> AuditEvent:
        """Record a ``data`` event attached to this run."""
        return record_event("data", action, reason, row_count, settings=self.settings, **details)


def assert_invariant(condition: bool, invariant: str, reason: str, **details: object) -> None:
    """Check an invariant, journal the outcome, and raise if it does not hold.

    A success is journalled too: an auditor must be able to prove that a check was
    performed, not only that it did not fail. A violation raises ``AssertionError`` and
    stops the run, because continuing on corrupted state would spread the corruption to
    every later step.
    """
    # The keys of **details never collide with row_count or settings, which mypy cannot know.
    record_event(
        "integrity",
        "invariant_checked" if condition else "invariant_violated",
        reason=reason,
        invariant=invariant,
        holds=condition,
        **details,  # type: ignore[arg-type]
    )

    if not condition:
        raise AssertionError(f"Invariant violated: {invariant}. {reason}")


def audit_trail(
    run_id: str | None = None,
    limit: int = 200,
    settings: Settings | None = None,
) -> list[dict[str, object]]:
    """Return the most recent audit events, optionally restricted to one run.

    The run identifier is validated (``_safe_run_id``) and then passed as a bound
    parameter; the limit is cast to an integer.
    """
    from banking_dataops.db import fetch_rows

    validated = _safe_run_id(run_id)
    if validated:
        return fetch_rows(
            """
            SELECT run_id, category, action, reason, row_count, details, occurred_at
            FROM audit_events
            WHERE run_id = %s
            ORDER BY occurred_at DESC
            LIMIT %s
            """,
            settings=settings,
            params=(validated, int(limit)),
        )

    return fetch_rows(
        """
        SELECT run_id, category, action, reason, row_count, details, occurred_at
        FROM audit_events
        ORDER BY occurred_at DESC
        LIMIT %s
        """,
        settings=settings,
        params=(int(limit),),
    )


def _safe_run_id(run_id: str | None) -> str:
    """Validate a run identifier before it enters a query.

    A whitelist: lowercase hexadecimal characters, at most 32. Validation comes first and
    the value is then bound as a parameter as well; validating alone would be a single line
    of defence, binding alone would accept a malformed identifier and return an empty
    result instead of signalling it (docs/CODE_STANDARD.md, section 5).

    Raises ``ValueError`` for anything else.
    """
    if run_id is None:
        return ""
    candidate = run_id.strip()
    if not candidate or len(candidate) > 32 or not all(c in "0123456789abcdef" for c in candidate):
        raise ValueError(f"Invalid run identifier {run_id!r}: expected hexadecimal, at most 32 characters.")

    return candidate


def summarise_runs(limit: int = 20, settings: Settings | None = None) -> list[dict[str, object]]:
    """Return one row per run: first and last event time, event count, failures, violations.

    The SQL view ``vw_run_summary`` (sql/07_audit_journal.sql) offers a richer version.
    """
    from banking_dataops.db import fetch_rows

    return fetch_rows(
        """
        SELECT
            run_id,
            MIN(occurred_at)                                        AS started_at,
            MAX(occurred_at)                                        AS ended_at,
            COUNT(*)                                                AS event_count,
            COUNT(*) FILTER (WHERE action = 'run_failed')           AS failures,
            COUNT(*) FILTER (WHERE action = 'invariant_violated')   AS violations
        FROM audit_events
        GROUP BY run_id
        ORDER BY MIN(occurred_at) DESC
        LIMIT %s
        """,
        settings=settings,
        params=(int(limit),),
    )


def iter_categories() -> Iterable[str]:
    """Return the allowed event categories, sorted."""
    return sorted(EVENT_CATEGORIES)
