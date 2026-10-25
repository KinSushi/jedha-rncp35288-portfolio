# Architecture de données de type Stripe : code, démonstration et déploiement

Bloc 2 de la certification AIA. Ce dossier contient le **code** (modèle de données, démonstration exécutable, déploiement local, infrastructure AWS) ; la **documentation** détaillée est dans `../livrables/aia2/` (commencer par `00_cahier_des_charges.md`, puis `13_couverture_et_limites.md`).

Toutes les données sont **synthétiques**. Aucun secret n'est versionné.

## Contenu

```text
stripe/
├── stripe_demo/            code Python (commenté en français)
│   ├── modele_oltp.py      modèle transactionnel, source de vérité (13 tables)
│   ├── oltp.py             base de démonstration, données synthétiques, déclencheurs CDC
│   ├── olap.py             entrepôt en étoile (DuckDB), chargement batch et CDC
│   ├── nosql.py            collections de documents (mongomock), requêtes
│   ├── fraude.py           modèle de fraude, score, indice de dérive (PSI)
│   ├── pipeline.py         démonstration de bout en bout
│   ├── generer_ddl.py      génère les scripts SQL et JavaScript de déploiement
│   ├── validation_mongo.py validation des documents d'après les schémas MongoDB
│   ├── vers_postgres.py    alimente un vrai PostgreSQL (production locale)
│   ├── supervision.py      retard des emplacements de réplication (commande d'astreinte)
│   ├── mesures.py          mesures réelles (disque, mémoire, modèle)
│   └── dimensionnement.py  extrapolation des volumes sous hypothèses déclarées
├── dags/                   DAG Airflow quotidien (stripe_quotidien.py), testé sans Airflow
├── sql/                    DDL PostgreSQL et Redshift (générés), requêtes métier
├── nosql/                  validateurs JSON Schema, script de création, requêtes mongosh
├── deploiement/            connecteur Debezium, comptes de service, génération des secrets
├── terraform/              modules AWS et environnements dev / prod (voir terraform/README.md)
├── tests/                  tests pytest
├── docker-compose.yml      production locale (PostgreSQL, MongoDB, Redpanda, Debezium)
└── Dockerfile              image du service de démonstration
```

## Prérequis

| Outil | Pour quoi | Obligatoire |
|---|---|---|
| Python 3.12 ou plus (testé avec 3.13) | démonstration et tests | oui |
| Docker avec Compose | production locale, tests sur PostgreSQL 16 | non |
| Node.js | tests des scripts `mongosh` (simulés) | non (tests ignorés sinon) |
| Terraform 1.6 ou plus | `validate` et `plan` de l'infrastructure | non, mais à faire avant tout déploiement |

## Démarrer

```text
cd stripe
python -m venv .venv
.venv\Scripts\activate                 # Linux ou macOS : source .venv/bin/activate
pip install -r requirements-dev.txt
python -m stripe_demo.pipeline         # la démonstration complète (environ 30 secondes)
```

La démonstration crée `sorties/` (ignoré par Git) : bases SQLite et DuckDB, rapport `rapport_demo.json`. Au premier lancement, DuckDB télécharge une petite extension (`sqlite`) ; il faut donc un accès réseau une fois.

## Tester et contrôler le style

```text
pytest                                  # 774 cas de test réussis (dont des cas paramétrés) ; 6 tests Docker sont ignorés
ruff check .                            # PEP 8, 99 colonnes
python -m stripe_demo.generer_ddl --verifier   # les scripts versionnés sont-ils à jour ?
```

Tests sur un vrai PostgreSQL 16 (conteneur jetable ; l'image `postgres:16` doit être déjà présente, rien n'est téléchargé) :

```text
set STRIPE_TESTS_DOCKER=1               # Linux ou macOS : export STRIPE_TESTS_DOCKER=1
pytest tests/test_postgres_docker.py
```

## Régénérer les scripts de déploiement

Le modèle de données existe en un seul endroit ; les fichiers suivants en sont **dérivés** et ne se modifient pas à la main :

```text
python -m stripe_demo.generer_ddl
```

écrit `sql/oltp_schema_postgresql.sql`, `sql/olap_schema.sql` et `nosql/creer_collections.js`. Un test échoue si l'un d'eux est périmé.

## Production locale avec Docker Compose

```text
python deploiement/generer_secrets.py   # 4 mots de passe aléatoires dans secrets/ (ignoré par Git)
docker compose up --build               # PostgreSQL, MongoDB, Redpanda, Debezium, démonstration
docker compose exec redpanda rpk topic consume stripe.public.paiements -o start -n 3
docker compose down -v                  # tout arrêter et effacer les volumes
```

Si un port est déjà pris sur la machine : `POSTGRES_PORT_HOTE=5433 docker compose up` (idem `MONGO_PORT_HOTE`, `CONNECT_PORT_HOTE` ; voir `modele.env.exemple`). Sous PowerShell : `$env:POSTGRES_PORT_HOTE=5433`.

Démonstration autonome dans le conteneur contraint (1 Gio, système de fichiers en lecture seule) :

```text
docker compose run --rm --no-deps demonstration python -m stripe_demo.pipeline
```

**État de vérification** (détails dans `../livrables/aia2/13_couverture_et_limites.md`) : le fichier Compose est validé par `docker compose config` ; l'image se construit ; les services `postgres`, `amorcage` et `demonstration` ont été exécutés ; **les services `mongodb`, `redpanda`, `connect` et l'enregistrement du connecteur n'ont pas été exécutés** (images non téléchargées).

## Infrastructure AWS

Voir `terraform/README.md` : `terraform init`, `validate`, `plan`, et l'analyse de sécurité `checkov -d terraform`.

## Mesures et dimensionnement

```text
python -m stripe_demo.mesures demo                  # mémoire, durée, taille des fichiers, modèle
python -m stripe_demo.mesures postgres --url postgresql+psycopg://utilisateur@hote:5432/base
python -m stripe_demo.dimensionnement               # volumes extrapolés sous hypothèses H1 à H3
```

## Glossaire

| Terme | Sens |
|---|---|
| ACID | atomicité, cohérence, isolation, durabilité : garanties d'une transaction |
| OLTP / OLAP | traitement transactionnel (écritures unitaires) / analytique (lectures agrégées) |
| CDC | capture des changements : publier chaque modification d'une base |
| WAL | journal de transactions de PostgreSQL |
| Étoile | schéma OLAP : une table de faits entourée de dimensions |
| SCD de type 2 | dimension qui conserve l'historique des versions |
| TTL | durée de vie : suppression automatique après un délai |
| PSI | indice de stabilité de population : mesure de la dérive d'une distribution |
| RPU | unité de calcul de Redshift Serverless |
| RTO / RPO | durée de reprise visée / perte de données admise |
| PAN, CDE | numéro de carte / environnement qui le traite (hors périmètre ici) |
| WORM | écriture unique, lecture multiple (objets non modifiables) |
| MCC | code de catégorie d'activité d'un marchand |
| IaC | infrastructure décrite par du code |

## Dépannage

| Symptôme | Cause probable | Remède |
|---|---|---|
| `extension "sqlite" ... failed` | pas d'accès réseau au premier lancement | se connecter une fois, ou copier le dossier `~/.duckdb` |
| `ports are not available: ... 5432` | un PostgreSQL local occupe le port | `POSTGRES_PORT_HOTE=5433` |
| Tests Docker ignorés | variable absente ou image `postgres:16` absente | définir `STRIPE_TESTS_DOCKER=1` ; `docker pull postgres:16` |
| `secret manquant : définir ..._FILE` | secrets non générés | `python deploiement/generer_secrets.py` |
| un service (`mongodb`, `redpanda`, `connect`) ne démarre pas | les options de durcissement (`cap_drop: ALL`, `no-new-privileges`) n'ont été éprouvées que sur `postgres` et le service Python | retirer temporairement `<<: *securite` du service concerné pour confirmer la cause, puis ajouter les seules capacités nécessaires |

## Conventions

Python 3.12, PEP 8 à 99 colonnes (ruff), commentaires en français qui expliquent le *pourquoi*, mention de droits d'auteur en tête de chaque fichier, aucun secret versionné.

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
