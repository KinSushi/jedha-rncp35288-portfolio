# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests unitaires de l'extraction et de la réconciliation des destinations."""

import os
import warnings

import pandas as pd
import pytest

from kayak.collecte import destinations
from kayak.collecte.destinations import (
    ALIAS,
    extraire_destinations,
    normaliser,
    reconcilier,
)
from kayak.stockage.data_lake import DataLake


def test_extraire_destinations_minimal():
    """L'extraction doit nettoyer numérotation et parenthèses."""
    html = """
    <html><body>
        <h2>1. Paris</h2>
        <h2>2. Verdon Gorge</h2>
        <h2>3. Avignon (my favorite!)</h2>
    </body></html>
    """
    attendu = ["Paris", "Verdon Gorge", "Avignon"]
    assert extraire_destinations(html) == attendu


def test_normaliser():
    """La normalisation supprime accents, ponctuation et normalise les espaces."""
    assert normaliser("Avignon (my favorite!)") == "avignon my favorite"
    assert normaliser("St Malo") == "st malo"
    assert normaliser("Gorges du Verdon") == "gorges du verdon"


def test_reconcilier_alias_et_absence():
    """La réconciliation doit mapper les alias et signaler les absences."""
    # « Nimmes » (faute de la source) doit être rattachée à « Nimes » par l'alias ; « Lyon »,
    # absente de la liste extraite, doit être signalée.
    extraites = ["Paris", "Verdon Gorge", "Nimmes", "Avignon"]
    reference = ["Paris", "Gorges du Verdon", "Nimes", "Lyon"]
    df = reconcilier(extraites, reference=reference)
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns) == ["id_ville", "ville", "ville_source", "statut"]
    assert df["id_ville"].tolist() == [1, 2, 3, 4]
    assert df["statut"].tolist() == [
        "identique",
        "alias",
        "alias",
        "absente_de_la_source",
    ]
    assert df.loc[df["ville"] == "Gorges du Verdon", "ville_source"].iloc[0] == "Verdon Gorge"
    assert ALIAS["verdon gorge"] == "Gorges du Verdon"


class _PageFactice:
    """Réponse HTTP minimale : un HTML avec quelques titres h2."""

    text = "<html><body><h2>1. Paris</h2><h2>2. Verdon Gorge</h2></body></html>"

    def raise_for_status(self):
        return None


def test_collecte_s_arrete_si_robots_txt_interdit(tmp_path, monkeypatch):
    """Si robots.txt interdit la page, aucune requête vers elle ne part."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    interdit = {"texte": "User-agent: *\nDisallow: /\n", "meta": {}}
    monkeypatch.setattr(destinations, "archiver_robots", lambda *a, **k: interdit)

    def jamais(*_args, **_kwargs):
        raise AssertionError("la page ne doit pas être demandée")

    monkeypatch.setattr(destinations.requests, "get", jamais)
    with pytest.raises(PermissionError):
        destinations.collecter_destinations(lake)


def test_collecte_archive_la_page_et_le_csv_reconcilie(tmp_path, monkeypatch):
    """Quand robots.txt l'autorise, la page brute et le CSV réconcilié sont archivés.

    Le CSV est en fins de ligne « \\n » même quand la fin de ligne native du système est
    « \\r\\n » (celle de Windows, forcée ici)."""
    monkeypatch.setattr(os, "linesep", "\r\n")
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", "2026-10-10")
    monkeypatch.setattr(destinations.time, "sleep", lambda _s: None)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    permis = {"texte": "User-agent: *\nDisallow: /prive/\n", "meta": {}}
    monkeypatch.setattr(destinations, "archiver_robots", lambda *a, **k: permis)
    monkeypatch.setattr(destinations.requests, "get", lambda *a, **k: _PageFactice())
    df = destinations.collecter_destinations(lake)
    assert lake.existe("raw/destinations/date=2026-10-10/page.html")
    assert lake.existe("raw/destinations/date=2026-10-10/destinations.csv")
    assert df.loc[df["ville"] == "Paris", "statut"].item() == "identique"
    assert b"\r" not in lake.lire("raw/destinations/date=2026-10-10/destinations.csv")


def test_extraction_sans_avertissement_de_parseur():
    """Le parseur HTML est nommé : BeautifulSoup n'émet aucun avertissement « parseur deviné ».

    Sans parseur nommé, il prendrait le meilleur installé (lxml s'il est là) : l'extraction
    dépendrait du poste. L'avertissement est transformé en erreur ici.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert extraire_destinations("<h2>1. Paris</h2><h2>2. Uzès (Gard)</h2>") == [
            "Paris",
            "Uzès",
        ]
