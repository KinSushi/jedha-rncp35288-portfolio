# Bloc 6 : direction de projets de gestion de données

## banking-dataops-monitoring : ce lot de données bancaires peut-il être publié ?

Un dispositif de contrôle de la qualité des données bancaires, conduit de bout en bout comme un projet : cahier des charges, veille, rétroplanning, budget, registre des risques, conformité RGPD, indicateurs de pilotage, décisions d'architecture datées, code, tests et preuves d'exécution.

Le code est dans [`banking_dataops_monitoring/`](banking_dataops_monitoring/). Chaque chiffre de cette page renvoie au fichier qui le produit.

Titre visé : Concepteur développeur en science des données (RNCP35288), bloc 6.

---

## 1. Problématique

Dans une institution financière, les données de transaction passent par plusieurs systèmes avant d'alimenter le reporting réglementaire et les états financiers. Une erreur n'est pas signalée : elle est acceptée en aval et découverte plus tard, parfois à l'audit. Le risque tient moins à l'existence d'erreurs qu'au **délai entre l'erreur et sa découverte** ([cahier des charges, § 1](banking_dataops_monitoring/docs/project/01-cahier-des-charges.md)).

Le projet outille une décision quotidienne, unique et traçable :

> **Ce lot de données peut-il alimenter les systèmes en aval, oui ou non ?**

Une seconde question s'y ajoute sans jamais modifier la première : parmi les transactions d'un lot publiable, par lesquelles un analyste doit-il commencer son instruction ? ([ADR-0008](banking_dataops_monitoring/docs/adr/0008-classifieur-de-priorisation.md))

## 2. Données

**Les données sont entièrement synthétiques.** Aucune donnée bancaire réelle, aucune donnée personnelle, aucune connexion à un système de production. Aucun fichier de données n'est versionné : on les génère.

- Le générateur `src/banking_dataops/generate_synthetic_data.py` produit trois fichiers (clients, comptes, transactions) à graine fixe et à **date d'ancre** fixe, le 27 juillet 2026 par défaut. Même graine et même ancre donnent les mêmes octets : empreinte de lot `89ac138fb9943f00` ([`data/README.md`](banking_dataops_monitoring/data/README.md), [`evidence/pipeline_full.txt`](banking_dataops_monitoring/evidence/pipeline_full.txt)).
- Le lot de référence compte 100 clients, 200 comptes et 1 500 transactions, graine 42 ([`evidence/pipeline_full.txt`](banking_dataops_monitoring/evidence/pipeline_full.txt), journal de chargement). Il est produit par la commande `run_pipeline.py --seed 42 --transactions 1500 --customers 100 --train`, consignée dans [`evidence/README.md`](banking_dataops_monitoring/evidence/README.md).
- Pourquoi du synthétique : aucun jeu public ne reproduit un flux transactionnel à plusieurs systèmes sources, et la génération permet d'injecter des anomalies choisies, donc de tester les contrôles ([ADR-0003](banking_dataops_monitoring/docs/adr/0003-donnees-synthetiques.md)).

**Obtenir les données** (depuis `banking_dataops_monitoring/`) :

```bash
make generate                                                        # ancre par défaut, reproductible
python -m banking_dataops.generate_synthetic_data --generated-at 2026-09-01T00:00:00Z   # autre ancre
python run_pipeline.py --generated-at today                          # lot daté du jour, non reproductible à l'octet
```

## 3. Méthode

### Architecture

Un pipeline par lots en couches Bronze, Silver et Gold sur PostgreSQL 16, orchestré en Python, livré en deux conteneurs (`postgres` et `app`) par Docker Compose ([`docs/pipeline_architecture.md`](banking_dataops_monitoring/docs/pipeline_architecture.md), [`docs/architecture.md`](banking_dataops_monitoring/docs/architecture.md)).

- **Le nettoyage répare la forme, jamais le fond** : un montant implausible traverse le nettoyage intact pour que les contrôles le jugent et le comptent.
- **Sept contrôles déterministes**, nommés et versionnés (`CTRL-001` à `CTRL-007`), plus une réconciliation par système source. **Le verdict de publication ne dérive que de ces contrôles.**
- **Répartition des responsabilités** : la base garantit l'intégrité structurelle, la couche de contrôle porte les règles métier. Trois des sept contrôles ne peuvent structurellement pas échouer, parce que le schéma les prévient ([ADR-0004](banking_dataops_monitoring/docs/adr/0004-contraintes-en-base.md)).
- **Trois consommateurs de la même décision** : un tableau de bord Streamlit pour l'analyste, un schéma en étoile de huit vues pour un rapport Power BI, et une API en lecture seule (`GET /verdict`) pour un pipeline aval ([ADR-0006](banking_dataops_monitoring/docs/adr/0006-api-en-lecture-seule.md)).
- **Traçabilité** : journal d'audit transversal, une ligne par événement avec sa raison ([ADR-0007](banking_dataops_monitoring/docs/adr/0007-tracabilite-et-reverse-tests.md)).

### Le modèle : prioriser, jamais bloquer

Un classifieur supervisé note chaque transaction et ordonne une file d'investigation. Il ne décide jamais de la publication : quand un lot est bloqué, il faut pouvoir dire quelle règle a été violée et sur quelles lignes, ce qu'un score ne permet pas ([ADR-0002](banking_dataops_monitoring/docs/adr/0002-regles-deterministes.md), [ADR-0008](banking_dataops_monitoring/docs/adr/0008-classifieur-de-priorisation.md)).

- Choix du modèle par validation croisée à 4 plis **sur les seules lignes d'entraînement** : une forêt aléatoire optimisée devait battre la régression logistique d'au moins 0,01 d'AUC ; elle ne l'a pas fait, et le modèle plus simple et lisible est retenu.
- Seuil de décision choisi hors-pli (F1 maximal sur les prédictions hors-pli de l'entraînement) ; **le jeu de test n'est évalué qu'une fois** ([ADR-0009](banking_dataops_monitoring/docs/adr/0009-selection-hors-test-bandes-et-ancre.md)).
- Bandes de priorité ancrées sur le seuil : « high » signifie un score au-dessus du seuil.

### Conduite de projet

Neuf décisions d'architecture datées, avec leurs options écartées ([`docs/adr/`](banking_dataops_monitoring/docs/adr/)), et sept documents de gestion de projet ([`docs/project/`](banking_dataops_monitoring/docs/project/)) : cahier des charges, veille technologique, rétroplanning et bilan, budget, registre des risques, conformité RGPD, indicateurs de pilotage.

## 4. Résultats chiffrés

Données synthétiques, cible issue d'une règle du générateur : **ces chiffres démontrent la méthode et la chaîne, pas un taux de détection réel.**

### Chaîne et contrôles (pile Docker du 11 octobre 2026, ancre par défaut)

| Mesure | Résultat | Source |
|---|---|---|
| Verdict à l'ancre par défaut | `WARN` : 6 contrôles réussis, 1 avertissement (fraîcheur, `CTRL-007`), publication autorisée | [`evidence/vw_publication_verdict.txt`](banking_dataops_monitoring/evidence/vw_publication_verdict.txt), [`evidence/api_verdict.json`](banking_dataops_monitoring/evidence/api_verdict.json) |
| Verdict avec un lot daté du jour | `PASS` : 7 contrôles sur 7 | [`evidence/variante_ancre_aujourdhui/vw_publication_verdict.txt`](banking_dataops_monitoring/evidence/variante_ancre_aujourdhui/vw_publication_verdict.txt) |
| Vérification de démonstration de bout en bout | `DEMO READY`, 15 vérifications sur 15 | [`evidence/smoke_check_full.txt`](banking_dataops_monitoring/evidence/smoke_check_full.txt) |
| Schéma en étoile | 8 vues présentes | [`evidence/powerbi_views.txt`](banking_dataops_monitoring/evidence/powerbi_views.txt) |
| Durée d'entraînement du modèle | environ 13 s : 13 079 ms dans le journal de l'étape, 13 093 ms dans le tableau récapitulatif en fin du même fichier | [`evidence/pipeline_full.txt`](banking_dataops_monitoring/evidence/pipeline_full.txt) |

### Modèle de priorisation

| Mesure | Résultat | Source |
|---|---|---|
| Sélection (AUC en validation croisée, entraînement seul) | régression logistique 0,9748 ; forêt aléatoire 0,9661 | [`evidence/model_metrics.json`](banking_dataops_monitoring/evidence/model_metrics.json) |
| Jeu de test | 375 transactions, dont 30 positives | idem (`rows_test`, matrice de confusion) |
| AUC de test | 0,970 | idem |
| Au seuil de 0,852 | précision 0,656 ; rappel 0,700 ; F1 0,677 | idem |
| Matrice de confusion (VN / FP / FN / VP) | 334 / 11 / 9 / 21 | idem |
| Référence « ordre d'arrivée » \* | AUC de 0,45 à 0,49 | [`evidence/rejeu_modele_2026-07-27.md`](banking_dataops_monitoring/evidence/rejeu_modele_2026-07-27.md), [ADR-0008](banking_dataops_monitoring/docs/adr/0008-classifieur-de-priorisation.md) |
| Référence « règle simple » \* (au moins 2 facteurs parmi nuit, transfrontalier, montant élevé) | AUC 0,811 ; précision 0,293 ; rappel 0,733 ; F1 0,419 | idem |
| File d'investigation (1 500 transactions) | 126 *high*, 99 *medium*, 1 275 *low* ; la bande *high* coïncide avec les transactions signalées au-dessus du seuil (0 écart sur 1 500) | [`evidence/bands_gold_predictions.txt`](banking_dataops_monitoring/evidence/bands_gold_predictions.txt) |

\* **Ces deux références ne viennent pas de la même exécution que les autres lignes.** Elles ont été mesurées par le rejeu des 10 et 11 octobre 2026, qui décrit l'**état antérieur** du modèle, quand le modèle et le seuil étaient choisis sur le jeu de test ; le rejeu le dit lui-même ([`evidence/README.md`](banking_dataops_monitoring/evidence/README.md), section « Avant la correction » ; [`evidence/rejeu_modele_2026-07-27.md`](banking_dataops_monitoring/evidence/rejeu_modele_2026-07-27.md), « Ce que ce rejeu ne prouve pas »). Les autres lignes du tableau viennent de l'entraînement actuel, avec sélection hors du jeu de test.

La règle simple ignore le canal et la catégorie de marchand ; le gain du modèle sur elle vient surtout de ces deux familles de facteurs, et il est mesuré sur une cible elle-même produite par une règle : il ne prouve pas un gain réel.

### Qualité du code

| Mesure | Résultat | Source |
|---|---|---|
| Suite de tests (20 octobre 2026) | 547 réussis, 3 ignorés, 1 échec attendu (`xfail`), soit 551 cas, en 38,93 s | [`evidence/01_pytest_summary.txt`](banking_dataops_monitoring/evidence/01_pytest_summary.txt), [`evidence/README.md`](banking_dataops_monitoring/evidence/README.md) |
| Tests de mutation (20 octobre 2026) | 3 927 mutants tués sur 3 967 ; les 40 survivants sont nommés et analysés comme équivalents | [`MUTANTS_EQUIVALENTS.md`](banking_dataops_monitoring/MUTANTS_EQUIVALENTS.md) |

Les 3 tests ignorés sautent sous une condition documentée ; l'échec attendu est un écart connu et déclaré du script de démarrage du conteneur ([`evidence/README.md`](banking_dataops_monitoring/evidence/README.md)).

### Conduite de projet

| Mesure | Résultat | Source |
|---|---|---|
| Registre des risques | 17 risques ; 3 de criticité 12 (R-01, R-04, R-07), aucun au-dessus ; 10 traités, 5 partiels, 2 acceptés | [`docs/project/05-registre-des-risques.md`](banking_dataops_monitoring/docs/project/05-registre-des-risques.md) |
| Budget de construction | 81 h, environ 5 200 €, **hors classifieur de priorisation et rapport Power BI** (non chiffrés) | [`docs/project/04-budget.md`](banking_dataops_monitoring/docs/project/04-budget.md) |
| Coût d'exploitation annuel | environ 11 800 € (600 + 360 + 10 800 = 11 760 €, arrondi) | idem |
| Coût cumulé | 17 000 € la première année ; 40 600 € sur trois ans | idem |
| Poste dominant | la maintenance des règles métier : 10 800 € par an, 63,5 % du coût de la première année | idem |

L'estimation de 81 heures date du cadrage du 20 juillet 2026 : aucun de ses six postes ne nomme le classifieur de priorisation, ajouté le 21 juillet, ni le rapport Power BI. Les coûts cumulés ci-dessus héritent de cette réserve ; le budget liste aussi ce qu'il ne couvre pas, dont les licences Power BI et le branchement sur des données réelles ([`docs/project/04-budget.md`](banking_dataops_monitoring/docs/project/04-budget.md), § 6).

Le budget ne conclut pas à la rentabilité : il chiffre le seuil (40 600 € d'incidents évités sur trois ans) qu'une direction métier devrait juger atteignable ([`docs/project/04-budget.md`](banking_dataops_monitoring/docs/project/04-budget.md)).

## 5. Reproduire

Prérequis : Docker. Toutes les commandes se lancent depuis `banking_dataops_monitoring/`.

```bash
# 1. Démarrer la pile (le pipeline s'exécute au démarrage du conteneur app)
docker compose up --build
#    Pour un lot daté du jour et un verdict PASS :
#    BANKING_DATAOPS_GENERATED_AT=today docker compose up --build
#    (PowerShell : $env:BANKING_DATAOPS_GENERATED_AT = "today"; docker compose up --build)

# 2. Dans un second terminal : entraîner le modèle, puis remplir la file d'investigation
docker compose exec app python -m banking_dataops.ml.train
docker compose exec app python -m banking_dataops.ml.predict

# 3. Vérification de bout en bout
make smoke

# 4. Arrêt (down -v supprime aussi la base)
docker compose down
```

- Tableau de bord : <http://localhost:8501> ; API de décision : <http://localhost:8000/verdict>.
- Si le port 5432 est déjà pris, définir `POSTGRES_PORT` (par exemple 55432) ; `API_PORT` et `DASHBOARD_PORT` existent de même.
- Sous Windows, sans Make : [`DEMARRAGE-WINDOWS.md`](banking_dataops_monitoring/DEMARRAGE-WINDOWS.md).
- Les identifiants présents dans `docker-compose.yml` sont ceux d'une base de développement locale et synthétique.

**Tests** (Python 3.12 ou plus récent, sans PostgreSQL) :

```bash
pip install -r requirements-dev.txt
pytest
ruff check .
```

**Pourquoi le verdict par défaut est `WARN`.** Le contrôle de fraîcheur `CTRL-007` avertit quand le dernier événement du lot a plus de 60 jours. À l'ancre par défaut, ce dernier événement date du 26 juillet 2026 : depuis le 25 septembre 2026, le verdict est donc `WARN` (6 sur 7) et la publication reste autorisée. Avec `--generated-at today`, le verdict est `PASS` (7 sur 7), au prix de la reproductibilité à l'octet ([ADR-0009](banking_dataops_monitoring/docs/adr/0009-selection-hors-test-bandes-et-ancre.md), [`data/README.md`](banking_dataops_monitoring/data/README.md)).

## 6. Limites

Ce que le projet ne démontre pas, dit tel quel :

- **La performance du modèle n'est pas un taux de détection réel** : la cible est une règle du générateur. Sur données réelles, elle viendrait d'investigations confirmées ([`docs/project/05-registre-des-risques.md`](banking_dataops_monitoring/docs/project/05-registre-des-risques.md), R-17).
- **La réconciliation compare des agrégats internes**, pas deux systèmes indépendants ; limite acceptée (R-09).
- **Les seuils des contrôles sont calibrés sur du synthétique** ; le risque de faux positifs qui pousseraient à débrancher les contrôles n'est traité que partiellement (R-07).
- **La dérive du modèle n'est pas mesurée** ; le réentraînement est volontaire, jamais automatique (R-15).
- **La suite de tests n'exécute pas les sept contrôles SQL sur une base** : ils ne s'exécutent que dans le pipeline lancé avec Docker et dans la vérification de démonstration.
- **La pseudonymisation n'est pas réalisée** : les identifiants séquentiels des données synthétiques ne sont pas une pseudonymisation au sens de l'article 4, point 5, du RGPD ([`docs/project/06-conformite-rgpd.md`](banking_dataops_monitoring/docs/project/06-conformite-rgpd.md)).
- **Preuves datées de deux exécutions** : la pile Docker le 11 octobre 2026, la suite de tests le 20 octobre 2026, sur deux états du code ([`evidence/README.md`](banking_dataops_monitoring/evidence/README.md)).
- **Limites de code connues** : l'option `--verify` de `run_pipeline.py` est acceptée mais ne vérifie rien ; `run_pipeline.py` n'appelle pas la prédiction ; l'ingestion charge les fichiers Bronze bruts et non les fichiers nettoyés ; la table de réconciliation est vidée à chaque exécution ; la tendance d'un contrôle repose sur un seul point ; le `Dockerfile` n'est pas multi-étapes.
- **Périmètre** : ce n'est ni une plateforme de données de production, ni un système de contrôle prêt pour une banque, ni un moteur de décision de crédit.

## 7. Organisation du dossier

```text
banking_dataops_monitoring/
|-- run_pipeline.py            orchestrateur : Bronze, nettoyage, Silver, contrôles, Gold, modèle
|-- src/banking_dataops/       génération, ingestion, contrôles, réconciliation, API, modèle
|-- sql/                       schéma, contrôles, vues du schéma en étoile, journal d'audit, couche Gold
|-- dashboard/                 tableau de bord Streamlit
|-- tests/                     suite pytest
|-- evidence/                  sorties réelles des commandes, avec leur date
|-- docs/adr/                  neuf décisions d'architecture datées
|-- docs/project/              cahier des charges, veille, rétroplanning, budget, risques, RGPD, indicateurs
|-- docs/powerbi/              spécification du rapport et mesures DAX
`-- docker-compose.yml, Dockerfile, Makefile
```

**Fichiers cités mais non publiés dans cette version.** Plusieurs documents et scripts du projet renvoient à des fichiers qui ne figurent pas dans ce dépôt :

- `CHANGELOG.md`, le journal des versions du projet ;
- l'historique Git d'origine, dont les ADR citent des commits (par exemple `09c4f2d`) : cette version est publiée sans lui ;
- le dossier de projet, cité par quelques documents de gestion de projet ;
- le rapport Power BI (`.pbix`) et les captures d'écran du tableau de bord : le dossier [`docs/powerbi/`](banking_dataops_monitoring/docs/powerbi/) en donne la spécification, les mesures DAX et le thème ;
- `.env.example`, le modèle de configuration locale : la commande `.\run.ps1 setup` écrit un fichier `.env` avec des ports libres, et les variables `BANKING_DATAOPS_*` sont décrites dans [`src/banking_dataops/config.py`](banking_dataops_monitoring/src/banking_dataops/config.py).

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
