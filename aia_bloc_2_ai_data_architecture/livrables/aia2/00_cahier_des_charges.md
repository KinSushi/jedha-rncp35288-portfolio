# 00 — Cahier des charges : architecture de données de type Stripe

> Bloc 2 de la certification AIA (RNCP41993) · énoncé « Stripe Business Case »
> Données **synthétiques** uniquement. Les mots « hypothèse », « mesuré » et « non vérifié » ont un sens précis dans tout le dossier (voir § 7).

## 1. Contexte et objectif

Une plateforme de paiement en ligne met en relation des **marchands**, leurs **clients** et des **moyens de paiement**. Elle doit :

- enregistrer chaque transaction sans jamais la perdre ni la dupliquer (intégrité financière) ;
- permettre aux équipes métier d'analyser l'activité sur de gros volumes (chiffre d'affaires, remboursements, litiges) ;
- stocker des données peu structurées (parcours de navigation, avis, caractéristiques en ligne) ;
- alimenter en continu, à partir des transactions, l'analytique et un **modèle de détection de fraude** ;
- respecter le **PCI-DSS v4.0.1**, le **RGPD** et le **CCPA**.

Le livrable est une **architecture de données** (OLTP, OLAP, NoSQL, flux), son code de déploiement (Terraform, Docker Compose), une **démonstration exécutable** et la documentation associée. L'énoncé demande huit livrables ; le tableau du § 6 les relie aux documents de ce dossier.

## 2. Parties prenantes

| Rôle | Attente principale |
|---|---|
| Direction produit | Disponibilité du paiement, mise à disposition rapide des indicateurs |
| Équipes marchands (support, risque) | Voir les litiges et les alertes de fraude presque en temps réel |
| Data / analytique | Un entrepôt fiable, des requêtes rapides, des définitions uniques des indicateurs |
| Data science | Données d'entraînement reproductibles, caractéristiques disponibles en production |
| Développement (Dev) | Contrats d'interface stables, schéma versionné |
| Exploitation (Ops) | Supervision, procédures de panne, mises à jour sans interruption |
| Sécurité / conformité (DPO, RSSI) | Preuves de chiffrement, de cloisonnement, de journalisation et de minimisation |

## 3. Exigences fonctionnelles

| Réf. | Exigence | Où elle est réalisée |
|---|---|---|
| F1 | Enregistrer un paiement de façon atomique, avec une clé d'idempotence unique | `stripe/stripe_demo/modele_oltp.py` (`paiements.cle_idempotence`), document 02 |
| F2 | Garantir l'intégrité référentielle et les domaines de valeurs dans la base | contraintes `FOREIGN KEY`, `CHECK`, `UNIQUE` ; `stripe/tests/test_oltp_contraintes.py` |
| F3 | Charger un entrepôt en étoile (batch) et le tenir à jour au fil de l'eau (CDC) | `stripe/stripe_demo/olap.py`, documents 03 et 05 |
| F4 | Fournir des agrégats pré-calculés (chiffre quotidien, fraude par pays) | `agr_chiffre_quotidien`, `agr_fraude_pays`, document 03 |
| F5 | Stocker des événements, des avis et des caractéristiques en ligne dans une base de documents | quatre collections, document 04 |
| F6 | Noter chaque paiement en temps réel avec un modèle de fraude, tracer la version du modèle | `stripe/stripe_demo/fraude.py`, document 07 |
| F7 | Répondre à des questions métier en SQL et en NoSQL | `stripe/sql/requetes_olap.sql`, `stripe/nosql/requetes_mongo.js`, document 08 |
| F8 | Surveiller la dérive du modèle et déclencher un réentraînement | PSI, document 07 |
| F9 | Exercer le droit à l'effacement sans détruire les écritures comptables | anonymisation du client, document 06 |

## 4. Exigences non fonctionnelles

| Réf. | Exigence | Objectif retenu | Statut de l'objectif |
|---|---|---|---|
| N1 | Disponibilité de l'OLTP | survie à la perte d'une zone de disponibilité | décision d'architecture (document 01) |
| N2 | Reprise après incident (OLTP) | RTO 15 min, RPO quasi nul pour une panne de zone | **hypothèse H5** (document 09) |
| N3 | Fraîcheur de l'entrepôt | quelques minutes après l'écriture OLTP | **hypothèse H9** |
| N4 | Latence de notation d'un paiement | rester une fraction de la latence d'un paiement | mesuré en démonstration : médiane de 4,5 à 9,1 ms selon l'exécution (document 07) ; objectif de production : **hypothèse H8** |
| N5 | Montée en charge | croissance linéaire sans refonte (lecteurs, stockage, partitions, serverless) | document 01 § 6 |
| N6 | Sécurité | chiffrement partout, réseau privé, moindre privilège, secrets gérés | document 06, code Terraform |
| N7 | Conformité | PCI-DSS v4.0.1 (périmètre réduit), RGPD, CCPA | document 06 |
| N8 | Maintenabilité | mise à jour des conteneurs et des moteurs sans interruption majeure | document 10 |
| N9 | Reproductibilité | tout l'environnement se reconstruit depuis le dépôt | Terraform, Docker Compose, DDL générés, tests |
| N10 | Maîtrise des coûts et de l'empreinte | plafonds, mise à l'échelle automatique, rétentions bornées | document 09 |

## 5. Contraintes et périmètre

**Dans le périmètre** : la plateforme de données (base transactionnelle, bus de messages, lac, entrepôt, base de documents, ML), leur sécurité et leur exploitation.

**Hors périmètre** (déclaré, non réalisé) :

- le **coffre de cartes** (environnement de données de titulaires de cartes, CDE) : la plateforme de données n'en reçoit que des **jetons** et les quatre derniers chiffres ; aucun numéro complet, cryptogramme ni date d'expiration n'est stocké (vérifié par `test_aucune_colonne_ne_peut_porter_un_numero_de_carte`) ;
- l'API de paiement et l'interface marchands ;
- l'organisation AWS (comptes multiples, CloudTrail d'organisation, IAM Identity Center) : citée comme prérequis, non codée ici ;
- la fonction de rotation des secrets (fonction Lambda) et l'exécution de Debezium sur MSK Connect : le connecteur est fourni, son déploiement en production reste à faire.

**Contraintes techniques** : Python 3.12, PostgreSQL 16, MongoDB 7, API Kafka, Terraform ≥ 1.6, fournisseur AWS 6.x.

## 6. Traçabilité : énoncé → dossier

| Livrable de l'énoncé | Document | Code associé |
|---|---|---|
| 1. Diagramme d'architecture global | 01 | `stripe/terraform/` (partie physique) |
| 2. ERD OLTP | 02 | `stripe/stripe_demo/modele_oltp.py`, `stripe/sql/oltp_schema_postgresql.sql` |
| 3. Schéma OLAP | 03 | `stripe/sql/olap_schema.sql`, `stripe/stripe_demo/olap.py` |
| 4. Modèle NoSQL | 04 | `stripe/nosql/schemas/*.json`, `stripe/nosql/creer_collections.js` |
| 5. Architecture du pipeline | 05 | `stripe/docker-compose.yml`, `stripe/deploiement/debezium_connecteur.json` |
| 6. Plan de sécurité et conformité | 06 | modules Terraform `kms`, `iam`, `secrets`, `reseau` ; `livrables/aia2/checkov.txt` |
| 7. Intégration du ML | 07 | `stripe/stripe_demo/fraude.py` |
| 8. Requêtes SQL et NoSQL | 08 | `stripe/sql/requetes_olap.sql`, `stripe/nosql/requetes_mongo.js` |
| Plan du ou des pipelines (plateforme) | 05 | — |
| Code de déploiement | `stripe/` | `terraform/`, `docker-compose.yml`, `Dockerfile` |
| Capture vidéo du pipeline | 12 (script, non publié dans cette version) | — |

La couverture des 27 indicateurs du référentiel est détaillée dans `13_couverture_et_limites.md`.

## 7. Conventions de ce dossier

- **Mesuré** : relevé par une commande reproductible, dont le résultat est dans `livrables/aia2/donnees/`.
- **Hypothèse** (H1, H2…) : valeur choisie faute de donnée, jamais présentée comme un fait. Le registre est dans le document 09.
- **Non vérifié** : écrit mais non exécuté faute de moyen (par exemple Terraform `plan`, Redshift, Debezium) ; la liste complète est dans `13_couverture_et_limites.md`.
- Les données sont synthétiques ; les taux de fraude et corrélations des résultats viennent de paramètres de simulation choisis (`stripe_demo/oltp.py`) et ne disent rien du monde réel.

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
