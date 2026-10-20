# Bloc 2 : analyse exploratoire, descriptive et inférentielle de données

## Deux études de cas : une base non massive avec pandas, une base massive avec Spark

| Étude de cas | Question | Données | Outils | Notebook |
|---|---|---|---|---|
| **1. Speed dating** | Pourquoi certaines personnes obtiennent-elles un second rendez-vous ? | CSV de 8 378 lignes × 195 colonnes, 551 participants | pandas, NumPy, SciPy, statsmodels, Matplotlib, Seaborn | [`1_Tinder/Speed_Dating_Bloc2_CDSD.ipynb`](1_Tinder/Speed_Dating_Bloc2_CDSD.ipynb) |
| **2. Steam** | Quels facteurs sont associés à la popularité d'un jeu ? | JSON imbriqué de 55 691 enregistrements (63,7 Mo) | PySpark (Spark 3.5.9), pandas, Matplotlib | [`2_Steam/Steam_Bloc2_CDSD_PySpark.ipynb`](2_Steam/Steam_Bloc2_CDSD_PySpark.ipynb) |

Les deux notebooks sont livrés exécutés, sorties enregistrées. Les fonctions de calcul sont rangées dans un module à côté de chaque notebook, qui l'importe (`speed_dating.py`, `steam_analyse.py`), et testées à part. Chaque chiffre de cette page renvoie à la cellule qui l'affiche ; les cellules sont numérotées à partir de 0, cellules de texte comprises.

Titre visé : Concepteur développeur en science des données (RNCP35288), bloc 2.

---

## 1. Problématiques

**Speed dating.** Le scénario de l'étude de cas : l'équipe marketing d'une application de rencontre constate une baisse des matchs et dispose des données d'expériences de *speed dating* menées de 2002 à 2004 auprès d'étudiants d'une université américaine. Après chaque rendez-vous de quatre minutes, chacun dit s'il veut revoir la personne et la note sur six critères. Le « second rendez-vous » est mesuré par le **match** : les deux ont dit oui (cellule 0).

**Steam.** Le scénario : un éditeur de jeux vidéo prépare un nouveau jeu et demande une analyse globale du catalogue Steam, aux niveaux du marché, des genres et des plateformes (cellule 0).

## 2. Données

**Aucun fichier de données n'est versionné dans ce dépôt.**

| Fichier attendu | Emplacement | Taille (octets) | Empreinte SHA-256 |
|---|---|---|---|
| `Speed+Dating+Data.csv` (encodage latin-1) | `1_Tinder/` | 5 192 296 | `fda9a924b18880fabe6c7a71961cff20aa3c7cc2a5aa8d6e823c2fc41893814c` |
| `steam_game_output.json` | `2_Steam/donnees/` | 63 688 273 | `7a8b65ce162d6297a4c79cae8a35481a45476b2f1a85d8f288634909c4569821` |

Chaque notebook lit son fichier par un chemin relatif : `./Speed+Dating+Data.csv` (cellule 3) et `donnees/steam_game_output.json` (`steam_analyse.py`, constante `CHEMIN_LOCAL`). Vérifier l'empreinte avant d'exécuter :

```bash
sha256sum 1_Tinder/Speed+Dating+Data.csv 2_Steam/donnees/steam_game_output.json
# PowerShell : Get-FileHash -Algorithm SHA256 <fichier>
```

Le fichier `.gitignore` du bloc empêche de versionner ces fichiers par erreur une fois déposés. Le dictionnaire des variables du jeu Speed dating (`Speed Dating Data Key.doc`) n'est pas joint non plus : le notebook ne le lit pas. La notice [`2_Steam/donnees/LIRE_DONNEES.md`](2_Steam/donnees/LIRE_DONNEES.md) reprend ces informations pour Steam.

- **Speed dating : des données personnelles pseudonymisées.** Âge, études, code postal, revenus du quartier et notes se rapportent à des personnes identifiables, même sans nom ; l'origine déclarée et l'importance accordée à la religion relèvent des catégories particulières de l'article 9 du RGPD (cellule 66). Les bases d'analyse ne reprennent aucune de ces colonnes : `minimiser_colonnes` refuse de les garder, et un test le vérifie (`speed_dating.py`, `tests/test_speed_dating.py`, `tests/test_notebook_tinder.py`). Seuls des résultats agrégés sont affichés.
- **Steam : aucune donnée de joueur.** Avis et propriétaires sont des compteurs agrégés ; un éditeur ou un développeur indépendant peut toutefois être une personne physique, et son nom n'est alors utilisé que pour des comptages (cellule 44, section 9).

## 3. Méthode

### Speed dating (pandas)

- **Trois bases, une par unité d'analyse** : la décision (8 378 lignes), le rendez-vous (4 184) et la personne (551) ; chaque test porte sur son unité naturelle. Chaque rendez-vous apparaît deux fois dans le fichier, une ligne de chaque côté : 8 368 lignes appariées à leur ligne miroir, plus 10 lignes sans partenaire identifié (cellule 12).
- **Nettoyage sans imputation** : les notes hors de l'échelle 1 à 10 deviennent manquantes, car imputer inventerait des jugements (cellule 0, « Choix » ; cellule 9).
- **Régressions logistiques par sexe** à erreurs standard groupées à deux voies (participant et partenaire), une taille d'effet à côté de chaque p-valeur, correction de Bonferroni par famille de tests (cellules 30 et 57).

### Steam (PySpark)

- **Lecture et aplatissement d'un JSON imbriqué** : `getField`, `explode`, fenêtres (cellule 0, « Choix ») ; le champ `tags` compte 441 sous-champs (cellule 3).
- **Classement par borne basse de Wilson** plutôt que par part brute d'avis positifs, et **part de gros succès** (au moins un million de propriétaires) avec son intervalle de confiance plutôt qu'un revenu médian, que le découpage en tranches de propriétaires rendrait artificiel (cellule 0, « Choix » ; cellule 30).
- **Expérience de parallélisme à protocole fixe** : le même calcul avec 1 à 24 partitions, un essai de chauffe puis cinq essais chronométrés, médiane retenue ; résultat recoupé avec pandas, tag par tag (cellules 41 et 42).

## 4. Résultats chiffrés

### Speed dating

| Mesure | Résultat | Source |
|---|---|---|
| Fichier brut | 8 378 lignes × 195 colonnes, 551 participants, 21 vagues | cellule 3 |
| Taux de oui ; taux de match | 41,99 % des 8 378 décisions ; 16,49 % des 4 184 rendez-vous | cellule 17 |
| Effet de l'attirance sur le oui (rapport de cotes pour + 1 écart-type) | femmes 2,24 (3 419 décisions) ; hommes 3,48 (3 545 décisions) | cellule 30 |
| Goûts partagés ; fun | femmes 1,81 et 1,74 ; hommes 1,83 et 1,63 | cellule 30 |
| Six tests, seuil de Bonferroni 0,05 / 6 = 0,0083 | 3 hypothèses nulles rejetées, 3 conservées | cellule 57 |
| Auto-évaluation de l'attirance contre note reçue | 73,2 % des 542 personnes se surestiment ; t apparié = 13,48, p = 6,20e-36, d = 0,58 | cellule 56 |

**Pourquoi l'unité d'analyse compte** : calculé à tort sur 8 220 lignes où chaque rendez-vous compte deux fois, le test sur la proximité des centres d'intérêt donne p = 0,0247, « significatif à 5 % » ; sur une ligne par rendez-vous (4 105), p = 0,177 (cellule 52).

**Réponse** (cellule 65) : on obtient un second rendez-vous quand on est trouvé attirant, drôle et qu'on donne le sentiment de goûts partagés, les trois seuls critères dont l'effet positif sur le oui est démontré pour les deux sexes ; l'origine commune, la proximité des centres d'intérêt et le rang du rendez-vous n'ont pas d'effet démontré.

### Steam

| Mesure | Résultat | Source |
|---|---|---|
| Lecture | 55 691 enregistrements, 16 partitions ; 55 690 jeux retenus | cellules 3 et 7 |
| Part de gros succès parmi les jeux payants | massivement multijoueur 4,08 % [2,84 ; 5,84] ; action 2,38 % ; indépendant 0,81 % ; casual 0,35 % | cellule 30 |
| Plateformes | Windows 99,97 % ; Mac 22,93 % ; Linux 15,19 % | cellule 34 |
| Langues | 52,4 % des jeux en anglais seulement ; 24,1 % disponibles en français | cellule 20 |

**Traitement parallélisé** (exécution locale, 24 cœurs disponibles : cellules 2 et 41) :

| Partitions | 1 | 2 | 4 | 8 | 16 | 24 |
|---|---|---|---|---|---|---|
| Temps médian de 5 essais (s) | 3,75 | 2,40 | 1,67 | 1,42 | 1,37 | 1,39 |
| Accélération | 1,00 | 1,57 | 2,25 | 2,64 | 2,73 | 2,70 |

Le temps est divisé par 2,7 au mieux, pas par 24 : une part du travail ne se parallélise pas (loi d'Amdahl). pandas obtient les mêmes 441 totaux, à 0 vote près, en 0,06 s, par un autre algorithme : l'expérience montre que Spark répartit un calcul lourd entre les cœurs, pas qu'il bat pandas sur un fichier de 64 Mo (cellules 42 et 44). Ces durées dépendent de la machine et de sa charge.

### Qualité du code

| Mesure | Résultat | Source |
|---|---|---|
| Tests de mutation, `speed_dating.py` | 328 mutants, 328 tués | [`1_Tinder/MUTANTS_EQUIVALENTS.md`](1_Tinder/MUTANTS_EQUIVALENTS.md) |
| Tests de mutation, `steam_analyse.py` | 968 mutants ; 3 survivants, tous équivalents (`legend=None` au lieu de `legend=False`), nommés et expliqués | [`2_Steam/MUTANTS_EQUIVALENTS.md`](2_Steam/MUTANTS_EQUIVALENTS.md) |

## 5. Reproduire

Prérequis : Python 3.12 (version inscrite dans les métadonnées des deux notebooks). Aucune installation de Java n'est nécessaire pour Steam : le paquet `jdk4py` en fournit un. Dans chaque dossier de projet :

```bash
python -m pip install -r requirements.txt
# déposer le fichier de données à l'emplacement du § 2, puis ouvrir le notebook depuis ce dossier
```

- Le module voisin (`speed_dating.py` ou `steam_analyse.py`) doit rester à côté du notebook, qui l'importe en cellule 2.
- Les tests marqués `notebook` exécutent le notebook entier et ont besoin du fichier de données ; les autres éprouvent le module seul :

```bash
python -m pytest -m "not notebook"                                                    # module seul
python -m pytest -W error::DeprecationWarning -W error::PendingDeprecationWarning   # tout, données présentes
```

- **Databricks** : le notebook Steam choisit seul sa session et son chemin de lecture sur Databricks, mais cette branche est écrite et commentée, **non exécutée** ; les sorties enregistrées et les captures de [`2_Steam/captures/`](2_Steam/captures/) viennent de l'exécution locale (cellule 44, section 10).

## 6. Limites

- **Speed dating** : données de 2002 à 2004, étudiants d'une université américaine, rencontres hétérosexuelles en face à face ; le match approche le second rendez-vous sans le mesurer ; corrélation n'est pas causalité ; 15 à 18 % des décisions sont écartées des régressions faute d'une note complète, sans imputation ; le test d'interaction ne démontre pas d'effet des préférences déclarées, ce qui ne prouve pas qu'il n'en existe aucun (cellule 65, « Limites de l'étude »).
- **Steam** : instantané du catalogue à novembre 2022 ; aucun chiffre de ventes (propriétaires estimés en tranches larges, prix au jour de l'extraction) ; les avis ne mesurent pas la qualité de façon neutre ; corrélations, pas causalités (cellule 44, section 8).
- **Recommandations** : des associations observées, à valider par des tests (A/B, prix) avant d'engager un budget (cellules 44 et 65).
- **Durées de Spark** : mesurées sur une seule machine, en local ; elles varient d'une exécution à l'autre.

## 7. Organisation du dossier

```text
README.md                           cette page
.gitignore                          exclut les fichiers de données et les caches
1_Tinder/
|-- Speed_Dating_Bloc2_CDSD.ipynb   notebook exécuté (68 cellules)
|-- speed_dating.py                 nettoyage, appariement, régression à erreurs groupées, tailles d'effet
|-- tests/                          tests du module et du notebook
`-- requirements.txt, pytest.ini, MUTANTS_EQUIVALENTS.md
2_Steam/
|-- Steam_Bloc2_CDSD_PySpark.ipynb  notebook exécuté (45 cellules)
|-- steam_analyse.py                session Spark, aplatissement, éditeurs, graphiques, parallélisme
|-- captures/                       11 figures des sorties de l'exécution locale
|-- donnees/                        notice des données (le fichier lui-même n'est pas versionné)
|-- tests/                          tests du module et du notebook
`-- requirements.txt, pytest.ini, MUTANTS_EQUIVALENTS.md
```
