-- Exploration queries for five kinds of unusual transaction, run on the Silver
-- tables (transactions, customers). Read only: nothing here creates or changes data.
--
-- Layer: controls, exploration side. No module, script, Makefile target or workflow
-- runs this file, and the tests only check that it exists, is not empty and splits into
-- statements. Run it by hand with psql against the project database.
--
-- The CTRL-013 to CTRL-017 labels name these queries and nothing more. The controls
-- that decide the publication verdict are CTRL-001 to CTRL-007 (quality_checks.py,
-- 02_data_quality_checks.sql), and docs/controls_matrix.md stops at CTRL-010. A hit
-- here is a lead for an analyst, never a reason to block a batch (ADR-0002).
--
-- is_suspicious and risk_score come from a rule in generate_synthetic_data.py, not from
-- the model and not from confirmed investigations (ADR-0003, ADR-0008). The model
-- output is gold.predictions, not these columns.


-- CTRL-013: transactions above 10,000 CHF.
-- Same cut-off as gold.fact_transactions.is_high_amount and the high-amount factor of the
-- generator, and strict like them: exactly 10,000 is not flagged. It is a project
-- parameter: docs/KPI_DEFINITIONS.md gives it as a common AML reporting threshold
-- without citing a source.
SELECT
    transaction_id,
    account_id,
    amount_chf,
    event_timestamp,
    channel,
    is_suspicious
FROM transactions
WHERE amount_chf > 10000
ORDER BY amount_chf DESC;


-- CTRL-014: cross-border, the transaction country differs from the customer country.
-- Same definition as gold.fact_transactions.is_cross_border, which is also a model
-- feature (ml/features.py).
--
-- The customer of a transaction is reached through accounts.customer_id, as in
-- pipeline/gold.py and ml/features.py. An earlier version joined transactions.account_id
-- (ACC-00001-01) directly to customers.customer_id (CUST-00001), which never matches and
-- made this query return no row.
SELECT
    t.transaction_id,
    t.account_id,
    t.country AS transaction_country,
    c.country AS customer_country,
    t.amount_chf,
    t.event_timestamp,
    t.is_suspicious
FROM transactions t
JOIN accounts a ON a.account_id = t.account_id
JOIN customers c ON c.customer_id = a.customer_id
WHERE t.country <> c.country
ORDER BY t.amount_chf DESC;


-- CTRL-015: night transactions, hours 0 to 5.
-- Same cut-off as gold.fact_transactions.is_night and the generator (hour < 6).
-- event_timestamp is a TIMESTAMP without time zone, so the hour is the stored one,
-- with no conversion to the customer local time.
SELECT
    transaction_id,
    account_id,
    EXTRACT(HOUR FROM event_timestamp) AS booking_hour,
    amount_chf,
    event_timestamp,
    channel,
    is_suspicious
FROM transactions
WHERE EXTRACT(HOUR FROM event_timestamp) < 6
ORDER BY event_timestamp;


-- CTRL-016: the same amount booked more than once on the same account and day.
-- CTRL-002 cannot see these: transaction_id is the primary key, and these are distinct
-- transactions with identical values. A hit may be a legitimate repeat such as a
-- standing payment, so it needs a human read.
SELECT
    account_id,
    booking_date,
    amount_chf,
    COUNT(*) AS duplicate_count
FROM transactions
GROUP BY account_id, booking_date, amount_chf
HAVING COUNT(*) > 1
ORDER BY duplicate_count DESC, booking_date;


-- CTRL-017: risk_score above 0.8 and flagged suspicious.
-- risk_score is the generator score, a weighted sum of night, cross-border, high-amount,
-- channel and merchant-category factors plus noise, clipped to [0, 0.9999]. It is not a
-- model output and not a probability of fraud, and the model is forbidden to read it
-- (ml/features.py, ADR-0008).
-- The 0.8 cut-off is defined nowhere else in the repo. The generator sets is_suspicious
-- at risk_score > 0.75 and the DAX measure High Risk Amount uses >= 0.7.
-- On generated rows is_suspicious is exactly risk_score > 0.75, so the second predicate
-- removes nothing there. It does exclude the demo row DEMO-TXN-0002, which carries
-- risk_score 1.42 and is_suspicious FALSE (demo_scenario.py).
SELECT
    transaction_id,
    account_id,
    amount_chf,
    risk_score,
    event_timestamp,
    channel,
    is_suspicious
FROM transactions
WHERE risk_score > 0.8
  AND is_suspicious = TRUE
ORDER BY risk_score DESC;
