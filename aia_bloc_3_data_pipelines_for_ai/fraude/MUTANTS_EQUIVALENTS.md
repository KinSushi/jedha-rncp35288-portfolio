# Mutants équivalents nommés : AIA bloc 3, détection de fraude

Copyright © 2026 · voir le fichier LICENSE du dépôt

Un mutant est une petite modification du code (un opérateur inversé, une valeur changée) faite par l'outil de mutation `mutmut`. Un mutant est **tué** quand au moins un test échoue. Un mutant qui survit est soit un trou dans les tests, soit un mutant **équivalent** : il ne change aucun comportement observable, donc aucun test ne peut le distinguer de l'original.

Ce fichier nomme les **87 mutants équivalents** qui restent, avec pour chacun la preuve de l'équivalence. Quand la preuve repose sur un fait de Python ou d'une bibliothèque, ce fait est contrôlé par un test de `tests/test_equivalent_mutants_premises.py` : si une mise à jour le change, le test échoue et le mutant est à réexaminer. Tous les autres mutants sont tués.

Les noms sont ceux de `mutmut 3` (`module.fonction__mutmut_N`, `ǁ` sépare la classe et la méthode). Le numéro N dépend du contenu de la fonction : il change si la fonction change.

## Par famille

### `json.dumps(..., ensure_ascii=False)` devient `ensure_ascii=None` (11)

- Mutation : le paramètre passe de `False` à `None`.
- Preuve : `json.dumps` ne lit `ensure_ascii` que comme une valeur de vérité : `None` est faux, comme `False`. Le texte produit est identique, octet pour octet (les accents restent écrits tels quels).
- Fait contrôlé par : test_json_dumps_lit_ensure_ascii_comme_un_booleen_none_vaut_false.
- Mutants :
  - `detection_fraude.entrainement.x_main__mutmut_92`
  - `detection_fraude.entrepot.xǁEntrepotǁenregistrer_rejet__mutmut_69`
  - `detection_fraude.entrepot.xǁEntrepotǁenregistrer_rejet__mutmut_82`
  - `detection_fraude.file_attente.xǁFileKafkaǁpublier__mutmut_29`
  - `detection_fraude.file_attente.xǁFileSQLiteǁpublier__mutmut_9`
  - `detection_fraude.finops.x_main__mutmut_134`
  - `detection_fraude.journal.xǁFormatJsonǁformat__mutmut_36`
  - `detection_fraude.modele.x_exporter_modele__mutmut_30`
  - `detection_fraude.notification.xǁCanalFichierǁenvoyer__mutmut_28`
  - `detection_fraude.producteur.x_ecrire_etat__mutmut_19`
  - `detection_fraude.supervision.x_main__mutmut_65`

### Nom de codec écrit en majuscules (`"utf-8"` devient `"UTF-8"`, `"ascii"` devient `"ASCII"`) (7)

- Mutation : seule la casse du nom de l'encodage change.
- Preuve : Python normalise le nom d'un codec avant de le chercher : `"UTF-8"` et `"utf-8"` désignent le même codec, les octets encodés ou décodés sont identiques.
- Fait contrôlé par : test_python_normalise_la_casse_des_noms_de_codec.
- Mutants :
  - `detection_fraude.configuration.xǁConfigurationǁsel_hachage__mutmut_39`
  - `detection_fraude.configuration.xǁConfigurationǁsel_hachage__mutmut_7`
  - `detection_fraude.file_attente.xǁFileKafkaǁlire__mutmut_30`
  - `detection_fraude.file_attente.xǁFileKafkaǁlire__mutmut_35`
  - `detection_fraude.file_attente.xǁFileKafkaǁpublier__mutmut_26`
  - `detection_fraude.file_attente.xǁFileKafkaǁpublier__mutmut_34`
  - `detection_fraude.masquage.xǁHacheurCarteǁempreinte__mutmut_14`

### Argument textuel d'une bibliothèque écrit dans une autre casse (8)

- Mutation : `float("nan")` devient `float("NAN")` ; `tz="UTC"` devient `tz="utc"` ; `to_dict("records")` devient `to_dict("RECORDS")` ; `"PRAGMA synchronous=NORMAL"` change de casse ; `kind="stable"` devient `kind="STABLE"`.
- Preuve : Chaque bibliothèque lit ces valeurs sans tenir compte de la casse : même NaN, même fuseau, même liste de dictionnaires, même réglage de SQLite (vérifié par la lecture du pragma), même tri stable de numpy. Pour `sort_values` sur deux colonnes, pandas n'utilise pas du tout `kind` (tri lexicographique toujours stable).
- Fait contrôlé par : test_float_lit_nan_sans_tenir_compte_de_la_casse, test_pandas_accepte_utc_en_minuscules_pour_un_fuseau, test_pandas_lit_l_orientation_records_sans_tenir_compte_de_la_casse, test_sqlite_lit_les_pragmas_sans_tenir_compte_de_la_casse, test_numpy_lit_le_genre_de_tri_sans_tenir_compte_de_la_casse, test_pandas_ignore_kind_pour_un_tri_sur_plusieurs_colonnes.
- Mutants :
  - `detection_fraude.entrainement.x_decouper_chronologiquement__mutmut_12`
  - `detection_fraude.entrainement.x_evaluer__mutmut_12`
  - `detection_fraude.entrainement.x_evaluer__mutmut_7`
  - `detection_fraude.file_attente.xǁFileSQLiteǁ__init____mutmut_24`
  - `detection_fraude.file_attente.xǁFileSQLiteǁ__init____mutmut_25`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_41`
  - `detection_fraude.jeu_donnees.x__millisecondes_utc__mutmut_15`
  - `detection_fraude.jeu_donnees.x_lignes_depuis_jeu__mutmut_21`

### Valeur par défaut d'une bibliothèque, écrite en toutes lettres dans le code (13)

- Mutation : l'argument est retiré, ou remplacé par `None`, ce qui revient à la valeur par défaut.
- Preuve : La valeur écrite dans le code EST la valeur par défaut : `FunctionTransformer(validate=False)`, `KBinsDiscretizer(encode="onehot")`, `OrdinalEncoder(encoded_missing_value=nan)` (scikit-learn) ; `gzip.open(..., "rb")` ; `logging.StreamHandler(sys.stderr)` ; `secrets.token_hex(32)` ; `np.ones(..., dtype="float64")` ; `np.searchsorted(..., side="left")` ; `OrderedDict.popitem(last=False)` (`None` est lu comme faux). Le comportement est donc identique.
- Fait contrôlé par : test_scikit_learn_a_pour_defauts_ceux_que_le_pipeline_ecrit_en_toutes_lettres, test_gzip_open_ouvre_en_lecture_binaire_par_defaut, test_un_gestionnaire_de_flux_sans_flux_ecrit_sur_stderr, test_secrets_token_hex_sans_taille_donne_trente_deux_octets, test_numpy_a_pour_defauts_float64_et_recherche_a_gauche, test_popitem_last_none_retire_le_plus_ancien_comme_last_false.
- Mutants :
  - `detection_fraude.configuration.xǁConfigurationǁsel_hachage__mutmut_30`
  - `detection_fraude.entrainement.x_construire_pipeline__mutmut_14`
  - `detection_fraude.entrainement.x_construire_pipeline__mutmut_22`
  - `detection_fraude.entrainement.x_construire_pipeline__mutmut_40`
  - `detection_fraude.entrainement.x_construire_pipeline__mutmut_79`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_11`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_13`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_54`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_63`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_73`
  - `detection_fraude.journal.x_configurer_journal__mutmut_16`
  - `detection_fraude.modele.x_lire_artefact__mutmut_4`
  - `detection_fraude.producteur.xǁProducteurǁ_deja_vu__mutmut_5`

### `kind="stable"` de `sort_values` sur deux colonnes retiré ou changé (3)

- Mutation : `kind` devient `None`, disparaît, ou devient `"XXstableXX"`.
- Preuve : Avec une liste de deux clés, pandas trie de façon lexicographique et stable sans lire `kind` : la valeur, même invalide, n'est jamais utilisée. Le découpage chronologique est identique.
- Fait contrôlé par : test_pandas_ignore_kind_pour_un_tri_sur_plusieurs_colonnes.
- Mutants :
  - `detection_fraude.entrainement.x_decouper_chronologiquement__mutmut_11`
  - `detection_fraude.entrainement.x_decouper_chronologiquement__mutmut_4`
  - `detection_fraude.entrainement.x_decouper_chronologiquement__mutmut_6`

### `zip(..., strict=True)` devient `strict=None`, `strict=False` ou disparaît (6)

- Mutation : le contrôle de longueur de `zip` est désactivé.
- Preuve : Les deux listes ont toujours la même longueur, garantie juste avant : dans `decoder_reponse`, une ligne dont le nombre de valeurs diffère du nombre de colonnes est refusée (`ReponseIllisible`) avant le `zip` ; dans `traiter_lot`, `argent` est construit élément par élément à partir de `nouveaux` (via `bronze`). Le contrôle ne peut donc jamais se déclencher : sans lui, le résultat est le même.
- Fait contrôlé par : propriété du code (les tests de refus de `decoder_reponse` prouvent le contrôle de longueur qui précède).
- Mutants :
  - `detection_fraude.consommateur.xǁConsommateurǁtraiter_lot__mutmut_89`
  - `detection_fraude.consommateur.xǁConsommateurǁtraiter_lot__mutmut_92`
  - `detection_fraude.consommateur.xǁConsommateurǁtraiter_lot__mutmut_93`
  - `detection_fraude.producteur.x_decoder_reponse__mutmut_45`
  - `detection_fraude.producteur.x_decoder_reponse__mutmut_48`
  - `detection_fraude.producteur.x_decoder_reponse__mutmut_49`

### Fenêtres glissantes : borne haute de `bisect_left` retirée (4)

- Mutation : `bisect_left(temps, t - fenêtre, 0, fin)` devient `bisect_left(temps, t - fenêtre, 0)` (ou `hi=None`).
- Preuve : `fin` est le nombre de transactions strictement antérieures à `t`. Toute transaction à partir de `fin` est au moins égale à `t`, donc jamais avant `t - fenêtre` : la borne ne retire rien à la recherche, l'indice trouvé est le même.
- Fait contrôlé par : test_la_borne_haute_de_la_recherche_dichotomique_est_redondante.
- Mutants :
  - `detection_fraude.fonctionnalites.xǁHistoriqueCarteǁstatistiques__mutmut_10`
  - `detection_fraude.fonctionnalites.xǁHistoriqueCarteǁstatistiques__mutmut_14`
  - `detection_fraude.fonctionnalites.xǁHistoriqueCarteǁstatistiques__mutmut_21`
  - `detection_fraude.fonctionnalites.xǁHistoriqueCarteǁstatistiques__mutmut_25`

### Ordre de visite des cartes (`groupby(..., sort=False)`) (3)

- Mutation : `sort=False` devient `sort=None`, `sort=True` ou disparaît.
- Preuve : Chaque carte est calculée indépendamment et ses résultats sont rangés à leurs propres positions : visiter les cartes dans un autre ordre ne change aucune valeur.
- Fait contrôlé par : test_le_calcul_par_lot_ne_depend_pas_de_l_ordre_dans_lequel_les_cartes_sont_visitees.
- Mutants :
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_29`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_31`
  - `detection_fraude.fonctionnalites.x_ajouter_historique_cartes__mutmut_34`

### Heure et jour de la semaine lus avec `utc=None` ou `utc=False` (3)

- Mutation : `pd.to_datetime(secondes, unit="s", utc=True)` perd `utc=True`.
- Preuve : Des secondes d'époque sont lues comme des instants UTC, avec ou sans fuseau attaché : l'heure et le jour de la semaine, seules valeurs gardées, sont identiques.
- Fait contrôlé par : test_pandas_lit_des_secondes_entieres_de_la_meme_facon_avec_ou_sans_utc.
- Mutants :
  - `detection_fraude.fonctionnalites.x_ajouter_caracteristiques_temporelles__mutmut_15`
  - `detection_fraude.fonctionnalites.x_ajouter_caracteristiques_temporelles__mutmut_4`
  - `detection_fraude.fonctionnalites.x_ajouter_caracteristiques_temporelles__mutmut_7`

### Valeur par défaut de `rejete_le` dans `_date_evenement` (3)

- Mutation : `rejet.get("rejete_le", "")` devient `get("rejete_le", None)`, `get("rejete_le")` ou `get("rejete_le", "XXXX")`.
- Preuve : La valeur par défaut ne sert que si `rejete_le` est absent ; elle passe alors par `str(...)` puis par le test `len(...) >= 10`. `""`, `"None"` et `"XXXX"` ont tous moins de 10 caractères : la même branche (date du jour) est prise.
- Fait contrôlé par : propriété du code.
- Mutants :
  - `detection_fraude.consommateur.x__date_evenement__mutmut_29`
  - `detection_fraude.consommateur.x__date_evenement__mutmut_31`
  - `detection_fraude.consommateur.x__date_evenement__mutmut_34`

### File SQLite en mémoire : test `":memory:"` neutralisé (3)

- Mutation : `str(self._chemin) != ":memory:"` devient `str(None) != ":memory:"`, ou compare à `"XX:memory:XX"` ou à `":MEMORY:"`.
- Preuve : Le test ne sert qu'à éviter de créer le dossier parent d'une base en mémoire. Pour `":memory:"`, ce parent est le dossier courant, qui existe : `mkdir(parents=True, exist_ok=True)` ne fait rien. Pour un vrai fichier, le test était déjà vrai.
- Fait contrôlé par : test_un_chemin_de_base_en_memoire_a_pour_parent_le_dossier_courant.
- Mutants :
  - `detection_fraude.file_attente.xǁFileSQLiteǁ__init____mutmut_3`
  - `detection_fraude.file_attente.xǁFileSQLiteǁ__init____mutmut_5`
  - `detection_fraude.file_attente.xǁFileSQLiteǁ__init____mutmut_6`

### Journal : valeurs par défaut de `configurer_journal` (3)

- Mutation : `niveau="INFO"` devient `"info"` ; `console="lisible"` devient `"XXlisibleXX"` ou `"LISIBLE"`.
- Preuve : Le niveau passe par `niveau.upper()` : `"info"` donne `"INFO"`. La console n'est comparée qu'à `"json"` : toute autre valeur donne le format lisible.
- Fait contrôlé par : propriété du code.
- Mutants :
  - `detection_fraude.journal.x_configurer_journal__mutmut_2`
  - `detection_fraude.journal.x_configurer_journal__mutmut_3`
  - `detection_fraude.journal.x_configurer_journal__mutmut_4`

### Mesures au seuil indépendantes de `zero_division` et du type des décisions (3)

- Mutation : dans `evaluer`, `zero_division=0` devient `1` ou disparaît ; `.astype(int)` devient `.astype(None)` (décisions en flottants 0.0 / 1.0).
- Preuve : `evaluer` renvoie NaN dès que le jeu n'a pas les deux classes, avant tout calcul : le rappel est donc toujours défini (au moins une fraude), `zero_division` ne sert jamais. Des décisions 0.0 / 1.0 donnent les mêmes précision, rappel et nombre d'alertes que 0 / 1.
- Fait contrôlé par : test_le_rappel_d_un_jeu_a_deux_classes_ne_depend_pas_de_zero_division.
- Mutants :
  - `detection_fraude.entrainement.x_evaluer__mutmut_14`
  - `detection_fraude.entrainement.x_evaluer__mutmut_63`
  - `detection_fraude.entrainement.x_evaluer__mutmut_64`

### Run MLflow parent : `if mlflow and run_parent` devient `if mlflow or run_parent` (2)

- Mutation : le `and` devient `or` (deux endroits de `entrainer`).
- Preuve : `run_parent` est créé si et seulement si `mlflow` l'est (même condition, ligne qui suit) : les deux valeurs sont vraies ensemble ou fausses ensemble, `and` et `or` donnent le même résultat.
- Fait contrôlé par : propriété du code (test_entrainer_trace_dans_mlflow_les_parametres_metriques_et_tags_exacts et test_entrainer_sans_suivi_n_appelle_jamais_mlflow_et_n_ecrit_que_dans_les_modeles couvrent les deux cas).
- Mutants :
  - `detection_fraude.entrainement.x_entrainer__mutmut_280`
  - `detection_fraude.entrainement.x_entrainer__mutmut_347`

### Seuil à précision fixée : choix du meilleur point (2)

- Mutation : `admissibles[np.argmax(r[admissibles])]` devient `admissibles[np.argmax(None)]` ; le F1 `2 * p * r / ...` devient `3 * p * r / ...`.
- Preuve : Le rappel de la courbe précision-rappel ne croît jamais quand le seuil monte : le meilleur rappel admissible est celui du premier point admissible, l'indice 0, ce que rend aussi `np.argmax(None)`. Multiplier tous les F1 par 1,5 ne déplace pas leur maximum.
- Fait contrôlé par : test_le_rappel_de_la_courbe_ne_croit_jamais_quand_le_seuil_monte, test_le_f1_multiplie_par_une_constante_positive_garde_le_meme_maximum.
- Mutants :
  - `detection_fraude.entrainement.x_rappel_a_precision_fixee__mutmut_15`
  - `detection_fraude.entrainement.x_rappel_a_precision_fixee__mutmut_32`

### Numéro de carte : `int(valeur) > 0` devient `>= 0` ou `> 1` (2)

- Mutation : la borne basse du contrôle de `cc_num` change.
- Preuve : Le contrôle exige déjà 8 à 19 caractères : un entier positif de cette longueur vaut au moins 10 000 000, et 0 ou 1 (un seul caractère) sont refusés par la longueur. La borne ne sert qu'à refuser les négatifs ; `> 0`, `>= 0` et `> 1` décident pareil.
- Fait contrôlé par : test_un_numero_de_carte_de_8_a_19_caracteres_est_au_moins_dix_millions_ou_negatif.
- Mutants :
  - `detection_fraude.schema.x__controle_brut__mutmut_17`
  - `detection_fraude.schema.x__controle_brut__mutmut_18`

### Horodatage en millisecondes : borne basse décalée d'une unité (2)

- Mutation : `946_684_800_000 <= valeur` devient `946_684_800_001 <= valeur` ou `<`.
- Preuve : Toute valeur sous 10**12 ressort inchangée de `_tronquer`, que la borne basse la laisse passer ou non : seule la valeur 946 684 800 000 change de branche, pour le même résultat.
- Fait contrôlé par : test_tronquer_laisse_inchange_tout_nombre_sous_mille_milliards.
- Mutants :
  - `detection_fraude.masquage.x__horodatage_ms_ou_masque__mutmut_5`
  - `detection_fraude.masquage.x__horodatage_ms_ou_masque__mutmut_6`

## Individuels

### `detection_fraude.entrainement.x_entrainer__mutmut_44`

- Mutation : `to_numpy(dtype=int)` devient `to_numpy(dtype=None)` pour les étiquettes.
- Preuve : La validation refuse toute ligne dont `is_fraud` n'est pas 0 ou 1 : la colonne `etiquette_source` du jeu d'entraînement est déjà de type `int64`, le tableau est le même.
- Fait contrôlé par : test_l_etiquette_du_jeu_d_entrainement_est_deja_un_entier.

### `detection_fraude.modele.xǁChargeurModeleǁ__init____mutmut_3`

- Mutation : `self._refusee = None` devient `self._refusee = ""`.
- Preuve : `_refusee` n'est comparée qu'à `courante`, testée vraie juste avant (`courante and ...`) : une version vraie n'est jamais égale ni à `None` ni à `""`.
- Fait contrôlé par : propriété du code.

### `detection_fraude.producteur.x__retry_after__mutmut_15`

- Mutation : `... if valeur is not None else None` devient `... if (valeur is not None) or True else None`.
- Preuve : Quand l'en-tête est absent, la branche mutée calcule `float(None)`, qui lève `TypeError`, aussitôt rattrapée par la fonction qui renvoie `None` : même résultat.
- Fait contrôlé par : test_float_de_none_leve_type_error_que_le_producteur_rattrape.

### `detection_fraude.producteur.x_interroger_api__mutmut_22`

- Mutation : `raise requests.HTTPError(derniere_cause)` devient `raise requests.HTTPError(None)`.
- Preuve : L'exception est levée pour rejoindre la branche de relance et rattrapée aussitôt dans la même fonction ; son message n'est jamais lu (la cause gardée est `derniere_cause`).
- Fait contrôlé par : propriété du code.

### `detection_fraude.producteur.xǁProducteurǁ_deja_vu__mutmut_3`

- Mutation : `self._vus[trans_num] = None` devient `= ""`.
- Preuve : `_vus` est un ensemble ordonné : seules ses clés sont lues (`in`, `len`, `popitem`), jamais ses valeurs.
- Fait contrôlé par : propriété du code.

### `detection_fraude.producteur.x_main__mutmut_79`

- Mutation : `--pannes` : `default=0` devient `default=None`.
- Preuve : L'option n'est lue que par `if arguments.pannes` : `0` et `None` sont faux tous les deux.
- Fait contrôlé par : test_bool_de_none_est_faux.

### `detection_fraude.producteur.x_main__mutmut_83`

- Mutation : `--pannes` : `default=0` disparaît (argparse met alors `None`).
- Preuve : Même raison : `0` et `None` sont faux tous les deux.
- Fait contrôlé par : test_bool_de_none_est_faux.

### `detection_fraude.supervision.x_controler__mutmut_51`

- Mutation : `.astimezone(UTC)` devient `.astimezone(None)` avant une soustraction.
- Preuve : La différence de deux instants munis d'un fuseau ne dépend pas du fuseau dans lequel ils sont exprimés.
- Fait contrôlé par : test_un_instant_aware_garde_sa_difference_quel_que_soit_son_fuseau_d_expression.

### `detection_fraude.rapport.x_jour_de_la_veille__mutmut_5`

- Mutation : `maintenant.replace(tzinfo=UTC)` devient `maintenant.replace(tzinfo=None)` (instant naïf).
- Preuve : Seule la date de l'instant est lue (`.date()`) : poser ou non le fuseau UTC sur un instant naïf ne change ni l'heure ni la date.
- Fait contrôlé par : propriété du code.
