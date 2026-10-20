# Mutants équivalents — `uber_bloc3.py`

Mesure : mutmut 3, sur le module entier, tests
`tests/test_uber_bloc3.py`. Un mutant n'est nommé ici que si **aucun test ne peut le
distinguer** du code d'origine : même comportement, mêmes valeurs rendues.

| Mutant | Changement | Pourquoi le comportement est identique |
|---|---|---|
| `uber_bloc3.x_fit_hotzones__mutmut_27` | `rng.choice(len(coords), size=fit_n, replace=False)` devient `replace=None` | NumPy ne lit que la vérité de `replace` : `None` est faux, comme `False`. Le tirage des points d'apprentissage est donc le même, point pour point. |
| `uber_bloc3.x_fit_hotzones__mutmut_220` | même changement pour le tirage des points de mesure de la qualité (`size=q_n`) | Même raison : mêmes points tirés, mêmes silhouette, Davies-Bouldin et Calinski-Harabasz. |
| `uber_bloc3.x_distance_au_segment__mutmut_10` | `t = 0.0 if not ab.any() else ...` devient `t = 1.0 if ...` | Cette branche ne sert que si le segment est réduit à un point : `ab = b - a` vaut alors exactement zéro, et `a + t * ab` vaut `a` quel que soit `t`. La distance rendue est la même. |

Preuves exécutées :
- `tests/test_uber_bloc3.py`, `test_numpy_traite_replace_none_comme_replace_false` : avec la même
  graine, `choice(..., replace=None)` tire exactement les mêmes points que `replace=False` ;
- `tests/test_uber_bloc3.py`, `test_distance_au_segment_selon_la_position_du_point`, cas « segment
  réduit à un point » : distance 5 de (3 ; 4) au point (0 ; 0), avec l'une ou l'autre valeur de `t`.

Les cinq autres survivants de la première mesure du 17/10/2026 (672 tués sur 680) sont tués par
des tests ajoutés ou par une écriture plus nette du code :
- rang de densité entier (`astype(int)`) : `test_fit_hotzones_decrit_chaque_zone` vérifie que les
  rangs sont des entiers (les exports écrivent « 1 », pas « 1.0 ») ;
- départ du trait de rappel au milieu de l'étiquette (`largeur / 2`) :
  `test_chercher_position_le_trait_de_rappel_part_du_milieu_de_l_etiquette` ;
- référence positive inférieure à 1 (`mae_ref <= 0`) : deux cas ajoutés à
  `test_qualifier_gain_selon_le_seuil_de_quinze_pour_cent` ;
- conversion des prévisions en double précision (`dtype="float64"`, deux mutants) : `indicateurs`
  convertit désormais les deux séries par une seule règle ; un mutant de cette règle touche le réel
  comme le prévu, et `test_indicateurs_calculent_en_double_precision_meme_depuis_des_float32` le
  tue. Résultats identiques pour le notebook (mêmes opérations en double précision).

---

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
