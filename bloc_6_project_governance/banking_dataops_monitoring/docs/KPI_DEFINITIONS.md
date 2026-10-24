# KPI definitions

Every indicator the project computes: its definition, its source, its threshold, and the decision it serves.

**Rule applied throughout:** an indicator that serves no decision is not a KPI, it is a statistic. Each entry below names the decision it supports. Anything that could not name one was left out.

---

## 1. Publication KPIs — the decision layer

Displayed at the top of the dashboard. Read in under three seconds by someone who does not write SQL.

| KPI | Definition | Source | Target | Decision served |
|---|---|---|---|---|
| **Controls passed** | Count of controls with status `PASS`, over the total executed | `quality_check_results` | 7 / 7 | Publish or block |
| **Rows in violation** | Sum of `failed_rows` over controls with status `FAIL` | `quality_check_results` | 0 | Sizes the remediation effort |
| **Transactions under control** | Rows evaluated in the batch | `gold.fact_transactions` | informative | Scope of the verdict |
| **Last control run** | `MAX(executed_at)` | `quality_check_results` | under 24 h | Is the verdict still current |

### Publication verdict

The single output of the whole pipeline.

| Verdict | Condition | Action |
|---|---|---|
| `PASS` | No control failed | Publication authorised |
| `WARN` | No failure, at least one warning | Publication under reservation, review required |
| `FAIL` | At least one control failed | Publication blocked, incident procedure |

**Why `WARN` does not block.** A warning is a reservation, not a refusal. Treating it as a block creates a structural false positive, and a system that cries wolf is unplugged within weeks. The distinction propagates all the way to the API: a `WARN` returns `can_publish: true`.

---

## 2. Data quality KPIs — the control layer

| KPI | Definition | Threshold | Severity |
|---|---|---|---|
| **CTRL-001 · Critical nulls** | Rows with a null in a mandatory field | 0 | high |
| **CTRL-002 · Duplicate transactions** | Transaction identifiers appearing more than once | 0 | high |
| **CTRL-003 · Invalid amounts** | `amount_chf <= 0` or `> 1 000 000` | 0 | high |
| **CTRL-004 · Orphan transactions** | Transactions whose account does not exist | 0 | high |
| **CTRL-005 · Invalid risk score** | `risk_score` outside `[0, 1]` | 0 | medium |
| **CTRL-006 · Invalid status** | Status outside `posted / pending / rejected` | 0 | medium |
| **CTRL-007 · Dataset freshness** | Most recent `event_timestamp` older than 60 days | warning only | low |

**Structural versus business.** CTRL-001, CTRL-002 and CTRL-004 cannot fail on inserted rows: the schema prevents them through `NOT NULL`, a primary key and a foreign key. They are kept in the matrix because a control that never fires still has to be *shown* not to fire — and because a schema change would make them meaningful again overnight.

The four others carry business rules no schema can express without freezing a threshold into it. `dim_control.control_family` makes this split sliceable in Power BI.

### Where the thresholds come from

| Threshold | Value | Origin |
|---|---|---|
| Amount ceiling | CHF 1 000 000 | Plausibility bound for retail banking. **To recalibrate on real data** |
| Risk score range | `[0, 1]` | Contract of the scoring engine, not a business choice |
| Freshness | 60 days | Two monthly closing cycles |
| High amount flag | CHF 10 000 | Common AML reporting threshold |
| Night window | 00:00 – 06:00 | Retail traffic collapses, automated abuse does not |

Every one of these is a parameter, not a constant. Recalibrating them with the business teams is the bulk of a real implementation.

---

## 3. Cleaning KPIs — Bronze to Silver

| KPI | Definition | Threshold | Decision served |
|---|---|---|---|
| **Quarantine rate** | Rows removed / rows in | **< 5 %**, pipeline stops above | Are the cleaning rules calibrated |
| **Duplicates removed** | Duplicate identifiers, first kept | informative | Upstream emission problem |
| **Impossible dates** | Booking date outside `[2000, 2100]` | informative | Parsing accident, not a business event |
| **Whitespace trimmed** | Text cells with leading or trailing spaces | informative | `"CH "` and `"CH"` split an aggregate in two |

**Why the quarantine rate has a hard stop.** Above 5 %, the rules are wrong, not the data. Continuing would silently shrink the batch a control is meant to judge.

---

## 4. Volume and reconciliation KPIs

| KPI | Definition | Source | Decision served |
|---|---|---|---|
| **Transaction count** | Rows in the batch | `gold.fact_transactions` | Detect a partial load |
| **Total amount CHF** | `SUM(amount_chf)` | idem | Order of magnitude check |
| **Volume change vs previous day** | `(today − yesterday) / yesterday` | `dim_date` | A sudden drop signals a broken feed |
| **Rolling 7-day average** | Mean count over the trailing week | idem | Smooths weekend seasonality |
| **Count by source system** | Rows grouped by `source_system` | idem | Which feed is missing |

**Known limitation.** Reconciliation compares internal aggregates, not two independent systems. It detects a coherence break, not a divergence between a source and a target. Documented in the project dossier as the main limitation, and first item on the roadmap.

---

## 5. Risk and model KPIs

| KPI | Definition | Source | Decision served |
|---|---|---|---|
| **Suspicious rate** | Labelled suspicious / total | `gold.fact_transactions` | Baseline for the investigation load |
| **Average risk score** | `AVG(risk_score)` | idem | Drift of the scoring engine |
| **High-risk exposure** | `SUM(amount_chf)` where `risk_score >= 0.7` | idem | Amount at stake, not just a count |
| **Queue by priority** | Transactions per `priority_band` | `gold.predictions` | Where an analyst starts |
| **ROC AUC** | Ranking quality of the classifier | `models/metrics.json` | **Publication floor: 0.70** |
| **Precision / Recall** | At the operating threshold | idem | Analyst time versus missed cases |

### Priority bands

Relative to the decision threshold, so a retune shifts the bands coherently instead of stranding them at absolute values (ADR-0009).

| Band | Condition | Meaning |
|---|---|---|
| `high` | score ≥ threshold | Predicted suspicious (this is `predicted_suspicious`): investigate first |
| `medium` | threshold × 0.60 ≤ score < threshold | Grey zone: investigate once the `high` items are done |
| `low` | score < threshold × 0.60 | No action |

**Measured behaviour (dataset built on the 27 July 2026 anchor, `evidence/model_metrics.json`, `evidence/bands_gold_predictions.txt`).** The decision threshold is 0.852 (F1-optimal on out-of-fold predictions of the training rows), so `medium` starts at 0.511. Of 1 500 scored transactions, 126 are `high`, 99 are `medium` and 1 275 are `low`. On the 375 test rows, reading `high` alone gives precision 0.656 and recall 0.700; reading `high` and `medium` together gives precision 0.435 and recall 0.900.

The previous rule put `high` at threshold × 1.30. With a threshold above about 0.77 that boundary exceeds 1, so the band was unreachable (0 `high` at the 27 July anchor). The model is selected, and the threshold chosen, on the training rows only; the test rows are scored once (ADR-0009).

**The model never blocks.** Its output orders an investigation queue. Blocking is the deterministic control layer's job, because a decision opposable to an auditor must name the rule it applied. See `docs/adr/0002` and `docs/adr/0008`.

---

## 6. Project steering KPIs

Not about the data — about the conduct of the project. Required by C6.4. The values are those of the plan of 20 July 2026
(`docs/project/03-retroplanning.md`, section 3: "hours consumed at 20 July 2026"; the requirements specification
is dated the same day). No later measurement was kept.

| KPI | Measure | Value at 20 July 2026 |
|---|---|---|
| Milestones reached | Reached / total | 2 / 8 |
| Effort consumed | Hours spent / estimated | 47 / 81 |
| Requirement coverage | Functional requirements delivered | 10 / 10 |
| High-criticality risks treated | Risks rated 12 with a treatment in place | 3 rated 12 (R-01, R-04, R-07): 2 treated, 1 partial (R-07). R-12 is rated 8 |
| Documentation debt | Known limitations left undocumented | 0 declared, not re-measured |
| CI health | Lint and tests green | Green at the time; the suite is now <!--tests-en-->547 passed, 3 skipped, 1 xfail (551 cases)<!--/tests--> (`evidence/01_pytest_summary.txt`, 20 October 2026). The CI has no PostgreSQL |

---

## 7. What is deliberately not measured

Stating this is part of the definition. A KPI list that pretends to be exhaustive is not credible.

| Missing KPI | Why it would matter | Why it is absent |
|---|---|---|
| **False positive rate** | A control that cries wolf gets unplugged | Requires an operating history and confirmed investigations |
| **Mean time to resolution** | Measures whether the system actually helps | Requires real incidents |
| **Control coverage by risk domain** | Reveals blind spots | Requires a risk taxonomy agreed with the business |
| **Pipeline duration** | Anticipates volume growth | No load testing performed |
| **Model drift** | A model silently degrades | Requires several months of scored batches |

The first is the most valuable to add, and the first item on the measurement roadmap.

---

## 8. Where each KPI is computed

| Layer | Location |
|---|---|
| SQL controls | `sql/02_data_quality_checks.sql`, `src/banking_dataops/quality_checks.py` |
| Cleaning counters | `src/banking_dataops/pipeline/cleaning.py` |
| Gold aggregates | `sql/08_gold_layer.sql`, `src/banking_dataops/pipeline/gold.py` |
| Model metrics | `src/banking_dataops/ml/train.py`, `models/metrics.json` |
| Dashboard display | `dashboard/streamlit_app.py` |
| Power BI measures | `docs/powerbi/dax-measures.md` |
| API exposure | `src/banking_dataops/api.py` |

**One definition, one place.** The derived columns — `booking_hour`, `is_night`, `is_cross_border`, `is_high_amount` — are computed once, in Gold. The KPI on screen and the feature fed to the model are therefore the same object. Computing them twice is how a dashboard and a classifier silently diverge.
