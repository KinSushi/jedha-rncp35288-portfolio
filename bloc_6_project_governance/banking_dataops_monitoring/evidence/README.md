# Preuves d'exécution

Chaque fichier de ce dossier est la sortie réelle d'une commande, jamais saisie à la main. Date, commande et état du code sont
donnés pour chacun. Heures en UTC (les journaux de la pile Docker sont en UTC).

**État du code** : les preuves de la pile Docker ont été produites sur l'arbre de travail du commit `d5c40eb` (branche
`certification-cdsd-bloc6`), le **11 octobre 2026** ; les preuves de la suite de tests ont été rejouées le **12 octobre 2026** sur le commit `ea68028` (section « Tests » ci-dessous).

**Pile** : `docker compose` (projet Compose nommé à part, option `-p`), PostgreSQL 16 (alpine), image construite par le `Dockerfile` (Python 3.12), ports
d'hôte 55432, 18000 et 18501 (5432 était occupé par un PostgreSQL local). Ancre de date par défaut, **2026-07-27T00:00:00Z**
(ADR-0009), sauf pour `variante_ancre_aujourdhui/`.

## Pile Docker, ancre par défaut (11 octobre 2026, 18 h 46 à 18 h 48 UTC)

| Fichier | Commande | Ce qu'il établit |
|---|---|---|
| `pipeline_full.txt` | `docker compose exec app python run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train` | Pipeline complet et entraînement. Empreinte du lot `89ac138fb9943f00` (celle de la preuve du 27 juillet), 1 500 lignes à chaque étape, sélection par validation croisée, seuil hors-pli, évaluation unique sur le test |
| `model_metrics.json` | copie de `models/metrics.json` | Rapport d'entraînement : AUC en validation croisée 0,9748 (logistique) et 0,9661 (forêt), seuil 0,852, sur le test précision 0,6562, rappel 0,7, F1 0,6774, matrice 334 / 11 / 9 / 21, AUC de test 0,97 et 0,9573 |
| `predict_output.txt` | `python -m banking_dataops.ml.predict` | 1 500 transactions notées dans `gold.predictions` |
| `bands_gold_predictions.txt` | requêtes SQL sur `gold.predictions` | Bandes : 126 *high*, 99 *medium*, 1 275 *low* ; *high* égale `predicted_suspicious` (0 écart sur 1 500) |
| `reconciliation_output.txt` | `python -m banking_dataops.reconciliation` | Agrégats de cohérence (relancée après le pipeline, qui vide la table) |
| `smoke_check_full.txt` | `python scripts/smoke_check.py` | `DEMO READY`, 15 assertions sur 15 |
| `powerbi_views.txt` | `python -m banking_dataops.powerbi_views` | Les huit vues du schéma en étoile existent |
| `vw_publication_verdict.txt`, `api_verdict.json` | vue SQL et `GET /verdict` | Verdict **WARN** : 6 contrôles réussis, 1 avertissement (CTRL-007, fraîcheur), `can_publish` vrai. Ce n'est pas un défaut de l'exécution : CTRL-007 compare le dernier événement (26 juillet 2026) à l'horloge, et 60 jours se sont écoulés (ADR-0009) |
| `audit_trail.txt`, `audit_runs.txt` | requêtes de `make audit-trail` et `make audit-runs` | Journal d'audit : 30 derniers événements avec leur raison, une ligne par exécution |
| `captures/dashboard_complet_ancre_defaut.png` | capture du tableau de bord (Edge sans interface) | Page entière |

## Variante : ancre = aujourd'hui (`variante_ancre_aujourdhui/`, 11 octobre 2026, 18 h 48 UTC)

Mêmes commandes avec `BANKING_DATAOPS_GENERATED_AT=today`, c'est-à-dire un lot daté du 11 octobre 2026, un dimanche. Verdict **PASS**, 7
contrôles sur 7. Modèle : validation croisée 0,9748 et 0,9658, seuil 0,852, AUC de test 0,9698, mêmes précision, rappel et F1, bandes
127 / 98 / 1 275. C'est la variante à utiliser pour une démonstration (bandeau vert), au prix de la reproductibilité à l'octet.

## Tests

| Fichier | Commande | Remarque |
|---|---|---|
| `pytest_full.txt`, `01_pytest_summary.txt` | `pytest -q -rs -W error::DeprecationWarning -W error::PendingDeprecationWarning` | **545 réussis, 3 ignorés, 1 xfail, 1 avertissement**, 25,92 s, le 12 octobre 2026 à 05 h 23 UTC, sur le commit `ea68028`. Sortie complète ; seuls les chemins absolus de la machine sont remplacés par `<venv>`. Les 3 ignorés sont des conditionnels documentés (`E''` dans trois fichiers SQL) |
| `03_tests_par_fichier.txt` | `pytest --collect-only -q` | 549 cas collectés, par fichier (collecte seule, aucun test exécuté pour ce fichier) |
| `reverse_full.txt`, `02_reverse_tests.txt` | `pytest tests/test_reverse_paths.py -v`, mêmes options | 12 réussis et 1 xfail strict (l'entrypoint ne démarre pas le tableau de bord si le pipeline échoue : écart connu, documenté par le test). Rejoué le 12/10/2026 05 h 23 UTC sur le commit `ea68028`, comme la suite complète de `pytest_full.txt`, qui contient ces cas |

L'avertissement restant vient de Starlette (`StarletteDeprecationWarning`, sous-classe de `UserWarning`) : il n'est pas un
`DeprecationWarning`, donc les options `-W error::DeprecationWarning` ne l'élèvent pas.

## Avant la correction

`rejeu_modele_2026-07-27.json` et `rejeu_modele_2026-07-27.md` décrivent l'état **antérieur** (modèle et seuil choisis sur le jeu de test),
rejoué le 10 et le 11 octobre 2026 ; ils sont conservés comme point de comparaison (seuil 0,8335, précision 0,657, rappel 0,767, F1 0,708,
bandes 0 / 227 / 1 273).

`archive/` contient les preuves antérieures remplacées ici (suffixe `_2026-08-ancien`) : sorties du pipeline et du smoke check de la
version précédente, et suite de 178 tests réussis et 6 ignorés. Les doublons `*_output.txt` (extraits des fichiers `*_full.txt`) n'ont pas été reconduits.

## Ce qui n'est pas dans ce dossier

- Les journaux de démarrage du conteneur : le serveur Streamlit y imprime l'adresse IP publique de la machine (« External URL »).
- La sortie brute de la mesure de mutation (`mutmut` 3.8.0, sous WSL). Son résultat est consigné dans `MUTANTS_EQUIVALENTS.md` : 3 926 mutants tués sur 3 967, mesure du 12 octobre 2026 ; les 41 survivants y sont nommés et analysés.
