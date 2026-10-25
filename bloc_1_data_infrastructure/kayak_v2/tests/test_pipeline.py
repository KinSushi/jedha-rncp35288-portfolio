# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de l'orchestration : lecture des tables Parquet du lac et étape « cartes » seule.

Avant la correction, ``--etapes cartes`` utilisé seul reconstituait les destinations à partir du
premier hôtel de chaque ville : la carte du top 5 plaçait donc les destinations aux coordonnées
d'un hôtel, pas à celles de la ville. Les cartes lisent désormais la table des villes de la zone
curated. Aucun export d'image ni de réseau : la fonction d'enregistrement est remplacée.
"""

import io

import pandas as pd
import pytest

from kayak import cartes, pipeline
from kayak.stockage.data_lake import DataLake

DATE = "2026-10-10"


def _parquet(df: pd.DataFrame) -> bytes:
    """Octets d'un fichier Parquet (comme ceux que Spark dépose dans le lac)."""
    tampon = io.BytesIO()
    df.to_parquet(tampon)
    return tampon.getvalue()


def test_lire_parquet_assemble_les_parties_dans_l_ordre(tmp_path):
    """Une table Parquet est un dossier de parties : elles sont lues et assemblées dans l'ordre."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    dossier = "curated/t/date=2026-10-10/t.parquet"
    lake.ecrire(f"{dossier}/part-00001.parquet", _parquet(pd.DataFrame({"a": [3, 4]})))
    lake.ecrire(f"{dossier}/part-00000.parquet", _parquet(pd.DataFrame({"a": [1, 2]})))
    lake.ecrire(f"{dossier}/_SUCCESS", b"")  # marqueur de Spark : ignoré
    assert pipeline.lire_parquet(lake, dossier)["a"].tolist() == [1, 2, 3, 4]


def test_lire_parquet_applique_les_filtres_a_la_lecture(tmp_path):
    """Un filtre pyarrow est appliqué dès la lecture : seules les lignes utiles arrivent."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    dossier = "curated/t/date=2026-10-10/t.parquet"
    tableau = pd.DataFrame({"rang_meteo": [1, 3, 6, 9, 20], "nom": list("abcde")})
    lake.ecrire(f"{dossier}/part-00000.parquet", _parquet(tableau))
    lu = pipeline.lire_parquet(lake, dossier, filtres=[("rang_meteo", "<=", 5)])
    assert lu["nom"].tolist() == ["a", "b"]
    # Sans filtre, tout est lu.
    assert pipeline.lire_parquet(lake, dossier)["nom"].tolist() == list("abcde")


def test_lire_parquet_signale_une_table_absente(tmp_path):
    """Une table absente lève une erreur claire plutôt que de rendre un tableau vide."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    with pytest.raises(FileNotFoundError):
        pipeline.lire_parquet(lake, "curated/absente/t.parquet")


def test_etape_cartes_seule_utilise_les_coordonnees_des_villes(tmp_path, monkeypatch):
    """« --etapes cartes » seul : les destinations sont aux coordonnées des VILLES.

    Le lac factice contient une ville dont les hôtels sont à 5 km de son centre : si la carte
    prenait les coordonnées du premier hôtel (ancien comportement), la pastille serait là.
    """
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    villes = pd.DataFrame(
        {
            "id_ville": [1, 2],
            "ville": ["Nice", "Lille"],
            "latitude": [43.70, 50.63],
            "longitude": [7.27, 3.06],
            "temp_max_moy_c": [20.0, 10.0],
            "pluie_totale_mm": [0.0, 9.0],
            "ensoleillement_moy_h": [9.0, 2.0],
            "proba_pluie_moy_pct": [5.0, 80.0],
            "score_meteo": [1.0, -1.0],
            "rang_meteo": [1, 2],
        }
    )
    enrichi = pd.DataFrame(
        {
            "hotel_id": [10, 11],
            "id_ville": [1, 2],
            "nom": ["Hôtel Nice", "Hôtel Lille"],
            "latitude": [43.75, 50.68],  # à environ 5 km du centre de chaque ville
            "longitude": [7.27, 3.06],
            "note": [float("nan")] * 2,
            "nb_avis": [float("nan")] * 2,
            "etoiles": [4.0, 3.0],
            "distance_km": [5.5, 5.6],
            "commune": [None, None],
            "hors_commune": [None, None],
            "ville": ["Nice", "Lille"],
            "rang_meteo": [1, 2],
        }
    )
    lake.ecrire(f"curated/villes/date={DATE}/villes.parquet/part-00000.parquet", _parquet(villes))
    lake.ecrire(
        f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet/part-00000.parquet",
        _parquet(enrichi),
    )
    figures = {}

    def enregistrer_factice(fig, nom):
        figures[nom] = fig
        return {"html": f"{nom}.html"}

    monkeypatch.setattr(pipeline, "DataLake", lambda: lake)
    monkeypatch.setattr(cartes, "enregistrer", enregistrer_factice)
    monkeypatch.setenv("KAYAK_DATE_COLLECTE", DATE)
    resultats = pipeline.executer(["cartes"], DATE)
    assert set(resultats["cartes"]) == {"destinations", "hotels"}
    pastille_nice = figures["top5_destinations"].data[0]
    assert (pastille_nice.lat[0], pastille_nice.lon[0]) == (43.70, 7.27)  # le centre de Nice
    assert "top20_hotels" in figures
