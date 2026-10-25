# Bloc 2 AIA : architecture de données pour l'IA

## Une plateforme de paiement : enregistrer sans perdre, analyser à grande échelle, détecter la fraude en temps réel

Une architecture de données complète pour une plateforme de paiement en ligne : base transactionnelle normalisée, capture des changements vers un bus de messages, lac et entrepôt en étoile, base de documents, modèle de fraude noté sur le flux, orchestration quotidienne, et infrastructure AWS décrite en Terraform. Une démonstration exécutable fait tourner la chaîne de bout en bout sur un poste.

Le code est dans [`stripe/`](stripe/), les documents de conception dans [`livrables/aia2/`](livrables/aia2/), les diagrammes en images dans [`livrables/schemas/`](livrables/schemas/), un résumé d'une page dans [`RESUME_UNE_PAGE.md`](RESUME_UNE_PAGE.md). Chaque chiffre de cette page renvoie au fichier qui le consigne.

Projet réalisé pour le bloc 2 de la certification Architecte en intelligence artificielle (RNCP38777, renouvelée sous le RNCP41993), consacré à la conception et au déploiement d'architectures de données pour l'IA.

> **Nature du travail.** Étude de cas réalisée sur l'énoncé « Stripe Business Case » ([document 00](livrables/aia2/00_cahier_des_charges.md)). L'architecture est une proposition : elle ne décrit pas l'infrastructure de l'entreprise et n'utilise aucune de ses données. Toutes les données sont synthétiques, et les volumes de dimensionnement sont des hypothèses illustratives ([document 09](livrables/aia2/09_dimensionnement_finops_greenops.md), H1).

![Schéma de l'architecture : les paiements des marchands entrent dans une base PostgreSQL de 13 tables ; Debezium lit son journal et publie un sujet Kafka par table ; les micro-lots vont dans un lac S3 au format Parquet, chargé dans un entrepôt en étoile (Redshift) qui sert les requêtes et l'entraînement ; le score de fraude note chaque nouveau paiement, tient à jour les caractéristiques des clients dans une base de documents (4 collections), que la version 1.0 du modèle ne lit pas encore, et alerte au-delà du seuil](figures/schema_architecture.png)

---

## 1. Problématique

Une plateforme de paiement relie des marchands, leurs clients et des moyens de paiement. Elle doit enregistrer chaque transaction sans la perdre ni la dupliquer, analyser de gros volumes, ranger des données peu structurées et noter chaque paiement avec un modèle de fraude, sous PCI-DSS v4.0.1, RGPD et CCPA ([résumé](RESUME_UNE_PAGE.md)).

Ces besoins tirent dans des directions opposées : des écritures unitaires sûres (ACID) d'un côté, des lectures massives de l'autre, des documents souples à côté, et un score à rendre en quelques millisecondes. Le projet y répond par des stockages séparés, reliés par la lecture du journal de transactions plutôt que par des requêtes sur les tables.

## 2. Données

**Les données sont entièrement synthétiques.** Aucune donnée réelle, aucun jeu à télécharger, aucun fichier de données versionné : la démonstration les génère.

- La démonstration crée 40 marchands, 2 000 clients et 48 000 paiements sur 60 jours (du 1er août au 29 septembre 2026), puis fait arriver 1 000 paiements de plus par la capture des changements, en cinq micro-lots de 200 ([`resultats_demo.json`](livrables/aia2/donnees/resultats_demo.json), [document 08](livrables/aia2/08_requetes.md), [document 05](livrables/aia2/05_pipeline.md)).
- Les taux de fraude et les corrélations sont **construits par le simulateur** : ils ne disent rien du monde réel ([document 00](livrables/aia2/00_cahier_des_charges.md), § 7 ; [document 13](livrables/aia2/13_couverture_et_limites.md), § 4).
- Aucun numéro de carte : la base ne reçoit qu'un jeton et les quatre derniers chiffres ; les adresses électroniques de démonstration sont hachées ([document 02](livrables/aia2/02_oltp_erd.md), [document 06](livrables/aia2/06_securite_conformite.md), hypothèse H10).

**Obtenir les données** : `python -m stripe_demo.pipeline` (section 5). Les bases et le rapport produits vont dans `stripe/sorties/`, ignoré par Git.

## 3. Méthode

### Architecture

| Besoin | Choix | Pourquoi |
|---|---|---|
| Écrire les paiements | PostgreSQL (Aurora en production) : 13 tables en troisième forme normale, 74 colonnes, 18 clés étrangères, 7 contraintes `CHECK`, 3 contraintes d'unicité, dont une clé d'idempotence | un paiement rejoué n'est pas débité deux fois ([document 02](livrables/aia2/02_oltp_erd.md)) |
| Relier les bases | capture des changements (Debezium) vers Kafka, 8 tables publiées | lire le journal de transactions au lieu d'interroger les tables ([document 05](livrables/aia2/05_pipeline.md)) |
| Analyser | lac S3 en Parquet, entrepôt en étoile (Redshift Serverless ; DuckDB dans la démonstration) | lectures massives séparées des écritures ([document 03](livrables/aia2/03_olap_schema.md)) |
| Données souples | base de documents, 4 collections, événements supprimés après 90 jours | structure souple, minimisation ([document 04](livrables/aia2/04_nosql_modele.md)) |
| Fraude | modèle d'arbres entraîné sur l'entrepôt, noté sur le flux ; dérive suivie par le PSI, réentraînement au-delà de 0,2 | le score suit la distribution réelle des paiements ([document 07](livrables/aia2/07_integration_ml.md)) |
| Orchestration | DAG Airflow quotidien de huit tâches ; un contrôle de qualité en échec arrête la publication de la partition et le réentraînement ; 3 nouvelles tentatives à intervalle croissant | une tâche en échec bloque les suivantes : rien n'est publié ni réentraîné sur des données incohérentes ([document 05](livrables/aia2/05_pipeline.md), § 2) |
| Infrastructure | Terraform AWS : 13 modules, environnements `dev` et `prod`, réseau privé sans passerelle Internet, 8 clés KMS (une par domaine), 6 rôles IAM sans joker | chiffrement par domaine, moindre privilège ([document 01](livrables/aia2/01_architecture_globale.md), § 3 ; [`stripe/terraform/`](stripe/terraform/)) |
| Calcul | ECS sur Fargate : service de score de 2 à 6 tâches, autoscaling sur le CPU (cible 60 %) ; tâche ETL quotidienne | bornes tirées de la pointe extrapolée (6 tâches dans le scénario S3) et de deux zones ; tailles posées en hypothèse ([document 09](livrables/aia2/09_dimensionnement_finops_greenops.md), H16 et § 4.5) |
| Supervision | alarmes CloudWatch (retard de l'emplacement de réplication, consommation de l'entrepôt, score sans tâche, événements Aurora) vers une rubrique SNS chiffrée ; commande d'astreinte qui mesure le retard de réplication | un connecteur arrêté laisse l'emplacement de réplication retenir le journal de transactions, qui s'accumule sur le disque ([document 10](livrables/aia2/10_continuite_supervision_maintenance.md), § 3 et § 4) |
| Production locale | Docker Compose : PostgreSQL, MongoDB, Redpanda, Debezium, conteneurs durcis | répéter la chaîne sur un poste ([`stripe/README.md`](stripe/README.md)) |

Les scripts SQL et JavaScript de déploiement sont **générés** depuis un modèle de données unique ; un test échoue si l'un d'eux est périmé ([`stripe/README.md`](stripe/README.md), « Régénérer les scripts de déploiement »).

### Documentation accessible

Les 17 diagrammes publiés sont écrits en Mermaid, affichés directement par GitHub, et exportés en images dans [`livrables/schemas/`](livrables/schemas/). Chacun est suivi d'une **description textuelle**, et un test vérifie qu'aucun diagramme n'en est privé ([`livrables/schemas/LISEZ_MOI.md`](livrables/schemas/LISEZ_MOI.md)). Le dossier comprend aussi 23 pannes décrites avec leur effet et leur parade, et 11 contrats d'interface avec une matrice de responsabilités ([document 13](livrables/aia2/13_couverture_et_limites.md), § 1).

## 4. Résultats chiffrés

Données synthétiques, fraude simulée : **ces chiffres démontrent la chaîne et la méthode, pas une performance réelle.**

### Démonstration de bout en bout

| Mesure | Résultat | Source |
|---|---|---|
| Base transactionnelle | 40 marchands, 2 000 clients, 48 000 paiements ; 48 000 faits chargés dans l'entrepôt | [`resultats_demo.json`](livrables/aia2/donnees/resultats_demo.json) |
| Modèle de fraude | 38 400 paiements pour apprendre, 9 600 pour évaluer (découpage dans le temps) ; taux de fraude 2,51 %, prévalence de la période de test 2,28 % | [document 07](livrables/aia2/07_integration_ml.md), § 4 |
| Qualité du modèle (test) | ROC-AUC 0,872 ; PR-AUC 0,16, environ 7 fois la prévalence, modeste en valeur absolue | idem |
| Flux | 1 000 paiements notés, 40 alertes, latence médiane de 9,1 ms par paiement ; 793 clients avec un document de caractéristiques | [`resultats_demo.json`](livrables/aia2/donnees/resultats_demo.json), [document 05](livrables/aia2/05_pipeline.md), § 5 |
| Alertes au seuil de 0,8 | 22 fraudes parmi les 1 000 paiements, 10 détectées : précision 25 %, rappel 45,5 % | [document 07](livrables/aia2/07_integration_ml.md), § 5.3 |
| Dérive | PSI 0,01 : stable | [document 07](livrables/aia2/07_integration_ml.md), § 6.1 ; [`resultats_demo.json`](livrables/aia2/donnees/resultats_demo.json) |
| Requête métier (exemple) | 24,87 % de litiges quand le pays de l'IP diffère de celui du client (2 549 paiements), 1,28 % sinon : une corrélation **construite par le simulateur** | [`resultats_demo.json`](livrables/aia2/donnees/resultats_demo.json), [document 08](livrables/aia2/08_requetes.md) |

### Mesures et dimensionnement

| Mesure | Résultat | Source |
|---|---|---|
| Occupation dans PostgreSQL 16 (48 000 paiements) | 298,5 octets par paiement, index compris | [document 09](livrables/aia2/09_dimensionnement_finops_greenops.md), § 1.1 |
| Journal de transactions du chargement complet | 646,3 octets par paiement | idem, § 1.1 |
| Démonstration dans un conteneur limité à 1 Gio | pic de 558 837 760 octets, 52 % de la limite | idem, § 4.5 |
| Coût mensuel (liste de prix publique AWS, eu-west-1, relevé du 10/10/2026) | au moins 2 471 USD en production, au moins 824 USD en développement : des **planchers**, plusieurs postes ne sont pas chiffrés | idem, § 5.5 |

### Qualité du code et de l'infrastructure

| Mesure | Résultat | Source |
|---|---|---|
| Tests (20/10/2026, sur la version publiée) | 774 réussis, 6 ignorés : les 6 tests qui exigent Docker (lancés à part sur PostgreSQL 16 le 19/10/2026, où ils réussissaient) | [document 13](livrables/aia2/13_couverture_et_limites.md), § 2 |
| Style | `ruff check .` sans écart | idem |
| Analyse de sécurité du Terraform (checkov, 19/10/2026) | 536 contrôles réussis, 0 échec, 18 ignorés (8 règles, chacune justifiée dans le code) | [`checkov.txt`](livrables/aia2/checkov.txt), [document 06](livrables/aia2/06_securite_conformite.md), § 9 |
| Tests de mutation (19/10/2026) | 3 186 mutants, 3 186 tués, aucun mutant équivalent | [`stripe/MUTANTS_EQUIVALENTS.md`](stripe/MUTANTS_EQUIVALENTS.md) |
| Couverture de la grille du référentiel | 26 indicateurs couverts sur 27 ; le 4.3 est partiel (pas de sortie de `terraform plan`) | [document 13](livrables/aia2/13_couverture_et_limites.md), § 1 |

## 5. Reproduire

Prérequis : Python 3.12 ou plus. Docker avec Compose et Node.js sont facultatifs ; Terraform n'est nécessaire que pour valider l'infrastructure. Toutes les commandes se lancent depuis `stripe/`.

```bash
cd stripe
python -m venv .venv
source .venv/bin/activate                 # Windows : .venv\Scripts\activate
pip install -r requirements-dev.txt
python -m stripe_demo.pipeline            # la démonstration complète
pytest                                    # 774 réussis, 6 ignorés (tests Docker)
ruff check .
python -m stripe_demo.generer_ddl --verifier   # les scripts versionnés sont-ils à jour ?
```

Au premier lancement, DuckDB télécharge une petite extension : il faut un accès réseau une fois.

**Tests sur un vrai PostgreSQL 16** (l'image `postgres:16` doit être présente) :

```bash
export STRIPE_TESTS_DOCKER=1              # Windows : set STRIPE_TESTS_DOCKER=1
pytest tests/test_postgres_docker.py
```

**Production locale** :

```bash
python deploiement/generer_secrets.py     # 4 mots de passe aléatoires dans secrets/, ignoré par Git
docker compose up --build
docker compose exec redpanda rpk topic consume stripe.public.paiements -o start -n 3
docker compose down -v
```

Seuls les services `postgres`, `amorcage` et `demonstration` ont été exécutés ; `mongodb`, `redpanda`, `connect` et l'enregistrement du connecteur ne l'ont pas été ([`stripe/README.md`](stripe/README.md), « État de vérification »).

**Infrastructure** : voir [`stripe/terraform/README.md`](stripe/terraform/README.md). Sans compte AWS, `terraform init -backend=false` puis `terraform validate` contrôlent la syntaxe et la cohérence des modules ; l'analyse de sécurité se rejoue, depuis la racine de ce dossier, par `checkov -d stripe/terraform --framework terraform --compact`. Ces commandes Terraform n'ont pas encore été exécutées (section 6).

## 6. Limites

Ce que le projet ne démontre pas, dit tel quel ([document 13](livrables/aia2/13_couverture_et_limites.md), § 3) :

- **Rien n'est déployé sur AWS.** Terraform n'a pas été exécuté : ni `init`, ni `validate`, ni `fmt`, ni `plan`. Seuls checkov et des tests statiques ont lu le code ; l'indicateur 4.3, qui attend la sortie de `terraform plan`, reste partiel.
- **MongoDB, Redpanda et Debezium n'ont jamais tourné** : leurs images n'ont pas été téléchargées. Aucun message de capture réel n'a été observé ; le format décrit au document 05 est celui attendu d'après la configuration.
- **Le DDL Redshift n'a pas été exécuté** ; seule sa variante portable l'a été, sur DuckDB. La prise en charge de `$jsonSchema` par DocumentDB est à confirmer.
- **Le DAG est testé sans Airflow** et n'a jamais été exécuté par un ordonnanceur. Les tâches de calcul et les alarmes sont codées en Terraform, non déployées ; les tableaux de bord ne sont pas codés ; la fonction de rotation des secrets et l'exécution de Debezium sur MSK Connect sont décrites, non codées.
- **Non testés** : l'isolation et la concurrence des transactions, la bascule d'Aurora et le comportement de l'emplacement de réplication après une bascule.
- **Les coûts sont des planchers** datés du 10/10/2026 ; aucune émission de CO₂ n'est chiffrée.
- **Le modèle est entraîné sur une fraude simulée** : sa PR-AUC est modeste et ne prédit rien sur des données réelles. Pas de registre de modèles, pas de déploiement en ombre, pas de mesure d'équité réelle ([document 07](livrables/aia2/07_integration_ml.md)).
- **La vidéo de démonstration n'est pas produite** ; son script (document 12) n'est pas publié dans cette version.

## 7. Organisation du dossier

```text
aia_bloc_2_ai_data_architecture/
|-- README.md                 cette page
|-- RESUME_UNE_PAGE.md        contexte, choix, preuves, coûts et limites en une page
|-- figures/                  schéma d'ensemble de l'architecture
|-- livrables/aia2/           14 documents (00 cahier des charges à 13 couverture et limites),
|                             checkov.txt, mesures en JSON, outil de relevé des prix
|-- livrables/schemas/        les 17 diagrammes publiés en images, avec leur index
`-- stripe/
    |-- stripe_demo/          modèle OLTP, démonstration, entrepôt, documents, fraude, supervision
    |-- dags/                 DAG Airflow quotidien
    |-- sql/, nosql/          DDL générés, requêtes métier, schémas de documents
    |-- deploiement/          connecteur Debezium, comptes de service, génération des secrets
    |-- terraform/            13 modules AWS, environnements dev et prod
    |-- tests/                suite pytest
    `-- docker-compose.yml, Dockerfile
```

Le code lit `livrables/aia2/` (mesures et documents vérifiés par les tests) : garder les deux dossiers côte à côte.

## 8. Différences avec le livrable déposé

- Le document `12_script_video.md` (script de la vidéo) n'est pas publié : la liste des documents attendus par `stripe/tests/test_conformite_depot.py` ne le contient donc pas. Le reste du test est celui du livrable.
- Le modèle de variables de la démonstration s'appelle ici `stripe/modele.env.exemple` (`modele.env` dans le livrable), pour qu'aucun fichier publié ne ressemble à un fichier `.env` réel ; il ne contient aucun secret.
- Les courriels fictifs des clients de la démonstration, hachés avant stockage, sont `client{i}@example.org` (domaine réservé) au lieu de `client{i}@exemple.org` : une ligne de `stripe/stripe_demo/oltp.py`, et dans `stripe/tests/test_oltp.py` le hachage attendu du client 1 et l'empreinte des tables de référentiel, qui couvre ces hachages.

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
