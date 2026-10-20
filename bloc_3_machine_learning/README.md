# Bloc 3 — Analyse prédictive de données structurées

Trois études de cas d'apprentissage automatique sur données tabulaires, réalisées pour le bloc 3 de la certification **Concepteur Développeur en Science des Données** (RNCP35288) : une classification sur données déséquilibrées, une régression sur un petit jeu de données, et un clustering géographique suivi d'une prévision horaire.

| Projet | Question métier | Supervisé (modèle retenu) | Non supervisé | Notebook |
|---|---|---|---|---|
| **Conversion** | Quel visiteur d'un site va s'abonner à la newsletter ? | Classification déséquilibrée : régression logistique, seuil optimisé pour le F1 | KMeans + PCA (segments de visiteurs) | [`2_Conversion/Conversion_Rate_Bloc3_CDSD_v2.ipynb`](2_Conversion/Conversion_Rate_Bloc3_CDSD_v2.ipynb) |
| **Walmart** | Combien un magasin vendra-t-il dans la semaine ? | Régression : Lasso, régularisation choisie par validation croisée | KMeans + PCA (profils de magasins) | [`1_Walmart/Walmart_Bloc3_CDSD_v2.ipynb`](1_Walmart/Walmart_Bloc3_CDSD_v2.ipynb) |
| **Uber** | Où se concentre la demande à New York, et combien de courses attendre par zone et par heure ? | Prévision horaire : gradient boosting, comparé à la règle « comme la semaine précédente » | MiniBatchKMeans (hot-zones), DBSCAN testé | [`3_Uber/Uber_Pickups_Bloc3_CDSD_v2.ipynb`](3_Uber/Uber_Pickups_Bloc3_CDSD_v2.ipynb) |

**Lire en 60 secondes.** Chaque notebook est exécuté et publié avec ses sorties : on peut le lire sur GitHub sans rien installer. Les chiffres de cette page sont recopiés des sorties enregistrées ; chaque chiffre renvoie à la cellule ou au fichier d'export qui le contient. Les cellules sont comptées à partir de 0, cellules de texte comprises.

---

## Ce que ces projets démontrent

- **Pipelines scikit-learn** qui encodent, normalisent et découpent sans fuite : imputation, `OneHotEncoder`, `StandardScaler` dans un `Pipeline` ou un `ColumnTransformer`, ajustés sur l'apprentissage seul.
- **Choix du modèle sur la validation** (croisée), le jeu de test ne servant qu'à confirmer ; comparaison systématique à une **référence**.
- **Métrique adaptée au problème** : F1 et PR-AUC pour une classe rare, R², RMSE et MAE pour une régression, MAE et WAPE pour une prévision.
- **Validation croisée** K-Fold stratifiée, K-Fold, et validation **chronologique** quand les données sont ordonnées dans le temps ; diagnostic de sur- et de sous-apprentissage.
- **Non supervisé** : choix de k par la silhouette et l'indice de Davies-Bouldin, sensibilité d'`eps` pour DBSCAN.
- **Influence des variables** : coefficients, colinéarité (VIF), intervalles par bootstrap, importance par permutation.
- **Recommandations** tirées des prédictions, avec leurs réserves.
- **Code testé** : calculs regroupés dans un module importé par chaque notebook, tests automatiques, contrôle statique et test de mutation.

---

## Données (non redistribuées)

Aucun fichier de données n'est publié dans ce dépôt. [`DONNEES.md`](DONNEES.md) explique comment obtenir chaque jeu, où le placer et comment vérifier son empreinte SHA-256.

| Projet | Jeu de données | Taille | Pourquoi il n'est pas ici |
|---|---|---|---|
| Walmart | Ventes hebdomadaires de magasins, indicateurs économiques (température, prix du carburant, CPI, chômage) | 150 lignes, 8 colonnes, 20 magasins | Fourni avec l'énoncé du projet par l'organisme de formation |
| Conversion | Visiteurs d'un site : pays, âge, nouveau visiteur ou non, canal d'arrivée, nombre de pages vues ; cible `converted` | 284 580 lignes d'apprentissage, 31 620 de test | Fourni avec l'énoncé du challenge par l'organisme de formation |
| Uber | Prises en charge à New York, avril à septembre 2014 : date et heure, latitude, longitude, base | 4 534 327 courses ; six fichiers CSV d'environ 210 Mo au total (archive compressée : environ 101 Mo) | Publiques (FiveThirtyEight), mais la source n'affiche pas de licence ; voir [`3_Uber/LIRE_DONNEES.md`](3_Uber/LIRE_DONNEES.md) |

Sources des tailles : notebook Walmart, cellules 4 et 5 ; notebook Conversion, cellule 6 ; notebook Uber, cellule 4, et, pour les octets, le tableau des empreintes de [`3_Uber/LIRE_DONNEES.md`](3_Uber/LIRE_DONNEES.md) (six fichiers de 26 110 064 à 47 540 750 octets, 209 715 627 au total ; archive de 101 042 186 octets).

**Aperçus retirés de la version publique.** Les notebooks sont publiés avec leurs sorties, à une exception près : les tableaux qui affichaient des lignes individuelles des jeux d'origine ont été retirés des sorties enregistrées. Le code de ces cellules est inchangé ; il les affiche de nouveau quand on exécute le notebook avec les données.

| Notebook | Sorties retirées (lignes individuelles) | Cellules |
|---|---|---|
| Walmart | Les 10 premières lignes brutes ; les 5 premières lignes après prétraitement et les mêmes lignes de `X` ; les 5 lignes du magasin 12 retirées par la règle des 3 écarts-types ; les 10 plus grosses erreurs du test, avec leurs variables | 4, 9, 12 et 22 |
| Conversion | Les 5 premières lignes ; les 2 lignes d'âge aberrant (111 et 123 ans) ; les 5 premières valeurs de la soumission | 6, 15 et 45 |
| Uber | Les 5 premières courses brutes ; 8 exemples de coordonnées exclues | 4 et 6 |

Restent affichés : les dimensions, les statistiques descriptives (Walmart, cellule 5 ; Conversion, cellule 15), les moyennes et les comptes par groupe, les métriques, et les figures. Les fichiers d'export publiés (`walmart_bloc3_exam_outputs/`, `outputs_bloc3_conversion_commente/`) ne contiennent que des résultats : métriques, coefficients, scores, moyennes par groupe de magasins, figures.

---

## 1. Conversion — qualifier les visiteurs qui vont s'abonner

**Problème.** Prédire l'abonnement à une newsletter et comprendre les facteurs associés. La classe positive est rare : **3,23 %** de convertis sur 284 580 visiteurs (cellule 11 ; `summary_results.json`, `conversion_rate_train`). Une référence qui ne prédit jamais de conversion obtient déjà 96,77 % d'exactitude et un F1 nul (cellule 27) : l'exactitude n'est donc pas le critère, le F1 l'est.

**Méthode.** Découpage stratifié 80/20 ; `OneHotEncoder` pour le pays et le canal, `StandardScaler` pour les variables numériques ; comparaison d'une référence, d'une forêt aléatoire équilibrée et de régressions logistiques (C = 1, C = 10, et C = 10 avec pondération des classes), chacune à son seuil optimal pour le F1 sur la validation (cellule 27). Modèle retenu : **régression logistique C = 10, seuil 0,45**.

**Résultats** (fichier [`summary_results.json`](2_Conversion/outputs_bloc3_conversion_commente/summary_results.json) ; cellules 31 et 36) :

| Mesure | Valeur |
|---|---|
| F1 sur la validation | **0,7768** (précision 0,8455, rappel 0,7184) |
| PR-AUC sur la validation | **0,8481**, contre 0,0323 pour un modèle sans information |
| F1 en validation croisée stratifiée à 5 plis, au seuil 0,45 | **0,7671 ± 0,0060** |
| Positifs prédits sur le jeu de test (31 620 lignes) | 2,75 % |

Le F1 de 0,7768 est mesuré sur la validation qui a aussi servi à choisir le seuil ; le chiffre de la validation croisée, 0,7671 ± 0,0060, est le plus prudent des deux.

**Influence des variables.** Sans `total_pages_visited`, c'est-à-dire si l'on prédit avant toute navigation, le F1 tombe à **0,2125** (cellule 38). Cette variable est un indicateur d'intention, pas un levier prouvé : un visiteur déjà décidé parcourt plus de pages (cellules 16 et 43).

**Segmentation.** KMeans sans la cible, de k = 2 à 8 : la silhouette est maximale à k = 2 (0,279), l'indice de Davies-Bouldin minimal à k = 4 (1,480), et l'inertie ne présente aucun coude (cellules 49 et 50). Le modèle à k = 4 isole un groupe de 29 521 visiteurs (10,4 %) qui convertit à 28,7 % (cellule 53).

**Recommandations** (cellule 43) : déclencher la demande d'inscription quand l'intention devient observable ; travailler la réactivation des visiteurs connus ; adapter l'approche par pays ; segmenter les messages d'acquisition, les écarts entre canaux d'arrivée restant modestes ; valider ces recommandations par expérimentation (tests A/B), le modèle ne montrant que des associations prédictives. Les probabilités de conversion calculées sur le jeu de test permettent en outre de prioriser les visiteurs à cibler (cellules 45 et 46).

---

## 2. Walmart — prévoir les ventes hebdomadaires d'un magasin

**Problème.** Prédire `Weekly_Sales` à partir du magasin, du calendrier et du contexte économique, avec au moins une régression linéaire, un modèle régularisé et des prétraitements imposés : suppression des cibles manquantes, variables de date, retrait des valeurs hors de la moyenne ± 3 écarts-types.

**Données préparées.** 150 → 136 → **131 lignes** ; la règle des 3 écarts-types sur le chômage retire les cinq lignes du magasin 12, qui disparaît (cellules 9 et 10).

**Méthode.** Découpage 80/20 (104 lignes d'apprentissage, 27 de test) ; `ColumnTransformer` (magasin en `OneHotEncoder`, variables numériques imputées puis standardisées) ; référence, régression linéaire, Ridge et Lasso, régularisation choisie par `GridSearchCV` (K-Fold à 5 plis) sur l'apprentissage seul (cellules 13, 14 et 16).

**Résultats** ([`model_metrics.csv`](1_Walmart/walmart_bloc3_exam_outputs/model_metrics.csv) ; cellules 16, 17 et 25) :

| Modèle | RMSE de validation croisée | R² apprentissage | R² test | MAE test |
|---|---|---|---|---|
| **Lasso (α = 316)** | **150 194 $** | 0,978 | **0,901** | 145 106 $ |
| Ridge (α = 0,0316) | 151 079 $ | 0,978 | 0,899 | 143 948 $ |
| Régression linéaire | 152 475 $ | 0,978 | 0,898 | 145 803 $ |
| Référence (moyenne) | 668 514 $ | 0,000 | −0,032 | 531 906 $ |

- R² moyen de validation en K-Fold : 0,949 ; diagnostic : sur-apprentissage modéré sur un petit jeu (cellule 25).
- **Stabilité** : toute la procédure rejouée sur 40 découpages donne un R² de test moyen de **0,934, de 0,473 à 0,975** ; le Lasso est retenu 28 fois sur 40 ([`repeated_splits_results.csv`](1_Walmart/walmart_bloc3_exam_outputs/repeated_splits_results.csv) ; cellule 27). L'avance du Lasso sur la régression linéaire est plus petite que la variation due au découpage.

**Influence des variables.** Le magasin domine. Le CPI est colinéaire avec le magasin (VIF 16,7, [`collinearity_vif.csv`](1_Walmart/walmart_bloc3_exam_outputs/collinearity_vif.csv)) : son effet propre n'est pas établi. Parmi les variables économiques, seuls la température et le chômage ont un effet dont le signe est établi par bootstrap (intervalle à 95 % qui exclut 0, cellule 34).

**Segmentation.** KMeans sur les profils moyens des 19 magasins : silhouette maximale à k = 4 (0,248), voisine des autres valeurs ([`silhouette_scores.csv`](1_Walmart/walmart_bloc3_exam_outputs/silhouette_scores.csv)) ; la segmentation reste exploratoire (cellule 30).

**Recommandations** (cellule 34) : piloter par magasin ou par segment de magasins ; n'utiliser que les effets de signe établi ; planifier avec une marge, l'erreur absolue moyenne au test représentant 12,4 % des ventes moyennes.

---

## 3. Uber — localiser la demande et prévoir les courses

**Problème.** Recommander aux chauffeurs les zones de forte demande selon le jour et l'heure : **où** (clustering en hot-zones) et **combien** (prévision par zone et par heure, comparée à la règle en place).

**Données.** 4 534 327 courses d'avril à septembre 2014 (cellule 4) ; 21 227 lignes hors de la zone de New York écartées (0,468 %) (cellule 6).

**Hot-zones.** Latitude et longitude projetées en kilomètres, pour que la distance de KMeans ait un sens. De k = 4 à 10, **k = 8** maximise la silhouette (0,492) et minimise l'indice de Davies-Bouldin (0,644) (cellule 14) ; un modèle et une carte par jour de la semaine (cellules 16 et 18 ; l'image de ces cartes n'est pas publiée, voir § Limites). Avec DBSCAN, quand `eps` passe de 0,1 à 1,2 km, le nombre de clusters varie de 49 à 4 et le plus grand cluster regroupe de 52 % à 97 % des points (cellule 22) : MiniBatchKMeans, dont les centroïdes sont directement exploitables, est retenu.

**Prévision.** Table zone × heure, encodage cyclique de l'heure, retards de 24 h et de 168 h calculés sur le passé seul ; apprentissage d'avril à août, test sur septembre ; validation croisée **chronologique** à 5 plis (`TimeSeriesSplit`) (cellules 26 à 29).

| Modèle | R² validation croisée | R² test | MAE test (courses/h) | WAPE test |
|---|---|---|---|---|
| Règle en place : « comme la semaine précédente » | 0,882 | 0,886 | 36,202 | 20,354 % |
| Ridge | 0,907 | 0,907 | 36,508 | 20,526 % |
| **Gradient boosting** | **0,909** | **0,907** | **34,164** | **19,209 %** |

Source : notebook Uber, cellule 29 (tableaux de la validation croisée chronologique et du test sur septembre).

Le gradient boosting **bat la règle en place de 5,6 %** sur la MAE ; Ridge ne la bat pas. Le gain est inégal : meilleur dans 6 zones sur 8, la zone Midtown apportant 90 % du gain (cellule 30). Variable la plus influente : le retard d'une semaine (importance par permutation, cellule 31).

**Recommandations** (cellule 32) : positionner les chauffeurs selon la prévision par zone, après un test A/B ; alimenter le modèle chaque jour ; prévoir une marge aux heures de pointe, sous-estimées de **17,9 %** sur les 5 % d'heures les plus chargées (cellule 30).

---

## Qualité du code

Dans chaque projet, les calculs sont regroupés dans un module importé par le notebook (`walmart_bloc3.py`, `conversion_bloc3.py`, `uber_bloc3.py`) ; les réglages que l'on voudrait modifier restent dans les cellules. Le module doit donc rester à côté de son notebook.

| Contrôle | Walmart | Conversion | Uber |
|---|---|---|---|
| Tests (`pytest`) | 101 | 78 | 126 |
| dont tests qui n'exigent aucune donnée (`-m "not notebook"`) | 93 | 71 | 119 |
| Mutation (erreurs détectées sur erreurs introduites) | 556 / 556 | 314 / 316 | 670 / 673 |

- Les tests vérifient les résultats attendus et les **refus** : une colonne inconnue, un seuil hors de [0, 1] ou des données absentes arrêtent le calcul avec un message qui nomme la faute. Les tests marqués `notebook` réexécutent chaque notebook et vérifient qu'il retrouve ses sorties enregistrées.
- Les 5 mutants non détectés ne changent pas le comportement du code ; chacun est justifié dans `MUTANTS_EQUIVALENTS.md` (Conversion et Uber).
- **ruff**, règles E, F, I, B, SLF, UP et W, cible Python 3.12 : 0 constat sur le notebook, le module et les tests de chaque projet. Le `ruff.toml` de chaque projet ne fixe que la largeur de ligne, 99 colonnes au lieu des 79 de PEP 8 (choix déclaré, que PEP 8 admet pour une équipe qui s'accorde dessus) : les règles se donnent en ligne de commande, voir « Reproduire ».
- **mypy `--strict`** : 0 erreur sur le module de chaque projet ; 0 erreur aussi sur le code des notebooks, mesuré sur un script qui met bout à bout leurs cellules de code (mypy ne lit pas les `.ipynb`). Les fichiers de tests ne sont pas soumis à mypy.
- Dernière exécution complète des tests, du contrôle statique et de la mutation : 17 octobre 2026, Python 3.12.14, sur la version complète des projets (avec les données et tous les fichiers d'export).

---

## Reproduire

Chaque projet est autonome. Versions exactes dans son `requirements.txt` (Python 3.12.14, numpy 2.5.4, pandas 3.0.6, scikit-learn 1.9.1, matplotlib 3.11.2) ; `requirements-dev.txt` y ajoute pytest 9.1.1.

```bash
cd bloc_3_machine_learning/2_Conversion      # ou 1_Walmart, 3_Uber
python -m venv .venv
source .venv/bin/activate                    # Windows : .venv\Scripts\activate
python -m pip install -r requirements-dev.txt

# Sans aucune donnée : tests du module
python -m pytest -m "not notebook"
```

Avec les données (voir [`DONNEES.md`](DONNEES.md)) :

```bash
# Walmart et Conversion : placer les CSV à côté du notebook
# Uber : indiquer l'archive des données
export UBER_DATA_ZIP=/chemin/vers/uber-trip-data.zip   # Windows : $env:UBER_DATA_ZIP = "..."

python -m pytest          # réexécute aussi le notebook (plusieurs minutes)
```

- Les tests `notebook` exécutent le notebook avec `nbclient` et le noyau `ipykernel`, tous deux épinglés dans `requirements.txt`. Sans les données, ceux de Walmart (8) et de Conversion (7) échouent. Pour Uber, 5 des 7 sont ignorés sans l'archive, et pytest le signale ; les 2 autres tournent toujours, parce qu'ils fabriquent leur propre petite archive ou pointent vers un fichier absent, et exécutent les premières cellules du notebook.
- Même avec les données, trois sortes de tests `notebook` échouent sur cette version publique, et c'est attendu : ceux qui comparent les exports recalculés à des fichiers d'export qui ne sont pas publiés ici (lignes du jeu de données, soumission du challenge, exports Uber) ; et ceux qui comparent les sorties enregistrées du notebook à une exécution neuve, puisque les aperçus de lignes individuelles ont été retirés des sorties publiées (voir « Données »).
- Pour ouvrir les notebooks dans un navigateur : `python -m pip install jupyterlab`, puis `jupyter lab` (non épinglé : il ne calcule rien).
- Les notebooks écrivent leurs exports dans leur propre dossier : travailler sur une copie du dossier du projet entier, module compris. Le fichier `.gitignore` de ce dossier empêche de versionner les données et les exports qui en contiennent des lignes.

**Contrôle statique.** ruff et mypy ne figurent pas dans `requirements-dev.txt` (`python -m pip install ruff mypy`). Lancer les commandes **depuis le dossier du projet** : ailleurs, ruff ne reconnaît plus le module comme local et signale à tort l'ordre des imports des tests (I001). Exemple pour Conversion :

```bash
cd bloc_3_machine_learning/2_Conversion
ruff check --select E,F,I,B,SLF,UP,W --target-version py312 .   # la largeur de 99 colonnes vient de ruff.toml
mypy --strict conversion_bloc3.py

# Code du notebook : cellules de code mises bout à bout dans un script, puis mypy
python -c "import json; nb = json.load(open('Conversion_Rate_Bloc3_CDSD_v2.ipynb', encoding='utf-8')); code = [''.join(c['source']) for c in nb['cells'] if c['cell_type'] == 'code']; open('code_du_notebook.py', 'w', encoding='utf-8').write('\n\n'.join(code) + '\n')"
mypy --strict code_du_notebook.py
rm code_du_notebook.py                                          # Windows : del code_du_notebook.py
```

Les versions de ruff et de mypy ne sont pas épinglées : une version plus récente peut ajouter des contrôles.

---

## Limites

- **Conversion.** Le seuil est choisi sur la validation, sans validation croisée imbriquée ; les probabilités ne sont pas calibrées (matrice des angles morts, cellule 61). Les coefficients ne prouvent aucune causalité, et le seuil est à recalibrer avant toute production (cellules 56 et 61). La comparaison se fait contre une référence qui ne prédit jamais de conversion (cellule 27). Le choix de k = 4 suit l'indice de Davies-Bouldin et un critère métier, alors que la silhouette préfère k = 2 (cellules 49, 50 et 56).
- **Walmart.** 131 lignes : les métriques dépendent fortement du découpage (R² de test de 0,473 à 0,975). Les seuils des valeurs extrêmes sont calculés avant le découpage, comme l'impose l'énoncé ; un pipeline de production les apprendrait sur l'apprentissage seul. Découpage aléatoire et non temporel ; coefficients descriptifs (cellule 32).
- **Uber.** La demande de septembre dépasse les niveaux appris (+48 % en moyenne) : un modèle à base d'arbres n'extrapole pas, d'où la sous-prévision des pointes. Ni météo, ni événements, ni offre de chauffeurs dans les données ; noms de zones approximatifs (cellules 30 et 33). Données de 2014.
- **Données personnelles.** Une localisation horodatée est une donnée personnelle au sens du RGPD (art. 4.1) : la version publique ne contient aucune course individuelle, ni en tableau ni en image : les trois cartes du notebook Uber qui affichent des points individuels (cellules 10, 18 et 20 ; voir la cellule 35) sont publiées sans leur image, et le code les redessine à partir des données. Les données Conversion ne sont pas anonymes au sens strict (1,04 % de profils uniques, cellule 54). Walmart ne contient aucune donnée personnelle (cellule 31).

---

## Structure

```text
bloc_3_machine_learning/
├── README.md
├── DONNEES.md                       comment obtenir les données
├── .gitignore                       données et exports à ne pas versionner
├── docs/evidence_note.md            note de preuve du bloc
├── 1_Walmart/
│   ├── Walmart_Bloc3_CDSD_v2.ipynb  notebook exécuté, avec ses sorties
│   ├── walmart_bloc3.py             module de calcul importé par le notebook
│   ├── tests/                       tests du module et du notebook
│   ├── walmart_bloc3_exam_outputs/  métriques, coefficients, scores
│   └── requirements.txt, requirements-dev.txt, pytest.ini, ruff.toml
├── 2_Conversion/
│   ├── Conversion_Rate_Bloc3_CDSD_v2.ipynb
│   ├── conversion_bloc3.py
│   ├── MUTANTS_EQUIVALENTS.md
│   ├── tests/
│   ├── outputs_bloc3_conversion_commente/   figures et résumé chiffré
│   └── requirements.txt, requirements-dev.txt, pytest.ini, ruff.toml
└── 3_Uber/
    ├── Uber_Pickups_Bloc3_CDSD_v2.ipynb
    ├── uber_bloc3.py
    ├── LIRE_DONNEES.md
    ├── MUTANTS_EQUIVALENTS.md
    ├── tests/
    └── requirements.txt, requirements-dev.txt, pytest.ini, ruff.toml
```
