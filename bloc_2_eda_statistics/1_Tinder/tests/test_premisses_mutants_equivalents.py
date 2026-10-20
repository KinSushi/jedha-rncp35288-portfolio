# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Faits de bibliothèque cités par `MUTANTS_EQUIVALENTS.md`.

Un mutant est dit équivalent quand aucun test ne peut le distinguer du code d'origine. Le mutant
`disp=None` a d'abord été tenu pour équivalent, puis reconnu discernable (le réglage reçu est
gardé dans le résultat) et tué par un test. Ce fichier vérifie les deux faits, pour qu'ils ne
restent pas de simples affirmations.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import statsmodels.formula.api as smf


def test_statsmodels_n_affiche_rien_avec_disp_none_comme_avec_disp_false(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Mutant logit_grappes « disp=False » -> « disp=None » : statsmodels ne teste que la vérité
    # de `disp` (statsmodels/base/optimizer.py, lignes 541 et 547 : « if disp: »). None et False
    # sont faux tous les deux : même ajustement, et rien n'est affiché dans les deux cas.
    hasard = np.random.default_rng(3)
    x = hasard.normal(size=200)
    donnees = pd.DataFrame({"x": x, "dec": (hasard.uniform(size=200) < 1 / (1 + np.exp(-x)))})
    donnees["dec"] = donnees["dec"].astype(int)
    avec_false = smf.logit("dec ~ x", data=donnees).fit(disp=False)
    avec_none = smf.logit("dec ~ x", data=donnees).fit(disp=None)
    assert avec_none.params["x"] == avec_false.params["x"]
    sortie = capsys.readouterr()
    assert (sortie.out, sortie.err) == ("", "")
    # Mais la valeur reçue est rangée telle quelle dans le résultat (optimizer.py, lignes 278 à
    # 288 ; model.py, ligne 667) : c'est ce qui rend le mutant discernable, donc tuable.
    assert (avec_false.mle_settings["disp"], avec_none.mle_settings["disp"]) == (False, None)
    # Témoin : avec disp=True, le journal apparaît bien ; le test ci-dessus n'est donc pas vide.
    smf.logit("dec ~ x", data=donnees).fit(disp=True)
    assert "Optimization terminated successfully" in capsys.readouterr().out
