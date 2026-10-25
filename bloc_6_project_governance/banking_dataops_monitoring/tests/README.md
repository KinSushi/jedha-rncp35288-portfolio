# `tests/` : 551 cas

```bash
pytest              # 547 passés, 3 ignorés, 1 échec attendu, 0 échec
```

La suite tourne entièrement hors ligne : aucun test n'exige de base de
données. Les 3 ignorés sont des conditionnels documentés (`E''` accepté dans trois fichiers de
`sql/`, `test_sql_splitter.py`). L'échec attendu est un `xfail` strict qui consigne un écart connu :
l'entrypoint du conteneur s'arrête si le pipeline échoue au lieu de démarrer le tableau de bord
(`test_reverse_paths.py`) ; corriger l'entrypoint fait échouer le test, qu'il faut alors durcir.

Dernière exécution complète : 547 passés, 3 ignorés, 1 xfail en 38,93 s, avec
`-W error::DeprecationWarning -W error::PendingDeprecationWarning` (`evidence/pytest_full.txt`).

| Fichier | Cas | Objet |
|---|---:|---|
| `test_sql_splitter_edges.py` | 75 | The SQL statement splitter (``banking_dataops.db.split_sql_statements``) on small hostile texts |
| `test_audit_journal.py` | 45 | The audit layer (``banking_dataops.audit``): fingerprints, events, the run frame, the invariants, the queries |
| `test_ml_training_contract.py` | 44 | contrat de l'entraînement : chiffres rapportés, journal et publication (tests de mutation) |
| `test_hostile_inputs.py` | 41 | Inputs the system must refuse or must not let through: improbable scores, forbidden API operations, hostile values |
| `test_api_refusals.py` | 36 | The decision API says no: writes, unknown routes, hostile parameters, cross-origin calls, database outages |
| `test_smoke_check.py` | 26 | The end-to-end acceptance script (``scripts/smoke_check.py``): fifteen checks, one exit code |
| `test_publish_to_cloud.py` | 21 | copie d'un lot vers un PostgreSQL managé (`scripts/publish_to_cloud.py`) |
| `test_run_pipeline.py` | 21 | The pipeline entry point (``run_pipeline.py``): order of the steps, exit codes, what a failed step stops |
| `test_doc_vivante.py` | 18 | contrôleur de documentation en cinq sections (CHOIX, OÙ, COMMENT, POURQUOI, JOURNALISATION) |
| `test_ingest.py` | 18 | Bronze to Silver load (``banking_dataops.ingest``): what is sent to PostgreSQL, what is journalled, what is refused |
| `test_sql_splitter.py` | 18 | découpage des instructions SQL |
| `test_monitoring_loaders.py` | 16 | The dashboard read side (`banking_dataops.monitoring`): exact SQL, limits, traces, verdict counts |
| `test_equivalent_mutants_premises.py` | 15 | The premises on which the mutants listed in ``MUTANTS_EQUIVALENTS.md`` are called equivalent |
| `test_gold_build.py` | 13 | Gold layer build (``banking_dataops.pipeline.gold``): load order, one transaction, invariants, refusals |
| `test_reverse_paths.py` | 13 | chemins interdits : le fichier le plus important |
| `test_cleaning.py` | 12 | nettoyage, quarantaine, invariants |
| `test_cleaning_run.py` | 11 | The cleaning step as a whole (``clean_bronze_to_silver``): files read and written, invariants, journal |
| `test_generate_contract.py` | 10 | The Bronze generator (``generate_synthetic_data``): files, identifiers, windows, the risk rule, refusals |
| `test_failures_commit_nothing.py` | 9 | When the database refuses a statement, the step stops, journals a failed run, and commits nothing it should not |
| `test_powerbi_views.py` | 9 | Star-schema views (``banking_dataops.powerbi_views``): creation, the check that all eight exist, the refusal |
| `test_bronze_to_features.py` | 8 | intégration bronze → variables (`tests/integration/`) |
| `test_logging_config.py` | 8 | The logging layer (``banking_dataops.logging_config``): run correlation, the two formats, the safe handler |
| `test_demo_scenario_run.py` | 7 | Demo scenario (``banking_dataops.demo_scenario``): injection, reset, and what both refuse to do |
| `test_api.py` | 6 | endpoints, indisponibilité, ADR-0002 |
| `test_db.py` | 5 | Database plumbing (``banking_dataops.db``): connections, SQL files in one transaction, reads, the reset |
| `test_analytics_queries.py` | 4 | requêtes d'analyse |
| `test_quality_checks_run.py` | 4 | The seven controls as they run (``banking_dataops.quality_checks``): the SQL sent, the verdict, the incidents |
| `test_anchor.py` | 3 | ancre de date des lots, option et variable (ADR-0009) |
| `test_api_contract.py` | 3 | The decision API (``banking_dataops.api``): the exact bodies, the SQL of the two direct reads, the request trace |
| `test_features_contract.py` | 3 | The feature layer (``banking_dataops.ml.features``): the extract, every derivation at its boundary, the leak guard |
| `test_ml_predict.py` | 3 | notation, bandes et refus, sur une base simulée |
| `test_reconciliation_run.py` | 3 | Reconciliation (``banking_dataops.reconciliation``): five aggregates, one coherence row, empty batch refused |
| `test_config.py` | 2 | configuration |
| `test_config_contract.py` | 2 | Settings (``banking_dataops.config``): defaults, the environment, the ``.env`` file, the connection string |
| `test_incident_report.py` | 2 | Incident report (``banking_dataops.incident_report``): when a report is written, what it says, when it is not |
| `test_ml.py` | 2 | variables, fuite de cible, bandes de priorité (ADR-0009) |
| `test_ml_train_arguments.py` | 2 | Arguments and edges of the training helpers (``banking_dataops.ml.train``) that the report tests cannot see |
| `test_ml_training.py` | 2 | sélection du modèle et seuil hors du jeu de test (ADR-0009) |
| `test_reproducibility.py` | 2 | déterminisme et empreintes |
| `test_demo_scenario.py` | 1 | injection et reset |
| `test_entrypoint_anchor.py` | 1 | ancre de date dans le point d'entrée du conteneur |
| `test_monitoring.py` | 1 | indicateurs |
| `test_path_aliases.py` | 1 | The path aliases (``src/ingestion``, ``src/cleaning``, ``src/transformations``, ``src/ml``) |
| `test_quality_checks.py` | 1 | contrôles de qualité |
| `test_reconciliation.py` | 1 | rapprochement |
| `test_sql_files_exist.py` | 1 | présence des fichiers SQL |
| `test_sql_string_hygiene.py` | 1 | interdit les commentaires Python dans les chaînes SQL |
| `test_synthetic_data.py` | 1 | génération |

## Les reverse tests

Un test classique vérifie que le chemin nominal fonctionne. Un reverse test
vérifie que le chemin interdit échoue proprement : sans plantage silencieux,
sans fuite de ressource, sans corruption d'état.

Quelques exemples réels :

```
un événement d'audit sans raison métier est refusé
un événement d'audit avec une catégorie inconnue est refusé
une action CLI inconnue refuse au lieu de prendre une valeur par défaut
la journalisation ne lève jamais, même si la sortie est cassée
l'API signale son indisponibilité au lieu de planter
le health check reste debout quand la base est tombée
un avertissement ne bloque jamais la publication
le verdict ne dérive que des résultats de contrôles persistés
le reset ne touche que les lignes marquées demo_scenario
aucun module ne modifie l'état sans laisser de trace
```

C'est la partie de la suite qui répond à la question d'un auditeur : « et quand ça
se passe mal ? »

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
