#!/usr/bin/env python3
"""Incident report: a Markdown document for the controls that failed or warned.

When a data-quality control fails, someone has to act on it. The incident rows in the database are the
machine record; this module produces the human one, a short Markdown file an engineer can review, attach to a
ticket or archive.

CHOIX
-----
Retained:

- Markdown: readable as plain text, rendered by GitHub, GitLab and Confluence, versionable in Git, and easy to
  convert or parse later.
- The content is deliberately short: a scope statement (synthetic data only), the table of the controls that
  failed or warned, five generic triage steps, and a public-safety reminder. It is a starting point for the
  analyst, not an analysis.
- A report is produced when at least one control FAILED or WARNED. When every control passed the function
  returns an empty string and the command line says there is nothing to report, logged at INFO because "no
  incident" is the desired outcome and must not look like a failure to generate.
- Writing the file is logged even though no table is written: a report nobody knows exists is a report nobody
  reads.

Rejected:

- Generating the report inside ``run_pipeline.py``: it is not called by the pipeline; it is a command an operator
  runs (``make incident``). Source : inconnue for why it was not wired in.
- Emailing or posting the report: this is a local, synthetic environment with no outbound integration
  (ADR-0005).

OÙ
---
Role: reporting on control failures, after ``quality_checks``. Caller: ``python -m
banking_dataops.incident_report`` (Makefile target ``incident``). Depends on ``banking_dataops.quality_checks``
(it re-runs the controls and so also refreshes ``quality_check_results`` and opens incidents),
``banking_dataops.config`` (``output_dir``) and ``banking_dataops.logging_config``. Writes the file
``incident_report.md`` in that output directory (git-ignored).

COMMENT
-------
``main`` runs the controls, passes the results to ``generate_incident_markdown``, which keeps the FAIL and WARN
rows and builds the text; if the text is empty it logs and stops, otherwise ``write_incident_report`` writes it
under ``settings.output_dir`` and logs the path and size.
Tests that prove it: no test calls this module directly. ``tests/test_reverse_paths.py``
(``test_every_state_changing_module_is_journalled_and_the_scan_refuses_a_module_that_leaves_no_trace``)
checks that modules which write leave a trace.

POURQUOI
--------
- WARN rows are included with the FAIL rows so that a freshness warning (CTRL-007) is visible to the analyst, even
  though a warning does not block publication (``banking_dataops.quality_checks``).
- The path and the size are logged so that an operator knows where to look (the reason string of the
  ``incident_report_written`` record).
- The public-safety note repeats the project rule that no real operational data belongs in a report
  (ADR-0003).

JOURNALISATION
--------------
INFO ``incident_report_written`` (category ``decision``, path and size in bytes) when a report is written, INFO
``no_incident_to_report`` (category ``control``) when there is none. These are log records only; the controls
themselves journal their results to ``audit_events``. ``main`` prints one line.
"""

from __future__ import annotations

from pathlib import Path

from banking_dataops.config import load_settings
from banking_dataops.logging_config import get_logger
from banking_dataops.quality_checks import QualityCheckResult, run_quality_checks

logger = get_logger("incident_report")


def generate_incident_markdown(results: list[QualityCheckResult]) -> str:
    """Return the Markdown report for the controls that failed or warned, or an empty string if none did."""
    relevant = [result for result in results if result.status in {"FAIL", "WARN"}]
    if not relevant:
        return ""
    lines = [
        "# Generated DataOps Incident Report",
        "",
        "## Scope",
        "",
        "Synthetic local portfolio environment. No real client, bank, insurance or health data.",
        "## Failed or warning controls",
        "",
        "| Control | Status | Severity | Failed rows |",
        "|---|---|---:|---:|",
    ]

    for result in relevant:
        lines.append(
            f"| {result.control_id} : {result.check_name} | "
            f"{result.status} | {result.severity} | {result.failed_rows} |"
        )
    lines.extend(
        [
            "",
            "## Triage steps",
            "",
            "1. Review the failed control.",
            "2. Identify affected table and row pattern.",
            "3. Re-run the relevant SQL query manually.",
            "4. Decide whether to regenerate data, adjust validation or document a known issue.",
            "5. Update the runbook if the failure mode is new.",
            "",
            "## Public-safety note",
            "",
            "Do not paste real operational data into this report.",
        ]
    )

    return "\n".join(lines) + "\n"


def write_incident_report(markdown: str, output_path: Path) -> None:
    """Write the report to disk, and log where, so that an operator knows where to look."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    logger.info(
        "incident_report_written",
        extra={
            "category": "decision",
            "reason": (
                "a failed control produced an incident; the report is the "
                "document an analyst works from and an auditor reads"
            ),
            "path": str(output_path),
            "bytes": len(markdown.encode("utf-8")),
        },
    )


def main() -> None:
    """Command-line entry point: run the controls, then write the report if anything failed or warned."""
    from banking_dataops.logging_config import configure_logging, new_run_id

    configure_logging()
    new_run_id()
    settings = load_settings()
    markdown = generate_incident_markdown(run_quality_checks(settings))
    if not markdown:
        # INFO, not WARNING: "no incident" is the desired outcome, and mixing it with a failure to
        # generate would train an operator to ignore the line.
        logger.info(
            "no_incident_to_report",
            extra={
                "category": "control",
                "reason": "every control passed; there is nothing to escalate",
                "rows": 0,
            },
        )

        print("No incident generated.")
        return
    output_path = settings.output_dir / "incident_report.md"
    write_incident_report(markdown, output_path)
    print(f"Incident report written to {output_path}")


if __name__ == "__main__":
    main()
