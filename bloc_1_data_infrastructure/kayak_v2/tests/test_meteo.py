# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests unitaires de la collecte météo : conversion, coordonnées manquantes, échec par ville.

Aucun appel réseau : le fournisseur de prévisions est remplacé par une fonction factice et les
pauses sont désactivées.
"""

import io
import json

import pandas as pd
import pytest
import requests

from kayak import config
from kayak.collecte import meteo
from kayak.collecte.meteo import collecter_meteo, convertir_openweather
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"


def test_convertir_openweather():
    """La conversion doit produire le schéma commun attendu."""
    reponse = {
        "daily": [
            {
                "dt": 1704067200,  # 2024-01-01 00:00:00 UTC
                "temp": {"max": 12.5, "min": 3.2},
                "rain": 2.5,
                "pop": 0.3,
                "wind_speed": 5.0,
            },
            {
                "dt": 1704153600,  # 2024-01-02
                "temp": {"max": 14.0, "min": 4.0},
                "pop": 0.0,
                "wind_speed": 3.0,
            },
        ]
    }
    previsions = convertir_openweather(reponse)
    assert [p["date"] for p in previsions] == ["2024-01-01", "2024-01-02"]

    premier = previsions[0]
    assert premier["date"] == "2024-01-01"
    assert premier["temp_max_c"] == 12.5
    assert premier["temp_min_c"] == 3.2
    assert premier["pluie_mm"] == 2.5
    assert premier["proba_pluie_pct"] == 30.0
    assert premier["ensoleillement_h"] is None
    assert premier["vent_max_kmh"] == 18.0
    assert premier["source"] == "openweather"

    deuxieme = previsions[1]
    assert deuxieme["pluie_mm"] == 0
    assert deuxieme["proba_pluie_pct"] == 0.0


def _prevision(temp: float) -> list[dict]:
    """Une prévision d'un jour, au schéma commun."""
    return [
        {
            "date": "2026-10-11",
            "temp_max_c": temp,
            "temp_min_c": temp - 8,
            "pluie_mm": 0.0,
            "proba_pluie_pct": 5.0,
            "ensoleillement_h": 9.0,
            "vent_max_kmh": 12.0,
            "source": "open-meteo",
        }
    ]


class _Fournisseur:
    """Faux Open-Meteo : mémorise les coordonnées demandées, échoue pour certaines villes."""

    def __init__(self, en_echec: tuple[float, ...] = ()):
        self.demandes: list[tuple[float, float]] = []
        self.en_echec = en_echec

    def __call__(self, lat, lon, session):
        self.demandes.append((lat, lon))
        if lat in self.en_echec:
            raise requests.ConnectionError("coupure réseau simulée")
        return _prevision(lat)


@pytest.fixture(autouse=True)
def _environnement(monkeypatch):
    """Date fixée, pas de clé OpenWeather (donc Open-Meteo), et aucune pause."""
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)
    monkeypatch.setattr(config, "OPENWEATHER_API_KEY", None)
    monkeypatch.setattr(meteo.time, "sleep", lambda _s: None)


def test_coordonnees_manquantes_en_nan_ne_declenchent_aucun_appel(tmp_path, monkeypatch):
    """Les coordonnées absentes du CSV sont lues en NaN, jamais en None.

    L'ancien test « lat is None » était donc mort : une ville non géocodée interrogeait l'API avec
    des NaN. Le test passe par un vrai CSV relu par pandas, comme le fait le pipeline.
    """
    csv = "id_ville,ville,latitude,longitude\n1,Nice,43.7,7.27\n2,Inconnue,,\n"
    coordonnees = pd.read_csv(io.StringIO(csv))
    assert coordonnees["latitude"].isna().tolist() == [False, True]  # NaN, pas None
    fournisseur = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    resultat = collecter_meteo(coordonnees, lake, pause_s=0)
    assert fournisseur.demandes == [(43.7, 7.27)]  # une seule ville interrogée
    assert resultat["id_ville"].tolist() == [1]
    # La ville sans coordonnées est archivée avec une source « aucune » et aucune prévision.
    inconnue = json.loads(lake.lire(f"raw/meteo/date={DATE}/ville_02.json"))
    assert inconnue["source"] == "aucune" and inconnue["previsions"] == []


def test_un_echec_n_emporte_pas_les_autres_villes_puis_reprise(tmp_path, monkeypatch):
    """Une ville en échec n'efface pas les autres ; une relance ne redemande que celle-là."""
    coordonnees = pd.DataFrame(
        {
            "id_ville": [1, 2, 3],
            "ville": ["A", "B", "C"],
            "latitude": [10.0, 20.0, 30.0],
            "longitude": [1.0, 2.0, 3.0],
        }
    )
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    fournisseur = _Fournisseur(en_echec=(20.0,))
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur)
    with pytest.raises(RuntimeError) as erreur:
        collecter_meteo(coordonnees, lake, pause_s=0)
    assert "B" in str(erreur.value)  # le message nomme la ville manquante
    # Les deux autres villes sont archivées individuellement ; B ne l'est pas.
    assert lake.existe(f"raw/meteo/date={DATE}/ville_01.json")
    assert not lake.existe(f"raw/meteo/date={DATE}/ville_02.json")
    assert lake.existe(f"raw/meteo/date={DATE}/ville_03.json")
    # Le fichier consolidé garde ce qui a été obtenu.
    lignes = lake.lire(f"raw/meteo/date={DATE}/previsions.jsonl").decode("utf-8").splitlines()
    assert [json.loads(ligne)["id_ville"] for ligne in lignes] == [1, 3]

    # Relance : le fournisseur fonctionne, seule la ville B est redemandée.
    fournisseur_repare = _Fournisseur()
    monkeypatch.setattr(meteo, "meteo_open_meteo", fournisseur_repare)
    resultat = collecter_meteo(coordonnees, lake, pause_s=0)
    assert fournisseur_repare.demandes == [(20.0, 2.0)]
    assert resultat["id_ville"].tolist() == [1, 2, 3]
