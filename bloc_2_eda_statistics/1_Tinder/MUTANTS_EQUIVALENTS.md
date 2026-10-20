# Mutants équivalents — `speed_dating.py`

**Aucun mutant équivalent.** Mesure du 19/10/2026 sur le module entier (mutmut 3,
tests `tests/test_speed_dating.py`) : **328 mutants, 328 tués,
aucun survivant**, aucun dépassement de délai. Le seul survivant nommé jusqu'ici (ci-dessous) est
tué par un test depuis la reprise du 17/10/2026. La mesure en vigueur est celle du 19/10/2026.

## Historique : le mutant `disp=None`, d'abord tenu pour équivalent

| Mutant | Changement | Ce qui a été établi | Traitement |
|---|---|---|---|
| `speed_dating.x_logit_grappes__mutmut_23` | `modele.fit(..., disp=False)` devient `modele.fit(..., disp=None)` | **Même comportement** : statsmodels ne lit que la vérité de `disp` pour afficher (« `if disp:` », `statsmodels/base/optimizer.py`, lignes 541 et 547, version 0.15.0 épinglée dans `requirements.txt`) ; `None` et `False` sont faux tous les deux : mêmes coefficients, rien d'affiché. **Mais pas indiscernable** : statsmodels range la valeur reçue telle quelle dans les réglages du résultat (`"disp": disp`, `optimizer.py`, lignes 278 à 288 ; `mlefit.mle_settings = optim_settings`, `statsmodels/base/model.py`, ligne 667). Un test peut donc lire `False` ou `None`. | Mesure du 17/10 (287/288) : nommé équivalent. Reprise après vérification : un mutant qu'un test peut distinguer n'est pas équivalent ; `tests/test_speed_dating.py`, `test_logit_grappes_retire_les_lignes_incompletes_et_n_affiche_rien`, vérifie maintenant que le résultat rendu porte `mle_settings["disp"] is False`. |

Les deux faits sont vérifiés par `tests/test_premisses_mutants_equivalents.py` : mêmes
coefficients et sortie vide avec `disp=None` et `disp=False` ; réglage rangé différent (`None`
contre `False`) ; témoin `disp=True`, qui affiche bien le journal d'optimisation.

Les quinze autres survivants de la première mesure du 17/10 (283 tués sur 299) avaient déjà
disparu : tués par des tests ajoutés (index et type des préférences normalisées, covariance
« cluster », d de Cohen sans unité, V de Cramér sur un tableau 3 × 3), ou supprimés avec le code
inutile qu'ils mutaient (retrait de « .00 », conversion des identifiants en entiers).
