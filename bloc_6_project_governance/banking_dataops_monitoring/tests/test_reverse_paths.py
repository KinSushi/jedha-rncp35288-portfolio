#!/usr/bin/env python3

"""Reverse tests: what must fail, must fail cleanly.

DOCTRINE
--------
A forward test proves the happy path works. A reverse test proves the forbidden
path *cannot be taken*, and that the refusal is explicit rather than silent.

Four failure modes are unacceptable and each has tests below:

    LEAK              an operation reaches data it must not reach
    SILENT CRASH      an error is swallowed and the caller believes it succeeded
    STATE CORRUPTION  an invariant is broken and execution continues
    LOOK-AHEAD        a decision uses information it should not have

Every test names the forbidden path it closes. A test that reads the source of the project (a scan) is only worth
something if the scan can fail, so each scan is a function that raises ``AssertionError`` naming the broken rule
and is also given a deliberately broken source, which it must refuse.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import logging
import re
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from banking_dataops import api, audit, demo_scenario
from banking_dataops.audit import (
    EVENT_CATEGORIES,
    AuditEvent,
    _safe_run_id,
    assert_invariant,
    fingerprint_bytes,
)
from banking_dataops.demo_scenario import _INSERT_SQL
from banking_dataops.logging_config import (
    SafeHandler,
    configure_logging,
    current_run_id,
    new_run_id,
)
from banking_dataops.quality_checks import CONTROL_REASONS, get_quality_checks, run_quality_checks

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "banking_dataops"

#: Modules that change state and must therefore be journalled.
STATE_CHANGING = [
    "ingest.py",
    "quality_checks.py",
    "demo_scenario.py",
    "pipeline/cleaning.py",
    "pipeline/gold.py",
    "ml/train.py",
    "ml/predict.py",
]
#: ``db.py`` is exempt from the scan for writes: it is the access layer itself, and logging there would produce one
#: line per query with no business context, noise that would bury the events that matter.
EXEMPT_FROM_WRITE_SCAN = {"db.py", "config.py", "__init__.py"}
WRITE_STATEMENT = re.compile(r"\b(INSERT INTO|UPDATE\s+\w|DELETE FROM|TRUNCATE)\b")
#: Match on USAGE, not on the mere presence of a word. A leftover ``from banking_dataops.audit import RunJournal``
#: would otherwise satisfy the check on a module that no longer journals anything, which was found while
#: mutation-testing this very guard.
TRACE_PATTERNS = (
    r"logger\.(debug|info|warning|error)\(",
    r"\brecord_event\(",
    r"with RunJournal\(",
    r"\brun\.note\(",
)


def _check_traceability(sources: dict[str, str]) -> None:
    """Every state-changing module journals, and no module writes without leaving a trace.

    Both journal and events are required, not either. A module can emit isolated events while having lost its
    run framing: the events then have no execution to attach to, and the audit trail loses the ability to answer
    "what ran". The first version accepted either, and passed against a deliberately broken module.
    """
    missing_journal = [name for name in STATE_CHANGING if "RunJournal" not in sources[name]]
    missing_events = [
        name for name in STATE_CHANGING if "record_event" not in sources[name] and "run.note" not in sources[name]
    ]
    assert not missing_journal, f"these modules mutate state without a run journal: {missing_journal}"
    assert not missing_events, f"these modules journal a run but emit no event: {missing_events}"

    offenders = []
    for relative, source in sorted(sources.items()):
        if Path(relative).name in EXEMPT_FROM_WRITE_SCAN or not WRITE_STATEMENT.search(source):
            continue
        if not any(re.search(pattern, source) for pattern in TRACE_PATTERNS):
            offenders.append(relative)
    assert not offenders, (
        f"these modules persist data without leaving any trace: {offenders}. "
        "A write nobody can see is a write nobody can audit."
    )


def _check_decision_trail(quality_source: str, api_source: str, verdict_source: str) -> None:
    """The verdict is journalled as a decision, the results are complete by invariant, the API traces each request.

    A verdict that leaves no trace cannot be reconstructed months later. Five green controls out of five look
    identical to seven out of seven on a dashboard showing a ratio, so an invariant must catch a missing result.
    A downstream pipeline that halts because ``/verdict`` returned false must be traceable to the exact call.
    The verdict reflects the last *evaluated* batch: recomputing it from the current table state would let a row
    inserted after the evaluation change a verdict already issued, the analogue of look-ahead bias.
    """
    assert '"decision"' in quality_source, "the verdict is not journalled under the decision category"
    assert "verdict_issued" in quality_source, "the verdict has no event of its own"
    assert "assert_invariant" in quality_source, "no invariant on the control results"
    assert "one result per declared control" in quality_source, "the invariant does not say what it counts"
    assert "app.add_middleware(BaseHTTPMiddleware, dispatch=trace_requests)" in api_source, (
        "no request middleware: requests are untraced"
    )
    assert "X-Run-Id" in api_source, "the run identifier is not returned to the caller"
    assert "new_run_id()" in api_source, "requests share one identifier instead of one each"
    assert "load_latest_quality_results" in verdict_source, "the verdict is not read from the persisted results"
    assert "FROM transactions" not in verdict_source, "the verdict is recomputed from live data"


def _results(*rows: tuple[str, str, int, str]) -> pd.DataFrame:
    now = datetime.now(UTC).replace(tzinfo=None)
    return pd.DataFrame(
        [
            {
                "control_id": control_id,
                "check_name": "check",
                "status": status,
                "failed_rows": failed_rows,
                "severity": severity,
                "executed_at": now,
            }
            for control_id, status, failed_rows, severity in rows
        ]
    )


# ===========================================================================
# LEAK — an operation must not reach data outside its scope
# ===========================================================================


def test_the_demo_reset_is_scoped_to_its_marker_and_a_mistyped_action_is_refused_instead_of_read_as_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORBIDDEN PATHS: a reset deleting real transactions, an injected row nobody can delete, a typo that deletes.

    The reset targets ``source_system = 'demo_scenario'``. If the generator ever produced that value, the reset
    would delete production-like rows: the absence of collision is the single guarantee that makes the module
    safe against a populated database. An injected row without the marker would survive every reset. Reading the
    source is crude but decisive: a DELETE on transactions without a WHERE clause on source_system would be
    catastrophic and must be caught by a test, not by a review. ``demo_scenario reste`` must not be read as
    ``reset``: refusing is the only acceptable behaviour when the alternative deletes rows.
    """
    generated = {"core_banking", "card_processor", "payments_gateway"}
    assert demo_scenario.DEMO_SOURCE_SYSTEM not in generated

    assert "%(source_system)s" in _INSERT_SQL
    for violation in demo_scenario.DEMO_VIOLATIONS:
        assert violation.transaction_id.startswith(demo_scenario.DEMO_ID_PREFIX)

    source = inspect.getsource(demo_scenario.reset)
    assert "DELETE FROM transactions WHERE source_system = %s" in source
    assert "TRUNCATE" not in source.upper()
    assert "DROP" not in source.upper()

    monkeypatch.setattr(sys, "argv", ["demo_scenario", "reste"])
    with pytest.raises(SystemExit, match="Unknown action"):
        demo_scenario.main()


def test_a_run_identifier_is_sixteen_hexadecimal_characters_unique_per_run_and_anything_else_reaches_no_query() -> None:
    """FORBIDDEN PATHS: a crafted identifier reaching a query, two runs sharing one audit trail.

    Validation is a whitelist: sixteen hexadecimal characters, nothing else. A blacklist would age badly. A
    timestamp-based identifier would collide on a retry within the same second, merging two distinct executions
    into one journal. A run identifier must never be inherited by accident. Without a stable fingerprint, no
    reproducibility claim can be verified.
    """
    for hostile in ["' OR 1=1 --", "abc; DROP TABLE audit_events", "../etc", "zz", "g" * 8]:
        with pytest.raises(ValueError, match="Invalid run identifier"):
            _safe_run_id(hostile)

    # The whitelist must not reject what it is meant to accept.
    assert _safe_run_id("a1b2c3d4e5f60718") == "a1b2c3d4e5f60718"
    assert _safe_run_id(None) == ""

    run_id = new_run_id()
    assert current_run_id() == run_id
    assert re.fullmatch(r"[0-9a-f]{16}", run_id)
    issued = [new_run_id() for _ in range(200)]
    assert len(set(issued)) == len(issued)

    assert fingerprint_bytes(b"abc") == fingerprint_bytes(b"abc")
    assert fingerprint_bytes(b"abc") != fingerprint_bytes(b"abd")


# ===========================================================================
# SILENT CRASH — an error must never be mistaken for a success
# ===========================================================================


def test_audit_event_without_a_reason_is_refused() -> None:
    """FORBIDDEN PATH: a journal entry recording what without why.

    Such an entry is a log line, not an audit record. Accepting it would let
    the journal degrade into free text within weeks.
    """

    with pytest.raises(ValueError, match="requires a reason"):
        AuditEvent(category="data", action="rows_inserted", reason="")
    with pytest.raises(ValueError, match="requires a reason"):
        AuditEvent(category="data", action="rows_inserted", reason="   ")


def test_audit_event_with_a_negative_row_count_is_refused() -> None:
    """FORBIDDEN PATH: a nonsensical count reaching the journal."""

    with pytest.raises(ValueError, match="cannot be negative"):
        AuditEvent(category="data", action="x", reason="y", row_count=-1)


def test_logging_never_raises_when_the_sink_is_broken_and_configuring_it_twice_does_not_duplicate_output() -> None:
    """FORBIDDEN PATHS: observability aborting the work it observes, every line logged twice after a re-import.

    A broken pipe or a full disk must not stop a data pipeline. This is a deliberate exception to "never
    swallow", and it is asserted rather than assumed: first that the sink really is broken, then that the
    handler survives it. Duplicated lines make row counts in the journal untrustworthy.
    """

    class BrokenStream:
        def write(self, _: str) -> None:
            raise OSError("broken pipe")

        def flush(self) -> None:
            raise OSError("broken pipe")

    with pytest.raises(OSError, match="broken pipe"):
        BrokenStream().write("the premise of this test: writing to this sink fails")

    handler = SafeHandler(stream=BrokenStream())
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "message", None, None)
    handler.emit(record)  # must not raise

    first = configure_logging()
    second = configure_logging()
    assert first is second
    assert [type(h).__name__ for h in second.handlers] == ["SafeHandler"]


def test_the_verdict_blocks_on_a_failure_not_on_a_warning_is_503_when_it_cannot_decide_and_health_stays_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORBIDDEN PATHS: publishing a batch a control rejected, a warning halting a pipeline, a 500 for a 503.

    Publishing known-bad data under a reassuring verdict is the single most damaging bug the system could have.
    The symmetric error is a control in WARN treated as a block: a structural false positive, and a dispositif
    that cries wolf is unplugged within weeks. A consumer distinguishes "the service is broken" from "no control
    has run yet"; collapsing both into a 500 makes the caller retry the wrong thing. A liveness probe that
    touches the database is not a liveness probe: an orchestrator would restart a healthy process because a
    dependency is unavailable.
    """
    client = TestClient(api.app, raise_server_exceptions=False)

    def serve(frame: pd.DataFrame) -> None:
        monkeypatch.setattr(api, "load_latest_quality_results", lambda: frame)

    serve(_results(("CTRL-001", "PASS", 0, "high"), ("CTRL-003", "FAIL", 1, "high")))
    failing = client.get("/verdict").json()
    assert failing["can_publish"] is False
    assert failing["status"] == "FAIL"

    serve(_results(("CTRL-001", "PASS", 0, "high"), ("CTRL-007", "WARN", 3, "low")))
    warning = client.get("/verdict").json()
    assert warning["can_publish"] is True
    assert warning["status"] == "WARN"

    serve(pd.DataFrame())
    assert client.get("/verdict").status_code == 503

    def database_is_down() -> pd.DataFrame:
        raise ConnectionError("database is down")

    monkeypatch.setattr(api, "load_latest_quality_results", database_is_down)
    assert client.get("/verdict").status_code == 503
    assert client.get("/health").json() == {"status": "ok"}

    # The API only reports: a write verb on the decision, or a route that does not exist, is refused.
    assert client.post("/verdict").status_code == 405
    assert client.delete("/controls").status_code == 405
    assert client.get("/nowhere").status_code == 404


# ===========================================================================
# STATE CORRUPTION — an invariant must stop execution, not degrade it
# ===========================================================================


def test_a_violated_invariant_stops_the_run_and_a_satisfied_one_is_journalled_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORBIDDEN PATH: continuing after a broken invariant.

    A corrupted state that keeps flowing produces a verdict nobody can trust. And an invariant checked in
    silence is indistinguishable from one never run: the auditor must be able to prove the check *happened*, not
    only that it did not fail.
    """
    new_run_id()
    captured: list[AuditEvent] = []
    monkeypatch.setattr(audit, "_persist_event", lambda e, s=None: captured.append(e) or True)

    with pytest.raises(AssertionError, match="Invariant violated"):
        assert_invariant(False, invariant="a == b", reason="they must match")
    assert captured[-1].action == "invariant_violated"

    assert_invariant(True, invariant="a == a", reason="tautology")
    event = captured[-1]
    assert event.action == "invariant_checked"
    assert event.category == "integrity"


def test_every_demo_violation_breaks_its_control_fits_the_schema_carries_a_reason_and_cannot_be_edited() -> None:
    """FORBIDDEN PATHS: a demonstration that demonstrates nothing, a payload the schema rejects, no defence.

    A payload that does not violate the rule it claims to violate would show a green dashboard while the presenter
    announces a failure; each threshold is checked against the real control definition. A NULL amount or a
    duplicate identifier would be refused by PostgreSQL, the control would never fire, and the demonstration
    would silently do nothing. A narrative without a business reason cannot be defended under questioning; the
    reason lives in the code rather than in the presentation notes, so the two cannot drift apart.
    """
    known = {check.control_id for check in get_quality_checks()}
    by_control = {v.control_id: v for v in demo_scenario.DEMO_VIOLATIONS}
    for control_id in by_control:
        assert control_id in known, f"{control_id} is not a real control"
    assert by_control["CTRL-003"].amount_chf > 1_000_000  # invalid_amounts
    assert by_control["CTRL-005"].risk_score > 1  # invalid_risk_score
    assert by_control["CTRL-006"].status not in {"posted", "pending", "rejected"}

    for violation in demo_scenario.DEMO_VIOLATIONS:
        assert violation.amount_chf is not None  # NOT NULL
        assert violation.status  # NOT NULL
        assert 0 <= violation.risk_score < 10  # NUMERIC(5, 4)
        assert len(violation.business_reason) > 60
        assert len(violation.narrative) > 20
    ids = [v.transaction_id for v in demo_scenario.DEMO_VIOLATIONS]
    assert len(set(ids)) == len(ids)  # PRIMARY KEY

    # The payload cannot be edited between two presentations: a changed amount would stop breaking its control.
    with pytest.raises(dataclasses.FrozenInstanceError):
        by_control["CTRL-003"].amount_chf = Decimal(1)  # type: ignore[misc]


# ===========================================================================
# REPRODUCIBILITY — an identical input must produce an identical output
# ===========================================================================


def test_the_audit_categories_are_a_closed_list_shared_with_the_sql_check_and_an_unknown_one_is_refused() -> None:
    """FORBIDDEN PATH: an open-ended category list.

    The category list is a contract shared with the SQL CHECK constraint. They must not drift: the database
    refuses what the code allows, or the reverse, and events start disappearing silently.
    """
    expected = {"pipeline", "data", "control", "decision", "demo", "integrity"}
    assert expected == EVENT_CATEGORIES
    sql = (ROOT / "sql" / "07_audit_journal.sql").read_text(encoding="utf-8")
    for category in expected:
        assert f"'{category}'" in sql, f"{category} missing from the SQL CHECK constraint"

    with pytest.raises(ValueError, match="Unknown audit category"):
        AuditEvent(category="whatever", action="x", reason="y")


# ===========================================================================
# TRACEABILITY — a standard that is not enforced is an intention
# ===========================================================================


def test_every_state_changing_module_is_journalled_and_the_scan_refuses_a_module_that_leaves_no_trace() -> None:
    """FORBIDDEN PATH: a module that writes without leaving a trace.

    `docs/CODE_STANDARD.md` states that every step declares what it did, on how many rows, and why. A module that
    mutates state without journalling makes that claim false, and the claim is what an auditor relies on. This
    scan is the mechanism that keeps the standard true as the code grows: it reads every module, so a module
    added tomorrow is covered automatically.
    """
    real = {path.relative_to(SOURCE).as_posix(): path.read_text(encoding="utf-8") for path in SOURCE.rglob("*.py")}
    _check_traceability(real)

    # Each rule fires on a source that breaks it, and only on that one.
    journal_only = "from banking_dataops.audit import RunJournal\n"
    hostile = [
        ({"pipeline/gold.py": "record_event('data', 'x', reason='y')"}, "without a run journal"),
        ({"pipeline/gold.py": "with RunJournal('gold'):\n    pass"}, "emit no event"),
        ({"new_module.py": "cursor.execute('INSERT INTO t VALUES (1)')"}, "without leaving any trace"),
        ({"new_module.py": journal_only + "cursor.execute('DELETE FROM t')"}, "without leaving any trace"),
    ]
    for override, broken_rule in hostile:
        with pytest.raises(AssertionError, match=re.escape(broken_rule)):
            _check_traceability({**real, **override})
    # The same write is accepted once it is logged: the scan refuses the silence, not the write.
    _check_traceability({**real, "new_module.py": "cursor.execute('TRUNCATE t')\nlogger.info('emptied')"})


def test_every_control_has_a_business_reason_and_the_decision_trail_is_complete_and_the_check_refuses_a_gap() -> None:
    """FORBIDDEN PATH: a control blocking a batch without a defensible reason, a verdict nobody can reconstruct.

    When a control failed, publication stops. Someone will ask why. The reason lives next to the rule so the two
    cannot drift apart, and the journal refuses an event without one.
    """
    declared = {check.control_id for check in get_quality_checks()}
    documented = set(CONTROL_REASONS)
    assert declared <= documented, f"controls without a business reason: {declared - documented}"
    for control_id, reason in CONTROL_REASONS.items():
        assert len(reason) > 60, f"{control_id} has a reason too short to defend"

    quality = inspect.getsource(run_quality_checks)
    served = inspect.getsource(api)
    verdict = inspect.getsource(api.verdict)
    _check_decision_trail(quality, served, verdict)

    sources = {"quality_source": quality, "api_source": served, "verdict_source": verdict}
    middleware = "app.add_middleware(BaseHTTPMiddleware, dispatch=trace_requests)"
    hostile = [
        ("quality_source", quality.replace('"decision"', '"other"'), "not journalled under the decision category"),
        ("quality_source", quality.replace("verdict_issued", "done"), "no event of its own"),
        ("quality_source", quality.replace("assert_invariant", "check"), "no invariant on the control results"),
        ("quality_source", quality.replace("one result per declared control", "ok"), "does not say what it counts"),
        ("api_source", served.replace(middleware, "pass"), "requests are untraced"),
        ("api_source", served.replace("X-Run-Id", "X-Id"), "not returned to the caller"),
        ("api_source", served.replace("new_run_id()", "run_id"), "share one identifier"),
        ("verdict_source", verdict.replace("load_latest_quality_results", "recompute"), "not read from the persisted"),
        ("verdict_source", verdict + "\n    # FROM transactions", "recomputed from live data"),
    ]
    for part, broken_source, broken_rule in hostile:
        assert broken_source != sources[part], f"the hostile example for {broken_rule!r} changed nothing"
        with pytest.raises(AssertionError, match=re.escape(broken_rule)):
            _check_decision_trail(**{**sources, part: broken_source})


def test_the_pipeline_does_not_require_a_model_to_complete_and_a_failing_training_is_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FORBIDDEN PATH: a first install that cannot start.

    On a fresh install there is no model artefact. If the pipeline raised on the missing model, it would die
    before the entrypoint reached the dashboard. Without ``--train`` the model code must not even be called; with
    it, a failing training stops the run instead of being skipped in silence.
    """
    from banking_dataops.generate_synthetic_data import DEFAULT_GENERATED_AT
    from banking_dataops.ml import train as train_module

    spec = importlib.util.spec_from_file_location("run_pipeline_no_model", ROOT / "run_pipeline.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "run_pipeline_no_model", module)
    spec.loader.exec_module(module)

    ran: list[str] = []
    for name in ("step_bronze", "step_cleaning", "step_silver", "step_gold"):
        monkeypatch.setattr(
            module, name, lambda *args, _n=name, **kwargs: ran.append(_n) or module.StepResult(_n, 1, 1)
        )

    trained: list[str] = []

    def failing_training(*args: object, **kwargs: object) -> None:
        trained.append("train")
        raise RuntimeError("training failed")

    monkeypatch.setattr(train_module, "train", failing_training)
    monkeypatch.setattr(audit, "_persist_event", lambda *args, **kwargs: False)

    for argv in (["--skip-ml"], [], ["--train", "--skip-ml"]):
        ran.clear()
        steps = module.run(module.parse_args(argv), DEFAULT_GENERATED_AT)
        assert ran == ["step_bronze", "step_cleaning", "step_silver", "step_gold"]
        assert [step.name for step in steps] == ["step_bronze", "step_cleaning", "step_silver", "step_gold"]
    assert trained == []

    with pytest.raises(RuntimeError, match="training failed"):
        module.run(module.parse_args(["--train"]), DEFAULT_GENERATED_AT)
    assert trained == ["train"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known gap: docker/entrypoint.sh runs under `set -e`, so a failing pipeline stops the container before "
        "the dashboard starts; the CHANGELOG (2.6.0) claims the opposite"
    ),
)
def test_entrypoint_starts_the_dashboard_even_when_the_pipeline_fails() -> None:
    """FORBIDDEN PATH: losing the only window onto a failure.

    A failed pipeline is precisely when someone needs the dashboard and the audit trail to understand what
    happened. Killing the container in that situation removes the diagnostic surface at the moment it is
    needed. The test reads the entrypoint: the pipeline command must be guarded, so that its failure does
    not end the script. It is expected to fail today (strict xfail): fixing the entrypoint turns it into a
    reported XPASS, which forces this marker to be removed.
    """
    script = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
    pipeline_lines = [
        line for line in script.splitlines() if "run_pipeline.py" in line and not line.lstrip().startswith("#")
    ]
    assert pipeline_lines, "the entrypoint no longer runs the pipeline"
    guarded = all("||" in line for line in pipeline_lines) or "set +e" in script
    assert guarded, "a failing run_pipeline.py ends the script under `set -e` before the services start"
