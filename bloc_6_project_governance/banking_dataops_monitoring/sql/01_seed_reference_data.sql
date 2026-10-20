-- Reference values of the transactions table: source systems, channels and statuses.
--
-- Despite its name this file seeds nothing. It holds no INSERT and no code path runs it:
-- ingest.py applies 00_schema.sql and 07_audit_journal.sql only, and sql/README.md lists
-- files 01 to 05 as analysis queries that create nothing. It is documentation for people
-- reading the SQL. The tests only check that it exists, is not empty and splits into
-- valid statements (tests/test_sql_files_exist.py, tests/test_sql_splitter.py).
--
-- The lists below are copied from generate_synthetic_data.py. The only one that a control
-- enforces is the status list (CTRL-006, ADR-0004: a reference list is a business rule,
-- so it is not a CHECK in 00_schema.sql). No control checks source_system or channel
-- values.
--
-- Adding a status means changing every copy: the generator, CTRL-006 (in quality_checks.py
-- and in 02_data_quality_checks.sql) and KNOWN_STATUSES in pipeline/cleaning.py. No test
-- compares the copies with one another.

-- Source systems written by the generator:
--   core_banking
--   card_processor
--   payments_gateway
-- A fourth value, demo_scenario, exists only while the demonstration batch of
-- demo_scenario.py is injected. It marks the rows to delete when the batch is removed.

-- Channels written by the generator:
--   web
--   mobile
--   branch
--   api

-- Statuses written by the generator, and the only values CTRL-006 accepts:
--   posted
--   pending
--   rejected

-- Placeholder that reads no table. It returns one constant row.
SELECT 'reference_values_documented' AS seed_status;
