# `sql/` : schéma et requêtes

Numérotés par ordre d'application. Le DDL est rejouable : `CREATE TABLE IF NOT
EXISTS`, `CREATE OR REPLACE VIEW`.

| Fichier | Type | Rôle |
|---|---|---|
| `00_schema.sql` | DDL | tables silver, contraintes, index |
| `01_seed_reference_data.sql` | requête | données de référence |
| `02_data_quality_checks.sql` | requêtes | les 7 contrôles, version SQL |
| `03_reconciliation_queries.sql` | requêtes | rapprochements inter-couches |
| `04_anomaly_queries.sql` | requêtes | exploration des anomalies |
| `05_performance_queries.sql` | requêtes | indicateurs de performance |
| `06_powerbi_star_schema.sql` | DDL | les 8 vues du schéma en étoile |
| `07_audit_journal.sql` | DDL | `audit_events` et ses contraintes |
| `08_gold_layer.sql` | DDL | schéma `gold`, dimensions et faits |

## Ce qui est appliqué automatiquement

- `ingest.py` applique `00_schema.sql` puis `07_audit_journal.sql`
- `powerbi_views.py` applique `06_powerbi_star_schema.sql`
- `pipeline/gold.py` s'appuie sur `08_gold_layer.sql`

Les fichiers 01 à 05 sont des requêtes d'analyse : ils ne créent rien.

## Contrainte notable

`audit_events` refuse en base tout événement sans raison métier :

```sql
CHECK (length(trim(reason)) > 0)
```

La règle est donc imposée par la base elle-même. Un reverse test
vérifie qu'une insertion sans raison est bien rejetée.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
