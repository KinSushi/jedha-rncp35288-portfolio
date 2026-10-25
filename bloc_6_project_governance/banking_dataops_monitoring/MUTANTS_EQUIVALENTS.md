# Mutants équivalents : bloc 6

Un mutant est équivalent quand aucun test ne peut le distinguer de l'original, parce que les deux programmes
se comportent de la même façon pour toute entrée. Cette liste nomme chacun des mutants que `mutmut 3.8.0` laisse
survivre sur `src/banking_dataops/`, avec la preuve. Chaque entrée porte un numéro d'ordre (N°, de 1 à 40) et le
numéro de `mutmut` (`#N`), qui est celui de la version du code mesurée : il change si le code de la fonction
change, le texte du changement permet alors de le retrouver. Le nom complet d'un mutant pour `mutmut show` est
`banking_dataops.<module>.x_<fonction>__mutmut_<N>` (`xǁ<Classe>ǁ<méthode>__mutmut_<N>` pour une méthode).

La preuve est la lecture du code, écrite dans la dernière colonne. Certaines preuves s'appuient sur un fait (une valeur
par défaut, un alias de codec, la valeur de vérité de `None`) : chacun de ces faits est une prémisse, vérifiée par un
cas de `tests/test_equivalent_mutants_premises.py`. La colonne « Prémisse vérifiée » donne, mot pour mot, la clé de ce
cas ; si une mise à jour change l'un de ces faits, ce cas échoue et les mutants qui s'y appuient sont à réexaminer. Une
prémisse vérifiée n'est pas une preuve : celle qui couvrait `_choose_threshold` #18 était vraie sur ses tirages et le
mutant n'était pas équivalent (voir plus bas).

## Environnement de la mesure

Mesure du 20 octobre 2026 par la porte de qualité du dossier (toutes couches), sur la version du code qui porte ce
fichier (le commit mesuré est consigné avec la mesure).

| Élément | Version |
|---|---|
| Mutation | `mutmut` 3.8.0 sous WSL (Ubuntu), `JOBLIB_MULTIPROCESSING=0`, 4 processus |
| Python (mutation, WSL) | 3.12.15 |
| numpy, pandas, scikit-learn (mutation, WSL) | numpy 2.5.4, pandas 3.0.6, scikit-learn 1.9.1 |
| Python et bibliothèques de la suite de tests (Windows) | Python 3.12.14 ; numpy 2.5.4, pandas 3.0.6, scikit-learn 1.9.1 |

`requirements.txt` ne fixe que des bornes basses (`pandas>=2.0`, `scikit-learn>=1.4`, numpy sans borne propre). La
colonne « Repose sur » dit de quoi dépend chaque équivalence :

- scikit-learn ou pandas : un comportement de la bibliothèque (N° 1 à 4, 6, 27, 29 à 31). Établi pour les
  versions ci-dessus seulement.
- Python : un comportement du langage ou de sa bibliothèque standard (N° 2 à 5, 7 à 9, 17 à 22, 32 à 34). Établi
  pour Python 3.12.
- code du projet : la lecture du code de la fonction seule, ou une valeur par défaut déclarée dans le projet (N° 10 à
  16, 23 à 26, 28, 35 à 40).

Sur une autre version, relancer `tests/test_equivalent_mutants_premises.py`, puis la mesure.

Un mutant qui fait tourner un test sans fin n'est pas listé ici : le garde-fou `limit` de `tests/conftest.py` arrête le
test, et le mutant est tué.

## Comptes

Nombre de mutants équivalents : 40 sur 3 967 mutants (3 927 tués, 40 survivants, tous listés ci-dessous),
mesure du 20 octobre 2026 sur tout `src/banking_dataops/` (18 modules). Un outil de mutation compte ces 40 survivants comme
non tués : il n'a pas de mécanisme pour exclure un mutant équivalent, d'où un score de 3 927 sur 3 967 et non de 100 %.

Sous la charge d'autres mesures, la porte peut classer en délai dépassé un mutant qui relance les tests d'entraînement
ou une boucle : un délai dépassé n'est ni un tué ni un survivant, il est repris seul jusqu'à verdict. Les reprises de la
mesure sont conservées avec elle.

Mesure précédente (12 octobre 2026) : 3 967 mutants, 3 926 tués, 41 survivants déclarés équivalents. Un contrôle
indépendant (19 octobre 2026) a confirmé 40 de ces équivalences et trouvé que la 41ᵉ, `ml.train._choose_threshold` #18,
ne l'était pas : elle est maintenant tuée par un test.

Un mutant ne doit pas dépendre de l'heure à laquelle on lance la mesure. Un survivant, `parse_anchor` avec
`datetime.now(None)` à la place de `datetime.now(UTC)`, n'était tué que lorsque le jour local différait du jour UTC (une
mesure à 23 h 57 le tuait, une mesure à 00 h 26 non) ; le test règle maintenant une horloge à 23 h 30 à UTC-5 et ne
dépend plus de l'heure réelle.

## Mutant retiré de la liste : `ml/train.py`, `_choose_threshold` #18 (tué)

Changement : `2 * precision * recall` devient `3 * precision * recall` dans le calcul du F1.

La preuve donnée jusqu'au 12 octobre (« un facteur positif ne change pas l'ordre, donc pas l'indice du maximum ») est
vraie en arithmétique exacte, pas en virgule flottante. Deux points d'une vraie courbe ont souvent exactement le même F1
(F1 = 2 VP / (2 VP + FP + FN)), et `np.argmax` garde le premier des maximums calculés, c'est-à-dire le seuil le plus
bas parmi les points dont les valeurs calculées sont identiques. Or le F1 est calculé à partir de la précision et du
rappel, déjà arrondis : deux F1 égaux en valeur exacte peuvent différer d'un bit, et ce bit dépend du facteur. Sur
certaines entrées, l'original (facteur 2) calcule les valeurs de l'égalité à l'identique et rend le seuil le plus bas,
alors que le mutant (facteur 3) les arrondit différemment (0,6 contre 0,6000000000000001) et rend un seuil plus haut :
le mutant est donc distinguable. La prémisse « numpy : un facteur positif… » ne tirait que des valeurs continues, qui ne
sont jamais à égalité ; elle est retirée de `tests/test_equivalent_mutants_premises.py`.

Limite de l'original, connue et non corrigée. Le facteur 2 ne rend pas identiques toutes les égalités exactes :
l'original lui-même ne rend pas toujours le seuil le plus bas d'une égalité exacte de F1. Sur des courbes tirées au
hasard (4 à 39 lignes, scores à trois décimales), le seuil rendu n'était pas le plus bas des F1 exactement égaux dans
141 cas sur 19 874 (graine 0, mesure du 20 octobre 2026) et dans 35 cas sur 4 966 (graine 12345, contrôle indépendant
du même jour). Exemple : 20 lignes, F1 = 2/5 aux seuils 0,006 et 0,755 ; l'original rend 0,755. La docstring de
`_choose_threshold` le dit. Un remède (F1 calculé à partir des comptes entiers, une seule division) toucherait le calcul
du seuil publié ; son effet sur les données du projet n'est pas mesuré, et la décision n'est pas prise.

Test qui tue le mutant : `test_choose_threshold_returns_the_lowest_threshold_of_a_tie_whose_f1_is_not_exact_in_binary`
(`tests/test_ml_training_contract.py`, deux entrées sur lesquelles l'original rend le seuil le plus bas, 0,1 et 0,034,
et le mutant 0,6 et 0,433), avec son pendant `test_choose_threshold_does_not_treat_a_slightly_better_f1_as_a_tie`. Ces
tests fixent des entrées, pas la règle pour toute entrée.

## `ml/train.py` (5)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 1 | `_build_estimator` #5 | l'argument `remainder="drop"` de `ColumnTransformer` est retiré | scikit-learn | « sklearn: ColumnTransformer drops the unlisted columns by default » | `"drop"` est la valeur par défaut de `ColumnTransformer.remainder`. L'argument explicite documente le choix, il ne le change pas. |
| 2 | `evaluate_once` #59 | `zip(CONFUSION_LABELS, valeurs, strict=True)` : `strict=True` devient `strict=None` | scikit-learn, Python | « sklearn: a 2x2 confusion matrix has four cells even for a single class » | `strict` ne change le comportement que si les deux séquences n'ont pas la même longueur. Ici `CONFUSION_LABELS` a quatre entrées et la matrice 2×2 de `confusion_matrix(..., labels=[False, True])` en a toujours quatre, même quand `y_test` ne contient qu'une classe. `zip` accepte `strict=None`. |
| 3 | `evaluate_once` #62 | même `zip` : l'argument `strict=True` est retiré | scikit-learn, Python | même prémisse que N° 2 | Même raisonnement que N° 2. |
| 4 | `evaluate_once` #72 | même `zip` : `strict=True` devient `strict=False` | scikit-learn, Python | même prémisse que N° 2 | Même raisonnement que N° 2. |
| 5 | `train` #147 | `round(threshold, 4)` devient `round(threshold, 5)` dans l'événement `threshold_selected` | Python | « python: rounding to four or five decimals a value already rounded to three changes nothing » | `threshold` sort de `out_of_fold_threshold` déjà arrondi à trois décimales (`THRESHOLD_DECIMALS`). Arrondir à quatre ou cinq décimales une valeur à trois décimales la rend identique ; la valeur ne va que dans le journal. |

## `api.py` (1)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 6 | `controls` #10 | `to_dict(orient="records")` devient `orient="RECORDS"` | pandas | « pandas: the orient argument of to_dict is case-insensitive » | pandas lit l'argument `orient` sans tenir compte de la casse : seul `orient != "tight"` est évalué avant `orient.lower()`. |

## `audit.py` (3)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 7 | `fingerprint_directory` #9 | `.encode("utf-8")` devient `.encode("UTF-8")` | Python (codecs) | « python: codec names are case-insensitive (UTF-8, ASCII) » | Python normalise le nom du codec : `"UTF-8"` et `"utf-8"` désignent le même codec. |
| 8 | `fingerprint_directory` #14 | `.encode("ascii")` devient `.encode("ASCII")` | Python (codecs) | même prémisse que N° 7 | Même raisonnement que N° 7. |
| 9 | `_as_json` #3 | `ensure_ascii=False` devient `ensure_ascii=None` | Python (json) | « json: ensure_ascii=None behaves as False » | `json.dumps` teste `ensure_ascii` comme un booléen : `None` est faux, comme `False`. |

## `db.py` (7) : `split_sql_statements`

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 10 | #5 | `in_single = False` devient `None` | code du projet | - | Le drapeau n'est lu que dans des contextes booléens (`and in_single`, `not in_single`) ; `None` et `False` y sont tous deux faux et `not None` est vrai, comme `not False`. |
| 11 | #7 | `in_line = False` devient `None` | code du projet | - | Même raisonnement : `if in_line:` seulement. |
| 12 | #9 | `in_block = False` devient `None` | code du projet | - | Même raisonnement : `if in_block:` seulement. |
| 13 | #20 | `in_line = False` devient `None` en fin de commentaire de ligne | code du projet | - | Même raisonnement que N° 11. |
| 14 | #32 | `in_block = False` devient `None` en fin de commentaire de bloc | code du projet | - | Même raisonnement que N° 12. |
| 15 | #74 | `tag_start = i + 1 < len(...)` devient `i + 2 < len(...)` | code du projet | - | Les deux ne diffèrent que si le texte finit juste après le premier caractère d'une balise (`... $a`). Dans les deux cas, la balise n'a pas de dollar fermant : elle n'ouvre rien, le `$` est copié comme un caractère ordinaire et le résultat est le même (cas `"SELECT $a"` et `"SELECT $abc"` dans `tests/test_sql_splitter_edges.py`, qui passent avec l'original). |
| 16 | #90 | `j = i + 1` devient `j = i + 2` au début de la lecture du nom de balise | code du projet | - | La lecture avance tant que le caractère est alphanumérique ou `_`. Le caractère d'indice `i + 1` est déjà vérifié alphabétique ou `_` par `tag_start` : le sauter ne change pas l'indice où la lecture s'arrête. |

## `generate_synthetic_data.py` (1)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 17 | `parse_args` #78 | `default=None` retiré de `add_argument("--generated-at", ...)` | Python (argparse) | « argparse: an option without default has the default None » | `None` est la valeur par défaut d'`argparse` pour une option sans défaut, le parseur n'ayant ni `argument_default` ni `set_defaults`. |

## `logging_config.py` (7)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 18 | `HumanFormatter.format` #13 | `getattr(record, "run_id", "")` devient `getattr(record, "run_id", None)` | Python | « python: None and the empty string are both falsy in an `or` fallback » | Le résultat passe par `or "-" * 8` : `None` et `""` sont tous deux faux et donnent les huit tirets. |
| 19 | `HumanFormatter.format` #22 | `"-" * 8` devient `"-" * 9` | Python | « python: slicing nine dashes to eight gives eight dashes » | Le résultat est ensuite tronqué par `run_id[:8]` : neuf tirets tronqués à huit en font huit. |
| 20 | `HumanFormatter.format` #27 | `getattr(record, "step", "")` devient `getattr(record, "step", None)` | Python | même prémisse que N° 18 | Même raisonnement que N° 18, avec `or "-"`. |
| 21 | `JsonFormatter.format` #48 | `ensure_ascii=False` devient `None` | Python (json) | même prémisse que N° 9 | Même fait que N° 9. |
| 22 | `configure_logging` #9 | `"INFO"` devient `"info"` | Python | « python: str.upper() makes 'info' and 'INFO' the same level name » | La chaîne passe par `.upper()` juste après : `"info".upper() == "INFO"`. |
| 23 | `configure_logging` #17 | `"human"` devient `"XXhumanXX"` | code du projet | - | Le format n'est comparé qu'à `"json"` : toute autre valeur donne le format lisible. |
| 24 | `configure_logging` #18 | `"human"` devient `"HUMAN"` | code du projet | - | Même raisonnement que N° 23, après `.lower()`. |

## `pipeline/cleaning.py` (7)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 25 | `clean_transactions` #67 | `report.currency_normalised += n` devient `= n` | code du projet | « dataclass: a fresh cleaning report starts every counter at zero » | Le rapport est créé neuf dans la même fonction et le compteur part de 0 ; il n'est incrémenté qu'une fois. |
| 26 | `clean_transactions` #83 | `report.status_normalised += n` devient `= n` | code du projet | même prémisse que N° 25 | Même raisonnement que N° 25. |
| 27 | `clean_transactions` #97 | `keep="first"` retiré de `duplicated(...)` | pandas | « pandas: duplicated marks every row but the first by default (keep='first') » | `"first"` est la valeur par défaut de l'argument `keep` de `DataFrame.duplicated` : sans lui, les mêmes lignes sont marquées. |
| 28 | `clean_bronze_to_silver` #47 | `max(report.rows_in, 1)` devient `max(report.rows_in, 2)` | code du projet | - | Les deux ne diffèrent que si `rows_in` vaut 0 ou 1. Avec 0, l'invariant précédent (`rows_out > 0`) arrête le run avant. Avec 1, il impose `rows_out == 1`, donc zéro ligne en quarantaine : le taux vaut 0 dans les deux cas. |
| 29 | `clean_bronze_to_silver` #72 | `to_csv(..., index=False)` devient `index=None` (transactions) | pandas | « pandas: to_csv(index=None) writes no index, like index=False » | pandas traite `None` comme faux : aucun index n'est écrit. |
| 30 | `clean_bronze_to_silver` #80 | idem pour `quarantine.csv` | pandas | même prémisse que N° 29 | Même raisonnement que N° 29. |
| 31 | `clean_bronze_to_silver` #102 | idem pour `customers_clean.csv` et `accounts_clean.csv` | pandas | même prémisse que N° 29 | Même raisonnement que N° 29. |

## `pipeline/gold.py` (3)

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 32 | `build` #70 | `cast("int", ...)` devient `cast(None, ...)` | Python (typing) | « typing: cast returns its second argument whatever the first » | `typing.cast` renvoie son second argument sans rien en faire ; le premier est une indication pour le vérificateur de types. |
| 33 | `build` #74 | `cast("int", ...)` devient `cast("XXintXX", ...)` | Python (typing) | même prémisse que N° 32 | Même raisonnement que N° 32. |
| 34 | `build` #75 | `cast("int", ...)` devient `cast("INT", ...)` | Python (typing) | même prémisse que N° 32 | Même raisonnement que N° 32. |

## `quality_checks.py` (6)

L'argument `warn_only=False` est retiré d'une des six définitions bloquantes de `get_quality_checks` (`CTRL-001` à `CTRL-006`). `False` est la
valeur par défaut du champ `warn_only` de `QualityCheck`, une `@dataclass(frozen=True)` du projet : l'objet construit est
identique. L'argument explicite dit au lecteur que le contrôle bloque. Le test
`test_the_seven_controls_are_declared_in_order_with_their_exact_sql` vérifie que les six valent bien `False`.

| N° | Mutant | Changement | Repose sur | Prémisse vérifiée | Pourquoi il est identique |
|---|---|---|---|---|---|
| 35 | `get_quality_checks` #10 | `warn_only=False` retiré de `CTRL-001` | code du projet | « dataclass: a control is blocking (warn_only False) unless declared otherwise » | Valeur par défaut du champ (ci-dessus). |
| 36 | `get_quality_checks` #27 | idem pour `CTRL-002` | code du projet | même prémisse que N° 35 | Même raisonnement que N° 35. |
| 37 | `get_quality_checks` #44 | idem pour `CTRL-003` | code du projet | même prémisse que N° 35 | Même raisonnement que N° 35. |
| 38 | `get_quality_checks` #61 | idem pour `CTRL-004` | code du projet | même prémisse que N° 35 | Même raisonnement que N° 35. |
| 39 | `get_quality_checks` #78 | idem pour `CTRL-005` | code du projet | même prémisse que N° 35 | Même raisonnement que N° 35. |
| 40 | `get_quality_checks` #95 | idem pour `CTRL-006` | code du projet | même prémisse que N° 35 | Même raisonnement que N° 35. |

## Ce que cette liste ne couvre pas

- Les mutants de `ml/predict.py` : 184 tués sur 184.
- `config.py` : aucune fonction mutable (`Settings` est une classe `pydantic` dont la seule méthode est une propriété
  décorée, que `mutmut` ne mute pas ; `load_settings` n'a rien à muter).
- Le code hors de `src/banking_dataops/` : `src/ingestion`, `src/cleaning`, `src/transformations`, `src/ml` ne contiennent que
  des réexportations (`tests/test_path_aliases.py`) ; `run_pipeline.py`, `scripts/`, `dashboard/` et `huggingface-space/`
  ne sont pas dans le périmètre `src/` mesuré.
