# Note de preuve — AIA bloc 3 — Pipelines de données pour l'IA

## Statut

Publié : le pipeline de détection de fraude en temps réel, présenté dans le [README du bloc](../README.md).

## Ce que le bloc démontre

Construction d'un pipeline de données automatisé, observable et conforme, dont le pipeline lui-même est le livrable principal : collecte et stockage, file de messages, notation, notification, entrepôt en couches, contrôles de qualité, orchestration, réentraînement et supervision.

## Preuves

| Preuve | Chemin |
|---|---|
| Documents de conception | [`livrables/aia3/`](../livrables/aia3/) |
| Code du pipeline | [`fraude/detection_fraude/`](../fraude/detection_fraude/) |
| Orchestration (DAG Airflow) | [`fraude/dags/`](../fraude/dags/) |
| Tests | [`fraude/tests/`](../fraude/tests/) |
| Obtention des données | [`README.md`](../README.md), section « Données », et [`fraude/data/README.md`](../fraude/data/README.md) |

Le script de la vidéo de démonstration n'est pas publié dans cette version.
