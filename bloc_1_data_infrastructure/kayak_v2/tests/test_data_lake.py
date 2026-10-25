# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests unitaires du data lake en mode local."""

from pathlib import Path

import pytest

from kayak.stockage.data_lake import DataLake


@pytest.fixture
def lake(tmp_path: Path) -> DataLake:
    """Data lake local isolé dans tmp_path."""
    return DataLake(bucket=None, prefixe="kayak", racine_locale=tmp_path)


def test_ecrire_lire_bytes(lake: DataLake, tmp_path: Path):
    """Écriture et lecture d'un objet binaire."""
    uri = lake.ecrire("raw/test/fichier.bin", b"contenu binaire")
    assert uri.startswith("file://")
    assert lake.lire("raw/test/fichier.bin") == b"contenu binaire"


def test_ecrire_lire_texte(lake: DataLake):
    """Écriture et lecture d'une chaîne encodée en UTF-8."""
    lake.ecrire("raw/test/fichier.txt", "texte avec accents : été")
    assert lake.lire("raw/test/fichier.txt").decode("utf-8") == "texte avec accents : été"


def test_existe(lake: DataLake):
    """La méthode existe reflète correctement la présence d'un objet."""
    assert not lake.existe("raw/test/inexistant.txt")
    lake.ecrire("raw/test/existant.txt", "ok")
    assert lake.existe("raw/test/existant.txt")


def test_lister(lake: DataLake):
    """La liste récursive retourne les chemins relatifs des fichiers."""
    lake.ecrire("a/f1.txt", "1")
    lake.ecrire("a/b/f2.txt", "2")
    lake.ecrire("c/f3.txt", "3")
    resultats = sorted(lake.lister("a"))
    assert resultats == ["a/b/f2.txt", "a/f1.txt"]


def test_chemin_local(lake: DataLake, tmp_path: Path):
    """En mode local, chemin_local renvoie le fichier lui-même."""
    lake.ecrire("spark/entree.csv", "a,b\n1,2\n")
    chemin = lake.chemin_local("spark/entree.csv")
    assert chemin == tmp_path / "spark" / "entree.csv"
    assert chemin.read_text() == "a,b\n1,2\n"
