# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests d'intégration : exécutent le notebook Uber avec nbclient et vérifient ses chiffres.

Chaque test copie le projet (notebook et module) dans un dossier temporaire : le notebook y écrit
ses exports sans toucher aux fichiers livrés. Les données (100 Mo) ne sont pas dans le dossier :
les tests qui en ont besoin les cherchent dans la variable d'environnement UBER_DATA_ZIP, puis
dans uber-trip-data.zip à côté du notebook, et sont IGNORÉS (avec la raison) si elles manquent.
Les tests qui n'ont pas besoin des vraies données (archive absente, empreinte fausse) tournent
toujours. Tous sont marqués `notebook` et sont lancés dans la mesure finale.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import nbformat
import pandas as pd
import pytest
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError
from pandas.testing import assert_frame_equal

RACINE = Path(__file__).resolve().parents[1]
NOTEBOOK = "Uber_Pickups_Bloc3_CDSD_v2.ipynb"
FICHIERS = (NOTEBOOK, "uber_bloc3.py")
SORTIES = "uber_outputs"
MOIS = ("apr", "may", "jun", "jul", "aug", "sep")
SANS_RETARD = '                "retard_168h",\n'

pytestmark = pytest.mark.notebook


def archive_des_donnees() -> Path:
    """Archive uber-trip-data.zip : UBER_DATA_ZIP, sinon à côté du notebook ; sinon on ignore."""
    candidats = [os.environ.get("UBER_DATA_ZIP", ""), str(RACINE / "uber-trip-data.zip")]
    for candidat in candidats:
        if candidat and Path(candidat).is_file():
            return Path(candidat)
    pytest.skip("données absentes : définir UBER_DATA_ZIP (voir LIRE_DONNEES.md)")


def dossier_nomme(parent: Path) -> Path:
    """Sous-dossier de `parent` au nom du projet (le notebook affiche ce nom)."""
    dossier = parent / RACINE.name
    dossier.mkdir()
    return dossier


def copier_projet(destination: Path) -> Path:
    """Copie le notebook et son module dans `destination`."""
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
        nb, timeout=1800, kernel_name="python3", resources={"metadata": {"path": str(dossier)}}
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
    archive = archive_des_donnees()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("UBER_DATA_ZIP", str(archive))
        dossier = copier_projet(dossier_nomme(tmp_path_factory.mktemp("uber_complet")))
        return dossier, executer(dossier)


def test_notebook_uber_chiffres_cles(passage_complet):
    """Le notebook entier tourne sans erreur : 6 empreintes conformes, MAE 34,164 contre 36,202."""
    dossier, nb = passage_complet
    texte = texte_des_sorties(nb)
    assert "Empreintes SHA-256 conformes : 6 sur 6" in texte
    assert "Nombre total de lignes brutes : 4 534 327" in texte
    assert "K_HOUR = 6 (silhouette maximale)" in texte
    assert "MAE : 34.2 courses/heure contre 36.2 pour le modèle en place" in texte
    resume = json.loads((dossier / SORTIES / "summary.json").read_text(encoding="utf-8"))
    assert resume["clean_rows"] == 4_513_100
    assert resume["k_day"] == 8
    assert resume["k_hour"] == 6
    prevision = resume["forecast"]
    assert prevision["retained_model"] == "Gradient boosting (HistGradientBoosting)"
    assert prevision["test_mae"] == pytest.approx(34.164, abs=5e-4)
    assert prevision["reference_mae"] == pytest.approx(36.202, abs=5e-4)
    assert prevision["bias_peaks_pct"] == pytest.approx(-17.9, abs=0.05)


def test_notebook_uber_reproduit_les_exports_livres(passage_complet):
    """Les dix fichiers d'agrégats recalculés sont ceux du dossier livré (graines fixées)."""
    dossier, _ = passage_complet
    livres = sorted((RACINE / SORTIES).glob("*.csv"))
    assert [p.name for p in livres] == sorted(p.name for p in (dossier / SORTIES).glob("*.csv"))
    for livre in livres:
        recalcule = pd.read_csv(dossier / SORTIES / livre.name)
        assert_frame_equal(pd.read_csv(livre), recalcule, rtol=1e-9, obj=livre.name)
    attendu = json.loads((RACINE / SORTIES / "summary.json").read_text(encoding="utf-8"))
    obtenu = json.loads((dossier / SORTIES / "summary.json").read_text(encoding="utf-8"))
    assert obtenu == attendu


def test_cellule_16_avec_quatre_zones_par_jour_chelsea_passe_en_tete(tmp_path, monkeypatch):
    """Manipulation du jury : K_DAY = 4. Attendu : Chelsea en tête le vendredi (52,8 %)."""
    monkeypatch.setenv("UBER_DATA_ZIP", str(archive_des_donnees()))
    dossier = copier_projet(tmp_path)
    nb = executer(dossier, 16, [("K_DAY = 8", "K_DAY = 4")])
    texte = texte_des_sorties(nb)
    assert "Chelsea / Flatiron (52.8%" in texte
    assert "Zones dont le repère le plus proche est à plus de 3 km : 5 sur 28" in texte


def test_cellule_29_sans_retard_168h_le_modele_ne_bat_plus_la_regle(tmp_path, monkeypatch):
    """Manipulation du jury : retirer retard_168h du modèle. Le verdict affiché change."""
    monkeypatch.setenv("UBER_DATA_ZIP", str(archive_des_donnees()))
    dossier = copier_projet(tmp_path)
    nb = executer(dossier, 30, [(SANS_RETARD, "")])
    texte = texte_des_sorties(nb)
    assert "MAE : 39.4 contre 36.2" in texte
    assert "le modèle ne bat pas le modèle en place" in texte
    assert "on conserverait la règle simple" in texte


def test_cellule_2_refuse_une_archive_absente(tmp_path, monkeypatch):
    """Sans données (variable vers un fichier absent, rien à côté), la cellule 2 s'arrête."""
    monkeypatch.setenv("UBER_DATA_ZIP", str(tmp_path / "absente.zip"))
    dossier = copier_projet(tmp_path)
    with pytest.raises(CellExecutionError, match="Données introuvables"):
        executer(dossier, 2)
    assert not any((dossier / SORTIES).glob("*"))


def test_cellule_4_compte_les_empreintes_non_conformes_sans_les_cacher(tmp_path, monkeypatch):
    """Une archive aux fichiers modifiés : « 0 sur 6 » est affiché, rien n'est tu."""
    faux = tmp_path / "faux.zip"
    with zipfile.ZipFile(faux, "w") as z:
        for mois in MOIS:
            z.writestr(
                f"uber-trip-data/uber-raw-data-{mois}14.csv",
                "Date/Time,Lat,Lon,Base\n4/1/2014 0:11:00,40.7690,-73.9549,B02512\n",
            )
    monkeypatch.setenv("UBER_DATA_ZIP", str(faux))
    projet = tmp_path / "projet"
    projet.mkdir()
    nb = executer(copier_projet(projet), 4)
    texte = texte_des_sorties(nb)
    assert "Empreintes SHA-256 conformes : 0 sur 6" in texte
    assert "Nombre total de lignes brutes : 6" in texte


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


def test_notebook_uber_sorties_enregistrees_identiques_a_une_execution_neuve(passage_complet):
    """Le notebook déposé affiche exactement ce que son code produit aujourd'hui : chaque texte,
    tableau et chiffre enregistré est retrouvé, cellule par cellule, par une exécution neuve."""
    _, nb = passage_complet
    livre = nbformat.read(RACINE / NOTEBOOK, as_version=4)
    assert sorties_par_cellule(livre) == sorties_par_cellule(nb)
