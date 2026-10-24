# 13 - Couverture du référentiel, vérifications et limites

> Ce document dit ce qui est prouvé, par quelle commande, et ce qui ne l'est pas encore. Conventions « mesuré », « hypothèse », « non vérifié » : document 00, § 7.

## 1. Couverture des 27 indicateurs (RNCP41993, bloc 2)

Statuts : **Couvert** = livrable écrit et preuve fournie ; **Partiel** = livrable écrit, une preuve attendue manque.

| Indicateur | Livrable / section | Preuve dans le dépôt | Statut |
|---|---|---|---|
| 1.1 Besoins de calcul CPU/GPU | 09 § 4.1 | mesures (entraînement 0,5 s, latence) ; extrapolation par scénario | Couvert |
| 1.2 Dimensionnement du stockage | 09 § 1, 3, 4.2 | octets par ligne mesurés sur PostgreSQL 16 ; `dimensionnement.py` + tests | Couvert (hypothèses H1 à H3) |
| 1.3 Cohérence entraînement / inférence | 09 § 4.3 ; 07 | reproductibilité vérifiée ; tâches de notation par scénario | Couvert |
| 2.1 Architecture logique | 01 § 2 | diagramme + tableau des composants | Couvert |
| 2.2 Architecture physique | 01 § 3 | diagramme + modules Terraform | Couvert (non déployé) |
| 2.3 Topologies calcul et stockage | 01 § 4 | tableau charge / calcul / stockage | Couvert |
| 2.4 Évolutivité | 01 § 6 | tableau des mécanismes ; variables Terraform | Couvert |
| 3.1 Analyse coûts / performance | 01 § 7.2 ; 09 § 5 | mesure d'index (15 pages contre 756) ; leviers de coût | Couvert : chiffrage daté au 09 § 5.5 (prix de la liste publique AWS, eu-west-1) : un plancher, plusieurs postes non chiffrés (liste au 09 § 5.5) |
| 3.2 Souveraineté | 01 § 7.3 ; 06 | région UE, clés du client, minimisation ; risque résiduel nommé | Couvert |
| 3.3 PaaS / IaaS / serverless | 01 § 7.1 ; 09 § 5.4 | tableau d'arbitrage par composant | Couvert |
| 4.1 Reproductibilité, idempotence | `stripe/terraform/` ; DDL `IF NOT EXISTS` | DDL rejoué sur PostgreSQL ; tests d'idempotence CDC | Couvert (principe et garde-fous) ; l'état « No changes » après `apply` n'a pas été constaté faute de `plan` |
| 4.2 Modularité, versioning | 13 modules, 2 environnements, Git | `test_terraform.py` (appels de modules cohérents) | Couvert |
| 4.3 Validation et sécurité du code | `checkov.txt`, `test_terraform.py` | 536 / 0 / 18 (19/10/2026) | **Partiel : pas de sortie de `terraform plan`** |
| 5.1 Structuration du stockage | 09 § 4.4 | lac par date, compaction, séparation lecture / écriture | Couvert |
| 5.2 Environnements d'exécution | 09 § 4.5 ; `docker-compose.yml`, `Dockerfile` ; module `calcul` | limite mesurée (52 %), conteneurs durcis, exécution réelle ; tâches Fargate de production dimensionnées (H16) et testées statiquement | Couvert (tâches de production non déployées) |
| 6.1 Scalabilité | 01 § 6 ; 02 § 7 | partitionnement vérifié sur PostgreSQL 16 | Couvert |
| 6.2 Failover | 02 § 4 et 5 ; 10 | tableau de promotion ; objectifs H5 | Couvert (mécanismes définis) ; bascule non exercée |
| 6.3 Continuité d'activité | 10 § 3 | 23 pannes → effet → parade | Couvert |
| 7.1 Gestion des identités | 06 § 6 ; module `iam` | 6 rôles, aucun joker (test), rôle d'exécution ECS distinct, rôles de base de données testés | Couvert |
| 7.2 Protection des données | 06 § 5 | chiffrement par domaine, TLS, segmentation | Couvert |
| 7.3 Gestion des secrets | 06 § 7 ; module `secrets` ; compose | tests « aucun secret en clair » | Couvert (rotation à fournir) |
| 8.1 FinOps | 09 § 5 | leviers, garde-fous, méthode, **chiffrage daté** (prod ≥ 2 471 USD par mois, dev ≥ 824 dont 76 % de cluster MSK au repos ; variante Redpanda recommandée : ≈ 164 USD de postes chiffrés, plus l'hébergement de Redpanda) | Couvert ; plancher, postes non chiffrés listés |
| 8.2 GreenOps | 09 § 6 | leviers, indicateurs de suivi, proxies mesurés (octets par couche, durée de calcul) | Couvert ; aucune émission de CO₂ chiffrée (facteurs d'émission non disponibles) |
| 8.3 Monitoring | 10 § 4 | tableau d'indicateurs et seuils | Couvert (plan de supervision : indicateurs, seuils, chaîne d'alerte ; quatre alertes et la notification codées, module `supervision`) ; tableaux de bord non codés |
| 9.1 Coordination technique | 11 | 11 contrats d'interface + matrice de responsabilités | Couvert |
| 9.2 Documentation | `stripe/README.md`, 13 documents (le document 12, script de la vidéo, n'est pas publié dans cette version) | descriptions textuelles de tous les diagrammes (test), glossaire | Couvert |
| 9.3 Maintenabilité | 10 § 6 | procédures par composant | Couvert (non rejoué) |

## 2. Ce qui a été vérifié, et comment

Environnement de rédaction : Windows 11, **Python 3.13**, SQLAlchemy 2.1, DuckDB 1.5, PostgreSQL 16.15 (conteneur), Docker 29.8, Node.js 22.

| Commande | Résultat |
|---|---|
| `ruff check .` (depuis `stripe/`) | `All checks passed!` |
| `pytest`, porte de qualité complète (19/10/2026) | **924 réussis, 6 ignorés** (les 6 tests optionnels qui exigent Docker) ; ruff, mypy strict, documentation vivante et tests inverses conformes ; mutation : résultat et détail par module dans `stripe/MUTANTS_EQUIVALENTS.md`. Version publiée, 20/10/2026 : **774 réussis, 6 ignorés** (un contrôle de conformité propre au dépôt d'origine et le document 12 ne sont pas publiés) |
| `pytest`, second environnement sous **Python 3.12.14** (10/10/2026) | **666 réussis, 5 ignorés** ; `ruff` sans écart ; `generer_ddl --verifier` : 0 |
| `pytest`, nouvelle exécution (11/10/2026) | **669 réussis, 5 ignorés** (3 cas de conformité de plus pour l'outil `livrables/aia2/outils/lire_prix_aws.py`, ajouté le 11/10) |
| `pytest`, porte de qualité complète (12/10/2026) | **802 réussis, 5 ignorés** ; ruff, mypy strict, documentation vivante et tests inverses conformes ; mutation : 2 721 mutants tués sur 2 721 |
| `STRIPE_TESTS_DOCKER=1 pytest tests/test_postgres_docker.py` | **6 réussis** sur PostgreSQL 16 (19/10/2026 ; le sixième : requête de retard sur un emplacement de réplication laissé sans lecteur) |
| `python -m stripe_demo.pipeline` | exécutée plusieurs fois, ~30 s ; 2 exécutions consécutives identiques hors latence |
| `python -m stripe_demo.generer_ddl --verifier` | code de sortie 0 |
| `docker compose config` | valide |
| `docker build` | image construite (base Python 3.14 disponible localement ; le défaut 3.12 n'a pas été construit) |
| `docker compose up` de `postgres`, puis `amorcage` et `demonstration` | succès : schéma appliqué, comptes de service créés par secrets, 2 000 clients et 150 paiements insérés avec le compte applicatif, conteneurs en lecture seule |
| démonstration complète dans le conteneur (limite 1 Gio) | succès en 15 s, pic mesuré 558 837 760 octets |
| `checkov -d stripe/terraform --framework terraform` | 461 réussis, 0 échec, 18 ignorés |
| même analyse rejouée le 17/10/2026 (hors ligne, `--skip-download`) | 461 réussis, 0 échec, 18 ignorés : mêmes contrôles et mêmes ressources ; numéros de ligne de `environnements/prod/main.tf` décalés d'une ligne, conformes au fichier actuel |
| même analyse rejouée le 19/10/2026 (checkov 3.3.28, hors ligne), après l'ajout des modules `calcul` et `supervision` | **536 réussis, 0 échec, 18 ignorés** : 75 contrôles de plus, sur les ressources ajoutées ; les 18 ignorés sont les mêmes |
| Mermaid 11.4.0 dans le navigateur (`parse` et `render`) | 18 diagrammes sur 18 valides |

Sur PostgreSQL 16 réel, vérifiés par exécution : idempotence du DDL (rejeu sans erreur), 13 tables, publication logique de 8 tables, refus du `UPDATE`/`DELETE` des montants au compte applicatif, journal d'audit en ajout seul, décodage logique d'une insertion et d'une mise à jour, plans d'exécution avec et sans index, contraintes du partitionnement natif.

## 3. Ce qui n'a pas été vérifié

1. **Terraform** : `init`, `validate`, `plan`, `fmt` non exécutés (binaire non installé sur la machine de rédaction, constaté de nouveau le 19/10/2026 ; les modules `calcul` et `supervision` ajoutés ce jour-là n'ont été lus que par checkov et par les tests statiques). `plan` exige de plus un compte AWS. L'indicateur 4.3 attend « la sortie de `terraform plan` » : elle manque ; seuls checkov et des tests statiques sont fournis. Le code a été comparé à la documentation du fournisseur (arguments des ressources principales), non exécuté.
2. **Images Docker non téléchargées** : `mongo`, `redpanda`, `debezium/connect`, `curl`. Donc : MongoDB réel, courtier, Debezium, enregistrement du connecteur et lecture des sujets n'ont **jamais tourné**. Les options de durcissement de ces services (`cap_drop: ALL`) n'ont été éprouvées que sur PostgreSQL et le service Python : premier suspect si l'un d'eux ne démarre pas. Aucun message Debezium réel n'a été capturé ; le format décrit dans le document 05 est celui attendu d'après la configuration.
3. **La vidéo** n'est pas produite ; son script (document 12) n'est pas publié dans cette version.
4. **Redshift** : le DDL Redshift (clés de distribution, vues matérialisées, `MERGE`) n'a pas été exécuté ; seule sa variante portable l'a été (DuckDB).
5. **DocumentDB** : la prise en charge de `$jsonSchema` est à confirmer.
6. ~~Python 3.12~~ : **vérifié depuis** : exécution du 10/10 sous Python 3.12.14, 666 tests réussis (669 depuis l'ajout de l'outil de relevé des prix, le 11/10 ; 802 depuis la porte de qualité du 12/10 ; 924 depuis celle du 19/10).
7. **Airflow** : DAG codé (`stripe/dags/stripe_quotidien.py`), testé sans Airflow et chargé sans erreur par Airflow 2.10.5, jamais exécuté par un ordonnanceur. **Tâches de calcul** et **alarmes** : codées en Terraform (modules `calcul` et `supervision`), non déployées ; tableaux de bord non codés. **Fonction de rotation des secrets**, exécution de Debezium sur MSK Connect : décrites, non codées.
8. **Isolation / concurrence** des transactions : non testée. Bascule Aurora et comportement de l'emplacement de réplication après bascule : non testés.
9. **Prix** : chiffrage daté ajouté le 10/10/2026 (09 § 5.5), à partir de la liste de prix publique d'AWS ; c'est un plancher. Non chiffrés (liste du 09 § 5.5) : connecteur Debezium sur MSK Connect, tâches Fargate du score et de l'ETL, points de terminaison d'interface, stockage et entrées-sorties d'Aurora, stockage de DocumentDB, de Redshift et de MSK (prix relevés, volumes non estimés), CloudWatch Logs, AWS Backup, transferts réseau, hébergement de Redpanda dans la variante recommandée ; émissions de CO₂ non calculées.

## 4. Hypothèses

Registre complet H1 à H17 dans `09_dimensionnement_finops_greenops.md` § 2 : volumes 100 000 / 1 000 000 / 10 000 000 paiements par jour (**illustratifs**), facteur de pointe 5, rétention OLTP 13 mois, région `eu-west-1`, objectifs de reprise, tailles d'instances, plafonds de consommation, seuils d'alerte, fraîcheur, traitement du PAN hors périmètre, bases légales, absence de vente de données. Les chiffres de **mesure** (octets par paiement, journal de transactions, mémoire, taille Parquet, durée d'entraînement) viennent de `livrables/aia2/donnees/` et se refont avec `python -m stripe_demo.mesures`.

Les taux de fraude et corrélations des requêtes (par exemple 24,87 % de litiges quand le pays de l'IP diffère) sont **construits par le simulateur** ; les documents le disent.

## 5. Reste à faire, par ordre d'utilité

1. Installer Terraform, exécuter `init -backend=false`, `validate`, `fmt -check`, puis `plan` sur un compte de test ; joindre la sortie (indicateur 4.3).
2. Télécharger les images manquantes, exécuter la chaîne complète du compose, capturer de vrais messages Debezium ; enregistrer la vidéo.
3. Écrire la fonction de rotation des secrets ; compléter la supervision (métriques applicatives de fraîcheur, de qualité et de PSI publiées par le DAG et le score, leurs alarmes, un tableau de bord).
4. Rendre le chargement des caractéristiques en ligne idempotent et leur donner un rôle dans le modèle (version 2).
5. Compléter le chiffrage des postes non relevés (09 § 5.5) et le refaire le jour de la décision.

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
