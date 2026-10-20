"""Incident report (``banking_dataops.incident_report``): when a report is written, what it says, when it is not.

The controls themselves are replaced by results built in the test; the report is read back from a temporary
directory, so nothing outside ``tmp_path`` is written.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from banking_dataops import incident_report, logging_config
from banking_dataops.config import Settings
from banking_dataops.incident_report import generate_incident_markdown, write_incident_report
from banking_dataops.quality_checks import QualityCheckResult

AT = datetime(2026, 7, 27, 9, 14, tzinfo=UTC)


def _result(control: str, name: str, status: str, severity: str, failed: int) -> QualityCheckResult:
    return QualityCheckResult(control, name, status, failed, severity, AT)


EXPECTED_REPORT = """# Generated DataOps Incident Report

## Scope

Synthetic local portfolio environment. No real client, bank, insurance or health data.
## Failed or warning controls

| Control | Status | Severity | Failed rows |
|---|---|---:|---:|
| CTRL-003 — invalid_amounts | FAIL | high | 3 |
| CTRL-007 — stale_data | WARN | low | 1 |

## Triage steps

1. Review the failed control.
2. Identify affected table and row pattern.
3. Re-run the relevant SQL query manually.
4. Decide whether to regenerate data, adjust validation or document a known issue.
5. Update the runbook if the failure mode is new.

## Public-safety note

Do not paste real operational data into this report.
"""


def test_the_report_lists_failed_and_warning_controls_only_is_written_in_utf_8_and_a_blocked_folder_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_records: list
) -> None:
    results = [
        _result("CTRL-001", "critical_nulls", "PASS", "high", 0),
        _result("CTRL-003", "invalid_amounts", "FAIL", "high", 3),
        _result("CTRL-005", "invalid_risk_score", "PASS", "medium", 0),
        _result("CTRL-007", "stale_data", "WARN", "low", 1),
    ]
    assert generate_incident_markdown(results) == EXPECTED_REPORT

    # FORBIDDEN PATH: an incident report for a clean batch, which would train operators to ignore reports. The
    # statuses are a closed vocabulary (PASS, WARN, FAIL): anything else, in lower case for instance, is no incident.
    assert generate_incident_markdown([]) == ""
    assert generate_incident_markdown([_result("CTRL-001", "critical_nulls", "PASS", "high", 0)]) == ""
    assert generate_incident_markdown([_result("CTRL-001", "critical_nulls", "fail", "high", 1)]) == ""

    # Writing creates the missing folders and logs where and how big.
    target = tmp_path / "output" / "reports" / "incident_report.md"
    markdown = "# Rapport — essai\n"
    write_incident_report(markdown, target)
    assert target.read_text(encoding="utf-8") == markdown
    record = next(r for r in log_records if r.getMessage() == "incident_report_written")
    assert record.levelname == "INFO"
    assert record.category == "decision"
    assert record.reason == (
        "a failed control produced an incident; the report is the document an analyst works from and an auditor reads"
    )
    assert record.path == str(target)
    # The size is in bytes, not characters: the dash is three bytes in UTF-8.
    assert record.bytes == len(markdown) + 2

    # Writing over an existing report replaces it, in UTF-8 whatever the platform default.
    existing = tmp_path / "incident_report.md"
    existing.write_text("old report that is much longer than the new one", encoding="utf-8")
    written: list[tuple[str, str | None]] = []
    original = Path.write_text

    def spy(
        self: Path, data: str, encoding: str | None = None, errors: str | None = None, newline: str | None = None
    ) -> int:
        written.append((self.name, encoding))
        return original(self, data, encoding=encoding, errors=errors, newline=newline)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_text", spy)
        write_incident_report("new\n", existing)
    assert existing.read_text(encoding="utf-8") == "new\n"
    assert written == [("incident_report.md", "utf-8")]

    # FORBIDDEN PATH: a file standing where the folder of the report should be is an error for the caller, not a
    # report silently written somewhere else.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a folder", encoding="utf-8")
    with pytest.raises(OSError):
        write_incident_report("# never written\n", blocker / "incident_report.md")
    assert blocker.read_text(encoding="utf-8") == "not a folder"


def _main_with(monkeypatch: pytest.MonkeyPatch, settings: Settings, results: list[QualityCheckResult]) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(logging_config, "configure_logging", lambda: calls.append("configure_logging"))
    monkeypatch.setattr(logging_config, "new_run_id", lambda: calls.append("new_run_id") or "0" * 16)
    monkeypatch.setattr(incident_report, "load_settings", lambda: settings)

    def run(given: Settings) -> list[QualityCheckResult]:
        assert given is settings
        calls.append("run_quality_checks")
        return results

    monkeypatch.setattr(incident_report, "run_quality_checks", run)
    return calls


def test_main_writes_the_report_when_a_control_failed_and_leaves_the_output_folder_unchanged_when_none_did(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    settings: Settings,
    log_records: list,
) -> None:
    runtime = settings.model_copy(update={"output_dir": tmp_path / "out"})
    with monkeypatch.context() as patch:
        calls = _main_with(patch, runtime, [_result("CTRL-003", "invalid_amounts", "FAIL", "high", 3)])
        incident_report.main()
    written = tmp_path / "out" / "incident_report.md"
    assert calls == ["configure_logging", "new_run_id", "run_quality_checks"]
    assert "| CTRL-003 — invalid_amounts | FAIL | high | 3 |" in written.read_text(encoding="utf-8")
    assert capsys.readouterr().out == f"Incident report written to {written}\n"

    # FORBIDDEN PATH: an empty or stale report written for a batch where every control passed.
    clean = settings.model_copy(update={"output_dir": tmp_path / "clean_out"})
    _main_with(monkeypatch, clean, [_result("CTRL-001", "critical_nulls", "PASS", "high", 0)])
    incident_report.main()
    assert not (tmp_path / "clean_out").exists()
    assert capsys.readouterr().out == "No incident generated.\n"
    record = next(r for r in log_records if r.getMessage() == "no_incident_to_report")
    assert (record.levelname, record.category, record.rows) == ("INFO", "control", 0)
    assert record.reason == "every control passed; there is nothing to escalate"
