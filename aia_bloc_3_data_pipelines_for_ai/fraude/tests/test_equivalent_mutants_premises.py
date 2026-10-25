# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Les faits de bibliothèque et de langage sur lesquels reposent les mutants équivalents nommés.

``MUTANTS_EQUIVALENTS.md`` nomme chaque mutant que ``mutmut`` laisse survivre parce qu'aucun
test ne peut le distinguer de l'original, avec la preuve de son équivalence. Plusieurs de ces
preuves s'appuient sur un fait que le projet ne maîtrise pas (un paramètre par défaut de
scikit-learn, la normalisation des noms de codec par Python, le comportement de
``json.dumps`` avec ``None``...). Chaque test ci-dessous contrôle un de ces faits. Si une mise
à jour d'une bibliothèque le change, le test échoue, et le mutant correspondant est à
réexaminer : il pourrait devenir détectable, ou l'original pourrait avoir changé de comportement.
"""

from __future__ import annotations

import codecs
import gzip
import inspect
import json
import logging
import math
import secrets
import sqlite3
import sys
from bisect import bisect_left
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import precision_recall_curve, recall_score
from sklearn.preprocessing import FunctionTransformer, KBinsDiscretizer, OrdinalEncoder

from detection_fraude.fonctionnalites import (
    FENETRE_COMPTE_S,
    FENETRE_MOYENNE_S,
    HistoriqueCarte,
    ajouter_historique_cartes,
)
from detection_fraude.jeu_donnees import preparer_jeu_entrainement
from detection_fraude.masquage import HacheurCarte, _tronquer
from detection_fraude.schema import _controle_brut
from tests.helpers import fabriquer_jeu


# --------------------------------------------------------------------------------------------
# Python et bibliothèque standard
# --------------------------------------------------------------------------------------------
def test_json_dumps_lit_ensure_ascii_comme_un_booleen_none_vaut_false():
    """``ensure_ascii=None`` écrit les accents tels quels, comme ``False`` (et pas ``\\u00e9``)."""
    valeur = {"commerçant": "Café Zéro", "liste": ["é", 1, None]}
    avec_none = json.dumps(valeur, ensure_ascii=None, indent=2)
    assert avec_none == json.dumps(valeur, ensure_ascii=False, indent=2)
    assert "é" in avec_none
    assert json.dumps(valeur, ensure_ascii=True) != avec_none  # la vraie valeur « vrai » se voit


@pytest.mark.parametrize("nom", ["utf-8", "ascii"])
def test_python_normalise_la_casse_des_noms_de_codec(nom):
    """``"UTF-8"`` et ``"ASCII"`` désignent les mêmes codecs que ``"utf-8"`` et ``"ascii"``."""
    assert codecs.lookup(nom.upper()) is codecs.lookup(nom)
    assert "Zéro".encode(nom.upper(), "replace") == "Zéro".encode(nom, "replace")
    assert "1234".encode(nom.upper()).decode(nom.upper()) == "1234"
    with pytest.raises(LookupError):  # un nom qui n'est pas un codec, lui, est refusé
        codecs.lookup(f"XX{nom}XX")


def test_gzip_open_ouvre_en_lecture_binaire_par_defaut(tmp_path):
    """Sans mode, ``gzip.open`` lit en binaire : ``"rb"`` est la valeur par défaut."""
    assert inspect.signature(gzip.open).parameters["mode"].default == "rb"
    fichier = tmp_path / "a.gz"
    with gzip.open(fichier, "wb") as flux:
        flux.write(b"abc")
    with gzip.open(fichier) as flux:
        assert flux.read() == b"abc"


def test_popitem_last_none_retire_le_plus_ancien_comme_last_false():
    """``OrderedDict.popitem(last=None)`` lit ``last`` comme un booléen faux : premier entré."""
    assert OrderedDict([("a", 1), ("b", 2)]).popitem(last=None) == ("a", 1)
    assert OrderedDict([("a", 1), ("b", 2)]).popitem(last=False) == ("a", 1)


def test_un_gestionnaire_de_flux_sans_flux_ecrit_sur_stderr():
    """``StreamHandler(None)`` prend ``sys.stderr``, comme ``StreamHandler(sys.stderr)``."""
    assert logging.StreamHandler(None).stream is sys.stderr
    assert logging.StreamHandler(sys.stderr).stream is sys.stderr


def test_secrets_token_hex_sans_taille_donne_trente_deux_octets():
    """``secrets.token_hex(None)`` produit autant de caractères que ``token_hex(32)`` (64)."""
    assert secrets.DEFAULT_ENTROPY == 32
    assert len(secrets.token_hex(None)) == len(secrets.token_hex(32)) == 64


def test_float_de_none_leve_type_error_que_le_producteur_rattrape():
    """``float(None)`` lève ``TypeError`` : l'en-tête ``Retry-After`` absent donne ``None``."""
    with pytest.raises(TypeError):
        float(None)  # type: ignore[arg-type]


def test_bool_de_none_est_faux():
    """Une option facultative lue avec ``None`` par défaut vaut ``False``, comme avec ``False``."""
    assert bool(None) is False
    assert bool({}.get("option")) is bool({}.get("option", False))


def test_un_chemin_de_base_en_memoire_a_pour_parent_le_dossier_courant():
    """``Path(":memory:").parent`` est « . » : le créer avec ``exist_ok`` ne change rien."""
    assert Path(":memory:").parent == Path(".")
    assert Path(":memory:").parent.exists()


def test_un_instant_aware_garde_sa_difference_quel_que_soit_son_fuseau_d_expression():
    """``astimezone(None)`` ou ``astimezone(UTC)`` : la différence de deux instants est la même.

    Attention : ce fait ne dit rien de l'équivalence du mutant
    `supervision.x_controler__mutmut_51` (`.astimezone(UTC)` devenu `.astimezone(None)`).
    La différence est bien la même, mais la conversion vers le fuseau **local** peut échouer là
    où la conversion vers UTC réussit, et le mutant a été tué le 21 octobre 2026 sur ce point.
    Ce test ne justifie plus rien sur ce mutant ; il reste vrai, et le test du projet qui tue
    le mutant est ailleurs.
    """
    maintenant = datetime(2026, 10, 10, 7, 20, tzinfo=UTC)
    dernier = datetime.fromisoformat("2026-10-10T07:05:00+00:00")
    assert maintenant - dernier.astimezone(None) == maintenant - dernier.astimezone(UTC)
    naif = datetime.fromisoformat("2026-10-10T07:05:00")  # sans fuseau : lu comme heure locale
    assert maintenant - naif.astimezone(None) == maintenant - naif.astimezone(UTC)


# --------------------------------------------------------------------------------------------
# SQLAlchemy, pandas, numpy, scikit-learn
# --------------------------------------------------------------------------------------------


def test_pandas_accepte_utc_en_minuscules_pour_un_fuseau():
    """``tz="utc"`` et ``tz="UTC"`` désignent le même fuseau."""
    assert pd.Timestamp("1970-01-01", tz="utc") == pd.Timestamp("1970-01-01", tz="UTC")


def test_pandas_lit_l_orientation_records_sans_tenir_compte_de_la_casse():
    """``to_dict("RECORDS")`` donne la même liste de dictionnaires que ``to_dict("records")``."""
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    assert df.to_dict("RECORDS") == df.to_dict("records")


def test_pandas_ignore_kind_pour_un_tri_sur_plusieurs_colonnes():
    """Avec deux clés, le tri est un tri lexicographique stable : ``kind`` n'est pas utilisé."""
    df = pd.DataFrame({"a": [2, 1, 2, 1, 2], "b": [1, 2, 0, 3, 1], "c": list("vwxyz")})
    attendu = df.sort_values(["a", "b"], kind="stable")
    for kind in (None, "XXstableXX", "STABLE", "quicksort"):
        assert df.sort_values(["a", "b"], kind=kind).equals(attendu)
    assert df.sort_values(["a", "b"]).equals(attendu)


def test_pandas_lit_des_secondes_entieres_de_la_meme_facon_avec_ou_sans_utc():
    """Des secondes d'époque donnent mêmes heure et jour avec ``utc`` à None, False ou True."""
    secondes = pd.Series([0, 86_399, 86_400, 1_608_548_400, 1_608_595_199], dtype="int64")
    reference = pd.to_datetime(secondes, unit="s", utc=True)
    for utc in (None, False):
        lu = pd.to_datetime(secondes, unit="s", utc=utc)
        assert list(lu.dt.hour) == list(reference.dt.hour)
        assert list(lu.dt.dayofweek) == list(reference.dt.dayofweek)


def test_numpy_a_pour_defauts_float64_et_recherche_a_gauche():
    """``np.ones`` rend du float64 sans ``dtype`` ; ``np.searchsorted`` cherche à gauche."""
    assert np.ones(3, dtype=None).dtype == np.ones(3, dtype="float64").dtype == np.float64
    classe = np.array([1, 2, 2, 3])
    assert int(np.searchsorted(classe, 2)) == int(np.searchsorted(classe, 2, side="left")) == 1


def test_scikit_learn_a_pour_defauts_ceux_que_le_pipeline_ecrit_en_toutes_lettres():
    """``validate=False``, ``encode="onehot"`` et ``encoded_missing_value=nan`` : les défauts."""
    assert FunctionTransformer().validate is False
    assert KBinsDiscretizer().encode == "onehot"
    assert np.isnan(OrdinalEncoder().encoded_missing_value)


def test_le_rappel_de_la_courbe_ne_croit_jamais_quand_le_seuil_monte():
    """Le meilleur rappel admissible est donc toujours le premier : ``argmax`` rend l'indice 0."""
    generateur = np.random.default_rng(7)
    for _ in range(20):
        y = generateur.integers(0, 2, 200)
        if y.sum() == 0:
            continue
        scores = generateur.random(200)
        precision, rappel, _ = precision_recall_curve(y, scores)
        r = rappel[:-1]
        assert (np.diff(r) <= 0).all()
        admissibles = np.flatnonzero(precision[:-1] >= 0.0)
        assert int(np.argmax(r[admissibles])) == 0


def test_le_rappel_d_un_jeu_a_deux_classes_ne_depend_pas_de_zero_division():
    """Avec au moins un vrai positif, le rappel est défini : ``zero_division`` ne sert jamais."""
    y = np.array([0, 1, 1, 0, 1])
    decisions = np.array([0, 0, 1, 0, 0])
    valeurs = {
        recall_score(y, decisions, zero_division=0),
        recall_score(y, decisions, zero_division=1),
        recall_score(y, decisions),
        recall_score(y, decisions.astype(None)),
    }
    assert valeurs == {1 / 3}


# --------------------------------------------------------------------------------------------
# Propriétés du code du projet qui rendent certains mutants équivalents
# --------------------------------------------------------------------------------------------
def test_la_borne_haute_de_la_recherche_dichotomique_est_redondante():
    """``bisect_left(temps, t - fenêtre, 0, fin)`` rend le même indice que sans ``fin``.

    ``fin`` est le nombre de transactions strictement antérieures à ``t`` ; ``t - fenêtre`` est
    plus petit que ``t`` : toute transaction à partir de ``fin`` est donc au moins égale à ``t``,
    elle ne peut pas être avant ``t - fenêtre``. La borne ne retire rien à la recherche.
    """
    generateur = np.random.default_rng(11)
    for _ in range(300):
        temps = sorted(int(x) for x in generateur.integers(0, 6 * FENETRE_MOYENNE_S, 25))
        t = int(generateur.integers(0, 6 * FENETRE_MOYENNE_S))
        fin = bisect_left(temps, t)
        for fenetre in (FENETRE_COMPTE_S, FENETRE_MOYENNE_S):
            assert bisect_left(temps, t - fenetre, 0, fin) == bisect_left(temps, t - fenetre)
    historique = HistoriqueCarte()
    for instant in (100, 200, 200, 300):
        historique.ajouter(instant, 1.0)
    assert historique.statistiques(200) == (1, 1.0, 1)


def test_le_calcul_par_lot_ne_depend_pas_de_l_ordre_dans_lequel_les_cartes_sont_visitees():
    """Trier les groupes par clé (``sort=True``) change l'ordre de visite, pas le résultat."""
    generateur = np.random.default_rng(5)
    lignes: dict[str, Any] = {
        "carte_hash": [f"carte{c}" for c in generateur.integers(0, 6, 60)],
        "horodatage_s": generateur.integers(10_000, 10_000 + 3 * FENETRE_COMPTE_S, 60),
        "montant": np.round(generateur.uniform(1, 500, 60), 2),
    }
    df = pd.DataFrame(lignes)
    attendu = ajouter_historique_cartes(df)
    for sort in (None, True):
        visite = df.groupby("carte_hash", sort=sort).indices
        assert sorted(visite) == sorted(df.groupby("carte_hash", sort=False).indices)
    melange = df.iloc[::-1].reset_index(drop=True)  # ordre des cartes inversé
    inverse = ajouter_historique_cartes(melange).iloc[::-1].reset_index(drop=True)
    assert inverse["nb_tx_24h"].tolist() == attendu["nb_tx_24h"].tolist()
    assert np.allclose(inverse["montant_relatif"], attendu["montant_relatif"], rtol=1e-9)


def test_tronquer_laisse_inchange_tout_nombre_sous_mille_milliards():
    """Un horodatage en millisecondes de 12 chiffres ou moins passe tel quel par ``_tronquer``.

    La borne basse de ``_horodatage_ms_ou_masque`` (an 2000, soit 946 684 800 000) est sous
    10**12 : tout entier de [borne basse ; 10**12[ est rendu inchangé que la borne le laisse
    passer ou non. Seuls comptent donc 10**12 et la borne haute.
    """
    for valeur in (946_684_800_000, 946_684_800_001, 999_999_999_999):
        assert _tronquer(valeur) == valeur
    assert _tronquer(10**12) == "[MASQUE]"


def test_float_lit_nan_sans_tenir_compte_de_la_casse():
    """``float("NAN")`` est un NaN, comme ``float("nan")`` : ``evaluer`` rend la même valeur."""
    assert math.isnan(float("NAN")) and math.isnan(float("nan"))
    with pytest.raises(ValueError):  # un texte qui n'est pas un nombre, lui, est refusé
        float("XXnanXX")


def test_numpy_lit_le_genre_de_tri_sans_tenir_compte_de_la_casse():
    """``np.argsort(kind="STABLE")`` trie comme ``kind="stable"`` (ordre des égalités gardé)."""
    valeurs = np.array([3, 1, 2, 1, 3, 1])
    assert np.argsort(valeurs, kind="STABLE").tolist() == [1, 3, 5, 2, 0, 4]
    assert np.argsort(valeurs, kind="stable").tolist() == [1, 3, 5, 2, 0, 4]


@pytest.mark.parametrize(
    "instruction",
    ["PRAGMA synchronous=NORMAL", "pragma synchronous=normal", "PRAGMA SYNCHRONOUS=NORMAL"],
)
def test_sqlite_lit_les_pragmas_sans_tenir_compte_de_la_casse(tmp_path, instruction):
    """Les trois écritures passent une base fichier de FULL (2) à NORMAL (1)."""
    base = sqlite3.connect(str(tmp_path / "f.db"))
    try:
        assert base.execute("PRAGMA synchronous").fetchone() == (2,)
        base.execute(instruction)
        assert base.execute("PRAGMA synchronous").fetchone() == (1,)
    finally:
        base.close()


def test_un_numero_de_carte_de_8_a_19_caracteres_est_au_moins_dix_millions_ou_negatif():
    """Dans ``_controle_brut``, ``int(valeur) > 0`` ne sert qu'à refuser les négatifs.

    Un entier écrit sur 8 à 19 caractères vaut au moins 10 000 000 s'il est positif ; 0 et 1
    s'écrivent sur un seul caractère et sont déjà refusés par la longueur. ``> 0``, ``>= 0`` et
    ``> 1`` donnent donc la même décision pour toute valeur.
    """
    for valeur in (0, 1, 9_999_999, 10_000_000, -1_000_000, -12_345_678, 10**18, -(10**17)):
        chiffres = len(str(valeur))
        if 8 <= chiffres <= 19:
            assert valeur >= 10_000_000 or valeur < 0
            assert (valeur > 0) == (valeur >= 0) == (valeur > 1)
    assert _controle_brut("cc_num", 0) == _controle_brut("cc_num", 1) == "longueur_invalide"
    assert _controle_brut("cc_num", -12_345_678) == "longueur_invalide"
    assert _controle_brut("cc_num", 12_345_678) is None


def test_l_etiquette_du_jeu_d_entrainement_est_deja_un_entier():
    """``to_numpy(dtype=int)`` et ``to_numpy()`` donnent le même tableau d'entiers.

    ``is_fraud`` absent ou différent de 0 et 1 est refusé par la validation : chaque ligne
    retenue a une étiquette entière, la colonne est donc de type ``int64``.
    """
    jeu, _ = preparer_jeu_entrainement(fabriquer_jeu(nb_lignes=200), HacheurCarte(b"s" * 32, 16))
    assert jeu["etiquette_source"].dtype == np.int64
    assert (
        jeu["etiquette_source"].to_numpy().dtype
        == jeu["etiquette_source"].to_numpy(dtype=int).dtype
    )
