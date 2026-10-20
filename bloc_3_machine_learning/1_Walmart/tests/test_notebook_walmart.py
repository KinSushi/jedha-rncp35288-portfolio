# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests d'intégration : exécutent le notebook Walmart avec nbclient et vérifient ses chiffres.

Chaque test copie le projet (notebook, module, CSV) dans un dossier temporaire : le notebook y
écrit ses exports sans toucher aux fichiers livrés. Ces tests sont marqués `notebook` ; ils sont
lents (le notebook entier prend environ deux minutes) mais ils sont lancés dans la mesure finale.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import nbformat
import pandas as pd
import pytest
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError
from pandas.testing import assert_frame_equal

RACINE = Path(__file__).resolve().parents[1]
NOTEBOOK = "Walmart_Bloc3_CDSD_v2.ipynb"
FICHIERS = (NOTEBOOK, "walmart_bloc3.py", "Walmart_Store_sales.csv")
REGLE = 'OUTLIER_COLS = ["Temperature", "Fuel_Price", "CPI", "Unemployment"]'
DERNIERE_CELLULE_DU_NETTOYAGE = 9

pytestmark = pytest.mark.notebook


def dossier_nomme(parent: Path) -> Path:
    """Sous-dossier de `parent` au nom du projet (le notebook affiche ce nom)."""
    dossier = parent / RACINE.name
    dossier.mkdir()
    return dossier


def copier_projet(destination: Path) -> Path:
    """Copie le notebook, son module et le CSV dans `destination`."""
    for nom in FICHIERS:
        shutil.copy2(RACINE / nom, destination / nom)
    return destination


def executer(dossier: Path, jusqu_a: int | None = None, remplacements=()):
    """Exécute le notebook copié (ou ses premières cellules) ; rend le notebook exécuté.

    `remplacements` liste des couples (ancien, nouveau) appliqués au code des cellules : c'est
    ce que ferait un jury qui modifie une valeur dans une cellule avant de relancer.
    """
    nb = nbformat.read(dossier / NOTEBOOK, as_version=4)
    if jusqu_a is not None:
        nb.cells = nb.cells[: jusqu_a + 1]
    for ancien, nouveau in remplacements:
        trouve = False
        for cellule in nb.cells:
            if cellule.cell_type == "code" and ancien in cellule.source:
                cellule.source = cellule.source.replace(ancien, nouveau)
                trouve = True
        assert trouve, f"{ancien!r} introuvable dans les cellules de code"
    client = NotebookClient(
        nb, timeout=1200, kernel_name="python3", resources={"metadata": {"path": str(dossier)}}
    )
    client.execute()
    return nb


def texte_des_sorties(nb) -> str:
    """Tout le texte affiché par les cellules de code (print et tableaux)."""
    morceaux = []
    for cellule in nb.cells:
        if cellule.cell_type != "code":
            continue
        for sortie in cellule.outputs:
            if sortie.output_type == "stream":
                morceaux.append(sortie.text)
            elif sortie.output_type in {"display_data", "execute_result"}:
                morceaux.append(sortie.data.get("text/plain", ""))
    return "\n".join(morceaux)


@pytest.fixture(scope="module")
def passage_complet(tmp_path_factory):
    """Exécute UNE fois le notebook entier (environ deux minutes) pour les tests qui suivent."""
    dossier = copier_projet(dossier_nomme(tmp_path_factory.mktemp("walmart_complet")))
    return dossier, executer(dossier)


def test_notebook_walmart_chiffres_cles(passage_complet):
    """Le notebook entier tourne sans erreur et retrouve ses chiffres : 131 lignes, Lasso, R²."""
    dossier, nb = passage_complet
    texte = texte_des_sorties(nb)
    assert "Shape après preprocessing pandas : (131, 11)" in texte
    assert "Magasins avant la règle : 20 ; après : 19" in texte
    assert "Magasin(s) supprimé(s) EN ENTIER : [12]" in texte
    assert "Modèle retenu (critère : RMSE de validation croisée sur l'apprentissage) : Lasso" in (
        texte
    )
    assert "Notebook terminé sans erreur." in texte
    metriques = pd.read_csv(dossier / "walmart_bloc3_exam_outputs" / "model_metrics.csv")
    meilleur = metriques.loc[0]
    assert meilleur["model"].startswith("Lasso")
    assert meilleur["test_R2"] == pytest.approx(0.901, abs=5e-4)
    assert meilleur["cv_RMSE_mean"] == pytest.approx(150_194, abs=1)


def test_notebook_walmart_reproduit_les_exports_livres(passage_complet):
    """Les neuf CSV recalculés sont ceux du dossier livré (graines fixées : mêmes chiffres)."""
    dossier, _ = passage_complet
    livres = sorted((RACINE / "walmart_bloc3_exam_outputs").glob("*.csv"))
    assert [p.name for p in livres] == sorted(
        p.name for p in (dossier / "walmart_bloc3_exam_outputs").glob("*.csv")
    )
    for livre in livres:
        recalcule = pd.read_csv(dossier / "walmart_bloc3_exam_outputs" / livre.name)
        assert_frame_equal(pd.read_csv(livre), recalcule, rtol=1e-9, obj=livre.name)


def test_cellule_9_sans_unemployment_ne_plante_pas(tmp_path):
    """Manipulation du jury : retirer Unemployment de OUTLIER_COLS ne doit plus lever d'erreur."""
    dossier = copier_projet(tmp_path)
    regle = 'OUTLIER_COLS = ["Temperature", "Fuel_Price", "CPI"]'
    nb = executer(dossier, DERNIERE_CELLULE_DU_NETTOYAGE, [(REGLE, regle)])
    texte = texte_des_sorties(nb)
    assert "Unemployment n'est pas dans OUTLIER_COLS" in texte
    assert "Magasins avant la règle : 20 ; après : 20" in texte  # le magasin 12 est conservé
    assert "Magasin(s) supprimé(s) EN ENTIER : []" in texte
    assert "Shape après preprocessing pandas : (136, 11)" in texte


def test_cellule_9_avec_quatre_ecarts_types_garde_trois_lignes_du_magasin_12(tmp_path):
    """Manipulation du jury : N_SIGMA = 4. Attendu : 134 lignes et 20 magasins."""
    dossier = copier_projet(tmp_path)
    nb = executer(dossier, DERNIERE_CELLULE_DU_NETTOYAGE, [("N_SIGMA = 3", "N_SIGMA = 4")])
    texte = texte_des_sorties(nb)
    assert "Shape après preprocessing pandas : (134, 11)" in texte
    assert "Magasins avant la règle : 20 ; après : 20" in texte
    assert "Lignes retirées par la règle ±4σ : 2" in texte


def test_cellule_9_refuse_une_colonne_inconnue(tmp_path):
    """Une faute de frappe dans OUTLIER_COLS arrête la cellule avec un ValueError explicite."""
    dossier = copier_projet(tmp_path)
    faute = REGLE.replace("Unemployment", "Unemploymnt")
    with pytest.raises(CellExecutionError, match="Unemploymnt"):
        executer(dossier, DERNIERE_CELLULE_DU_NETTOYAGE, [(REGLE, faute)])


def test_cellule_9_refuse_un_sigma_nul(tmp_path):
    """N_SIGMA = 0 donnerait un intervalle réduit à la moyenne : refusé."""
    dossier = copier_projet(tmp_path)
    with pytest.raises(CellExecutionError, match="strictement positif"):
        executer(dossier, DERNIERE_CELLULE_DU_NETTOYAGE, [("N_SIGMA = 3", "N_SIGMA = 0")])


def test_cellule_3_refuse_un_csv_absent_sans_rien_ecrire(tmp_path):
    """Sans le CSV à côté du notebook, la cellule 3 s'arrête et n'écrit aucun export (inchangé)."""
    dossier = copier_projet(tmp_path)
    (dossier / "Walmart_Store_sales.csv").unlink()
    with pytest.raises(CellExecutionError, match="Placez Walmart_Store_sales.csv"):
        executer(dossier, 3)
    assert not any((dossier / "walmart_bloc3_exam_outputs").glob("*.csv"))


def sorties_par_cellule(nb) -> list[tuple[int, str, str, str]]:
    """Par cellule de code : texte des flux (fusionnés), text/plain et text/markdown affichés.

    Le HTML (schémas des estimateurs scikit-learn) et les images ne sont pas comparés : le HTML
    dépend de l'encodage de la console, les images sont des graphiques déjà écrits dans les
    exports. Tous les chiffres affichés passent par les flux, text/plain ou text/markdown.
    """
    resultat = []
    for i, cellule in enumerate(nb.cells):
        if cellule.cell_type != "code":
            continue
        flux, brut, markdown = "", [], []
        for sortie in cellule.outputs:
            if sortie.output_type == "stream":
                flux += sortie.text
            elif sortie.output_type in {"display_data", "execute_result"}:
                brut.append(sortie.data.get("text/plain", ""))
                markdown.append(sortie.data.get("text/markdown", ""))
        resultat.append((i, flux, "\n".join(brut), "\n".join(markdown)))
    return resultat


def test_notebook_walmart_sorties_enregistrees_identiques_a_une_execution_neuve(passage_complet):
    """Le notebook déposé affiche exactement ce que son code produit aujourd'hui : chaque texte,
    tableau et chiffre enregistré est retrouvé, cellule par cellule, par une exécution neuve."""
    _, nb = passage_complet
    livre = nbformat.read(RACINE / NOTEBOOK, as_version=4)
    assert sorties_par_cellule(livre) == sorties_par_cellule(nb)
