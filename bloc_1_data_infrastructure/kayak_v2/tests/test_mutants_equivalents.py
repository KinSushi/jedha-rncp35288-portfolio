# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Preuves d'équivalence des mutants qu'aucun test du projet ne peut distinguer.

Un mutant est ÉQUIVALENT quand le code modifié a exactement le même comportement que le code
d'origine : aucun test ne peut alors le faire échouer, et le chercher serait vain. Chaque test
ci-dessous établit, sur la bibliothèque concernée, l'égalité de comportement qui rend une famille de
mutants équivalente. La liste des mutants (fichier, ligne, mutation) est dans
``MUTANTS_EQUIVALENTS.md``, qui renvoie ici. Ces tests ne s'exécutent pas contre le code du projet :
ils ne font pas partie de la sélection de tests de la mutation.
"""

import argparse
import codecs
import io
import json
import random
import re
from urllib.parse import quote

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
import pytest
from IPython.display import Image
from PIL import Image as ImagePIL
from plotly import graph_objects as go
from plotly.subplots import make_subplots


def test_le_nom_d_encodage_utf8_ne_depend_pas_de_la_casse():
    """``"UTF-8"`` et ``"utf-8"`` sont le même codec : mêmes octets à l'encodage et au décodage."""
    assert codecs.lookup("UTF-8").name == codecs.lookup("utf-8").name
    texte = "Hôtel de l'été - œuvre"
    assert texte.encode("UTF-8") == texte.encode("utf-8")
    octets = texte.encode("utf-8")
    assert octets.decode("UTF-8") == octets.decode("utf-8") == texte


def test_ensure_ascii_none_se_comporte_comme_faux():
    """``ensure_ascii=None`` n'échappe pas les accents, comme ``ensure_ascii=False``."""
    donnees = {"ville": "Nîmes", "n": [1, 2]}
    assert json.dumps(donnees, ensure_ascii=None) == json.dumps(donnees, ensure_ascii=False)
    tampon_none, tampon_faux = io.StringIO(), io.StringIO()
    json.dump(donnees, tampon_none, ensure_ascii=None, indent=2)
    json.dump(donnees, tampon_faux, ensure_ascii=False, indent=2)
    assert tampon_none.getvalue() == tampon_faux.getvalue()


def test_to_csv_index_none_se_comporte_comme_faux():
    """``DataFrame.to_csv(index=None)`` n'écrit pas l'index, comme ``index=False``."""
    tableau = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    assert tableau.to_csv(index=None, lineterminator="\n") == tableau.to_csv(
        index=False, lineterminator="\n"
    )


def test_diviser_par_un_signe_unitaire_equivaut_a_multiplier():
    """Pour un signe de +1,0 ou -1,0, ``x * signe`` et ``x / signe`` donnent les mêmes flottants."""
    valeurs = pd.Series([-3.5, 0.0, 1.25, 10.0, 1e-9])
    for signe in (1.0, -1.0):
        assert (valeurs * signe).tolist() == (valeurs / signe).tolist()


def test_rank_ascending_none_se_comporte_comme_faux():
    """``Series.rank(ascending=None)`` classe comme ``ascending=False`` (valeur évaluée fausse)."""
    scores = pd.Series([0.3, 0.9, 0.1, 0.9])
    attendu = scores.rank(ascending=False, method="min").tolist()
    assert scores.rank(ascending=None, method="min").tolist() == attendu


# --- Ajouts du 17/10/2026 : faits de bibliothèque derrière les mutants équivalents restants ----


def test_split_premier_morceau_ne_depend_pas_de_maxsplit():
    """Le premier morceau d'un découpage est le même quel que soit ``maxsplit`` (1, 2 ou aucun).

    ``str.split``, ``re.split`` : seul le reste du découpage change, jamais l'élément ``[0]``.
    """
    for ligne in ["Disallow: /a # b # c", "sans diese", "#tout", "a#b"]:
        assert ligne.split("#", 1)[0] == ligne.split("#")[0] == ligne.split("#", 2)[0]
    for agent in ["KayakEtudeCertif/1.0 (contact)", "Robot x/2", "seul"]:
        motif = r"[/\s(]"
        assert re.split(motif, agent, maxsplit=1)[0] == re.split(motif, agent)[0]
        assert re.split(motif, agent, maxsplit=2)[0] == re.split(motif, agent, maxsplit=1)[0]


def test_lettres_majuscules_inserees_puis_retirees_ne_changent_rien():
    r"""Des « X » insérés entre les lettres d'un nom en minuscules disparaissent au filtrage
    ``[^a-z\s]`` qui suit : le nom normalisé est le même (mutant « XXXX ».join)."""
    for nom in ["aigues mortes", "uzes", "saintmalo", "l isle sur la sorgue"]:
        assert re.sub(r"[^a-z\s]", "", "XXXX".join(nom)) == re.sub(r"[^a-z\s]", "", "".join(nom))


def test_quote_ne_code_jamais_une_lettre():
    """``urllib.parse.quote`` ne code jamais une lettre : « X » ajouté à ``safe`` ne change rien."""
    for cle in ["abc123", "a+b/c=d&e", "XxX-é ", "clé secrète/42"]:
        assert quote(cle, safe="") == quote(cle, safe="XXXX")


def test_argparse_valeur_par_defaut_est_none():
    """Une option ``argparse`` sans ``default`` vaut None : ``default=None`` ne change rien."""
    avec, sans = argparse.ArgumentParser(), argparse.ArgumentParser()
    avec.add_argument("--date", default=None)
    sans.add_argument("--date")
    assert vars(avec.parse_args([])) == vars(sans.parse_args([])) == {"date": None}
    assert vars(avec.parse_args(["--date", "x"])) == vars(sans.parse_args(["--date", "x"]))


def test_pandas_tri_et_conversion_par_defaut():
    """``sort_values`` range les valeurs manquantes à la fin par défaut, et ``astype(None)``
    convertit en flottants comme ``astype(float)``."""
    tableau = pd.DataFrame({"a": [2.0, None, 1.0, None], "b": [1, 2, 3, 4]})
    pd.testing.assert_frame_equal(
        tableau.sort_values(by=["a"], ascending=[False]),
        tableau.sort_values(by=["a"], ascending=[False], na_position="last"),
    )
    serie = pd.Series([8.0, None, 5.0])
    pd.testing.assert_series_equal(serie.astype(None), serie.astype(float))


def test_numpy_empiler_selon_l_axe_moins_un_ou_un():
    """Pour des colonnes à une dimension, ``np.stack(..., axis=-1)`` égale ``axis=1``."""
    colonnes = [pd.Series(["a", "b"]), pd.Series(["1.0", "2.5"]), pd.Series(["?", "4"])]
    assert np.array_equal(np.stack(colonnes, axis=-1), np.stack(colonnes, axis=1))


def test_plotly_types_de_sous_figures_et_alias_map1():
    """Plotly lit le type d'une sous-figure sans tenir compte de la casse (« MAP », « TABLE »),
    et ``map1`` désigne la même carte que ``map``."""

    def grille(carte: str, tableau: str) -> go.Figure:
        return make_subplots(
            rows=2,
            cols=4,
            specs=[
                [{"type": carte, "colspan": 2}, None] * 2,
                [{"type": tableau, "colspan": 2}, None, {"type": "table", "colspan": 2}, None],
            ],
        )

    reference = grille("map", "table").to_dict()
    assert grille("MAP", "table").to_dict() == reference
    assert grille("map", "TABLE").to_dict() == reference
    figure_a, figure_b = go.Figure(grille("map", "table")), go.Figure(grille("map", "table"))
    figure_a.update_layout({"map": {"zoom": 7}})
    figure_b.update_layout({"map1": {"zoom": 7}})
    assert figure_a.to_dict() == figure_b.to_dict()


def test_pillow_reduction_de_couleurs_et_format_png():
    """Pillow : pour une image RGB, ``quantize`` réduit par défaut à 256 couleurs par la méthode
    MEDIANCUT ; et le nom de format « png » vaut « PNG » à l'enregistrement."""
    generateur = random.Random(1)
    image = ImagePIL.new("RGB", (40, 30))
    image.putdata([tuple(generateur.randrange(256) for _ in range(3)) for _ in range(1200)])
    reference = image.quantize(colors=256, method=ImagePIL.Quantize.MEDIANCUT)
    for variante in (
        image.quantize(colors=256, method=None),
        image.quantize(method=ImagePIL.Quantize.MEDIANCUT),
        image.quantize(colors=256),
    ):
        assert variante.tobytes() == reference.tobytes()
        assert variante.getpalette() == reference.getpalette()
    majuscules, minuscules = io.BytesIO(), io.BytesIO()
    reference.save(majuscules, format="PNG", optimize=True)
    reference.save(minuscules, format="png", optimize=True)
    assert majuscules.getvalue() == minuscules.getvalue()


def test_ipython_image_png_deduit_ou_normalise_son_format():
    """IPython : une image PNG en octets a le format « png », qu'il soit donné en minuscules, en
    majuscules, à None ou omis ; le rendu envoyé au notebook est le même."""
    tampon = io.BytesIO()
    ImagePIL.new("RGB", (4, 3), (10, 20, 30)).save(tampon, format="PNG")
    donnees = tampon.getvalue()
    images = [
        Image(data=donnees, format="png", width=1000),
        Image(data=donnees, format=None, width=1000),
        Image(data=donnees, width=1000),
        Image(data=donnees, format="PNG", width=1000),
    ]
    assert {image.format for image in images} == {"png"}
    rendus = {json.dumps(image._repr_mimebundle_(), sort_keys=True) for image in images}  # noqa: SLF001
    assert len(rendus) == 1


def test_pyarrow_ecrivains_csv_et_parquet():
    """pyarrow : ``write`` et ``write_batch`` écrivent la même chose (CSV et Parquet), et le CSV a
    son en-tête par défaut (options absentes, None, ou ``include_header=None``)."""
    lot = pa.RecordBatch.from_pylist([{"nom": 'H "A", N', "note": 9.1}, {"nom": "B", "note": None}])

    def csv(methode: str, **options: object) -> bytes:
        tampon = io.BytesIO()
        ecrivain = pacsv.CSVWriter(tampon, lot.schema, **options)
        getattr(ecrivain, methode)(lot)
        ecrivain.close()
        return tampon.getvalue()

    reference = csv("write", write_options=pacsv.WriteOptions(include_header=True))
    assert reference.startswith(b'"nom","note"')
    assert csv("write_batch", write_options=pacsv.WriteOptions(include_header=True)) == reference
    assert csv("write", write_options=None) == reference
    assert csv("write") == reference
    assert csv("write", write_options=pacsv.WriteOptions(include_header=None)) == reference

    def parquet(methode: str) -> pa.Table:
        tampon = io.BytesIO()
        ecrivain = pq.ParquetWriter(tampon, lot.schema)
        getattr(ecrivain, methode)(lot)
        ecrivain.close()
        return pq.read_table(io.BytesIO(tampon.getvalue()))

    assert parquet("write").equals(parquet("write_batch"))


@pytest.mark.filterwarnings(
    "ignore::DeprecationWarning:pyspark", "ignore::PendingDeprecationWarning:pyspark"
)
def test_spark_type_de_jointure_insensible_a_la_casse():
    """Spark lit le type de jointure sans tenir compte de la casse (« LEFT » vaut « left »), et
    une jointure sans type, ou de type None, est une jointure interne (« inner »)."""
    from kayak.etl.spark_etl import obtenir_spark  # session locale seulement (Java de jdk4py)

    spark = obtenir_spark()
    villes = spark.createDataFrame([(1, "Nice"), (2, "Lille")], ["id_ville", "ville"])
    agregats = spark.createDataFrame([(1, 20.5)], ["id_ville", "temp"])
    minuscules = sorted(villes.join(agregats, "id_ville", "left").collect())
    majuscules = sorted(villes.join(agregats, "id_ville", "LEFT").collect())
    assert minuscules == majuscules
    assert [tuple(ligne) for ligne in minuscules] == [(1, "Nice", 20.5), (2, "Lille", None)]
    # Sans type, ou avec None, la jointure est « inner », quelle que soit la casse écrite.
    interne = sorted(villes.join(agregats, "id_ville", "inner").collect())
    assert sorted(villes.join(agregats, "id_ville").collect()) == interne
    assert sorted(villes.join(agregats, "id_ville", None).collect()) == interne
    assert sorted(villes.join(agregats, "id_ville", "INNER").collect()) == interne
    assert [tuple(ligne) for ligne in interne] == [(1, "Nice", 20.5)]



@pytest.mark.filterwarnings(
    "ignore::DeprecationWarning:pyspark", "ignore::PendingDeprecationWarning:pyspark"
)
def test_spark_options_de_lecture_csv_insensibles_a_la_casse(tmp_path):
    """Spark lit le nom d'une option de lecture sans tenir compte de la casse : « HEADER » vaut
    « header » (la première ligne donne les noms de colonnes dans les deux cas)."""
    from kayak.etl.spark_etl import obtenir_spark

    chemin = tmp_path / "coordonnees.csv"
    chemin.write_text("id_ville,ville\n1,Nice\n2,Lille\n", encoding="utf-8")
    spark = obtenir_spark()
    minuscules = spark.read.option("header", True).csv(str(chemin))
    majuscules = spark.read.option("HEADER", True).csv(str(chemin))
    assert minuscules.columns == majuscules.columns == ["id_ville", "ville"]
    assert majuscules.collect() == minuscules.collect()


def test_typing_cast_ne_fait_rien_a_l_execution():
    """``typing.cast`` rend son second argument tel quel, quel que soit le type écrit : il ne
    sert qu'à mypy."""
    from typing import cast

    lignes = [{"a": 1}]
    for type_ecrit in ("list[dict[str, Any]]", "XXlist[dict[str, Any]]XX", "LIST", None):
        assert cast(type_ecrit, lignes) is lignes


def test_texte_ascii_identique_dans_tous_les_encodages_courants(tmp_path):
    """Une empreinte de cache S3 (ETag hexadécimal, date ISO) est en ASCII :
    UTF-8, cp1252 (Windows) et latin-1 l'écrivent avec les mêmes octets et la relisent pareil.
    L'encodage demandé pour la marque du cache ne change donc rien."""
    empreinte = "9b2cf535f27731c974343645a3985328|2026-10-10 12:00:00+00:00"
    encodages = ("utf-8", "cp1252", "latin-1")
    octets = {codecs.lookup(nom).name: empreinte.encode(nom) for nom in encodages}
    assert len(set(octets.values())) == 1
    fichier = tmp_path / "marque"
    fichier.write_bytes(octets["utf-8"])
    assert {fichier.read_text(encoding=nom) for nom in encodages} == {empreinte}


def test_sqlalchemy_valeurs_par_defaut_des_colonnes_et_des_index():
    """SQLAlchemy : ``nullable=None`` donne NOT NULL comme ``nullable=False`` ; une colonne de clé
    étrangère sans type prend celui de la colonne visée (INTEGER) ; un index sans nom reçoit le
    nom « ix_<table>_<colonne> »."""
    from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table
    from sqlalchemy import create_engine as moteur
    from sqlalchemy.schema import CreateIndex, CreateTable

    def ddl(nullable: object, type_explicite: bool, nom_index: str | None) -> str:
        meta = MetaData()
        Table("villes", meta, Column("id_ville", Integer, primary_key=True))
        colonnes = [Integer] if type_explicite else []
        hotels = Table(
            "hotels",
            meta,
            Column("hotel_id", Integer, primary_key=True),
            Column("id_ville", *colonnes, ForeignKey("villes.id_ville"), nullable=False),
            Column("nom", String(300), nullable=nullable),
            Column("note", Integer),
        )
        index = Index(nom_index, hotels.c.note)
        sqlite = moteur("sqlite://")
        return str(CreateTable(hotels).compile(sqlite)) + str(CreateIndex(index).compile(sqlite))

    reference = ddl(False, True, "ix_hotels_note")
    assert "nom VARCHAR(300) NOT NULL" in reference and "id_ville INTEGER NOT NULL" in reference
    assert ddl(None, True, "ix_hotels_note") == reference
    assert ddl(False, False, "ix_hotels_note") == reference
    assert ddl(False, True, None) == reference


def test_sql_drop_view_insensible_a_la_casse_et_sans_trace():
    """``DROP VIEW IF EXISTS`` : SQLite lit les mots-clés et le nom non cité de la vue sans tenir
    compte de la casse, et ne garde nulle part le texte d'une suppression. Les trois écritures
    ne font rien sur une base sans vue, suppriment la même vue quand elle existe, et laissent le
    même schéma.

    Ce fait ne vaut QUE pour la suppression : le texte d'un ``CREATE VIEW`` est, lui, gardé tel
    quel dans ``sqlite_master`` ; sa casse change donc l'entrepôt, et elle est vérifiée par
    tests/test_spark_etl_hors_spark.py (définition exacte de la vue).
    """
    import sqlite3

    creation = "CREATE VIEW vue_hotels_destinations AS SELECT 1 AS un"

    def schemas_successifs(suppression: str) -> list[list[tuple[object, ...]]]:
        connexion = sqlite3.connect(":memory:")
        schemas = []
        for requete in (suppression, creation, suppression, creation):
            connexion.execute(requete)
            schema = connexion.execute("SELECT type, name, sql FROM sqlite_master")
            schemas.append(schema.fetchall())
        connexion.close()
        return schemas

    reference = schemas_successifs("DROP VIEW IF EXISTS vue_hotels_destinations")
    vue = [("view", "vue_hotels_destinations", creation)]
    assert reference == [[], vue, [], vue]  # rien à supprimer, création, suppression, création
    for variante in (
        "drop view if exists vue_hotels_destinations",
        "DROP VIEW IF EXISTS VUE_HOTELS_DESTINATIONS",
    ):
        assert schemas_successifs(variante) == reference


def test_pandas_to_dict_orientation_insensible_a_la_casse():
    """``DataFrame.to_dict("RECORDS")`` rend la même liste d'enregistrements que « records »."""
    tableau = pd.DataFrame({"a": [1, 2], "b": ["x", None]})
    assert tableau.to_dict("RECORDS") == tableau.to_dict("records")
