# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests des parties de `kayak/etl/spark_etl.py` qui n'ont PAS besoin de Spark.

Aucune session Spark, aucune machine virtuelle Java : le découpage par lots, le chargement de
l'entrepôt SQL (SQLite dans un dossier temporaire), le moteur transactionnel, la conversion
pandas, la détection de Hadoop, le choix de la session, le dossier de travail et l'écriture
CSV / Parquet du lac sont vérifiés avec des objets factices (faux `SparkSession`, faux
DataFrame) ou de vrais fichiers locaux. Les tests Spark restent dans `tests/test_etl.py`.

Ce fichier prouve surtout des REFUS et des états INCHANGÉS : un lot qui échoue annule toute la
transaction et l'entrepôt garde son contenu précédent ; un dossier d'écriture mal formé ne
publie rien ; une variable d'environnement, un objet pandas ou un fichier voisin ne sont jamais
touchés par erreur. Aucun réseau.
"""

import copy
import datetime
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pyspark
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import (
    ArgumentError,
    IntegrityError,
    NoSuchModuleError,
    OperationalError,
    StatementError,
)

from kayak.etl import spark_etl
from kayak.etl.spark_etl import (
    _creer_moteur,
    _dossier_de_travail,
    _ecrire_executeurs,
    _hadoop_natif_disponible,
    _lignes_pandas,
    _lignes_spark,
    charger_entrepot,
    ecrire_csv,
    ecrire_parquet,
    obtenir_spark,
    par_lots,
)
from kayak.stockage.data_lake import DataLake

JOUR = datetime.date(2026, 10, 10)
LENDEMAIN = datetime.date(2026, 10, 11)
VARIABLES_JAVA = ("JAVA_HOME", "PYSPARK_PYTHON", "PYSPARK_DRIVER_PYTHON")
CHEMIN_CSV = "curated/kayak_enrichi/date=2026-10-10/kayak_enrichi.csv"
CHEMIN_PARQUET = "curated/villes/date=2026-10-10/villes.parquet"


# --- Jeux de données et photographies de l'entrepôt ---------------------------------------------


def _url(dossier: Path, nom: str = "entrepot.db") -> str:
    """URL SQLAlchemy d'un fichier SQLite du dossier temporaire."""
    return f"sqlite:///{(dossier / nom).as_posix()}"


def _lake(dossier: Path) -> DataLake:
    """Data lake local dans un dossier temporaire."""
    return DataLake(bucket=None, racine_locale=dossier / "lake")


def _ville(ident: int, nom: str, rang: int) -> dict[str, Any]:
    """Une ville avec toutes ses colonnes renseignées."""
    return {
        "id_ville": ident,
        "ville": nom,
        "latitude": 43.0 + ident,
        "longitude": 4.0 + ident,
        "temp_max_moy_c": 19.5,
        "pluie_totale_mm": 3.5,
        "ensoleillement_moy_h": 8.5,
        "proba_pluie_moy_pct": 20.0,
        "score_meteo": 0.9,
        "rang_meteo": rang,
    }


def _prevision(id_ville: int, jour: datetime.date) -> dict[str, Any]:
    """Une prévision journalière avec toutes ses colonnes renseignées."""
    return {
        "id_ville": id_ville,
        "date": jour,
        "temp_max_c": 20.0,
        "temp_min_c": 12.0,
        "pluie_mm": 0.0,
        "proba_pluie_pct": 5.0,
        "ensoleillement_h": 9.0,
        "vent_max_kmh": 14.0,
        "source": "open-meteo",
    }


def _hotel(ident: int, id_ville: int = 1, **autres: Any) -> dict[str, Any]:
    """Un hôtel minimal (les trois colonnes obligatoires) ; `autres` ajoute des colonnes."""
    return {"hotel_id": ident, "id_ville": id_ville, "nom": f"Hôtel {ident}", **autres}


def _jeu() -> tuple[list[dict], list[dict], list[dict]]:
    """Trois villes, quatre prévisions, trois hôtels : de quoi remplir l'entrepôt."""
    villes = [_ville(1, "Nice", 1), _ville(2, "Lille", 2), _ville(3, "Brest", 3)]
    meteo = [
        _prevision(1, JOUR),
        _prevision(1, LENDEMAIN),
        _prevision(2, JOUR),
        _prevision(3, JOUR),
    ]
    hotels = [_hotel(100, 1, note=8.5), _hotel(101, 2), _hotel(102, 1, etoiles=3)]
    return villes, meteo, hotels


def _jeu_different() -> tuple[list[dict], list[dict], list[dict]]:
    """Un second jeu, sans doublon, dont chaque valeur diffère du premier."""
    villes = [_ville(1, "Nouvelle A", 1), _ville(2, "Nouvelle B", 2)]
    meteo = [_prevision(1, LENDEMAIN)]
    hotels = [_hotel(900, 1), _hotel(901, 2)]
    return villes, meteo, hotels


def _photographie(url: str) -> dict[str, Any]:
    """Tout l'état d'un entrepôt : colonnes, lignes (triées) et index de chaque table, lignes de
    chaque vue. Deux photographies égales = deux entrepôts identiques."""
    moteur = create_engine(url)
    inspecteur = inspect(moteur)
    etat: dict[str, Any] = {}
    with moteur.connect() as connexion:
        for nom in sorted(inspecteur.get_table_names()):
            lignes = connexion.execute(text(f"SELECT * FROM {nom}")).fetchall()
            etat[f"lignes:{nom}"] = sorted((tuple(ligne) for ligne in lignes), key=repr)
            etat[f"colonnes:{nom}"] = [c["name"] for c in inspecteur.get_columns(nom)]
            etat[f"index:{nom}"] = sorted(i["name"] for i in inspecteur.get_indexes(nom))
        for nom in sorted(inspecteur.get_view_names()):
            lignes = connexion.execute(text(f"SELECT * FROM {nom}")).fetchall()
            etat[f"vue:{nom}"] = sorted((tuple(ligne) for ligne in lignes), key=repr)
    moteur.dispose()
    return etat


def _fichiers_du_lake(lake: DataLake) -> dict[str, bytes]:
    """Photographie du lake : chaque objet (chemin logique) et ses octets."""
    return {chemin: lake.lire(chemin) for chemin in lake.lister("")}


# --- par_lots -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nombre", "taille", "attendu"),
    [
        (7, 3, [[0, 1, 2], [3, 4, 5], [6]]),
        (6, 3, [[0, 1, 2], [3, 4, 5]]),
        (4, 4, [[0, 1, 2, 3]]),
        (2, 5, [[0, 1]]),
        (3, 1, [[0], [1], [2]]),
    ],
    ids=["dernier_lot_partiel", "multiple_exact", "taille_egale", "taille_superieure", "taille_un"],
)
def test_par_lots_coupe_en_lots_d_au_plus_taille_lignes(nombre, taille, attendu):
    """Un lot est rendu dès qu'il atteint `taille` lignes ; le reste forme un dernier lot partiel,
    et un nombre de lignes multiple de la taille ne laisse pas de lot vide à la fin."""
    lots = list(par_lots(({"i": i} for i in range(nombre)), taille))
    assert [[ligne["i"] for ligne in lot] for lot in lots] == attendu


def test_par_lots_flux_vide_ne_produit_aucun_lot():
    """Un flux vide ne donne même pas un lot vide : le premier `next` lève StopIteration."""
    with pytest.raises(StopIteration):
        next(par_lots(iter(()), 3))


def test_par_lots_lit_le_flux_paresseusement_et_laisse_le_reste_intact():
    """Après le premier lot, la source n'a produit que `taille` lignes (rien n'est lu d'avance) et
    les lignes suivantes sont intactes, prêtes à être lues par le lot suivant."""
    produites: list[int] = []

    def flux():
        for i in range(10):
            produites.append(i)
            yield {"i": i}

    source = flux()
    lots = par_lots(source, 3)
    assert produites == []
    premier = next(lots)
    assert [ligne["i"] for ligne in premier] == [0, 1, 2]
    assert produites == [0, 1, 2]
    assert [ligne["i"] for ligne in source] == [3, 4, 5, 6, 7, 8, 9]


def test_par_lots_ne_modifie_pas_les_lignes_et_ne_les_copie_pas():
    """Les enregistrements sortent tels quels (mêmes objets) : ni copiés, ni modifiés."""
    lignes = [{"i": 1, "valeurs": [1]}, {"i": 2, "valeurs": [2]}]
    avant = copy.deepcopy(lignes)
    lots = list(par_lots(lignes, 1))
    assert lignes == avant
    assert [lot[0] is ligne for lot, ligne in zip(lots, lignes, strict=True)] == [True, True]


def test_par_lots_refuse_un_flux_absent():
    """`None` n'est pas un flux : l'erreur est levée à la première lecture, elle n'est pas
    transformée en flux vide."""
    lots = par_lots(None, 3)
    with pytest.raises(TypeError, match="not iterable"):
        next(lots)


def test_par_lots_refuse_une_taille_qui_n_est_pas_un_nombre():
    """Une taille de lot textuelle est refusée à la première ligne, elle n'est pas convertie."""
    lots = par_lots([{"i": 1}], "3")
    with pytest.raises(TypeError, match="not supported between instances of 'int' and 'str'"):
        next(lots)


# --- charger_entrepot : structure ---------------------------------------------------------------

SCHEMA = {
    "villes": [
        ("id_ville", "INTEGER"),
        ("ville", "VARCHAR(100)"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
        ("temp_max_moy_c", "FLOAT"),
        ("pluie_totale_mm", "FLOAT"),
        ("ensoleillement_moy_h", "FLOAT"),
        ("proba_pluie_moy_pct", "FLOAT"),
        ("score_meteo", "FLOAT"),
        ("rang_meteo", "INTEGER"),
    ],
    "meteo_quotidienne": [
        ("id_ville", "INTEGER"),
        ("date", "DATE"),
        ("temp_max_c", "FLOAT"),
        ("temp_min_c", "FLOAT"),
        ("pluie_mm", "FLOAT"),
        ("proba_pluie_pct", "FLOAT"),
        ("ensoleillement_h", "FLOAT"),
        ("vent_max_kmh", "FLOAT"),
        ("source", "VARCHAR(20)"),
    ],
    "hotels": [
        ("hotel_id", "BIGINT"),
        ("id_ville", "INTEGER"),
        ("nom", "VARCHAR(300)"),
        ("url", "VARCHAR(500)"),
        ("latitude", "FLOAT"),
        ("longitude", "FLOAT"),
        ("adresse", "VARCHAR(300)"),
        ("note", "FLOAT"),
        ("nb_avis", "INTEGER"),
        ("etoiles", "INTEGER"),
        ("description", "TEXT"),
        ("date_collecte", "DATE"),
        ("source", "VARCHAR(20)"),
        ("distance_km", "FLOAT"),
        ("commune", "VARCHAR(100)"),
        ("hors_commune", "BOOLEAN"),
    ],
}


def test_schema_de_l_entrepot_tables_colonnes_et_types_exacts(tmp_path):
    """L'entrepôt contient exactement trois tables, avec leurs colonnes et leurs types dans
    l'ordre du cahier des charges (rien de plus, rien de moins)."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    inspecteur = inspect(create_engine(url))
    assert set(inspecteur.get_table_names()) == set(SCHEMA)
    for table, attendu in SCHEMA.items():
        colonnes = [(c["name"], str(c["type"])) for c in inspecteur.get_columns(table)]
        assert colonnes == attendu, table


def test_cles_primaires_etrangeres_et_index_de_l_entrepot(tmp_path):
    """Clés primaires (simple, composite), clés étrangères vers `villes` et les deux index non
    uniques des colonnes de filtre et de tri (`id_ville`, `note`)."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    inspecteur = inspect(create_engine(url))
    cles = {table: inspecteur.get_pk_constraint(table)["constrained_columns"] for table in SCHEMA}
    assert cles == {
        "villes": ["id_ville"],
        "meteo_quotidienne": ["id_ville", "date"],
        "hotels": ["hotel_id"],
    }
    etrangeres = {
        table: [
            (f["constrained_columns"], f["referred_table"], f["referred_columns"])
            for f in inspecteur.get_foreign_keys(table)
        ]
        for table in SCHEMA
    }
    vers_villes = [(["id_ville"], "villes", ["id_ville"])]
    assert etrangeres == {"villes": [], "meteo_quotidienne": vers_villes, "hotels": vers_villes}
    index = {
        i["name"]: (i["column_names"], bool(i["unique"])) for i in inspecteur.get_indexes("hotels")
    }
    assert index == {
        "ix_hotels_id_ville": (["id_ville"], False),
        "ix_hotels_note": (["note"], False),
    }
    assert inspecteur.get_indexes("villes") == []
    assert inspecteur.get_indexes("meteo_quotidienne") == []


def test_vue_hotels_destinations_expose_l_hotel_et_la_meteo_de_sa_ville(tmp_path):
    """La vue métier joint chaque hôtel à sa ville et expose, dans cet ordre, les 16 colonnes
    annoncées ; chaque valeur vient de la bonne table et de la bonne colonne."""
    url = _url(tmp_path)
    villes = [_ville(1, "Nice", 4)]
    hotels = [
        _hotel(
            100,
            1,
            nom="Hôtel Azur",
            note=8.7,
            nb_avis=120,
            etoiles=4,
            url="https://exemple.test/h100",
            latitude=43.69,
            longitude=7.25,
            source="openstreetmap",
            distance_km=1.25,
            commune="Nice",
            hors_commune=True,
        )
    ]
    charger_entrepot(villes, [], hotels, url)
    moteur = create_engine(url)
    assert inspect(moteur).get_view_names() == ["vue_hotels_destinations"]
    with moteur.connect() as connexion:
        lignes = [
            dict(ligne)
            for ligne in connexion.execute(text("SELECT * FROM vue_hotels_destinations")).mappings()
        ]
    assert lignes == [
        {
            "rang_meteo": 4,
            "ville": "Nice",
            "score_meteo": 0.9,
            "temp_max_moy_c": 19.5,
            "pluie_totale_mm": 3.5,
            "hotel": "Hôtel Azur",
            "note": 8.7,
            "nb_avis": 120,
            "etoiles": 4,
            "url": "https://exemple.test/h100",
            "latitude": 43.69,
            "longitude": 7.25,
            "source": "openstreetmap",
            "distance_km": 1.25,
            "commune": "Nice",
            "hors_commune": 1,
        }
    ]
    assert list(lignes[0]) == [
        "rang_meteo",
        "ville",
        "score_meteo",
        "temp_max_moy_c",
        "pluie_totale_mm",
        "hotel",
        "note",
        "nb_avis",
        "etoiles",
        "url",
        "latitude",
        "longitude",
        "source",
        "distance_km",
        "commune",
        "hors_commune",
    ]


def test_definition_de_la_vue_gardee_par_l_entrepot_est_le_texte_exact_du_code(tmp_path):
    """SQLite garde le texte de la requête d'une vue tel qu'il a été écrit (``sqlite_master``) :
    c'est ce que lit un analyste qui affiche le schéma (``.schema`` du client sqlite3), et ce que
    contient le fichier ``entrepot/kayak.db`` livré. Le texte stocké est donc vérifié en entier,
    casse comprise : une requête de même résultat, mais écrite autrement, changerait l'entrepôt.
    """
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    with create_engine(url).connect() as connexion:
        definitions = connexion.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type = 'view'")
        ).fetchall()
    assert [tuple(ligne) for ligne in definitions] == [
        (
            "vue_hotels_destinations",
            "CREATE VIEW vue_hotels_destinations AS "
            "SELECT v.rang_meteo, v.ville, v.score_meteo, v.temp_max_moy_c, "
            "v.pluie_totale_mm, "
            "h.nom AS hotel, h.note, h.nb_avis, h.etoiles, h.url, h.latitude, h.longitude, "
            "h.source, h.distance_km, h.commune, h.hors_commune "
            "FROM hotels h JOIN villes v ON v.id_ville = h.id_ville",
        )
    ]


def test_hotel_d_une_ville_inconnue_sans_effet_sur_la_vue(tmp_path):
    """La vue joint hôtels et villes (jointure interne) : un hôtel dont la ville n'existe pas
    entre dans la table `hotels` mais n'ajoute aucune ligne à la vue, et les autres lignes de la
    vue restent inchangées."""
    villes, meteo, hotels = _jeu()
    reference = _url(tmp_path, "reference.db")
    avec_orphelin = _url(tmp_path, "orphelin.db")
    charges_reference = charger_entrepot(villes, meteo, hotels, reference)
    charges = charger_entrepot(villes, meteo, [*hotels, _hotel(999, 99)], avec_orphelin)
    assert charges["hotels"] == charges_reference["hotels"] + 1
    vue_reference = _photographie(reference)["vue:vue_hotels_destinations"]
    assert vue_reference
    assert _photographie(avec_orphelin)["vue:vue_hotels_destinations"] == vue_reference


# --- charger_entrepot : valeurs, lots, comptes --------------------------------------------------


@pytest.mark.parametrize(
    ("taille_lot", "nb_hotels", "taille_vue", "lots_attendus"),
    [
        (2, 5, 2, ([2, 1], [2, 2], [2, 2, 1])),
        (None, 501, 500, ([3], [4], [500, 1])),
    ],
    ids=["lots_de_deux", "taille_par_defaut"],
)
def test_charger_decoupe_chaque_table_en_lots_et_renvoie_les_comptes(
    tmp_path, monkeypatch, taille_lot, nb_hotels, taille_vue, lots_attendus
):
    """Les villes, puis la météo, puis les hôtels (ordre des clés étrangères) sont insérés par lots
    de `taille_lot` lignes (500 par défaut) ; les comptes renvoyés sont ceux des tables."""
    enregistre: list[tuple[int, tuple[str, ...], list[int]]] = []
    origine = spark_etl.par_lots

    def espion(lignes, taille):
        lots = list(origine(lignes, taille))
        enregistre.append((taille, tuple(lots[0][0])[:2], [len(lot) for lot in lots]))
        return iter(lots)

    monkeypatch.setattr(spark_etl, "par_lots", espion)
    villes, meteo, _ = _jeu()
    hotels = [_hotel(1000 + i, 1 + i % 3) for i in range(nb_hotels)]
    options = {} if taille_lot is None else {"taille_lot": taille_lot}

    charges = charger_entrepot(villes, meteo, hotels, _url(tmp_path), **options)

    assert charges == {"villes": 3, "meteo_quotidienne": 4, "hotels": nb_hotels}
    assert enregistre == [
        (taille_vue, ("id_ville", "ville"), lots_attendus[0]),
        (taille_vue, ("id_ville", "date"), lots_attendus[1]),
        (taille_vue, ("hotel_id", "id_ville"), lots_attendus[2]),
    ]


def test_valeurs_manquantes_deviennent_null_et_valeurs_fausses_sont_conservees(tmp_path):
    """Une clé absente ou valant None devient NULL ; 0, 0.0, la chaîne vide et False ne sont PAS
    pris pour des valeurs manquantes."""
    url = _url(tmp_path)
    villes = [{"id_ville": 1, "ville": "Nice"}]
    meteo = [{"id_ville": 1, "date": JOUR, "temp_max_c": None, "pluie_mm": 0.0}]
    hotels = [
        _hotel(1, 1, note=None),
        _hotel(2, 1, note=0.0, etoiles=0, description="", hors_commune=False),
    ]
    charger_entrepot(villes, meteo, hotels, url)
    moteur = create_engine(url)
    with moteur.connect() as connexion:
        ville = connexion.execute(text("SELECT * FROM villes")).mappings().one()
        prevision = connexion.execute(text("SELECT * FROM meteo_quotidienne")).mappings().one()
        lignes = connexion.execute(text("SELECT * FROM hotels ORDER BY hotel_id")).mappings().all()
    assert dict(ville) == {
        "id_ville": 1,
        "ville": "Nice",
        **dict.fromkeys(
            [
                "latitude",
                "longitude",
                "temp_max_moy_c",
                "pluie_totale_mm",
                "ensoleillement_moy_h",
                "proba_pluie_moy_pct",
                "score_meteo",
                "rang_meteo",
            ]
        ),
    }
    assert prevision["pluie_mm"] == 0.0
    assert prevision["temp_max_c"] is None
    assert prevision["vent_max_kmh"] is None
    assert lignes[0]["note"] is None
    assert lignes[0]["description"] is None
    assert lignes[0]["hors_commune"] is None
    assert lignes[1]["note"] == 0.0
    assert lignes[1]["etoiles"] == 0
    assert lignes[1]["description"] == ""
    assert lignes[1]["hors_commune"] == 0


# --- charger_entrepot : refus et état inchangé --------------------------------------------------


def test_doublon_de_cle_primaire_d_hotel_est_refuse(tmp_path):
    """Deux hôtels de même `hotel_id` : IntegrityError, c'est la clé primaire de `hotels`."""
    villes, meteo, hotels = _jeu()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, _hotel(100, 2)], _url(tmp_path))


def test_doublon_de_cle_composite_de_meteo_est_refuse(tmp_path):
    """Deux prévisions pour la même ville le même jour : la clé est le couple (ville, date)."""
    villes, meteo, hotels = _jeu()
    attendu = r"UNIQUE constraint failed: meteo_quotidienne\.id_ville, meteo_quotidienne\.date"
    with pytest.raises(IntegrityError, match=attendu):
        charger_entrepot(villes, [*meteo, _prevision(1, JOUR)], hotels, _url(tmp_path))


def test_doublon_de_cle_primaire_de_ville_est_refuse(tmp_path):
    """Deux villes de même `id_ville` : IntegrityError, c'est la clé primaire de `villes`."""
    villes, meteo, hotels = _jeu()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: villes\.id_ville"):
        charger_entrepot([*villes, _ville(1, "Doublon", 9)], meteo, hotels, _url(tmp_path))


def test_doublon_de_cle_entre_deux_lots_est_refuse(tmp_path):
    """La détection ne dépend pas du découpage : le même hôtel dans deux lots différents est
    refusé aussi (lots d'une ligne)."""
    villes, meteo, hotels = _jeu()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, _hotel(101, 1)], _url(tmp_path), taille_lot=1)


def test_ville_sans_nom_est_refusee(tmp_path):
    """Le nom d'une ville est obligatoire : sans `ville`, NOT NULL est violée."""
    villes, meteo, hotels = _jeu()
    with pytest.raises(IntegrityError, match=r"NOT NULL constraint failed: villes\.ville"):
        charger_entrepot([*villes, {"id_ville": 9}], meteo, hotels, _url(tmp_path))


def test_hotel_sans_nom_est_refuse(tmp_path):
    """Le nom d'un hôtel est obligatoire : sans `nom`, NOT NULL est violée."""
    villes, meteo, hotels = _jeu()
    sans_nom = {"hotel_id": 7, "id_ville": 1}
    with pytest.raises(IntegrityError, match=r"NOT NULL constraint failed: hotels\.nom"):
        charger_entrepot(villes, meteo, [*hotels, sans_nom], _url(tmp_path))


def test_hotel_sans_ville_est_refuse(tmp_path):
    """Un hôtel doit être rattaché à une destination : sans `id_ville`, NOT NULL est violée."""
    villes, meteo, hotels = _jeu()
    sans_ville = {"hotel_id": 7, "nom": "Sans ville"}
    with pytest.raises(IntegrityError, match=r"NOT NULL constraint failed: hotels\.id_ville"):
        charger_entrepot(villes, meteo, [*hotels, sans_ville], _url(tmp_path))


def test_hotel_sans_identifiant_est_refuse(tmp_path):
    """Un hôtel sans `hotel_id` n'a pas de clé primaire : il est refusé, non numéroté d'office."""
    villes, meteo, hotels = _jeu()
    sans_identifiant = {"id_ville": 1, "nom": "Sans identifiant"}
    with pytest.raises(IntegrityError, match=r"NOT NULL constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, sans_identifiant], _url(tmp_path))


def test_prevision_sans_ville_est_refusee(tmp_path):
    """Une prévision sans `id_ville` n'appartient à aucune ville : sa clé est incomplète."""
    villes, _, hotels = _jeu()
    with pytest.raises(
        IntegrityError, match=r"NOT NULL constraint failed: meteo_quotidienne\.id_ville"
    ):
        charger_entrepot(villes, [{"date": JOUR}], hotels, _url(tmp_path))


def test_prevision_sans_date_est_refusee(tmp_path):
    """Une prévision sans `date` n'a pas de jour : sa clé primaire est incomplète."""
    villes, _, hotels = _jeu()
    with pytest.raises(
        IntegrityError, match=r"NOT NULL constraint failed: meteo_quotidienne\.date"
    ):
        charger_entrepot(villes, [{"id_ville": 1}], hotels, _url(tmp_path))


def test_date_de_meteo_qui_n_est_pas_une_date_est_refusee(tmp_path):
    """Une date en texte n'est pas convertie en silence : SQLAlchemy refuse l'instruction."""
    villes, _, hotels = _jeu()
    meteo = [{"id_ville": 1, "date": "2026-10-10"}]
    with pytest.raises(StatementError, match="SQLite Date type only accepts Python date objects"):
        charger_entrepot(villes, meteo, hotels, _url(tmp_path))


def test_booleen_d_hotel_qui_n_est_pas_un_booleen_est_refuse(tmp_path):
    """`hors_commune` est un booléen ou NULL : le texte « oui » est refusé, pas stocké tel quel."""
    villes, meteo, _ = _jeu()
    with pytest.raises(StatementError, match="Not a boolean value"):
        charger_entrepot(villes, meteo, [_hotel(5, 1, hors_commune="oui")], _url(tmp_path))


@pytest.mark.parametrize("position", [0, 1, 2], ids=["villes", "meteo", "hotels"])
def test_flux_absent_est_refuse_et_laisse_l_entrepot_inchange(tmp_path, position):
    """Un flux `None` (étape amont en panne) lève TypeError ; l'entrepôt déjà chargé garde son
    contenu et sa vue, sans table vidée."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    flux = list(_jeu())
    flux[position] = None
    with pytest.raises(TypeError, match="not iterable"):
        charger_entrepot(*flux, url)
    assert _photographie(url) == avant


def test_ligne_qui_n_est_pas_un_dictionnaire_est_refusee_et_entrepot_inchange(tmp_path):
    """Un enregistrement sous forme de tuple (au lieu d'un dictionnaire) lève AttributeError et
    annule le chargement."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    villes, meteo, _ = _jeu()
    with pytest.raises(AttributeError, match="'tuple' object has no attribute 'get'"):
        charger_entrepot(villes, meteo, [(1, 1, "Hôtel")], url)
    assert _photographie(url) == avant


def test_echec_dans_les_villes_annule_tout_et_laisse_l_entrepot_inchange(tmp_path):
    """Doublon dans le PREMIER flux inséré : tout est annulé, y compris la suppression des
    anciennes tables ; l'entrepôt précédent (tables, index, vue) est inchangé."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    assert avant["lignes:hotels"]
    villes, meteo, hotels = _jeu_different()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: villes\.id_ville"):
        charger_entrepot([*villes, _ville(2, "Doublon", 9)], meteo, hotels, url)
    assert _photographie(url) == avant


def test_echec_dans_la_meteo_annule_tout_et_laisse_l_entrepot_inchange(tmp_path):
    """Doublon dans le SECOND flux inséré (les villes nouvelles sont déjà insérées) : tout est
    annulé et l'entrepôt précédent est inchangé."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    villes, meteo, hotels = _jeu_different()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: meteo_quotidienne"):
        charger_entrepot(villes, [*meteo, *meteo], hotels, url)
    assert _photographie(url) == avant


def test_echec_dans_les_hotels_annule_tout_et_laisse_l_entrepot_inchange(tmp_path):
    """Doublon dans le DERNIER flux inséré (villes et météo nouvelles déjà insérées) : tout est
    annulé et l'entrepôt précédent est inchangé, vue comprise."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    villes, meteo, hotels = _jeu_different()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, _hotel(900, 2)], url)
    assert _photographie(url) == avant


def test_flux_interrompu_en_cours_de_route_annule_tout_et_laisse_l_entrepot_inchange(tmp_path):
    """Une panne de la source (ici après deux hôtels) se propage telle quelle et annule la
    transaction : les deux hôtels déjà insérés ne subsistent pas."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)

    def hotels_puis_panne():
        yield _hotel(900, 1)
        yield _hotel(901, 2)
        raise RuntimeError("flux interrompu")

    villes, meteo, _ = _jeu_different()
    with pytest.raises(RuntimeError, match="flux interrompu"):
        charger_entrepot(villes, meteo, hotels_puis_panne(), url)
    assert _photographie(url) == avant


def test_echec_sur_base_vierge_ne_laisse_ni_table_ni_vue(tmp_path):
    """Premier chargement qui échoue : la création des tables est annulée aussi (aucune table
    vide ne traîne), alors que SQLite valide d'ordinaire un CREATE TABLE tout de suite."""
    url = _url(tmp_path)
    villes, meteo, hotels = _jeu()
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, _hotel(100, 2)], url)
    inspecteur = inspect(create_engine(url))
    assert inspecteur.get_table_names() == []
    assert inspecteur.get_view_names() == []


def test_colonnes_en_trop_dans_les_flux_sont_sans_effet_sur_l_entrepot(tmp_path):
    """Les clés inconnues (colonnes que l'entrepôt n'a pas) sont ignorées : l'entrepôt obtenu est
    identique à celui des mêmes données sans ces clés, et aucune colonne n'est ajoutée."""
    villes, meteo, hotels = _jeu()
    propre = _url(tmp_path, "propre.db")
    charger_entrepot(villes, meteo, hotels, propre)
    encombre = _url(tmp_path, "encombre.db")
    charger_entrepot(
        [{**v, "intrus": "x"} for v in villes],
        [{**m, "parasite": 1} for m in meteo],
        [{**h, "pays": "FR", "_rang_proximite": 1} for h in hotels],
        encombre,
    )
    assert _photographie(encombre) == _photographie(propre)
    colonnes = _photographie(encombre)["colonnes:hotels"]
    assert "pays" not in colonnes
    assert "_rang_proximite" not in colonnes


def test_relance_avec_les_memes_donnees_laisse_l_entrepot_inchange(tmp_path):
    """Charger deux fois les mêmes données donne le même entrepôt : pas de doublon, pas d'erreur
    de clé, pas de vue en double (le chargement est idempotent)."""
    url = _url(tmp_path)
    premier = charger_entrepot(*_jeu(), url)
    apres_premier = _photographie(url)
    second = charger_entrepot(*_jeu(), url)
    assert apres_premier["lignes:hotels"]
    assert second == premier
    assert _photographie(url) == apres_premier


def test_chargement_ne_modifie_pas_les_lignes_recues(tmp_path):
    """Les dictionnaires reçus (avec leurs clés en trop) sont lus, jamais modifiés."""
    villes, meteo, hotels = _jeu()
    hotels = [{**h, "intrus": [1, 2]} for h in hotels]
    avant = copy.deepcopy((villes, meteo, hotels))
    charger_entrepot(villes, meteo, hotels, _url(tmp_path))
    assert (villes, meteo, hotels) == avant


def test_tables_etrangeres_restent_intactes_apres_chargement_et_apres_echec(tmp_path):
    """Seules les tables de l'ETL sont supprimées : une table qui n'est pas la sienne, dans la
    même base, garde ses lignes après un chargement réussi puis après un chargement en échec."""
    url = _url(tmp_path)
    moteur = create_engine(url)
    with moteur.begin() as connexion:
        connexion.execute(text("CREATE TABLE notes_perso (id INTEGER, texte TEXT)"))
        connexion.execute(text("INSERT INTO notes_perso VALUES (1, 'à garder')"))
    moteur.dispose()

    def notes() -> list[tuple[int, str]]:
        with create_engine(url).connect() as connexion:
            return [tuple(r) for r in connexion.execute(text("SELECT * FROM notes_perso"))]

    villes, meteo, hotels = _jeu()
    charger_entrepot(villes, meteo, hotels, url)
    assert notes() == [(1, "à garder")]
    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed: hotels\.hotel_id"):
        charger_entrepot(villes, meteo, [*hotels, _hotel(100, 2)], url)
    assert notes() == [(1, "à garder")]


def test_taille_de_lot_en_texte_est_refusee_et_l_entrepot_reste_inchange(tmp_path):
    """Une taille de lot « 2 » (texte) fait échouer le premier lot, donc toute la transaction :
    l'entrepôt déjà chargé garde son contenu."""
    url = _url(tmp_path)
    charger_entrepot(*_jeu(), url)
    avant = _photographie(url)
    with pytest.raises(TypeError, match="not supported between instances of 'int' and 'str'"):
        charger_entrepot(*_jeu_different(), url, taille_lot="2")
    assert _photographie(url) == avant


def test_url_de_base_absente_est_refusee():
    """Sans URL de connexion (`None`), SQLAlchemy lève ArgumentError : aucune base n'est ouverte."""
    with pytest.raises(ArgumentError, match="Expected string or URL object, got None"):
        charger_entrepot(*_jeu(), None)


def test_base_dans_un_dossier_inexistant_est_refusee(tmp_path):
    """SQLite ne crée pas les dossiers manquants : l'erreur d'ouverture est propagée, aucun
    fichier de base n'apparaît."""
    url = _url(tmp_path / "dossier_absent")
    with pytest.raises(OperationalError, match="unable to open database file"):
        charger_entrepot(*_jeu(), url)
    assert not (tmp_path / "dossier_absent").exists()


# --- _creer_moteur ------------------------------------------------------------------------------


def test_moteur_sqlite_ouvre_chaque_transaction_par_un_begin_explicite(tmp_path):
    """Sous SQLite, la connexion n'est plus en mode implicite (`isolation_level` à None) et la
    transaction est ouverte par un BEGIN dès l'entrée dans `begin()` ; un moteur SQLAlchemy
    ordinaire, lui, garde le mode implicite et n'a encore rien ouvert."""
    url = _url(tmp_path)
    with _creer_moteur(url).begin() as connexion:
        brute = connexion.connection.dbapi_connection
        assert brute.isolation_level is None
        assert brute.in_transaction is True
    with create_engine(url).begin() as connexion:
        ordinaire = connexion.connection.dbapi_connection
        assert ordinaire.isolation_level == ""
        assert ordinaire.in_transaction is False


def test_moteur_sqlite_annule_aussi_les_creations_de_tables(tmp_path):
    """Grâce au BEGIN explicite, un CREATE TABLE suivi d'une erreur est annulé comme le reste :
    la base ne garde aucune table."""
    moteur = _creer_moteur(_url(tmp_path))
    with pytest.raises(RuntimeError, match="échec voulu"):
        with moteur.begin() as connexion:
            connexion.exec_driver_sql("CREATE TABLE essai (x INTEGER)")
            raise RuntimeError("échec voulu")
    assert inspect(moteur).get_table_names() == []


def test_moteur_d_un_autre_dialecte_est_rendu_inchange_sans_ecouteur(monkeypatch):
    """PostgreSQL est déjà transactionnel pour le schéma : le moteur créé avec l'URL donnée est
    rendu tel quel, et aucun écouteur d'événement n'est posé dessus."""
    moteur_factice = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    copie = copy.deepcopy(moteur_factice)
    urls: list[str] = []
    ecouteurs: list[tuple[Any, str]] = []

    def faux_create_engine(url):
        urls.append(url)
        return moteur_factice

    monkeypatch.setattr(spark_etl, "create_engine", faux_create_engine)
    monkeypatch.setattr(
        spark_etl,
        "event",
        SimpleNamespace(listens_for=lambda cible, nom: ecouteurs.append((cible, nom))),
    )

    moteur = _creer_moteur("postgresql://kayak@hote/entrepot")

    assert moteur is moteur_factice
    assert moteur_factice == copie
    assert urls == ["postgresql://kayak@hote/entrepot"]
    assert ecouteurs == []


def test_moteur_refuse_une_url_mal_formee():
    """Une chaîne qui n'est pas une URL de base lève ArgumentError, elle n'ouvre rien."""
    with pytest.raises(ArgumentError, match="Could not parse SQLAlchemy URL"):
        _creer_moteur("ceci n'est pas une url")


def test_moteur_refuse_un_dialecte_inconnu():
    """Un dialecte que SQLAlchemy ne connaît pas lève NoSuchModuleError, avec son nom."""
    with pytest.raises(NoSuchModuleError, match="sqlalchemy.dialects:inconnu"):
        _creer_moteur("inconnu://hote/base")


# --- _lignes_pandas -----------------------------------------------------------------------------


def test_lignes_pandas_remplace_les_valeurs_manquantes_par_none_et_garde_les_types():
    """NaN, NaT et None deviennent None (NULL en SQL) ; les entiers restent des entiers, et 0,
    False et la chaîne vide ne sont pas pris pour des valeurs manquantes."""
    tableau = pd.DataFrame(
        {
            "entier": [1, 0],
            "decimal": [0.5, float("nan")],
            "texte": ["a", None],
            "vide": ["", "x"],
            "booleen": [False, True],
            "date": [pd.Timestamp("2026-10-10"), pd.NaT],
        }
    )
    lignes = list(_lignes_pandas(tableau))
    assert lignes == [
        {
            "entier": 1,
            "decimal": 0.5,
            "texte": "a",
            "vide": "",
            "booleen": False,
            "date": pd.Timestamp("2026-10-10"),
        },
        {
            "entier": 0,
            "decimal": None,
            "texte": None,
            "vide": "x",
            "booleen": True,
            "date": None,
        },
    ]
    assert type(lignes[0]["entier"]) is int
    assert lignes[1]["decimal"] is None
    assert lignes[1]["texte"] is None
    assert lignes[1]["date"] is None


@pytest.mark.parametrize(
    "tableau",
    [pd.DataFrame(), pd.DataFrame({"a": [], "b": []})],
    ids=["sans_colonne", "colonnes_sans_ligne"],
)
def test_lignes_pandas_dataframe_vide_ne_produit_aucune_ligne(tableau):
    """Un DataFrame sans ligne ne produit aucun enregistrement : le premier `next` s'arrête."""
    with pytest.raises(StopIteration):
        next(_lignes_pandas(tableau))


def test_lignes_pandas_ne_modifie_pas_le_dataframe_source():
    """La conversion travaille sur une copie : le NaN du DataFrame d'origine reste un NaN."""
    tableau = pd.DataFrame({"a": [1.0, float("nan")], "b": ["x", None]})
    avant = tableau.copy(deep=True)
    list(_lignes_pandas(tableau))
    pd.testing.assert_frame_equal(tableau, avant)
    assert tableau["a"].isna().tolist() == [False, True]


def test_lignes_pandas_refuse_un_objet_qui_n_est_pas_un_dataframe():
    """La fonction est un générateur : l'erreur n'apparaît qu'à la première lecture, et elle
    signale l'objet invalide au lieu de rendre un flux vide."""
    flux = _lignes_pandas(None)
    with pytest.raises(AttributeError, match="astype"):
        next(flux)


# --- _lignes_spark ------------------------------------------------------------------------------


class _FauxFlux:
    """Imite ``DataFrame.toLocalIterator`` : rend les lignes (objets ``Row``) une à une."""

    def __init__(self, lignes: list[Any]) -> None:
        self.lignes = lignes

    def toLocalIterator(self) -> Any:
        return iter(self.lignes)


def test_lignes_spark_remplace_les_nan_flottants_par_none_et_garde_le_reste():
    """Un NaN flottant devient None (NULL en SQL, y compris sous PostgreSQL, qui stockerait
    sinon la valeur « NaN ») ; les autres valeurs, entiers et textes compris, restent telles
    quelles."""
    from pyspark.sql import Row

    flux = _FauxFlux([Row(a=1.5, b=float("nan"), c=None, d="x", e=3), Row(a=float("nan"), b=2.0)])
    lignes = list(_lignes_spark(flux))
    assert lignes == [
        {"a": 1.5, "b": None, "c": None, "d": "x", "e": 3},
        {"a": None, "b": 2.0},
    ]


# --- _hadoop_natif_disponible -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("systeme", "hadoop_home", "attendu"),
    [
        ("posix", None, True),
        ("posix", "/opt/hadoop", True),
        ("nt", None, False),
        ("nt", "", False),
        ("nt", "hadoop", True),
    ],
    ids=["linux", "linux_avec_home", "windows_sans_home", "windows_home_vide", "windows_avec_home"],
)
def test_hadoop_natif_depend_du_systeme_et_de_hadoop_home(
    monkeypatch, systeme, hadoop_home, attendu
):
    """L'écriture native de Spark est possible hors Windows, ou sous Windows avec HADOOP_HOME
    NON VIDE (winutils) ; la réponse est un vrai booléen."""
    environnement = {} if hadoop_home is None else {"HADOOP_HOME": hadoop_home}
    monkeypatch.setattr(spark_etl, "os", SimpleNamespace(name=systeme, environ=environnement))
    assert _hadoop_natif_disponible() is attendu


def test_hadoop_natif_ne_modifie_pas_l_environnement(monkeypatch):
    """La détection lit l'environnement sans jamais y écrire (ni HADOOP_HOME, ni autre)."""
    environnement = {"HADOOP_HOME": "hadoop", "PATH": "x"}
    avant = dict(environnement)
    monkeypatch.setattr(spark_etl, "os", SimpleNamespace(name="nt", environ=environnement))
    _hadoop_natif_disponible()
    assert environnement == avant


# --- obtenir_spark ------------------------------------------------------------------------------


class FauxConstructeur:
    """Imite `SparkSession.builder` : note chaque appel et l'environnement Java au démarrage."""

    def __init__(self, session: Any, erreur: Exception | None = None) -> None:
        self.session = session
        self.erreur = erreur
        self.appels: list[tuple[Any, ...]] = []
        self.environnement: dict[str, str | None] = {}

    def master(self, valeur: str) -> "FauxConstructeur":
        self.appels.append(("master", valeur))
        return self

    def appName(self, nom: str) -> "FauxConstructeur":
        self.appels.append(("appName", nom))
        return self

    def config(self, cle: str, valeur: str) -> "FauxConstructeur":
        self.appels.append(("config", cle, valeur))
        return self

    def getOrCreate(self) -> Any:
        self.appels.append(("getOrCreate",))
        self.environnement = {nom: os.environ.get(nom) for nom in VARIABLES_JAVA}
        if self.erreur is not None:
            raise self.erreur
        return self.session


def _faux_spark(monkeypatch, erreur: Exception | None = None):
    """Remplace `SparkSession` par une fausse classe ; rend (fausse session, faux constructeur)."""
    niveaux: list[str] = []
    session = SimpleNamespace(
        niveaux=niveaux, sparkContext=SimpleNamespace(setLogLevel=niveaux.append)
    )
    constructeur = FauxConstructeur(session, erreur)
    monkeypatch.setattr(spark_etl, "SparkSession", SimpleNamespace(builder=constructeur))
    return session, constructeur


def _isoler_environnement(monkeypatch) -> None:
    """Retire les variables que `obtenir_spark` écrit ; pytest les restaure à la fin du test."""
    for nom in (*VARIABLES_JAVA, "DATABRICKS_RUNTIME_VERSION"):
        monkeypatch.setenv(nom, "sentinelle")
        monkeypatch.delenv(nom)


@pytest.mark.parametrize("version", ["", "14.3.x-scala2.12"], ids=["vide", "renseignee"])
def test_obtenir_spark_sur_databricks_ne_regle_rien_et_laisse_l_environnement_inchange(
    monkeypatch, version
):
    """Sur Databricks (variable PRÉSENTE, même vide), la session existante est reprise telle
    quelle : aucun réglage, pas de niveau de journal, pas de Java importé, aucune variable
    Java ou Python écrite."""
    session, constructeur = _faux_spark(monkeypatch)
    _isoler_environnement(monkeypatch)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", version)
    monkeypatch.setitem(sys.modules, "jdk4py", None)

    assert obtenir_spark() is session

    assert constructeur.appels == [("getOrCreate",)]
    assert session.niveaux == []
    assert [nom in os.environ for nom in VARIABLES_JAVA] == [False, False, False]


@pytest.mark.parametrize(
    "voisines",
    [{}, {"DATABRICKS": "1", "DATABRICKS_RUNTIME": "1", "DATABRICKS_RUNTIME_VERSIONS": "1"}],
    ids=["aucune", "variables_voisines"],
)
def test_obtenir_spark_en_local_regle_la_session_et_l_environnement_avant_le_demarrage(
    monkeypatch, voisines
):
    """Hors Databricks (une variable aux noms voisins ne compte pas) : master local, nom de
    l'application, quatre réglages (booléens, ou nombre en chaîne), niveau de journal ERROR, et
    variables Java et Python déjà écrites au moment où la session démarre."""
    session, constructeur = _faux_spark(monkeypatch)
    _isoler_environnement(monkeypatch)
    for nom, valeur in voisines.items():
        monkeypatch.setenv(nom, valeur)
    java = Path("faux") / "java"
    monkeypatch.setitem(sys.modules, "jdk4py", SimpleNamespace(JAVA_HOME=java))

    assert obtenir_spark() is session

    assert constructeur.appels == [
        ("master", "local[*]"),
        ("appName", "KayakETL"),
        ("config", "spark.ui.enabled", False),
        ("config", "spark.sql.shuffle.partitions", "8"),
        ("config", "spark.sql.caseSensitive", True),
        ("config", "spark.sql.execution.arrow.pyspark.enabled", True),
        ("getOrCreate",),
    ]
    assert session.niveaux == ["ERROR"]
    assert constructeur.environnement == {
        "JAVA_HOME": str(java),
        "PYSPARK_PYTHON": sys.executable,
        "PYSPARK_DRIVER_PYTHON": sys.executable,
    }


def test_obtenir_spark_sans_jdk4py_echoue_avant_tout_reglage(monkeypatch):
    """Sans le paquet qui fournit Java, l'ImportError sort telle quelle : le constructeur Spark
    n'est pas appelé et l'environnement reste inchangé."""
    _, constructeur = _faux_spark(monkeypatch)
    _isoler_environnement(monkeypatch)
    monkeypatch.setitem(sys.modules, "jdk4py", None)
    with pytest.raises(ImportError, match="jdk4py"):
        obtenir_spark()
    assert constructeur.appels == []
    assert [nom in os.environ for nom in VARIABLES_JAVA] == [False, False, False]


def test_obtenir_spark_propage_l_echec_du_demarrage_sans_regler_le_journal(monkeypatch):
    """Si Spark ne démarre pas (Java introuvable...), l'erreur sort telle quelle et on ne tente
    pas de régler le niveau de journal d'une session qui n'existe pas."""
    session, _ = _faux_spark(monkeypatch, RuntimeError("Java introuvable"))
    _isoler_environnement(monkeypatch)
    monkeypatch.setitem(sys.modules, "jdk4py", SimpleNamespace(JAVA_HOME=Path("faux")))
    with pytest.raises(RuntimeError, match="Java introuvable"):
        obtenir_spark()
    assert session.niveaux == []


def test_obtenir_spark_sur_databricks_propage_l_echec_sans_toucher_a_l_environnement(
    monkeypatch,
):
    """Sur Databricks, si la session existante est indisponible, l'erreur sort telle quelle ;
    aucun repli local n'est tenté et aucune variable Java ou Python n'est écrite."""
    _, constructeur = _faux_spark(monkeypatch, RuntimeError("session indisponible"))
    _isoler_environnement(monkeypatch)
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "14.3")
    with pytest.raises(RuntimeError, match="session indisponible"):
        obtenir_spark()
    assert constructeur.appels == [("getOrCreate",)]
    assert [nom in os.environ for nom in VARIABLES_JAVA] == [False, False, False]


# --- _dossier_de_travail ------------------------------------------------------------------------


def test_dossier_de_travail_vit_sous_tmp_du_lake_puis_disparait_avec_lui(tmp_path):
    """Le dossier est créé vide sous `.tmp` du lake ; à la sortie il est supprimé avec son
    contenu, et `.tmp`, devenu vide, est retiré lui aussi."""
    lake = _lake(tmp_path)
    tmp = tmp_path / "lake" / ".tmp"
    assert not tmp.exists()
    with _dossier_de_travail(lake) as travail:
        assert isinstance(travail, Path)
        assert travail.is_dir()
        assert travail.parent == lake.dossier_temporaire()
        assert list(travail.iterdir()) == []
        (travail / "partiel.txt").write_text("brouillon", encoding="utf-8")
    assert not travail.exists()
    assert not tmp.exists()


def test_dossier_de_travail_conserve_un_dossier_tmp_qui_contient_autre_chose_intact(tmp_path):
    """Si `.tmp` abrite le fichier d'un autre usage, il n'est pas supprimé et son fichier est
    intact ; seul le dossier de travail disparaît."""
    lake = _lake(tmp_path)
    voisin = lake.dossier_temporaire() / "voisin.txt"
    voisin.write_text("à garder", encoding="utf-8")
    with _dossier_de_travail(lake) as travail:
        pass
    assert not travail.exists()
    assert voisin.read_text(encoding="utf-8") == "à garder"
    assert voisin.parent == lake.dossier_temporaire()


def test_dossier_de_travail_refuse_un_lake_absent():
    """Sans lake (`None`), il n'y a pas de dossier temporaire à créer : AttributeError."""
    with pytest.raises(AttributeError, match="dossier_temporaire"):
        with _dossier_de_travail(None):
            pass


def test_dossier_de_travail_laisse_intact_le_dossier_oublie_par_une_execution_precedente(
    tmp_path,
):
    """Un dossier de travail resté là après un arrêt brutal n'est pas celui de l'exécution en
    cours : il n'est ni supprimé ni modifié, et `.tmp` reste donc en place."""
    lake = _lake(tmp_path)
    oublie = lake.dossier_temporaire() / "tmp_oublie"
    oublie.mkdir()
    (oublie / "partiel.parquet").write_bytes(b"PAR1")
    with _dossier_de_travail(lake) as travail:
        assert travail != oublie
    assert not travail.exists()
    assert (oublie / "partiel.parquet").read_bytes() == b"PAR1"


def test_dossier_de_travail_est_supprime_meme_en_cas_d_erreur(tmp_path):
    """Une exception dans le bloc sort telle quelle, et le dossier de travail n'est pas laissé."""
    lake = _lake(tmp_path)
    cree: list[Path] = []
    with pytest.raises(RuntimeError, match="écriture impossible"):
        with _dossier_de_travail(lake) as travail:
            cree.append(travail)
            (travail / "partiel.txt").write_text("brouillon", encoding="utf-8")
            raise RuntimeError("écriture impossible")
    assert cree
    assert not cree[0].exists()


# --- ecrire_csv et ecrire_parquet : faux DataFrame ----------------------------------------------


class FauxEcrivain:
    """Imite `df.write` : note la chaîne d'appels et dépose les fichiers que Spark écrirait."""

    def __init__(self, df: "FauxDataFrame") -> None:
        self.df = df

    def mode(self, mode: str) -> "FauxEcrivain":
        self.df.journal.append(("mode", mode))
        return self

    def option(self, cle: str, valeur: str) -> "FauxEcrivain":
        self.df.journal.append(("option", cle, valeur))
        return self

    def parquet(self, uri: str) -> None:
        self.deposer("parquet", uri)

    def csv(self, uri: str) -> None:
        self.deposer("csv", uri)

    def deposer(self, format_: str, uri: str) -> None:
        self.df.journal.append((format_, uri))
        if self.df.erreur is not None:
            raise self.df.erreur
        dossier = Path(url2pathname(urlparse(uri).path))
        for nom, contenu in self.df.fichiers.items():
            cible = dossier / nom
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_bytes(contenu)


class FauxSortie:
    """Résultat de `mapInArrow` : paresseux, comme Spark, l'exécution a lieu au `count()`."""

    def __init__(self, df: "FauxDataFrame", fonction: Any) -> None:
        self.df = df
        self.fonction = fonction

    def count(self) -> int:
        self.df.journal.append(("count",))
        total = 0
        for numero, lots in enumerate(self.df.partitions):
            self.df.contexte.numero = numero
            sorties = [lot.to_pylist() for lot in self.fonction(iter(lots))]
            self.df.sorties.extend(sorties)
            total += sum(len(sortie) for sortie in sorties)
        return total


class FauxDataFrame:
    """Imite le DataFrame Spark vu par `ecrire_csv` et `ecrire_parquet`, sans Spark.

    `coalesce` rend un NOUVEAU faux DataFrame qui partage le journal : on voit ainsi si
    l'écriture s'est faite sur le DataFrame réduit ou sur l'original. `fichiers` est ce que
    l'écriture native dépose dans le dossier demandé ; `partitions` (listes de lots Arrow)
    alimente la fonction passée à `mapInArrow`, une partition après l'autre.
    """

    def __init__(
        self,
        fichiers: dict[str, bytes] | None = None,
        partitions: list[list[pa.RecordBatch]] | None = None,
        contexte: Any = None,
        erreur: Exception | None = None,
    ) -> None:
        self.fichiers = fichiers if fichiers is not None else {}
        self.partitions = partitions if partitions is not None else []
        self.contexte = contexte
        self.erreur = erreur
        self.coalesce_demande: int | None = None
        self.journal: list[tuple[Any, ...]] = []
        self.sorties: list[list[dict[str, Any]]] = []

    def coalesce(self, nombre: int) -> "FauxDataFrame":
        self.journal.append(("coalesce", nombre))
        reduit = copy.copy(self)  # copie superficielle : le journal et les sorties sont partagés
        reduit.coalesce_demande = nombre
        return reduit

    @property
    def write(self) -> FauxEcrivain:
        self.journal.append(("write", self.coalesce_demande))
        return FauxEcrivain(self)

    def mapInArrow(self, fonction: Any, schema: str) -> FauxSortie:
        self.journal.append(("mapInArrow", self.coalesce_demande, schema))
        return FauxSortie(self, fonction)


@pytest.fixture
def contexte_tache(monkeypatch):
    """Faux `TaskContext` : `partitionId()` rend le numéro de la partition en cours d'exécution
    (c'est ce que Spark fournit dans un exécuteur)."""
    contexte = SimpleNamespace(numero=0)
    contexte.partitionId = lambda: contexte.numero
    monkeypatch.setattr(pyspark.TaskContext, "get", staticmethod(lambda: contexte))
    return contexte


def _natif(monkeypatch, disponible: bool) -> None:
    """Force le choix entre l'écriture native de Spark et le repli par les exécuteurs."""
    monkeypatch.setattr(spark_etl, "_hadoop_natif_disponible", lambda: disponible)


def _lot(*lignes: dict[str, Any]) -> pa.RecordBatch:
    """Un lot de lignes au format Arrow, tel que le reçoit une partition."""
    return pa.RecordBatch.from_pylist(list(lignes))


HOTELS = [
    {"nom": 'Hôtel "Le Clos", Nice', "note": 9.1, "etoiles": 4},
    {"nom": "Auberge", "note": None, "etoiles": None},
]
SORTIE_NATIVE_CSV = {
    "part-00000-abc.csv": b'"nom","note"\r\n"Hotel ""A""",9.1\r\n',
    "_SUCCESS": b"",
    ".part-00000-abc.csv.crc": b"crc",
    "._SUCCESS.crc": b"crc",
}


def test_ecrire_csv_natif_publie_le_fichier_part_et_ignore_les_fichiers_hadoop(
    tmp_path, monkeypatch
):
    """Avec Hadoop, le DataFrame est réduit à UN fichier (`coalesce(1)`), écrit avec en-tête et
    guillemets doublés dans un dossier `table.csv` ; seul le fichier `part-*.csv` est copié tel
    quel (octets exacts) dans le lake, et le chemin logique est renvoyé."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    df = FauxDataFrame(fichiers=SORTIE_NATIVE_CSV)

    chemin = ecrire_csv(df, lake, CHEMIN_CSV)

    assert chemin == CHEMIN_CSV
    assert lake.lire(CHEMIN_CSV) == SORTIE_NATIVE_CSV["part-00000-abc.csv"]
    *debut, ecriture = df.journal
    assert debut == [
        ("coalesce", 1),
        ("write", 1),
        ("mode", "overwrite"),
        ("option", "header", True),
        ("option", "quote", '"'),
        ("option", "escape", '"'),
    ]
    assert ecriture[0] == "csv"
    uri = urlparse(ecriture[1])
    assert uri.scheme == "file"
    assert uri.path.endswith("/table.csv")
    assert "/lake/.tmp/" in uri.path
    assert _fichiers_du_lake(lake) == {CHEMIN_CSV: SORTIE_NATIVE_CSV["part-00000-abc.csv"]}
    assert not (tmp_path / "lake" / ".tmp").exists()


@pytest.mark.parametrize(
    "fichiers",
    [
        {},
        {"_SUCCESS": b""},
        {"autre.csv": b"a,b\r\n"},
        {"part-00000.parquet": b"PAR1"},
        {".part-00000.csv.crc": b"crc"},
        {"sous_dossier/part-00000.csv": b"a,b\r\n"},
    ],
    ids=[
        "dossier_vide",
        "succes_seul",
        "nom_inattendu",
        "parquet",
        "somme_de_controle",
        "imbrique",
    ],
)
def test_ecrire_csv_sans_fichier_part_leve_une_erreur_et_laisse_le_lake_intact(
    tmp_path, monkeypatch, fichiers
):
    """Zéro fichier `part-*.csv` au premier niveau du dossier (vide, métadonnées seules, autre
    nom, autre format, sous-dossier) : RuntimeError, et ni le fichier du lake ni rien d'autre ne
    change."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    lake.ecrire(CHEMIN_CSV, b"ancien contenu")
    avant = _fichiers_du_lake(lake)
    with pytest.raises(RuntimeError, match="un seul fichier CSV était attendu, trouvé : 0"):
        ecrire_csv(FauxDataFrame(fichiers=fichiers), lake, CHEMIN_CSV)
    assert _fichiers_du_lake(lake) == avant


def test_ecrire_csv_avec_deux_fichiers_part_leve_une_erreur_et_laisse_le_lake_intact(
    tmp_path, monkeypatch
):
    """Deux fichiers `part-*.csv` (le `coalesce` n'a pas suffi) : RuntimeError au lieu d'en
    publier un au hasard ; le lake est intact, aucun résidu de travail ne reste."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    lake.ecrire(CHEMIN_CSV, b"ancien contenu")
    avant = _fichiers_du_lake(lake)
    df = FauxDataFrame(fichiers={"part-00000.csv": b"a\r\n1\r\n", "part-00001.csv": b"2\r\n"})
    with pytest.raises(RuntimeError, match="un seul fichier CSV était attendu, trouvé : 2"):
        ecrire_csv(df, lake, CHEMIN_CSV)
    assert _fichiers_du_lake(lake) == avant
    assert not list((tmp_path / "lake" / ".tmp").glob("*"))


def test_ecrire_csv_propage_un_echec_d_ecriture_et_laisse_le_lake_intact(tmp_path, monkeypatch):
    """Si Spark échoue (disque plein...), l'erreur sort telle quelle, le CSV précédent du lake
    est intact et le dossier de travail est supprimé."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    lake.ecrire(CHEMIN_CSV, b"ancien contenu")
    avant = _fichiers_du_lake(lake)
    with pytest.raises(RuntimeError, match="disque plein"):
        ecrire_csv(FauxDataFrame(erreur=RuntimeError("disque plein")), lake, CHEMIN_CSV)
    assert _fichiers_du_lake(lake) == avant
    assert not list((tmp_path / "lake" / ".tmp").glob("*"))


def test_ecrire_csv_refuse_un_dataframe_absent_sans_rien_ecrire(tmp_path, monkeypatch):
    """`None` à la place du DataFrame lève AttributeError avant toute écriture : le lake reste
    vide et aucun dossier de travail n'est créé."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    with pytest.raises(AttributeError, match="coalesce"):
        ecrire_csv(None, lake, CHEMIN_CSV)
    assert _fichiers_du_lake(lake) == {}
    assert not (tmp_path / "lake" / ".tmp").exists()


def test_ecriture_par_les_executeurs_hors_tache_spark_est_refusee_sans_rien_ecrire(
    tmp_path, monkeypatch
):
    """Hors d'une tâche Spark (sur le programme principal), `TaskContext.get()` rend None : la
    fonction d'écriture lève AttributeError et ne dépose aucun fichier."""
    monkeypatch.setattr(pyspark.TaskContext, "get", staticmethod(lambda: None))
    sortie = tmp_path / "table.parquet"
    df = FauxDataFrame(partitions=[[_lot(*HOTELS)]], contexte=SimpleNamespace(numero=0))
    with pytest.raises(AttributeError, match="partitionId"):
        _ecrire_executeurs(df, sortie, "parquet")
    assert list(sortie.iterdir()) == []


@pytest.fixture(autouse=True)
def _schema_csv_verifie(monkeypatch):
    """Garde-fou de test : ``pyarrow.csv.CSVWriter`` refuse ici un schéma qui n'en est pas un.

    Avec un schéma absent (None), pyarrow 25 s'arrête sur une erreur de segmentation au lieu de
    lever une exception : tout pytest s'arrêterait sans dire quel test a échoué. Le garde-fou
    lève une TypeError à la place ; un appel correct passe à l'identique au vrai ``CSVWriter``.
    """
    vrai_csvwriter = pacsv.CSVWriter

    def csvwriter_verifie(destination: Any, schema: Any, *args: Any, **options: Any) -> Any:
        if not isinstance(schema, pa.Schema):
            raise TypeError(f"schéma Arrow attendu, reçu {schema!r}")
        return vrai_csvwriter(destination, schema, *args, **options)

    monkeypatch.setattr(pacsv, "CSVWriter", csvwriter_verifie)


@pytest.mark.parametrize("format_", ["csv", "parquet"])
def test_ecriture_par_les_executeurs_compte_les_lignes_de_tous_les_lots_d_une_partition(
    tmp_path, contexte_tache, format_
):
    """Une partition reçue en plusieurs lots Arrow : le compte rendu additionne leurs lignes, et
    le fichier écrit les contient toutes, dans l'ordre. Comme dans Spark, les lots d'une
    partition partagent le même schéma."""
    sortie = tmp_path / f"table.{format_}"
    premier = _lot(HOTELS[0])
    second = pa.RecordBatch.from_pylist([HOTELS[1]], schema=premier.schema)
    df = FauxDataFrame(partitions=[[premier, second]], contexte=contexte_tache)
    _ecrire_executeurs(df, sortie, format_)
    assert df.sorties == [[{"lignes": 2}]]
    fichier = sortie / f"part-00000.{format_}"
    relu = pd.read_csv(fichier) if format_ == "csv" else pq.read_table(fichier).to_pandas()
    assert relu["nom"].tolist() == [HOTELS[0]["nom"], HOTELS[1]["nom"]]


def test_ecriture_csv_par_les_executeurs_refuse_un_schema_absent(tmp_path):
    """Le garde-fou ci-dessus refuse bien un schéma absent, avant toute écriture."""
    with pytest.raises(TypeError, match="schéma Arrow attendu"):
        pacsv.CSVWriter(str(tmp_path / "x.csv"), None)
    assert list(tmp_path.iterdir()) == []


def test_ecriture_par_les_executeurs_cree_le_dossier_de_sortie_et_ses_parents(
    tmp_path, contexte_tache
):
    """Le dossier de sortie est créé avec ses parents manquants, et chaque partition y dépose un
    fichier nommé d'après son numéro."""
    sortie = tmp_path / "niveau1" / "niveau2" / "table.parquet"
    df = FauxDataFrame(partitions=[[_lot(*HOTELS)]], contexte=contexte_tache)
    _ecrire_executeurs(df, sortie, "parquet")
    assert [fichier.name for fichier in sortie.iterdir()] == ["part-00000.parquet"]


def test_ecriture_par_les_executeurs_dans_un_dossier_existant_laisse_les_voisins_intacts(
    tmp_path, contexte_tache
):
    """Un dossier de sortie qui existe déjà est accepté (pas d'erreur) et les fichiers qu'il
    contient déjà sont intacts : seul le fichier de la partition est ajouté."""
    sortie = tmp_path / "table.csv"
    sortie.mkdir()
    (sortie / "voisin.txt").write_text("à garder", encoding="utf-8")
    df = FauxDataFrame(partitions=[[_lot(*HOTELS)]], contexte=contexte_tache)
    _ecrire_executeurs(df, sortie, "csv")
    assert sorted(fichier.name for fichier in sortie.iterdir()) == ["part-00000.csv", "voisin.txt"]
    assert (sortie / "voisin.txt").read_text(encoding="utf-8") == "à garder"


def test_ecrire_csv_de_repli_passe_par_les_executeurs_et_publie_un_csv_standard(
    tmp_path, monkeypatch, contexte_tache
):
    """Sans Hadoop (Windows), l'écriture native n'est jamais employée : `mapInArrow` écrit, au
    `count()`, un fichier par partition NON VIDE (`part-00000.csv`). Le CSV publié a son en-tête,
    ses guillemets doublés, ses valeurs manquantes vides, et se relit à l'identique."""
    _natif(monkeypatch, False)
    lake = _lake(tmp_path)
    df = FauxDataFrame(partitions=[[_lot(*HOTELS)], []], contexte=contexte_tache)

    assert ecrire_csv(df, lake, CHEMIN_CSV) == CHEMIN_CSV

    assert df.journal == [
        ("coalesce", 1),
        ("mapInArrow", 1, "lignes long"),
        ("count",),
    ]
    assert df.sorties == [[{"lignes": 2}], [{"lignes": 0}]]
    octets = lake.lire(CHEMIN_CSV)
    texte = octets.decode("utf-8")
    lignes = texte.splitlines()
    assert lignes[0].replace('"', "") == "nom,note,etoiles"
    assert '""Le Clos""' in texte
    assert lignes[-1].replace('"', "") == "Auberge,,"
    lu = pd.read_csv(io.BytesIO(octets))
    attendu = pd.DataFrame(
        {
            "nom": ['Hôtel "Le Clos", Nice', "Auberge"],
            "note": [9.1, float("nan")],
            "etoiles": [4.0, float("nan")],
        }
    )
    pd.testing.assert_frame_equal(lu, attendu)


def test_ecrire_csv_de_repli_avec_deux_partitions_non_vides_est_refuse(
    tmp_path, monkeypatch, contexte_tache
):
    """Deux partitions non vides donnent deux fichiers (`part-00000.csv` et `part-00001.csv`) :
    le contrôle « un seul fichier » les refuse et rien n'est publié."""
    _natif(monkeypatch, False)
    lake = _lake(tmp_path)
    df = FauxDataFrame(partitions=[[_lot(*HOTELS)], [_lot(*HOTELS)]], contexte=contexte_tache)
    with pytest.raises(RuntimeError, match="un seul fichier CSV était attendu, trouvé : 2"):
        ecrire_csv(df, lake, CHEMIN_CSV)
    assert _fichiers_du_lake(lake) == {}


def test_ecrire_csv_de_repli_sans_aucune_ligne_est_refuse(tmp_path, monkeypatch, contexte_tache):
    """Si toutes les partitions sont vides, aucun fichier n'est créé : RuntimeError (« trouvé :
    0 ») au lieu de publier un CSV absent ou vide."""
    _natif(monkeypatch, False)
    lake = _lake(tmp_path)
    df = FauxDataFrame(partitions=[[], []], contexte=contexte_tache)
    with pytest.raises(RuntimeError, match="un seul fichier CSV était attendu, trouvé : 0"):
        ecrire_csv(df, lake, CHEMIN_CSV)
    assert not lake.existe(CHEMIN_CSV)
    assert df.sorties == [[{"lignes": 0}], [{"lignes": 0}]]


SORTIE_NATIVE_PARQUET = {
    "part-00000-abc.snappy.parquet": b"PAR1 contenu PAR1",
    "_SUCCESS": b"",
    ".part-00000-abc.snappy.parquet.crc": b"crc",
    "._SUCCESS.crc": b"crc",
}


def test_ecrire_parquet_natif_publie_le_jeu_de_fichiers_et_remplace_l_ancien(tmp_path, monkeypatch):
    """Avec Hadoop, le DataFrame est réduit à `FICHIERS_PARQUET` fichier (`coalesce(1)`) puis écrit
    en mode « overwrite » dans `table.parquet` ; les fichiers cachés et les sommes de contrôle
    `.crc` ne sont pas publiés, et les fichiers d'un ancien jeu disparaissent."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    lake.ecrire(f"{CHEMIN_PARQUET}/part-00007-ancien.parquet", b"ancien")
    df = FauxDataFrame(fichiers=SORTIE_NATIVE_PARQUET)

    publies = ecrire_parquet(df, lake, CHEMIN_PARQUET)

    attendu = {
        f"{CHEMIN_PARQUET}/_SUCCESS": b"",
        f"{CHEMIN_PARQUET}/part-00000-abc.snappy.parquet": b"PAR1 contenu PAR1",
    }
    assert isinstance(publies, list)
    assert sorted(publies) == sorted(attendu)
    assert _fichiers_du_lake(lake) == attendu
    *debut, ecriture = df.journal
    assert debut == [("coalesce", spark_etl.FICHIERS_PARQUET), ("write", 1), ("mode", "overwrite")]
    assert ecriture[0] == "parquet"
    assert urlparse(ecriture[1]).path.endswith("/table.parquet")
    assert not (tmp_path / "lake" / ".tmp").exists()


def test_ecrire_parquet_propage_un_echec_d_ecriture_et_laisse_le_lake_intact(tmp_path, monkeypatch):
    """Si Spark échoue, l'erreur sort telle quelle : l'ancien jeu de fichiers du lake est intact
    (rien n'est remplacé à moitié) et le dossier de travail est supprimé."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    lake.ecrire(f"{CHEMIN_PARQUET}/part-00000-ancien.parquet", b"ancien")
    avant = _fichiers_du_lake(lake)
    with pytest.raises(RuntimeError, match="disque plein"):
        ecrire_parquet(FauxDataFrame(erreur=RuntimeError("disque plein")), lake, CHEMIN_PARQUET)
    assert _fichiers_du_lake(lake) == avant
    assert not list((tmp_path / "lake" / ".tmp").glob("*"))


def test_ecrire_csv_refuse_un_lake_absent_sans_rien_ecrire(monkeypatch):
    """Sans lake (`None`), AttributeError : aucune écriture Spark n'est même tentée."""
    _natif(monkeypatch, True)
    df = FauxDataFrame(fichiers=SORTIE_NATIVE_CSV)
    with pytest.raises(AttributeError, match="dossier_temporaire"):
        ecrire_csv(df, None, CHEMIN_CSV)
    assert df.journal == [("coalesce", 1)]


def test_ecrire_parquet_refuse_un_lake_absent_sans_rien_ecrire(monkeypatch):
    """Sans lake (`None`), AttributeError : aucune écriture Spark n'est même tentée."""
    _natif(monkeypatch, True)
    df = FauxDataFrame(fichiers=SORTIE_NATIVE_PARQUET)
    with pytest.raises(AttributeError, match="dossier_temporaire"):
        ecrire_parquet(df, None, CHEMIN_PARQUET)
    assert df.journal == [("coalesce", 1)]


def test_ecrire_parquet_refuse_un_dataframe_absent_sans_rien_ecrire(tmp_path, monkeypatch):
    """`None` à la place du DataFrame lève AttributeError avant toute écriture : le lake reste
    vide et aucun dossier de travail n'est créé."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    with pytest.raises(AttributeError, match="coalesce"):
        ecrire_parquet(None, lake, CHEMIN_PARQUET)
    assert _fichiers_du_lake(lake) == {}
    assert not (tmp_path / "lake" / ".tmp").exists()


def test_ecrire_parquet_de_repli_ecrit_un_fichier_par_partition_non_vide(
    tmp_path, monkeypatch, contexte_tache
):
    """Sans Hadoop, l'écriture native n'est jamais employée ; `mapInArrow` écrit au `count()` un
    `part-NNNNN.parquet` (numéro de la partition sur 5 chiffres) pour chaque partition non vide :
    ici seule la partition 1 écrit, et les lignes se relisent à l'identique."""
    _natif(monkeypatch, False)
    lake = _lake(tmp_path)
    df = FauxDataFrame(partitions=[[], [_lot(*HOTELS)]], contexte=contexte_tache)

    publies = ecrire_parquet(df, lake, CHEMIN_PARQUET)

    fichier = f"{CHEMIN_PARQUET}/part-00001.parquet"
    assert publies == [fichier]
    assert df.journal == [
        ("coalesce", spark_etl.FICHIERS_PARQUET),
        ("mapInArrow", 1, "lignes long"),
        ("count",),
    ]
    assert df.sorties == [[{"lignes": 0}], [{"lignes": 2}]]
    assert pq.read_table(lake.chemin_local(fichier)).to_pylist() == HOTELS
    assert _fichiers_du_lake(lake).keys() == {fichier}


def test_ecrire_parquet_de_repli_sans_ligne_ne_cree_aucun_fichier(
    tmp_path, monkeypatch, contexte_tache
):
    """Des partitions toutes vides n'écrivent rien : aucun fichier n'est publié et le lake reste
    inchangé (vide)."""
    _natif(monkeypatch, False)
    lake = _lake(tmp_path)
    df = FauxDataFrame(partitions=[[], []], contexte=contexte_tache)
    assert ecrire_parquet(df, lake, CHEMIN_PARQUET) == []
    assert _fichiers_du_lake(lake) == {}


def test_ecrire_csv_laisse_les_autres_objets_du_lake_intacts(tmp_path, monkeypatch):
    """Écrire le CSV du jour ne touche qu'à son propre chemin : le CSV de la veille et un fichier
    d'une autre zone gardent leurs octets."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    voisins = {
        "curated/kayak_enrichi/date=2026-10-09/kayak_enrichi.csv": b"veille",
        "raw/geocodage/date=2026-10-10/coordonnees.csv": b"id_ville,ville\n1,Nice\n",
    }
    for chemin, contenu in voisins.items():
        lake.ecrire(chemin, contenu)
    ecrire_csv(FauxDataFrame(fichiers=SORTIE_NATIVE_CSV), lake, CHEMIN_CSV)
    assert {chemin: lake.lire(chemin) for chemin in voisins} == voisins


def test_ecrire_parquet_laisse_les_autres_objets_du_lake_intacts(tmp_path, monkeypatch):
    """Publier le jeu du jour ne touche qu'à son propre dossier : le jeu de la veille et un
    fichier d'une autre zone gardent leurs octets."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    voisins = {
        "curated/villes/date=2026-10-09/villes.parquet/part-00000.parquet": b"veille",
        "raw/geocodage/date=2026-10-10/coordonnees.csv": b"id_ville,ville\n1,Nice\n",
    }
    for chemin, contenu in voisins.items():
        lake.ecrire(chemin, contenu)
    ecrire_parquet(FauxDataFrame(fichiers=SORTIE_NATIVE_PARQUET), lake, CHEMIN_PARQUET)
    assert {chemin: lake.lire(chemin) for chemin in voisins} == voisins


def test_ecrire_csv_relance_avec_le_meme_dataframe_laisse_le_lake_inchange(tmp_path, monkeypatch):
    """Relancer l'écriture avec les mêmes données redonne exactement le même lake (le fichier est
    remplacé, pas ajouté)."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    ecrire_csv(FauxDataFrame(fichiers=SORTIE_NATIVE_CSV), lake, CHEMIN_CSV)
    apres_premiere_ecriture = _fichiers_du_lake(lake)
    ecrire_csv(FauxDataFrame(fichiers=SORTIE_NATIVE_CSV), lake, CHEMIN_CSV)
    assert apres_premiere_ecriture == {CHEMIN_CSV: SORTIE_NATIVE_CSV["part-00000-abc.csv"]}
    assert _fichiers_du_lake(lake) == apres_premiere_ecriture


def test_ecrire_parquet_relance_avec_le_meme_dataframe_laisse_le_lake_inchange(
    tmp_path, monkeypatch
):
    """Relancer la publication avec les mêmes données redonne exactement le même lake (le jeu de
    fichiers est remplacé, pas complété)."""
    _natif(monkeypatch, True)
    lake = _lake(tmp_path)
    ecrire_parquet(FauxDataFrame(fichiers=SORTIE_NATIVE_PARQUET), lake, CHEMIN_PARQUET)
    apres_premiere_ecriture = _fichiers_du_lake(lake)
    ecrire_parquet(FauxDataFrame(fichiers=SORTIE_NATIVE_PARQUET), lake, CHEMIN_PARQUET)
    assert apres_premiere_ecriture
    assert _fichiers_du_lake(lake) == apres_premiere_ecriture
