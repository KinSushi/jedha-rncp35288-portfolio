# jedha-rncp35288-portfolio

<div align="center">

<img src="assets/jedha-portfolio-banner.svg" alt="jedha-rncp35288-portfolio banner" width="100%"/>

<br/>

**Public, sanitized technical evidence portfolio for the Jedha Data Science & AI certification track**

RNCP Level 6 Â· Six-block evidence Â· Data Infrastructure Â· EDA Â· ML Â· NLP Â· MLOps Â· Project Governance

![RNCP Level 6](https://img.shields.io/badge/RNCP-Level%206-2EA043?style=flat)
![Jedha](https://img.shields.io/badge/Jedha-Data%20Science%20%26%20AI-1F6FEB?style=flat)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat&logo=python&logoColor=white)
![SQL](https://img.shields.io/badge/SQL-Data%20Infrastructure-003B57?style=flat&logo=postgresql&logoColor=white)
![MLOps](https://img.shields.io/badge/MLOps-MLflow%20%2F%20Docker-0194E2?style=flat)
![Public Safety](https://img.shields.io/badge/Public%20Docs-Sanitized-24292F?style=flat)

</div>

---

## Executive summary

This repository is a **public sanitized evidence portfolio** for the Jedha Data Science & AI RNCP Level 6 six-block track.

It is designed to organize technical evidence without publishing private school documents, exam materials, grades, certificates with personal identifiers, CPF documents, contracts, invoices or application material.

The portfolio maps technical artifacts to six competency blocks:

```text
data infrastructure -> EDA/statistics -> machine learning -> NLP/deep learning -> MLOps deployment -> project governance
```

---

## Public-safety boundary

This repository must never contain:

- private Jedha course content;
- exam subjects or answers;
- grades;
- private certificates with identifiers;
- administrative PDFs;
- CPF files;
- contracts or invoices;
- CVs or cover letters;
- employer-specific applications;
- real banking, insurance, health, client or employer data;
- secrets, tokens or credentials.

Allowed:

- personal technical code;
- sanitized screenshots;
- synthetic or open datasets;
- architecture diagrams;
- model cards;
- data cards;
- runbooks;
- project governance documents;
- evidence index.

---

## Six-block evidence map

| Block | Public evidence folder | Technical signal |
|---|---|---|
| Bloc 1 â€” Data infrastructure | `bloc_1_data_infrastructure/` | ingestion, SQL, schema, data quality, architecture |
| Bloc 2 â€” EDA / statistics | `bloc_2_eda_statistics/` | analysis, distributions, anomalies, hypothesis testing |
| Bloc 3 â€” Machine learning | `bloc_3_machine_learning/` | supervised/unsupervised ML, evaluation, explainability |
| Bloc 4 â€” NLP / deep learning | `bloc_4_nlp_deep_learning/` | text processing, document classification, GenAI foundations |
| Bloc 5 â€” Industrialization / MLOps | `bloc_5_mlops_deployment/` | API, Docker, MLflow, tests, monitoring |
| Bloc 6 â€” Project governance | `bloc_6_project_governance/` | project charter, risk register, model/data governance |

---

## Relationship with the broader portfolio

This repository should reference, not duplicate, the technical repos:

| External repo | Related block |
|---|---|
| `banking-dataops-monitoring` | Bloc 1, Bloc 6 |
| `fraud-mlops-control-tower` | Bloc 3, Bloc 5, Bloc 6 |
| `database-migration-quality-lab` | Bloc 1, Bloc 6 |
| `secure-wealth-rag-assistant` | Bloc 4, Bloc 6 |
| `sovralys-infra-lab` | Bloc 1, Bloc 5 |
| `pty-flights-pricing` | Bloc 1, operational automation evidence |

---

## Repository structure

```text
jedha-rncp35288-portfolio/
â”œâ”€â”€ README.md
â”œâ”€â”€ PORTFOLIO.md
â”œâ”€â”€ LICENSE
â”œâ”€â”€ .gitignore
â”œâ”€â”€ assets/
â”‚   â””â”€â”€ jedha-portfolio-banner.svg
â”œâ”€â”€ docs/
â”‚   â”œâ”€â”€ certification_mapping.md
â”‚   â”œâ”€â”€ evidence_index.md
â”‚   â”œâ”€â”€ public_safety_rules.md
â”‚   â”œâ”€â”€ portfolio_review_checklist.md
â”‚   â””â”€â”€ templates/
â”œâ”€â”€ bloc_1_data_infrastructure/
â”œâ”€â”€ bloc_2_eda_statistics/
â”œâ”€â”€ bloc_3_machine_learning/
â”œâ”€â”€ bloc_4_nlp_deep_learning/
â”œâ”€â”€ bloc_5_mlops_deployment/
â””â”€â”€ bloc_6_project_governance/
```

---

## How to use this repo

1. Keep private school/admin documents outside GitHub.
2. Add only sanitized technical evidence.
3. Link to dedicated project repos when evidence belongs there.
4. Update `docs/evidence_index.md` after every added artifact.
5. Run public-safety review before every push.

---

## Non-goals

This repository is not:

- a dump of school files;
- a private certificate folder;
- a place for applications;
- a place for grades;
- a copy of Jedha course content;
- a production data/ML system.

---

## Portfolio signal

This repo proves structure, traceability and documentation discipline across the complete Data / MLOps certification track, while keeping sensitive material private.
---

## Portfolio layer

This repository is part of the KinSushi public technical portfolio.

| Layer | Evidence |
|---|---|
| Certification evidence | public sanitized six-block evidence map for RNCP Level 6 track |

Detailed cross-repository context: [docs/PORTFOLIO_LAYER.md](docs/PORTFOLIO_LAYER.md)

