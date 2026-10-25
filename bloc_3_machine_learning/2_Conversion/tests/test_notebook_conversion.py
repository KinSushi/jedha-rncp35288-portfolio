# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests d'intégration : exécutent le notebook Conversion avec nbclient et vérifient ses chiffres.

Chaque test copie le projet (notebook, module, CSV) dans un dossier temporaire : le notebook y
écrit ses exports sans toucher aux fichiers livrés. Ces tests sont marqués `notebook` ; ils sont
lents (le notebook entier prend environ une minute) mais ils sont lancés dans la mesure finale.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import nbformat
import pandas as pd
import pytest
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError
from pandas.testing import assert_frame_equal

RACINE = Path(__file__).resolve().parents[1]
NOTEBOOK = "Conversion_Rate_Bloc3_CDSD_v2.ipynb"
FICHIERS = (
    NOTEBOOK,
    "conversion_bloc3.py",
    "conversion_data_train.csv",
    "conversion_data_test.csv",
)
SORTIES = "outputs_bloc3_conversion_commente"
SOUMISSION = "conversion_data_test_predictions_CDSD_Bloc3_LogReg_commente.csv"
PROBABILITES = "conversion_data_test_predictions_with_probabilities_CDSD_Bloc3_LogReg_commente.csv"
SEUIL_DU_MEILLEUR = "best_threshold = float(best_row[\"threshold\"])"
CELLULE_MEILLEUR = 31

pytestmark = pytest.mark.notebook


def dossier_nomme(parent: Path) -> Path:
    """Sous-dossier de `parent` au nom du projet (le notebook affiche ce nom)."""
    dossier = parent / RACINE.name
    dossier.mkdir()
    return dossier


def copier_projet(destination: Path) -> Path:
    """Copie le notebook, son module et les deux CSV dans `destination`."""
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
    """Exécute UNE fois le notebook entier (environ une minute) pour les tests qui suivent."""
    dossier = copier_projet(dossier_nomme(tmp_path_factory.mktemp("conversion_complet")))
    return dossier, executer(dossier)


def test_notebook_conversion_chiffres_cles(passage_complet):
    """Le notebook entier tourne sans erreur et retrouve ses chiffres : F1 0,7768 au seuil 0,45."""
    dossier, nb = passage_complet
    texte = texte_des_sorties(nb)
    assert "Train shape: (284580, 6)" in texte
    assert "Meilleur modèle : Logistic Regression C=10" in texte
    assert "Seuil optimisé : 0.450" in texte
    assert "F1 validation        : 0.7768" in texte
    assert "Taux de positifs prédits sur le test : 2.75%" in texte
    resume = json.loads((dossier / SORTIES / "summary_results.json").read_text(encoding="utf-8"))
    assert resume["best_model"] == "Logistic Regression C=10"
    assert resume["best_threshold"] == pytest.approx(0.45)
    assert resume["validation_f1"] == pytest.approx(0.776796, abs=1e-6)
    assert resume["k_best_silhouette"] == 2
    assert resume["k_best_davies_bouldin"] == 4
    soumission = pd.read_csv(dossier / SOUMISSION)
    assert list(soumission.columns) == ["converted"]
    assert soumission.shape == (31620, 1)
    assert set(soumission["converted"]) == {0, 1}


def test_notebook_conversion_reproduit_les_exports_livres(passage_complet):
    """Les fichiers recalculés sont ceux du dossier livré (graines fixées : mêmes chiffres)."""
    dossier, _ = passage_complet
    assert_frame_equal(pd.read_csv(RACINE / SOUMISSION), pd.read_csv(dossier / SOUMISSION))
    assert_frame_equal(
        pd.read_csv(RACINE / PROBABILITES), pd.read_csv(dossier / PROBABILITES), rtol=1e-9
    )
    livre = json.loads((RACINE / SORTIES / "summary_results.json").read_text(encoding="utf-8"))
    fichier = dossier / SORTIES / "summary_results.json"
    recalcule = json.loads(fichier.read_text(encoding="utf-8"))
    assert recalcule == pytest.approx(livre, rel=1e-9)
    matrice = "matrice_angles_morts_reponses_jury.csv"
    assert_frame_equal(
        pd.read_csv(RACINE / SORTIES / matrice), pd.read_csv(dossier / SORTIES / matrice)
    )
    for figure in sorted((RACINE / SORTIES).glob("*.png")):
        assert (dossier / SORTIES / figure.name).stat().st_size > 0, figure.name


def test_cellule_31_avec_un_seuil_impose_change_la_matrice_de_confusion(tmp_path):
    """Manipulation du jury : seuil 0,30 puis 0,60. Attendu : F1 de 0,7651 et 0,7446."""
    dossier = copier_projet(tmp_path)
    nb = executer(dossier, CELLULE_MEILLEUR, [(SEUIL_DU_MEILLEUR, "best_threshold = 0.30")])
    texte = texte_des_sorties(nb)
    assert "Seuil optimisé : 0.300" in texte
    assert "F1 validation        : 0.7651" in texte
    bas = executer(dossier, CELLULE_MEILLEUR, [(SEUIL_DU_MEILLEUR, "best_threshold = 0.60")])
    assert "F1 validation        : 0.7446" in texte_des_sorties(bas)


def test_cellule_19_sans_le_pays_le_f1_baisse(tmp_path):
    """Manipulation du jury : retirer le pays du modèle. Attendu : F1 0,7584, seuil 0,37."""
    dossier = copier_projet(tmp_path)
    nb = executer(
        dossier,
        CELLULE_MEILLEUR,
        [('cat_cols = ["country", "source"]', 'cat_cols = ["source"]')],
    )
    texte = texte_des_sorties(nb)
    assert "F1 validation        : 0.7584" in texte
    assert "Seuil optimisé : 0.370" in texte


def test_cellule_7_refuse_un_train_sans_la_cible(tmp_path):
    """Sans la colonne converted dans le train, le contrôle de schéma arrête le notebook."""
    dossier = copier_projet(tmp_path)
    train = pd.read_csv(dossier / "conversion_data_train.csv").drop(columns="converted")
    train.to_csv(dossier / "conversion_data_train.csv", index=False)
    with pytest.raises(CellExecutionError, match="cible 'converted' est absente"):
        executer(dossier, 7)


def test_cellule_6_refuse_un_csv_de_test_absent_sans_rien_exporter(tmp_path):
    """Sans le CSV de test, la lecture échoue et aucune figure ni aucun export n'est écrit."""
    dossier = copier_projet(tmp_path)
    (dossier / "conversion_data_test.csv").unlink()
    with pytest.raises(CellExecutionError, match="conversion_data_test.csv"):
        executer(dossier, 6)
    assert not any((dossier / SORTIES).glob("*"))


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


def test_notebook_conversion_sorties_enregistrees_identiques_a_une_execution_neuve(
    passage_complet,
):
    """Le notebook déposé affiche exactement ce que son code produit aujourd'hui : chaque texte,
    tableau et chiffre enregistré est retrouvé, cellule par cellule, par une exécution neuve."""
    _, nb = passage_complet
    livre = nbformat.read(RACINE / NOTEBOOK, as_version=4)
    assert sorties_par_cellule(livre) == sorties_par_cellule(nb)
