# Mutants équivalents — `conversion_bloc3.py`

Mesure : mutmut 3, sur le module entier, tests
`tests/test_conversion_bloc3.py`. Un mutant n'est nommé ici que si **aucun test ne peut le
distinguer** du code d'origine : même comportement, mêmes valeurs rendues.

**Mesure du 21 octobre 2026** (commit `28aa9ba`) : 343 mutants, **341 tués, 2 survivants**, 0
délai dépassé, 0 mutant sans test. Les deux survivants sont nommés ci-dessous. Le verdict
mécanique de la porte est FAIL parce que l'outil compte les mutants équivalents comme des
survivants ; c'est le même état que les autres blocs.

| Mutant | Changement | Pourquoi le comportement est identique |
|---|---|---|
| `conversion_bloc3.x_evaluate_classifier__mutmut_44` | `pred_train = (proba_train >= threshold).astype(int)` devient `.astype(None)` | `astype(None)` donne des flottants 0.0 / 1.0 au lieu d'entiers 0 / 1. Les prédictions restent locales : la fonction ne rend que les métriques et le pipeline. Sur une cible admise, scikit-learn lit des flottants entiers comme des classes binaires (0.0 = 0, 1.0 = 1) : F1, précision, rappel et exactitude sont les mêmes. |
| `conversion_bloc3.x_evaluate_classifier__mutmut_47` | `pred_valid = (proba_valid >= threshold).astype(int)` devient `.astype(None)` | Même raison, pour les prédictions de validation. |

Preuves exécutées :

- `tests/test_conversion_bloc3.py`,
  `test_premisse_les_metriques_ne_dependent_pas_du_type_des_predictions` : le mutant change bien
  le type en `float64`, et les quatre métriques sont égales pour les deux types, sur dix types de
  cible 0 / 1 (entiers, flottants, `Int64`, booléens, catégories) et sur une cible d'une seule
  classe.
- `outils/verifier_equivalence_44_47.py` (21 octobre 2026, hors livrable) : comparaison directe
  de l'original et des deux mutants, sur le module corrigé. Pour une cible admise, les trois
  rendent le même dictionnaire, au caractère près. Pour une cible hors domaine, les trois lèvent
  la même `ValueError`, au message près.

## Ce qui a changé le 20 octobre 2026, et ce que cela change

**Avant.** Les mutants étaient numérotés 32 et 35 (mesure du 17 octobre 2026) et la preuve
donnée dans ce fichier était fausse. Elle disait : « `astype(None)` donne des flottants 0.0 /
1.0 […] scikit-learn lit des flottants entiers comme des classes binaires : F1, précision, rappel
et exactitude sont les mêmes. » Elle ne valait que pour une cible 0 / 1 — et la fonction
n'interdisait pas une autre cible. Le contrôle indépendant du 20 octobre a produit une cible
sans classe 1 (des étiquettes 2) au seuil 1 : scikit-learn échouait alors, et le message de
l'erreur dépendait du type des prédictions (« [0 2] » contre « [0. 2.] »). Les deux mutants
étaient donc distinguables, et la preuve était fausse.

**Le remède (commit `929a50e`).** `evaluate_classifier` refuse désormais, **avant tout
ajustement**, une cible qui contient une valeur autre que 0 et 1 (`_refuser_cible_non_binaire`,
l. 250). Le message nomme l'argument fautif et les valeurs refusées. Aucun calcul n'est changé
pour une cible 0 / 1.

**Ce que le remède change, et ce qu'il ne change pas.** L'entrée qui distinguait les deux
mutants n'existe plus : la fonction la refuse avant d'arriver aux prédictions, et le refus est le
même pour l'original et pour les mutants. Sur toutes les entrées que la fonction **accepte**,
les deux mutants sont redevenus équivalents à l'original : même valeurs rendues, même type
d'exception, même message. C'est exactement le critère retenu par le contrôle indépendant de
mutation (« tout mutant qu'une entrée acceptée par la fonction distingue par un effet
observable »).

**Conséquence.** Les deux mutants sont de nouveau nommés ici, sous leur nouvelle numérotation
(44 et 47). La numérotation change avec le code : elle vient de la mesure du 21 octobre 2026.

## Les sept autres survivants de la première mesure du 17 octobre 2026

Ils sont tués par des tests ajoutés le même jour :

- bornes du seuil (`0.0 <= threshold <= 1.0`) : `test_evaluate_classifier_accepte_les_seuils_extremes_zero_et_un` ;
- ajustement sur l'apprentissage (`pipeline.fit(x_train, y_train)`) : `test_evaluate_classifier_ajuste_le_modele_sur_l_apprentissage_seulement` ;
- part des visiteurs américains dans un cluster (`s == "US"`) : `test_resume_clusters_parts_de_pays_et_de_sources_sur_des_clusters_desequilibres`.

## Les tests ajoutés le 20 octobre 2026

Le remède a sept tests nouveaux dans `tests/test_conversion_bloc3.py` :

- `test_evaluate_classifier_refuse_une_cible_autre_que_zero_ou_un` : sept cas (2, -1, 0,5, NaN,
  `pd.NA`, « 0 » / « 1 » en texte), en apprentissage comme en validation, avec le message exact ;
- `test_evaluate_classifier_refuse_la_cible_avant_d_ajuster_le_modele` : le refus précède
  l'ajustement, le modèle n'a rien vu ;
- `test_evaluate_classifier_accepte_toute_cible_zero_un` : une cible 0 / 1 est admise quel que
  soit son type numérique (booléens, flottants, `Int64`, catégories), et les métriques sont
  identiques à celles de la cible entière ; une cible d'une seule classe aussi ;
- `test_premisse_les_metriques_ne_dependent_pas_du_type_des_predictions` : la prémisse ci-dessus,
  étendue à dix types de cible et à une seule classe.

---

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
