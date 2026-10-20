#!/usr/bin/env python3
"""Create the star-schema views that the Power BI report imports, and check they all exist.

Power BI needs a star schema: dimension and fact tables. Defining it in SQL, in the repository, rather than
in Power Query inside the report keeps the logic where it can be versioned, reviewed and audited. A
transformation buried in a report file is invisible to code review and to an auditor.

CHOIX
-----
Retained:

- The star schema is eight SQL views in the ``public`` schema, defined in ``sql/06_powerbi_star_schema.sql``:
  four dimensions (date, customer, account, control), three facts (transactions, quality results, incidents)
  and the publication verdict.
- Views, not copies: they hold no data of their own and always reflect the current Silver tables, so there is
  no second copy to keep in sync. The Gold layer (``pipeline.gold``) later materialises some of them into
  tables for the readers that query often.
- ``CREATE OR REPLACE VIEW``: the creation is idempotent and can be re-run at any time.
- After creation the module checks that every one of the eight names is present (``EXPECTED_VIEWS``).

Rejected:

- Modelling the star schema in Power Query: not versionable as text, not reviewable, not testable.
- Tables instead of views for the Power BI model: a copy to refresh. Source : inconnue for why Power BI reads
  views while the dashboard reads Gold tables.
- Trusting the SQL file to create everything: a typo could create six views out of eight without raising, and
  the two missing ones would only appear as an empty report panel.

OÙ
---
Role: the Power BI interface, between Silver and the report. Callers: ``python -m banking_dataops.powerbi_views``
(Makefile target ``powerbi-views``, ``run.ps1 powerbi``) and, for the SQL file only, ``pipeline.gold``, which
runs ``sql/06_powerbi_star_schema.sql`` itself and imports ``ROOT`` from this module. ``run_pipeline.py`` does not
call ``create_views``. Depends on ``banking_dataops.db``, ``banking_dataops.audit`` and
``banking_dataops.config``. Creates the views listed in ``EXPECTED_VIEWS``; read by Power BI
(``docs/powerbi/``), by the Gold build and by the Makefile target ``powerbi-check``.

COMMENT
-------
``create_views`` runs the SQL file in one transaction (``db.execute_sql_file``), lists the views present in the
``public`` schema whose name starts with ``vw_`` (``list_views``), asserts that none of the expected ones is
missing, and journals the creation. ``main`` creates the views and prints ``ok`` or ``MISSING`` per name.
Tests that prove it: no unit test runs it (it needs PostgreSQL). tests/test_sql_files_exist.py checks that
the SQL file exists and tests/test_reverse_paths.py
(``test_every_state_changing_module_is_journalled_and_the_scan_refuses_a_module_that_leaves_no_trace``) that the
module leaves a trace; the views are checked for real by ``scripts/smoke_check.py`` and by the Makefile target
``powerbi-check``.

POURQUOI
--------
- Journalled although idempotent: "the views exist" is a precondition of the Gold build and of the report;
  when either fails, the first question is whether this step ran at all, and a step that leaves no trace
  cannot answer it (docs/CODE_STANDARD.md, section 3).
- The invariant exists because a missing view appears as an empty Power BI panel rather than as an error: the
  report would look built and show nothing.
- The count of views is eight here; docs/pipeline_architecture.md says nine. The code and the SQL file define
  eight, so that document is out of date.

JOURNALISATION
--------------
Under ``RunJournal("powerbi_views")``: the run start and end, the invariant ``every declared star-schema view
exists after creation`` (category ``integrity``) and the event ``star_schema_views_created`` (category ``data``,
with the number and names of the views). They go to the structured log at INFO and, best effort, to
``audit_events``. A missing view raises ``AssertionError``. ``main`` prints one line per view.
"""

from __future__ import annotations

from pathlib import Path

from banking_dataops.audit import RunJournal, assert_invariant
from banking_dataops.config import Settings, load_settings
from banking_dataops.db import execute_sql_file, fetch_rows
from banking_dataops.logging_config import get_logger

logger = get_logger("powerbi_views")

ROOT = Path(__file__).resolve().parents[2]
STAR_SCHEMA_SQL = ROOT / "sql" / "06_powerbi_star_schema.sql"

#: The views sql/06_powerbi_star_schema.sql must create. Kept in sync by hand; the invariant in ``create_views``
#: catches a name that is missing from the database.
EXPECTED_VIEWS = (
    "vw_dim_date",
    "vw_dim_customer",
    "vw_dim_account",
    "vw_dim_control",
    "vw_fact_transactions",
    "vw_fact_quality_results",
    "vw_fact_incidents",
    "vw_publication_verdict",
)


def create_views(settings: Settings | None = None) -> None:
    """Create or replace every star-schema view, then check that all of them exist.

    Journalled although idempotent: when the Gold build or the report fails, the first question is
    whether this step ran at all. The invariant afterwards catches a view missing from the database.
    """
    runtime = settings or load_settings()
    with RunJournal("powerbi_views", settings=runtime) as run:
        execute_sql_file(STAR_SCHEMA_SQL, settings=runtime)
        present = set(list_views(runtime))
        missing = [view for view in EXPECTED_VIEWS if view not in present]
        assert_invariant(
            not missing,
            invariant="every declared star-schema view exists after creation",
            reason=(
                "a missing view surfaces as an empty Power BI panel rather than "
                "an error; the report would look built and show nothing"
            ),
            expected=len(EXPECTED_VIEWS),
            missing=missing,
        )

        run.note(
            "star_schema_views_created",
            reason="the star schema the Gold build and the Power BI model both read",
            row_count=len(EXPECTED_VIEWS),
            views=list(EXPECTED_VIEWS),
        )


def list_views(settings: Settings | None = None) -> list[str]:
    """Return the ``vw_`` views present in the ``public`` schema, sorted by name."""
    rows = fetch_rows(
        """
        SELECT table_name
        FROM information_schema.views
        WHERE table_schema = 'public' AND table_name LIKE 'vw_%'
        ORDER BY table_name
        """,
        settings=settings,
    )

    return [str(row["table_name"]) for row in rows]


def main() -> None:
    """Command-line entry point: create the views and print ok or MISSING for each expected name."""
    create_views()
    present = list_views()
    for view in EXPECTED_VIEWS:
        mark = "ok" if view in present else "MISSING"
        print(f"{mark:>8}  {view}")


if __name__ == "__main__":
    main()
