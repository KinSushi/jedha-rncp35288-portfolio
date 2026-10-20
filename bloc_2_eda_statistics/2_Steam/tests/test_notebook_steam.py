# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Test d'intégration : le notebook Steam se rejoue en entier et redonne les chiffres du jury.

Le notebook est copié avec son module et ses données dans un dossier temporaire, puis exécuté
par nbclient : le projet n'est jamais modifié. L'exécution dure de deux à un peu plus de trois
minutes (135,5 s : réexécution enregistrée du 17/10/2026 ; 133 à 188 s : huit rejeux de ce test
du 17/10 au 19/10 au matin), et 512 s sur une machine saturée (19/10, midi) ;
le test est donc marqué `notebook`. Les durées de l'expérience dépendent de la machine : elles ne
sont comparées à rien, seuls leur nombre et leur signe le sont.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import nbformat
import pytest
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

RACINE = Path(__file__).resolve().parent.parent
NOTEBOOK = "Steam_Bloc2_CDSD_PySpark.ipynb"
DONNEES = Path("donnees") / "steam_game_output.json"

pytestmark = pytest.mark.notebook


def copier_projet(destination: Path, avec_donnees: bool = True) -> Path:
    """Copie le notebook, son module et (sauf demande contraire) le fichier de données."""
    for nom in (NOTEBOOK, "steam_analyse.py"):
        shutil.copy2(RACINE / nom, destination / nom)
    if avec_donnees:
        (destination / DONNEES).parent.mkdir()
        shutil.copy2(RACINE / DONNEES, destination / DONNEES)
    return destination / NOTEBOOK


def executer(chemin: Path) -> nbformat.NotebookNode:
    """Exécute le notebook dans son dossier (les chemins relatifs sont donc vérifiés)."""
    notebook = nbformat.read(str(chemin), as_version=4)
    client = NotebookClient(
        notebook,
        timeout=1800,
        kernel_name="python3",
        resources={"metadata": {"path": str(chemin.parent)}},
    )
    client.execute()
    return notebook


def textes_des_sorties(notebook: nbformat.NotebookNode) -> dict[int, str]:
    """Texte des sorties de chaque cellule de code (flux et résultats), par indice de cellule."""
    resultat: dict[int, str] = {}
    for indice, cellule in enumerate(notebook.cells):
        if cellule.cell_type != "code":
            continue
        morceaux: list[str] = []
        for sortie in cellule.outputs:
            if sortie.output_type == "stream":
                morceaux.append(sortie.text)
            elif sortie.output_type in ("execute_result", "display_data"):
                morceaux.append(sortie.data.get("text/plain", ""))
        resultat[indice] = "".join(morceaux)
    return resultat


@pytest.fixture(scope="module")
def notebook_execute(tmp_path_factory: pytest.TempPathFactory) -> nbformat.NotebookNode:
    dossier = tmp_path_factory.mktemp("steam")
    return executer(copier_projet(dossier))


def test_notebook_rejoue_sans_erreur_ni_chemin_de_machine(
    notebook_execute: nbformat.NotebookNode,
) -> None:
    en_erreur = [
        i
        for i, cellule in enumerate(notebook_execute.cells)
        if cellule.cell_type == "code"
        and any(sortie.output_type == "error" for sortie in cellule.outputs)
    ]
    assert en_erreur == []
    tout = "\n".join(textes_des_sorties(notebook_execute).values())
    assert not re.search(r"[A-Za-z]:\\\\?Users|/Users/|file:/|/home/", tout)


def test_notebook_rejoue_les_chiffres_du_jury(notebook_execute: nbformat.NotebookNode) -> None:
    tout = "\n".join(textes_des_sorties(notebook_execute).values())
    attendus = [
        # lecture et nettoyage (sections 1 et 2)
        "Spark 3.5.9 | exécution : locale",
        "55 691 enregistrements lus en",
        " - tags : structure de 441 champs (votes par étiquette)",
        "Âges requis aberrants (> 21 ans) mis à NULL : 5",
        "Jeux retenus : 55 690",
        "Lignes (jeu, genre) : 157110 | lignes (jeu, langue) : 202293",
        "Éditeurs distincts — avant normalisation : 29818 | après : 29434",
        "Jeux à plusieurs éditeurs : 2006",
        # éditeurs (3.1) : Ubisoft, huitième ex æquo, une fois ses écritures regroupées
        "Jeux publiés par les 10 premiers éditeurs : 1851 (3.3% du catalogue)",
        # plateformes et langues
        "Jeux d'au moins 500 avis : 7767",
        # parallélisme (section 7) : les 441 totaux sont identiques entre Spark et pandas
        "Totaux identiques pour les 441 tags (écart maximal : 0 vote)",
    ]
    manquants = [texte for texte in attendus if texte not in tout]
    assert manquants == []


def test_notebook_sorties_enregistrees_inchangees_hors_durees_et_plan(
    notebook_execute: nbformat.NotebookNode,
) -> None:
    """Les sorties enregistrées sont celles que produit l'exécution, sauf ce qui dépend du moment.

    Écartées : la lecture du fichier (durée), l'expérience de parallélisme (durées), la
    comparaison avec pandas (durée) et le plan d'exécution (numéros internes de Spark).
    """
    livre = textes_des_sorties(nbformat.read(str(RACINE / NOTEBOOK), as_version=4))
    frais = textes_des_sorties(notebook_execute)
    ecartees = {3, 41, 42, 43}
    differentes = [i for i in livre if i not in ecartees and livre[i] != frais[i]]
    assert differentes == []


def test_notebook_mesure_une_acceleration_croissante_avec_les_partitions(
    notebook_execute: nbformat.NotebookNode,
) -> None:
    """Plus de partitions, plus vite : la référence vaut 1 et le meilleur réglage la dépasse."""
    tableau = next(
        texte
        for texte in textes_des_sorties(notebook_execute).values()
        if texte.startswith("Cœurs disponibles")
    )
    accelerations = [float(m) for m in re.findall(r"(\d+\.\d+)\s*$", tableau, re.MULTILINE)]
    assert accelerations[0] == 1.0  # une partition : la référence
    assert max(accelerations) > 1.5


def test_notebook_sans_donnees_echoue_au_chargement(tmp_path: Path) -> None:
    chemin = copier_projet(tmp_path, avec_donnees=False)
    with pytest.raises(CellExecutionError, match="steam_game_output.json"):
        executer(chemin)
