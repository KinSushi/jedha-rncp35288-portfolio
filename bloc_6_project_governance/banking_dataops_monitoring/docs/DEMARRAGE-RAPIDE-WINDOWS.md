# Démarrage rapide Windows : pipeline Python sans Docker, et Power BI

Guide pas à pas pour exécuter le pipeline sans Docker contre une base PostgreSQL (locale ou
Neon, gratuite), puis brancher Power BI Desktop. Pour la pile Docker, voir `DEMARRAGE-WINDOWS.md`.

Toutes les commandes sont en PowerShell, depuis le dossier décompressé de l'archive de code
(`banking_dataops_monitoring`). Chaque étape indique le résultat attendu : si le résultat attendu n'est
pas obtenu, il vaut mieux s'arrêter que d'enchaîner.

---

## Vue d'ensemble

| Étape | Quoi | Durée |
|---|---|---|
| 1 | Environnement Python | 5 min |
| 2 | Une base PostgreSQL : Neon (gratuite) ou locale | 5 min |
| 3 | Pointer le pipeline sur cette base | 2 min |
| 4 | Lancer le pipeline, le modèle et la vérification | 1 à 2 min |
| 5 | Connecter Power BI Desktop | 10 min |
| 6 | Rejouer la démonstration d'anomalies | 2 min |

Le pipeline n'a pas besoin de Docker : il crée lui-même le schéma, charge les données, construit la
couche Gold, entraîne le modèle, note les transactions et crée les 8 vues du schéma en étoile. Résultat attendu
à la fin : `DEMO READY - 15/15`.

---

## 1. Environnement Python

```powershell
python --version
```

Attendu : `Python 3.12` ou plus récent. Sinon, installer Python depuis python.org en cochant
« Add Python to PATH ».

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install -e .
```

Si PowerShell refuse d'exécuter le script d'activation :

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Attendu : l'invite commence par `(.venv)`.

---

## 2. La base PostgreSQL

Option Neon (base managée gratuite).

1. Créer un compte sur [neon.tech](https://neon.tech), puis un projet dans une région Europe (latence).
2. Dans Dashboard → Connection string, copier l'URI. Elle ressemble à :

```
postgresql://UTILISATEUR:MOTDEPASSE@ep-xxxx-xxxx.eu-central-1.aws.neon.tech/DBNAME?sslmode=require
```

Noter ses morceaux : `UTILISATEUR`, `MOTDEPASSE`, l'hôte `ep-...neon.tech`, `DBNAME`.

Neon plutôt que Supabase : Supabase met le projet en pause après 7 jours d'inactivité, avec un réveil
manuel de plusieurs minutes ; Neon reprend à froid en moins d'une seconde.

Option locale. Un PostgreSQL 16 sur la machine, ou le service `postgres` de `docker-compose.yml`
(`docker compose up -d postgres`, port 5432 de l'hôte). Utilisateur `dataops`, mot de passe `dataops`,
base `banking_dataops` : ce sont les identifiants de développement d'une base locale synthétique.

---

## 3. Pointer le pipeline sur la base

Dans la même fenêtre PowerShell (les variables ne survivent pas à sa fermeture : aucun secret n'est
écrit sur disque) :

```powershell
$env:BANKING_DATAOPS_DATABASE_HOST     = "ep-xxxx-xxxx.eu-central-1.aws.neon.tech"
$env:BANKING_DATAOPS_DATABASE_PORT     = "5432"
$env:BANKING_DATAOPS_DATABASE_NAME     = "DBNAME"
$env:BANKING_DATAOPS_DATABASE_USER     = "UTILISATEUR"
$env:BANKING_DATAOPS_DATABASE_PASSWORD = "MOTDEPASSE"
$env:BANKING_DATAOPS_DATABASE_SSLMODE  = "require"
$env:PYTHONPATH                        = "src"
```

(Pour une base locale : `localhost`, `banking_dataops`, `dataops`, `dataops`, et pas de variable `SSLMODE`.)

Tester la connexion avant de lancer quoi que ce soit :

```powershell
python -c "import psycopg,os; psycopg.connect(f\"host={os.environ['BANKING_DATAOPS_DATABASE_HOST']} port=5432 dbname={os.environ['BANKING_DATAOPS_DATABASE_NAME']} user={os.environ['BANKING_DATAOPS_DATABASE_USER']} password={os.environ['BANKING_DATAOPS_DATABASE_PASSWORD']} sslmode=require\").close(); print('connexion OK')"
```

Attendu : `connexion OK`. Sinon, ne pas continuer : revérifier les valeurs.

---

## 4. Lancer le pipeline

```powershell
python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train
python -m banking_dataops.ml.predict
python -m banking_dataops.powerbi_views
python scripts\smoke_check.py
```

Pour un lot daté du jour et un verdict `PASS` (7 sur 7), ajouter `--generated-at today` à la première
commande ; par défaut le lot est daté du 27 juillet 2026 et, depuis le 25 septembre 2026, le contrôle de
fraîcheur CTRL-007 donne un verdict `WARN` (ADR-0009).

L'étape `ml.predict` est indispensable : `run_pipeline.py --train` *entraîne* le modèle mais ne
*note* pas les transactions. Sans elle, `gold.predictions` reste vide et le smoke check échoue.

Attendu, dans l'ordre :

- `Pipeline completed successfully (anchor ...)` avec 5 lignes (bronze, cleaning, silver, gold, ml_train) ;
- `1500 transaction(s) scored into gold.predictions.` ;
- 8 lignes `ok vw_...` ;
- `DEMO READY - 15 checks passed`.

Durée totale : 1 à 2 minutes (l'entraînement du modèle prend quelques dizaines de secondes).

---

## 5. Connecter Power BI Desktop

1. Obtenir les données → Base de données PostgreSQL.
2. Renseigner :

   | Champ | Valeur |
   |---|---|
   | Serveur | `ep-xxxx-xxxx.eu-central-1.aws.neon.tech:5432` (port inclus), ou `localhost:5432` |
   | Base de données | `DBNAME`, ou `banking_dataops` en local |
   | Mode | Import |

3. Authentification : Base de données, avec l'utilisateur et le mot de passe de l'étape 3.
4. Niveau de confidentialité : Organizational.

Charger ces 8 vues (pas les tables brutes) :

```
vw_dim_date · vw_dim_customer · vw_dim_account · vw_dim_control
vw_fact_transactions · vw_fact_quality_results · vw_fact_incidents
vw_publication_verdict
```

Puis poser les relations et les mesures DAX : [`powerbi/connexion-cloud-neon-supabase.md`](powerbi/connexion-cloud-neon-supabase.md)
(sections « Relations du modèle » et suivantes).

Brancher le rapport livré (`docs/powerbi/banking_dataops_monitoring.pbix`, non publié dans ce dépôt) sur une autre base :
le fichier est configuré sur une base PostgreSQL managée (Neon). Dans Power BI Desktop : **Transformer les
données → Paramètres de la source de données → Changer la source**, puis saisir le serveur (`localhost:5432`
pour la pile Docker, ou le port défini par `POSTGRES_PORT`) et la base (`banking_dataops`), identifiants
`dataops` / `dataops` ; enfin Actualiser. Les huit vues doivent exister dans la base visée (étape 4, ou
pile Docker démarrée).

> Import plutôt que DirectQuery : quelques milliers de lignes seulement. Une fois importé, le rapport
> fonctionne hors ligne.

---

## 6. Rejouer la démonstration d'anomalies

Le scénario de démonstration fonctionne aussi contre cette base :

```powershell
python -m banking_dataops.demo_scenario inject
python -m banking_dataops.quality_checks     # le verdict bascule en FAIL
python -m banking_dataops.demo_scenario reset
python -m banking_dataops.quality_checks     # retour à PASS (ou WARN, selon l'ancre)
```

Actualiser Power BI entre chaque étape pour voir la bascule.

---

## En cas de problème

| Symptôme | Cause | Solution |
|---|---|---|
| `connexion OK` ne s'affiche pas (étape 3) | identifiants erronés | recopier l'URI depuis le dashboard Neon |
| `invalid sslmode value: ""` | variable `SSLMODE` définie vide | la mettre à `require`, ou ne pas la définir |
| `NOT READY ... gold.predictions (0 rows)` | `ml.predict` non exécuté | lancer `python -m banking_dataops.ml.predict` |
| Power BI : `SSL connection required` | `sslmode` absent | ajouter `?sslmode=require`, ou saisir le port dans le champ Serveur |
| `[boot] PostgreSQL ready!` répété | artefact d'affichage du terminal Compose, pas une boucle | `docker compose up -d` puis `docker compose logs -f app --no-color` |
| Conteneur qui redémarre vraiment | état corrompu par un plantage précédent | `docker compose down -v` (le `-v` purge le volume), puis `build --no-cache` et `up` |
| `no usable system locales` (postgres) | image Alpine/musl | normal, aucun impact : l'encodage UTF8 est confirmé juste après |
| `ignoring /docker-entrypoint-initdb.d/*` | ce projet applique son schéma en Python | normal, attendu |

### Comprendre les journaux Docker

Si `docker compose up` affiche plusieurs `[boot] PostgreSQL ready!` d'affilée, ce n'est pas une boucle de
redémarrage. L'entrypoint imprime `[boot] Waiting for PostgreSQL...` de façon inconditionnelle avant sa
boucle d'attente, et cette boucle contient un `break` dès le premier succès : un vrai crash en boucle
réafficherait `Waiting` à chaque tentative. Le bandeau interactif de Compose (`Watch / Detach`) se mélange au
flux de sortie. Pour un journal sans artefact :

```powershell
docker compose up -d
docker compose logs -f app --no-color
```

Si le conteneur redémarre réellement (`exited with code 1` visible) :

```powershell
docker compose down -v
docker compose build --no-cache
docker compose up
```

Le `-v` purge le volume PostgreSQL ; sans lui, on repart de l'état laissé par le plantage et le symptôme se
reproduit.

---

## Ce qui a été vérifié, et ce qui ne l'a pas été

Vérifié, avec des preuves dans ce dépôt : la chaîne complète sur la pile Docker locale (`evidence/`) :
schéma créé, 1 500 transactions, Gold, modèle entraîné, 1 500 scores, 8 vues, `DEMO READY 15/15`.

Non rejoué pour cette archive : la séquence sans Docker de ce guide, et la connexion à une base Neon.
Les procédures Neon s'appuient sur la documentation officielle de la plateforme ; les temps de réveil et les
quotas des offres gratuites restent à constater.

---

Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
