-- Gold layer: the gold schema, the materialised star schema, and the table that holds the
-- model output.
--
-- Layer: Gold, with the model output at its edge. Bronze is the CSV files, Silver the tables
-- of 00_schema.sql in the public schema. pipeline/gold.py (step gold of run_pipeline.py)
-- executes this file, then loads the star schema tables. gold.predictions is filled separately by
-- ml/predict.py (make ml-predict), which run_pipeline.py does not call. Also replayed by
-- scripts/publish_to_cloud.py.
-- Read by the dashboard (monitoring.py), huggingface-space/app.py and scripts/smoke_check.py.
-- Power BI does not read these tables: its model imports the vw_ views of
-- 06_powerbi_star_schema.sql.
--
-- Tables rather than the views of 06: Gold is a snapshot. The dashboard reads what the last
-- build wrote instead of recomputing the views, and the content changes only when
-- pipeline/gold.py runs.
--
-- Idempotence: every load is TRUNCATE then INSERT (pipeline/gold.py for the star schema,
-- ml/predict.py for predictions). Gold is derived from Silver and holds no state of its own,
-- so a rerun rebuilds it. An UPSERT would leave rows for transactions that disappeared
-- upstream. CREATE TABLE IF NOT EXISTS does not alter a table that already exists: changing
-- a column in this file has no effect on a database where Gold is already built.
--
-- No FOREIGN KEY is declared here, only primary keys. pipeline/gold.py loads the dimensions
-- before the facts and asserts only that fact_transactions is not empty and has as many rows
-- as the Silver transactions.
--
-- ADR-0002 and ADR-0008: a prediction orders the investigation queue and never blocks
-- publication. ADR-0004: control_family. ADR-0009: priority bands and threshold.

-- Gold has its own schema, apart from the Silver tables and the vw_ views of 06, which are
-- in public.
CREATE SCHEMA IF NOT EXISTS gold;

-- Same columns, in the same order, as vw_dim_date: pipeline/gold.py loads it from the view
-- by position. The row count depends on the day of the build, because the view ends today.
CREATE TABLE IF NOT EXISTS gold.dim_date (
    date_key       DATE    PRIMARY KEY,
    year           INTEGER NOT NULL,
    quarter        INTEGER NOT NULL,
    month_number   INTEGER NOT NULL,
    month_name     TEXT    NOT NULL,
    year_month     TEXT    NOT NULL,
    week_number    INTEGER NOT NULL,
    weekday_number INTEGER NOT NULL,
    weekday_name   TEXT    NOT NULL,
    is_weekend     BOOLEAN NOT NULL
);

-- Loaded by position from vw_dim_customer with SELECT *: keep the same column order.
CREATE TABLE IF NOT EXISTS gold.dim_customer (
    customer_id      TEXT PRIMARY KEY,
    customer_segment TEXT NOT NULL,
    customer_country TEXT NOT NULL,
    customer_since   DATE NOT NULL,
    account_count    INTEGER NOT NULL
);

-- Loaded by position from vw_dim_account with SELECT *: keep the same column order.
CREATE TABLE IF NOT EXISTS gold.dim_account (
    account_id       TEXT PRIMARY KEY,
    customer_id      TEXT NOT NULL,
    account_type     TEXT NOT NULL,
    account_currency TEXT NOT NULL,
    opened_on        DATE NOT NULL,
    customer_segment TEXT NOT NULL,
    customer_country TEXT NOT NULL
);

-- Loaded from vw_dim_control, by position as well.
CREATE TABLE IF NOT EXISTS gold.dim_control (
    control_id     TEXT PRIMARY KEY,
    check_name     TEXT NOT NULL,
    severity       TEXT NOT NULL,
    severity_rank  INTEGER NOT NULL,
    -- Separates what the schema enforces structurally from what only the control layer can
    -- see (ADR-0004): Structural for CTRL-001, CTRL-002 and CTRL-004, Business rule for the
    -- others, as set in vw_dim_control.
    control_family TEXT NOT NULL
);

-- The columns of vw_fact_transactions except currency, plus four derived ones. It is loaded
-- straight from transactions, accounts and customers by pipeline/gold.py, not from the view.
CREATE TABLE IF NOT EXISTS gold.fact_transactions (
    transaction_id       TEXT PRIMARY KEY,
    account_id           TEXT NOT NULL,
    date_key             DATE NOT NULL,
    event_timestamp      TIMESTAMP NOT NULL,
    source_system        TEXT NOT NULL,
    channel              TEXT NOT NULL,
    merchant_category    TEXT NOT NULL,
    transaction_country  TEXT NOT NULL,
    status               TEXT NOT NULL,
    -- Narrower than the Silver NUMERIC(18, 2): an amount of 10^12 or more makes the Gold INSERT
    -- fail.
    amount_chf           NUMERIC(14, 2) NOT NULL,
    -- The generator score, not the model score (ADR-0008). Silver declares it NOT NULL, and
    -- this column does not repeat the constraint.
    risk_score           NUMERIC(5, 4),
    is_suspicious        BOOLEAN NOT NULL,
    -- TRUE for the rows injected by demo_scenario.py (source_system = demo_scenario). They stay
    -- in the table and the analytics queries of monitoring.py filter them out.
    is_demo_row          BOOLEAN NOT NULL DEFAULT FALSE,
    -- Derived by pipeline/gold.py when the table is loaded: booking_hour from event_timestamp,
    -- is_night is hour < 6, is_cross_border is transaction country different from the customer
    -- country, is_high_amount is amount_chf > 10000 (strict). The cut-offs also appear in
    -- generate_synthetic_data.py and in 04_anomaly_queries.sql.
    -- ml/features.py recomputes the same four rules in Python from Silver and does not read
    -- these columns, so each definition exists twice. The tests cover the Python side only
    -- (tests/integration/test_bronze_to_features.py).
    booking_hour         INTEGER NOT NULL,
    is_night             BOOLEAN NOT NULL,
    is_cross_border      BOOLEAN NOT NULL,
    is_high_amount       BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gold_fact_tx_date ON gold.fact_transactions (date_key);

CREATE INDEX IF NOT EXISTS idx_gold_fact_tx_account ON gold.fact_transactions (account_id);

-- Same columns as vw_fact_quality_results, loaded by position. One row per control, holding
-- its latest execution (check_id is the control identifier), not a history.
CREATE TABLE IF NOT EXISTS gold.fact_quality_results (
    check_id     TEXT PRIMARY KEY,
    control_id   TEXT NOT NULL,
    date_key     DATE NOT NULL,
    executed_at  TIMESTAMP NOT NULL,
    status       TEXT NOT NULL,
    failed_rows  INTEGER NOT NULL,
    is_pass      INTEGER NOT NULL,
    is_fail      INTEGER NOT NULL,
    is_warn      INTEGER NOT NULL
);

-- Written by ml/predict.py, which truncates the table and reinserts the whole queue in one
-- transaction. pipeline/gold.py does not touch it, so its rows outlive a Gold rebuild until
-- the next prediction run. It is a queue, not a history: each run replaces the previous one.
CREATE TABLE IF NOT EXISTS gold.predictions (
    -- TRUNCATE in predict.py does not restart this sequence, so the ids keep growing from run
    -- to run. Follow transaction_id instead. This schema does not make it unique
    -- (idx_gold_predictions_tx is a plain index): one row per transaction comes from the
    -- truncate and insert in predict.py.
    prediction_id      BIGSERIAL PRIMARY KEY,
    transaction_id     TEXT      NOT NULL,
    -- Probability from the classifier (predict_proba), rounded to 5 decimals by predict.py.
    -- Not fact_transactions.risk_score, which is the generator score.
    suspicion_score    NUMERIC(6, 5) NOT NULL
                       CHECK (suspicion_score >= 0 AND suspicion_score <= 1),
    -- suspicion_score >= decision_threshold, computed by predict.py and stored, so that each row
    -- keeps the decision it was scored with.
    predicted_suspicious BOOLEAN NOT NULL,
    -- The threshold in force when the row was scored. It is chosen on the training set only
    -- (ADR-0009), rounded to 3 decimals and used in that form everywhere, so the threshold
    -- read beside a score is exactly the one that classified it.
    decision_threshold   NUMERIC(4, 3) NOT NULL,
    -- Triage band, assigned by priority_band() in ml/predict.py (ADR-0009):
    --   high    suspicion_score >= decision_threshold. Predicted suspicious, investigate first.
    --           This is exactly predicted_suspicious.
    --   medium  0.60 x decision_threshold <= suspicion_score < decision_threshold. Grey zone,
    --           worth a look once the high items are done.
    --   low     suspicion_score < 0.60 x decision_threshold.
    -- The bands are anchored on the threshold, so a retrain that moves it moves both boundaries. The
    -- earlier rule put high at 1.30 x threshold, which exceeds 1 for any threshold above about
    -- 0.77, so no row could be high. The 0.60 (MEDIUM_BAND_FLOOR) is kept from that version and
    -- was chosen by the project without a recorded source. ADR-0009 measured it on the test rows
    -- of 27 July 2026: reading everything above it gave a recall of 0.90 for a precision of 0.43.
    -- The CHECK only limits the values. The rule lives in Python, and rows written by hand can
    -- disagree with it. The band and predicted_suspicious come from the unrounded score while
    -- suspicion_score is stored rounded, so a score within 0.000005 of a boundary can look as
    -- if it sits on the other side.
    priority_band      TEXT      NOT NULL CHECK (priority_band IN ('high','medium','low')),
    -- Always the literal suspicion_classifier written by predict.py, not the model family. The
    -- family (logistic regression or random forest) is in the training report and in the
    -- predictions_written audit event.
    model_name         TEXT      NOT NULL,
    -- Timestamp of the training run, YYYYMMDD-HHMMSS (ml/train.py).
    model_version      TEXT      NOT NULL,
    -- Identifier of the ml.predict execution, to join audit_events.run_id. It is not the
    -- run_pipeline.py identifier, since that script does not call predict.
    run_id             TEXT      NOT NULL,
    -- Not supplied by predict.py, so the default applies.
    scored_at          TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gold_predictions_tx ON gold.predictions (transaction_id);

CREATE INDEX IF NOT EXISTS idx_gold_predictions_band ON gold.predictions (priority_band, suspicion_score DESC);

COMMENT ON TABLE gold.predictions IS
    'Model output. Prioritises investigation; never blocks publication. '
    'Blocking is the deterministic control layer''s responsibility (see ADR-0002).';

-- One row per priority band. Read by monitoring.load_prediction_summary, which sorts high,
-- medium, low itself, since the view has no ORDER BY.
-- Inner join: a prediction whose transaction is missing from gold.fact_transactions is not
-- counted, and the join follows transaction_id whatever run produced the score. Demo rows
-- have no prediction, because ml/features.py leaves them out of the scored batch.
-- labelled_suspicious counts fact_transactions.is_suspicious, the label set by the generator
-- rule (ADR-0003, ADR-0008) and not a confirmed investigation.
CREATE OR REPLACE VIEW gold.vw_prediction_summary AS
SELECT
    p.priority_band,
    COUNT(*)::int                                        AS transaction_count,
    ROUND(AVG(p.suspicion_score), 4)                     AS avg_score,
    SUM(f.amount_chf)                                    AS total_amount_chf,
    COUNT(*) FILTER (WHERE f.is_suspicious)::int         AS labelled_suspicious,
    MAX(p.scored_at)                                     AS last_scored_at
FROM gold.predictions p
JOIN gold.fact_transactions f USING (transaction_id)
GROUP BY p.priority_band;
