# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de l'ETL Spark sur UN data lake synthétique riche, exécuté une seule fois.

Un jeu de neuf villes et d'une vingtaine d'hôtels contient chacune des anomalies que l'ETL doit
traiter : identifiant ou nom absent, doublon exact, hôtel sorti pour deux villes voisines, hôtel
hors du cadre de la France, hôtel déclaré allemand, note hors de l'échelle 0-10 ou aux bornes,
communes écrites autrement que la destination, destination qui n'est pas une commune. L'ETL est
lancé UNE fois (fixture de module) ; chaque test vérifie ensuite une règle précise sur son
résultat : compteurs de qualité, tables de l'entrepôt SQL, fichiers Parquet et CSV de la zone
« curated », zone « raw » laissée intacte. Aucun accès réseau ; SQLite dans un dossier temporaire.
"""

import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
from typing import Any, Literal

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from kayak.etl.spark_etl import executer_etl, extraire
from kayak.pipeline import lire_parquet
from kayak.stockage.data_lake import DataLake

DATE = "2026-02-02"

#: Colonnes de la table enrichie (Parquet et CSV de la zone « curated »), dans l'ordre.
COLONNES_ENRICHIES = [
    "id_ville", "hotel_id", "nom", "url", "latitude", "longitude", "adresse", "note", "nb_avis",
    "etoiles", "description", "date_collecte", "source", "distance_km", "commune", "hors_commune",
    "ville", "temp_max_moy_c", "pluie_totale_mm", "ensoleillement_moy_h", "proba_pluie_moy_pct",
    "score_meteo", "rang_meteo",
]

# PySpark 3.5.9 est antérieur à pandas 3 et à Python 3.12 : ses appels INTERNES émettent des
# DeprecationWarning que notre code ne provoque pas. Constatés : ``distutils.version.LooseVersion``
# (module remplacé par setuptools sous Python 3.12), ``Series.astype(copy=False)`` (mot-clé ``copy``
# déprécié par pandas 3) et ``is_datetime64tz_dtype`` (fonction dépréciée de pandas). PySpark ignore
# lui-même le premier à l'import, mais pytest restaure les filtres entre la collecte et chaque test,
# et ``-W error`` en fait des échecs. Le marqueur ci-dessous ignore les avertissements de
# dépréciation ATTRIBUÉS À UN MODULE DE PYSPARK, et seulement dans les tests qui lancent Spark : un
# avertissement attribué à ``kayak`` ou aux tests reste une erreur.
SPARK = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning:pyspark",
    "ignore::PendingDeprecationWarning:pyspark",
)

#: id, nom (comme dans l'énoncé), latitude, longitude
VILLES: list[tuple[int, str, float | None, float | None]] = [
    (1, "Aigues Mortes", 43.5662, 4.1915),
    (2, "Gorges du Verdon", 43.7706, 6.33),
    (3, "St Malo", 48.6493, -2.0257),
    (4, "Nimes", 43.8374, 4.3601),
    (5, "Lyon", 45.764, 4.8357),
    (6, "Strasbourg", 48.5846, 7.7507),
    (7, "Biarritz", 43.4832, -1.5586),
    (8, "Bayonne", 43.4933, -1.4748),
    (9, "Atlantide", 0.0, 0.0),  # hors du cadre de la France : coordonnées invalides
]

#: id de ville -> (tmax jour 1, tmax jour 2, pluie 1, pluie 2, soleil 1, soleil 2)
METEO: dict[int, tuple[float, float, float, float, float, float]] = {
    1: (26.0, 28.0, 0.0, 0.0, 10.0, 10.0),
    2: (24.0, 24.0, 0.0, 1.0, 9.0, 9.0),
    3: (18.0, 20.0, 3.0, 2.0, 5.0, 6.0),
    4: (22.0, 22.0, 0.0, 0.0, 8.0, 8.0),
    5: (15.0, 15.0, 4.0, 4.0, 3.0, 3.0),
    6: (15.0, 15.0, 4.0, 4.0, 3.0, 3.0),  # mêmes mesures que Lyon : ex æquo
    7: (20.0, 20.0, 1.0, 1.0, 7.0, 7.0),
    8: (19.0, 19.0, 1.0, 1.0, 7.0, 7.0),
    9: (10.0, 10.0, 9.0, 9.0, 1.0, 1.0),
}


def _hotel(ident: Any, ville: int, lat: float, lon: float, **champs: Any) -> dict[str, Any]:
    """Un hôtel brut au schéma commun ; ``champs`` ajoute ou remplace des valeurs."""
    base: dict[str, Any] = {
        "hotel_id": ident,
        "id_ville": ville,
        "nom": f"Hôtel {ident}",
        "url": f"https://exemple.test/h{ident}",
        "latitude": lat,
        "longitude": lon,
        "adresse": "rue",
        "note": None,
        "nb_avis": None,
        "etoiles": 3,
        "description": "texte",
        "date_collecte": DATE,
    }
    base.update(champs)
    return base


#: Hôtels du robot Booking (note renseignée, pas de commune ni de pays ni de « source »).
HOTELS_BOOKING = [
    _hotel(101, 1, 43.57, 4.19, note=9.0, nb_avis=120, etoiles=4, description="  texte  "),
    _hotel(102, 4, 43.84, 4.36, note=8.5),
    _hotel(103, 5, 45.76, 4.83, note=10.0),  # borne haute de l'échelle : conservée
    _hotel(104, 5, 45.77, 4.84, note=0.0),  # borne basse : conservée
    _hotel(105, 3, 48.65, -2.02, note=10.1),  # hors échelle : note vidée, hôtel gardé
    _hotel(106, 3, 48.64, -2.03, note=-0.5),  # hors échelle
    _hotel(None, 3, 48.65, -2.01, note=7.0),  # sans identifiant : écarté
    _hotel(108, 3, 48.65, -2.00, nom=None, note=7.0),  # sans nom : écarté
    _hotel(109, 6, 40.0, 7.75, note=6.0),  # hors du cadre de la France
]

#: Hôtels d'OpenStreetMap (commune et pays déclarés, source « openstreetmap »).
HOTELS_OSM = [
    _hotel(201, 1, 43.5762, 4.1915, commune="Aigues-Mortes", source="openstreetmap"),
    _hotel(202, 1, 43.5600, 4.0950, commune="La Grande-Motte", source="openstreetmap"),
    _hotel(203, 1, 43.5700, 4.1900, commune=None, source="openstreetmap"),
    _hotel(204, 2, 43.7706, 6.3300, commune="Aiguines", source="openstreetmap"),
    _hotel(205, 6, 48.5765, 7.8157, commune="Kehl", pays="DE", source="openstreetmap"),
    _hotel(206, 6, 48.5800, 7.7500, pays="fr", source="openstreetmap"),
    # Un même hôtel sorti pour deux villes voisines : rattaché à la plus proche (Bayonne).
    _hotel(207, 7, 43.4933, -1.4740, source="openstreetmap"),
    _hotel(207, 8, 43.4933, -1.4740, source="openstreetmap"),
    _hotel(209, 3, 48.6490, -2.0300, commune="Saint-Malo", source="openstreetmap"),
    _hotel(210, 5, 45.7600, 4.8400, commune="Lyon Cedex 7", source="openstreetmap"),
    _hotel(211, 4, 43.8374, 4.3601, commune="Nîmes", source="openstreetmap"),
    _hotel(213, 1, 43.5666, 4.1919, source="openstreetmap"),
    _hotel(213, 1, 43.5666, 4.1919, source="openstreetmap"),  # doublon exact
]

#: Hôtels placés sur les bornes du rectangle de la France métropolitaine (latitude 41,0 à 51,5 ;
#: longitude -5,5 à 10,0) : les bornes sont INCLUSES, un dix-millième de degré plus loin est exclu.
HOTELS_BORNES = [
    _hotel(301, 4, 41.0, 2.0, source="openstreetmap"),  # borne sud : gardé
    _hotel(302, 4, 40.9999, 2.0, source="openstreetmap"),  # juste au sud : écarté
    _hotel(303, 4, 51.5, 2.0, source="openstreetmap"),  # borne nord : gardé
    _hotel(304, 4, 51.5001, 2.0, source="openstreetmap"),  # juste au nord : écarté
    _hotel(305, 4, 45.0, -5.5, source="openstreetmap"),  # borne ouest : gardé
    _hotel(306, 4, 45.0, -5.5001, source="openstreetmap"),  # juste à l'ouest : écarté
    _hotel(307, 4, 45.0, 10.0, source="openstreetmap"),  # borne est : gardé
    _hotel(308, 4, 45.0, 10.0001, source="openstreetmap"),  # juste à l'est : écarté
    _hotel(309, 4, 52.0, 2.0, source="openstreetmap"),  # bien au nord : écarté
    _hotel(310, 4, 45.0, -6.0, source="openstreetmap"),  # bien à l'ouest : écarté
    _hotel(311, 4, 45.0, 10.5, source="openstreetmap"),  # bien à l'est : écarté
]

#: Compteurs attendus, comptés à la main sur les listes ci-dessus.
QUALITE_ATTENDUE = {
    "villes_lues": 9,
    "villes_sans_coordonnees_valides": 1,  # Atlantide
    "previsions_journalieres": 18,  # 9 villes x 2 jours
    "hotels_lus": 33,  # 9 de Booking + 13 d'OpenStreetMap + 11 aux bornes du rectangle
    "hotels_sans_identifiant_ou_nom": 2,
    "hotels_en_double": 2,  # l'hôtel 207 (deux villes) et l'hôtel 213 (deux fois)
    # l'hôtel 109 (latitude 40), l'hôtel 205 (pays DE) et 7 des 11 hôtels aux bornes du rectangle
    "hotels_hors_de_france": 9,
    "notes_hors_echelle": 2,  # 10,1 et -0,5
    "hotels_sans_note": 16,
    # Communes comparées : 202 (La Grande-Motte) seul est une autre commune ; 201, 209, 210, 211
    # sont la même commune écrite autrement ; les 11 autres hôtels retenus ne sont pas évaluables
    # (6 hôtels Booking sans commune, 203/206/207/213 sans commune, 204 dans un massif, et les 4
    # hôtels des bornes, sans commune).
    "hotels_hors_commune": 1,
    "hotels_commune_non_evaluable": 15,
    "hotels_retenus": 20,
}


def _ecrire_jsonl(lake: DataLake, chemin: str, lignes: list[dict[str, Any]]) -> None:
    lake.ecrire(chemin, "".join(json.dumps(ligne, ensure_ascii=False) + "\n" for ligne in lignes))


def _construire_lake(dossier: Path) -> DataLake:
    lake = DataLake(bucket=None, racine_locale=dossier)
    lignes_csv = "".join(
        f"{i},{nom},{'' if lat is None else lat},{'' if lon is None else lon},{nom}\n"
        for i, nom, lat, lon in VILLES
    )
    lake.ecrire(
        f"raw/geocodage/date={DATE}/coordonnees.csv",
        "id_ville,ville,latitude,longitude,nom_complet\n" + lignes_csv,
    )
    previsions = []
    for ville, (t1, t2, p1, p2, s1, s2) in METEO.items():
        jours = []
        for date, tmax, pluie, soleil in (("2026-02-02", t1, p1, s1), ("2026-02-03", t2, p2, s2)):
            jours.append(
                {
                    "date": date,
                    "temp_max_c": tmax,
                    "temp_min_c": tmax - 8,
                    "pluie_mm": pluie,
                    "proba_pluie_pct": 5 if pluie == 0 else 80,
                    "ensoleillement_h": soleil,
                    "vent_max_kmh": 12,
                    "source": "test",
                }
            )
        if ville == 3:  # une prévision en double (même ville, même jour) : une seule est gardée
            jours.append(dict(jours[0]))
        previsions.append({"id_ville": ville, "previsions": jours})
    _ecrire_jsonl(lake, f"raw/meteo/date={DATE}/previsions.jsonl", previsions)
    _ecrire_jsonl(lake, f"raw/booking/date={DATE}/hotels.jsonl", HOTELS_BOOKING)
    _ecrire_jsonl(lake, f"raw/osm/date={DATE}/hotels.jsonl", [*HOTELS_OSM, *HOTELS_BORNES])
    return lake


def _empreintes(dossier: Path) -> dict[str, str]:
    """Empreinte SHA-256 de chaque fichier d'un dossier (clé : chemin relatif)."""
    return {
        f.relative_to(dossier).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(dossier.rglob("*"))
        if f.is_file()
    }


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance de haversine, écrite indépendamment de l'ETL pour servir de référence."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


class _Collecteur(logging.Handler):
    """Garde (nom, niveau, message) de chaque enregistrement de journal reçu."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[tuple[str, int, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append((record.name, record.levelno, record.getMessage()))


class Etl:
    """Le résultat d'un unique passage de l'ETL, et de quoi le relire."""

    def __init__(self, dossier: Path) -> None:
        self.lake = _construire_lake(dossier / "lake")
        self.avant = _empreintes(dossier / "lake" / "raw")
        self.dossier = dossier
        self.url = f"sqlite:///{(dossier / 'entrepot.db').as_posix()}"
        journal = logging.getLogger("kayak.etl.spark_etl")
        collecteur, niveau = _Collecteur(), journal.level
        journal.addHandler(collecteur)
        journal.setLevel(logging.INFO)
        try:
            self.resultat = executer_etl(self.lake, DATE, self.url)
        finally:
            journal.removeHandler(collecteur)
            journal.setLevel(niveau)
        self.messages = collecteur.messages
        self.apres = _empreintes(dossier / "lake" / "raw")

    def sql(self, requete: str) -> list[tuple[Any, ...]]:
        with create_engine(self.url).connect() as connexion:
            return [tuple(ligne) for ligne in connexion.execute(text(requete)).fetchall()]


def _portee(fixture_name: str, config: pytest.Config) -> Literal["module", "function"]:
    """Portée « module » d'ordinaire ; « function » pendant le relevé de l'outil de mutation.

    mutmut commence par un relevé (``MUTANT_UNDER_TEST`` vaut « stats ») : il relie chaque test
    aux fonctions du projet qu'il appelle, en les observant pendant le test. Une fixture de module
    ne s'exécute que pendant le PREMIER test du module : seul ce test serait relié au code qu'elle
    appelle, et les autres ne seraient jamais lancés contre ses mutants. Pendant ce relevé, la
    fixture est donc refaite pour chaque test. Ensuite, pour chaque mutant, elle redevient de
    module : faite une fois, avec le mutant actif, pour tous les tests choisis. Le contenu des
    tests ne change pas.
    """
    return "function" if os.environ.get("MUTANT_UNDER_TEST") == "stats" else "module"


@pytest.fixture(scope=_portee)
def etl(tmp_path_factory: pytest.TempPathFactory) -> Etl:
    """L'ETL lancé une seule fois sur le jeu riche, pour tous les tests du module."""
    return Etl(tmp_path_factory.mktemp("etl_riche"))


@SPARK
def test_compteurs_de_qualite_exacts(etl: Etl) -> None:
    """Chaque anomalie est comptée AVANT d'être corrigée, avec les valeurs attendues."""
    assert etl.resultat["qualite"] == QUALITE_ATTENDUE


@SPARK
def test_la_zone_raw_reste_inchangee_apres_l_etl(etl: Etl) -> None:
    """L'ETL ne modifie jamais les fichiers bruts : même liste, mêmes empreintes avant et après."""
    assert etl.apres == etl.avant


@SPARK
def test_hotel_sans_identifiant_ou_sans_nom_est_refuse(etl: Etl) -> None:
    """Un hôtel sans identifiant ou sans nom n'entre pas dans l'entrepôt (deux écartés)."""
    ids = {ligne[0] for ligne in etl.sql("SELECT hotel_id FROM hotels")}
    assert 108 not in ids  # l'hôtel sans nom
    assert {101, 102, 103, 104, 105, 106} <= ids  # les hôtels Booking valides sont là
    assert etl.sql("SELECT COUNT(*) FROM hotels WHERE nom IS NULL") == [(0,)]


@SPARK
def test_hotels_hors_de_france_sont_refuses(etl: Etl) -> None:
    """Le rectangle englobant écarte l'hôtel 109 ; le pays déclaré « DE » écarte l'hôtel 205."""
    ids = {ligne[0] for ligne in etl.sql("SELECT hotel_id FROM hotels")}
    assert 109 not in ids
    assert 205 not in ids
    assert 206 in ids  # pays « fr » en minuscules : accepté


@SPARK
def test_bornes_du_rectangle_de_la_france_sont_incluses_et_au_dela_refusees(etl: Etl) -> None:
    """Latitude 41,0 à 51,5, longitude -5,5 à 10,0 : bornes incluses, un cheveu plus loin refusé."""
    ids = {ligne[0] for ligne in etl.sql("SELECT hotel_id FROM hotels WHERE hotel_id >= 301")}
    assert ids == {301, 303, 305, 307}


@SPARK
def test_note_hors_echelle_est_videe_et_l_hotel_est_garde(etl: Etl) -> None:
    """Une note hors de 0-10 devient NULL mais l'hôtel reste ; les bornes 0 et 10 sont valides."""
    notes = dict(etl.sql("SELECT hotel_id, note FROM hotels WHERE hotel_id BETWEEN 101 AND 106"))
    assert notes == {101: 9.0, 102: 8.5, 103: 10.0, 104: 0.0, 105: None, 106: None}


@SPARK
def test_hotel_present_pour_deux_villes_est_rattache_a_la_plus_proche(etl: Etl) -> None:
    """L'hôtel 207 (Biarritz et Bayonne) n'est gardé qu'une fois, pour Bayonne, plus proche."""
    assert etl.sql("SELECT id_ville FROM hotels WHERE hotel_id = 207") == [(8,)]


@SPARK
def test_doublon_exact_est_garde_une_seule_fois(etl: Etl) -> None:
    """Deux lignes identiques (hôtel 213) donnent une seule ligne dans l'entrepôt."""
    assert etl.sql("SELECT hotel_id FROM hotels WHERE hotel_id = 213") == [(213,)]


@SPARK
def test_distance_au_centre_en_haversine(etl: Etl) -> None:
    """La distance au centre de la destination est celle de la formule de haversine, à 1 m près."""
    centres = {i: (lat, lon) for i, _, lat, lon in VILLES}
    lignes = etl.sql("SELECT hotel_id, id_ville, latitude, longitude, distance_km FROM hotels")
    assert lignes  # le jeu n'est pas vide
    for ident, ville, lat, lon, distance in lignes:
        attendue = haversine_km(*centres[ville], lat, lon)
        assert distance == pytest.approx(attendue, abs=0.0006), ident
        assert distance == round(distance, 3), ident  # arrondie au mètre, pas plus fin


@SPARK
def test_marquage_hors_commune_selon_la_graphie_et_le_type_de_destination(etl: Etl) -> None:
    """Même commune malgré tirets, accents, abréviation et « cedex » ; vide si non évaluable."""
    marques = dict(etl.sql("SELECT hotel_id, hors_commune FROM hotels WHERE hotel_id >= 201"))
    assert marques[201] == 0  # « Aigues-Mortes » = « Aigues Mortes »
    assert marques[202] == 1  # La Grande-Motte est une autre commune
    assert marques[203] is None  # commune inconnue : non évaluable
    assert marques[204] is None  # « Gorges du Verdon » n'est pas une commune
    assert marques[209] == 0  # « Saint-Malo » = « St Malo »
    assert marques[210] == 0  # « Lyon Cedex 7 » = « Lyon »
    assert marques[211] == 0  # « Nîmes » = « Nimes »


@SPARK
def test_classement_meteo_avec_ex_aequo(etl: Etl) -> None:
    """Rang 1 pour la ville la plus chaude, sèche et ensoleillée ; les ex æquo partagent le rang."""
    rangs = dict(etl.sql("SELECT ville, rang_meteo FROM villes"))
    assert rangs["Aigues Mortes"] == 1
    assert rangs["Atlantide"] == max(rangs.values())
    assert rangs["Lyon"] == rangs["Strasbourg"]  # mêmes mesures, même rang
    assert rangs["Lyon"] > rangs["Biarritz"]  # moins bien classées que Biarritz


@SPARK
def test_agregats_meteo_arrondis(etl: Etl) -> None:
    """Les agrégats par ville sont des moyennes et des sommes arrondies (2 décimales)."""
    ligne = etl.sql(
        "SELECT temp_max_moy_c, pluie_totale_mm, ensoleillement_moy_h, proba_pluie_moy_pct "
        "FROM villes WHERE ville = 'St Malo'"
    )
    assert ligne == [(19.0, 5.0, 5.5, 80.0)]


@SPARK
def test_villes_et_meteo_chargees_en_entier(etl: Etl) -> None:
    """Les neuf villes et leurs dix-huit prévisions sont dans l'entrepôt, chacune sur deux jours."""
    assert [v[0] for v in etl.sql("SELECT id_ville FROM villes ORDER BY id_ville")] == list(
        range(1, 10)
    )
    jours = etl.sql("SELECT DISTINCT date FROM meteo_quotidienne ORDER BY date")
    assert [str(j[0]) for j in jours] == ["2026-02-02", "2026-02-03"]
    assert etl.resultat["charges"] == {"villes": 9, "meteo_quotidienne": 18, "hotels": 20}


@SPARK
def test_textes_nettoyes_et_source_par_defaut(etl: Etl) -> None:
    """La description est débarrassée de ses espaces ; une ligne sans source est « booking »."""
    assert etl.sql("SELECT description, source FROM hotels WHERE hotel_id = 101") == [
        ("texte", "booking")
    ]
    assert etl.sql("SELECT source FROM hotels WHERE hotel_id = 201") == [("openstreetmap",)]


@SPARK
def test_parquet_csv_et_entrepot_contiennent_les_memes_hotels(etl: Etl) -> None:
    """Le Parquet et le CSV enrichis écrits par Spark portent les mêmes hôtels que l'entrepôt."""
    parquet = lire_parquet(etl.lake, f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet")
    contenu_csv = etl.lake.lire(f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.csv")
    csv = pd.read_csv(io.BytesIO(contenu_csv))
    en_base = sorted(ligne[0] for ligne in etl.sql("SELECT hotel_id FROM hotels"))
    assert sorted(parquet["hotel_id"].tolist()) == en_base
    assert sorted(csv["hotel_id"].tolist()) == en_base
    # Table enrichie : triée par rang météo puis note décroissante (les notes absentes en dernier).
    assert parquet["rang_meteo"].is_monotonic_increasing
    # Colonnes exactes, dans l'ordre, et les mêmes dans les deux formats : aucune colonne de
    # travail (rang de proximité, centre de la destination, pays) ne reste dans la livraison.
    assert list(parquet.columns) == list(csv.columns) == COLONNES_ENRICHIES


@SPARK
def test_table_des_villes_ecrite_dans_l_ordre_du_classement(etl: Etl) -> None:
    """La table curated des villes est écrite triée par rang météo, avec les vraies coordonnées."""
    villes = lire_parquet(etl.lake, f"curated/villes/date={DATE}/villes.parquet")
    assert villes["rang_meteo"].is_monotonic_increasing
    assert villes.iloc[0]["ville"] == "Aigues Mortes"
    assert villes.iloc[0]["latitude"] == 43.5662


@SPARK
def test_aucun_dossier_de_travail_ne_reste_dans_le_lake(etl: Etl) -> None:
    """L'ETL retire ses dossiers temporaires : ni fichier partiel ni somme de contrôle ne reste."""
    restes = [c for c in etl.lake.lister("") if c.endswith((".tmp", ".crc", ".partiel"))]
    assert restes == []
    assert not (etl.dossier / "lake" / ".tmp").exists()


def test_extraire_sans_fichier_d_hotels_est_refuse(tmp_path: Path) -> None:
    """Sans hotels.jsonl (ni Booking ni OpenStreetMap), l'extraction refuse de continuer."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    lake.ecrire(f"raw/geocodage/date={DATE}/coordonnees.csv", "id_ville,ville\n")
    with pytest.raises(FileNotFoundError, match=f"aucun fichier d'hôtels pour la date {DATE}"):
        extraire(None, lake, DATE)  # l'erreur est levée avant tout accès à Spark


def test_extraire_ignore_un_fichier_d_hotels_vide(tmp_path: Path) -> None:
    """Un hotels.jsonl vide (Booking n'a rien servi) n'est pas lu : ce n'est pas une source."""
    lake = DataLake(bucket=None, racine_locale=tmp_path)
    lake.ecrire(f"raw/booking/date={DATE}/hotels.jsonl", "")
    with pytest.raises(FileNotFoundError):
        extraire(None, lake, DATE)


@SPARK
def test_resultat_de_l_etl_expose_tables_qualite_charges_et_chemins_ecrits(etl: Etl) -> None:
    """Le résultat a quatre clés ; les chemins publiés sont ceux de la zone curated."""
    resultat = etl.resultat
    assert set(resultat) == {"tables", "qualite", "charges", "curated"}
    assert set(resultat["tables"]) == {"villes"}
    # Windows écrit « part-00000.parquet » ; Spark natif (Linux) un nom à suffixe et un _SUCCESS.
    for table, dossier in (
        ("villes", f"curated/villes/date={DATE}/villes.parquet/"),
        ("kayak_enrichi", f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet/"),
    ):
        parties = [c for c in resultat["curated"][table] if c.endswith(".parquet")]
        assert parties and all(c.startswith(dossier + "part-") for c in parties)
    assert resultat["curated"]["kayak_enrichi_csv"] == (
        f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.csv"
    )
    assert resultat["tables"]["villes"].columns.tolist() == [
        "id_ville", "ville", "latitude", "longitude", "temp_max_moy_c", "pluie_totale_mm",
        "ensoleillement_moy_h", "proba_pluie_moy_pct", "score_meteo", "rang_meteo",
    ]


@SPARK
def test_un_seul_message_d_information_resume_l_etl(etl: Etl) -> None:
    """Le journal de l'ETL tient en une ligne INFO : « ETL terminé », compteurs, lignes chargées."""
    messages = [m for m in etl.messages if m[0] == "kayak.etl.spark_etl"]
    assert messages == [
        (
            "kayak.etl.spark_etl",
            logging.INFO,
            f"ETL terminé : {etl.resultat['qualite']} ; "
            f"lignes chargées : {etl.resultat['charges']}",
        )
    ]


@SPARK
def test_lignes_completes_d_un_hotel_booking_et_d_un_hotel_openstreetmap(etl: Etl) -> None:
    """Chaque colonne de la table des hôtels reprend la source, typée et nettoyée (deux sources)."""
    colonnes = (
        "hotel_id, id_ville, nom, url, latitude, longitude, adresse, note, nb_avis, etoiles, "
        "description, date_collecte, source, commune, hors_commune"
    )
    booking = etl.sql(f"SELECT {colonnes} FROM hotels WHERE hotel_id = 101")
    assert booking == [
        (101, 1, "Hôtel 101", "https://exemple.test/h101", 43.57, 4.19, "rue", 9.0, 120, 4,
         "texte", "2026-02-02", "booking", None, None)
    ]
    osm = etl.sql(f"SELECT {colonnes} FROM hotels WHERE hotel_id = 201")
    assert osm == [
        (201, 1, "Hôtel 201", "https://exemple.test/h201", 43.5762, 4.1915, "rue", None, None, 3,
         "texte", "2026-02-02", "openstreetmap", "Aigues-Mortes", 0)
    ]


@SPARK
def test_lignes_completes_des_villes_avec_leur_score_et_leur_rang(etl: Etl) -> None:
    """Moyennes, sommes, score z moyen et rang de chaque ville, recalculés ici indépendamment."""
    lignes = []
    for ident, nom, lat, lon in VILLES:
        t1, t2, p1, p2, s1, s2 = METEO[ident]
        lignes.append(
            {"id_ville": ident, "ville": nom, "latitude": lat, "longitude": lon,
             "temp": round((t1 + t2) / 2, 2), "pluie": round(p1 + p2, 2),
             "soleil": round((s1 + s2) / 2, 2),
             "proba": round(((5 if p1 == 0 else 80) + (5 if p2 == 0 else 80)) / 2, 1)}
        )
    tableau = pd.DataFrame(lignes)

    def z(serie):
        return (serie - serie.mean()) / serie.std()

    tableau["score"] = (z(tableau["temp"]) + z(tableau["soleil"]) - z(tableau["pluie"])) / 3
    tableau["rang"] = tableau["score"].rank(ascending=False, method="min").astype(int)
    en_base = etl.sql(
        "SELECT id_ville, ville, latitude, longitude, temp_max_moy_c, pluie_totale_mm, "
        "ensoleillement_moy_h, proba_pluie_moy_pct, score_meteo, rang_meteo FROM villes "
        "ORDER BY id_ville"
    )
    assert [ligne[:5] for ligne in en_base] == [
        (r.id_ville, r.ville, r.latitude, r.longitude, r.temp) for r in tableau.itertuples()
    ]
    assert [ligne[5:8] for ligne in en_base] == [
        (r.pluie, r.soleil, r.proba) for r in tableau.itertuples()
    ]
    assert [ligne[8] for ligne in en_base] == pytest.approx(tableau["score"].tolist())
    assert [ligne[9] for ligne in en_base] == tableau["rang"].tolist()


@SPARK
def test_ligne_complete_d_une_prevision_quotidienne(etl: Etl) -> None:
    """Une prévision reprend ses sept mesures et sa source ; la ville 3 n'a pas de doublon."""
    ligne = etl.sql(
        "SELECT id_ville, date, temp_max_c, temp_min_c, pluie_mm, proba_pluie_pct, "
        "ensoleillement_h, vent_max_kmh, source FROM meteo_quotidienne "
        "WHERE id_ville = 3 ORDER BY date"
    )
    assert ligne == [
        (3, "2026-02-02", 18.0, 10.0, 3.0, 80.0, 5.0, 12.0, "test"),
        (3, "2026-02-03", 20.0, 12.0, 2.0, 80.0, 6.0, 12.0, "test"),
    ]
