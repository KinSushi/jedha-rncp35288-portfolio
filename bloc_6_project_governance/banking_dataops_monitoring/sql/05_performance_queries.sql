-- Volume and freshness checks on the Silver tables, and two EXPLAIN statements to read
-- query plans by hand. Read only: nothing here creates or changes data.
--
-- Layer: operations, outside the pipeline. No module, script, Makefile target or
-- workflow runs this file, and the tests only check that it exists, is not empty and
-- splits into statements. Run it by hand with psql against the project database.
--
-- Batches are daily (ADR-0001), so "fresh" here means "loaded by the last daily batch",
-- not "seconds old". The database is a local PostgreSQL container (ADR-0005).
--
-- EXPLAIN without ANALYZE prints the estimated plan and executes nothing.


-- Row counts. UNION ALL because the first column differs in every branch, so there is
-- nothing to deduplicate. The column names come from the first SELECT only.
-- Reference values after a default run_pipeline.py (100 customers, 2 accounts each,
-- 1,500 transactions): 100, 200, 1500, and 7 for quality_check_results, which keeps one
-- row per control because quality_checks.py upserts with the control identifier as check_id.
SELECT 'customers' AS table_name, COUNT(*) AS row_count FROM customers

UNION ALL

SELECT 'accounts', COUNT(*) FROM accounts

UNION ALL

SELECT 'transactions', COUNT(*) FROM transactions

UNION ALL

SELECT 'quality_check_results', COUNT(*) FROM quality_check_results;


-- Freshness: the newest event. CTRL-007 (quality_checks.py) applies the same idea as a
-- control: a warning, never a block, when the newest event_timestamp is more than 60 days
-- before CURRENT_TIMESTAMP.
-- With the default fixed date anchor (ADR-0009) the generated events stop on 26 July 2026
-- whatever the calendar day, so this value does not advance, and CTRL-007 starts to warn
-- as soon as CURRENT_TIMESTAMP is more than 60 days past it.
SELECT MAX(event_timestamp) AS latest_event_timestamp
FROM transactions;


-- Plan of the daily volume aggregate. Same grouping as monitoring.load_transaction_volume,
-- which also sums the amounts. The index that could serve it is idx_transactions_booking_date
-- (00_schema.sql). Whether the planner picks it depends on the table statistics, and no
-- plan output is recorded in the repo.
EXPLAIN
SELECT booking_date, COUNT(*) AS transaction_count
FROM transactions
GROUP BY booking_date
ORDER BY booking_date;


-- Plan of the latest suspicious transactions. Close to the dashboard query
-- monitoring.load_suspicious_transactions, which orders by risk_score DESC then
-- event_timestamp DESC and lists its columns instead of *. The ordering used here can
-- follow idx_transactions_event_timestamp, whereas an ordering by risk_score has no index
-- to follow: 00_schema.sql defines none on that column.
EXPLAIN
SELECT *
FROM transactions
WHERE is_suspicious = TRUE
ORDER BY event_timestamp DESC
LIMIT 50;


-- Not done: for much larger volumes, partition transactions by booking_date and review
-- the indexes on source_system, channel, status and is_suspicious against the real
-- workload.
