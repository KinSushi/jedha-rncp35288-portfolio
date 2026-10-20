# 05 — Backlog produit et trois sprints

*Bloc 3 AIA — Détection automatique de fraude. Ce document répond à l'indicateur 7.2
(« Le travail de développement est-il structuré en itérations (sprints) avec une priorisation
claire des fonctionnalités ? »).*

## 1. Cadre

* **Objectif produit** : notifier l'équipe antifraude dès qu'une fraude est détectée et lui livrer
  chaque matin le rapport de la veille, avec un pipeline observable, sûr et rejouable.
* **Équipe et rythme (hypothèses)** : une personne, trois sprints d'une semaine. Les tailles sont
  **relatives** (S, M, L) : aucune durée n'a été mesurée.
* **Priorisation** : méthode MoSCoW (M = indispensable, S = important, C = souhaitable, W = pas
  maintenant) croisée avec la valeur métier et le risque. Le risque le plus élevé est traité
  d'abord : la **confidentialité** (masquage) et la **perte de transaction**, avant le confort.
* **Définition de « terminé »** : code commenté et conforme à `ruff` ; tests pytest verts ;
  comportement vérifié par une commande documentée ; document de conception à jour ; aucun secret
  dans le dépôt.

## 2. Sprint 1 — Le flux sûr de bout en bout (le chemin chaud)

*But : une transaction entre, est masquée, notée, stockée et notifiée ; une ligne corrompue ne
bloque rien.*

| Id | Récit | Priorité | Taille | Critères d'acceptation | Indicateurs | État |
|---|---|---|---|---|---|---|
| S1-1 | En tant qu'exploitant, je veux que le producteur interroge l'API chaque minute avec des relances, afin de supporter les pannes éphémères | M | M | relances à attente exponentielle, `Retry-After`, 4xx non relancé, journal de chaque tentative | 4.3 | Fait |
| S1-2 | En tant que DPO, je veux que les champs sensibles soient masqués dès l'ingestion, afin qu'aucune donnée identifiante ne soit écrite | M | M | liste blanche ; HMAC de la carte ; aucun fichier produit ne contient les données d'entrée (test) | 5.1 | Fait |
| S1-3 | En tant qu'ingénieur données, je veux valider le schéma et isoler les lignes invalides en DLQ, afin de ne pas bloquer le flux | M | M | 22 corruptions détectées ; toutes les erreurs remontées ; DLQ sans donnée identifiante | 4.1, 4.2 | Fait |
| S1-4 | En tant qu'analyste, je veux un consommateur qui note, stocke en bronze, argent, or et alerte, afin d'être prévenu | M | L | une transaction de base de données par lot ; position de la file validée après le commit ; notification par journal et fichier | 2.1, 1.1 | Fait |
| S1-5 | En tant qu'exploitant, je veux une idempotence totale, afin qu'une relivraison ne duplique ni ne renotifie | M | M | clé `trans_num` ; test de relivraison ; atomicité testée | 4.3 | Fait |
| S1-6 | En tant que data scientist, je veux entraîner un modèle reproductible suivi dans MLflow, afin de noter | S | M | split chronologique, PR-AUC, rappel à précision fixée, empreinte des données, export versionné vérifié | 2.2, 3.3 | Fait |
| S1-7 | En tant qu'exploitant, je veux un mode local sans Docker, afin de démontrer le flux | S | S | `python -m detection_fraude.local demo` rejoue un jour en quelques secondes | 3.3 | Fait |

**Revue de fin de sprint** : le rejeu du 21/12/2020 (6 390 lignes dont 6 corrompues) traverse le
pipeline en 4,4 s de traitement (9,7 s pour la commande entière, modèle chargé et rapport produit)
en mode local, produit 10 alertes dont 7 vraies, et envoie 6 lignes en DLQ.

## 3. Sprint 2 — Industrialisation : orchestration, environnements, conteneurs

*But : plus aucun lancement manuel ; le même code tourne en développement et en production locale.*

| Id | Récit | Priorité | Taille | Critères d'acceptation | Indicateurs | État |
|---|---|---|---|---|---|---|
| S2-1 | En tant que métier, je veux un rapport du matin à 07:00, afin de voir la veille | M | M | HTML + CSV + couche or ; idempotent ; source muette refusée | 1.1, 3.1 | Fait |
| S2-2 | En tant qu'exploitant, je veux des DAG avec dépendances, relances et SLA, afin d'être alerté | M | M | graphe en chaîne ; `retries`, `retry_delay`, `sla`, callbacks ; analysés par le vrai Airflow | 3.1, 3.2, 4.3, 6.1 | Fait |
| S2-3 | En tant que data scientist, je veux un réentraînement hebdomadaire avec barrière de qualité, afin de ne jamais dégrader la production | S | M | refus sous le plancher ou en régression ; bascule atomique ; rechargement à chaud | 2.2 | Fait |
| S2-4 | En tant qu'exploitant, je veux une configuration par environnement, afin de ré-exécuter à l'identique | M | S | `dev`/`prod` ; prod refuse l'absence de secret ; tests | 3.3, 5.2 | Fait |
| S2-5 | En tant qu'exploitant, je veux un `docker-compose` de production locale (Redpanda, PostgreSQL, MLflow, Airflow, producteur, consommateur), afin d'avoir l'infrastructure complète | M | L | démarrage ordonné, contrôles de santé, secrets montés, ports sur `127.0.0.1`, ressources bornées | 1.2, 5.2 | Fait |
| S2-6 | En tant qu'auditeur, je veux des journaux d'audit structurés, afin de diagnostiquer | S | S | JSON, un événement par ligne, nettoyage des champs sensibles | 6.3 | Fait |

**Revue de fin de sprint** : la production locale démarre de zéro (secrets, images, base, modèle,
planification) avec deux commandes ; le même rejeu donne les mêmes 10 alertes que le mode local.

## 4. Sprint 3 — Exploitation, conformité, preuves

*But : prouver et documenter ce qui précède ; couvrir les angles morts.*

| Id | Récit | Priorité | Taille | Critères d'acceptation | Indicateurs | État |
|---|---|---|---|---|---|---|
| S3-1 | En tant qu'exploitant, je veux une supervision (fraîcheur, alertes en attente, rejets, modèle) et un DAG toutes les 10 minutes, afin de détecter une panne silencieuse | M | M | constats classés par gravité ; notification ; échec visible dans Airflow | 6.1 | Fait |
| S3-2 | En tant qu'exploitant, je veux retraiter l'historique depuis le bronze, afin d'appliquer une règle modifiée | M | M | idempotent ; sans renotifier ; décisions modifiées comptées | 2.3 | Fait |
| S3-3 | En tant qu'auditeur, je veux le lignage (par colonne, par exécution, par ligne), afin de remonter à l'origine | S | S | table `meta_lignage` ; documentation de lignage | 7.1 | Fait |
| S3-4 | En tant que directeur, je veux une estimation FinOps, afin de connaître le coût | S | S | consommations mesurées × prix unitaires hypothétiques, outil `finops` | 6.2 | Fait |
| S3-5 | En tant que DPO, je veux la note de sécurité (RGPD, PCI DSS, TLS, secrets), afin de valider | M | M | `03_securite_conformite.md` | 5.1, 5.2, 5.3 | Fait |
| S3-6 | En tant que repreneur, je veux une documentation complète et le script de la vidéo, afin de reprendre le projet | M | M | README, architecture, exploitation, script de 3 minutes | 7.3 | Fait |
| S3-7 | En tant qu'équipe, je veux vérifier le pipeline sur deux piles de bibliothèques et dans les conteneurs | S | M | tests verts avec pandas 3 et pandas 2.1 ; DAG analysés par Airflow | 3.3 | Fait |

## 5. Backlog restant (hors des trois sprints), par priorité

| Rang | Élément | Priorité | Pourquoi | Taille |
|---:|---|---|---|---|
| 1 | TLS et authentification : Kafka en `SASL_SSL`, PostgreSQL en `sslmode=require`, MLflow et Airflow derrière un mandataire TLS | M | seul écart de sécurité majeur de la production locale (section 5 de la note de sécurité) | M |
| 2 | Outils pour les droits des personnes : recherche et effacement par numéro de carte (recalcul du HMAC) | M | RGPD art. 15, 17 ; procédure décrite mais pas outillée | S |
| 3 | AIPD et registre des traitements rédigés avec le DPO | M | obligation avant toute donnée réelle | M |
| 4 | Verrouillage de la boîte d'envoi (`FOR UPDATE SKIP LOCKED`) pour plusieurs consommateurs | S | monter à trois consommateurs (trois partitions) | M |
| 5 | Surveillance de la dérive des données et du modèle (taux de fraude, distribution des scores, précision réelle quand les étiquettes arrivent) | S | le modèle vieillit | L |
| 6 | Planification de la purge du bronze (la fonction existe : `purger_bronze`) en DAG | S | limitation de conservation automatique | S |
| 7 | Passage à Airflow 3 : les SLA y sont remplacés par des « Deadline Alerts » | S | évolution de la plateforme | M |
| 8 | Types natifs PostgreSQL (`TIMESTAMPTZ`), partitionnement par jour de l'argent | C | volumes plus grands | M |
| 9 | Métriques Prometheus et tableaux de bord (latence de notification, retard de la file, rejets) | C | observabilité continue | M |
| 10 | Intégration continue (ruff, pytest, analyse des DAG) à chaque envoi | S | garde-fou permanent | S |
| 11 | Blocage automatique de la carte après revue humaine, avec les garanties de l'article 22 | W | décision à prendre avec le métier et le DPO | L |
| 12 | Agrégation des alertes par carte (une alerte par rafale plutôt que quatre) | C | fatigue d'alerte | S |

## 6. Suivi des risques par sprint

| Risque | Sprint de traitement | Mesure |
|---|---|---|
| Donnée identifiante écrite quelque part | 1 | test qui parcourt tous les fichiers produits |
| Perte ou doublon de transaction | 1 | idempotence, atomicité, validation de la file après commit |
| Écart entre l'entraînement et le service | 1-2 | une seule définition des caractéristiques, test d'égalité lot/flux |
| Panne silencieuse | 3 | supervision toutes les 10 minutes, SLA, alertes |
| Dérive d'environnement | 2-3 | configuration unique, contraintes de versions, tests sur deux piles |

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
