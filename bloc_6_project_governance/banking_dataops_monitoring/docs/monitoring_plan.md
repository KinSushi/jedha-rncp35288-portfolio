# Monitoring Plan

## Monitored dimensions

| Dimension | Metric | Evidence |
|---|---|---|
| Data freshness | latest event timestamp | quality check CTRL-007 |
| Quality status | PASS/WARN/FAIL counts | quality_check_results |
| Reconciliation | counts and amount deltas | reconciliation_results |
| Risk/anomaly | suspicious transaction count | dashboard |
| Runtime | dashboard/database availability | Streamlit error handling |
| Public safety | synthetic-only review | VALIDATION.md |

## Dashboard sections

- data-quality status;
- latest quality results;
- transaction volume by booking date;
- reconciliation results;
- status distribution;
- channel summary;
- suspicious/high-risk transaction sample;
- operational commands.

## Future observability path

- Prometheus metrics endpoint;
- Grafana dashboard;
- OpenTelemetry traces for ingestion and checks;
- alerting on failed controls;
- scheduled job integration.

## Public-safety note

Monitoring examples use synthetic data only.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
