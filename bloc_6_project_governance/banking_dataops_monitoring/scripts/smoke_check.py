#!/usr/bin/env python3
"""End-to-end health check of a running stack: fifteen assertions, one exit code.

The check logic used to live as Python one-liners embedded in PowerShell strings and in bash. Every one was an
escaping accident waiting to happen, and one did happen: PowerShell escapes a single quote by doubling it, not with
a backslash, so an SQL literal `''` silently terminated the surrounding string and broke the whole script at parse
time, before a single check ran. The logic now lives here, in a language that can be tested; PowerShell and bash
only invoke it and read its exit code.

CHOIX
-----
Retained: one Python script run inside the ``app`` container, printing PASS or FAIL per assertion and exiting 0 only
if every assertion held. It covers the whole chain in six steps: Silver loaded (3), controls executed (2), Gold built
and matching Silver (3), model and predictions (3), audit journal (3), and the independence of the verdict from the
model (1). A missing table is a failed check, not a stack trace (``scalar`` returns -1), so that the other results
stay visible. Colour codes are printed only on a terminal; on Windows virtual-terminal processing is switched on
first, otherwise escape codes would print as garbage.
Rejected: embedding the checks in ``run.ps1`` or ``smoke_test.sh`` (the escaping defect above), and counting every
audit event ever recorded (the journal is never pruned, so such a check could only degrade).

OÙ
---
Role: the end-to-end acceptance check. Callers: ``run.ps1 smoke`` and ``scripts/smoke_test.sh`` (both run it with
``docker compose exec -T app python scripts/smoke_check.py``), through the Makefile target ``smoke``. Depends on
``banking_dataops.db`` and, for the last check, ``banking_dataops.api``. Reads the Silver and Gold tables,
``quality_check_results``, ``gold.predictions``, ``audit_events`` and the model artefact
``suspicion_classifier.joblib`` of the ``models`` directory. It is read-only. The dashboard is probed
separately, from the host, by the two callers.

COMMENT
-------
Each assertion is one ``check(label, condition, detail)``; ``scalar`` runs a query and returns its first value or -1.
"No control in failure" counts only ``FAIL``: a freshness warning (CTRL-007) does not make the stack "not ready". The
invariant check is scoped to the last run whose journal says "step pipeline began", because the latest run
identifier is often a unit test and an unbounded count would turn red for good after one rehearsal. The last check
reads the source of ``api.verdict`` and refuses if it mentions ``gold.predictions`` or ``suspicion_score``.
Tests that prove it: tests/test_smoke_check.py makes each of the fifteen checks fail in turn on a database
replaced by a table of counts and reads the exit code; against a running stack the script is itself the test.
Its output is saved in ``evidence/smoke_check_full.txt``.

POURQUOI
--------
- Exit code 0 means the project is demonstrable, anything else means it is not (POSIX convention).
- The ADR-0002 check reads source code rather than trusting documentation: the single most important
  architectural guarantee is that a model score cannot influence the publication decision.
- The check on the model artefact only tests that the file exists; it does not test that the predictions come from
  it. That the bands follow the threshold (ADR-0009) is covered by tests/test_ml_predict.py, not here.
- Fifteen assertions: counted in the source; the figure "15/15" in the demo screenshots matches. Other documents
  quote other numbers: source : inconnue for the origin of those.

JOURNALISATION
--------------
Standard output only: this is a command-line entry point, so ``print`` is its output channel. It writes nothing to
``audit_events``; it reads that table. Failures are shown with their label and detail, and summarised in the final
banner ("DEMO READY" or "NOT READY").
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast


# Colour codes are emitted only when the output is a terminal. A CI log or a
# redirected file gets clean text instead of escape sequences.
#
# WINDOWS: Windows PowerShell 5.1 does not interpret ANSI sequences by default.
# Emitting them there prints literal garbage like "<-[0;32m PASS" in front of
# every line — precisely the console someone would be projecting during a
# demonstration. The Windows console supports ANSI since Windows 10, but the
# mode has to be switched on explicitly. We try; if it fails, we fall back to
# plain text rather than print escape codes at an audience.
def _enable_ansi_on_windows() -> bool:
    """Turn on virtual-terminal processing. Returns True if colours are usable."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


_TTY = sys.stdout.isatty() and _enable_ansi_on_windows()
GREEN = "\033[0;32m" if _TTY else ""
RED = "\033[0;31m" if _TTY else ""
YELLOW = "\033[0;33m" if _TTY else ""
OFF = "\033[0m" if _TTY else ""
ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "models" / "suspicion_classifier.joblib"
_results: list[bool] = []


def step(title: str) -> None:
    """Print a section header."""

    print(f"\n{YELLOW}==> {title}{OFF}")


def check(label: str, condition: bool, detail: str = "") -> bool:
    """Record one assertion and print its outcome."""

    suffix = f"  {detail}" if detail else ""
    if condition:
        print(f"{GREEN}  PASS{OFF}  {label}{suffix}")
    else:
        print(f"{RED}  FAIL{OFF}  {label}{suffix}")
    _results.append(bool(condition))
    return bool(condition)


def scalar(query: str, default: int = -1) -> int:
    """Return the first column of the first row, or a default on failure.

    WHY it swallows the exception: a missing table is a legitimate answer here
    — it means that step has not run — and it must be reported as a failed
    check rather than a stack trace that hides the eleven other results.
    """

    try:
        from banking_dataops.db import fetch_rows

        rows = fetch_rows(query)
        if not rows:
            return default
        return int(cast("int", next(iter(rows[0].values()))))
    except Exception:
        return default


def main() -> int:
    """Run every check and return a POSIX exit code."""

    step("1. Silver layer")
    transactions = scalar("SELECT COUNT(*) AS n FROM transactions")
    check("transactions loaded", transactions > 0, f"({transactions} rows)")
    accounts = scalar("SELECT COUNT(*) AS n FROM accounts")
    check("accounts loaded", accounts > 0, f"({accounts} rows)")
    customers = scalar("SELECT COUNT(*) AS n FROM customers")
    check("customers loaded", customers > 0, f"({customers} rows)")
    step("2. Quality controls")
    controls = scalar("SELECT COUNT(*) AS n FROM quality_check_results")
    check("7 controls persisted", controls == 7, f"(found {controls})")
    failed = scalar("SELECT COUNT(*) AS n FROM quality_check_results WHERE status = 'FAIL'")
    check("no control in failure", failed == 0, f"({failed} failing)")
    step("3. Gold layer")
    gold = scalar("SELECT COUNT(*) AS n FROM gold.fact_transactions")
    check("gold.fact_transactions populated", gold > 0, f"({gold} rows)")
    # A gap between Silver and Gold means a join dropped rows. Every downstream
    # aggregate and every model score would then run on an undeclared subset.
    check(
        "gold matches silver exactly",
        gold == transactions and gold > 0,
        f"(gold {gold} vs silver {transactions})",
    )
    dims = scalar(
        """
        SELECT COUNT(*) AS n FROM (
            (SELECT 1 FROM gold.dim_date     LIMIT 1) UNION ALL
            (SELECT 1 FROM gold.dim_customer LIMIT 1) UNION ALL
            (SELECT 1 FROM gold.dim_account  LIMIT 1) UNION ALL
            (SELECT 1 FROM gold.dim_control  LIMIT 1)
        ) AS populated
        """
    )
    check("all 4 gold dimensions populated", dims == 4, f"({dims}/4)")
    step("4. Machine learning")
    check(
        "model artefact present",
        MODEL_PATH.exists(),
        "" if MODEL_PATH.exists() else "- train it first",
    )
    predictions = scalar("SELECT COUNT(*) AS n FROM gold.predictions")
    check("gold.predictions populated", predictions > 0, f"({predictions} rows)")
    # A probability outside [0, 1] means the model output is corrupted, and the
    # investigation queue built from it would be meaningless.
    bad_scores = scalar("SELECT COUNT(*) AS n FROM gold.predictions WHERE suspicion_score < 0 OR suspicion_score > 1")
    check("every suspicion score within [0, 1]", bad_scores == 0, f"({bad_scores} outside)")
    step("5. Audit trail")
    events = scalar("SELECT COUNT(*) AS n FROM audit_events")
    check("audit journal populated", events > 0, f"({events} events)")
    # The journal refuses an event without a reason at construction time; this
    # verifies the guarantee actually held in practice.
    no_reason = scalar("SELECT COUNT(*) AS n FROM audit_events WHERE reason IS NULL OR length(trim(reason)) = 0")
    check("every event carries a business reason", no_reason == 0, f"({no_reason} without)")
    # Scoped to the last full pipeline run, deliberately.
    # WHY NOT THE WHOLE JOURNAL: the journal is append-only and never pruned, so
    # an unbounded count can only degrade over time. One reverse-path test
    # asserting that a broken invariant *is* recorded, or one demo injection
    # replayed during a rehearsal, would turn this check red permanently — while
    # saying nothing about the state being demonstrated.
    # WHY NOT SIMPLY THE LATEST run_id: every RunJournal opens its own run_id,
    # including the ones the test suite opens when it runs against a live
    # database. The latest run_id is often a unit test, not the pipeline.
    # WHAT THIS ASKS: "did the last end-to-end pipeline run break an invariant?"
    violations = scalar(
        "SELECT COUNT(*) AS n FROM audit_events "
        "WHERE action = 'invariant_violated' "
        "AND run_id = ("
        "  SELECT run_id FROM audit_events "
        "  WHERE action = 'run_started' AND reason = 'step pipeline began' "
        "  ORDER BY occurred_at DESC LIMIT 1"
        ")"
    )
    check("no invariant was violated", violations == 0, f"({violations} violations)")
    step("6. Publication verdict independence")
    # The single most important architectural guarantee: a model score must
    # never be able to influence the publication decision. Verified by reading
    # the source of the endpoint, not by trusting the documentation.
    try:
        import inspect

        from banking_dataops import api

        source = inspect.getsource(api.verdict)
        independent = "gold.predictions" not in source and "suspicion_score" not in source
    except Exception:
        independent = False
    check("verdict does not read model predictions (ADR-0002)", independent)
    passed = sum(1 for r in _results if r)
    failures = len(_results) - passed
    print("\n" + "=" * 54)
    if failures == 0:
        print(f"{GREEN}  DEMO READY  -  {passed} checks passed{OFF}")
        print("=" * 54 + "\n")
        return 0
    print(f"{RED}  NOT READY  -  {failures} failure(s), {passed} passed{OFF}")
    print("=" * 54 + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
