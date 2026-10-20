-- Five aggregate summaries of the transactions table, one per dimension: source system,
-- booking date, channel, status and suspicious flag. Reconciliation layer, read-only.
--
-- The pipeline does not run this file. reconciliation.py keeps its own copy of each query
-- (SOURCE_SYSTEM_QUERY, DAILY_RECONCILIATION_QUERY, CHANNEL_SUMMARY_QUERY,
-- STATUS_SUMMARY_QUERY, SUSPICIOUS_SUMMARY_QUERY). That copy runs through
-- python -m banking_dataops.reconciliation, started by make reconcile and by
-- docker/entrypoint.sh, not by run_pipeline.py. This file is the readable version for
-- analysts running a summary by hand. No test compares the two copies, so a change has to
-- be made in both.
--
-- Why several dimensions: a batch can pass every row-level control and still be wrong as
-- a whole, for instance a feed that delivers half its rows. Each summary shows a
-- different way for the totals to be off.
--
-- Limit, on purpose (R-09, docs/project/05-registre-des-risques.md): every figure comes
-- from the same table, so nothing here is compared with an independent second system. A
-- true reconciliation needs that second source. The queries return rows for a person or a
-- dashboard to read, with no threshold and no PASS or FAIL. The first one is the
-- source-system reconciliation listed as CTRL-008 in docs/controls_matrix.md. The other
-- four have no control id there.
--
-- ROUND(AVG(risk_score), 4) uses the scale of the column (NUMERIC(5,4)): AVG of a numeric
-- returns more decimals than the column holds.


-- By source system. A fourth group, demo_scenario, appears while the demonstration batch
-- of demo_scenario.py is injected. A source system that delivered nothing is absent from
-- the result, not shown as zero.
SELECT
    source_system,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    MIN(event_timestamp) AS first_event_timestamp,
    MAX(event_timestamp) AS last_event_timestamp
FROM transactions
GROUP BY source_system
ORDER BY source_system;


-- By booking date. FILTER inside the aggregate keeps a day with no suspicious transaction
-- in the result with a count of 0, where a WHERE clause would drop the day. A day with no
-- transaction at all is still absent: finding a gap means comparing the dates.
SELECT
    booking_date,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    COUNT(*) FILTER (WHERE is_suspicious) AS suspicious_count
FROM transactions
GROUP BY booking_date
ORDER BY booking_date;


-- By channel. non_posted_count counts pending, rejected and unknown statuses alike, that
-- is anything but posted. FILTER keeps a channel whose transactions are all posted in the
-- result with a count of 0.
SELECT
    channel,
    COUNT(*) AS transaction_count,
    ROUND(AVG(risk_score), 4) AS avg_risk_score,
    COUNT(*) FILTER (WHERE status <> 'posted') AS non_posted_count
FROM transactions
GROUP BY channel
ORDER BY transaction_count DESC;


-- By status. A status outside the reference list (CTRL-006) is not filtered out: it shows
-- as a row of its own, which is how an unknown value becomes visible here.
SELECT
    status,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf
FROM transactions
GROUP BY status
ORDER BY status;


-- By suspicious flag. is_suspicious is not a model output: the generator sets it to
-- risk_score > 0.75, so the suspicious group has the higher average risk score by
-- construction and a gap between the two averages says nothing about a model. Model
-- scores live in gold.predictions and never enter a verdict (ADR-0002, ADR-0008).
SELECT
    is_suspicious,
    COUNT(*) AS transaction_count,
    ROUND(SUM(amount_chf), 2) AS total_amount_chf,
    ROUND(AVG(risk_score), 4) AS avg_risk_score
FROM transactions
GROUP BY is_suspicious
ORDER BY is_suspicious DESC;
