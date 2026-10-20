# Mutants équivalents — bloc 6

Un mutant est **équivalent** quand aucun test ne peut le distinguer de l'original, parce que les deux programmes
se comportent de la même façon pour toute entrée. Cette liste nomme chacun des mutants que `mutmut 3.8.0` laisse
survivre sur `src/banking_dataops/`, avec la preuve. Les numéros (`#N`) sont ceux de `mutmut` pour la version du code
de ce commit ; ils changent si le code de la fonction change, le texte du changement permet de les retrouver.

Les faits de bibliothèque sur lesquels reposent les preuves sont vérifiés par
`tests/test_equivalent_mutants_premises.py` (un cas par fait) : si une mise à jour d'une bibliothèque change l'un
d'eux, ce test échoue et le mutant correspondant est à réexaminer.

Mesure : `mutmut` 3.8.0 sous WSL (Ubuntu), `JOBLIB_MULTIPROCESSING=0`, 4 processus, priorité minimale. Un mutant dont le seul effet est un délai dépassé n'est pas listé ici : les tests le tuent
(garde-fou `limit` de `tests/conftest.py`).

Nombre de mutants équivalents : **41** sur 3 967 mutants (3 926 tués, 41 survivants, tous listés ci-dessous), mesure du
12 octobre 2026 sur tout `src/banking_dataops/` (18 modules). Un outil de mutation compte ces 41 survivants comme non tués : il n'a pas
de mécanisme pour exclure un mutant équivalent, d'où un score de 3 926 sur 3 967 et non de 100 %.

Un mutant ne doit pas dépendre de l'heure à laquelle on lance la mesure. Un survivant, `parse_anchor` avec
`datetime.now(None)` à la place de `datetime.now(UTC)`, n'était tué que lorsque le jour local différait du jour UTC (une
mesure à 23 h 57 le tuait, une mesure à 00 h 26 non) ; le test règle maintenant une horloge à 23 h 30 à UTC-5 et ne
dépend plus de l'heure réelle.

## `ml/train.py` (6)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `_build_estimator` #5 | l'argument `remainder="drop"` de `ColumnTransformer` est retiré | `"drop"` est la valeur par défaut de `ColumnTransformer.remainder` (fait vérifié : premisse « sklearn : ColumnTransformer… »). L'argument explicite documente le choix, il ne le change pas. |
| `_choose_threshold` | `2 * precision * recall` devient `3 * precision * recall` | `f1` n'est lu que par `np.argmax`. Un facteur positif multiplie toutes les valeurs de la même façon : l'ordre, donc l'indice du maximum, ne change pas (premisse « numpy : un facteur positif… », vérifiée sur 200 courbes aléatoires). Aucun test ne lit la valeur de `f1`. |
| `evaluate_once` #59, #62, #72 | `strict=True` devient `strict=None`, `strict=False` ou disparaît dans `zip(CONFUSION_LABELS, valeurs)` | `strict` ne change le comportement que si les deux séquences n'ont pas la même longueur. Ici `CONFUSION_LABELS` a quatre entrées et la matrice 2×2 de `confusion_matrix(..., labels=[False, True])` en a toujours quatre, même quand `y_test` ne contient qu'une classe (premisse « sklearn : une matrice 2×2… »). |
| `train` #147 | `round(threshold, 4)` devient `round(threshold, 5)` dans l'événement `threshold_selected` | `threshold` sort de `out_of_fold_threshold` déjà arrondi à trois décimales (`THRESHOLD_DECIMALS`). Arrondir à quatre ou cinq décimales une valeur à trois décimales la rend identique (premisse « python : arrondir… »). |

## `api.py` (1)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `controls` #10 | `to_dict(orient="records")` devient `orient="RECORDS"` | pandas lit l'argument `orient` sans tenir compte de la casse (premisse « pandas : l'argument orient… »). |

## `audit.py` (3)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `fingerprint_directory` #9 | `.encode("utf-8")` devient `.encode("UTF-8")` | Python normalise le nom du codec : `"UTF-8"` et `"utf-8"` désignent le même codec (premisse « python : les noms de codec… »). |
| `fingerprint_directory` #14 | `.encode("ascii")` devient `.encode("ASCII")` | Idem, même premisse. |
| `_as_json` #3 | `ensure_ascii=False` devient `ensure_ascii=None` | `json.dumps` teste `ensure_ascii` comme un booléen : `None` est faux, comme `False` (premisse « json : ensure_ascii=None… »). |

## `db.py` (7) — `split_sql_statements`

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| #5 | `in_single = False` devient `None` | Le drapeau n'est lu que dans des contextes booléens (`and in_single`, `not in_single`) ; `None` et `False` y sont tous deux faux et `not None` est vrai, comme `not False`. |
| #7 | `in_line = False` devient `None` | Même raisonnement : `if in_line:` seulement. |
| #9 | `in_block = False` devient `None` | Même raisonnement : `if in_block:` seulement. |
| #20 | `in_line = False` devient `None` en fin de commentaire de ligne | Même raisonnement. |
| #32 | `in_block = False` devient `None` en fin de commentaire de bloc | Même raisonnement. |
| #74 | `tag_start = i + 1 < len(...)` devient `i + 2 < len(...)` | Les deux ne diffèrent que si le texte finit juste après le premier caractère d'une balise (`... $a`). Dans les deux cas, la balise n'a pas de dollar fermant : elle n'ouvre rien, le `$` est copié comme un caractère ordinaire et le résultat est le même (cas `"SELECT $a"` et `"SELECT $abc"` dans `tests/test_sql_splitter_edges.py`, qui passent avec l'original). |
| #90 | `j = i + 1` devient `j = i + 2` au début de la lecture du nom de balise | La lecture avance tant que le caractère est alphanumérique ou `_`. Le caractère d'indice `i + 1` est déjà vérifié alphabétique ou `_` par `tag_start` : le sauter ne change pas l'indice où la lecture s'arrête. |

## `generate_synthetic_data.py` (1)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `parse_args` #78 | `default=None` retiré de `add_argument("--generated-at", ...)` | `None` est la valeur par défaut d'`argparse` pour une option sans défaut (premisse « argparse : une option sans défaut… »). |

## `logging_config.py` (7)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `HumanFormatter.format` #13 | `getattr(record, "run_id", "")` devient `getattr(record, "run_id", None)` | Le résultat passe par `or "-" * 8` : `None` et `""` sont tous deux faux et donnent les huit tirets (premisse « python : None et la chaîne vide… »). |
| `HumanFormatter.format` #22 | `"-" * 8` devient `"-" * 9` | Le résultat est ensuite tronqué par `run_id[:8]` : neuf tirets tronqués à huit en font huit (premisse « python : tronquer neuf tirets… »). |
| `HumanFormatter.format` #27 | `getattr(record, "step", "")` devient `getattr(record, "step", None)` | Même raisonnement que #13, avec `or "-"`. |
| `JsonFormatter.format` #48 | `ensure_ascii=False` devient `None` | Même fait que `audit._as_json` #3. |
| `configure_logging` #9 | `"INFO"` devient `"info"` | La chaîne passe par `.upper()` juste après : `"info".upper() == "INFO"`. |
| `configure_logging` #17 | `"human"` devient `"XXhumanXX"` | Le format n'est comparé qu'à `"json"` : toute autre valeur donne le format lisible. |
| `configure_logging` #18 | `"human"` devient `"HUMAN"` | Même raisonnement que #17, après `.lower()`. |

## `pipeline/cleaning.py` (7)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `clean_transactions` #67 | `report.currency_normalised += n` devient `= n` | Le rapport est créé neuf dans la même fonction et le compteur part de 0 (premisse « dataclass : un rapport neuf… ») ; il n'est incrémenté qu'une fois. |
| `clean_transactions` #83 | `report.status_normalised += n` devient `= n` | Même raisonnement. |
| `clean_transactions` #97 | `keep="first"` retiré de `duplicated(...)` | `"first"` est la valeur par défaut de pandas (premisse « pandas : drop_duplicates garde… »). |
| `clean_bronze_to_silver` #47 | `max(report.rows_in, 1)` devient `max(report.rows_in, 2)` | Le seul cas où les deux diffèrent est `rows_in == 1`. Avec une seule ligne en entrée, l'invariant précédent (`rows_out > 0`) impose `rows_out == 1`, donc zéro ligne en quarantaine : le taux vaut 0 dans les deux cas. |
| `clean_bronze_to_silver` #72 | `to_csv(..., index=False)` devient `index=None` (transactions) | pandas traite `None` comme faux : aucun index n'est écrit (premisse « pandas : to_csv(index=None)… »). |
| `clean_bronze_to_silver` #80 | idem pour `quarantine.csv` | Même raisonnement. |
| `clean_bronze_to_silver` #102 | idem pour `customers_clean.csv` et `accounts_clean.csv` | Même raisonnement. |

## `pipeline/gold.py` (3)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `build` #70 | `cast("int", ...)` devient `cast(None, ...)` | `typing.cast` renvoie son second argument sans rien en faire ; le premier est une indication pour le vérificateur de types (premisse « typing : cast renvoie… »). |
| `build` #74 | `cast("XXintXX", ...)` | Même raisonnement. |
| `build` #75 | `cast("INT", ...)` | Même raisonnement. |

## `quality_checks.py` (6)

| Mutant | Changement | Pourquoi il est identique |
|---|---|---|
| `get_quality_checks` #10, #27, #44, #61, #78, #95 | l'argument `warn_only=False` est retiré d'une des six définitions bloquantes | `False` est la valeur par défaut du champ `warn_only` de `QualityCheck` (premisse « dataclass : un contrôle est bloquant… »). L'argument explicite dit au lecteur que le contrôle bloque. Le test `test_the_seven_controls_are_declared_in_order_with_their_exact_sql` vérifie que les six valent bien `False`. |

## Ce que cette liste ne couvre pas

- Les mutants de `ml/predict.py` : 184 tués sur 184.
- `config.py` : aucune fonction mutable (`Settings` est une classe `pydantic` dont la seule méthode est une propriété
  décorée, que `mutmut` ne mute pas ; `load_settings` n'a rien à muter).
- Le code hors de `src/banking_dataops/` : `src/ingestion`, `src/cleaning`, `src/transformations`, `src/ml` ne contiennent que
  des réexportations (`tests/test_path_aliases.py`) ; `run_pipeline.py`, `scripts/`, `dashboard/` et `huggingface-space/`
  ne sont pas dans le périmètre `src/` demandé.
