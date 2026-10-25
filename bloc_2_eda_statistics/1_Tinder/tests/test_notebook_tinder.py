# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Test d'intégration : le notebook Tinder se rejoue en entier et redonne les chiffres du jury.

Le notebook est copié avec son module et ses données dans un dossier temporaire, puis exécuté
par nbclient : le projet n'est jamais modifié. Un échec ici dit que le notebook livré ne se
rejoue plus, ou qu'un chiffre cité à l'oral a changé.
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
NOTEBOOK = "Speed_Dating_Bloc2_CDSD.ipynb"
DONNEES = "Speed+Dating+Data.csv"

pytestmark = pytest.mark.notebook


def copier_projet(destination: Path, avec_donnees: bool = True) -> Path:
    """Copie le notebook, son module et (sauf demande contraire) le fichier de données."""
    for nom in (NOTEBOOK, "speed_dating.py"):
        shutil.copy2(RACINE / nom, destination / nom)
    if avec_donnees:
        shutil.copy2(RACINE / DONNEES, destination / DONNEES)
    return destination / NOTEBOOK


def executer(chemin: Path) -> nbformat.NotebookNode:
    """Exécute le notebook dans son dossier (les chemins relatifs sont donc vérifiés)."""
    notebook = nbformat.read(str(chemin), as_version=4)
    client = NotebookClient(
        notebook,
        timeout=600,
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
    dossier = tmp_path_factory.mktemp("tinder")
    return executer(copier_projet(dossier))


def test_notebook_rejoue_les_chiffres_du_jury(notebook_execute: nbformat.NotebookNode) -> None:
    en_erreur = [
        i
        for i, cellule in enumerate(notebook_execute.cells)
        if cellule.cell_type == "code"
        and any(sortie.output_type == "error" for sortie in cellule.outputs)
    ]
    assert en_erreur == []
    tout = "\n".join(textes_des_sorties(notebook_execute).values())
    assert not re.search(r"[A-Za-z]:\\\\?Users|/Users/|/home/", tout)  # aucun chemin de machine
    attendus = [
        # structure des données (section 1 et 2)
        "Forme du jeu brut : (8378, 195)",
        "Nombre de participants uniques (iid) : 551",
        "Lignes appariées à leur ligne miroir : 8368 = 4184 rendez-vous × 2",
        "dec d'une ligne = dec_o de sa ligne miroir : 100%",
        # premier constat (section 3)
        'Taux de "oui" : 41.99 % des 8378 décisions',
        "Taux de match réciproque : 16.49 % des 4184 rendez-vous",
        # ce qui fait dire oui (section 4) : rapports de cotes de l'attirance, femmes puis hommes
        "{'attr_z': 2.24,",
        "{'attr_z': 3.48,",
        "Différence hommes moins femmes du coefficient de l'attirance : "
        "+0.439 (log-cote), p = 1.03e-03",
        # le contre-exemple de la pseudo-réplication (test d)
        "Rendez-vous : 4105 ; écart-type de int_corr : 0.304",
        "contre-exemple, à tort sur 8220 lignes (rendez-vous doublés) : p = 2.47e-02",
        # les six tests (section 5)
        "Welch : t = 8.85, p = 1.51e-17, d de Cohen = 0.76",
        "Wilcoxon : W = 5052, p = 1.10e-74, rang bisérial = 0.93",
        "t apparié = 13.48, p = 6.20e-36, d = 0.58",
        "Seuil de Bonferroni : 0.05 / 6 = 0.0083",
    ]
    manquants = [texte for texte in attendus if texte not in tout]
    assert manquants == []


def test_notebook_decide_trois_rejets_puis_trois_conservations_dans_l_ordre_des_tests(
    notebook_execute: nbformat.NotebookNode,
) -> None:
    tableau = next(
        texte
        for texte in textes_des_sorties(notebook_execute).values()
        # le tableau des six tests, et non celui des douze interactions (« … pour les 12 … »)
        if texte.startswith("Seuil de Bonferroni : 0.05 / 6")
    )
    decisions = re.findall(r"H0 rejetée|H0 conservée", tableau)
    # (a) Welch, (b) Wilcoxon, (f) t apparié : rejetés ; (c) origine, (d) intérêts, (e) rang : non.
    assert decisions == [
        "H0 rejetée",
        "H0 rejetée",
        "H0 conservée",
        "H0 conservée",
        "H0 conservée",
        "H0 rejetée",
    ]


def test_notebook_sorties_enregistrees_inchangees_apres_reexecution(
    notebook_execute: nbformat.NotebookNode,
) -> None:
    """Les sorties enregistrées dans le notebook livré sont celles que produit son exécution."""
    livre = textes_des_sorties(nbformat.read(str(RACINE / NOTEBOOK), as_version=4))
    frais = textes_des_sorties(notebook_execute)
    differentes = [i for i in livre if livre[i] != frais[i]]
    assert differentes == []


def test_notebook_sans_donnees_echoue_au_chargement(tmp_path: Path) -> None:
    chemin = copier_projet(tmp_path, avec_donnees=False)
    with pytest.raises(CellExecutionError, match="Speed\\+Dating\\+Data.csv"):
        executer(chemin)


def test_notebook_refuse_de_reprendre_une_colonne_protegee_par_le_rgpd(tmp_path: Path) -> None:
    chemin = copier_projet(tmp_path)
    notebook = nbformat.read(str(chemin), as_version=4)
    cellule = next(c for c in notebook.cells if "colonnes_propres = [" in c.source)
    cellule.source = cellule.source.replace('    "iid",\n', '    "iid",\n    "race",\n', 1)
    assert '"race",' in cellule.source
    nbformat.write(notebook, str(chemin))
    with pytest.raises(CellExecutionError, match="protégée"):
        executer(chemin)
