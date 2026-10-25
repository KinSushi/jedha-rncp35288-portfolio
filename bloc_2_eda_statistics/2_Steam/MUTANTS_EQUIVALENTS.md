# Mutants équivalents : `steam_analyse.py`

Mesure en vigueur : mutmut 3, lancé sous WSL (le sous-système Windows pour Linux), sur le module
**entier** et le code du 19/10/2026 (refus du 18/10 compris), tests `tests/test_steam_analyse.py` :
968 mutants, 965 tués. Les trois survivants sont les trois mêmes qu'aux mesures du 17/10 (mêmes
noms, même changement), et tous les trois sont équivalents : aucun test ne peut les distinguer du
code d'origine, car le comportement est identique. Le changement de chaque mutant ci-dessous est
celui que rend `mutmut show <nom>` dans la copie mutée lors de cette mesure (relu le 19/10).

Portée de « équivalent » ici : la fonction rend `None` et sa seule sortie est la figure. Avec
`legend=None` comme avec `legend=False`, pandas ne range la valeur que dans son objet de tracé
interne, qui n'est rendu ni gardé nulle part, et ne s'en sert que par sa vérité (« `if
self.legend:` » ; la seule autre lecture, `self.legend == "reverse"`, est fausse dans les deux
cas). Seul un test qui remplacerait la méthode de tracé de pandas pour lire ses arguments verrait
une différence : il contrôlerait le texte de l'appel, pas un comportement.

| Mutant | Changement | Pourquoi il est équivalent | Preuve |
|---|---|---|---|
| `steam_analyse.x_montrer_graphique__mutmut_25` | barres horizontales : `legend=False` devient `legend=None` | pandas ne lit que la vérité de `legend` (« `if self.legend:` », `pandas/plotting/_matplotlib/core.py`, ligne 902, pandas 3.0.6 épinglé dans `requirements.txt`). `None` et `False` sont faux tous les deux : aucune légende n'est dessinée. | `tests/test_premisses_mutants_equivalents.py`, cas `barh` : aucune légende avec `False` ni avec `None` ; témoin `True`, qui en dessine une. |
| `steam_analyse.x_montrer_graphique__mutmut_41` | barres verticales : même changement | même raison | même test, cas `bar` |
| `steam_analyse.x_montrer_graphique__mutmut_52` | courbe : même changement | même raison | même test, cas `line` |

Historique de la mesure : le 17/10, 820 mutants tués sur 952 au premier passage, puis 934 sur 938,
puis 935 sur 938 (les trois équivalents ci-dessus). Les autres survivants ont été tués par des
tests (noms de champs lus avec leur casse exacte, tableaux recalculés pour chaque test, départage
des écritures d'éditeurs, colonne en trop sur un graphique, mois d'une date sans jour), ou ont
disparu avec une écriture plus simple du code (couleur unique, réglages de partitions). Les refus
ajoutés le 18/10 (tag demandé deux fois, quantile z nul ou négatif) portent le module à 968
mutants ; aucun de leurs mutants ne survit.

*Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC*
