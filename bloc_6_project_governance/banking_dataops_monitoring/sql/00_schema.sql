-- Silver layer: the PostgreSQL tables the pipeline loads (customers, accounts,
-- transactions) and the tables the control layer writes about each batch
-- (quality_check_results, incident_reports, reconciliation_results).
--
-- Applied by ingest.create_schema() at the start of every ingest_all(), followed by
-- 07_audit_journal.sql, and by scripts/publish_to_cloud.py on a cloud database. Every
-- statement is IF NOT EXISTS, so replaying the file is harmless. It empties nothing:
-- db.reset_tables() truncates these six tables right afterwards, and that truncate then
-- insert is what lets a batch be replayed identically (ADR-0001).
--
-- ADR-0004: the schema guarantees structure only, through NOT NULL, primary keys and
-- foreign keys. There is deliberately no CHECK on amount_chf, risk_score or status:
-- those rules are CTRL-003, CTRL-005 and CTRL-006 in quality_checks.py, which must be
-- able to see a bad row, record it and block the batch. A CHECK would refuse the insert
-- and leave no trace. The flip side is that CTRL-001, CTRL-002 and CTRL-004 cannot fail
-- on a row inserted into these tables.
--
-- Column order is load-bearing. ingest.py runs COPY ... WITH CSV HEADER without a column
-- list, and PostgreSQL discards the header line instead of matching it, so CSV columns
-- land in table columns by position. The order below is the order of the CSV written by
-- generate_synthetic_data.py. Moving a column here misloads the data, and does so
-- silently when the types happen to agree.


-- Customers and accounts are reference data. Identifiers are opaque and synthetic: no
-- personal identifier is stored (ADR-0003, docs/project/06-conformite-rgpd.md).
CREATE TABLE IF NOT EXISTS customers (
    customer_id TEXT PRIMARY KEY,

    customer_segment TEXT NOT NULL,

    -- Compared with transactions.country by the Gold build to derive is_cross_border
    -- (pipeline/gold.py), so both columns must use the same country codes.
    country TEXT NOT NULL,

    created_at TIMESTAMP NOT NULL
);


CREATE TABLE IF NOT EXISTS accounts (
    account_id TEXT PRIMARY KEY,

    -- The foreign key fixes the load order customers, accounts, transactions
    -- (ingest_all) and the copy order of scripts/publish_to_cloud.py: parents first.
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),

    account_type TEXT NOT NULL,

    currency TEXT NOT NULL,

    opened_at TIMESTAMP NOT NULL
);


-- The central table: every control, reconciliation query, Gold fact and model feature
-- reads from it.
CREATE TABLE IF NOT EXISTS transactions (
    -- The primary key is what makes CTRL-002 unable to fail. demo_scenario.py depends on
    -- it too: it inserts with ON CONFLICT (transaction_id) DO NOTHING, so that injecting
    -- the demonstration batch twice adds no second row.
    transaction_id TEXT PRIMARY KEY,

    -- A transaction with an unknown account is refused at load, which is why CTRL-004
    -- (orphan transactions) cannot fail here.
    account_id TEXT NOT NULL REFERENCES accounts(account_id),

    -- Free text, no CHECK. The generator writes core_banking, card_processor or
    -- payments_gateway. demo_scenario.py tags the rows it injects with "demo_scenario"
    -- and deletes on that value alone, so no real feed may ever use it.
    source_system TEXT NOT NULL,

    event_timestamp TIMESTAMP NOT NULL,

    booking_date DATE NOT NULL,

    -- The range (> 0 and <= 1,000,000) is CTRL-003, not a CHECK.
    amount_chf NUMERIC(18, 2) NOT NULL,

    currency TEXT NOT NULL,

    channel TEXT NOT NULL,

    merchant_category TEXT NOT NULL,

    country TEXT NOT NULL,

    -- The contract is [0, 1] and belongs to CTRL-005. NUMERIC(5,4) holds values up to
    -- 9.9999 in absolute value, wider than the contract, so an out-of-range score is
    -- stored and reaches CTRL-005 instead of being refused at load.
    risk_score NUMERIC(5, 4) NOT NULL,

    -- The reference list (posted, pending, rejected) is CTRL-006, not a CHECK.
    status TEXT NOT NULL,

    -- Training label of the priority classifier (ADR-0008). On synthetic data the
    -- generator sets it to risk_score > 0.75, which is why risk_score is never a model
    -- feature.
    is_suspicious BOOLEAN NOT NULL DEFAULT FALSE,

    -- No default: the value comes from the CSV, where the generator writes the date
    -- anchor of the batch (ADR-0009), not the time of insertion.
    created_at TIMESTAMP NOT NULL
);


-- Outcome of each control for the latest run. GET /verdict and vw_publication_verdict
-- (06_powerbi_star_schema.sql) read the publication verdict from this table: a FAIL
-- blocks publication and a WARN does not. Only the deterministic controls decide
-- (ADR-0002). A past run is traced through audit_events (ADR-0007), not here.
CREATE TABLE IF NOT EXISTS quality_check_results (
    -- Holds the control id (CTRL-001 and so on), not a per-execution id: quality_checks.py
    -- passes the control id and upserts with ON CONFLICT (check_id) DO UPDATE, so running
    -- a control again replaces its row.
    check_id TEXT PRIMARY KEY,

    -- Not a foreign key: the control definitions live in code (get_quality_checks).
    control_id TEXT NOT NULL,

    check_name TEXT NOT NULL,

    -- PASS, WARN or FAIL, without a CHECK. WARN is only produced for controls declared
    -- warn_only, which today is CTRL-007 alone.
    status TEXT NOT NULL,

    -- For CTRL-007 this is a flag (1 = stale, 0 = fresh), not a row count.
    failed_rows INTEGER NOT NULL,

    -- high, medium or low, set per control in quality_checks.py: CTRL-001 to CTRL-004
    -- high, CTRL-005 and CTRL-006 medium, CTRL-007 low.
    severity TEXT NOT NULL,

    -- quality_checks.py supplies the value (one timestamp for the whole run), so the
    -- default is only a fallback.
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- Written by reconciliation.run_reconciliation(), one row per run, under the name
-- source_system_total. Both sides of the comparison are read from the same table, so
-- count_delta and amount_delta are written as a constant 0. This is a coherence check,
-- not a reconciliation between two independent systems: a 0 here does not prove that
-- two systems agree (accepted risk R-09, docs/project/05-registre-des-risques.md).
CREATE TABLE IF NOT EXISTS reconciliation_results (
    -- REC- followed by 12 uppercase hexadecimal characters, generated by reconciliation.py.
    reconciliation_id TEXT PRIMARY KEY,

    reconciliation_name TEXT NOT NULL,

    source_count INTEGER,

    target_count INTEGER,

    count_delta INTEGER,

    source_total NUMERIC(18,2),

    target_total NUMERIC(18,2),

    amount_delta NUMERIC(18,2),

    -- reconciliation.py does not supply it, so the default stamps the wall clock at
    -- insertion.
    executed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- quality_checks.py opens one incident per control whose status is FAIL. A WARN opens
-- none.
CREATE TABLE IF NOT EXISTS incident_reports (
    -- INC- followed by 12 uppercase hexadecimal characters, generated by quality_checks.py.
    -- The insert uses ON CONFLICT (incident_id) DO NOTHING, which needs this primary key.
    incident_id TEXT PRIMARY KEY,

    -- Written as "Failed data control: " followed by the check name.
    title TEXT NOT NULL,

    -- Copied from the severity of the failed control.
    severity TEXT NOT NULL,

    -- The code only ever writes open. GET /incidents lists the rows where status is open,
    -- and the demonstration reset can delete the open incidents of its own controls.
    status TEXT NOT NULL,

    -- Nullable in the schema, always filled by quality_checks.py. Matches
    -- quality_check_results.control_id by value, not by foreign key.
    failed_control TEXT,

    -- Written as the control id followed by "failed with N row(s)".
    summary TEXT NOT NULL,

    -- quality_checks.py supplies the value (the timestamp of the run), so the default is
    -- only a fallback.
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- Single-column indexes, one per column that the queries of sql/02 to sql/05 filter, join
-- or group on. No query plan is recorded in the repository, so their effect at the
-- current volume is unmeasured.
CREATE INDEX IF NOT EXISTS idx_transactions_account_id ON transactions(account_id);

CREATE INDEX IF NOT EXISTS idx_transactions_source_system ON transactions(source_system);

CREATE INDEX IF NOT EXISTS idx_transactions_booking_date ON transactions(booking_date);

CREATE INDEX IF NOT EXISTS idx_transactions_channel ON transactions(channel);

CREATE INDEX IF NOT EXISTS idx_transactions_is_suspicious ON transactions(is_suspicious);

CREATE INDEX IF NOT EXISTS idx_transactions_event_timestamp ON transactions(event_timestamp);

CREATE INDEX IF NOT EXISTS idx_transactions_status ON transactions(status);
