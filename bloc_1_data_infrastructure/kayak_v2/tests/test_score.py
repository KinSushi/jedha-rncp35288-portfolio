# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests unitaires pour le calcul du score météo."""

import pandas as pd

from kayak.etl.score import calculer_score_meteo


def test_calculer_score_meteo_meilleure_ville_rang_1():
    """Vérifie que la ville la plus chaude, sèche et ensoleillée obtient le rang 1."""
    # Données synthétiques de 3 villes
    data = {
        "id_ville": [1, 2, 3],
        "ville": ["VilleA", "VilleB", "VilleC"],
        "temp_max_moy_c": [30.0, 25.0, 20.0],  # VilleA la plus chaude
        "pluie_totale_mm": [10.0, 20.0, 30.0],  # VilleA la plus sèche
        "ensoleillement_moy_h": [10.0, 8.0, 6.0],  # VilleA la plus ensoleillée
    }
    df = pd.DataFrame(data)

    resultat = calculer_score_meteo(df)

    # La ville A doit avoir le rang 1
    assert resultat.loc[resultat["ville"] == "VilleA", "rang_meteo"].iloc[0] == 1
    # Les rangs doivent être uniques et de 1 à 3
    assert sorted(resultat["rang_meteo"].tolist()) == [1, 2, 3]
    # Le score de la ville A doit être le plus élevé
    assert (
        resultat.loc[resultat["ville"] == "VilleA", "score_meteo"].iloc[0]
        == resultat["score_meteo"].max()
    )


def test_calculer_score_meteo_avec_proba_pluie():
    """Vérifie le fonctionnement avec proba_pluie_moy_pct au lieu d'ensoleillement."""
    data = {
        "id_ville": [1, 2, 3],
        "ville": ["VilleA", "VilleB", "VilleC"],
        "temp_max_moy_c": [30.0, 25.0, 20.0],
        "pluie_totale_mm": [10.0, 20.0, 30.0],
        "proba_pluie_moy_pct": [10.0, 30.0, 50.0],  # VilleA la moins pluvieuse
    }
    df = pd.DataFrame(data)

    resultat = calculer_score_meteo(df)

    assert resultat.loc[resultat["ville"] == "VilleA", "rang_meteo"].iloc[0] == 1
    assert sorted(resultat["rang_meteo"].tolist()) == [1, 2, 3]
