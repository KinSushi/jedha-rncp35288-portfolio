# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Tests des fonctions du notebook Steam (`steam_analyse.py`).

Les fonctions Spark sont éprouvées sur de petits enregistrements qui reproduisent les défauts
réels du fichier (éditeur vide, tabulations, balises dans les langues, âge de 180 ans, trois
formats de date...). Chaque valeur attendue est calculée à la main. Les tests dont le nom dit
« refuse », « leve » ou « inchange » vérifient qu'une entrée interdite est refusée, ou que l'entrée
n'a pas été modifiée : pour chaque comportement attendu, il y a un comportement interdit.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jdk4py
import matplotlib.pyplot as plt
import pandas as pd
import pytest
from matplotlib.colors import to_hex
from pyspark.errors import AnalysisException
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, StringType, StructField, StructType

import steam_analyse as sa


def complet(message: str) -> str:
    """Motif qui n'accepte que ce message, en entier (pas un morceau de message)."""
    return "^" + re.escape(message) + "$"


# ------------------------------------------------------------------------------------------
# Session et chemins (sans Spark)
# ------------------------------------------------------------------------------------------


def test_sur_databricks_lit_la_variable_d_environnement_et_le_chemin_suit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert sa.sur_databricks({"DATABRICKS_RUNTIME_VERSION": "14.3"}) is True
    assert sa.sur_databricks({"HOME": "/tmp"}) is False
    monkeypatch.setenv("DATABRICKS_RUNTIME_VERSION", "14.3")  # os.environ par défaut
    assert sa.sur_databricks() is True
    monkeypatch.delenv("DATABRICKS_RUNTIME_VERSION")
    assert sa.sur_databricks() is False
    adresse = "s3://compartiment-exemple/steam_game_output.json"
    assert sa.chemin_donnees(True, {"STEAM_CHEMIN_S3": adresse}) == adresse
    monkeypatch.setenv("STEAM_CHEMIN_S3", adresse)  # os.environ par défaut
    assert sa.chemin_donnees(True) == adresse
    assert sa.chemin_donnees(False) == "donnees/steam_game_output.json"
    assert sa.chemin_donnees(False, {}) == "donnees/steam_game_output.json"


@pytest.mark.parametrize("environnement", [{}, {"STEAM_CHEMIN_S3": ""}, {"STEAM_CHEMIN_S3": "  "}])
def test_chemin_donnees_sur_databricks_refuse_une_adresse_absente_ou_vide(
    environnement: dict[str, str],
) -> None:
    with pytest.raises(
        ValueError,
        match=complet(
            "sur Databricks, définir la variable d'environnement STEAM_CHEMIN_S3 "
            "(adresse S3 du fichier JSON)"
        ),
    ):
        sa.chemin_donnees(True, environnement)


def test_sur_databricks_laisse_l_environnement_recu_inchange() -> None:
    environnement = {"DATABRICKS_RUNTIME_VERSION": "14.3", "HOME": "/tmp"}
    avant = dict(environnement)
    sa.sur_databricks(environnement)
    assert environnement == avant


# ------------------------------------------------------------------------------------------
# Fonctions pandas
# ------------------------------------------------------------------------------------------


def test_wilson_reproduit_les_valeurs_connues_encadre_la_proportion_et_se_resserre() -> None:
    bas, haut = sa.wilson(pd.Series([90, 0, 10]), pd.Series([100, 10, 10]))
    # 90 sur 100 : [0,8256 ; 0,9448] (valeurs de référence des tables de Wilson à 95 %).
    assert bas.iloc[0] == pytest.approx(0.8256, abs=1e-4)
    assert haut.iloc[0] == pytest.approx(0.9448, abs=1e-4)
    # 0 sur 10 : la borne basse est nulle ; 10 sur 10 : la borne haute vaut 1.
    assert bas.iloc[1] == pytest.approx(0.0, abs=1e-12)
    assert haut.iloc[1] == pytest.approx(0.2775, abs=1e-4)
    assert bas.iloc[2] == pytest.approx(0.7225, abs=1e-4)
    assert haut.iloc[2] == pytest.approx(1.0, abs=1e-12)
    # Un seul essai, réussi, est une proportion valable : [1 / (1 + 1,96²) ; 1].
    un_bas, un_haut = sa.wilson(pd.Series([1]), pd.Series([1]))
    assert un_bas.iloc[0] == pytest.approx(1 / (1 + 1.96**2))
    assert un_haut.iloc[0] == pytest.approx(1.0)
    # La proportion est encadrée, et l'intervalle se resserre quand l'effectif grandit.
    petit_bas, petit_haut = sa.wilson(pd.Series([5]), pd.Series([10]))
    grand_bas, grand_haut = sa.wilson(pd.Series([500]), pd.Series([1000]))
    assert petit_bas.iloc[0] < 0.5 < petit_haut.iloc[0]
    assert grand_bas.iloc[0] < 0.5 < grand_haut.iloc[0]
    assert (grand_haut.iloc[0] - grand_bas.iloc[0]) < (petit_haut.iloc[0] - petit_bas.iloc[0])
    # Un niveau de confiance plus élevé élargit l'intervalle ; un niveau plus bas (z = 1, environ
    # 68 %) le resserre : 50 sur 100 donne alors [0,4502 ; 0,5498], calculé à la main.
    bas_99, _ = sa.wilson(pd.Series([50]), pd.Series([100]), z=2.576)
    assert bas_99.iloc[0] < sa.wilson(pd.Series([50]), pd.Series([100]))[0].iloc[0]
    bas_68, haut_68 = sa.wilson(pd.Series([50]), pd.Series([100]), z=1.0)
    assert bas_68.iloc[0] == pytest.approx(0.4502, abs=1e-4)
    assert haut_68.iloc[0] == pytest.approx(0.5498, abs=1e-4)


EFFECTIF_INVALIDE = "effectif nul ou négatif : la proportion n'existe pas"
SUCCES_INVALIDE = "nombre de succès négatif ou supérieur à l'effectif"


@pytest.mark.parametrize(
    ("succes", "effectif", "message"),
    [
        ([1], [0], EFFECTIF_INVALIDE),
        ([1], [-5], EFFECTIF_INVALIDE),
        ([-1], [10], SUCCES_INVALIDE),
        ([11], [10], SUCCES_INVALIDE),
    ],
)
def test_wilson_leve_une_erreur_si_ce_n_est_pas_une_proportion(
    succes: list[int], effectif: list[int], message: str
) -> None:
    with pytest.raises(ValueError, match=complet(message)):
        sa.wilson(pd.Series(succes), pd.Series(effectif))


@pytest.mark.parametrize(
    ("succes", "effectif"),
    [
        ([90.0, float("nan")], [100.0, 10.0]),  # un nombre de succès manquant
        ([90.0, 5.0], [100.0, float("nan")]),  # un effectif manquant : « NaN <= 0 » est faux
    ],
)
def test_wilson_refuse_un_succes_ou_un_effectif_manquant(
    succes: list[float], effectif: list[float]
) -> None:
    # Sans ce refus, la valeur manquante passerait les contrôles (toute comparaison avec NaN est
    # fausse) et rendrait une borne NaN, sans prévenir, dans un tableau de résultats.
    with pytest.raises(
        ValueError,
        match=complet("nombre de succès ou effectif manquant : la proportion n'existe pas"),
    ):
        sa.wilson(pd.Series(succes), pd.Series(effectif))


@pytest.mark.parametrize("z", [0.0, -1.96])
def test_wilson_refuse_un_quantile_z_nul_ou_negatif(z: float) -> None:
    # z = 0 confondrait les deux bornes ; z < 0 les inverserait (borne « basse » au-dessus de la
    # « haute ») : deux intervalles faux, rendus sans erreur sans ce refus.
    message = f"quantile z strictement positif attendu, reçu {z}"
    with pytest.raises(ValueError, match=complet(message)):
        sa.wilson(pd.Series([50]), pd.Series([100]), z=z)


def test_wilson_leve_une_erreur_pour_des_series_mal_alignees() -> None:
    # Succès d'un tableau et effectifs d'un autre (genres dans un ordre différent) : pandas
    # refuse de les comparer, au lieu d'aligner en silence des lignes qui ne se correspondent pas.
    succes = pd.Series([3, 1], index=["Action", "RPG"])
    effectif = pd.Series([10, 10], index=["Indie", "RPG"])
    with pytest.raises(ValueError, match="identically-labeled"):
        sa.wilson(succes, effectif)


def test_wilson_laisse_les_series_d_entree_inchangees() -> None:
    succes, effectif = pd.Series([90, 5]), pd.Series([100, 10])
    avant = (succes.copy(), effectif.copy())
    sa.wilson(succes, effectif)
    pd.testing.assert_series_equal(succes, avant[0])
    pd.testing.assert_series_equal(effectif, avant[1])


@pytest.mark.parametrize(
    ("coeurs", "attendu"),
    [
        (24, [1, 2, 4, 8, 16, 24]),
        (16, [1, 2, 4, 8, 16]),
        (10, [1, 2, 4, 8, 10]),
        (4, [1, 2, 4]),
        (3, [1, 2, 3]),
        (1, [1]),
    ],
)
def test_reglages_partitions_ne_depasse_pas_le_nombre_de_coeurs(
    coeurs: int, attendu: list[int]
) -> None:
    assert sa.reglages_partitions(coeurs) == attendu


@pytest.mark.parametrize("coeurs", [0, -4])
def test_reglages_partitions_leve_une_erreur_sans_aucun_coeur(coeurs: int) -> None:
    with pytest.raises(ValueError, match=complet(f"au moins un cœur attendu, reçu {coeurs}")):
        sa.reglages_partitions(coeurs)


def test_tableau_acceleration_divise_la_mediane_a_une_partition_par_celle_du_reglage() -> None:
    essais = {1: [8.0, 9.0, 8.0], 2: [4.0, 5.0, 4.0], 4: [2.0, 2.0, 3.0]}
    tableau = sa.tableau_acceleration(essais)
    assert tableau.index.name == "partitions"
    assert tableau.index.tolist() == [1, 2, 4]
    assert list(tableau.columns) == [
        "essai 1 (s)",
        "essai 2 (s)",
        "essai 3 (s)",
        "médiane (s)",
        "accélération",
    ]
    assert tableau["médiane (s)"].tolist() == [8.0, 4.0, 2.0]
    assert tableau["accélération"].tolist() == pytest.approx([1.0, 2.0, 4.0])
    # C'est la médiane et non la moyenne : [1, 3, 2, 10, 4] a pour médiane 3 (moyenne 4).
    inegal = sa.tableau_acceleration({1: [1.0, 3.0, 2.0, 10.0, 4.0]})
    assert inegal.loc[1, "médiane (s)"] == 3.0
    assert inegal.loc[1, "accélération"] == 1.0


def test_tableau_acceleration_leve_une_erreur_sans_le_reglage_a_une_partition() -> None:
    with pytest.raises(KeyError):
        sa.tableau_acceleration({2: [4.0, 4.0], 4: [2.0, 2.0]})


def test_tableau_acceleration_laisse_les_mesures_d_entree_inchangees() -> None:
    essais = {1: [8.0, 9.0], 2: [4.0, 5.0]}
    avant = {k: list(v) for k, v in essais.items()}
    sa.tableau_acceleration(essais)
    assert essais == avant


@pytest.mark.parametrize("duree", [0.0, -1.5, float("nan")])
def test_tableau_acceleration_refuse_une_duree_nulle_negative_ou_manquante(duree: float) -> None:
    with pytest.raises(
        ValueError, match=complet("durée nulle, négative ou manquante dans les essais")
    ):
        sa.tableau_acceleration({1: [8.0, 9.0], 2: [4.0, duree]})


def test_tableau_acceleration_leve_une_erreur_pour_des_nombres_d_essais_inegaux() -> None:
    with pytest.raises(ValueError):  # pandas refuse des colonnes de longueurs différentes
        sa.tableau_acceleration({1: [8.0, 9.0], 2: [4.0]})


def test_tableau_acceleration_refuse_l_absence_de_toute_mesure() -> None:
    with pytest.raises(ValueError, match=complet("aucun réglage mesuré")):
        sa.tableau_acceleration({})


def test_comparer_votes_aligne_les_deux_moteurs_et_compte_un_tag_absent_comme_zero() -> None:
    noms = ["Action", "Indie", "RPG"]
    spark_ = pd.Series({"Action": 10, "Indie": 4, "RPG": 0})
    pandas_ = pd.Series({"Action": 10, "Indie": 4, "RPG": 0})
    comparaison = sa.comparer_votes(spark_, pandas_, noms)
    assert list(comparaison.columns) == ["spark", "pandas"]
    assert comparaison["spark"].tolist() == comparaison["pandas"].tolist() == [10, 4, 0]
    # « RPG » sans vote côté Spark (absent) : compté 0, comme dans pandas.
    sans_rpg = sa.comparer_votes(pd.Series({"Action": 10, "Indie": 4}), pandas_, noms)
    assert sans_rpg.loc["RPG", "spark"] == 0


def test_comparer_votes_refuse_un_total_different() -> None:
    noms = ["Action", "Indie"]
    spark_ = pd.Series({"Action": 10, "Indie": 4})
    pandas_ = pd.Series({"Action": 10, "Indie": 5})
    with pytest.raises(
        ValueError, match=complet("totaux de votes différents entre Spark et pandas (écart 1)")
    ):
        sa.comparer_votes(spark_, pandas_, noms)


def test_comparer_votes_refuse_un_tag_voté_dans_spark_et_absent_de_pandas() -> None:
    spark_ = pd.Series({"Action": 10, "Indie": 4})
    pandas_ = pd.Series({"Action": 10, "Indie": 0})  # « Indie » compté 0 par pandas
    with pytest.raises(
        ValueError, match=complet("totaux de votes différents entre Spark et pandas (écart 4)")
    ):
        sa.comparer_votes(spark_, pandas_, ["Action", "Indie"])


def test_comparer_votes_refuse_un_nombre_de_tags_different_de_celui_attendu() -> None:
    spark_ = pd.Series({"Action": 10, "Indie": 4})
    pandas_ = pd.Series({"Action": 10, "Indie": 4})
    with pytest.raises(ValueError, match=complet("2 tags comparés au lieu de 3 attendus")):
        sa.comparer_votes(spark_, pandas_, ["Action", "Indie", "RPG"])


def test_comparer_votes_refuse_d_autres_tags_que_ceux_attendus_meme_en_nombre_egal() -> None:
    # Les deux moteurs s'accordent, et le nombre de tags est le bon, mais ce ne sont pas les tags
    # attendus (« RPG » manque, « Indie » est en trop) : l'accord ne prouve rien.
    spark_ = pd.Series({"Action": 10, "Indie": 4})
    pandas_ = pd.Series({"Action": 10, "Indie": 4})
    with pytest.raises(ValueError, match=complet("tags comparés différents des tags attendus")):
        sa.comparer_votes(spark_, pandas_, ["Action", "RPG"])


def test_comparer_votes_refuse_une_liste_attendue_qui_nomme_deux_fois_le_meme_tag() -> None:
    # Les deux moteurs s'accordent sur deux tags, mais la liste attendue en annonce trois, dont
    # « Action » deux fois : le compte ne correspond pas, la comparaison est refusée.
    spark_ = pd.Series({"Action": 10, "Indie": 4})
    pandas_ = pd.Series({"Action": 10, "Indie": 4})
    with pytest.raises(ValueError, match=complet("2 tags comparés au lieu de 3 attendus")):
        sa.comparer_votes(spark_, pandas_, ["Action", "Indie", "Action"])


def test_comparer_votes_laisse_les_series_d_entree_inchangees() -> None:
    spark_ = pd.Series({"Action": 10.0, "Indie": 4.0})
    pandas_ = pd.Series({"Action": 10.0, "Indie": 4.0})
    avant = (spark_.copy(), pandas_.copy())
    sa.comparer_votes(spark_, pandas_, ["Action", "Indie"])
    pd.testing.assert_series_equal(spark_, avant[0])
    pd.testing.assert_series_equal(pandas_, avant[1])


PLAN_LOCAL = (
    "FileScan json [data#12] Batched: false, Format: JSON, Location: "
    "InMemoryFileIndex(1 paths)[file:/C:/Dossier avec espaces/2_Steam/donnees/"
    "steam_game_output.json], PartitionFilters: [], ReadSchema: struct<data:struct<tags:struct<>>>"
)


def test_plan_sans_chemin_de_machine_remplace_le_chemin_avec_espaces_ou_tronque() -> None:
    propre = sa.plan_sans_chemin_de_machine(PLAN_LOCAL)
    assert "[donnees/steam_game_output.json], PartitionFilters" in propre
    assert "file:" not in propre
    assert "espaces" not in propre
    tronque = "Location: InMemoryFileIndex(1 paths)[file:/D:/essai/très/long/chemin/...], Format"
    # Le chemin tronqué par Spark (« ... ») est remplacé en entier.
    assert sa.plan_sans_chemin_de_machine(tronque) == (
        "Location: InMemoryFileIndex(1 paths)[donnees/steam_game_output.json], Format"
    )


def test_plan_sans_chemin_de_machine_rend_tel_quel_un_plan_sans_chemin() -> None:
    plan = "HashAggregate(keys=[tag#1], functions=[sum(votes#2)])\n+- Exchange hashpartitioning"
    assert sa.plan_sans_chemin_de_machine(plan) == plan


@pytest.mark.parametrize(
    "plan",
    [
        "Location: [file:/tmp/autre_fichier.csv]",  # un chemin que l'on ne sait pas masquer
        "cache dans /Users/nom/spark-warehouse",  # un dossier personnel sans « file: »
    ],
)
def test_plan_sans_chemin_de_machine_refuse_un_chemin_qui_subsiste(plan: str) -> None:
    with pytest.raises(
        ValueError, match=complet("un chemin de machine subsiste dans le plan d'exécution")
    ):
        sa.plan_sans_chemin_de_machine(plan)


# ------------------------------------------------------------------------------------------
# Spark : session, schéma, lecture
# ------------------------------------------------------------------------------------------


def test_obtenir_spark_regle_la_session_locale_et_reprend_celle_de_databricks(
    spark: SparkSession,
) -> None:
    assert spark.sparkContext.appName == "Steam_Bloc2_CDSD"
    assert spark.sparkContext.master == "local[*]"  # le maître par défaut, celui du notebook
    assert spark.conf.get("spark.sql.shuffle.partitions") == "8"
    assert spark.conf.get("spark.ui.enabled") == "false"
    assert os.environ["JAVA_HOME"] == str(jdk4py.JAVA_HOME)
    assert sa.obtenir_spark(True) is spark  # sur Databricks : la session existante


def test_obtenir_spark_pose_java_home_meme_quand_la_variable_manque(
    spark: SparkSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sur une machine sans Java installé, JAVA_HOME n'existe pas : obtenir_spark doit le poser
    # lui-même vers le Java de jdk4py, sinon Spark ne démarre pas.
    monkeypatch.delenv("JAVA_HOME", raising=False)
    assert sa.obtenir_spark(False) is spark  # la session déjà ouverte est reprise
    assert os.environ["JAVA_HOME"] == str(jdk4py.JAVA_HOME)


def test_champs_struct_suit_le_chemin_et_rend_les_champs_de_la_structure() -> None:
    tags = StructType([StructField("Action", StringType()), StructField("Indie", StringType())])
    data = StructType([StructField("name", StringType()), StructField("tags", tags)])
    schema = StructType([StructField("data", data)])
    assert [c.name for c in sa.champs_struct(schema, "data")] == ["name", "tags"]
    assert [c.name for c in sa.champs_struct(schema, "data", "tags")] == ["Action", "Indie"]
    assert [c.name for c in sa.champs_struct(tags)] == ["Action", "Indie"]  # sans chemin


def test_champs_struct_leve_une_erreur_si_le_champ_n_est_pas_une_structure() -> None:
    schema = StructType([StructField("data", StructType([StructField("name", StringType())]))])
    with pytest.raises(TypeError, match=complet("« data.name » n'est pas une structure : string")):
        sa.champs_struct(schema, "data", "name")
    with pytest.raises(
        TypeError, match=complet("avant « suite », ce n'est pas une structure : string")
    ):
        sa.champs_struct(schema, "data", "name", "suite")


def test_champs_struct_leve_une_erreur_pour_un_champ_absent_du_chemin() -> None:
    # Une faute de frappe dans le chemin (« tag » au lieu de « tags ») est refusée : elle ne
    # rend pas une liste de champs vide, qui ferait chronométrer un calcul sans aucun tag.
    tags = StructType([StructField("Action", StringType())])
    schema = StructType([StructField("data", StructType([StructField("tags", tags)]))])
    with pytest.raises(KeyError, match=complet("'No StructField named tag'")):
        sa.champs_struct(schema, "data", "tag")


def test_premiere_ligne_rend_la_premiere_ligne(spark: SparkSession) -> None:
    donnees = spark.createDataFrame([(3, "c"), (1, "a")], ["n", "lettre"]).orderBy("n")
    ligne = sa.premiere_ligne(donnees)
    assert (ligne["n"], ligne["lettre"]) == (1, "a")


def test_premiere_ligne_leve_une_erreur_sur_un_dataframe_vide(spark: SparkSession) -> None:
    vide = spark.createDataFrame([], "n int")
    with pytest.raises(ValueError, match=complet("DataFrame vide : aucune première ligne")):
        sa.premiere_ligne(vide)


# ------------------------------------------------------------------------------------------
# Spark : nettoyage de petits enregistrements qui reproduisent les défauts du fichier
# ------------------------------------------------------------------------------------------


def enregistrement(appid: int, **champs: Any) -> dict[str, Any]:
    """Un enregistrement du fichier Steam : identifiant, puis objet imbriqué `data`."""
    data: dict[str, Any] = {
        "appid": appid,
        "name": f"Jeu {appid}",
        "developer": "Dev",
        "publisher": "Edit",
        "genre": "Action, Indie",
        "languages": "English, French",
        "categories": ["Single-player"],
        "platforms": {"windows": True, "mac": False, "linux": True},
        "positive": 90,
        "negative": 10,
        "ccu": 5,
        "price": 999,
        "initialprice": 1999,
        "discount": 50,
        "required_age": 0,
        "release_date": "2021/05/14",
        "owners": "20,000 .. 50,000",
        "type": "game",
        "tags": {"Action": 3, "Indie": 1},
    }
    data.update(champs)
    return {"id": str(appid), "data": data}


def lire_json(
    spark: SparkSession, dossier: Path, enregistrements: list[dict[str, Any]]
) -> DataFrame:
    """Écrit les enregistrements comme le fichier réel (une liste JSON sur une ligne) et les lit."""
    chemin = dossier / "mini_steam.json"
    chemin.write_text(json.dumps(enregistrements), encoding="utf-8")
    return spark.read.json(str(chemin))


@pytest.fixture(scope="module")
def brut(spark: SparkSession, tmp_path_factory: pytest.TempPathFactory) -> DataFrame:
    enregistrements = [
        enregistrement(1),  # le cas ordinaire
        enregistrement(2, publisher="", developer="  ", languages="", release_date=""),
        enregistrement(
            3,
            developer="Valve\t\t\u00a0",
            release_date="2000/11/1",
            price=0,
            initialprice=0,
            discount=0,
        ),  # fmt: skip
        enregistrement(4, languages="German[b]*[/b], English\r\n, ,French", release_date="2019/01"),
        enregistrement(
            5, owners="1,000,000 .. 2,000,000", required_age=21, release_date="n'importe"
        ),
        enregistrement(6, positive=0, negative=0, owners="inconnu", required_age=180),
        enregistrement(7, required_age=22, languages="English"),
        enregistrement(8, required_age=35, positive=1, negative=0, type="hardware"),
        enregistrement(9, required_age=18, positive=3, negative=1, price=15000),
        enregistrement(10, release_date="2019/07"),  # sans jour, un autre mois que janvier
    ]
    return lire_json(spark, tmp_path_factory.mktemp("steam"), enregistrements)


# Les tableaux calculés par le module sont recalculés pour CHAQUE test (portée « function ») :
# avec un calcul partagé par tout le fichier, l'outil de mutation n'attribuerait la fonction
# testée qu'au premier test qui l'appelle, et les autres ne seraient jamais confrontés aux
# mutants (mesuré le 17/10 : des dizaines de mutants survivaient ainsi).
@pytest.fixture
def aplati(brut: DataFrame) -> dict[int, dict[str, Any]]:
    return {r["appid"]: r.asDict() for r in sa.aplatir_jeux(brut).collect()}


@pytest.fixture
def derives(brut: DataFrame) -> dict[int, dict[str, Any]]:
    jeux = sa.ajouter_champs_derives(sa.aplatir_jeux(brut))
    return {r["appid"]: r.asDict() for r in jeux.collect()}


def test_espaces_normalises_et_texte_ou_nul_nettoient_les_textes(spark: SparkSession) -> None:
    donnees = spark.createDataFrame(
        [
            ("Ubisoft Entertainment\t\t\t\t",),
            ("  deux   mots\u00a0\u00a0ici ",),
            ("",),
            ("   ",),
            (None,),
            ("propre",),
        ],
        ["t"],
    )
    lignes = donnees.select(
        sa.espaces_normalises(F.col("t")).alias("normalise"), sa.texte_ou_nul("t").alias("ou_nul")
    ).collect()
    assert [r["normalise"] for r in lignes] == [
        "Ubisoft Entertainment",
        "deux mots ici",
        "",
        "",
        None,
        "propre",
    ]
    assert [r["ou_nul"] for r in lignes] == [
        "Ubisoft Entertainment",
        "deux mots ici",
        None,
        None,
        None,
        "propre",
    ]


def test_texte_ou_nul_leve_une_erreur_pour_une_colonne_absente(spark: SparkSession) -> None:
    donnees = spark.createDataFrame([("a",)], ["t"])
    with pytest.raises(AnalysisException, match="absente"):
        donnees.select(sa.texte_ou_nul("absente"))


def test_aplatir_jeux_extrait_les_champs_garde_l_ordre_et_convertit_les_prix_en_dollars(
    aplati: dict[int, dict[str, Any]],
) -> None:
    jeu = aplati[1]
    assert list(jeu) == [
        "appid", "nom", "developpeur", "editeur", "genres_texte", "langues_texte", "categories",
        "windows", "mac", "linux", "avis_positifs", "avis_negatifs", "joueurs_simultanes",
        "prix_usd", "prix_initial_usd", "remise_pct", "age_requis_brut", "date_texte",
        "proprietaires_texte", "type",
    ]  # fmt: skip
    assert jeu["nom"] == "Jeu 1"
    assert jeu["prix_usd"] == pytest.approx(9.99)  # 999 cents
    assert jeu["prix_initial_usd"] == pytest.approx(19.99)
    assert jeu["remise_pct"] == 50
    assert (jeu["windows"], jeu["mac"], jeu["linux"]) == (True, False, True)
    assert jeu["genres_texte"] == "Action, Indie"
    assert jeu["age_requis_brut"] == 0
    assert jeu["date_texte"] == "2021/05/14"
    assert jeu["type"] == "game"
    assert aplati[9]["prix_usd"] == pytest.approx(150.0)
    assert aplati[3]["prix_initial_usd"] == 0.0  # jeu gratuit


def test_aplatir_jeux_remplace_un_texte_vide_par_nul_et_normalise_les_espaces(
    aplati: dict[int, dict[str, Any]],
) -> None:
    assert aplati[2]["editeur"] is None  # chaîne vide : pas un éditeur
    assert aplati[2]["developpeur"] is None  # espaces seuls
    assert aplati[3]["developpeur"] == "Valve"  # tabulations et espace insécable retirés
    assert aplati[1]["editeur"] == "Edit"


def test_aplatir_jeux_leve_une_erreur_si_le_fichier_n_a_pas_l_objet_data(
    spark: SparkSession,
) -> None:
    plat = spark.createDataFrame([(1, "x")], ["appid", "name"])
    with pytest.raises(AnalysisException, match="data"):
        sa.aplatir_jeux(plat)


def test_aplatir_jeux_leve_une_erreur_si_data_n_est_pas_une_structure(
    spark: SparkSession,
) -> None:
    # Un fichier où « data » serait un simple texte (JSON aplati ou mal exporté) est refusé par
    # Spark dès la sélection : aucun champ n'est inventé à NULL.
    texte = spark.createDataFrame([('{"appid": 1}',)], ["data"])
    with pytest.raises(AnalysisException, match="data"):
        sa.aplatir_jeux(texte)


def test_liste_langues_retire_les_balises_et_compte_zero_pour_une_liste_vide_ou_absente(
    derives: dict[int, dict[str, Any]], spark: SparkSession
) -> None:
    assert derives[1]["langues"] == ["English", "French"]
    assert derives[4]["langues"] == ["German", "English", "French"]  # balises, \r\n, nom vide
    assert derives[7]["langues"] == ["English"]
    assert derives[2]["langues"] == []
    assert derives[2]["nb_langues"] == 0  # et non 1 : un split de chaîne vide donne [""]
    assert derives[1]["nb_langues"] == len(derives[1]["langues"])
    assert derives[4]["nb_langues"] == len(derives[4]["langues"])
    schema = StructType([StructField("langues", ArrayType(StringType()))])
    absentes = spark.createDataFrame([(None,), (["a", "b"],), ([],)], schema)
    lignes = absentes.select(sa.nombre_de_langues("langues").alias("n")).collect()
    comptes = [r["n"] for r in lignes]
    assert comptes == [0, 2, 0]  # absente : 0 (et non -1) ; ["a", "b"] : 2 ; vide : 0


def test_liste_langues_leve_une_erreur_pour_une_colonne_absente(spark: SparkSession) -> None:
    donnees = spark.createDataFrame([("English",)], ["texte"])
    with pytest.raises(AnalysisException, match="absente"):
        donnees.select(sa.liste_langues("absente"))


def test_ajouter_champs_derives_lit_les_trois_formats_de_date_et_rend_nul_le_reste(
    derives: dict[int, dict[str, Any]],
) -> None:
    assert derives[1]["date_sortie"] == datetime.date(2021, 5, 14)
    assert derives[3]["date_sortie"] == datetime.date(2000, 11, 1)  # jour sur un chiffre
    assert derives[4]["date_sortie"] == datetime.date(2019, 1, 1)  # sans jour : le 1er du mois
    # Juillet, et non janvier : « M » est le mois (« m » serait la minute, et donnerait janvier).
    assert derives[10]["date_sortie"] == datetime.date(2019, 7, 1)
    assert derives[2]["date_sortie"] is None  # date vide
    assert derives[5]["date_sortie"] is None  # date illisible
    assert (derives[1]["annee"], derives[1]["mois"]) == (2021, 5)
    assert (derives[4]["annee"], derives[4]["mois"]) == (2019, 1)
    assert derives[2]["annee"] is None


def test_borne_proprietaires_retire_les_virgules_et_rend_nul_sans_tranche(
    derives: dict[int, dict[str, Any]],
) -> None:
    assert (derives[1]["proprietaires_min"], derives[1]["proprietaires_max"]) == (20000.0, 50000.0)
    assert derives[1]["proprietaires_milieu"] == 35000.0
    # « 1,000,000 » converti tel quel donnerait NULL.
    assert (derives[5]["proprietaires_min"], derives[5]["proprietaires_max"]) == (1e6, 2e6)
    assert derives[5]["proprietaires_milieu"] == 1.5e6
    assert derives[6]["proprietaires_min"] is None  # texte sans tranche
    assert derives[6]["proprietaires_max"] is None
    assert derives[6]["proprietaires_milieu"] is None


@pytest.mark.parametrize("position", [0, 3, -1])
def test_borne_proprietaires_leve_une_erreur_hors_des_positions_une_et_deux(position: int) -> None:
    with pytest.raises(
        ValueError, match=complet(f"position 1 (basse) ou 2 (haute) attendue, reçu {position}")
    ):
        sa.borne_proprietaires(position)


def test_avis_total_part_positive_et_score_de_wilson(
    derives: dict[int, dict[str, Any]],
) -> None:
    jeu = derives[1]  # 90 positifs, 10 négatifs
    assert jeu["total_avis"] == 100
    assert jeu["part_positive"] == pytest.approx(0.9)
    # borne basse de l'intervalle [0,8256 ; 0,9448] d'une proportion de 90 sur 100
    assert jeu["score_wilson"] == pytest.approx(0.8256, abs=1e-4)
    # 3 positifs sur 4 : la borne de Wilson est bien plus basse que la part brute (0,75).
    assert derives[9]["part_positive"] == pytest.approx(0.75)
    assert derives[9]["score_wilson"] == pytest.approx(0.3006, abs=1e-4)
    # Un seul avis, positif : part brute 100 %, mais borne de Wilson 1 / (1 + 1,96²) = 0,2065.
    assert derives[8]["total_avis"] == 1
    assert derives[8]["part_positive"] == pytest.approx(1.0)
    assert derives[8]["score_wilson"] == pytest.approx(1 / (1 + 1.96**2), abs=1e-4)
    # Aucun avis : ni part positive ni score (et non 0, qui ferait passer le jeu pour mauvais).
    assert derives[6]["total_avis"] == 0
    assert derives[6]["part_positive"] is None
    assert derives[6]["score_wilson"] is None


def test_score_wilson_penalise_les_petits_effectifs_et_suit_le_niveau_de_confiance(
    spark: SparkSession,
) -> None:
    donnees = spark.createDataFrame([(50, 1.0), (100000, 0.98), (100, 0.5)], ["n", "p"])
    scores = donnees.select(
        sa.score_wilson(F.col("n"), F.col("p")).alias("z95"),
        sa.score_wilson(F.col("n"), F.col("p"), z=2.576).alias("z99"),
    ).collect()
    petit, grand, moyen = scores
    assert petit["z95"] < grand["z95"]  # 100 % sur 50 avis ne passe pas devant 98 % sur 100 000
    assert petit["z95"] == pytest.approx(0.9286, abs=1e-4)
    assert grand["z95"] == pytest.approx(0.9791, abs=1e-4)
    assert moyen["z95"] == pytest.approx(0.4038, abs=1e-4)
    assert moyen["z99"] < moyen["z95"]  # un niveau de confiance plus élevé abaisse la borne


@pytest.mark.parametrize("z", [0.0, -1.96])
def test_score_wilson_refuse_un_quantile_z_nul_ou_negatif(spark: SparkSession, z: float) -> None:
    # z = 0 rendrait la part brute (100 % sur un avis repasserait en tête) ; z < 0 une « borne
    # basse » au-dessus de la part. Le refus a lieu avant tout calcul Spark, comme dans `wilson`.
    message = f"quantile z strictement positif attendu, reçu {z}"
    with pytest.raises(ValueError, match=complet(message)):
        sa.score_wilson(F.col("n"), F.col("p"), z=z)
    # Juste au-dessus du refus, z = 1 (environ 68 %) est permis : 50 sur 100 donne 0,4502.
    ligne = spark.createDataFrame([(100, 0.5)], ["n", "p"]).select(
        sa.score_wilson(F.col("n"), F.col("p"), z=1.0).alias("z68")
    ).first()
    assert ligne is not None and ligne["z68"] == pytest.approx(0.4502, abs=1e-4)


def test_part_positive_et_score_de_wilson_restent_nuls_sans_avis_meme_en_mode_ansi(
    spark: SparkSession,
) -> None:
    # En mode ANSI (Databricks SQL), une division par zéro lève une erreur : seul le garde
    # `total > 0` évite le calcul. Hors ANSI, le résultat serait NULL de toute façon.
    donnees = spark.createDataFrame([(0, 0), (3, 4)], "positifs int, total int")
    spark.conf.set("spark.sql.ansi.enabled", "true")
    try:
        lignes = donnees.select(
            sa.part_positive(F.col("positifs"), F.col("total")).alias("part"),
            sa.score_wilson(F.col("total"), F.col("positifs") / F.col("total")).alias("score"),
        ).collect()
    finally:
        spark.conf.set("spark.sql.ansi.enabled", "false")
    assert (lignes[0]["part"], lignes[0]["score"]) == (None, None)
    assert lignes[1]["part"] == pytest.approx(0.75)


def test_age_requis_garde_de_0_a_21_ans_et_rend_nul_au_dela(
    derives: dict[int, dict[str, Any]],
) -> None:
    assert derives[1]["age_requis"] == 0
    assert derives[9]["age_requis"] == 18
    assert derives[5]["age_requis"] == 21  # borne comprise
    assert derives[7]["age_requis"] is None  # 22 ans
    assert derives[6]["age_requis"] is None  # 180 ans, erreur de saisie
    assert derives[8]["age_requis"] is None  # 35 ans
    assert derives[6]["age_requis_brut"] == 180  # la valeur brute reste pour le décompte


def test_ajouter_champs_derives_ajoute_les_colonnes_dans_l_ordre_du_notebook(
    derives: dict[int, dict[str, Any]],
) -> None:
    ajoutees = list(derives[1])[20:]
    assert ajoutees == [
        "langues", "nb_langues", "date_sortie", "annee", "mois", "proprietaires_min",
        "proprietaires_max", "proprietaires_milieu", "total_avis", "part_positive",
        "score_wilson", "age_requis",
    ]  # fmt: skip


def test_ajouter_champs_derives_leve_une_erreur_sans_les_colonnes_aplaties(
    spark: SparkSession,
) -> None:
    incomplet = spark.createDataFrame([(1, "English")], ["appid", "langues_texte"])
    with pytest.raises(AnalysisException):
        sa.ajouter_champs_derives(incomplet)


# ------------------------------------------------------------------------------------------
# Spark : éditeurs
# ------------------------------------------------------------------------------------------


@pytest.fixture  # recalculée pour chaque test, pour la même raison qu'« aplati »
def table_editeurs(spark: SparkSession) -> DataFrame:
    editeurs = {
        1: "Ubisoft",
        2: "Ubisoft Entertainment\t\t\t\t",
        3: "Ubisoft®",
        4: "Ubisoft - San Francisco",
        5: "Cyan Worlds Inc, Ubisoft",
        6: "Strategy First Inc.",
        7: "Strategy First",
        8: "8floor",
        9: "8Floor",
        10: "8Floor",
        11: "Foo, Inc.",
        12: "-",
        13: None,
        14: "Bar Entertainment Inc.",
        15: "Bar",
        16: "Baz Entertainment Inc. Ltd.",
        # L'écriture la plus fréquente n'est pas la première dans l'ordre alphabétique :
        # « zeta Games » (deux fois) doit l'emporter sur « Zeta Games » (une fois).
        17: "zeta Games",
        18: "Zeta Games",
        19: "zeta Games",
        # Égalité (une fois chacune) : l'ordre alphabétique tranche, « ACME » avant « acme ».
        20: "acme",
        21: "ACME",
    }
    jeux = spark.createDataFrame(list(editeurs.items()), "appid int, editeur_texte string")
    jeux = jeux.select("appid", sa.texte_ou_nul("editeur_texte").alias("editeur"))
    return sa.editeurs_par_jeu(jeux)


def paires(table: DataFrame) -> set[tuple[int, str]]:
    return {(r["appid"], r["cle"]) for r in table.collect()}


def test_editeurs_par_jeu_regroupe_les_ecritures_et_decoupe_les_editeurs_multiples(
    table_editeurs: DataFrame,
) -> None:
    resultat = paires(table_editeurs)
    ubisoft = {appid for appid, cle in resultat if cle == "ubisoft"}
    assert ubisoft == {1, 2, 3, 4, 5}  # « Ubisoft », « Entertainment », « ® », « - San Francisco »
    assert {a for a, c in resultat if c == "strategy first"} == {6, 7}
    assert {a for a, c in resultat if c == "8floor"} == {8, 9, 10}  # insensible à la casse
    assert {a for a, c in resultat if c == "bar"} == {14, 15}  # deux suffixes retirés
    # Trois suffixes : deux passages en retirent deux, « Entertainment » reste (limite documentée).
    assert {a for a, c in resultat if c == "baz entertainment"} == {16}
    assert {c for a, c in resultat if a == 5} == {"cyan worlds", "ubisoft"}  # deux éditeurs
    assert {c for a, c in resultat if a == 11} == {"foo"}  # « Foo, Inc. » reste un seul éditeur


def test_editeurs_par_jeu_ecarte_les_noms_sans_lettre_ni_chiffre_et_les_valeurs_absentes(
    table_editeurs: DataFrame,
) -> None:
    appids = {a for a, _ in paires(table_editeurs)}
    assert 12 not in appids  # « - » n'est pas un éditeur
    assert 13 not in appids  # éditeur absent


def test_editeurs_par_jeu_affiche_l_ecriture_la_plus_frequente_et_garde_les_colonnes(
    table_editeurs: DataFrame,
) -> None:
    noms = {r["cle"]: r["editeur"] for r in table_editeurs.collect()}
    assert noms["8floor"] == "8Floor"  # deux fois « 8Floor », une fois « 8floor »
    assert noms["ubisoft"] == "Ubisoft"
    assert noms["strategy first"] == "Strategy First"  # « Strategy First Inc. » -> même nom
    assert noms["bar"] == "Bar"
    assert noms["zeta games"] == "zeta Games"  # la plus fréquente, pas la première par l'alphabet
    assert noms["acme"] == "ACME"  # égalité : ordre alphabétique (majuscules d'abord)
    assert set(table_editeurs.columns) == {"cle", "appid", "editeur_brut", "editeur"}


def test_editeurs_par_jeu_leve_une_erreur_sans_la_colonne_editeur(spark: SparkSession) -> None:
    sans_editeur = spark.createDataFrame([(1, "x")], ["appid", "nom"])
    with pytest.raises(AnalysisException, match="editeur"):
        sa.editeurs_par_jeu(sans_editeur)


# ------------------------------------------------------------------------------------------
# Spark : expérience de parallélisme
# ------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def brut_tags(spark: SparkSession, tmp_path_factory: pytest.TempPathFactory) -> DataFrame:
    enregistrements = [
        enregistrement(1, tags={"Action": 3, "Indie": 1, "RPG": 2}),
        enregistrement(2, tags={"Action": 4, "Indie": 0}),  # pas de « RPG » : NULL, pas 0
        enregistrement(3, tags={"Action": 5, "RPG": 7}),  # pas d'« Indie »
    ]
    return lire_json(spark, tmp_path_factory.mktemp("tags"), enregistrements)


def test_votes_par_tag_somme_les_votes_et_compte_les_jeux_qui_ont_vote(
    brut_tags: DataFrame,
) -> None:
    noms = [c.name for c in sa.champs_struct(brut_tags.schema, "data", "tags")]
    resultat = sa.vers_pandas(sa.votes_par_tag(brut_tags.select("data.tags"), noms))
    resultat = resultat.set_index("tag")
    assert resultat.loc["Action", "votes"] == 3 + 4 + 5
    assert resultat.loc["Action", "nb_jeux"] == 3
    assert resultat.loc["Indie", "votes"] == 1 + 0  # un vote nul compte comme jeu
    assert resultat.loc["Indie", "nb_jeux"] == 2  # le jeu 3 n'a pas de champ « Indie »
    assert resultat.loc["RPG", "votes"] == 2 + 7
    assert resultat.loc["RPG", "nb_jeux"] == 2
    assert sorted(resultat.index) == sorted(noms)


def test_votes_par_tag_leve_une_erreur_pour_un_tag_absent_du_schema(brut_tags: DataFrame) -> None:
    # Un nom de tag qui n'est pas un champ de la structure est une faute de frappe : Spark la
    # refuse, au lieu de rendre silencieusement zéro vote.
    with pytest.raises(AnalysisException, match="Inconnu"):
        sa.votes_par_tag(brut_tags.select("data.tags"), ["Action", "Inconnu"]).collect()


def test_votes_par_tag_leve_une_erreur_sans_aucun_nom_de_tag(brut_tags: DataFrame) -> None:
    with pytest.raises(ValueError, match=complet("aucun nom de tag à sommer")):
        sa.votes_par_tag(brut_tags.select("data.tags"), [])


def test_votes_par_tag_refuse_un_tag_demande_deux_fois(brut_tags: DataFrame) -> None:
    # Sans ce refus, « RPG » serait déplié deux fois : 18 votes au lieu de 9, 4 jeux au lieu de 2.
    with pytest.raises(ValueError, match=complet("tag(s) demandé(s) plusieurs fois : Indie, RPG")):
        sa.votes_par_tag(brut_tags.select("data.tags"), ["RPG", "Indie", "Action", "Indie", "RPG"])


def test_votes_par_tag_leve_une_erreur_si_la_source_n_est_pas_la_selection_des_tags(
    brut_tags: DataFrame,
) -> None:
    # La source attendue est brut.select("data.tags") (une colonne « tags ») ; passer le fichier
    # brut entier, oubli plausible, est refusé par Spark au lieu de rendre zéro vote.
    with pytest.raises(AnalysisException, match="tags"):
        sa.votes_par_tag(brut_tags, ["Action"])


def test_chronometrer_rend_une_duree_positive_par_essai_et_cinq_essais_par_defaut(
    brut_tags: DataFrame,
) -> None:
    noms = [c.name for c in sa.champs_struct(brut_tags.schema, "data", "tags")]
    trois = sa.chronometrer(brut_tags, noms, 2, essais=3)
    assert len(trois) == 3  # cardinal-ok: une durée par essai demandé (essais=3)
    # Trois jeux se calculent en bien moins d'une minute : une durée plus longue trahirait un
    # chronomètre faux (par exemple un instant de départ ajouté au lieu d'être retranché).
    assert all(0 < d < 60 for d in trois)
    un_seul = sa.chronometrer(brut_tags, noms, 2, essais=1)
    assert len(un_seul) == 1  # un essai est permis, zéro non
    par_defaut = sa.chronometrer(brut_tags, noms, 1)
    assert len(par_defaut) == 5  # cardinal-ok: défaut annoncé par le notebook, « cinq essais »
    assert all(isinstance(d, float) for d in par_defaut)


@pytest.mark.parametrize(
    ("partitions", "essais", "message"),
    [
        (0, 5, "au moins une partition attendue, reçu 0"),
        (-2, 5, "au moins une partition attendue, reçu -2"),
        (2, 0, "au moins un essai attendu, reçu 0"),
        (2, -1, "au moins un essai attendu, reçu -1"),
    ],
)
def test_chronometrer_leve_une_erreur_sans_partition_ou_sans_essai(
    brut_tags: DataFrame, partitions: int, essais: int, message: str
) -> None:
    with pytest.raises(ValueError, match=complet(message)):
        sa.chronometrer(brut_tags, ["Action"], partitions, essais)


def test_chronometrer_refuse_une_liste_de_tags_vide_avant_tout_calcul_spark(
    spark: SparkSession,
) -> None:
    # La source n'a même pas de colonne « data » : si le refus venait après le premier calcul
    # Spark, on verrait une AnalysisException. On voit le ValueError : rien n'a été lancé, rien
    # n'est resté en cache.
    sans_tags = spark.createDataFrame([(1,)], ["appid"])
    with pytest.raises(ValueError, match=complet("aucun nom de tag à chronométrer")):
        sa.chronometrer(sans_tags, [], 2)


def test_chronometrer_refuse_un_tag_en_double_avant_tout_calcul_spark(spark: SparkSession) -> None:
    # Même source sans colonne « data » : le ValueError prouve que rien n'a été mis en cache.
    sans_tags = spark.createDataFrame([(1,)], ["appid"])
    with pytest.raises(ValueError, match=complet("tag(s) demandé(s) plusieurs fois : Action")):
        sa.chronometrer(sans_tags, ["Action", "RPG", "Action"], 2)


# ------------------------------------------------------------------------------------------
# Graphiques
# ------------------------------------------------------------------------------------------


@pytest.fixture
def figure_propre() -> Iterator[None]:
    plt.close("all")
    yield
    plt.close("all")


@pytest.fixture
def agrege(spark: SparkSession) -> DataFrame:
    return spark.createDataFrame([("a", 3), ("b", 5), ("c", 4)], ["lettre", "n"])


def test_vers_pandas_garde_colonnes_ordre_valeurs_et_types(agrege: DataFrame) -> None:
    resultat = sa.vers_pandas(agrege)
    assert list(resultat.columns) == ["lettre", "n"]
    assert resultat["lettre"].tolist() == ["a", "b", "c"]
    assert resultat["n"].tolist() == [3, 5, 4]
    assert str(resultat["n"].dtype) == "int64"  # un entier Spark reste un entier pandas
    decimal = sa.vers_pandas(agrege.selectExpr("n / 2 AS moitie"))
    assert decimal["moitie"].tolist() == [1.5, 2.5, 2.0]
    assert str(decimal["moitie"].dtype) == "float64"


def test_vers_pandas_rend_un_tableau_vide_qui_garde_ses_colonnes(agrege: DataFrame) -> None:
    vide = sa.vers_pandas(agrege.filter("n > 100"))
    assert vide.empty
    assert list(vide.columns) == ["lettre", "n"]  # et non un tableau sans colonnes


def test_topandas_de_spark_echoue_sous_w_error_et_vers_pandas_le_remplace(
    agrege: DataFrame,
) -> None:
    # Prémisse de vers_pandas : sous Python 3.12, toPandas() de Spark 3.5 passe par distutils,
    # soit absent (ModuleNotFoundError), soit fourni par setuptools et obsolète
    # (DeprecationWarning, transformé en erreur par `-W error::DeprecationWarning`). vers_pandas, lui, passe.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises((DeprecationWarning, ModuleNotFoundError), match="distutils"):
            agrege.toPandas()
        assert sa.vers_pandas(agrege)["n"].tolist() == [3, 5, 4]


def appeler(agrege: DataFrame, monkeypatch: pytest.MonkeyPatch, **options: Any) -> Any:
    appels: list[str] = []
    monkeypatch.setattr(plt, "show", lambda: appels.append("show"))
    # Une colonne numérique de plus, que le graphique doit ignorer : seule « n » est tracée.
    avec_autre = agrege.withColumn("autre", F.lit(100))
    sa.montrer_graphique(avec_autre, "Titre", "lettre", "n", databricks=False, **options)
    assert appels == ["show"]  # le graphique est bien montré une fois
    return plt.gca()


def test_montrer_graphique_barres_horizontales_la_premiere_ligne_est_en_haut(
    agrege: DataFrame, monkeypatch: pytest.MonkeyPatch, figure_propre: None
) -> None:
    ax = appeler(agrege, monkeypatch, etiquette_x="Nombre", etiquette_y="Lettre")
    assert ax.get_title() == "Titre"
    assert (ax.get_xlabel(), ax.get_ylabel()) == ("Nombre", "Lettre")
    assert [t.get_text() for t in ax.get_yticklabels()] == ["c", "b", "a"]  # ordre inversé
    assert [p.get_width() for p in ax.patches] == [4, 5, 3]
    assert ax.get_legend() is None
    assert to_hex(ax.patches[0].get_facecolor()) == to_hex("steelblue")
    assert tuple(plt.gcf().get_size_inches()) == (10.0, 6.0)


def test_montrer_graphique_barres_verticales_courbe_et_etiquettes_vides_par_defaut(
    agrege: DataFrame, monkeypatch: pytest.MonkeyPatch, figure_propre: None
) -> None:
    barres = appeler(agrege, monkeypatch, type_graphique="bar")
    assert [t.get_text() for t in barres.get_xticklabels()] == ["a", "b", "c"]
    assert [p.get_height() for p in barres.patches] == [3, 5, 4]
    assert barres.get_legend() is None
    assert to_hex(barres.patches[0].get_facecolor()) == to_hex("steelblue")
    assert (barres.get_xlabel(), barres.get_ylabel()) == ("", "")  # jamais le nom d'une colonne
    plt.close("all")
    axe_courbe = appeler(agrege, monkeypatch, type_graphique="line")
    assert len(axe_courbe.lines) == 1  # « autre » n'est pas tracée
    courbe = axe_courbe.lines[0]
    assert courbe.get_marker() == "o"
    assert list(courbe.get_ydata()) == [3, 5, 4]
    assert {"a", "b", "c"} <= {t.get_text() for t in axe_courbe.get_xticklabels()}  # axe x
    assert to_hex(courbe.get_color()) == to_hex("steelblue")
    assert plt.gca().get_legend() is None
    # La courbe est tracée dans la figure préparée (10 x 6, titrée), pas dans une nouvelle.
    assert courbe.axes.get_title() == "Titre"
    assert tuple(courbe.figure.get_size_inches()) == (10.0, 6.0)


def test_montrer_graphique_leve_une_erreur_pour_une_colonne_absente(
    agrege: DataFrame, monkeypatch: pytest.MonkeyPatch, figure_propre: None
) -> None:
    monkeypatch.setattr(plt, "show", lambda: None)
    with pytest.raises(KeyError):
        sa.montrer_graphique(agrege, "T", "lettre", "colonne_absente", databricks=False)


def test_montrer_graphique_sur_databricks_appelle_display_sans_dessiner(
    agrege: DataFrame, figure_propre: None
) -> None:
    recus: list[DataFrame] = []
    sa.montrer_graphique(agrege, "T", "lettre", "n", databricks=True, afficher=recus.append)
    assert recus == [agrege]
    assert plt.get_fignums() == []  # aucune figure matplotlib sur Databricks


def test_montrer_graphique_sur_databricks_leve_une_erreur_sans_fonction_d_affichage(
    agrege: DataFrame, figure_propre: None
) -> None:
    with pytest.raises(
        ValueError, match=complet("sur Databricks, la fonction d'affichage display() est requise")
    ):
        sa.montrer_graphique(agrege, "T", "lettre", "n", databricks=True)
    assert plt.get_fignums() == []


@pytest.mark.parametrize("databricks", [False, True])
def test_montrer_graphique_refuse_un_type_de_graphique_inconnu(
    agrege: DataFrame, figure_propre: None, databricks: bool
) -> None:
    with pytest.raises(
        ValueError, match=complet("type de graphique inconnu : camembert (('barh', 'bar', 'line'))")
    ):
        sa.montrer_graphique(
            agrege, "T", "lettre", "n", "camembert", databricks=databricks, afficher=print
        )
    assert plt.get_fignums() == []
