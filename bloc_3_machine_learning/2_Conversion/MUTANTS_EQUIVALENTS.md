# Mutants équivalents — `conversion_bloc3.py`

Mesure : mutmut 3, sur le module entier, tests
`tests/test_conversion_bloc3.py`. Un mutant n'est nommé ici que si **aucun test ne peut le
distinguer** du code d'origine : même comportement, mêmes valeurs rendues.

| Mutant | Changement | Pourquoi le comportement est identique |
|---|---|---|
| `conversion_bloc3.x_evaluate_classifier__mutmut_32` | `pred_train = (proba_train >= threshold).astype(int)` devient `.astype(None)` | `astype(None)` donne des flottants 0.0 / 1.0 au lieu d'entiers 0 / 1. Ces prédictions restent locales : la fonction ne rend que les métriques et le pipeline. scikit-learn lit des flottants entiers comme des classes binaires (0.0 = 0, 1.0 = 1) : F1, précision, rappel et exactitude sont les mêmes. |
| `conversion_bloc3.x_evaluate_classifier__mutmut_35` | `pred_valid = (proba_valid >= threshold).astype(int)` devient `.astype(None)` | Même raison, pour les prédictions de validation. |

Preuve exécutée : `tests/test_conversion_bloc3.py`,
`test_premisse_les_metriques_ne_dependent_pas_du_type_des_predictions` (le mutant change bien le
type en `float64`, et les quatre métriques sont égales pour les deux types).

Les sept autres survivants de la première mesure du 17/10/2026 (307 tués sur 316) sont tués par
des tests ajoutés le même jour :
- bornes du seuil (`0.0 <= threshold <= 1.0`) : `test_evaluate_classifier_accepte_les_seuils_extremes_zero_et_un` ;
- ajustement sur l'apprentissage (`pipeline.fit(x_train, y_train)`) :
  `test_evaluate_classifier_ajuste_le_modele_sur_l_apprentissage_seulement` ;
- part des visiteurs américains dans un cluster (`s == "US"`) :
  `test_resume_clusters_parts_de_pays_et_de_sources_sur_des_clusters_desequilibres`.

---

*Copyright © 2026 · voir le fichier LICENSE du dépôt*
