#!/usr/bin/env python3
"""Read-only decision API: the publication verdict, in a form a downstream machine can branch on.

The dashboard answers "can this batch be released?" for a person. This API answers it for a
pipeline: before consuming the batch it asks ``/verdict`` and halts if ``can_publish`` is false.

CHOIX
-----
Retained:

- Read-only. There is no POST, PUT or DELETE route: the controls decide, the API reports. That removes
  the authentication and authorisation surface a control plane would need (ADR-0006).
- FastAPI, for the generated OpenAPI documentation and the Pydantic validation of the response
  models. Source : inconnue for the comparison with other frameworks; ADR-0006 records the read-only
  decision, not the framework choice.
- Two probes: ``/health`` (the process is alive, no database access) and ``/ready`` (the database
  answers). A process can be alive and not ready.
- ``can_publish`` is true when no control FAILED. A control that only warns (CTRL-007, freshness) does not
  block publication; the ``status`` field says ``WARN`` so a consumer can still see it.
- A middleware gives each request its own run identifier and logs its outcome, so a route added later
  inherits the tracing, and a halt reported by a consumer ("the verdict said I could publish at 09:14")
  can be traced to one exact call. The identifier is returned in the ``X-Run-Id`` header.
- No score from the model is exposed or used: the verdict comes from the controls alone (ADR-0002).

Rejected:

- Authentication: the data are synthetic and the API only reads (ADR-0006). If exposed beyond a local
  network it would have to be added.
- CORS: not configured, because the consumers are machines. If browsers called it, origins would have to
  be restricted.
- A write route to close incidents or re-run controls: it would turn the API into a control plane.
- A separate container for the API: the compose file runs the API and the dashboard as two processes of
  one container, started by ``docker/entrypoint.sh`` (ADR-0005, section "Un seul conteneur").

OÙ
---
Role: the machine interface of the control layer. Callers: uvicorn (``banking_dataops.api:app``, started by
``docker/entrypoint.sh``), the Makefile targets ``api-open`` and ``api-verdict``, and downstream pipelines.
Depends on ``banking_dataops.monitoring`` (latest control results), ``banking_dataops.db`` (``/ready`` and
``/incidents``) and ``banking_dataops.logging_config``. Reads ``quality_check_results`` and
``incident_reports``. Unlike a library module it configures logging when it is imported, because uvicorn
imports it as the application entry point.

COMMENT
-------
``/verdict`` loads the latest control results, summarises them with ``summarize_quality_status``, and
returns the counts, the identifiers of the failed controls and the number of rows in violation. With no
control executed yet it answers 503 rather than a vacuous "can publish". ``/controls`` lists every control
result, ``/incidents`` the open incidents (at most 50, newest first). A database error becomes a 503 with
the message, never a stack trace.
Tests that prove it: tests/test_api.py (``test_verdict_allows_publication_when_everything_passes``,
``test_verdict_blocks_publication_on_failure``, ``test_warning_still_allows_publication``,
``test_verdict_returns_503_when_no_control_ran``, ``test_health_does_not_touch_the_database``),
tests/test_reverse_paths.py
(``test_the_verdict_blocks_on_a_failure_not_on_a_warning_is_503_when_it_cannot_decide_and_health_stays_up``,
``test_every_control_has_a_business_reason_and_the_decision_trail_is_complete_and_the_check_refuses_a_gap``).

POURQUOI
--------
- Read-only and no authentication: ADR-0006.
- 503 when no control ran: a consumer that reads an empty result as "nothing failed" would publish an
  unevaluated batch; the failure must be explicit (docs/CODE_STANDARD.md, section 7).
- Warnings do not block: CTRL-007 exists to flag a broken feed without refusing a batch that may be valid
  (``CONTROL_REASONS`` in ``banking_dataops.quality_checks``).
- A fresh run identifier per request and the duration in the log: a slow readiness probe is the first
  symptom of a database under pressure. Source : inconnue for the claim that it shows up "before anywhere
  else".

JOURNALISATION
--------------
The middleware logs ``http_request`` for every call under the logger ``api``: INFO, or WARNING for a 5xx
response, with the method, path, status and duration in milliseconds; the category is ``decision`` for
``/verdict`` and ``data`` otherwise. ``/verdict`` also logs ``verdict_served`` at INFO with the status and
``can_publish``. These are log records only: the API writes nothing to ``audit_events`` (it is read-only).
Database errors are converted to ``HTTPException`` 503 and not swallowed. No ``print``.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from banking_dataops.db import fetch_rows
from banking_dataops.logging_config import (
    configure_logging,
    current_run_id,
    get_logger,
    new_run_id,
)
from banking_dataops.monitoring import (
    load_latest_quality_results,
    summarize_quality_status,
)

# Configured at import: this module IS the application when served by uvicorn, so it owns the
# logging decision. A library would not do this.
configure_logging()
logger = get_logger("api")
app = FastAPI(
    title="Banking DataOps Monitoring API",
    description=(
        "Read-only publication verdict for downstream systems. "
        "Synthetic data only, not connected to production systems."
    ),
    version="1.0.0",
)


async def trace_requests(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Give every request its own run identifier and log its outcome.

    A middleware rather than a log line per endpoint, so correlation does not depend on each handler
    remembering to log. One identifier per request, so that a consumer reporting a decision can be
    traced to one exact call and not to a process that has been up for a week.
    """
    run_id = new_run_id()
    # Monotonic clock: immune to changes of the system time.
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = int((time.monotonic() - started) * 1000)
    level = logger.warning if response.status_code >= 500 else logger.info
    level(
        "http_request",
        extra={
            "category": "decision" if request.url.path == "/verdict" else "data",
            "reason": (
                "a downstream pipeline queried the publication decision"
                if request.url.path == "/verdict"
                else f"read-only query on {request.url.path}"
            ),
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": duration_ms,
        },
    )

    response.headers["X-Run-Id"] = run_id
    return response


class ControlResult(BaseModel):
    """Outcome of one data-quality control, as returned by ``/controls``."""

    control_id: str = Field(examples=["CTRL-003"])
    check_name: str = Field(examples=["invalid_amounts"])
    status: str = Field(description="PASS, WARN or FAIL", examples=["FAIL"])
    failed_rows: int
    severity: str = Field(description="high, medium or low")
    executed_at: datetime


class Verdict(BaseModel):
    """The publication decision in machine-readable form. Downstream systems branch on ``can_publish``."""

    can_publish: bool = Field(
        description="True when no control failed. This is the field a downstream pipeline should branch on."
    )
    status: str = Field(description="PASS, WARN or FAIL")
    controls_total: int
    controls_passed: int
    controls_failed: int
    controls_warning: int
    rows_in_violation: int
    failed_control_ids: list[str]
    evaluated_at: datetime | None


class Incident(BaseModel):
    """An open incident raised by a failing control. ``failed_control`` links it back to that control."""

    incident_id: str
    title: str
    severity: str
    failed_control: str | None
    created_at: datetime


def health() -> dict[str, str]:
    """Return ok when the process is running. Does not touch the database (that is ``/ready``)."""
    return {"status": "ok"}


def ready() -> dict[str, str]:
    """Return ok when the database answers.

    A process can be alive but not ready (database down, schema not yet created), hence two probes.
    Raises ``HTTPException`` 503 if the database is unavailable.
    """
    try:
        fetch_rows("SELECT 1 AS ok")
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ready"}


def verdict() -> Verdict:
    """Return the publication decision for the latest evaluated batch.

    ``can_publish`` is true when no control failed; a warning alone does not block. A downstream
    pipeline should call this before consuming the batch and halt when it is false.

    Raises ``HTTPException`` 503 if the database is unavailable or no control has been executed yet.
    """
    try:
        frame = load_latest_quality_results()
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    if frame.empty:
        raise HTTPException(
            status_code=503,
            detail="no control has been executed yet - run the quality pipeline first",
        )
    summary = summarize_quality_status(frame["status"].tolist())
    failing = frame[frame["status"] == "FAIL"]
    # The verdict is the one thing a consumer branches on. Logging it here, with the run identifier
    # already set by the middleware, means a downstream halt can be traced to the exact response
    # that caused it.
    logger.info(
        "verdict_served",
        extra={
            "category": "decision",
            "reason": (
                "publication decision returned to a machine consumer; it reports "
                "the controls' verdict and never a model score (ADR-0002)"
            ),
            "can_publish": summary.failed_checks == 0,
            "status": summary.overall_status,
            "run_id": current_run_id(),
        },
    )

    return Verdict(
        can_publish=summary.failed_checks == 0,
        status=summary.overall_status,
        controls_total=summary.total_checks,
        controls_passed=summary.passed_checks,
        controls_failed=summary.failed_checks,
        controls_warning=summary.warning_checks,
        rows_in_violation=int(failing["failed_rows"].sum()),
        failed_control_ids=sorted(failing["control_id"].tolist()),
        evaluated_at=frame["executed_at"].max(),
    )


def controls() -> list[ControlResult]:
    """Return every control with its outcome, for investigation.

    Raises ``HTTPException`` 503 if the database is unavailable.
    """
    try:
        frame = load_latest_quality_results()
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return [ControlResult.model_validate(row) for row in frame.to_dict(orient="records")]


def incidents() -> list[Incident]:
    """Return the open incidents, newest first, at most 50.

    Raises ``HTTPException`` 503 if the database is unavailable.
    """
    try:
        rows = fetch_rows(
            """
            SELECT incident_id, title, severity, failed_control, created_at
            FROM incident_reports
            WHERE status = 'open'
            ORDER BY created_at DESC
            LIMIT 50
            """
        )
    except Exception as exc:  # pragma: no cover - runtime path
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return [Incident.model_validate(row) for row in rows]


# The middleware and the five routes are registered here, after their definitions, and not with decorators:
# the mutation tool never mutates a decorated function, so the whole module would have escaped it.
# ``app.add_middleware`` and ``app.add_api_route`` are what ``@app.middleware("http")`` and ``@app.get(...)`` call.
app.add_middleware(BaseHTTPMiddleware, dispatch=trace_requests)
app.add_api_route("/health", health, methods=["GET"], summary="Liveness probe")
app.add_api_route("/ready", ready, methods=["GET"], summary="Readiness probe")
app.add_api_route("/verdict", verdict, methods=["GET"], response_model=Verdict, summary="Publication verdict")
app.add_api_route(
    "/controls", controls, methods=["GET"], response_model=list[ControlResult], summary="Detailed controls"
)
app.add_api_route("/incidents", incidents, methods=["GET"], response_model=list[Incident], summary="Open incidents")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
