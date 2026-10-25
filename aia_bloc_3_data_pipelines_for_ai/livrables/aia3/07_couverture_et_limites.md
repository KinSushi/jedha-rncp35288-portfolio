# 07 - Couverture du référentiel, vérifications et limites

> Ce document dit ce qui est prouvé, par quelle commande, et ce qui ne l'est pas encore.

## 1. Les 21 indicateurs, un par un

| Indicateur | Réponse | Preuve |
|---|---|---|
| 1.1 Modes de traitement | alerte = flux, rapport = lot, entraînement et retraitement = lot ; ETL à l'entrée (masquage), ELT ensuite ; exemple chiffré d'une carte touchée en rafale | `01_architecture.md` §3 |
| 1.2 Collecte dimensionnée | volumes de l'API (5/min, 1 440/jour retenus), du jeu (pic 16/min), message 431 octets, débit mesuré ≥ 1 000 tx/s, scénarios de panne sans perte | `01` §4 ; tests de relivraison et d'atomicité |
| 1.3 Schéma des flux | diagramme Mermaid : entrées, sorties, stockages intermédiaires | `01` §2 et §5 |
| 2.1 Modularité | médaillon bronze / argent / or / méta ; une fonction par transformation | `02` §1 ; `entrepot.py`, `consommateur.py` |
| 2.2 Feature engineering | prétraitements différents selon les mathématiques du modèle ; agrégations 24 h / 30 j, lot et flux identiques | `02` §2 ; `fonctionnalites.py`, `test_fonctionnalites.py` |
| 2.3 Retraitement | commande documentée, idempotente, sans renotifier | `02` §3 ; `retraitement.py` |
| 3.1 Orchestrateur | 3 DAG planifiés, actifs dès la création, aucun lancement manuel | `dags/`, `02` §4.1 |
| 3.2 Dépendances | chaîne sans tâche orpheline, vérifiée par test et par Airflow réel | `test_dags.py`, `02` §4.2 |
| 3.3 Reproductibilité | dev / prod, même code, contraintes de versions, deux piles testées, mêmes résultats en mode local et en conteneur | `02` §4.3 ; `config/` |
| 4.1 Validation de schéma | 10 contrôles étagés, 22 corruptions testées | `02` §5 ; `schema.py` |
| 4.2 DLQ | sujet dédié, archivée, sans donnée identifiante | `02` §6 |
| 4.3 Relances | tableau de 6 niveaux ; vérifié sur l'API réelle | `02` §7 ; `producteur.py`, `dags/` |
| 5.1 Privacy by design | masquage dans le code d'ingestion, liste blanche, test sur tous les fichiers produits | `03` §2 ; `masquage.py` |
| 5.2 Secrets | `FRAUDE_<NOM>` ou `_FICHIER`, secrets Docker, aucun secret dans le code ni le YAML (testé) | `03` §6 ; `configuration.py` |
| 5.3 Chiffrement | tableau des flux : HTTPS, STARTTLS, webhook HTTPS imposés ; Kafka et PostgreSQL non chiffrés en local (assumé, justifié) | `03` §5 |
| 6.1 Observabilité et alerte | 9 seuils, DAG de supervision, SLA Airflow et dans le code | `04` §2 ; `supervision.py` |
| 6.2 FinOps | consommations mesurées × prix hypothétiques ; le coût est du calcul permanent, pas des données | `04` §4 ; `finops.py` |
| 6.3 Journaux | JSON structurés, catalogue des événements, recettes de diagnostic | `04` §3 ; `journal.py` |
| 7.1 Lignage | par colonne, par exécution (`meta_lignage`), par ligne | `02` §8 |
| 7.2 Backlog | 3 sprints, récits, critères d'acceptation, MoSCoW, backlog restant | `05` |
| 7.3 Documentation | README, documents de conception, procédures d'incident | `fraude/README.md`, `livrables/aia3/` |

## 2. Ce qui a été vérifié, et comment

| Vérification | Commande | Résultat |
|---|---|---|
| Qualité du code | `cd fraude && ruff check . && ruff format --check .` | « All checks passed » ; 46 fichiers formatés (99 colonnes) |
| Tests, pile récente (pandas 3.0.6, numpy 2.5, SQLAlchemy 2.1.4, Python 3.12.14) | `pytest` | **283 réussis, 1 ignoré** (le test qui exige Airflow installé) |
| Tests, pile de l'image Airflow (pandas 2.1.4, numpy 1.26.4, SQLAlchemy 1.4.54) | `pytest` | **282 réussis, 2 ignorés** (Airflow ; `dill` non installé) |
| Mode local, rejeu du 21/12/2020 | `python -m detection_fraude.local demo --reinitialiser` | 6 390 lignes lues, **6 en DLQ**, 6 384 traitées, **10 alertes (7 vraies)** envoyées, 1 rapport ; traitement 4,4 s (9,7 s la commande entière) ; supervision sans anomalie |
| Mode local, API réelle | `… demo --source api --appels 3 --intervalle 13 --pannes 2` | 2 relances (1,07 s, 2,19 s) puis succès, 3 transactions réelles traitées |
| Retraitement, rapport, supervision | `retraitement --depuis 2020-12-21 --jusqu 2020-12-21 --rapports`, `rapport`, `supervision` | 0 décision modifiée (idempotent), rapport régénéré, aucun constat |
| Production locale depuis zéro | `python scripts/initialiser_secrets.py && docker compose up -d --build` | 9 services, tous « healthy » ou terminés à 0, en **1 min 36 s** (images déjà en cache ; jeu téléchargé, premier modèle entraîné en 24 à 29 s) |
| Même résultat dans les deux modes | rejeu du 21/12/2020 sur Kafka + PostgreSQL | **mêmes 10 identifiants de transaction** en alerte qu'en mode local ; 6 rejets archivés |
| Airflow réel (2.10.5) | `airflow dags list`, `list-import-errors` | 3 DAG actifs, aucune erreur d'import |
| DAG du rapport | exécution planifiée de lui-même (relance après source muette) puis déclenchement | 4 tâches en chaîne réussies ; rapport HTML + CSV produits (3 827 paiements, 3 rejets, 2 fraudes détectées) |
| DAG de réentraînement | `airflow dags test fraude_reentrainement_hebdo …` | 5 tâches réussies, entraînement 34 à 42 s, barrière franchie, modèle promu |
| Rechargement à chaud | consommateur après promotion par Airflow | le lot suivant est noté avec la nouvelle version, sans redémarrage |
| Relances en conteneur | `producteur --source api --nombre 1 --pannes 2` | 2 relances puis succès sur l'API réelle |
| Mermaid | rendu dans un navigateur (mermaid 10.9.1) | les 2 diagrammes se lisent sans erreur |
| Confidentialité | `test_aucune_donnee_personnelle_dans_aucun_fichier_produit` | aucun nom, rue, ville, métier, date de naissance ni numéro de carte dans aucun fichier produit |
| Mentions | recherche sur les fichiers suivis (72 avec ce rapport) | la ligne de copyright figure dans tous les fichiers ; aucun secret ; fins de ligne LF |
| **Second environnement** (10/10/2026, Python 3.12.14) | `ruff check`, `ruff format --check`, `pytest`, `python -m detection_fraude.local demo --reinitialiser` | ruff propre ; **282 réussis, 2 ignorés** ; mode local : 6 390 lignes lues, 6 en DLQ, 6 384 traitées, **10 alertes**, supervision sans anomalie |
| **Porte de qualité** (21/10/2026, Python 3.12.14, pandas 3.0.6, numpy 2.5.4, SQLAlchemy 2.0.54) | `ruff check`, `mypy --strict`, `pytest -W error::DeprecationWarning`, mutation (`mutmut`) | ruff et mypy sans erreur (25 fichiers) ; **1 621 réussis, 1 ignoré** (Airflow) ; mutation sur tout le code : 9 063 mutants tués sur 9 148, les 85 autres équivalents, nommés avec leur preuve dans `fraude/MUTANTS_EQUIVALENTS.md` |

## 3. Hypothèses et points à confirmer

* Données **synthétiques** : le RGPD et PCI DSS sont appliqués par transposition ; les numéros
  d'exigence PCI DSS v4.0.1 sont à faire confirmer par un RSSI ; l'AIPD et le registre des traitements
  restent à rédiger avec un DPO.
* « La veille » est un **jour UTC** ; la devise des montants n'est pas fournie par la source.
* Les **prix FinOps** (0,03 € le vCPU-heure, 0,10 € le Go-mois, 0,08 € le Go sortant) sont des
  hypothèses d'ordre de grandeur, signalées comme telles dans l'outil et le document ; les tailles
  de lignes en base sont des estimations par pages SQLite (PostgreSQL diffère).
* Durées de conservation (bronze 90 jours, argent et or 13 mois) : hypothèses à valider.
* Plancher de PR-AUC (0,30) et tolérance de régression (0,02) : choix de conception, pas des mesures.
* Sprints d'une semaine, une personne, tailles relatives : hypothèses de présentation.
* Le taux de fraude du jeu s'effondre après le 22/12/2020 : la précision cible de 0,90 (atteinte en
  validation) retombe à 0,707 sur la période de test au même seuil ; documenté, non corrigé.

## 4. Ce qui reste, et les limites

* TLS entre services (Kafka, PostgreSQL, MLflow) non activé dans la production locale ; une seule
  instance de consommateur (boîte d'envoi sans verrou) ; serveur MLflow gourmand (1,5 Go résidents) ;
  pas d'intégration continue ; Airflow 3 remplace les SLA par des « Deadline Alerts » (migration au
  backlog).
* Le test qui exige Airflow est ignoré sur les postes sans Airflow (Windows) ; la même vérification
  a été faite dans l'image officielle avec `airflow dags list` et `list-import-errors`.
* Le modèle est volontairement simple (énoncé : « le pipeline, pas l'algorithme »).

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
