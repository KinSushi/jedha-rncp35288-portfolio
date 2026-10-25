# Mutants équivalents

Un mutant est **équivalent** quand le code modifié se comporte exactement comme le code
d'origine : aucun test ne peut le faire échouer. Chaque mutant ci-dessous a survécu à la
mutation (mutmut 3, sous WSL, mesure du 21/10/2026, après le retrait de la famille U), et
appartient à une famille dont la preuve est donnée : soit un fait de bibliothèque vérifié par
un test de `tests/test_mutants_equivalents.py` (ces tests ne visent pas le code du projet et ne font pas partie
de la sélection de la mutation), soit un raisonnement sur le code cité.

Équivalent s'entend ici au sens strict : le code muté rend les mêmes résultats et écrit
les mêmes octets (fichiers, tables et schéma de l'entrepôt, figures, requêtes envoyées,
messages). Seul le temps de calcul peut changer (familles D et P1 : un découpage plus
long, ou des tours de boucle de plus, dont le résultat est jeté ou nul).

Total : **99 mutants non tués, tous prouvés équivalents.** Les trois mutants de la famille U ont été retirés de la liste le 21 octobre 2026 : ils sont désormais tués par deux tests du projet.

## Familles et preuves

| Famille | Mutation | Preuve | Mutants |
|---|---|---|---:|
| A | Nom de codec en majuscules (« UTF-8 » au lieu de « utf-8 ») | Python lit le nom d'un codec sans tenir compte de la casse : mêmes octets à l'encodage et au décodage (tests/test_mutants_equivalents.py::test_le_nom_d_encodage_utf8_ne_depend_pas_de_la_casse). | 15 |
| B | `ensure_ascii=None` au lieu de `False` (json) | json ne teste que la vérité de l'option : None se comporte comme False (tests/test_mutants_equivalents.py::test_ensure_ascii_none_se_comporte_comme_faux). | 10 |
| C | `to_csv(index=None)` au lieu de `index=False` (pandas) | pandas n'écrit pas l'index dans les deux cas (tests/test_mutants_equivalents.py::test_to_csv_index_none_se_comporte_comme_faux). | 2 |
| D | `maxsplit` changé ou retiré, seul le premier morceau étant lu | Le premier morceau d'un découpage ne dépend pas de maxsplit (tests/test_mutants_equivalents.py::test_split_premier_morceau_ne_depend_pas_de_maxsplit). | 4 |
| E | « XXXX » inséré entre les lettres d'un nom avant le filtrage `[^a-z\s]` | Le filtrage qui suit retire les « X » majuscules : même nom normalisé (tests/test_mutants_equivalents.py::test_lettres_majuscules_inserees_puis_retirees_ne_changent_rien). | 1 |
| F | `quote(cle, safe="XXXX")` au lieu de `safe=""` | quote ne code jamais une lettre : ajouter « X » aux caractères sûrs ne change rien (tests/test_mutants_equivalents.py::test_quote_ne_code_jamais_une_lettre). | 1 |
| H | `default=None` retiré d'une option argparse | Sans default, argparse donne None (tests/test_mutants_equivalents.py::test_argparse_valeur_par_defaut_est_none). | 2 |
| I | Valeur par défaut de pandas écrite ou retirée (`na_position="last"`, `astype(None)`) | « last » est le défaut de sort_values et astype(None) convertit en flottants (tests/test_mutants_equivalents.py::test_pandas_tri_et_conversion_par_defaut). | 2 |
| J | `np.stack(..., axis=+1)` au lieu de `axis=-1` | Pour des colonnes à une dimension, les deux axes désignent le même (tests/test_mutants_equivalents.py::test_numpy_empiler_selon_l_axe_moins_un_ou_un). | 1 |
| K | Type de sous-figure Plotly en majuscules, ou `map1` au lieu de `map` | Plotly lit le type sans tenir compte de la casse, et map1 désigne la carte map (tests/test_mutants_equivalents.py::test_plotly_types_de_sous_figures_et_alias_map1). | 4 |
| L | Réglages par défaut de Pillow (`quantize`, nom de format « png ») | Pour une image RGB, quantize réduit par défaut à 256 couleurs par MEDIANCUT ; « png » vaut « PNG » (tests/test_mutants_equivalents.py::test_pillow_reduction_de_couleurs_et_format_png). | 4 |
| M | Format de `IPython.display.Image` donné autrement (None, absent, « PNG ») | IPython déduit « png » des octets ou le met en minuscules : même rendu (tests/test_mutants_equivalents.py::test_ipython_image_png_deduit_ou_normalise_son_format). | 3 |
| N | Écrivains pyarrow : options par défaut, `write` ou `write_batch` | write et write_batch écrivent la même chose (CSV et Parquet) et le CSV a son en-tête par défaut (tests/test_mutants_equivalents.py::test_pyarrow_ecrivains_csv_et_parquet). | 7 |
| O | Type de jointure Spark : en majuscules, ou « inner » remplacé par le défaut | Spark lit le type de jointure sans tenir compte de la casse, et une jointure sans type (ou de type None) est « inner » (tests/test_mutants_equivalents.py::test_spark_type_de_jointure_insensible_a_la_casse). | 5 |
| R | Nom d'option de lecture CSV de Spark en majuscules (« HEADER ») | Spark lit le nom d'une option sans tenir compte de la casse (tests/test_mutants_equivalents.py::test_spark_options_de_lecture_csv_insensibles_a_la_casse). | 1 |
| T | Type écrit dans `typing.cast` changé | cast rend son argument tel quel à l'exécution ; il ne sert qu'à mypy (tests/test_mutants_equivalents.py::test_typing_cast_ne_fait_rien_a_l_execution). | 4 |
| U | Encodage de la marque du cache S3 (`chemin_local`) retiré ou mis à None | **Famille retirée le 21/10/2026** : les trois mutants ne sont pas équivalents. La marque contient l'ETag (hexadécimal) **et la date de modification, qui n'est pas ASCII** (`LastModified` peut contenir un accent : c'est le cas des tests du projet, « dimanche été »). L'encodage implicite n'est pas toujours UTF-8 : sous Windows en cp1252, et dès qu'on le force ailleurs. Le code écrit et relit la marque en UTF-8 explicitement, et deux tests du projet (`tests/test_data_lake_inverses.py`) le vérifient en forçant l'encodage implicite à ASCII. | 0 |
| V | `strict` de `zip` retiré, False ou None au lieu de True (`publier_dossier`) | Les deux listes ont toujours la même longueur : la seconde est construite élément par élément depuis la première (`publies = [... for f in fichiers]`). Le contrôle de longueur ne se déclenche donc jamais, avec ou sans strict. | 6 |
| X | Défauts de SQLAlchemy écrits autrement (`nullable=None`, type de la clé étrangère retiré, index sans nom) | nullable=None donne NOT NULL comme False ; une colonne de clé étrangère prend le type de la colonne visée ; un index sans nom reçoit « ix_<table>_<colonne> », le nom écrit dans le code : même schéma (tests/test_mutants_equivalents.py::test_sqlalchemy_valeurs_par_defaut_des_colonnes_et_des_index). | 9 |
| Z | Casse de l'ordre `DROP VIEW IF EXISTS vue_hotels_destinations` | SQLite lit les mots-clés et le nom non cité de la vue sans tenir compte de la casse, et ne garde nulle part le texte d'une suppression : les deux écritures suppriment la même vue (ou ne font rien sur une base sans vue) et laissent le même schéma (tests/test_mutants_equivalents.py::test_sql_drop_view_insensible_a_la_casse_et_sans_trace). Ce fait ne vaut pas pour `CREATE VIEW`, dont SQLite garde le texte tel quel : sa casse est vérifiée par tests/test_spark_etl_hors_spark.py, et aucun mutant de ce texte ne survit. | 2 |
| W | Orientation « RECORDS » de `to_dict` en majuscules | pandas lit l'orientation sans tenir compte de la casse (tests/test_mutants_equivalents.py::test_pandas_to_dict_orientation_insensible_a_la_casse). | 1 |
| S | Score météo : signe unitaire en division, `rank(ascending=None)` | Pour un signe de +1 ou -1, multiplier et diviser donnent les mêmes flottants ; ascending=None classe comme False (tests/test_mutants_equivalents.py::test_diviser_par_un_signe_unitaire_equivaut_a_multiplier et tests/test_mutants_equivalents.py::test_rank_ascending_none_se_comporte_comme_faux). | 2 |
| P1 | Booléen initial de `ecarter_marqueurs` (`None` ou `True` au lieu de `False`) | None et False prennent la même branche de `if not deplace`. Avec True, la boucle fait tous ses tours au lieu de s'arrêter tôt ; mais un tour sans paire plus proche que le diamètre ne déplace rien (`continue` pour chaque paire) : les positions finales sont les mêmes (kayak/cartes.py, boucle de ecarter_marqueurs). | 2 |
| P2 | `break` au lieu de `continue` sur le tableau vide de `carte_hotels` | Le premier tableau a ceil(n/2) hôtels, le second n - ceil(n/2) : si le premier est vide, le second l'est aussi. Sortir de la boucle ou passer au suivant mène donc au même résultat. | 1 |
| P3 | Valeur par défaut d'une clé absente qui ne peut pas changer le test qui suit | verifier_reponse : la remarque n'est cherchée que pour « runtime error », absente de "", "None" et "XXXX". convertir : la classe n'est lue que si son premier caractère est un chiffre, ce qui n'est vrai ni pour "" ni pour "XXXX". | 4 |
| P4 | Booléen initial de `lire_groupes` (`None` ou `True` au lieu de `False`) | Au premier « User-agent », `courant is None` impose déjà un nouveau groupe ; ensuite la variable est réécrite avant d'être relue. None se comporte comme False dans `not`. | 3 |
| P6 | Nom de la colonne de travail `_rang_proximite` changé dans `.drop(...)` | Retirer une colonne qui n'existe pas ne fait rien ; mais la colonne de travail ne sort pas non plus de `transformer` : la sélection explicite des colonnes de l'hôtel, quelques lignes plus bas (`hotels = hotels.select(...)`), ne la reprend pas. Les tables, le Parquet et le CSV ont donc les mêmes colonnes (vérifié par tests/test_etl_riche.py, colonnes exactes de la table enrichie). | 2 |
| P5 | Jeton de continuation initial `""` au lieu de `None` (`DataLake.lister`) | Le jeton n'est lu que par `if continuation_token:` avant le premier appel : "" et None sont tous deux faux, donc le premier appel part sans jeton dans les deux cas ; ensuite le jeton est remplacé par celui de la réponse. | 1 |

## Liste des mutants

| Mutant | Ligne d'origine | Ligne mutée | Famille |
|---|---|---|---|
| `cartes.x_carte_hotels__mutmut_132` | `nom_carte = "map" if colonne == 0 else f"map{colonne + 1}"` | `nom_carte = "map" if (colonne == 0) and False else f"map{colonne + 1}"` | K |
| `cartes.x_carte_hotels__mutmut_364` | `axis=-1,` | `axis=+1,` | J |
| `cartes.x_carte_hotels__mutmut_384` | `continue` | `break` | P2 |
| `cartes.x_carte_hotels__mutmut_40` | `[{"type": "map", "colspan": 2}, None] * n,` | `[{"type": "MAP", "colspan": 2}, None] * n,` | K |
| `cartes.x_carte_hotels__mutmut_48` | `[{"type": "table", "colspan": n}, *([None] * (n - 1)), {"type": "table", "colspan": n}]` | `[{"type": "TABLE", "colspan": n}, *([None] * (n - 1)), {"type": "table", "colspan": n}]` | K |
| `cartes.x_carte_hotels__mutmut_57` | `[{"type": "table", "colspan": n}, *([None] * (n - 1)), {"type": "table", "colspan": n}]` | `[{"type": "table", "colspan": n}, *([None] * (n - 1)), {"type": "TABLE", "colspan": n}]` | K |
| `cartes.x_ecarter_marqueurs__mutmut_38` | `deplace = False` | `deplace = None` | P1 |
| `cartes.x_ecarter_marqueurs__mutmut_39` | `deplace = False` | `deplace = True` | P1 |
| `cartes.x_enregistrer__mutmut_35` | `with open(chemins["html"], "w", encoding="utf-8", newline="\n") as fichier:` | `with open(chemins["html"], "w", encoding="UTF-8", newline="\n") as fichier:` | A |
| `cartes.x_selectionner_top_hotels__mutmut_65` | `hotels = hotels.sort_values(by=cles, ascending=sens, na_position="last").head(top).copy()` | `hotels = hotels.sort_values(by=cles, ascending=sens, ).head(top).copy()` | I |
| `cartes.x_selectionner_top_hotels__mutmut_81` | `hotels["indice_qualite"] = hotels["note"].fillna(hotels["etoiles"] * 2).fillna(5).astype(float)` | `hotels["indice_qualite"] = hotels["note"].fillna(hotels["etoiles"] * 2).fillna(5).astype(None)` | I |
| `collecte.booking_spider.x__executer_robot__mutmut_6` | `with open(chemin_villes, encoding="utf-8") as fichier:` | `with open(chemin_villes, encoding="UTF-8") as fichier:` | A |
| `collecte.booking_spider.x_lancer_collecte_booking__mutmut_113` | `lignes = [json.loads(ligne) for ligne in contenu.decode("utf-8").splitlines() if ligne]` | `lignes = [json.loads(ligne) for ligne in contenu.decode("UTF-8").splitlines() if ligne]` | A |
| `collecte.booking_spider.x_lancer_collecte_booking__mutmut_65` | `with open(entree, "w", encoding="utf-8") as fichier:` | `with open(entree, "w", encoding="UTF-8") as fichier:` | A |
| `collecte.booking_spider.x_lancer_collecte_booking__mutmut_68` | `json.dump(villes, fichier, ensure_ascii=False)` | `json.dump(villes, fichier, ensure_ascii=None)` | B |
| `collecte.booking_spider.xǁBookingHotelsSpiderǁclosed__mutmut_30` | `with open(self.sortie + ".rapport.json", "w", encoding="utf-8", newline="\n") as fichier:` | `with open(self.sortie + ".rapport.json", "w", encoding="UTF-8", newline="\n") as fichier:` | A |
| `collecte.booking_spider.xǁBookingHotelsSpiderǁclosed__mutmut_34` | `{"fin": reason, "villes": self.rapport}, fichier, ensure_ascii=False, indent=2` | `{"fin": reason, "villes": self.rapport}, fichier, ensure_ascii=None, indent=2` | B |
| `collecte.destinations.x_collecter_destinations__mutmut_49` | `lake.ecrire(chemin_csv, df.to_csv(index=False, lineterminator="\n"))` | `lake.ecrire(chemin_csv, df.to_csv(index=None, lineterminator="\n"))` | C |
| `collecte.destinations.x_normaliser__mutmut_5` | `nom = "".join(c for c in unicodedata.normalize("NFKD", nom) if not unicodedata.combining(c))` | `nom = "XXXX".join(c for c in unicodedata.normalize("NFKD", nom) if not unicodedata.combining(c))` | E |
| `collecte.geocodage.x_collecter_coordonnees__mutmut_21` | `reponse = json.loads(lake.lire(chemin).decode("utf-8"))` | `reponse = json.loads(lake.lire(chemin).decode("UTF-8"))` | A |
| `collecte.geocodage.x_collecter_coordonnees__mutmut_32` | `lake.ecrire(chemin, json.dumps(reponse, ensure_ascii=False, indent=2))` | `lake.ecrire(chemin, json.dumps(reponse, ensure_ascii=None, indent=2))` | B |
| `collecte.geocodage.x_collecter_coordonnees__mutmut_53` | `coordonnees.to_csv(index=False, lineterminator="\n"),` | `coordonnees.to_csv(index=None, lineterminator="\n"),` | C |
| `collecte.hotels_osm.x__reponse_de_ville__mutmut_46` | `lake.ecrire(chemin_brut, json.dumps(reponse, ensure_ascii=False))` | `lake.ecrire(chemin_brut, json.dumps(reponse, ensure_ascii=None))` | B |
| `collecte.hotels_osm.x__reponse_de_ville__mutmut_8` | `reponse = json.loads(lake.lire(chemin_brut).decode("utf-8"))` | `reponse = json.loads(lake.lire(chemin_brut).decode("UTF-8"))` | A |
| `collecte.hotels_osm.x_convertir__mutmut_45` | `etoiles = etiquettes.get("stars", "")` | `etoiles = etiquettes.get("stars", "XXXX")` | P3 |
| `collecte.hotels_osm.x_diagnostic_troncature__mutmut_17` | `reponse = json.loads(lake.lire(chemin).decode("utf-8"))` | `reponse = json.loads(lake.lire(chemin).decode("UTF-8"))` | A |
| `collecte.hotels_osm.x_ecrire_hotels__mutmut_9` | `"".join(json.dumps(h, ensure_ascii=False) + "\n" for h in hotels),` | `"".join(json.dumps(h, ensure_ascii=None) + "\n" for h in hotels),` | B |
| `collecte.hotels_osm.x_main__mutmut_26` | `analyseur.add_argument("--date", default=None, help="date de collecte AAAA-MM-JJ")` | `analyseur.add_argument("--date", help="date de collecte AAAA-MM-JJ")` | H |
| `collecte.hotels_osm.x_verifier_reponse__mutmut_4` | `remarque = str(reponse.get("remark", ""))` | `remarque = str(reponse.get("remark", None))` | P3 |
| `collecte.hotels_osm.x_verifier_reponse__mutmut_6` | `remarque = str(reponse.get("remark", ""))` | `remarque = str(reponse.get("remark", ))` | P3 |
| `collecte.hotels_osm.x_verifier_reponse__mutmut_9` | `remarque = str(reponse.get("remark", ""))` | `remarque = str(reponse.get("remark", "XXXX"))` | P3 |
| `collecte.journal.x__entree__mutmut_17` | `octets_corps = corps if isinstance(corps, bytes) else corps.encode("utf-8")` | `octets_corps = corps if isinstance(corps, bytes) else corps.encode("UTF-8")` | A |
| `collecte.journal.x__entree__mutmut_26` | `texte = unquote_plus(octets_corps.decode("utf-8", "replace"))` | `texte = unquote_plus(octets_corps.decode("UTF-8", "replace"))` | A |
| `collecte.meteo.x_collecter_meteo__mutmut_113` | `lake.ecrire(chemin_ville, json.dumps(entree, ensure_ascii=False))` | `lake.ecrire(chemin_ville, json.dumps(entree, ensure_ascii=None))` | B |
| `collecte.meteo.x_collecter_meteo__mutmut_129` | `contenu_jl = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entrees_brutes)` | `contenu_jl = "".join(json.dumps(e, ensure_ascii=None) + "\n" for e in entrees_brutes)` | B |
| `collecte.meteo.x_collecter_meteo__mutmut_34` | `entree = json.loads(lake.lire(chemin_ville).decode("utf-8"))` | `entree = json.loads(lake.lire(chemin_ville).decode("UTF-8"))` | A |
| `collecte.meteo.x_collecter_meteo__mutmut_87` | `message = message.replace(cle, "***").replace(quote(cle, safe=""), "***")` | `message = message.replace(cle, "***").replace(quote(cle, safe="XXXX"), "***")` | F |
| `collecte.minimisation.x__minimiser_archive_ville__mutmut_28` | `return json.dumps(minimisee, ensure_ascii=False), retirees, masques` | `return json.dumps(minimisee, ensure_ascii=None), retirees, masques` | B |
| `collecte.minimisation.x__minimiser_hotels_jsonl__mutmut_19` | `lignes.append(json.dumps(minimise, ensure_ascii=False) + "\n")` | `lignes.append(json.dumps(minimise, ensure_ascii=None) + "\n")` | B |
| `collecte.robots.x_archiver_robots__mutmut_74` | `lake.ecrire(f"{dossier}/robots_meta.json", json.dumps(meta, ensure_ascii=False, indent=2))` | `lake.ecrire(f"{dossier}/robots_meta.json", json.dumps(meta, ensure_ascii=None, indent=2))` | B |
| `collecte.robots.x_jeton_produit__mutmut_10` | `return re.split(r"[/\s(]", user_agent.strip(), maxsplit=1)[0].lower()` | `return re.split(r"[/\s(]", user_agent.strip(), maxsplit=2)[0].lower()` | D |
| `collecte.robots.x_jeton_produit__mutmut_7` | `return re.split(r"[/\s(]", user_agent.strip(), maxsplit=1)[0].lower()` | `return re.split(r"[/\s(]", user_agent.strip(), )[0].lower()` | D |
| `collecte.robots.x_lire_groupes__mutmut_12` | `ligne = ligne.split("#", 1)[0].strip()` | `ligne = ligne.split("#", )[0].strip()` | D |
| `collecte.robots.x_lire_groupes__mutmut_15` | `ligne = ligne.split("#", 1)[0].strip()` | `ligne = ligne.split("#", 2)[0].strip()` | D |
| `collecte.robots.x_lire_groupes__mutmut_3` | `dernier_etait_agent = False` | `dernier_etait_agent = None` | P4 |
| `collecte.robots.x_lire_groupes__mutmut_4` | `dernier_etait_agent = False` | `dernier_etait_agent = True` | P4 |
| `collecte.robots.x_lire_groupes__mutmut_49` | `dernier_etait_agent = False` | `dernier_etait_agent = None` | P4 |
| `etl.score.x_calculer_score_meteo__mutmut_57` | `z_score(df[col_soleil]) * signe_soleil` | `z_score(df[col_soleil]) / signe_soleil` | S |
| `etl.score.x_calculer_score_meteo__mutmut_75` | `df["rang_meteo"] = df["score_meteo"].rank(ascending=False, method="min").astype(int)` | `df["rang_meteo"] = df["score_meteo"].rank(ascending=None, method="min").astype(int)` | S |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_22` | `options = pacsv.WriteOptions(include_header=True)` | `options = None` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_23` | `options = pacsv.WriteOptions(include_header=True)` | `options = pacsv.WriteOptions(include_header=None)` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_28` | `ecrivain = pacsv.CSVWriter(chemin, lot.schema, write_options=options)` | `ecrivain = pacsv.CSVWriter(chemin, lot.schema, write_options=None)` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_31` | `ecrivain = pacsv.CSVWriter(chemin, lot.schema, write_options=options)` | `ecrivain = pacsv.CSVWriter(chemin, lot.schema, )` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_37` | `if format_ == "csv":` | `if format_ != "csv":` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_38` | `if format_ == "csv":` | `if format_ == "XXcsvXX":` | N |
| `etl.spark_etl.x__ecrire_executeurs__mutmut_39` | `if format_ == "csv":` | `if format_ == "CSV":` | N |
| `etl.spark_etl.x__lignes_pandas__mutmut_10` | `yield from cast("list[dict[str, Any]]", propre.to_dict("records"))` | `yield from cast("XXlist[dict[str, Any]]XX", propre.to_dict("records"))` | T |
| `etl.spark_etl.x__lignes_pandas__mutmut_11` | `yield from cast("list[dict[str, Any]]", propre.to_dict("records"))` | `yield from cast("list[dict[str, any]]", propre.to_dict("records"))` | T |
| `etl.spark_etl.x__lignes_pandas__mutmut_12` | `yield from cast("list[dict[str, Any]]", propre.to_dict("records"))` | `yield from cast("LIST[DICT[STR, ANY]]", propre.to_dict("records"))` | T |
| `etl.spark_etl.x__lignes_pandas__mutmut_15` | `yield from cast("list[dict[str, Any]]", propre.to_dict("records"))` | `yield from cast("list[dict[str, Any]]", propre.to_dict("RECORDS"))` | W |
| `etl.spark_etl.x__lignes_pandas__mutmut_6` | `yield from cast("list[dict[str, Any]]", propre.to_dict("records"))` | `yield from cast(None, propre.to_dict("records"))` | T |
| `etl.spark_etl.x_charger_entrepot__mutmut_125` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), primary_key=True),` | `Column("id_ville", None, ForeignKey("villes.id_ville"), primary_key=True),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_129` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), primary_key=True),` | `Column("id_ville", ForeignKey("villes.id_ville"), primary_key=True),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_240` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), nullable=False),` | `Column("id_ville", None, ForeignKey("villes.id_ville"), nullable=False),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_242` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), nullable=False),` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), nullable=None),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_244` | `Column("id_ville", Integer, ForeignKey("villes.id_ville"), nullable=False),` | `Column("id_ville", ForeignKey("villes.id_ville"), nullable=False),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_255` | `Column("nom", String(300), nullable=False),` | `Column("nom", String(300), nullable=None),` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_350` | `Index("ix_hotels_id_ville", table_hotels.c.id_ville)` | `Index(None, table_hotels.c.id_ville)` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_356` | `Index("ix_hotels_note", table_hotels.c.note)` | `Index(None, table_hotels.c.note)` | X |
| `etl.spark_etl.x_charger_entrepot__mutmut_367` | `connexion.execute(text("DROP VIEW IF EXISTS vue_hotels_destinations"))` | `connexion.execute(text("drop view if exists vue_hotels_destinations"))` | Z |
| `etl.spark_etl.x_charger_entrepot__mutmut_368` | `connexion.execute(text("DROP VIEW IF EXISTS vue_hotels_destinations"))` | `connexion.execute(text("DROP VIEW IF EXISTS VUE_HOTELS_DESTINATIONS"))` | Z |
| `etl.spark_etl.x_charger_entrepot__mutmut_42` | `Column("ville", String(100), nullable=False),` | `Column("ville", String(100), nullable=None),` | X |
| `etl.spark_etl.x_extraire__mutmut_46` | `"coordonnees": spark.read.option("header", True).csv(` | `"coordonnees": spark.read.option("HEADER", True).csv(` | R |
| `etl.spark_etl.x_transformer__mutmut_229` | `villes_pdf = calculer_score_meteo(villes.join(agregats, "id_ville", "left").toPandas())` | `villes_pdf = calculer_score_meteo(villes.join(agregats, "id_ville", "LEFT").toPandas())` | O |
| `etl.spark_etl.x_transformer__mutmut_461` | `hotels = hotels.join(centres, "id_ville", "left").withColumn(` | `hotels = hotels.join(centres, "id_ville", "LEFT").withColumn(` | O |
| `etl.spark_etl.x_transformer__mutmut_523` | `.drop("_rang_proximite")` | `.drop("XX_rang_proximiteXX")` | P6 |
| `etl.spark_etl.x_transformer__mutmut_524` | `.drop("_rang_proximite")` | `.drop("_RANG_PROXIMITE")` | P6 |
| `etl.spark_etl.x_transformer__mutmut_723` | `"inner",` | `None,` | O |
| `etl.spark_etl.x_transformer__mutmut_726` | `"inner",` + `).orderBy(` | `).orderBy(` | O |
| `etl.spark_etl.x_transformer__mutmut_762` | `"inner",` | `"INNER",` | O |
| `outils_notebook.x_bilan_overpass__mutmut_21` | `reponse = json.loads(lake.lire(chemin).decode("utf-8"))` | `reponse = json.loads(lake.lire(chemin).decode("UTF-8"))` | A |
| `outils_notebook.x_image_allegee__mutmut_23` | `image.quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT).save(` | `image.quantize(colors=256, method=None).save(` | L |
| `outils_notebook.x_image_allegee__mutmut_24` | `image.quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT).save(` | `image.quantize(method=ImagePIL.Quantize.MEDIANCUT).save(` | L |
| `outils_notebook.x_image_allegee__mutmut_25` | `image.quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT).save(` | `image.quantize(colors=256, ).save(` | L |
| `outils_notebook.x_image_allegee__mutmut_28` | `tampon, format="PNG", optimize=True` | `tampon, format="png", optimize=True` | L |
| `outils_notebook.x_image_allegee__mutmut_31` | `return Image(data=tampon.getvalue(), format="png", width=1000)  # type: ignore[no-untyped-call]` | `return Image(data=tampon.getvalue(), format=None, width=1000)  # type: ignore[no-untyped-call]` | M |
| `outils_notebook.x_image_allegee__mutmut_34` | `return Image(data=tampon.getvalue(), format="png", width=1000)  # type: ignore[no-untyped-call]` | `return Image(data=tampon.getvalue(), width=1000)  # type: ignore[no-untyped-call]` | M |
| `outils_notebook.x_image_allegee__mutmut_37` | `return Image(data=tampon.getvalue(), format="png", width=1000)  # type: ignore[no-untyped-call]` | `return Image(data=tampon.getvalue(), format="PNG", width=1000)  # type: ignore[no-untyped-call]` | M |
| `pipeline.x_main__mutmut_26` | `analyseur.add_argument("--date", default=None, help="date de collecte AAAA-MM-JJ")` | `analyseur.add_argument("--date", help="date de collecte AAAA-MM-JJ")` | H |
| `stockage.data_lake.xǁDataLakeǁchemin_local__mutmut_25` | `marque.read_text(encoding="utf-8")` | `marque.read_text(encoding=None)` | **Tué** (21/10/2026) |
| `stockage.data_lake.xǁDataLakeǁchemin_local__mutmut_27` | `if cible.exists() and marque.exists() and marque.read_text(encoding="utf-8") == empreinte:` | `if cible.exists() and marque.exists() and marque.read_text(encoding="UTF-8") == empreinte:` | A |
| `stockage.data_lake.xǁDataLakeǁchemin_local__mutmut_47` | `marque.write_text(empreinte, encoding="utf-8")` | `marque.write_text(empreinte, encoding=None)` | **Tué** (21/10/2026) |
| `stockage.data_lake.xǁDataLakeǁchemin_local__mutmut_49` | `marque.write_text(empreinte, encoding="utf-8")` | `marque.write_text(empreinte, )` | **Tué** (21/10/2026) |
| `stockage.data_lake.xǁDataLakeǁchemin_local__mutmut_51` | `marque.write_text(empreinte, encoding="utf-8")` | `marque.write_text(empreinte, encoding="UTF-8")` | A |
| `stockage.data_lake.xǁDataLakeǁecrire__mutmut_6` | `donnees = contenu.encode("utf-8")` | `donnees = contenu.encode("UTF-8")` | A |
| `stockage.data_lake.xǁDataLakeǁlister__mutmut_14` | `continuation_token = None` | `continuation_token = ""` | P5 |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_43` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, strict=None):` | V |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_46` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, ):` | V |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_47` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, strict=False):` | V |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_69` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, strict=None):` | V |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_72` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, ):` | V |
| `stockage.data_lake.xǁDataLakeǁpublier_dossier__mutmut_73` | `for fichier, logique in zip(fichiers, publies, strict=True):` | `for fichier, logique in zip(fichiers, publies, strict=False):` | V |

---

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
