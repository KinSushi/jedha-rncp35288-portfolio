# Démarrage sous Windows

Le projet fonctionne nativement sous Windows. Aucun `make`, aucun `chmod`, aucun script bash : uniquement **Docker Desktop**.

---

## Le problème des ports, résolu automatiquement

Un ingénieur data a presque toujours un PostgreSQL déjà en écoute sur 5432. Le script sonde les trois ports au démarrage et écrit un `.env` avec ceux qui sont libres.

**Rien n'est tué, rien n'est désinstallé.** Le conteneur continue d'utiliser 5432 en interne ; seule la porte d'entrée côté machine change. Un PostgreSQL local n'est pas touché.

---

## Premier démarrage

Décompresser `04-Code-source.zip`, ouvrir l'explorateur dans le dossier `banking_dataops_monitoring`, puis **double-cliquer sur `DEMARRER-WINDOWS.bat`**.

Ou, depuis PowerShell :

```powershell
cd "C:\chemin\vers\banking_dataops_monitoring"
.\run.ps1 setup
```

Les guillemets sont obligatoires si le chemin contient des espaces ou des parenthèses.

Puis, dans l'ordre :

```powershell
.\run.ps1 start      # démarre et attend que le tableau de bord réponde
.\run.ps1 train      # relance le pipeline avec --train, puis remplit la file d'investigation (gold.predictions)
.\run.ps1 smoke      # doit afficher DEMO READY
```

`start` affiche les trois adresses réellement utilisées, ports décalés compris.

### Lot daté du jour

Par défaut, le lot est daté du 27 juillet 2026 (reproductible à l'octet, ADR-0009). Depuis le 25 septembre 2026, le contrôle de fraîcheur CTRL-007 y voit un jeu de données de plus de 60 jours : le verdict est `WARN` (6 contrôles réussis sur 7, publication autorisée sous réserve). Pour un lot daté du jour et un verdict `PASS` (7 sur 7), définir la variable dans la même fenêtre PowerShell, avant `start` :

```powershell
$env:BANKING_DATAOPS_GENERATED_AT = "today"
.\run.ps1 start
```

La variable est transmise au conteneur par `docker-compose.yml`. Hors Docker, `python run_pipeline.py --generated-at today` produit le même lot.

---

## Si quelque chose ne va pas : commencer par là

```powershell
.\run.ps1 doctor
```

Affiche la version de PowerShell, l'état de Docker, la présence des fichiers, les ports occupés, le contenu du `.env`, les images construites et l'état des conteneurs : les faits qui déterminent si le projet peut démarrer.

---

## Si PowerShell refuse d'exécuter le script

Message typique : *l'exécution de scripts est désactivée sur ce système*.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Valable pour **cette session uniquement**, aucune modification permanente de la machine. Le fichier `.bat` fait déjà ce contournement tout seul.

---

## Où vit la logique

`run.ps1` n'orchestre que Docker. Toute la logique de vérification est dans `scripts/smoke_check.py`, appelé aussi bien par PowerShell que par bash : une seule implémentation, deux appelants, aucune imbrication de code Python dans des chaînes PowerShell.

---

## Toutes les commandes

```powershell
.\run.ps1 help
```

| Commande | Effet |
|---|---|
| `setup` | Sonde les ports, construit l'image. À faire une fois, avant d'avoir besoin de la pile |
| `start` | Démarre tout depuis un volume vide, attend le tableau de bord |
| `demoday` | Redémarre `postgres` et `app` **sans supprimer le volume** (contrairement à `start`) et attend le tableau de bord |
| `train` | Relance le pipeline avec `--train` (le lot est régénéré, les lignes de démonstration disparaissent), puis `ml.predict` |
| `smoke` | Vérification de bout en bout, doit afficher **DEMO READY** |
| `verify` | Lance `run_pipeline.py --verify` : une exécution ordinaire, l'option ne vérifie rien (voir `docs/pipeline_architecture.md`) |
| `demo-inject` | Injecte le lot dégradé |
| `demo-reset` | Le retire et relance les contrôles |
| `demo-status` | Combien de lignes de démonstration sont présentes |
| `stop` | Arrête, conserve les données |
| `reset` | Vide le volume et redémarre |
| `logs` | Suit les journaux du pipeline |
| `status` | État des deux conteneurs (`postgres` et `app`) |
| `test` | La suite de tests : <!--tests-fr-->545 réussis, 3 ignorés, 1 échec attendu (xfail), soit 549 cas<!--/tests--> (preuves du 12 octobre 2026) |
| `lint` | `ruff` |
| `reverse-test` | Les chemins interdits et la reproductibilité, en détail (`test_reverse_paths.py` : 13 cas, dont 12 réussis et 1 xfail ; `test_reproducibility.py` : 7 cas) |
| `audit` | 25 derniers actes, avec leur motif métier |
| `verdict` | Le verdict de publication |
| `queue` | La file d'investigation du modèle |
| `powerbi` | Crée les vues et affiche les paramètres de connexion |

---

## Redémarrage sans remise à zéro

```powershell
.\run.ps1 demoday
```

Démarre `postgres` et `app` sans exécuter `docker compose down -v` : la base et les prédictions existantes sont conservées. L'API (`/verdict`) tourne dans le conteneur `app`, à côté du tableau de bord : elle est donc disponible dans ce mode. Pour entraîner le modèle sans régénérer le lot, utiliser `docker compose exec app python -m banking_dataops.ml.train` puis `... ml.predict`.

---

## Correspondance avec le Makefile

Les deux exposent les mêmes tâches. Sous Linux ou macOS, `make smoke` ; sous Windows, `.\run.ps1 smoke`.

| Makefile | PowerShell |
|---|---|
| `make stack-up` | `.\run.ps1 start` |
| `make pipeline-train` | `.\run.ps1 train` |
| `make smoke` | `.\run.ps1 smoke` |
| `make pipeline-verify` | `.\run.ps1 verify` |
| `make demo-inject-app` | `.\run.ps1 demo-inject` |
| `make audit-trail` | `.\run.ps1 audit` |
| `make powerbi-views` | `.\run.ps1 powerbi` |

---

## En cas de blocage

```powershell
.\run.ps1 logs
.\run.ps1 status
```

| Symptôme | Cause | Correctif |
|---|---|---|
| `port is already allocated` | `.env` absent ou périmé | `.\run.ps1 setup` : sonde à nouveau les ports et réécrit le `.env` |
| `Docker is not running` | Docker Desktop éteint | Le lancer, attendre l'icône verte |
| Le tableau de bord ne répond pas | Le pipeline a échoué | `.\run.ps1 logs` |
| `no such file: entrypoint.sh` | Fins de ligne Windows dans l'image | `docker compose build --no-cache` |
| `model artefact present : FAIL` | Modèle jamais entraîné | `.\run.ps1 train` |

---

## Ce qui n'est pas nécessaire

Ni Git Bash, ni WSL, ni `make`, ni `chmod`, ni Git : le code source est dans l'archive. Uniquement **Docker Desktop**.
