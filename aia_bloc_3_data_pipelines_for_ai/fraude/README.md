# Détection automatique de fraude : pipeline temps réel et rapport quotidien

Projet du bloc 3 de la certification AIA (énoncé « Automatic Fraud Detection »). Le métier
demande deux choses : **être notifié dès qu'une fraude est détectée** et **voir chaque matin les
paiements et les fraudes de la veille**. L'énoncé le rappelle : l'essentiel est le pipeline de
données, pas l'algorithme. Le modèle est donc simple ; le soin porte sur la collecte, la
qualité, la confidentialité, l'orchestration et l'exploitation.

Les documents de conception sont dans `../livrables/aia3/` (architecture justifiée, qualité,
sécurité, exploitation, backlog, couverture et limites).

## En bref

| Besoin | Fraîcheur | Mode | Composants |
|---|---|---|---|
| Alerte dès qu'une fraude est détectée | quelques secondes | **flux** | producteur → file → consommateur → notification |
| Rapport du matin (veille) | une fois par jour, 07:00 | **lot** | DAG Airflow → couche argent → rapport HTML/CSV |
| Réentraînement du modèle | hebdomadaire | **lot** | DAG Airflow → MLflow → barrière de qualité → promotion |
| Rejeu de l'historique | à la demande | **lot** | `retraitement` depuis la couche bronze |

```
API temps réel ─┐                          ┌─ bronze (masqué, immuable)
                ├─ producteur ─ file ─ consommateur ─ argent (typé, enrichi, noté) ─ or (alertes, rapports)
Jeu fraudTest ──┘  valide+masque  │  note+stocke+notifie
                                  └─ DLQ (lignes rejetées) ─ archivée dans meta_rejets
```

## Arborescence

```
fraude/
├── detection_fraude/        Code (Python 3.12), commenté en français
│   ├── configuration.py     Configuration par environnement, lecture des secrets
│   ├── journal.py           Journaux d'audit JSON (une ligne par événement)
│   ├── schema.py            Contrats de données (brut, masqué) et validation
│   ├── masquage.py          Masquage dès l'ingestion (HMAC de la carte, âge, distance…)
│   ├── file_attente.py      File de messages : SQLite (local) / Kafka (production)
│   ├── producteur.py        API ou jeu → relances → validation → masquage → file / DLQ
│   ├── consommateur.py      File → bronze / argent / or → alertes
│   ├── entrepot.py          Tout le SQL (SQLite et PostgreSQL), idempotence, lignage
│   ├── fonctionnalites.py   Caractéristiques identiques à l'entraînement et au scoring
│   ├── entrainement.py      Pipeline scikit-learn, suivi MLflow, barrière de qualité
│   ├── modele.py            Export versionné, intégrité SHA-256, rechargement à chaud
│   ├── notification.py      Journal, fichier, SMTP (TLS), webhook (HTTPS), boîte d'envoi
│   ├── rapport.py           Rapport du matin (HTML + CSV + couche or)
│   ├── retraitement.py      Recalcul de l'historique depuis le bronze
│   ├── supervision.py       Contrôles de santé et seuils d'alerte (SLA)
│   ├── finops.py            Estimation des coûts
│   ├── taches_airflow.py    Fonctions appelées par les DAG (testables sans Airflow)
│   ├── local.py             Démonstration sans Docker
│   ├── jeu_donnees.py, telechargement.py
├── dags/                    3 DAG : rapport du matin, réentraînement hebdomadaire, supervision
├── config/                  base.yaml, dev.yaml, prod.yaml (aucun secret)
├── docker/                  Dockerfile (application, Airflow, MLflow), script PostgreSQL
├── docker-compose.yml       Production locale
├── scripts/                 initialiser_secrets.py
├── tests/                   Tests pytest (aucun appel réseau)
├── stubs/                   Types d'Airflow pour mypy (Airflow n'est installé que dans Docker)
├── MUTANTS_EQUIVALENTS.md   Mutants qui survivent sans changer aucun comportement, avec la preuve
└── data/                    Reçoit fraudTest.csv (non versionné)
```

## Démarrage rapide sans Docker (mode local)

Prérequis : Python 3.12 et le jeu `fraudTest.csv` (150 Mo), fourni avec l'énoncé et non
redistribué ici : le placer dans `data/`, ou renseigner son adresse dans `config/base.yaml` pour
le téléchargement automatique (voir `data/README.md`). Le mode `--source api` exige de même
l'adresse de l'API (clé `source.api_url` ou variable `FRAUDE_API_URL`).

```bash
cd fraude
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell : .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

python -m detection_fraude.local preparer       # télécharge le jeu (SHA-256 vérifié) et entraîne le modèle
python -m detection_fraude.local demo --reinitialiser
```

La démonstration rejoue le 21 décembre 2020 du jeu (6 390 transactions, période de test du
modèle), injecte **6 lignes corrompues** (elles finissent en DLQ), notifie chaque fraude
détectée, produit le rapport du matin et contrôle la santé du pipeline. Tout est écrit dans
`donnees/` : file, entrepôt SQLite, alertes (`alertes.jsonl`), journal d'audit
(`journaux/audit.jsonl`), rapports (`rapports/`), modèles (`modeles/`), suivi MLflow.

Variantes :

```bash
# Interroger l'API réelle (3 appels espacés de 13 s), avec 2 pannes réseau simulées au départ
python -m detection_fraude.local demo --source api --appels 3 --pannes 2

# Les deux processus séparément, dans deux terminaux (comme en production)
python -m detection_fraude.consommateur
python -m detection_fraude.producteur --source jeu --jour 2020-12-21 --corruptions 6
python -m detection_fraude.producteur --source api                # boucle infinie, 1 appel/min

# Rapport du matin pour un jour, retraitement, supervision, coûts
python -m detection_fraude.rapport --jour 2020-12-21
python -m detection_fraude.retraitement --depuis 2020-12-21 --jusqu 2020-12-22 --rapports
python -m detection_fraude.supervision --sans-flux
python -m detection_fraude.finops --tx-par-jour 1440

# Réentraînement manuel (le DAG hebdomadaire fait la même chose)
python -m detection_fraude.entrainement --promouvoir
```

Pour consulter le suivi des entraînements : `pip install mlflow` puis
`mlflow ui --backend-store-uri sqlite:///donnees/mlflow.db`.

## Tests et qualité du code

```bash
ruff check . && ruff format --check .      # PEP 8, 99 colonnes
mypy --strict dags detection_fraude scripts # typage strict (les stubs d'Airflow sont dans stubs/)
pytest -W error::DeprecationWarning -W error::PendingDeprecationWarning   # tous les tests, sans réseau
```

SQLAlchemy est borné à `< 2.1` (`requirements.txt`) : la version 2.1 déprécie la stratégie
« noload » que MLflow 3.17 emploie encore, ce qui fait échouer les tests lancés avec les
avertissements de dépréciation en erreurs. La production tourne déjà en SQLAlchemy 1.4.54
(contraintes d'Airflow 2.10.5).

Les tests couvrent le schéma, la file des rejets, le masquage (y compris une recherche des
données personnelles dans **tous** les fichiers produits), l'idempotence, l'atomicité, les
relances, les notifications, le rapport, le retraitement, les DAG (structure, dépendances,
relances, SLA), la configuration par environnement et le parcours complet.

La solidité des tests est mesurée par mutation (`mutmut`) : chaque petite modification du code
doit faire échouer au moins un test. Les seuls mutants qui survivent ne changent aucun
comportement ; ils sont nommés, avec leur preuve, dans `MUTANTS_EQUIVALENTS.md`, et les faits
de bibliothèque sur lesquels reposent ces preuves sont contrôlés par
`tests/test_equivalent_mutants_premises.py`.

## Production locale (Docker)

Services : Redpanda (compatible Kafka), PostgreSQL, MLflow, Airflow (planificateur, serveur web),
producteur, consommateur, plus une tâche d'amorçage (jeu de données et premier modèle).
Avant le premier lancement, renseigner dans `config/base.yaml` l'adresse du jeu
(`source.jeu_url`) et celle de l'API (`source.api_url`), fournies avec l'énoncé : la tâche
d'amorçage télécharge le jeu, et le producteur interroge l'API.

```bash
cd fraude
python scripts/initialiser_secrets.py     # crée secrets/ (jamais versionné) ; valeurs non affichées
cp .env.example .env                      # facultatif : ports, SMTP (hôte, expéditeur, destinataires)
docker compose up -d --build
docker compose ps                         # tous les services « healthy » après quelques minutes
```

| Interface | Adresse (machine hôte) | Identifiants |
|---|---|---|
| Airflow | <http://127.0.0.1:18080> | `admin` / contenu de `secrets/airflow_admin_motdepasse.txt` |
| MLflow | <http://127.0.0.1:15000> | aucun (accès local uniquement) |
| Kafka (externe) | `127.0.0.1:19092` | aucun (accès local uniquement) |
| PostgreSQL | `127.0.0.1:15432` | rôle `fraude` (ou `postgres` dans le conteneur), mot de passe dans `secrets/pg_fraude_motdepasse.txt` |

Ordre de démarrage garanti par les dépendances et les contrôles de santé : PostgreSQL et Redpanda →
MLflow → `airflow-init` (migration de la base, compte administrateur) → `entrainement-initial`
(téléchargement du jeu et premier modèle) → producteur et consommateur → planificateur et serveur
web Airflow. Les trois DAG sont actifs dès la création : **aucun lancement manuel** n'est
nécessaire.

Commandes utiles :

```bash
docker compose logs -f consommateur producteur            # journaux JSON du flux
docker compose exec consommateur python -m detection_fraude.supervision --sans-flux
docker compose exec producteur python -m detection_fraude.producteur --source jeu --jour 2020-12-21 --corruptions 6
docker compose exec producteur python -m detection_fraude.producteur --source api --nombre 1 --pannes 2   # relances
docker compose exec airflow-scheduler airflow dags list
mkdir -p sorties/rapports && docker compose cp airflow-scheduler:/donnees/rapports/. ./sorties/rapports
docker compose down            # arrêt (les volumes sont conservés) ; ajouter -v pour tout effacer
```

Pour que le DAG du rapport du matin ait des données « de la veille » alors que le jeu date de
2020, `--rebaser-sur AAAA-MM-JJ` recale un jour rejoué sur la date voulue (option de
démonstration ; choisir un jour du jeu de même jour de semaine).

Ports, ressources et rôle de chaque service : voir `livrables/aia3/04_exploitation.md`.

## Configuration

Le fichier de configuration est choisi par `FRAUDE_ENV` (`dev` par défaut, `prod`). Seuls les
fichiers `config/*.yaml` (sans secret) et l'environnement changent d'un environnement à l'autre ;
le code est identique.

| Variable | Rôle | Secret ? |
|---|---|---|
| `FRAUDE_ENV` | `dev` ou `prod` | non |
| `FRAUDE_DONNEES` | dossier des données générées (défaut `donnees/`) | non |
| `FRAUDE_SEL_HACHAGE` ou `_FICHIER` | clé HMAC du hachage des cartes (obligatoire en `prod`) | **oui** |
| `FRAUDE_DSN_ENTREPOT` ou `_FICHIER` | chaîne de connexion de l'entrepôt (obligatoire en `prod`) | **oui** |
| `FRAUDE_KAFKA_SERVEURS` | serveurs Kafka | non |
| `FRAUDE_KAFKA_PROTOCOLE_SECURITE`, `_SASL_MECANISME`, `_UTILISATEUR`, `_MOT_DE_PASSE`, `_CA` | sécurité Kafka | mot de passe : **oui** |
| `FRAUDE_SMTP_HOTE`, `_PORT`, `_EXPEDITEUR`, `_DESTINATAIRES` | courriel d'alerte | non |
| `FRAUDE_SMTP_UTILISATEUR`, `FRAUDE_SMTP_MOT_DE_PASSE` (ou `_FICHIER`) | identifiants SMTP | **oui** |
| `FRAUDE_WEBHOOK_URL` ou `_FICHIER` | URL du webhook (contient souvent un jeton) | **oui** |
| `MLFLOW_TRACKING_URI` | serveur de suivi | non |
| `FRAUDE_JEU_CHEMIN`, `FRAUDE_API_URL`, `FRAUDE_NIVEAU_JOURNAL` | chemin du jeu, URL de l'API, niveau de journal | non |

La variante `_FICHIER` lit le secret dans un fichier monté (secrets Docker ou Kubernetes) et
prime sur la variable.

## Ce que fait chaque garantie

* **Aucune donnée personnelle après l'ingestion** : nom, prénom, rue, ville, code postal,
  métier, genre et coordonnées du domicile sont supprimés ; le numéro de carte devient une
  empreinte HMAC à clé secrète plus ses quatre derniers chiffres ; la date de naissance devient
  un âge. Détails et limites : `livrables/aia3/03_securite_conformite.md`.
* **Aucune ligne corrompue ne bloque le flux** : elle va dans `transactions.rejets` (DLQ) avec
  la liste de ses erreurs, sans donnée identifiante.
* **Aucun doublon, aucune alerte perdue** : écritures idempotentes (clé `trans_num`), position
  de la file validée après le commit de la base, alertes écrites dans la même transaction que la
  transaction notée puis envoyées canal par canal (« boîte d'envoi »).
* **Traçabilité** : chaque ligne de la couche argent porte la version du modèle, la version des
  règles et le lot ; la table `meta_lignage` consigne chaque exécution d'étape.

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
