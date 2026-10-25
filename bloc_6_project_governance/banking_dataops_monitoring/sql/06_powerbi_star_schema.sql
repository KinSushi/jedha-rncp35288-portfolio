-- Power BI star schema: eight views over the Silver tables, in the public schema.
--
-- Layer: reporting. The Power BI model imports exactly these views and no table
-- (docs/powerbi/README.md). Other readers: pipeline/gold.py (it loads the Gold dimensions
-- from them), make powerbi-check (vw_publication_verdict) and huggingface-space/app.py
-- (verdict and control views).
-- Applied by banking_dataops.powerbi_views.create_views (make powerbi-views), by
-- pipeline/gold.py before every Gold build, and by scripts/publish_to_cloud.py. Safe to
-- rerun, every view is CREATE OR REPLACE.
--
-- The star schema is modelled here, not in Power Query, so that its logic is versioned and
-- open to code review and to an auditor.
--
-- Column order matters. pipeline/gold.py fills gold.dim_date, dim_customer, dim_account,
-- dim_control and fact_quality_results from the views below with INSERT ... SELECT and no
-- target column list, so columns are matched by position. Adding, removing or moving a
-- column in one of these five views breaks the Gold build unless 08_gold_layer.sql changes
-- with it. gold.fact_transactions is not loaded from vw_fact_transactions, and
-- vw_fact_incidents has no Gold table.
--
-- ADR-0002: the publication verdict comes from the deterministic controls, never from a
-- model score. ADR-0004: control_family exposes which controls the schema already enforces.

-- Gapless calendar, as the COMMENT ON VIEW below says. It runs from the earlier of the first
-- booking date and 90 days before today, to the later of the last booking date and today.
-- It always covers at least the last 90 days, and it extends to today even when the data
-- stops earlier, as it does with the fixed default anchor (ADR-0009). Its length therefore
-- depends on the day it is read. The origin of the 90 is not documented in the repo.
CREATE OR REPLACE VIEW vw_dim_date AS
WITH bounds AS (
    SELECT
        LEAST(MIN(booking_date), CURRENT_DATE - INTERVAL '90 days')::date AS date_from,
        GREATEST(MAX(booking_date), CURRENT_DATE)::date                   AS date_to
    FROM transactions
)
SELECT
    d::date                                              AS date_key,
    EXTRACT(YEAR    FROM d)::int                         AS year,
    EXTRACT(QUARTER FROM d)::int                         AS quarter,
    EXTRACT(MONTH   FROM d)::int                         AS month_number,
    TO_CHAR(d, 'TMMonth')                                AS month_name,
    TO_CHAR(d, 'YYYY-MM')                                AS year_month,
    EXTRACT(WEEK    FROM d)::int                         AS week_number,
    EXTRACT(ISODOW  FROM d)::int                         AS weekday_number,
    TO_CHAR(d, 'TMDay')                                  AS weekday_name,
    -- ISODOW runs from Monday = 1 to Sunday = 7. Same weekend as ml/features.py, where
    -- is_weekend is day_of_week >= 5 with Monday = 0.
    (EXTRACT(ISODOW FROM d) >= 6)                        AS is_weekend
FROM bounds, generate_series(bounds.date_from, bounds.date_to, INTERVAL '1 day') AS d;

COMMENT ON VIEW vw_dim_date IS
    'Continuous date dimension. Required by Power BI time-intelligence functions, '
    'which need a gapless calendar and break on a date column derived from facts.';

-- LEFT JOIN below: a customer without an account still appears, with account_count = 0,
-- because COUNT(a.account_id) ignores the NULL that the join produces.
CREATE OR REPLACE VIEW vw_dim_customer AS
SELECT
    c.customer_id,
    c.customer_segment,
    c.country                AS customer_country,
    c.created_at::date       AS customer_since,
    COUNT(a.account_id)::int AS account_count
FROM customers c
LEFT JOIN accounts a ON a.customer_id = c.customer_id
GROUP BY c.customer_id, c.customer_segment, c.country, c.created_at;

-- customer_segment and customer_country are repeated on the account: the Gold cohort query
-- monitoring.load_segment_cohorts reads them from gold.dim_account without a second join.
-- The INNER JOIN drops no account, since accounts.customer_id is NOT NULL with a foreign key.
CREATE OR REPLACE VIEW vw_dim_account AS
SELECT
    a.account_id,
    a.customer_id,
    a.account_type,
    a.currency          AS account_currency,
    a.opened_at::date   AS opened_on,
    c.customer_segment,
    c.country           AS customer_country
FROM accounts a
JOIN customers c ON c.customer_id = a.customer_id;

-- Derived from quality_check_results, not from a control catalogue: the view is empty before
-- the first run, and a control with no result row has no row here. DISTINCT collapses nothing
-- today because that table keeps one row per control (check_id is the control identifier).
CREATE OR REPLACE VIEW vw_dim_control AS
SELECT DISTINCT
    control_id,
    check_name,
    severity,
    -- Sort key, high first. Sorting the labels alphabetically would give high, low, medium.
    -- 9 for any other value.
    CASE severity
        WHEN 'high'   THEN 1
        WHEN 'medium' THEN 2
        WHEN 'low'    THEN 3
        ELSE 9
    END AS severity_rank,
    -- CTRL-001 (NOT NULL), CTRL-002 (primary key) and CTRL-004 (foreign key) are enforced by the
    -- schema and cannot fail on inserted rows (ADR-0004). Any other identifier, including a
    -- control added later, falls into Business rule by default.
    CASE control_id
        WHEN 'CTRL-001' THEN 'Structural'
        WHEN 'CTRL-002' THEN 'Structural'
        WHEN 'CTRL-004' THEN 'Structural'
        ELSE 'Business rule'
    END AS control_family
FROM quality_check_results;

COMMENT ON VIEW vw_dim_control IS
    'control_family separates what the schema enforces structurally from what only '
    'the control layer can see. This is the central argument of the project, made '
    'sliceable in the report.';

-- Grain: one transaction. risk_score and is_suspicious are the generator score and label
-- (ADR-0003, ADR-0008). The model score is in gold.predictions and is not exposed here.
CREATE OR REPLACE VIEW vw_fact_transactions AS
SELECT
    t.transaction_id,
    t.account_id,
    t.booking_date        AS date_key,
    t.event_timestamp,
    t.source_system,
    t.channel,
    t.merchant_category,
    t.country             AS transaction_country,
    t.status,
    t.currency,
    t.amount_chf,
    t.risk_score,
    t.is_suspicious,
    -- Rows injected by demo_scenario.py carry this source_system. The flag lets reports leave
    -- them out: Transaction Count Excl Demo in dax-measures.md, NOT is_demo_row in the Gold
    -- queries of monitoring.py.
    (t.source_system = 'demo_scenario') AS is_demo_row
FROM transactions t;

-- Grain: one row per control, holding its latest execution. quality_checks.py upserts with
-- the control identifier as check_id, so no history accumulates here.
CREATE OR REPLACE VIEW vw_fact_quality_results AS
SELECT
    q.check_id,
    q.control_id,
    q.executed_at::date AS date_key,
    q.executed_at,
    q.status,
    q.failed_rows,
    -- 0/1 integers so that they can be summed, as monitoring.load_control_trend does. A status
    -- other than PASS, WARN or FAIL leaves all three flags at 0.
    CASE q.status WHEN 'PASS' THEN 1 ELSE 0 END AS is_pass,
    CASE q.status WHEN 'FAIL' THEN 1 ELSE 0 END AS is_fail,
    CASE q.status WHEN 'WARN' THEN 1 ELSE 0 END AS is_warn
FROM quality_check_results q;

-- Grain: one incident report. quality_checks.py opens one for each FAIL control, never for a
-- WARN, always with status open and the severity of the control. db.reset_tables empties
-- incident_reports at every ingestion, so only the incidents of the current batch show here.
CREATE OR REPLACE VIEW vw_fact_incidents AS
SELECT
    i.incident_id,
    i.failed_control    AS control_id,
    i.created_at::date  AS date_key,
    i.created_at,
    i.severity,
    i.status,
    i.title
FROM incident_reports i;

-- One row. Same rule as monitoring.QualitySummary.overall_status behind GET /verdict in
-- api.py: FAIL if any control failed, else WARN if any warned, else PASS. A WARN does not
-- block (docs/KPI_DEFINITIONS.md), so can_publish is false only when a control failed.
-- The Power BI measure Publication Verdict shows the same verdict as BLOCKED, UNDER
-- RESERVATION or CLEARED.
--
-- It aggregates the whole of quality_check_results, which is right only because that table
-- keeps one row per control. A history kept there would add all the runs together.
--
-- With no result row at all, before the first run, it still returns one row: controls_total
-- 0, verdict PASS, can_publish true. GET /verdict answers 503 in that case (api.py), so read
-- controls_total before trusting the verdict.
CREATE OR REPLACE VIEW vw_publication_verdict AS
SELECT
    COUNT(*)::int                                          AS controls_total,
    COUNT(*) FILTER (WHERE status = 'PASS')::int            AS controls_passed,
    COUNT(*) FILTER (WHERE status = 'WARN')::int            AS controls_warning,
    COUNT(*) FILTER (WHERE status = 'FAIL')::int            AS controls_failed,
    -- Failed rows of FAIL controls only, not of WARN. The sum is NULL when nothing failed and
    -- the report must read 0.
    COALESCE(SUM(failed_rows) FILTER (WHERE status = 'FAIL'), 0)::int AS rows_in_violation,
    CASE
        WHEN COUNT(*) FILTER (WHERE status = 'FAIL') > 0 THEN 'FAIL'
        WHEN COUNT(*) FILTER (WHERE status = 'WARN') > 0 THEN 'WARN'
        ELSE 'PASS'
    END                                                    AS verdict,
    (COUNT(*) FILTER (WHERE status = 'FAIL') = 0)          AS can_publish,
    MAX(executed_at)                                       AS evaluated_at
FROM quality_check_results;
