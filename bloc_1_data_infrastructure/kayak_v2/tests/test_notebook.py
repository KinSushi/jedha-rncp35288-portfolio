# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Test d'intégration : le notebook Kayak_Bloc1_CDSD.ipynb s'exécute de bout en bout, HORS LIGNE.

Le notebook ne collecte rien : il relit le data lake archivé, relance l'ETL Spark sur les fichiers
bruts de la collecte du 11/10/2026, interroge l'entrepôt SQL et dessine les cartes. Ce test le
prouve de deux façons : (1) il exécute le notebook dans une COPIE du projet (le lake livré n'est
pas touché) avec une garde (tests/hors_ligne/sitecustomize.py) qui interdit toute connexion hors
de la machine, et prive de réseau le navigateur que lance l'export PNG (mandataire local
injoignable) ; (2) il vérifie que cette garde refuse bien une connexion sortante. Il vérifie
ensuite les chiffres clés affichés par le notebook. Le test est long (ETL Spark, cartes) : il porte
le marqueur ``notebook``.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

import nbformat
import pytest
from nbclient import NotebookClient

from kayak.pipeline import lire_parquet
from kayak.stockage.data_lake import DataLake

RACINE = Path(__file__).resolve().parent.parent
GARDE = Path(__file__).resolve().parent / "hors_ligne"
NOTEBOOK = "Kayak_Bloc1_CDSD.ipynb"
# Port 9 (« discard ») de la boucle locale : aucun service n'y répond sur ce poste (connexion
# refusée, mesuré le 17/10/2026).
MANDATAIRE_INJOIGNABLE = "http://127.0.0.1:9"


def _copier_projet(destination: Path) -> None:
    """Copie le strict nécessaire à l'exécution : paquet, data lake, requêtes SQL, notebook."""
    shutil.copytree(
        RACINE / "kayak", destination / "kayak", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copytree(
        RACINE / "datalake", destination / "datalake", ignore=shutil.ignore_patterns(".tmp")
    )
    (destination / "entrepot").mkdir()
    shutil.copy2(
        RACINE / "entrepot" / "requetes_exemple.sql",
        destination / "entrepot" / "requetes_exemple.sql",
    )
    shutil.copy2(RACINE / NOTEBOOK, destination / NOTEBOOK)


def _sorties_texte(notebook: nbformat.NotebookNode) -> str:
    """Tout le texte affiché par les cellules de code (flux, résultats), dans l'ordre."""
    morceaux: list[str] = []
    for cellule in notebook.cells:
        if cellule.cell_type != "code":
            continue
        for sortie in cellule.get("outputs", []):
            if sortie.output_type == "stream":
                morceaux.append(sortie.text)
            elif sortie.output_type in ("execute_result", "display_data"):
                morceaux.append(sortie.get("data", {}).get("text/plain", ""))
    return "\n".join(morceaux)


def test_la_garde_hors_ligne_refuse_une_connexion_sortante() -> None:
    """Une connexion vers un hôte distant échoue avec le code de sortie non nul de l'interpréteur.

    Sans cette preuve, l'exécution « hors ligne » du notebook ne prouverait rien : la garde
    pourrait être sans effet. Ni la résolution de nom ni la connexion ne sortent de la machine.
    """
    # -I ignore PYTHONPATH : on ajoute donc le dossier de la garde au chemin dans le code lui-même.
    code = (
        f"import sys; sys.path.insert(0, {str(GARDE)!r}); import sitecustomize, socket; "
        "socket.create_connection(('example.com', 80), timeout=3)"
    )
    resultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert resultat.returncode != 0
    assert "réseau interdit (mode hors ligne)" in resultat.stderr


def test_la_garde_hors_ligne_laisse_passer_la_boucle_locale() -> None:
    """Une connexion vers 127.0.0.1 reste permise : le noyau Jupyter et Spark en ont besoin."""
    code = (
        f"import sys; sys.path.insert(0, {str(GARDE)!r}); import sitecustomize, socket; "
        "serveur = socket.socket(); serveur.bind(('127.0.0.1', 0)); serveur.listen(1); "
        "socket.create_connection(serveur.getsockname(), timeout=3).close(); print('local-ok')"
    )
    resultat = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert resultat.returncode == 0, resultat.stderr
    assert "local-ok" in resultat.stdout


def test_le_navigateur_de_l_export_png_recoit_le_mandataire_injoignable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Avec CHOREO_PROXY_SERVER, le navigateur de Kaleido est lancé avec ce mandataire.

    C'est ce qui prive le navigateur de réseau pendant le test du notebook : sans cette option, il
    chargerait les tuiles du fond de carte depuis Internet. Le navigateur n'est pas démarré ici :
    on lit seulement la ligne de commande que Choreographer construirait.
    """
    from choreographer.browsers.chromium import Chromium
    from choreographer.channels import Pipe

    monkeypatch.setenv("CHOREO_PROXY_SERVER", MANDATAIRE_INJOIGNABLE)
    executable = tmp_path / "navigateur.exe"
    executable.write_bytes(b"")  # Choreographer exige seulement que le fichier existe
    profil = tmp_path / "profil"
    profil.mkdir()
    canal = Pipe()
    try:
        navigateur = Chromium(canal, path=executable, tmp_dir=str(profil))
        navigateur.pre_open()
        ligne_de_commande = list(navigateur.get_cli())
    finally:
        canal.close()
    assert f"--proxy-server={MANDATAIRE_INJOIGNABLE}" in ligne_de_commande


def test_le_mandataire_injoignable_refuse_toute_connexion() -> None:
    """Le mandataire donné au navigateur ne répond pas : toute connexion vers lui est refusée."""
    with pytest.raises(ConnectionRefusedError):
        adresse = urlsplit(MANDATAIRE_INJOIGNABLE)
        socket.create_connection((adresse.hostname, adresse.port), timeout=3).close()


@pytest.mark.notebook
@pytest.mark.skipif(
    not (RACINE / "datalake").is_dir(),
    reason="data lake absent : il n'est pas publié (voir OBTENIR_LES_DONNEES.md)",
)
def test_notebook_rejoue_l_etl_hors_ligne_et_donne_les_chiffres_cles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le notebook s'exécute sans erreur, sans réseau, et affiche les chiffres du projet."""
    _copier_projet(tmp_path)
    chemin_pythonpath = os.pathsep.join(
        [str(GARDE), *([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])]
    )
    monkeypatch.setenv("PYTHONPATH", chemin_pythonpath)  # hérité par le noyau et par Spark
    # La garde ne couvre que Python. Le navigateur que Kaleido lance pour l'export PNG est un
    # autre programme : il chargerait le fond de carte OpenStreetMap depuis Internet.
    # Choreographer (qui lance ce navigateur) lit CHOREO_PROXY_SERVER et le passe en option
    # --proxy-server ; ce mandataire local n'existe pas, donc le navigateur n'atteint rien.
    monkeypatch.setenv("CHOREO_PROXY_SERVER", MANDATAIRE_INJOIGNABLE)
    notebook = nbformat.read(str(tmp_path / NOTEBOOK), as_version=4)
    NotebookClient(
        notebook,
        timeout=1800,
        kernel_name="python3",
        resources={"metadata": {"path": str(tmp_path)}},
    ).execute()  # lève CellExecutionError à la première cellule en erreur
    texte = _sorties_texte(notebook)

    # Collecte : 35 villes géocodées, 245 prévisions (7 jours x 35 villes), 142 requêtes archivées.
    assert "Villes géocodées : 35 / 35" in texte
    assert "Prévisions journalières : 245" in texte
    assert "Overpass : 70 requêtes" in texte
    # ETL : compteurs de qualité de la collecte complète du 11/10/2026.
    for indicateur, valeur in (
        ("villes_lues", 35),
        ("previsions_journalieres", 245),
        ("hotels_lus", 4128),
        ("hotels_en_double", 131),
        ("hotels_hors_de_france", 0),
        ("hotels_hors_commune", 230),
        ("hotels_commune_non_evaluable", 3176),
        ("hotels_retenus", 3997),
    ):
        assert re.search(rf"^\s*{indicateur}\s+{valeur}$", texte, re.MULTILINE), indicateur
    assert "{'villes': 35, 'meteo_quotidienne': 245, 'hotels': 3997}" in texte
    assert "3997 lignes, 23 colonnes" in texte and "mêmes colonnes) : True" in texte
    # Résultats : le top 5 des destinations, lu dans la zone curated réécrite par le notebook.
    villes = lire_parquet(
        DataLake(bucket=None, racine_locale=tmp_path / "datalake"),
        "curated/villes/date=2026-10-11/villes.parquet",
    )
    assert villes["ville"].head(5).tolist() == [
        "Aigues Mortes",
        "Nimes",
        "Aix en Provence",
        "Avignon",
        "Collioure",
    ]
    assert "Quittent le top 5 : ['Bormes les Mimosas', 'Cassis', 'Toulouse']" in texte
    assert "Hôtels communs aux deux classements : 17 sur 20" in texte
    assert "3 hôtels en commun entre les deux collectes sur 20" in texte
    # Entrepôt : le contrôle d'intégrité des clés ne trouve aucune ligne orpheline.
    assert "(requête 6, attendu : aucune ligne) : 0 ligne(s)" in texte
    # Cartes : les deux fichiers HTML interactifs existent.
    assert (tmp_path / "sorties" / "top5_destinations.html").is_file()
    assert (tmp_path / "sorties" / "top20_hotels.html").is_file()
    # Sorties montrées au jury : ni message brut du pilote du navigateur de l'export PNG, ni
    # chemin du poste (lettre de lecteur suivie d'une barre oblique inverse).
    assert "Wait expired" not in texte
    assert re.search(r"\b[A-Za-z]:\\", texte) is None
