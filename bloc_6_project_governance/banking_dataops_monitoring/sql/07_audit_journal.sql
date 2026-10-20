-- Audit journal: the audit_events table and the vw_run_summary view.
--
-- Layer: audit, across every step of the pipeline. Created by ingest.create_schema right
-- after 00_schema.sql, because the first event is emitted by ingestion itself and the table
-- must exist by then. Also replayed by scripts/publish_to_cloud.py. Written by
-- banking_dataops.audit (record_event, RunJournal, assert_invariant). Read by make
-- audit-trail and make audit-runs, scripts/smoke_check.py and huggingface-space/app.py.
-- Safe to rerun: IF NOT EXISTS and CREATE OR REPLACE.
--
-- ADR-0007. The structured log says what happens while a step runs. This table says what
-- happened and why, months later and in SQL, to someone who was not there. The log is always
-- written and the table only on a best-effort basis, so a lost row here never stops a step.
-- It is separate from quality_check_results, which holds outcomes (one row per control,
-- overwritten at each run): this table holds the acts that produced them, such as a run
-- started, rows loaded, an invariant checked, a verdict issued.
--
-- Retention: no purge. A retention period is a governance decision, still open before any
-- production use (ADR-0007, docs/project/06-conformite-rgpd.md).
--
-- Append only by convention. The application only inserts and reads, and db.reset_tables
-- leaves this table alone. The database enforces neither (no trigger, no REVOKE), and
-- scripts/publish_to_cloud.py truncates its copy in the target database before refilling it.

CREATE TABLE IF NOT EXISTS audit_events (
    event_id     BIGSERIAL PRIMARY KEY,
    -- Correlates every event of one execution: 16 hexadecimal characters from
    -- banking_dataops.logging_config.new_run_id. The format is not enforced here.
    run_id       TEXT        NOT NULL,
    -- Closed list, mirrored in banking_dataops.audit.EVENT_CATEGORIES, and
    -- tests/test_reverse_paths.py checks that the six values appear in this file. The constraint
    -- is declarative on purpose: an unknown category must be rejected by the database even if
    -- the application layer is bypassed.
    category     TEXT        NOT NULL
                 CHECK (category IN ('pipeline','data','control','decision','demo','integrity')),
    -- What happened, as a past-tense label such as table_loaded, control_fail or run_failed.
    action       TEXT        NOT NULL CHECK (length(trim(action)) > 0),
    -- Why it happened, in business terms: the column an auditor reads. NOT NULL and non-empty,
    -- because a journal that records what without why is a log (ADR-0007). AuditEvent refuses an
    -- empty reason too, and that Python side is tested in tests/test_reverse_paths.py. This SQL
    -- constraint is not exercised by any test, since the test suite never reaches PostgreSQL.
    reason       TEXT        NOT NULL CHECK (length(trim(reason)) > 0),
    -- How many rows were affected. NULL when the notion does not apply, for example run_started.
    row_count    INTEGER     CHECK (row_count IS NULL OR row_count >= 0),
    -- Free structured context: the keyword arguments of record_event, serialised by
    -- audit._as_json. jsonb rather than json so that it can be queried and indexed.
    details      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- audit._persist_event always supplies it, in UTC with the time zone dropped. The DEFAULT
    -- only serves rows inserted by hand.
    occurred_at  TIMESTAMP   NOT NULL DEFAULT NOW()
);

-- The trail of one run, in time order: audit.audit_trail filters on run_id and sorts on
-- occurred_at DESC.
CREATE INDEX IF NOT EXISTS idx_audit_events_run
    ON audit_events (run_id, occurred_at DESC);

-- Events of one category over a period, for example the integrity events of a month.
-- Satisfied and violated invariants share that category, so a query for violations
-- still filters on action = invariant_violated.
CREATE INDEX IF NOT EXISTS idx_audit_events_category_time
    ON audit_events (category, occurred_at DESC);

COMMENT ON TABLE audit_events IS
    'Traceable acts. Answers what ran, on what data, and why the result is what it is.';

-- One row per run, the entry point of an investigation. Read by make audit-runs. It is not
-- one of the eight star-schema views of 06 (powerbi_views.EXPECTED_VIEWS).
-- duration_seconds is the span between the first and the last event of the run.
-- A step journal opened inside another one reuses its run_id (audit.RunJournal), so a
-- pipeline run holds the events of every step, and failures counts one run_failed per
-- journal that the exception crosses: one broken step can show failures above 1.
-- The two action names below are written by RunJournal.__exit__ (run_failed) and by
-- assert_invariant (invariant_violated). Renaming either in audit.py would make this view
-- report clean for runs that failed.
CREATE OR REPLACE VIEW vw_run_summary AS
SELECT
    run_id,
    MIN(occurred_at)                                              AS started_at,
    MAX(occurred_at)                                              AS ended_at,
    EXTRACT(EPOCH FROM (MAX(occurred_at) - MIN(occurred_at)))::int AS duration_seconds,
    COUNT(*)::int                                                 AS event_count,
    COUNT(*) FILTER (WHERE action = 'run_failed')::int            AS failures,
    COUNT(*) FILTER (WHERE action = 'invariant_violated')::int     AS violations,
    (COUNT(*) FILTER (WHERE action IN ('run_failed','invariant_violated')) = 0) AS clean
FROM audit_events
GROUP BY run_id;

COMMENT ON VIEW vw_run_summary IS
    'clean = the run ended without failure and without invariant violation.';
