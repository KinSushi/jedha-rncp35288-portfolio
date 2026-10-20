-- SQL form of the seven data-quality controls CTRL-001 to CTRL-007, each returning
-- (check_name, failed_rows) over the transactions table. Control layer, Silver tables.
--
-- The pipeline does not run this file. quality_checks.py keeps its own copy of every
-- predicate in get_quality_checks() (it returns the count alone) and that copy is what
-- run_pipeline.py executes, stores in quality_check_results and turns into incidents.
-- This file is the readable version for analysts running a control by hand (sql/README.md
-- lists it among the analysis queries). No test compares the two copies, so a change to a
-- rule has to be made in both.
--
-- The severity and the warn-only flag are not carried here, quality_checks.py sets them:
-- CTRL-001 to CTRL-004 high, CTRL-005 and CTRL-006 medium, CTRL-007 low and warn-only.
-- The rules are deterministic on purpose and decide whether a batch can be published
-- (ADR-0002). Why each one exists is recorded as an audit reason in CONTROL_REASONS.
--
-- ADR-0004 splits the work with the schema: 00_schema.sql guarantees structure, these
-- queries check meaning. CTRL-001, CTRL-002 and CTRL-004 are kept even though they cannot
-- fail on rows inserted into that schema, because a control that never fires still has to
-- be shown not to fire (docs/KPI_DEFINITIONS.md). Thresholds and their origin are listed
-- in the same document.


-- CTRL-001 critical_nulls. A null in a mandatory field means an upstream system dropped a
-- value. Cannot fail here: transaction_id is the primary key and the three other columns
-- are NOT NULL.
SELECT 'critical_nulls' AS check_name, COUNT(*) AS failed_rows
FROM transactions
WHERE transaction_id IS NULL
   OR account_id IS NULL
   OR event_timestamp IS NULL
   OR amount_chf IS NULL;


-- CTRL-002 duplicate_transactions. failed_rows is the number of identifiers that appear
-- more than once, not the number of surplus rows. Cannot fail here: transaction_id is the
-- primary key.
SELECT 'duplicate_transactions' AS check_name, COUNT(*) AS failed_rows
FROM (
    SELECT transaction_id
    FROM transactions
    GROUP BY transaction_id
    HAVING COUNT(*) > 1
) duplicates;


-- CTRL-003 invalid_amounts. An amount outside the plausibility band usually means a
-- decimal shift or a currency applied twice upstream. The ceiling of CHF 1,000,000 is a
-- retail-banking plausibility bound to recalibrate on real data. Zero is invalid and the
-- ceiling itself is accepted. A null amount matches neither test and is left to CTRL-001.
SELECT 'invalid_amounts' AS check_name, COUNT(*) AS failed_rows
FROM transactions
WHERE amount_chf <= 0
   OR amount_chf > 1000000;


-- CTRL-004 orphan_transactions. LEFT JOIN rather than an inner join, so that a transaction
-- without an account survives the join and can be counted. accounts.account_id is the
-- primary key, therefore never null on a match: a null after the join means no account.
-- Cannot fail here: transactions.account_id carries a foreign key.
SELECT 'orphan_transactions' AS check_name, COUNT(*) AS failed_rows
FROM transactions t
LEFT JOIN accounts a ON t.account_id = a.account_id
WHERE a.account_id IS NULL;


-- CTRL-005 invalid_risk_score. The range [0, 1] is the contract of the scoring engine, not
-- a business choice, and both bounds are valid. A score outside it means the engine
-- returned a raw value instead of a normalised one. The column type accepts up to 9.9999,
-- so such a value is stored and reaches this control.
SELECT 'invalid_risk_score' AS check_name, COUNT(*) AS failed_rows
FROM transactions
WHERE risk_score < 0
   OR risk_score > 1;


-- CTRL-006 invalid_status. The reference list is written out here, in quality_checks.py,
-- in the generator and in cleaning.py (see 01_seed_reference_data.sql). A status outside
-- it means an upstream system changed without the referential being aligned.
SELECT 'invalid_status' AS check_name, COUNT(*) AS failed_rows
FROM transactions
WHERE status NOT IN ('posted', 'pending', 'rejected');


-- CTRL-007 stale_data. Warn-only: a stale dataset signals a broken feed rather than bad
-- data, so it never blocks. The 60 days are two monthly closing cycles. failed_rows is a
-- flag (1 stale, 0 fresh), not a row count.
--
-- Two behaviours to know. First, the age is measured against the wall clock
-- (CURRENT_TIMESTAMP), not against the date anchor of the batch (ADR-0009): every event
-- of a batch generated with the default anchor, 2026-07-27, falls in the 30 days before
-- it, so this control returns 1 from about 60 days after that date, whatever the pipeline
-- did. Second, on an empty table MAX is null, the comparison is not true and the CASE
-- returns 0, so an empty table reads as fresh. What prevents that in practice is the
-- invariant in ingest.py, which refuses an empty load.
SELECT 'stale_data' AS check_name,
       CASE
           WHEN MAX(event_timestamp) < CURRENT_TIMESTAMP - INTERVAL '60 days'
           THEN 1
           ELSE 0
       END AS failed_rows
FROM transactions;
