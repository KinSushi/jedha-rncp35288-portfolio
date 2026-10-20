# Incident Runbook — banking-dataops-monitoring

## Detection

An incident can be detected through:

- failed SQL quality check;
- failed reconciliation query;
- dashboard status change;
- failed scheduled run;
- failed CI test;
- manual review.

## Triage steps

1. Identify failed control ID.
2. Review failed row count.
3. Check latest ingestion timestamp.
4. Compare source-system reconciliation summary.
5. Isolate affected booking date, source system or channel.
6. Generate incident note with `make incident`.
7. Decide: regenerate data, rerun ingestion, adjust validation or document known issue.

## Investigation SQL

```sql
SELECT * FROM quality_check_results ORDER BY executed_at DESC;
SELECT * FROM reconciliation_results ORDER BY executed_at DESC LIMIT 20;
SELECT * FROM transactions WHERE is_suspicious ORDER BY risk_score DESC LIMIT 20;
```

## Root-cause categories

| Category | Examples |
|---|---|
| Data ingestion | missing file, malformed rows, source delay |
| Data quality | nulls, duplicates, invalid amounts |
| Reconciliation | missing source system, unexpected totals |
| Runtime | container down, database unavailable |
| Documentation | missing data dictionary or control description |

## Resolution template

```markdown
# Incident: <title>

## Impact
What failed and which dataset/control was affected?

## Symptoms
Failed query, row count, dashboard status or log excerpt.

## Root cause
Technical explanation.

## Resolution
Steps taken.

## Preventive action
Additional control, test or documentation update.
```

## Public-safety note

Do not paste real client, bank, insurance, health, account or employer data into incident notes.
