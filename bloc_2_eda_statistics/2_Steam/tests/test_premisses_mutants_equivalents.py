# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Prémisses des mutants équivalents nommés dans `MUTANTS_EQUIVALENTS.md`.

Un mutant est dit équivalent quand aucun test ne peut le distinguer du code d'origine. Cette
affirmation repose sur un fait de bibliothèque : ce fichier le vérifie, pour qu'il ne reste pas
une simple affirmation.
"""

from __future__ import annotations

from collections.abc import Iterator

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import pytest

matplotlib.use("Agg")


@pytest.fixture
def figure_propre() -> Iterator[None]:
    plt.close("all")
    yield
    plt.close("all")


@pytest.mark.parametrize("type_graphique", ["barh", "bar", "line"])
def test_pandas_ne_dessine_aucune_legende_avec_legend_none_comme_avec_legend_false(
    type_graphique: str, figure_propre: None
) -> None:
    # Mutants montrer_graphique « legend=False » -> « legend=None » : pandas ne teste que la
    # vérité de `legend` ; None et False sont faux tous les deux, aucune légende n'est dessinée.
    donnees = pd.DataFrame({"lettre": ["a", "b", "c"], "n": [3, 5, 4]})
    legendes = []
    for legende in (False, None, True):
        _, ax = plt.subplots()
        getattr(donnees.plot, type_graphique)(x="lettre", y="n", ax=ax, legend=legende)
        legendes.append(ax.get_legend() is not None)
    # Témoin : avec legend=True, la légende apparaît ; le test n'est donc pas vide.
    assert legendes == [False, False, True]
