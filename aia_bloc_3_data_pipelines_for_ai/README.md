# Bloc 3 AIA : pipelines de données pour l'IA

## Détection de fraude en temps réel : alerter tout de suite, rendre compte chaque matin

Un pipeline de données complet autour d'un modèle de détection de fraude sur des paiements par carte : collecte depuis une API temps réel ou par rejeu d'un jeu étiqueté, validation de schéma, masquage des données personnelles dès l'ingestion, file de messages, notation, notification de chaque fraude détectée, entrepôt en couches, rapport du matin, réentraînement hebdomadaire et supervision orchestrés par Airflow.

Le code est dans [`fraude/`](fraude/), les documents de conception dans [`livrables/aia3/`](livrables/aia3/). Chaque chiffre de cette page renvoie au document qui le consigne.

Projet réalisé pour le bloc 3 de la certification Architecte en intelligence artificielle (RNCP38777, renouvelée sous le RNCP41993) : « Concevoir et mettre en œuvre des pipelines de données pour l'IA ».

![Schéma de l'architecture : API temps réel et jeu de rejeu, producteur avec relances, validation et masquage, sujet Kafka des transactions valides, file des rejets, consommateur, notifications, entrepôt PostgreSQL bronze, argent, or, trois DAG Airflow, modèles avec pointeur ACTUEL et suivi MLflow](figures/schema_architecture.png)

---

## 1. Problématique

L'équipe antifraude exprime deux besoins dont l'exigence de fraîcheur n'est pas la même ([architecture, § 1](livrables/aia3/01_architecture.md)) :

| Besoin | Fraîcheur | Mode retenu |
|---|---|---|
| Être notifié dès qu'une fraude est détectée | quelques secondes | **flux** : producteur, file, consommateur, notification |
| Voir chaque matin les paiements et les fraudes de la veille | une fois par jour, 07:00 | **lot** : DAG Airflow, couche argent, rapport HTML et CSV |

Le cadrage le dit : la partie la plus importante est le pipeline de données, pas l'algorithme. Le modèle est donc volontairement simple et remplaçable ; le soin porte sur la collecte, la qualité, la confidentialité, l'orchestration et l'exploitation.

## 2. Données

**Aucune donnée n'est versionnée dans ce dossier, et aucune n'est redistribuée.** Le pipeline s'appuie sur deux sources fournies avec l'énoncé du projet :

- **un jeu étiqueté de transactions synthétiques**, `fraudTest.csv` : 555 719 lignes, 150 354 339 octets, qui couvrent 194 jours ([architecture, § 4.1](livrables/aia3/01_architecture.md) ; [`fraude/config/base.yaml`](fraude/config/base.yaml)). Il sert à l'entraînement et au rejeu d'une journée ;
- **une API de démonstration** qui renvoie une transaction « courante » par appel, cinq appels par minute au plus ; le pipeline l'interroge une fois par minute ([architecture, § 4.1](livrables/aia3/01_architecture.md)).

**Obtenir le jeu.** Le fichier accompagne l'énoncé du projet ; il n'est pas publié ici. Deux façons de le mettre en place ([`fraude/data/README.md`](fraude/data/README.md)) :

1. le copier dans `fraude/data/fraudTest.csv`, ou indiquer son chemin par la variable `FRAUDE_JEU_CHEMIN` ;
2. renseigner son adresse dans [`fraude/config/base.yaml`](fraude/config/base.yaml) (clé `source.jeu_url`), puis lancer, depuis `fraude/` :

```bash
python -m detection_fraude.telechargement
```

La commande télécharge le fichier dans `fraude/data/` et vérifie sa taille et son empreinte SHA-256, inscrites dans `config/base.yaml`. Le fichier est exclu de Git par le `.gitignore` du dossier.

**Adresse de l'API.** Elle n'est pas publiée non plus : la renseigner dans `config/base.yaml` (clé `source.api_url`) ou par la variable `FRAUDE_API_URL`. Les valeurs livrées pointent vers le domaine réservé `exemple.invalid`, qui ne répond jamais. Le rejeu du jeu, les tests et la démonstration locale n'en ont pas besoin.

Une transaction de l'API contient 21 champs, dont des noms, une adresse, une date de naissance et un numéro de carte. Ces champs sont supprimés, hachés ou généralisés **avant** toute écriture (section 3) ([sécurité et conformité, § 1 et § 2](livrables/aia3/03_securite_conformite.md)).

## 3. Méthode

### Le chemin chaud : flux, quelques secondes

- **Collecte** : producteur Python, relances à attente exponentielle avec gigue (1 s, 2 s, 4 s, 8 s ; 5 tentatives), respect de `Retry-After` ; un cycle en échec est abandonné sans arrêter le processus ([architecture, § 4.3](livrables/aia3/01_architecture.md)).
- **Validation de schéma** : contrôles étagés à l'entrée et contrat de la file à la sortie du masquage ; une ligne invalide part dans une **file des rejets** (`transactions.rejets`) avec la liste de ses erreurs, sans donnée identifiante, et le lot continue ([pipeline et qualité, § 5 et § 6](livrables/aia3/02_pipeline_et_qualite.md)).
- **Masquage dès l'ingestion**, en liste blanche : nom, prénom et rue supprimés ; numéro de carte remplacé par une empreinte HMAC-SHA256 à clé secrète et ses quatre derniers chiffres ; date de naissance remplacée par un âge ; position du domicile remplacée par une distance au commerçant ([sécurité et conformité, § 2](livrables/aia3/03_securite_conformite.md)).
- **File de messages** : Kafka (Redpanda dans la production locale), trois partitions, producteur idempotent ; SQLite en mode local ([architecture, § 2 et § 4.3](livrables/aia3/01_architecture.md)).
- **Consommateur idempotent** : clé `trans_num`, `INSERT … ON CONFLICT DO NOTHING`, position de la file validée **après** le commit de la base ; une relivraison ne duplique rien et ne renotifie personne ([architecture, § 4.3 et § 4.4](livrables/aia3/01_architecture.md)).
- **Notification** : alerte écrite dans la même transaction que la ligne notée, puis envoyée canal par canal (journal, fichier, courriel en TLS, webhook en HTTPS) ([`fraude/README.md`](fraude/README.md)).

### Le chemin froid : lots orchestrés par Airflow

- **Entrepôt en couches** : bronze (masqué, immuable), argent (typé, enrichi, noté), or (alertes, rapports), plus une couche de métadonnées (rejets archivés, lignage de chaque exécution) ([pipeline et qualité, § 1 et § 8](livrables/aia3/02_pipeline_et_qualite.md)).
- **Trois DAG**, actifs dès leur création, sans lancement manuel : rapport du matin à 07:00 pour la veille, réentraînement le dimanche à 03:00, supervision toutes les 10 minutes ; relances, délais maximaux et SLA déclarés ([architecture, § 2](livrables/aia3/01_architecture.md) ; [pipeline et qualité, § 4](livrables/aia3/02_pipeline_et_qualite.md)).
- **Retraitement de l'historique** depuis la couche bronze, idempotent, sans rappeler l'API ni renotifier ([pipeline et qualité, § 3](livrables/aia3/02_pipeline_et_qualite.md)).
- **Caractéristiques** calculées en deux versions, vectorisée pour le lot et incrémentale pour le flux, dont l'égalité est vérifiée par un test ([pipeline et qualité, § 2](livrables/aia3/02_pipeline_et_qualite.md)).

### Le modèle : simple, tracé, protégé par une barrière

- Gradient boosting par histogrammes, comparé à une régression logistique ; **découpage chronologique** 70 % / 15 % / 15 %, jamais aléatoire sur une série temporelle ([architecture, § 1](livrables/aia3/01_architecture.md) ; [pipeline et qualité, § 2](livrables/aia3/02_pipeline_et_qualite.md)).
- **Seuil choisi sur la validation** : le meilleur rappel avec une précision d'au moins 0,90, parce que chaque alerte coûte du temps d'analyste.
- **Barrière de qualité** avant toute promotion : un candidat est refusé si sa PR-AUC de test est sous 0,30 ou inférieure de plus de 0,02 à celle du modèle en service ([`fraude/config/base.yaml`](fraude/config/base.yaml) ; [pipeline et qualité, § 4.2](livrables/aia3/02_pipeline_et_qualite.md)). Un refus est un échec définitif, non relancé, qui déclenche une alerte ; l'ancien modèle reste en service.
- **Traçabilité** : graine, empreinte du jeu, versions, hyperparamètres et métriques consignés dans MLflow ; modèle exporté avec son empreinte SHA-256, vérifiée avant chargement ; promotion par bascule atomique du pointeur `ACTUEL`, que le consommateur détecte pour recharger le modèle sans redémarrer ([pipeline et qualité, § 2 et § 4.2](livrables/aia3/02_pipeline_et_qualite.md) ; [`fraude/README.md`](fraude/README.md)).

### Exploitation

- **Supervision** : neuf seuils, dont la notification d'une fraude en 60 s au plus, aucune transaction depuis 15 min, plus de 2 % de rejets sur une heure, une alerte non envoyée depuis 10 min ([exploitation, § 2](livrables/aia3/04_exploitation.md)).
- **Journaux d'audit** JSON, une ligne par événement ([exploitation, § 3](livrables/aia3/04_exploitation.md)).
- **Secrets** hors du code et des fichiers de configuration : variables d'environnement ou fichiers montés ; un test le vérifie ([sécurité et conformité, § 6](livrables/aia3/03_securite_conformite.md)).
- **Coûts** : consommations mesurées multipliées par des prix unitaires hypothétiques ([exploitation, § 4](livrables/aia3/04_exploitation.md)).
- **Conduite** : trois sprints, récits, critères d'acceptation et backlog restant ([backlog et sprints](livrables/aia3/05_backlog_sprints.md)).

## 4. Résultats chiffrés

Jeu synthétique : **ces chiffres démontrent la méthode et la chaîne, pas un taux de détection sur des paiements réels.**

### Modèle (gradient boosting, retenu)

| Mesure | Validation | Test | Source |
|---|---:|---:|---|
| Lignes (fraudes) | 83 358 (283) | 83 358 (86) | [pipeline et qualité, § 2](livrables/aia3/02_pipeline_et_qualite.md) |
| PR-AUC | 0,8236 | 0,7899 | idem |
| ROC-AUC | — | 0,9959 | idem |
| Seuil retenu | 0,4150 | | idem |
| Au seuil : précision / rappel | — | 0,707 / 0,814 (99 alertes, 70 vraies sur 86 fraudes) | idem |
| Référence : régression logistique, PR-AUC | 0,5003 | 0,4085 | idem |

L'entraînement porte sur 389 003 lignes (1 776 fraudes). La précision visée de 0,90, atteinte sur la validation, retombe à 0,707 sur le test au même seuil : le taux de fraude du jeu s'effondre en fin de période (aucune fraude après le 22/12/2020). La barrière de qualité suit la PR-AUC, moins sensible à ce décalage ([pipeline et qualité, § 2](livrables/aia3/02_pipeline_et_qualite.md)).

### Pipeline, mode local (rejeu du 21 décembre 2020)

| Mesure | Résultat | Source |
|---|---|---|
| Lignes lues / en file des rejets / traitées | 6 390 / 6 (corruptions injectées) / 6 384 | [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md) ; [pipeline et qualité, § 6](livrables/aia3/02_pipeline_et_qualite.md) |
| Alertes envoyées | 10, dont 7 vraies fraudes | [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md) |
| Supervision | aucune anomalie | idem |
| Traitement de la journée | 4,4 à 6,3 s selon l'exécution, soit 1 000 à 1 450 transactions par seconde ; environ 250 fois le pic du jeu (4 par seconde) | [architecture, § 4.1 et § 4.2](livrables/aia3/01_architecture.md) |
| Délai entre détection et notification | 0,05 à 0,54 s (moyenne 0,30 s, 10 alertes) | [exploitation, § 2.1](livrables/aia3/04_exploitation.md) |

### Production locale (Docker Compose, une machine)

| Mesure | Résultat | Source |
|---|---|---|
| Démarrage depuis zéro | 9 services sains ou terminés sans erreur en 1 min 36 s (images déjà en cache) | [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md) |
| Même rejeu sur Kafka et PostgreSQL | les mêmes 10 transactions en alerte qu'en mode local ; 6 rejets archivés | idem |
| Airflow 2.10.5 | 3 DAG actifs, aucune erreur d'import | idem |
| DAG du rapport du matin | 4 tâches réussies ; rapport HTML et CSV (3 827 paiements, 3 rejets, 2 fraudes détectées) | idem |
| DAG de réentraînement | 5 tâches réussies, entraînement de 34 à 42 s, barrière franchie, modèle promu | idem |
| Relances sur l'API réelle | 2 pannes simulées, 2 relances, puis succès | idem |
| Délai entre détection et notification | 0,6 à 4,5 s ; moyennes de 1,1 à 2,3 s sur quatre exécutions | [exploitation, § 2.1](livrables/aia3/04_exploitation.md) |
| Coût mensuel estimé (1 440 transactions par jour, prix hypothétiques) | environ 158,79 €, à plus de 99,9 % du calcul permanent | [exploitation, § 4.2 et § 4.3](livrables/aia3/04_exploitation.md) |

### Qualité du code (19 octobre 2026)

| Mesure | Résultat | Source |
|---|---|---|
| Tests (`pytest`, avertissements de dépréciation traités comme des erreurs) | 1 621 tests : 1 620 réussis, 1 ignoré (il exige Airflow installé), aucun appel réseau | [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md) |
| Analyse statique | `ruff` et `mypy --strict` sans erreur (25 fichiers) | idem |
| Tests de mutation (`mutmut`, tout le code) | 9 061 mutants tués sur 9 148 ; les 87 autres ne changent aucun comportement et sont nommés avec leur preuve | [`fraude/MUTANTS_EQUIVALENTS.md`](fraude/MUTANTS_EQUIVALENTS.md) |
| Confidentialité | aucun nom, rue, ville, métier, date de naissance ni numéro de carte dans aucun fichier produit (test dédié) | [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md) |

## 5. Reproduire

Prérequis : Python 3.12 et le jeu `fraudTest.csv` (150 Mo), mis en place comme indiqué à la section 2. Toutes les commandes se lancent depuis `fraude/`.

**Mode local, sans Docker** (file et entrepôt SQLite) :

```bash
python -m venv .venv
source .venv/bin/activate            # Windows PowerShell : .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt

python -m detection_fraude.local preparer             # met en place le jeu s'il manque, puis entraîne le modèle
python -m detection_fraude.local demo --reinitialiser # rejoue le 21/12/2020 avec 6 lignes corrompues
```

Tout est écrit dans `donnees/` : file, entrepôt, alertes, journal d'audit, rapports, modèles, suivi MLflow.

**Tests et qualité** (aucun appel réseau, aucun jeu de données requis) :

```bash
ruff check . && ruff format --check .
mypy --strict dags detection_fraude scripts
pytest -W error::DeprecationWarning -W error::PendingDeprecationWarning
```

SQLAlchemy est borné sous 2.1 : la version 2.1 déprécie une stratégie de chargement que MLflow emploie encore ([`fraude/README.md`](fraude/README.md)).

**Production locale** (Docker Compose : Redpanda, PostgreSQL, MLflow, Airflow, producteur, consommateur). Renseigner d'abord, dans `config/base.yaml`, l'adresse du jeu et celle de l'API (section 2) : la tâche d'amorçage télécharge le jeu, et le producteur interroge l'API.

```bash
python scripts/initialiser_secrets.py     # crée secrets/, jamais versionné ; valeurs non affichées
docker compose up -d --build
docker compose ps                         # services sains après quelques minutes
```

Les interfaces n'écoutent que sur la machine hôte (127.0.0.1). Ports, identifiants et commandes utiles : [`fraude/README.md`](fraude/README.md) ; ressources et procédures d'incident : [exploitation](livrables/aia3/04_exploitation.md).

## 6. Limites

Ce que le projet ne démontre pas, dit tel quel :

- **Données synthétiques** : le RGPD et PCI DSS sont appliqués par transposition ; les numéros d'exigence PCI DSS sont à faire confirmer par un responsable de la sécurité, et l'analyse d'impact comme le registre des traitements restent à rédiger avec un DPO ([couverture et limites, § 3](livrables/aia3/07_couverture_et_limites.md)).
- **La pseudonymisation n'est pas une anonymisation** : l'empreinte stable de la carte, recoupée avec l'âge, l'État, l'horodatage et le commerçant, pourrait permettre une réidentification ; le jeu masqué reste soumis au RGPD ([sécurité et conformité, § 2](livrables/aia3/03_securite_conformite.md)).
- **« Production » veut dire ici une machine unique sous Docker Compose** : TLS entre les services non activé, courtier unique (facteur de réplication 1), une seule instance de consommateur, pas d'intégration continue ([couverture et limites, § 4](livrables/aia3/07_couverture_et_limites.md) ; [architecture, § 4.5](livrables/aia3/01_architecture.md)).
- **Notifications** : les mesures consignées portent sur le journal et le fichier d'alertes ; les canaux courriel et webhook sont implémentés et testés sans serveur réel ([`fraude/config/dev.yaml`](fraude/config/dev.yaml) ; [couverture et limites, § 2](livrables/aia3/07_couverture_et_limites.md)).
- **Lignes rejetées** : la file des rejets sert à compter et à diagnostiquer ; leur contenu y est masqué, et aucune procédure de correction ni de réinjection n'est outillée ([pipeline et qualité, § 6](livrables/aia3/02_pipeline_et_qualite.md)).
- **La dérive des données et du modèle n'est pas surveillée** : elle est au backlog ([backlog et sprints](livrables/aia3/05_backlog_sprints.md), backlog restant, rang 5).
- **Hypothèses** : prix unitaires des coûts, durées de conservation (bronze 90 jours, argent et or 13 mois) ; « la veille » est un jour UTC et la devise des montants n'est pas fournie par la source ([couverture et limites, § 3](livrables/aia3/07_couverture_et_limites.md)).
- **Le test qui exige Airflow est ignoré sur un poste sans Airflow** ; la même vérification a été faite dans l'image Docker d'Airflow ([couverture et limites, § 4](livrables/aia3/07_couverture_et_limites.md)).

## 7. Organisation du dossier

```text
.
|-- README.md
|-- .gitignore                       exclut le jeu, les données produites et les secrets
|-- figures/schema_architecture.png   schéma de l'architecture
|-- livrables/aia3/                   01 architecture, 02 pipeline et qualité, 03 sécurité et conformité,
|                                     04 exploitation, 05 backlog et sprints, 07 couverture et limites
`-- fraude/
    |-- detection_fraude/             producteur, consommateur, masquage, schéma, entrepôt, modèle,
    |                                 notifications, rapport, retraitement, supervision, coûts
    |-- dags/                         rapport du matin, réentraînement hebdomadaire, supervision
    |-- config/                       base.yaml, dev.yaml, prod.yaml (aucun secret)
    |-- docker/, docker-compose.yml   production locale
    |-- tests/                        suite pytest, sans réseau
    |-- MUTANTS_EQUIVALENTS.md        mutants survivants, nommés avec leur preuve
    `-- data/                         reçoit fraudTest.csv (non versionné, non redistribué)
```
