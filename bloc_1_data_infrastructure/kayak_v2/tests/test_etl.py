# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Tests de l'ETL Spark sur de petits data lakes synthétiques.

Les jeux contiennent volontairement les anomalies que l'ETL doit traiter : un hôtel en double, un
hôtel aux coordonnées hors de France, une note hors de l'échelle 0-10, un même hôtel sorti pour
deux villes voisines, des communes qui diffèrent de la destination. Les tests vérifient que
chaque anomalie est comptée et corrigée, que la table curated est écrite en Parquet par Spark,
que l'entrepôt SQL contient les bonnes lignes, clés et vue, et que son chargement est atomique.
Aucun accès réseau ; SQLite dans un dossier temporaire.
"""

import io
import json

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from kayak.etl import spark_etl
from kayak.etl.spark_etl import (
    _normaliser_lieu,
    charger_entrepot,
    executer_etl,
    par_lots,
)
from kayak.pipeline import lire_parquet
from kayak.stockage.data_lake import DataLake

DATE = "2026-01-01"

# PySpark 3.5.9 est antérieur à pandas 3 et à Python 3.12 : ses appels INTERNES émettent des
# DeprecationWarning que notre code ne provoque pas. Constatés : ``distutils.version.LooseVersion``
# (module remplacé par setuptools sous Python 3.12), ``Series.astype(copy=False)`` (mot-clé ``copy``
# déprécié par pandas 3) et ``is_datetime64tz_dtype`` (fonction dépréciée de pandas). PySpark ignore
# lui-même le premier à l'import, mais pytest restaure les filtres entre la collecte et chaque test,
# et ``-W error`` en fait des échecs. Le marqueur ci-dessous ignore les avertissements de
# dépréciation ATTRIBUÉS À UN MODULE DE PYSPARK, et seulement dans les tests qui lancent Spark : un
# avertissement attribué à ``kayak`` ou aux tests reste une erreur.
# Écarté : épingler pandas 2.x. Il ne supprimerait que ``astype(copy=False)`` : pandas 2.2.3 émet
# déjà l'avertissement de ``is_datetime64tz_dtype`` (pandas/core/dtypes/common.py, version 2.2.3),
# et ``LooseVersion`` vient de distutils, pas de pandas. Le filtre resterait donc nécessaire.
# PySpark 4 n'appelle plus ni distutils ni ``is_datetime64tz_dtype`` (lu dans le code de la version
# 4.2.0) ; passer à PySpark 4 est un changement de version majeur, non essayé.
SPARK = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning:pyspark",
    "ignore::PendingDeprecationWarning:pyspark",
)


def _prevision(date: str, tmax: float, pluie: float, soleil: float) -> dict:
    """Une prévision journalière au schéma commun."""
    return {
        "date": date,
        "temp_max_c": tmax,
        "temp_min_c": tmax - 8,
        "pluie_mm": pluie,
        "proba_pluie_pct": 5 if pluie == 0 else 80,
        "ensoleillement_h": soleil,
        "vent_max_kmh": 12,
        "source": "t",
    }


def _hotel(ident, ville, lat, lon, note=None, **autres) -> dict:
    """Un hôtel brut ; ``autres`` ajoute ou remplace des champs (commune, source...)."""
    return {
        "hotel_id": ident,
        "id_ville": ville,
        "nom": f"Hôtel {ident}",
        "url": f"https://www.booking.com/hotel/fr/h{ident}.fr.html",
        "latitude": lat,
        "longitude": lon,
        "adresse": "rue",
        "note": note,
        "nb_avis": 10,
        "etoiles": 3,
        "description": "texte",
        "date_collecte": DATE,
        **autres,
    }


def _ecrire_lake(dossier, villes: list[tuple], hotels: list[dict], meteo: dict) -> DataLake:
    """Construit un data lake : villes (id, nom, lat, lon), hôtels, météo {id_ville: tmax}."""
    lake = DataLake(bucket=None, racine_locale=dossier)
    lignes = "".join(f"{i},{nom},{lat},{lon},{nom}\n" for i, nom, lat, lon in villes)
    lake.ecrire(
        f"raw/geocodage/date={DATE}/coordonnees.csv",
        "id_ville,ville,latitude,longitude,nom_complet\n" + lignes,
    )
    previsions = [
        {
            "id_ville": ville,
            "previsions": [
                _prevision("2026-01-01", tmax, 0 if tmax > 10 else 8, 9 if tmax > 10 else 1),
                _prevision("2026-01-02", tmax + 1, 0 if tmax > 10 else 5, 8 if tmax > 10 else 2),
            ],
        }
        for ville, tmax in meteo.items()
    ]
    lake.ecrire(
        f"raw/meteo/date={DATE}/previsions.jsonl",
        "".join(json.dumps(p) + "\n" for p in previsions),
    )
    lake.ecrire(
        f"raw/booking/date={DATE}/hotels.jsonl", "".join(json.dumps(h) + "\n" for h in hotels)
    )
    return lake


def _preparer_lake(dossier) -> DataLake:
    """Lake de base : Nice et Lille, cinq lignes d'hôtels dont un doublon et deux anomalies."""
    hotels = [
        _hotel(10, 1, 43.70, 7.26, 9.1),
        _hotel(10, 1, 43.70, 7.26, 9.1),  # doublon
        _hotel(11, 1, 40.0, 7.26, 8.0),  # hors de France (latitude 40)
        _hotel(12, 2, 50.63, 3.07, 12.0),  # note hors échelle
        _hotel(13, 2, 50.62, 3.05, 7.5),
    ]
    return _ecrire_lake(
        dossier,
        [(1, "Nice", 43.70, 7.27), (2, "Lille", 50.63, 3.06)],
        hotels,
        {1: 18, 2: 6},
    )


@SPARK
def test_etl_bout_en_bout(tmp_path):
    lake = _preparer_lake(tmp_path / "lake")
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    qualite = resultat["qualite"]
    assert qualite["hotels_lus"] == 5
    assert qualite["hotels_en_double"] == 1
    assert qualite["hotels_hors_de_france"] == 1
    assert qualite["notes_hors_echelle"] == 1
    assert qualite["hotels_retenus"] == 3
    assert resultat["charges"] == {"villes": 2, "meteo_quotidienne": 4, "hotels": 3}
    # Nice (chaude, sèche, ensoleillée) doit être la destination n° 1.
    villes = resultat["tables"]["villes"]
    assert villes.loc[villes["rang_meteo"] == 1, "ville"].item() == "Nice"
    # La vue métier existe et joint correctement hôtels et villes.
    with create_engine(url).connect() as connexion:
        lignes = connexion.execute(
            text("SELECT ville, hotel FROM vue_hotels_destinations ORDER BY hotel")
        ).fetchall()
    assert [tuple(ligne) for ligne in lignes] == [
        ("Nice", "Hôtel 10"),
        ("Lille", "Hôtel 12"),
        ("Lille", "Hôtel 13"),
    ]


@SPARK
def test_curated_ecrit_en_parquet_par_spark(tmp_path):
    """Les tables curated sont des dossiers Parquet écrits par Spark, relisibles et complets."""
    lake = _preparer_lake(tmp_path / "lake")
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    # Le chemin publié est un dossier de fichiers .parquet (une table = un dossier).
    parties = lake.lister(f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet")
    assert parties and all(p.endswith(".parquet") or p.endswith("_SUCCESS") for p in parties)
    assert set(resultat["curated"]["kayak_enrichi"]) == set(parties)
    # Un seul CSV dans la zone curated : le CSV enrichi de l'énoncé (écrit par Spark, voir
    # test_csv_enrichi_ecrit_par_spark_identique_au_parquet) ; la table des villes n'en a pas.
    assert [c for c in lake.lister("curated") if c.endswith(".csv")] == [
        f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.csv"
    ]
    # Relecture : mêmes lignes que l'entrepôt, colonnes de distance et de commune présentes.
    enrichi = lire_parquet(lake, f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet")
    # Les hôtels retenus (le doublon de 10 et l'hôtel 11, hors de France, sont écartés), et
    # autant de lignes que dans la table des hôtels de l'entrepôt.
    assert sorted(enrichi["hotel_id"]) == [10, 12, 13]
    assert resultat["charges"]["hotels"] == len(enrichi)
    assert {"distance_km", "commune", "hors_commune", "rang_meteo", "ville"} <= set(enrichi)
    villes = lire_parquet(lake, f"curated/villes/date={DATE}/villes.parquet")
    assert villes["ville"].tolist() == ["Nice", "Lille"]  # ordre du classement
    assert villes["latitude"].tolist() == [43.70, 50.63]  # les vraies coordonnées des villes
    # L'ETL n'a pas laissé de dossier de travail dans le lake.
    assert not (tmp_path / "lake" / ".tmp").exists()


@SPARK
def test_hotel_garde_pour_la_destination_la_plus_proche(tmp_path):
    """Un hôtel sorti pour deux villes voisines est rattaché à la plus proche, pas à la première.

    Biarritz et Bayonne sont à une dizaine de kilomètres ; l'hôtel 77 est collecté pour les deux
    (Biarritz figure en PREMIER dans le fichier : l'ancien dropDuplicates l'aurait gardé) mais il
    est dans Bayonne.
    """
    hotels = [
        _hotel(77, 1, 43.4933, -1.4740, None),  # collecté pour Biarritz (id 1)... et
        _hotel(77, 2, 43.4933, -1.4740, None),  # pour Bayonne (id 2) : il est dans Bayonne
        _hotel(78, 1, 43.4800, -1.5600, None),  # hôtel de Biarritz, unique
    ]
    lake = _ecrire_lake(
        tmp_path / "lake",
        [(1, "Biarritz", 43.4832, -1.5586), (2, "Bayonne", 43.4933, -1.4748)],
        hotels,
        {1: 18, 2: 17},
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    assert resultat["qualite"]["hotels_en_double"] == 1
    with create_engine(url).connect() as connexion:
        lignes = connexion.execute(
            text("SELECT hotel_id, id_ville, distance_km FROM hotels ORDER BY hotel_id")
        ).fetchall()
    assert [(h, v) for h, v, _ in lignes] == [(77, 2), (78, 1)]
    # Distance à Bayonne : quelques dizaines de mètres (l'hôtel est presque au centre).
    assert lignes[0][2] < 0.2


@SPARK
def test_distance_haversine_et_commune_de_l_hotel(tmp_path):
    """Distance au centre en km (haversine) et marquage « hors commune » selon addr:city."""
    centre = (43.5662, 4.1915)  # Aigues-Mortes
    hotels = [
        # 0,01° de latitude au nord = 1,112 km (un degré de méridien vaut 111,195 km).
        _hotel(1, 1, centre[0] + 0.01, centre[1], commune="Aigues-Mortes", source="openstreetmap"),
        _hotel(2, 1, 43.5600, 4.0950, commune="La Grande-Motte", source="openstreetmap"),
        _hotel(3, 1, 43.5700, 4.1900, commune=None, source="openstreetmap"),
        _hotel(4, 2, 43.7706, 6.3300, commune="Aiguines", source="openstreetmap"),
        _hotel(5, 3, 48.6490, -2.0300, commune="Saint-Malo", source="openstreetmap"),
        _hotel(6, 4, 43.8374, 4.3601, commune="Nîmes", source="openstreetmap"),
        _hotel(7, 5, 45.7600, 4.8400, commune="Lyon Cedex 7", source="openstreetmap"),
        _hotel(8, 6, 48.6360, -1.5110, commune="Le Mont-Saint-Michel", source="openstreetmap"),
        _hotel(9, 1, 43.5600, 4.2000, commune="Le Grau-du-Roi", source="openstreetmap"),
    ]
    lake = _ecrire_lake(
        tmp_path / "lake",
        [
            (1, "Aigues Mortes", centre[0], centre[1]),
            (2, "Gorges du Verdon", 43.7706, 6.3300),
            (3, "St Malo", 48.6493, -2.0257),
            (4, "Nimes", 43.8374, 4.3601),
            (5, "Lyon", 45.7640, 4.8357),
            (6, "Mont Saint Michel", 48.6360, -1.5115),
        ],
        hotels,
        {1: 23, 2: 20, 3: 15, 4: 22, 5: 17, 6: 14},
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    with create_engine(url).connect() as connexion:
        lignes = connexion.execute(
            text("SELECT hotel_id, distance_km, hors_commune FROM hotels ORDER BY hotel_id")
        ).fetchall()
    distances = {h: d for h, d, _ in lignes}
    hors = {h: c for h, _, c in lignes}
    assert distances[1] == pytest.approx(1.112, abs=0.002)
    assert 7.0 < distances[2] < 8.5  # La Grande-Motte est à environ 8 km, dans l'aire
    # Même commune malgré la graphie (tirets, accents, abréviation, article, « cedex ») : faux.
    for hotel in (1, 5, 6, 7, 8):
        assert hors[hotel] == 0, hotel
    # Autre commune que la destination : vrai.
    assert hors[2] == 1 and hors[9] == 1
    # Commune absente d'OpenStreetMap, ou destination qui n'est pas une commune : vide (NULL).
    assert hors[3] is None and hors[4] is None
    assert resultat["qualite"]["hotels_hors_commune"] == 2
    assert resultat["qualite"]["hotels_commune_non_evaluable"] == 2


@SPARK
def test_hotel_declare_hors_de_france_est_ecarte_meme_dans_le_rectangle(tmp_path):
    """Un hôtel de Kehl (Allemagne) est dans le rectangle « France » : le pays déclaré l'écarte.

    Kehl est de l'autre côté du Rhin, à quelques kilomètres de Strasbourg : ses coordonnées
    tombent dans le rectangle englobant la France, que seul le contrôle du pays déclaré
    (addr:country) peut écarter. Un pays « FR » ou absent n'écarte rien.
    """
    hotels = [
        _hotel(1, 1, 48.5765, 7.8157, pays="DE", source="openstreetmap"),  # Kehl : écarté
        _hotel(2, 1, 48.5800, 7.7500, pays="fr", source="openstreetmap"),  # Strasbourg, « fr »
        _hotel(3, 1, 48.5700, 7.7600, source="openstreetmap"),  # pays non renseigné : gardé
        _hotel(4, 1, 40.0, 7.7500, source="openstreetmap"),  # hors du rectangle : écarté
    ]
    lake = _ecrire_lake(
        tmp_path / "lake", [(1, "Strasbourg", 48.5846, 7.7507)], hotels, {1: 14}
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    assert resultat["qualite"]["hotels_hors_de_france"] == 2
    with create_engine(url).connect() as connexion:
        ids = [r[0] for r in connexion.execute(text("SELECT hotel_id FROM hotels ORDER BY 1"))]
    assert ids == [2, 3]


@SPARK
def test_csv_enrichi_ecrit_par_spark_identique_au_parquet(tmp_path):
    """Le CSV enrichi de l'énoncé est écrit par Spark et contient les mêmes lignes que le Parquet.

    Les valeurs à protéger sont de vrais pièges du CSV : une virgule, des guillemets et un
    saut de ligne dans la description d'un hôtel. Le fichier doit se relire à l'identique.
    """
    description = 'Hôtel "Le Clos", 4 étoiles,\nvue sur la mer'
    hotels = [
        _hotel(10, 1, 43.70, 7.26, 9.1, description=description),
        _hotel(13, 2, 50.62, 3.05, 7.5),
    ]
    lake = _ecrire_lake(
        tmp_path / "lake",
        [(1, "Nice", 43.70, 7.27), (2, "Lille", 50.63, 3.06)],
        hotels,
        {1: 18, 2: 6},
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    chemin = f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.csv"
    assert resultat["curated"]["kayak_enrichi_csv"] == chemin
    assert lake.existe(chemin)
    csv = pd.read_csv(io.BytesIO(lake.lire(chemin)))
    parquet = lire_parquet(lake, f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet")
    assert csv["hotel_id"].tolist() == parquet["hotel_id"].tolist() == [10, 13]
    assert csv.columns.tolist() == parquet.columns.tolist()
    # La description piégée revient identique (virgule, guillemets et saut de ligne compris).
    assert csv.loc[csv["hotel_id"] == 10, "description"].item() == description
    assert csv["ville"].tolist() == parquet["ville"].tolist()
    assert csv["distance_km"].tolist() == pytest.approx(parquet["distance_km"].tolist())
    # L'ETL ne laisse pas de dossier de travail ni de fichier partiel dans le lake.
    assert not [c for c in lake.lister("") if c.endswith((".tmp", ".crc"))]


# --- Chargement de l'entrepôt : par lots et dans une seule transaction ---------------------------


def _villes_meteo_hotels(nb_hotels: int, a_partir_de: int = 1):
    """Petites tables en dictionnaires, pour tester le chargement sans passer par Spark."""
    villes = [{"id_ville": 1, "ville": "A", "rang_meteo": 1}, {"id_ville": 2, "ville": "B"}]
    meteo = [{"id_ville": 1, "date": pd.Timestamp("2026-01-01").date(), "temp_max_c": 20.0}]
    hotels = [
        {"hotel_id": i, "id_ville": 1, "nom": f"H{i}", "distance_km": 0.5}
        for i in range(a_partir_de, a_partir_de + nb_hotels)
    ]
    return villes, meteo, hotels


def test_par_lots_decoupe_sans_perdre_de_ligne():
    """Le découpage rend des lots d'au plus ``taille`` lignes, dans l'ordre, sans en perdre."""
    lots = list(par_lots(({"i": i} for i in range(5)), 2))
    assert [len(lot) for lot in lots] == [2, 2, 1]
    assert [ligne["i"] for lot in lots for ligne in lot] == [0, 1, 2, 3, 4]
    assert list(par_lots(iter([]), 3)) == []


def test_chargement_par_lots(tmp_path, monkeypatch):
    """Le chargement insère les hôtels lot par lot (ici 2, 2 puis 1 ligne) et les compte."""
    tailles: list[int] = []
    origine = spark_etl.par_lots

    def espion(lignes, taille):
        for lot in origine(lignes, taille):
            tailles.append(len(lot))
            yield lot

    monkeypatch.setattr(spark_etl, "par_lots", espion)
    url = f"sqlite:///{(tmp_path / 'e.db').as_posix()}"
    villes, meteo, hotels = _villes_meteo_hotels(5)
    charges = charger_entrepot(villes, meteo, hotels, url, taille_lot=2)
    assert charges == {"villes": 2, "meteo_quotidienne": 1, "hotels": 5}
    # Villes : 2 lignes en un lot ; météo : 1 ligne ; hôtels : 5 lignes en lots de 2, 2 et 1.
    assert tailles == [2, 1, 2, 2, 1]


def test_echec_de_chargement_laisse_l_entrepot_intact(tmp_path):
    """Un lot qui échoue annule TOUT : l'entrepôt garde son contenu précédent, vue comprise.

    Avant la correction, les tables étaient supprimées puis recréées hors transaction, puis
    remplies par un seul executemany : un échec laissait des tables vides.
    """
    url = f"sqlite:///{(tmp_path / 'e.db').as_posix()}"
    villes, meteo, hotels = _villes_meteo_hotels(3)
    assert charger_entrepot(villes, meteo, hotels, url)["hotels"] == 3

    # Seconde charge : le deuxième lot contient une clé primaire en double (hotel_id 11 deux fois).
    villes2, meteo2, _ = _villes_meteo_hotels(0)
    hotels2 = [
        {"hotel_id": 10, "id_ville": 1, "nom": "N1"},
        {"hotel_id": 11, "id_ville": 1, "nom": "N2"},
        {"hotel_id": 11, "id_ville": 2, "nom": "N2 bis"},
    ]
    with pytest.raises(IntegrityError):
        charger_entrepot(villes2, meteo2, hotels2, url, taille_lot=2)

    with create_engine(url).connect() as connexion:
        noms = [r[0] for r in connexion.execute(text("SELECT nom FROM hotels ORDER BY hotel_id"))]
        nb_villes = connexion.execute(text("SELECT COUNT(*) FROM villes")).scalar_one()
        nb_vue = connexion.execute(
            text("SELECT COUNT(*) FROM vue_hotels_destinations")
        ).scalar_one()
    assert noms == ["H1", "H2", "H3"]  # l'ancien contenu est intact
    assert nb_villes == 2 and nb_vue == 3  # villes conservées, vue toujours là


def test_chargement_idempotent(tmp_path):
    """Relancer le chargement avec les mêmes données redonne les mêmes tables, sans doublon."""
    url = f"sqlite:///{(tmp_path / 'e.db').as_posix()}"
    villes, meteo, hotels = _villes_meteo_hotels(4)
    premier = charger_entrepot(villes, meteo, hotels, url)
    second = charger_entrepot(villes, meteo, hotels, url)
    assert premier == second == {"villes": 2, "meteo_quotidienne": 1, "hotels": 4}


# Paires (commune telle qu'écrite par OpenStreetMap, forme comparable attendue) : chaque règle de
# la normalisation a son cas, dont les ligatures, chaque lettre accentuée et l'abréviation « Ste ».
NORMALISATIONS = [
    ("Aigues-Mortes", "aigues mortes"),
    ("Nîmes", "nimes"),
    ("Le Mont-Saint-Michel", "mont saint michel"),
    ("St Malo", "saint malo"),
    ("Ste Maxime", "sainte maxime"),
    ("Sainte-Maxime", "sainte maxime"),
    ("Lyon Cedex 7", "lyon"),
    ("Cœur-de-Lœuvre", "coeur de loeuvre"),
    ("Lætitia", "laetitia"),
    ("CŒUR", "coeur"),
    ("àâäáãåçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaaceeeeiiiinooooouuuuyy"),
    ("ÀÂÄÁÃÅÇÉÈÊËÍÌÎÏÑÓÒÔÖÕÚÙÛÜÝŸ", "aaaaaaceeeeiiiinooooouuuuyy"),
]


@SPARK
def test_normalisation_des_noms_de_lieu_chaque_regle():
    """Accents, casse, ligatures (œ, æ), ponctuation, « cedex », « St » et « Ste », article :
    chaque règle ramène la commune à la forme de la destination, lettre par lettre."""
    spark = spark_etl.obtenir_spark()
    communes = spark.createDataFrame([(brut,) for brut, _ in NORMALISATIONS], ["commune"])
    lignes = communes.select(_normaliser_lieu("commune")).collect()
    resultat = [ligne[0] for ligne in lignes]
    assert resultat == [attendu for _, attendu in NORMALISATIONS]


@SPARK
def test_agregats_meteo_arrondis_et_une_prevision_par_ville_et_par_jour(tmp_path):
    """Trois jours de prévisions, dont un donné deux fois par deux sources : un seul est gardé.

    Le doublon est repéré sur (ville, jour) seulement, même si la source diffère. Les moyennes
    tombent sur des décimales infinies : chaque agrégat est arrondi à son nombre de décimales
    (température, pluie et ensoleillement à 2, probabilité de pluie à 1).
    """
    lake = _ecrire_lake(
        tmp_path / "lake", [(1, "Nice", 43.70, 7.27)], [_hotel(10, 1, 43.70, 7.26, 9.1)], {1: 20}
    )
    jours = [
        ("2026-01-01", 20.0, 0.123, 8.0, 10, "open-meteo"),
        ("2026-01-01", 20.0, 0.123, 8.0, 10, "openweather"),  # même ville, même jour
        ("2026-01-02", 21.0, 0.004, 9.0, 15, "open-meteo"),
        ("2026-01-03", 21.0, 0.0, 9.5, 16, "open-meteo"),
    ]
    previsions = [
        {**_prevision(jour, tmax, pluie, soleil), "proba_pluie_pct": proba, "source": source}
        for jour, tmax, pluie, soleil, proba, source in jours
    ]
    lake.ecrire(
        f"raw/meteo/date={DATE}/previsions.jsonl",
        json.dumps({"id_ville": 1, "previsions": previsions}) + "\n",
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    assert resultat["qualite"]["previsions_journalieres"] == 3
    assert resultat["charges"]["meteo_quotidienne"] == 3
    with create_engine(url).connect() as connexion:
        agregats = connexion.execute(
            text(
                "SELECT temp_max_moy_c, pluie_totale_mm, ensoleillement_moy_h, "
                "proba_pluie_moy_pct FROM villes"
            )
        ).fetchall()
    # 62 / 3 = 20,666... ; 0,123 + 0,004 = 0,127 ; 26,5 / 3 = 8,833... ; 41 / 3 = 13,666...
    assert [tuple(ligne) for ligne in agregats] == [(20.67, 0.13, 8.83, 13.7)]
    # Les hôtels de Booking n'ont pas de colonne « source » : elle vaut « booking » par défaut.
    with create_engine(url).connect() as connexion:
        sources = connexion.execute(text("SELECT hotel_id, source FROM hotels")).fetchall()
    assert [tuple(ligne) for ligne in sources] == [(10, "booking")]


@SPARK
def test_ville_sans_aucune_prevision_fait_echouer_l_etl_au_lieu_d_etre_retiree(tmp_path):
    """Une destination sans aucune prévision n'est pas retirée en silence du classement.

    Les villes sont jointes à leurs agrégats en gardant TOUTES les villes : celle qui n'a pas de
    météo arrive au calcul du score sans mesures, et le calcul refuse alors de lui donner un rang
    (voir tests/test_score_inverses.py). Rien n'est chargé dans l'entrepôt.
    """
    lake = _ecrire_lake(
        tmp_path / "lake",
        [(1, "Nice", 43.70, 7.27), (2, "Lille", 50.63, 3.06), (3, "Biarritz", 43.48, -1.56)],
        [_hotel(10, 1, 43.70, 7.26, 9.1)],
        {1: 18, 2: 6},  # aucune prévision pour Biarritz
    )
    entrepot = tmp_path / "entrepot.db"
    with pytest.raises(ValueError, match="non-finite"):
        executer_etl(lake, DATE, f"sqlite:///{entrepot.as_posix()}")
    assert not entrepot.exists()


@SPARK
def test_hotel_rattache_a_une_ville_inconnue_est_refuse(tmp_path):
    """Un hôtel dont la ville n'est pas dans les coordonnées est refusé : l'ETL s'arrête et le dit.

    Gardé, il n'aurait ni destination ni distance et serait chargé comme une ligne orpheline de
    l'entrepôt ; écarté sans rien dire, il disparaîtrait des comptes. Rien n'est chargé.
    """
    lake = _ecrire_lake(
        tmp_path / "lake",
        [(1, "Nice", 43.70, 7.27), (2, "Lille", 50.63, 3.06)],
        [_hotel(10, 1, 43.70, 7.26, 9.1), _hotel(11, 7, 43.71, 7.25, 8.0)],  # ville 7 inconnue
        {1: 18, 2: 6},
    )
    entrepot = tmp_path / "entrepot.db"
    message = (
        r"^1 hôtel\(s\) rattaché\(s\) à une ville inconnue "
        r"\(absente des coordonnées, ou sans nom\)$"
    )
    with pytest.raises(ValueError, match=message):
        executer_etl(lake, DATE, f"sqlite:///{entrepot.as_posix()}")
    assert not entrepot.exists()


@SPARK
def test_hotel_a_egale_distance_de_deux_destinations_va_a_la_plus_petite_ville(tmp_path):
    """À distance exactement égale de deux destinations, l'hôtel va à celle de plus petit numéro.

    Les deux centres sont symétriques de part et d'autre de l'hôtel (même latitude) : les deux
    distances sont égales au bit près. Le départage par numéro de ville rend le résultat
    reproductible ; la ligne de la plus grande ville est écrite la première, pour qu'un tri sans
    départage la garde.
    """
    lake = _ecrire_lake(
        tmp_path / "lake",
        [(1, "Ouest", 45.0, 4.9), (2, "Est", 45.0, 5.1)],
        [_hotel(10, 2, 45.0, 5.0, 9.0), _hotel(10, 1, 45.0, 5.0, 9.0)],
        {1: 18, 2: 6},
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    resultat = executer_etl(lake, DATE, url)
    assert resultat["qualite"]["hotels_en_double"] == 1
    with create_engine(url).connect() as connexion:
        lignes = connexion.execute(text("SELECT hotel_id, id_ville FROM hotels")).fetchall()
    assert [tuple(ligne) for ligne in lignes] == [(10, 1)]


@SPARK
def test_table_enrichie_triee_par_note_puis_etoiles_puis_distance_puis_nom(tmp_path):
    """Dans une destination : note décroissante, puis étoiles décroissantes, puis distance
    croissante, puis nom ; une note ou une classe absente passe après les autres.

    Chaque clé de tri départage au moins une paire d'hôtels, et l'ordre d'écriture des hôtels
    n'est pas celui attendu.
    """
    centre = (43.70, 7.27)
    proche, loin = (43.70, 7.271), (43.70, 7.29)
    hotels = [
        _hotel(1, 1, *proche, None),  # note absente : en dernier
        _hotel(2, 1, *proche, 8.0),  # note plus basse
        _hotel(3, 1, *proche, 9.0, etoiles=None),  # classe absente : après les classés
        _hotel(4, 1, *proche, 9.0, etoiles=3),
        _hotel(5, 1, *loin, 9.0, etoiles=4),  # même note et classe, plus loin
        # Ex æquo complet : départagés par le nom, dans l'ordre inverse de leurs identifiants.
        _hotel(6, 1, *proche, 9.0, etoiles=4, nom="Hôtel Zéphyr"),
        _hotel(7, 1, *proche, 9.0, etoiles=4, nom="Hôtel Azur"),
    ]
    lake = _ecrire_lake(tmp_path / "lake", [(1, "Nice", *centre)], hotels, {1: 18})
    executer_etl(lake, DATE, f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}")
    enrichi = lire_parquet(lake, f"curated/kayak_enrichi/date={DATE}/kayak_enrichi.parquet")
    assert enrichi["hotel_id"].tolist() == [7, 6, 5, 4, 3, 2, 1]


@pytest.fixture(params=["true", "false"], ids=["partitions_regroupees", "partitions_separees"])
def regroupement_des_partitions(request):
    """Règle le regroupement adaptatif des partitions de brassage de Spark le temps d'un test.

    « true » est le défaut de Spark : sur un petit volume, il réunit ses partitions en une
    seule. « false » les garde séparées (8 ici), comme sur un gros volume. Le réglage d'avant est
    rendu à la fin du test.
    """
    spark = spark_etl.obtenir_spark()
    reglage = "spark.sql.adaptive.coalescePartitions.enabled"
    avant = spark.conf.get(reglage)
    spark.conf.set(reglage, request.param)
    yield request.param
    spark.conf.set(reglage, avant)


@SPARK
def test_entrepot_charge_par_ville_puis_jour_et_par_ville_puis_hotel(
    tmp_path, regroupement_des_partitions
):
    """L'entrepôt est chargé dans un ordre stable : la météo par ville puis jour, les hôtels par
    ville puis identifiant (ordre d'insertion, ``rowid`` de SQLite).

    Les fichiers bruts sont écrits dans le désordre (jours à rebours, identifiants mêlés entre
    deux villes). Sur un si petit volume, Spark réunit ses partitions de brassage en une seule :
    l'ordre pourrait alors venir de ce hasard. Le cas « partitions_separees » les garde séparées,
    comme sur un gros volume : l'ordre ne peut plus venir que du tri de l'ETL, clé secondaire
    comprise (jour, identifiant de l'hôtel).
    """
    centres = {1: (43.70, 7.27), 2: (50.63, 3.06)}
    identifiants = {1: [38, 15, 47, 22, 31, 10], 2: [50, 33, 19, 27, 42, 11]}
    hotels = []
    for rang in range(6):  # les deux villes alternent, chacune dans un ordre d'identifiants mêlé
        for ville in (2, 1):
            latitude, longitude = centres[ville]
            ident = identifiants[ville][rang]
            hotels.append(_hotel(ident, ville, latitude + rang / 1000, longitude, 8.0))
    lake = _ecrire_lake(
        tmp_path / "lake", [(2, "Lille", *centres[2]), (1, "Nice", *centres[1])], hotels, {}
    )
    jours = [f"2026-01-0{numero}" for numero in range(7, 0, -1)]  # du 7 au 1er janvier
    previsions = [
        {"id_ville": ville, "previsions": [_prevision(jour, 15, 0, 5) for jour in jours]}
        for ville in (2, 1)
    ]
    lake.ecrire(
        f"raw/meteo/date={DATE}/previsions.jsonl",
        "".join(json.dumps(p) + "\n" for p in previsions),
    )
    url = f"sqlite:///{(tmp_path / 'entrepot.db').as_posix()}"
    executer_etl(lake, DATE, url)
    with create_engine(url).connect() as connexion:
        meteo = connexion.execute(
            text("SELECT id_ville, date FROM meteo_quotidienne ORDER BY rowid")
        ).fetchall()
        en_base = connexion.execute(
            text("SELECT id_ville, hotel_id FROM hotels ORDER BY rowid")
        ).fetchall()
    attendu_meteo = [(ville, jour) for ville in (1, 2) for jour in sorted(jours)]
    attendu_hotels = [(ville, ident) for ville in (1, 2) for ident in sorted(identifiants[ville])]
    assert [tuple(ligne) for ligne in meteo] == attendu_meteo
    assert [tuple(ligne) for ligne in en_base] == attendu_hotels
