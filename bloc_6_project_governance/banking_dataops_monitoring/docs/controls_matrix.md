# Controls Matrix

| Control ID | Control name | Risk covered | Evidence | Severity | Frequency | Owner | Status |
|---|---|---|---|---|---|---|---|
| CTRL-001 | Critical fields not null | Missing key operational data | SQL + Python check | high | Every run | DataOps pipeline | Active |
| CTRL-002 | Transaction ID uniqueness | Duplicate transaction records | SQL + Python check | high | Every run | DataOps pipeline | Active |
| CTRL-003 | Amount validity | Implausible amounts | SQL + Python check | high | Every run | DataOps pipeline | Active |
| CTRL-004 | Referential integrity | Orphan transactions | SQL + Python check | high | Every run | DataOps pipeline | Active |
| CTRL-005 | Risk-score range | Invalid risk values | SQL + Python check | medium | Every run | DataOps pipeline | Active |
| CTRL-006 | Status validity | Unexpected lifecycle states | SQL + Python check | medium | Every run | DataOps pipeline | Active |
| CTRL-007 | Freshness | Stale dataset | SQL + Python check | low | Daily | DataOps pipeline | Active |
| CTRL-008 | Source-system reconciliation | Missing or imbalanced feed | `reconciliation.py` | medium | Daily | DataOps pipeline | Active |
| CTRL-009 | Suspicious transaction monitoring | Risk concentration | `monitoring.py` | medium | Daily | DataOps pipeline | Active |
| CTRL-010 | Public-safety review | Private data leakage | manual review | high | Every release | Repository owner | Active |

## Public-safety rule

All datasets must be synthetic or open. Do not publish real banking, insurance, health, client, employer or private data.
