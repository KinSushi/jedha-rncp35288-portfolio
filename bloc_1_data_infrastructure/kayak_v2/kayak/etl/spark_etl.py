# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""ETL du projet Kayak avec Spark : data lake (zone « raw ») → nettoyage → entrepôt SQL.

Étapes :
1. **Extraction** : lecture par Spark des fichiers bruts déposés par la collecte (coordonnées en
   CSV, prévisions et hôtels en JSON lines).
2. **Transformation** (Spark) : typage, nettoyage, contrôles de qualité comptés, dépliage des
   prévisions imbriquées (``explode``), agrégation météo par ville (``groupBy``), score météo,
   distance de chaque hôtel au centre de sa destination (haversine), rattachement de chaque
   hôtel à la destination la PLUS PROCHE (fonction de fenêtre), marquage des hôtels dont la
   commune OpenStreetMap n'est pas celle de la destination, jointure hôtels × villes.
3. **Chargement** :
   - zone « curated » du data lake : les tables enrichies sont écrites en **Parquet par
     Spark lui-même**, sans repasser par pandas, et le **CSV enrichi** (météo + hôtels)
     demandé par l'énoncé est lui aussi écrit par Spark ;
   - entrepôt SQL (clés primaires et étrangères, index, vue) : chargement **par lots** et dans
     **une seule transaction** ; si un lot échoue, tout est annulé et l'entrepôt reste dans son
     état précédent (jamais de tables vidées). SQLite en local, PostgreSQL sur AWS RDS en
     production (même code, seule l'URL de connexion change).

Limite assumée : le volume actuel tiendrait dans pandas ; le code utilise l'API Spark (la même
en local et sur un cluster) mais n'est PAS exécutable tel quel sur un cluster : les fichiers sont
écrits dans un dossier de travail local puis publiés dans le lake (sur cluster, on écrirait vers
des chemins ``s3a://``). Aucune table d'hôtels ou de météo n'est ramenée en entier dans pandas
PAR L'ETL ; seule la table des villes (une ligne par destination) passe par pandas, pour le
score. L'ETL n'a été exécuté que sur 35 villes, sur un poste unique (étude d'architecture).

CHOIX
-----
- **Retenu : Spark** pour le nettoyage et l'agrégation, bien que 4 128 hôtels tiendraient dans
  pandas : le cahier des charges vise toutes les destinations de Kayak et la compétence C1.2
  demande le calcul distribué. *Écarté* : tout faire dans pandas, qui ne démontrerait pas la
  montée en charge visée.
- **Retenu : écrire le Parquet et le CSV par Spark lui-même**, avec un repli par les exécuteurs
  (``mapInArrow`` et pyarrow) quand l'écriture native est indisponible (Windows sans
  ``winutils.exe``). *Écarté* : ramener la table dans pandas pour l'écrire, qui la chargerait en
  entier dans le programme principal.
- **Retenu : rattacher un hôtel sorti pour deux villes à la destination la plus proche** (fonction
  de fenêtre sur la distance, puis sur l'identifiant de ville pour les ex æquo). *Écarté* :
  ``dropDuplicates(["hotel_id"])``, qui gardait une ville arbitraire.
- **Retenu : « hors de France » = hors du rectangle englobant OU pays déclaré différent de FR.**
  *Écarté* : le rectangle seul, qui laissait passer les hôtels de Kehl (Allemagne).
- **Retenu : distance de haversine.** *Écarté* : le théorème de Pythagore sur les degrés, faux
  d'environ 25 % à cette latitude.
- **Retenu : charger l'entrepôt par lots dans UNE transaction** (suppression, création et
  insertions ensemble). *Écarté* : supprimer et recréer les tables hors transaction puis tout
  insérer d'un coup, qui laissait des tables vides après un échec.
- **Retenu : noms de colonnes sensibles à la casse** (``spark.sql.caseSensitive`` à ``true``).
  *Écarté* : le défaut de Spark, qui confond « Ville » et « ville » et laisse passer une faute de
  casse sans erreur ; tous les noms du projet sont en minuscules, donc les résultats ne changent
  pas.
- **Retenu : trier avant le chargement, clé secondaire comprise** (météo par ville PUIS jour,
  hôtels par ville PUIS identifiant) : l'ordre d'insertion se lit dans l'entrepôt (``rowid`` de
  SQLite). *Écarté* : se fier à l'ordre où Spark rend les lignes. Sur un petit volume, Spark réunit
  ses partitions de brassage et cet ordre semble acquis ; partitions séparées, comme sur un gros
  volume, il ne l'est plus.
- **Retenu : PySpark 3.5.9 et ANSI désactivé par défaut.** *Écarté* : PySpark 4.x, dont le mode
  ANSI par défaut change les conversions de type et obligerait à revalider chaque chiffre (non
  essayé dans ce travail ; décision de prudence, pas une mesure).

OÙ
---
- **Place** : étape ``etl`` du pipeline (kayak/pipeline.py:175) ; lit la zone ``raw`` du lake,
  écrit la zone ``curated`` et l'entrepôt SQL.
- **Appelants** : ``kayak/pipeline.py`` et le notebook de présentation
  (``Kayak_Bloc1_CDSD_source.py``).
- **Dépendances** : ``kayak.config`` (zones non communales), ``kayak.etl.score``,
  ``kayak.stockage.data_lake``, pyspark 3.5.9 (Java fourni par ``jdk4py``), pyarrow, pandas,
  SQLAlchemy 2, ``setuptools`` (module ``distutils`` importé par PySpark sous Python 3.12).

COMMENT
-------
- ``obtenir_spark`` crée la session locale (8 partitions de brassage, échanges Arrow) ;
  ``extraire`` lit les fichiers du lake ; ``transformer`` compte chaque anomalie AVANT de la
  corriger ; ``ecrire_parquet`` et ``ecrire_csv`` publient dans le lake ; ``charger_entrepot``
  insère par lots ; ``executer_etl`` enchaîne le tout.
- **Preuves** : l'ETL de bout en bout (tests/test_etl.py:130) ; le rattachement à la destination
  la plus proche (tests/test_etl.py:186) ; la distance et le marquage de commune
  (tests/test_etl.py:217) ; les hôtels déclarés hors de France (tests/test_etl.py:267) ;
  l'ordre de chargement de l'entrepôt, partitions de Spark réunies ou séparées
  (tests/test_etl.py:588) ; le
  texte exact de la vue, tel que l'entrepôt le garde
  (tests/test_spark_etl_hors_spark.py:390).
- **Refus** (tests inverses) : un hôtel rattaché à une ville inconnue est refusé
  (tests/test_etl.py:500) ; un chargement qui échoue laisse l'entrepôt intact
  (tests/test_etl.py:367) ; l'extraction sans fichier d'hôtels est refusée
  (tests/test_etl_riche.py:437) ; l'ETL laisse la zone ``raw`` inchangée
  (tests/test_etl_riche.py:290).
- **Jeu riche** : tests/test_etl_riche.py lance l'ETL une fois sur un lake qui réunit toutes les
  anomalies et vérifie chaque compteur de qualité (tests/test_etl_riche.py:284).
- Les parties qui n'ont pas besoin de Spark (découpage par lots, chargement SQL, session, écriture
  avec un faux DataFrame) sont testées dans tests/test_spark_etl_hors_spark.py.

POURQUOI
--------
- Formule de haversine : https://en.wikipedia.org/wiki/Haversine_formula
  (lecture non consignée, à revérifier) ; rayon moyen de la Terre : constante
  kayak/etl/spark_etl.py:177.
- SQLite et DDL transactionnel : la documentation SQLAlchemy décrit le défaut du pilote qui n'émet
  pas BEGIN avant un CREATE ou un DROP et la recette de l'événement ``begin``
  (https://docs.sqlalchemy.org/en/20/dialects/sqlite.html, rubrique « Transactions with SQLite and
  the sqlite3 driver », lecture non consignée, à revérifier) ; code correspondant :
  kayak/etl/spark_etl.py:657.
- Écriture native de Spark sous Windows : ``winutils.exe`` est exigé, constaté dans le journal de
  Spark (« Did not find winutils.exe ») ; détection dans kayak/etl/spark_etl.py:496.
- PySpark 3.5.9, Python 3.12 et pandas 3 : les appels INTERNES de PySpark émettent trois
  ``DeprecationWarning`` (``distutils.version.LooseVersion``, ``Series.astype(copy=False)``,
  ``is_datetime64tz_dtype``) que notre code ne provoque pas. Ils sont isolés par un filtre
  attribué au module ``pyspark``, dans les seuls tests qui lancent Spark (tests/test_etl.py:45) ;
  un avertissement attribué à ``kayak`` ou aux tests reste une erreur. Voies non retenues :
  pandas 2.x, qui n'en supprimerait qu'un (``astype(copy=False)``) : pandas 2.2.3 émet déjà celui
  de ``is_datetime64tz_dtype``, et ``LooseVersion`` vient de distutils ; PySpark 4.x, qui n'appelle
  plus ni l'un ni l'autre (lu dans le code de la version 4.2.0), mais change de version majeure
  (voir CHOIX ; non essayé). Les deux changeraient la version avec laquelle les résultats livrés
  ont été obtenus.
- Rectangle de la France métropolitaine et contrôles de qualité : choix du projet, voir l'étude
  d'architecture ; source : inconnue pour les bornes exactes (41,0 à 51,5 de latitude, -5,5 à 10,0
  de longitude), qui encadrent la métropole sans en suivre les frontières.

JOURNALISATION
--------------
- Un seul message, au niveau INFO, par ``executer_etl`` : « ETL terminé » avec les compteurs de
  qualité et les lignes chargées (kayak/etl/spark_etl.py:804).
- Spark lui-même est réglé au niveau ERROR (``setLogLevel("ERROR")``), pour que son bruit ne
  couvre pas les compteurs.
- Les compteurs de qualité sont aussi RENVOYÉS dans le résultat (clé ``qualite``), pour être
  affichés par le notebook et vérifiés par les tests.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, cast

import pandas as pd
from pyspark.sql import Column as ColonneSpark
from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql.types import DoubleType, IntegerType, LongType, StringType
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Connection,
    Date,
    Engine,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    text,
)

from kayak import config
from kayak.etl.score import calculer_score_meteo
from kayak.stockage.data_lake import DataLake

journal = logging.getLogger(__name__)

# Rectangle englobant la France métropolitaine : une coordonnée hors de ce cadre est une
# erreur de géocodage ou de saisie (contrôle de qualité de la collecte).
LATITUDE_MIN, LATITUDE_MAX = 41.0, 51.5
LONGITUDE_MIN, LONGITUDE_MAX = -5.5, 10.0

RAYON_TERRE_KM = 6371.0088  # rayon moyen de la Terre, pour la formule de haversine
TAILLE_LOT = 500  # lignes insérées par instruction dans l'entrepôt (un lot = un executemany)
FICHIERS_PARQUET = 1  # un seul fichier par table : quelques centaines de Ko à ce volume


def obtenir_spark() -> SparkSession:
    """Session Spark : celle de Databricks si on y est, sinon une session locale.

    En local, Java est fourni par le paquet jdk4py (aucune installation système) ; 8 partitions
    de brassage suffisent pour ce volume (200 par défaut).
    """
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        return SparkSession.builder.getOrCreate()
    import jdk4py

    os.environ["JAVA_HOME"] = str(jdk4py.JAVA_HOME)  # os.environ n'accepte que des chaînes
    # Les processus Python de Spark (exécuteurs) doivent utiliser le MÊME interpréteur que le
    # programme principal ; sinon Spark prend le premier « python » du PATH et refuse de
    # travailler si les versions diffèrent.
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_DRIVER_PYTHON"] = sys.executable
    session = (
        SparkSession.builder.master("local[*]")
        .appName("KayakETL")
        .config("spark.ui.enabled", False)
        .config("spark.sql.shuffle.partitions", "8")
        # Noms de colonnes exacts : par défaut Spark confond « Ville » et « ville », ce qui
        # laisserait passer une faute de casse sans erreur. Tous les noms du projet sont en
        # minuscules.
        .config("spark.sql.caseSensitive", True)
        # Échanges Spark ↔ pandas par lots Arrow (colonnes) plutôt que ligne par ligne : sans
        # cela, createDataFrame(pandas) lance un processus Python par partition (24 sur ce poste)
        # et un simple comptage prenait une trentaine de secondes sous Windows.
        .config("spark.sql.execution.arrow.pyspark.enabled", True)
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    return session


def _dans_la_france(latitude: str, longitude: str) -> ColonneSpark:
    """Expression Spark : vrai si le point est dans le cadre de la France métropolitaine."""
    return F.col(latitude).between(LATITUDE_MIN, LATITUDE_MAX) & F.col(longitude).between(
        LONGITUDE_MIN, LONGITUDE_MAX
    )


def _distance_km(lat1: str, lon1: str, lat2: str, lon2: str) -> ColonneSpark:
    """Expression Spark : distance entre deux points (formule de haversine), en kilomètres.

    Haversine plutôt que Pythagore sur les degrés : un degré de longitude ne vaut pas la même
    distance qu'un degré de latitude, et l'erreur serait de l'ordre de 25 % à cette latitude.
    """
    delta_lat = F.radians(F.col(lat2) - F.col(lat1))
    delta_lon = F.radians(F.col(lon2) - F.col(lon1))
    a = (
        F.sin(delta_lat / 2) ** 2
        + F.cos(F.radians(F.col(lat1))) * F.cos(F.radians(F.col(lat2))) * F.sin(delta_lon / 2) ** 2
    )
    return 2 * RAYON_TERRE_KM * F.asin(F.sqrt(a))


def _normaliser_lieu(colonne: str) -> ColonneSpark:
    """Expression Spark : nom de lieu ramené à une forme comparable (commune ↔ destination).

    « Aigues-Mortes » et « Aigues Mortes », « Nîmes » et « Nimes », « Le Mont-Saint-Michel » et
    « Mont Saint Michel », « St Malo » et « Saint-Malo », « Lyon Cedex 7 » et « Lyon » doivent se
    reconnaître : on retire les accents, la casse, la ponctuation, l'article initial, le suffixe
    « cedex » et on développe l'abréviation « st » / « ste ».
    """
    nom = F.lower(F.col(colonne))
    nom = F.regexp_replace(F.regexp_replace(nom, "œ", "oe"), "æ", "ae")
    nom = F.translate(nom, "àâäáãåçéèêëíìîïñóòôöõúùûüýÿ", "aaaaaaceeeeiiiinooooouuuuyy")
    nom = F.trim(F.regexp_replace(nom, "[^a-z0-9]+", " "))  # tirets, apostrophes, ponctuation
    nom = F.trim(F.regexp_replace(nom, r"\bcedex( \d+)?$", ""))
    nom = F.regexp_replace(nom, r"^st ", "saint ")
    nom = F.regexp_replace(nom, r"^ste ", "sainte ")
    return F.regexp_replace(nom, r"^(le|la|les|l) ", "")


def extraire(spark: SparkSession, lake: DataLake, date: str) -> dict[str, DataFrame]:
    """Lit les fichiers bruts de la date donnée (chemins locaux : cache si le lake est sur S3)."""
    racine = f"raw/{{}}/date={date}/"

    def chemin(source: str, fichier: str) -> str:
        return str(lake.chemin_local(racine.format(source) + fichier))

    # Hôtels : Booking (robot Scrapy) et, en repli, OpenStreetMap ; on réunit les fichiers non
    # vides (unionByName aligne les colonnes par leur nom).
    sources = []
    for source in ("booking", "osm"):
        relatif = racine.format(source) + "hotels.jsonl"
        if lake.existe(relatif) and lake.lire(relatif).strip():
            sources.append(spark.read.json(chemin(source, "hotels.jsonl")))
    if not sources:
        raise FileNotFoundError(f"aucun fichier d'hôtels pour la date {date}")
    hotels = sources[0]
    for autre in sources[1:]:
        hotels = hotels.unionByName(autre, allowMissingColumns=True)
    return {
        "coordonnees": spark.read.option("header", True).csv(
            chemin("geocodage", "coordonnees.csv")
        ),
        "previsions": spark.read.json(chemin("meteo", "previsions.jsonl")),
        "hotels": hotels,
    }


def transformer(brut: dict[str, DataFrame]) -> dict[str, Any]:
    """Nettoie et enrichit.

    Renvoie un dictionnaire avec :
    - ``villes`` : table des villes (pandas, une ligne par destination) ;
    - ``villes_spark`` : la même, en DataFrame Spark ;
    - ``meteo``, ``hotels``, ``enrichi`` : DataFrames Spark, PAS ramenés dans pandas ;
    - ``qualite`` : les compteurs de contrôle de qualité.
    """
    qualite: dict[str, int] = {}

    # --- Villes : typage et contrôle des coordonnées.
    villes = brut["coordonnees"].select(
        F.col("id_ville").cast(IntegerType()).alias("id_ville"),
        F.trim("ville").alias("ville"),
        F.col("latitude").cast(DoubleType()).alias("latitude"),
        F.col("longitude").cast(DoubleType()).alias("longitude"),
    )
    qualite["villes_lues"] = villes.count()
    qualite["villes_sans_coordonnees_valides"] = villes.filter(
        ~_dans_la_france("latitude", "longitude") | F.col("latitude").isNull()
    ).count()

    # --- Prévisions : une ligne par ville dans le fichier brut, avec la liste imbriquée des
    # 7 jours ; explode() la déplie en une ligne par (ville, jour).
    meteo = (
        brut["previsions"]
        .select("id_ville", F.explode("previsions").alias("p"))
        .select(
            F.col("id_ville").cast(IntegerType()).alias("id_ville"),
            F.to_date("p.date").alias("date"),
            F.col("p.temp_max_c").cast(DoubleType()).alias("temp_max_c"),
            F.col("p.temp_min_c").cast(DoubleType()).alias("temp_min_c"),
            F.col("p.pluie_mm").cast(DoubleType()).alias("pluie_mm"),
            F.col("p.proba_pluie_pct").cast(DoubleType()).alias("proba_pluie_pct"),
            F.col("p.ensoleillement_h").cast(DoubleType()).alias("ensoleillement_h"),
            F.col("p.vent_max_kmh").cast(DoubleType()).alias("vent_max_kmh"),
            F.col("p.source").alias("source"),
        )
        .dropDuplicates(["id_ville", "date"])
    )
    qualite["previsions_journalieres"] = meteo.count()

    # --- Agrégats météo par ville (calcul distribué), puis score sur 35 lignes en pandas.
    agregats = meteo.groupBy("id_ville").agg(
        F.round(F.mean("temp_max_c"), 2).alias("temp_max_moy_c"),
        F.round(F.sum("pluie_mm"), 2).alias("pluie_totale_mm"),
        F.round(F.mean("ensoleillement_h"), 2).alias("ensoleillement_moy_h"),
        F.round(F.mean("proba_pluie_pct"), 1).alias("proba_pluie_moy_pct"),
    )
    villes_pdf = calculer_score_meteo(villes.join(agregats, "id_ville", "left").toPandas())
    villes_pdf = villes_pdf.sort_values("rang_meteo").reset_index(drop=True)
    villes_spark = villes.sparkSession.createDataFrame(villes_pdf)

    # --- Hôtels : typage, doublons, contrôles ; chaque anomalie est COMPTÉE avant correction.
    colonnes_brutes = brut["hotels"].columns
    hotels = brut["hotels"].select(
        F.col("hotel_id").cast(LongType()).alias("hotel_id"),
        F.col("id_ville").cast(IntegerType()).alias("id_ville"),
        F.trim("nom").alias("nom"),
        F.col("url"),
        F.col("latitude").cast(DoubleType()).alias("latitude"),
        F.col("longitude").cast(DoubleType()).alias("longitude"),
        F.trim("adresse").alias("adresse"),
        # Commune déclarée par OpenStreetMap (addr:city) ; absente des autres sources.
        (F.trim("commune") if "commune" in colonnes_brutes else F.lit(None))
        .cast(StringType())
        .alias("commune"),
        # Pays déclaré par OpenStreetMap (addr:country) : sert au contrôle « hors de France ».
        (F.upper(F.trim("pays")) if "pays" in colonnes_brutes else F.lit(None))
        .cast(StringType())
        .alias("pays"),
        F.col("note").cast(DoubleType()).alias("note"),
        F.col("nb_avis").cast(IntegerType()).alias("nb_avis"),
        F.col("etoiles").cast(IntegerType()).alias("etoiles"),
        F.trim("description").alias("description"),
        F.to_date("date_collecte").alias("date_collecte"),
        F.coalesce(F.col("source"), F.lit("booking")).alias("source")
        if "source" in colonnes_brutes
        else F.lit("booking").alias("source"),
    )
    qualite["hotels_lus"] = hotels.count()
    hotels = hotels.filter(F.col("hotel_id").isNotNull() & F.col("nom").isNotNull())
    qualite["hotels_sans_identifiant_ou_nom"] = qualite["hotels_lus"] - hotels.count()

    # Distance de chaque hôtel au centre de la destination pour laquelle il a été collecté :
    # l'« aire de destination » de 8 km n'est pas la commune, et la distance le rend mesurable.
    centres = villes.select(
        "id_ville",
        F.col("ville").alias("ville"),
        F.col("latitude").alias("centre_latitude"),
        F.col("longitude").alias("centre_longitude"),
    )
    hotels = hotels.join(centres, "id_ville", "left").withColumn(
        "distance_km",
        F.round(_distance_km("centre_latitude", "centre_longitude", "latitude", "longitude"), 3),
    )
    # Un hôtel dont la ville n'est pas dans la table des villes n'aurait ni destination ni
    # distance, et deviendrait une ligne orpheline de l'entrepôt (clé étrangère vers villes) :
    # il est refusé, au lieu d'être chargé ou écarté sans le dire.
    orphelins = hotels.filter(F.col("ville").isNull()).count()
    if orphelins:
        raise ValueError(
            f"{orphelins} hôtel(s) rattaché(s) à une ville inconnue "
            "(absente des coordonnées, ou sans nom)"
        )

    # Un même hôtel peut sortir pour deux villes voisines (Biarritz et Bayonne, Colmar et
    # Eguisheim). On le garde UNE fois, rattaché à la destination la plus proche. Le simple
    # dropDuplicates(["hotel_id"]) gardait une ville arbitraire. Fenêtre : par hôtel, tri par
    # distance croissante (les distances inconnues en dernier), puis par id_ville pour qu'un
    # ex æquo donne toujours le même résultat.
    avant = hotels.count()
    fenetre = Window.partitionBy("hotel_id").orderBy(
        F.col("distance_km").asc_nulls_last(), F.col("id_ville").asc()
    )
    hotels = (
        hotels.withColumn("_rang_proximite", F.row_number().over(fenetre))
        .filter(F.col("_rang_proximite") == 1)
        .drop("_rang_proximite")
    )
    qualite["hotels_en_double"] = avant - hotels.count()
    # « Hors de France » : hors du rectangle englobant (erreur de géocodage ou de saisie) OU
    # pays déclaré autre que « FR ». Le rectangle seul laissait passer les hôtels de Kehl
    # (Allemagne), de l'autre côté du Rhin, rattachés à Strasbourg : un rectangle ne suit pas
    # les frontières, il n'attrape que les erreurs grossières.
    en_france = _dans_la_france("latitude", "longitude") & (
        F.col("pays").isNull() | (F.col("pays") == "FR")
    )
    qualite["hotels_hors_de_france"] = hotels.filter(~en_france).count()
    hotels = hotels.filter(en_france)
    qualite["notes_hors_echelle"] = hotels.filter(~F.col("note").between(0, 10)).count()
    hotels = hotels.withColumn("note", F.when(F.col("note").between(0, 10), F.col("note")))
    qualite["hotels_sans_note"] = hotels.filter(F.col("note").isNull()).count()

    # Commune de l'hôtel différente de celle de la destination ? Vrai / faux quand la comparaison
    # est possible ; VIDE sinon (F.when sans « otherwise ») : commune absente d'OpenStreetMap, ou
    # destination qui n'est pas une commune (massif, département, monument). La ville n'est
    # jamais vide ici : un hôtel de ville inconnue a été refusé plus haut.
    hotels = hotels.withColumn(
        "hors_commune",
        F.when(
            F.col("commune").isNotNull()
            & ~F.col("ville").isin(list(config.DESTINATIONS_NON_COMMUNALES)),
            _normaliser_lieu("commune") != _normaliser_lieu("ville"),
        ),
    )
    qualite["hotels_hors_commune"] = hotels.filter(F.col("hors_commune")).count()
    qualite["hotels_commune_non_evaluable"] = hotels.filter(F.col("hors_commune").isNull()).count()
    qualite["hotels_retenus"] = hotels.count()

    # Table des hôtels telle qu'elle est chargée dans l'entrepôt : le nom de la ville et les
    # coordonnées du centre ne servent qu'au calcul, ils restent dans la table « villes ».
    hotels = hotels.select(
        "hotel_id",
        "id_ville",
        "nom",
        "url",
        "latitude",
        "longitude",
        "adresse",
        "note",
        "nb_avis",
        "etoiles",
        "description",
        "date_collecte",
        "source",
        "distance_km",
        "commune",
        "hors_commune",
    )  # (« pays » ne sert qu'au contrôle ci-dessus : il n'entre pas dans les tables)

    # --- Jointure hôtels × villes (Spark) : la table enrichie demandée par l'énoncé.
    enrichi = hotels.join(
        villes_spark.select(
            "id_ville",
            "ville",
            "temp_max_moy_c",
            "pluie_totale_mm",
            "ensoleillement_moy_h",
            "proba_pluie_moy_pct",
            "score_meteo",
            "rang_meteo",
        ),
        "id_ville",
        "inner",
    ).orderBy(
        "rang_meteo",
        F.col("note").desc_nulls_last(),
        F.col("etoiles").desc_nulls_last(),
        F.col("distance_km").asc_nulls_last(),
        "nom",
    )
    return {
        "villes": villes_pdf,
        "villes_spark": villes_spark,
        "meteo": meteo.orderBy("id_ville", "date"),
        "hotels": hotels.orderBy("id_ville", "hotel_id"),
        "enrichi": enrichi,
        "qualite": qualite,
    }


def _hadoop_natif_disponible() -> bool:
    """Spark peut-il écrire des fichiers avec les bibliothèques Hadoop de l'environnement ?

    Sous Linux, macOS, EMR et Databricks : oui. Sous Windows, l'écriture native de Spark exige
    les binaires Hadoop (winutils.exe, variable HADOOP_HOME) que Spark ne fournit pas : sans
    eux, chaque écriture échoue dès la création du dossier de sortie. On le détecte pour choisir
    la méthode d'écriture (voir ``ecrire_parquet``).
    """
    return os.name != "nt" or bool(os.environ.get("HADOOP_HOME"))


def _ecrire_executeurs(df: DataFrame, dossier: Path, format_: str) -> None:
    """Écrit un DataFrame en Parquet ou en CSV depuis les exécuteurs Spark, avec pyarrow.

    Chaque partition Spark écrit son propre fichier ``part-NNNNN.parquet`` (ou ``.csv``) : le
    calcul et l'écriture restent distribués et rien ne passe par pandas ni par le programme
    principal. Les lots de lignes (Arrow) sont écrits au fil de l'eau, sans charger la
    partition entière. Méthode de repli quand l'écriture native de Spark est indisponible
    (Windows sans winutils). Le CSV est écrit à la norme habituelle : séparateur virgule,
    en-tête, guillemets doublés dans les valeurs, valeurs manquantes vides.
    """
    dossier.mkdir(parents=True, exist_ok=True)
    cible = str(dossier)

    def ecrire_partition(lots: Iterable[Any]) -> Iterator[Any]:
        # Importés ici : cette fonction s'exécute dans le processus Python de l'exécuteur.
        import pyarrow as pa  # type: ignore[import-untyped]
        import pyarrow.csv as pacsv  # type: ignore[import-untyped]
        import pyarrow.parquet as pq  # type: ignore[import-untyped]
        from pyspark import TaskContext

        # Dans un exécuteur, TaskContext.get() n'est jamais None (il l'est seulement hors tâche).
        numero = TaskContext.get().partitionId()  # type: ignore[union-attr]
        chemin = os.path.join(cible, f"part-{numero:05d}.{format_}")
        ecrivain = None
        total = 0
        for lot in lots:
            if ecrivain is None:  # le fichier n'est créé que si la partition a des lignes
                if format_ == "csv":
                    options = pacsv.WriteOptions(include_header=True)
                    ecrivain = pacsv.CSVWriter(chemin, lot.schema, write_options=options)
                else:
                    ecrivain = pq.ParquetWriter(chemin, lot.schema)
            if format_ == "csv":
                ecrivain.write(lot)
            else:
                ecrivain.write_batch(lot)
            total += lot.num_rows
        if ecrivain is not None:
            ecrivain.close()
        yield pa.RecordBatch.from_pylist([{"lignes": total}])

    # mapInArrow est une transformation paresseuse : count() déclenche l'exécution.
    df.mapInArrow(ecrire_partition, "lignes long").count()


@contextlib.contextmanager
def _dossier_de_travail(lake: DataLake) -> Iterator[Path]:
    """Dossier temporaire sur le même disque que le lake ; il disparaît avec son contenu.

    Le dossier de travail du lake lui-même est retiré s'il est vide : on ne le laisse pas
    traîner dans la livraison.
    """
    try:
        with tempfile.TemporaryDirectory(dir=lake.dossier_temporaire()) as travail:
            yield Path(travail)
    finally:  # y compris quand le bloc lève une exception
        with contextlib.suppress(OSError):
            lake.dossier_temporaire().rmdir()


def ecrire_parquet(df: DataFrame, lake: DataLake, chemin: str) -> list[str]:
    """Écrit un DataFrame Spark en Parquet dans le data lake et renvoie les chemins écrits.

    Spark écrit d'abord dans un dossier de travail du lake, puis ``DataLake.publier_dossier``
    met le résultat en place (échange de dossier en local, envoi des objets sur S3). Un seul
    fichier est produit (``coalesce``) : à ce volume, la lecture en est plus simple ; au-delà
    de quelques centaines de Mo, il faudrait garder plusieurs fichiers.

    Sur un cluster, on écrirait directement vers un chemin ``s3a://`` (connecteur Hadoop-AWS) :
    le disque d'un exécuteur n'est pas le disque du programme principal. Ce n'est pas fait ici,
    où l'ETL tourne sur un seul poste : le chemin de travail est local, puis publié dans le lake.
    """
    df = df.coalesce(FICHIERS_PARQUET)
    with _dossier_de_travail(lake) as travail:
        sortie = travail / "table.parquet"
        if _hadoop_natif_disponible():
            df.write.mode("overwrite").parquet(sortie.resolve().as_uri())
        else:
            _ecrire_executeurs(df, sortie, "parquet")
        return lake.publier_dossier(chemin, sortie)


def ecrire_csv(df: DataFrame, lake: DataLake, chemin: str) -> str:
    """Écrit un DataFrame Spark en UN fichier CSV dans le data lake et renvoie son chemin.

    L'énoncé du projet demande un « CSV enrichi » dans le bucket : c'est ce fichier, lisible
    par tout le monde (tableur, pandas). Le Parquet reste le format de travail. C'est Spark qui
    écrit le CSV (``coalesce(1)`` : un seul fichier), sans pandas ; le fichier obtenu est ensuite
    copié tel quel dans le lake. Cette copie lit le fichier entier en mémoire : acceptable pour
    un fichier de quelques centaines de Ko, pas pour une table de plusieurs gigaoctets, qu'il
    faudrait garder en Parquet réparti.
    """
    df = df.coalesce(1)
    with _dossier_de_travail(lake) as travail:
        sortie = travail / "table.csv"
        if _hadoop_natif_disponible():
            (
                df.write.mode("overwrite")
                .option("header", True)
                .option("quote", '"')
                .option("escape", '"')  # guillemets doublés, comme pandas et les tableurs
                .csv(sortie.resolve().as_uri())
            )
        else:
            _ecrire_executeurs(df, sortie, "csv")
        parties = sorted(sortie.glob("part-*.csv"))
        if len(parties) != 1:
            raise RuntimeError(f"un seul fichier CSV était attendu, trouvé : {len(parties)}")
        lake.ecrire(chemin, parties[0].read_bytes())
    return chemin


def _lignes_pandas(df: pd.DataFrame) -> Iterator[dict[str, Any]]:
    """Enregistrements d'un DataFrame pandas, NaN remplacés par None (NULL en SQL)."""
    propre = df.astype(object).where(df.notna(), None)
    yield from cast("list[dict[str, Any]]", propre.to_dict("records"))


def _lignes_spark(df: DataFrame) -> Iterator[dict[str, Any]]:
    """Enregistrements d'un DataFrame Spark, lus en flux (une partition à la fois).

    ``toLocalIterator`` ne ramène pas toute la table dans la mémoire du programme principal,
    contrairement à ``toPandas`` ou ``collect``. Les NaN flottants deviennent None (NULL).
    """
    for ligne in df.toLocalIterator():
        yield {
            cle: (None if isinstance(valeur, float) and valeur != valeur else valeur)
            for cle, valeur in ligne.asDict().items()
        }


def par_lots(lignes: Iterable[dict[str, Any]], taille: int) -> Iterator[list[dict[str, Any]]]:
    """Découpe un flux d'enregistrements en listes d'au plus ``taille`` éléments."""
    lot: list[dict[str, Any]] = []
    for ligne in lignes:
        lot.append(ligne)
        if len(lot) >= taille:
            yield lot
            lot = []
    if lot:
        yield lot


def _creer_moteur(db_url: str) -> Engine:
    """Moteur SQLAlchemy ; sous SQLite, les instructions de schéma deviennent transactionnelles.

    Par défaut, le pilote SQLite de Python n'ouvre pas de transaction avant un CREATE ou un
    DROP : ces ordres seraient validés immédiatement, et un échec ultérieur laisserait des
    tables vides. On le neutralise (recette de la documentation SQLAlchemy) en émettant
    nous-mêmes BEGIN. PostgreSQL est déjà transactionnel pour le schéma : rien à faire.
    """
    moteur = create_engine(db_url)
    if moteur.dialect.name == "sqlite":

        @event.listens_for(moteur, "connect")
        def _desactiver_begin_implicite(connexion_dbapi: Any, _enregistrement: Any) -> None:
            connexion_dbapi.isolation_level = None

        @event.listens_for(moteur, "begin")
        def _begin_explicite(connexion: Connection) -> None:
            connexion.exec_driver_sql("BEGIN")

    return moteur


def charger_entrepot(
    villes: Iterable[dict[str, Any]],
    meteo: Iterable[dict[str, Any]],
    hotels: Iterable[dict[str, Any]],
    db_url: str,
    taille_lot: int = TAILLE_LOT,
) -> dict[str, int]:
    """Recrée les tables relationnelles et les charge, par lots, dans UNE transaction.

    Paramètres : trois flux d'enregistrements (dictionnaires) — villes, météo, hôtels. Tout se
    passe dans la même transaction : suppression de la vue et des tables, création, insertion
    de chaque lot, création de la vue. Si une étape échoue (clé en double, valeur invalide), la
    transaction est annulée et l'entrepôt garde son contenu précédent : jamais de tables vides
    ou à moitié remplies. Idempotent : la relance recrée les mêmes tables.
    """
    moteur = _creer_moteur(db_url)
    schema = MetaData()
    table_villes = Table(
        "villes",
        schema,
        Column("id_ville", Integer, primary_key=True),
        Column("ville", String(100), nullable=False),
        Column("latitude", Float),
        Column("longitude", Float),
        Column("temp_max_moy_c", Float),
        Column("pluie_totale_mm", Float),
        Column("ensoleillement_moy_h", Float),
        Column("proba_pluie_moy_pct", Float),
        Column("score_meteo", Float),
        Column("rang_meteo", Integer),
    )
    table_meteo = Table(
        "meteo_quotidienne",
        schema,
        Column("id_ville", Integer, ForeignKey("villes.id_ville"), primary_key=True),
        Column("date", Date, primary_key=True),
        Column("temp_max_c", Float),
        Column("temp_min_c", Float),
        Column("pluie_mm", Float),
        Column("proba_pluie_pct", Float),
        Column("ensoleillement_h", Float),
        Column("vent_max_kmh", Float),
        Column("source", String(20)),
    )
    table_hotels = Table(
        "hotels",
        schema,
        Column("hotel_id", BigInteger, primary_key=True),  # identifiants Booking à 8 chiffres
        Column("id_ville", Integer, ForeignKey("villes.id_ville"), nullable=False),
        Column("nom", String(300), nullable=False),
        Column("url", String(500)),
        Column("latitude", Float),
        Column("longitude", Float),
        Column("adresse", String(300)),
        Column("note", Float),
        Column("nb_avis", Integer),
        Column("etoiles", Integer),
        Column("description", Text),
        Column("date_collecte", Date),
        Column("source", String(20)),  # booking ou openstreetmap : la provenance reste visible
        Column("distance_km", Float),  # distance au centre de la destination (aire de 8 km)
        Column("commune", String(100)),  # commune déclarée par OpenStreetMap (addr:city)
        Column("hors_commune", Boolean),  # commune différente de la destination ; NULL = inconnu
    )
    # Index sur les colonnes de filtre et de tri les plus fréquentes des requêtes métier.
    Index("ix_hotels_id_ville", table_hotels.c.id_ville)
    Index("ix_hotels_note", table_hotels.c.note)

    def conserver_colonnes(
        lignes: Iterable[dict[str, Any]], table: Table
    ) -> Iterator[dict[str, Any]]:
        """Ne garde que les colonnes de la table (les flux peuvent en porter d'autres)."""
        noms = [c.name for c in table.columns]
        for ligne in lignes:
            yield {nom: ligne.get(nom) for nom in noms}

    with moteur.begin() as connexion:
        # Tout est dans ce bloc : la sortie du bloc valide la transaction, une exception
        # l'annule (rollback) y compris la suppression des anciennes tables.
        connexion.execute(text("DROP VIEW IF EXISTS vue_hotels_destinations"))
        schema.drop_all(connexion)
        schema.create_all(connexion)
        for table, lignes in (
            (table_villes, villes),
            (table_meteo, meteo),
            (table_hotels, hotels),
        ):
            for lot in par_lots(conserver_colonnes(lignes, table), taille_lot):
                connexion.execute(table.insert(), lot)
        # Vue « prête à l'emploi » pour les équipes métier : hôtels et météo de leur ville.
        connexion.execute(
            text(
                "CREATE VIEW vue_hotels_destinations AS "
                "SELECT v.rang_meteo, v.ville, v.score_meteo, v.temp_max_moy_c, "
                "v.pluie_totale_mm, "
                "h.nom AS hotel, h.note, h.nb_avis, h.etoiles, h.url, h.latitude, h.longitude, "
                "h.source, h.distance_km, h.commune, h.hors_commune "
                "FROM hotels h JOIN villes v ON v.id_ville = h.id_ville"
            )
        )
    with moteur.connect() as connexion:
        return {
            nom: connexion.execute(text(f"SELECT COUNT(*) FROM {nom}")).scalar_one()
            for nom in ("villes", "meteo_quotidienne", "hotels")
        }


def executer_etl(lake: DataLake, date_collecte: str, db_url: str) -> dict[str, Any]:
    """Exécute l'ETL complet et renvoie la table des villes, les compteurs et les chemins écrits.

    Le lac reçoit deux tables en Parquet écrites par Spark (``curated/villes`` et
    ``curated/kayak_enrichi``) et le CSV enrichi de l'énoncé (``kayak_enrichi.csv``, écrit par
    Spark aussi) ; l'entrepôt est chargé par lots dans une transaction. Les résultats détaillés
    se relisent depuis le lac (zone « curated »), pas depuis la mémoire.
    """
    spark = obtenir_spark()
    resultat = transformer(extraire(spark, lake, date_collecte))
    qualite = resultat["qualite"]
    # Zone « curated » : Parquet écrit par Spark, sans passer par pandas.
    base = f"date={date_collecte}"
    ecrits = {
        "villes": ecrire_parquet(
            resultat["villes_spark"].orderBy("rang_meteo"),
            lake,
            f"curated/villes/{base}/villes.parquet",
        ),
        "kayak_enrichi": ecrire_parquet(
            resultat["enrichi"], lake, f"curated/kayak_enrichi/{base}/kayak_enrichi.parquet"
        ),
        # Le CSV enrichi (météo + hôtels) que demande l'énoncé, écrit lui aussi par Spark.
        "kayak_enrichi_csv": ecrire_csv(
            resultat["enrichi"], lake, f"curated/kayak_enrichi/{base}/kayak_enrichi.csv"
        ),
    }
    charges = charger_entrepot(
        _lignes_pandas(resultat["villes"]),
        _lignes_spark(resultat["meteo"]),
        _lignes_spark(resultat["hotels"]),
        db_url,
    )
    journal.info("ETL terminé : %s ; lignes chargées : %s", qualite, charges)
    return {
        "tables": {"villes": resultat["villes"]},
        "qualite": qualite,
        "charges": charges,
        "curated": ecrits,
    }
