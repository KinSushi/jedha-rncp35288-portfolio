# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Refus du contrat de données (critères 4.1 et 4.2) : une règle, un test, une valeur exacte.

Ce que chaque test garantit, au-delà du simple « une erreur existe » :

* la liste d'erreurs est **exactement** celle attendue (champ, code, détail) ;
* la ligne reçue n'est pas modifiée par la validation (instantané pris ``avant``) : la file des
  rejets doit pouvoir archiver la ligne telle qu'elle est arrivée ;
* le détail ne recopie jamais la valeur d'un champ sensible.

L'horloge du module est figée (``INSTANT``) : les bornes de date se testent à la milliseconde.
"""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest

from detection_fraude import schema
from detection_fraude.schema import Erreur, valider_brut, valider_masque
from tests.helpers import MARQUEUR, ligne_brute

INSTANT = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
INSTANT_MS = 1_791_633_600_000  # INSTANT en millisecondes depuis l'époque
UN_JOUR_MS = 86_400_000
AN_2000_MS = 946_684_800_000

CHAMPS_BRUTS = (
    "cc_num",
    "merchant",
    "category",
    "amt",
    "first",
    "last",
    "gender",
    "street",
    "city",
    "state",
    "zip",
    "lat",
    "long",
    "city_pop",
    "job",
    "dob",
    "trans_num",
    "merch_lat",
    "merch_long",
    "current_time",
)
CHAMPS_MASQUES = (
    "version_schema",
    "trans_num",
    "horodatage_s",
    "carte_hash",
    "carte_fin",
    "commercant",
    "categorie",
    "montant",
    "etat",
    "population_ville",
    "age",
    "distance_km",
    "lat_commercant",
    "long_commercant",
    "source",
)


class HorlogeFigee(datetime):
    """``datetime`` dont ``now`` rend toujours ``INSTANT`` ; exige qu'on la lise en UTC."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        if tz is not UTC:
            raise AssertionError("la validation doit lire l'heure en UTC")
        return INSTANT


@pytest.fixture(autouse=True)
def horloge_figee(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fige l'heure vue par le module de schéma : aucune borne ne dépend de l'heure murale."""
    monkeypatch.setattr(schema, "datetime", HorlogeFigee)


def message_valide(**surcharges: Any) -> dict[str, Any]:
    """Un message masqué conforme, écrit en dur (indépendant du code de masquage)."""
    base: dict[str, Any] = {
        "version_schema": 1,
        "trans_num": "c7e1d0a94f3b42e8a65d1f0b9e2c7a14",
        "horodatage_s": 1_791_028_800,
        "carte_hash": "0123456789abcdef0123456789abcdef",
        "carte_fin": "1234",
        "commercant": "fraud_Marchand 3",
        "categorie": "grocery_pos",
        "montant": 42.5,
        "etat": "NY",
        "population_ville": 8_000_000,
        "age": 40,
        "distance_km": 10.783,
        "lat_commercant": 40.8,
        "long_commercant": -73.95,
        "source": "api",
        "etiquette_source": None,
    }
    base.update(surcharges)
    return base


# --------------------------------------------------------------------------------------------
# Ligne brute : forme générale
# --------------------------------------------------------------------------------------------
def test_ce_qui_n_est_pas_un_objet_est_refuse_en_nommant_son_type_et_reste_inchange():
    """Une ligne qui n'est pas un objet est refusée d'un seul bloc, sans la recopier."""
    cas: list[tuple[Any, str]] = [
        ("texte", "str"),
        (["a", "b"], "list"),
        (None, "NoneType"),
        (42, "int"),
        (b"octets", "bytes"),
    ]
    for entree, nom_du_type in cas:
        entree_avant = copy.deepcopy(entree)
        assert valider_brut(entree) == [Erreur("*", "ligne_non_objet", nom_du_type)]
        assert entree == entree_avant


def test_une_ligne_vide_cumule_un_refus_absent_par_champ_dans_l_ordre_du_contrat():
    """Une ligne vide est refusée pour chacun des vingt champs obligatoires, dans cet ordre."""
    ligne: dict[str, Any] = {}
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [Erreur(champ, "absent") for champ in CHAMPS_BRUTS]
    assert ligne == ligne_avant


def test_chaque_champ_obligatoire_absent_est_refuse_seul_et_la_ligne_reste_inchangee():
    """Retirer un seul champ obligatoire produit exactement un refus « absent » pour ce champ."""
    for champ in CHAMPS_BRUTS:
        ligne = ligne_brute()
        del ligne[champ]
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur(champ, "absent")]
        assert ligne == ligne_avant


def test_un_champ_obligatoire_a_none_est_absent_et_la_ligne_reste_inchangee():
    """Un champ présent mais nul vaut un champ manquant : il n'est jamais contrôlé comme valeur."""
    for champ in CHAMPS_BRUTS:
        ligne = ligne_brute(**{champ: None})
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur(champ, "absent")]
        assert ligne == ligne_avant


def test_une_valeur_fausse_mais_presente_n_est_pas_absente():
    """Zéro et texte vide sont des valeurs (donc contrôlées), pas des champs manquants."""
    ligne = ligne_brute(amt=0, merchant="", gender="", zip=0)
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [
        Erreur("merchant", "vide", "''"),
        Erreur("amt", "hors_bornes", "0"),
        Erreur("gender", "valeur_inconnue"),
        Erreur("zip", "hors_bornes"),
    ]
    assert ligne == ligne_avant


def test_toutes_les_erreurs_sont_remontees_dans_l_ordre_des_champs_puis_is_fraud():
    """La file des rejets reçoit toutes les raisons d'un coup, dans un ordre stable."""
    ligne = ligne_brute(is_fraud=5, amt="x", lat=999, gender="Z", merchant="")
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [
        Erreur("merchant", "vide", "''"),
        Erreur("amt", "type_invalide", "'x'"),
        Erreur("gender", "valeur_inconnue"),
        Erreur("lat", "hors_bornes"),
        Erreur("is_fraud", "valeur_inconnue", "5"),
    ]
    assert ligne == ligne_avant


def test_la_validation_d_une_ligne_valide_ne_modifie_rien_et_ignore_les_champs_en_plus():
    """Une ligne conforme (même avec un champ de plus) passe sans qu'on y touche."""
    ligne = ligne_brute(**{"Unnamed: 0": 12})
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == []
    assert ligne == ligne_avant
    vue = MappingProxyType(ligne_brute(amt=-1))
    assert valider_brut(vue) == [Erreur("amt", "hors_bornes", "-1")]


# --------------------------------------------------------------------------------------------
# Ligne brute : un test par règle de refus
# --------------------------------------------------------------------------------------------
def test_un_numero_de_carte_qui_n_est_pas_un_entier_est_refuse_sans_etre_cite():
    """Le numéro de carte fautif n'est jamais recopié dans le détail (donnée sensible)."""
    for valeur in ("4929-SECRET-0000", True, 4929.5, ["4929"], {"n": 1}):
        ligne = ligne_brute(cc_num=valeur)
        ligne_avant = copy.deepcopy(ligne)
        erreurs = valider_brut(ligne)
        assert erreurs == [Erreur("cc_num", "type_invalide", "")]
        assert "SECRET" not in repr(erreurs)
        assert ligne == ligne_avant


def test_un_numero_de_carte_hors_de_8_a_19_chiffres_ou_negatif_est_refuse():
    """Sept ou vingt chiffres, un nombre négatif ou nul ne sont pas des numéros de carte."""
    refuses = [1234, 9_999_999, 10**19, -12_345_678, -4_929_000_000_000_000, 0]
    for valeur in refuses:
        ligne = ligne_brute(cc_num=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("cc_num", "longueur_invalide", "")]
        assert ligne == ligne_avant


def test_un_montant_qui_n_est_pas_un_nombre_fini_est_refuse_avec_un_apercu():
    """Texte, booléen, NaN et infinis ne sont pas des montants ; le détail montre la valeur."""
    cas: list[tuple[Any, str]] = [
        ("abc", "'abc'"),
        (True, "True"),
        (False, "False"),
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        (float("-inf"), "-inf"),
        ([1], "[1]"),
    ]
    for valeur, apercu in cas:
        ligne = ligne_brute(amt=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("amt", "type_invalide", apercu)]
        assert ligne == ligne_avant


def test_un_montant_nul_negatif_ou_au_dessus_du_plafond_est_refuse_hors_bornes():
    """Le montant doit être strictement positif et ne pas dépasser un million."""
    cas: list[tuple[Any, str]] = [
        (0, "0"),
        (0.0, "0.0"),
        (-0.01, "-0.01"),
        (1_000_000.01, "1000000.01"),
        (1_000_001, "1000001"),
        (1e300, "1e+300"),
    ]
    for valeur, apercu in cas:
        ligne = ligne_brute(amt=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("amt", "hors_bornes", apercu)]
        assert ligne == ligne_avant


def test_un_texte_libre_vide_trop_long_ou_non_textuel_est_refuse():
    """Marchand, prénom, nom, rue, ville, métier : ni vides, ni de plus de 200 caractères."""
    sensibles = {"first", "last", "street", "city", "job"}
    for champ in ("merchant", "first", "last", "street", "city", "job"):
        cas: list[tuple[Any, str, str]] = [
            ("", "vide", "''"),
            ("   ", "vide", "'   '"),
            ("\t\n", "vide", "'\\t\\n'"),
            (" " * 201, "vide", "'" + " " * 38 + "…"),
            ("a" * 201, "trop_long", "'" + "a" * 38 + "…"),
            (12, "type_invalide", "12"),
            (["x"], "type_invalide", "['x']"),
        ]
        for valeur, code, apercu in cas:
            ligne = ligne_brute(**{champ: valeur})
            ligne_avant = copy.deepcopy(ligne)
            attendu = Erreur(champ, code, "" if champ in sensibles else apercu)
            assert valider_brut(ligne) == [attendu]
            assert ligne == ligne_avant


def test_une_valeur_sensible_refusee_n_est_jamais_recopiee_dans_le_detail():
    """Prénom, nom, rue, ville, métier, naissance, zip, lat, long, genre : détail toujours vide."""
    sentinelle = f"SENTINELLE-{MARQUEUR}"
    cas = {
        "first": 12,
        "last": 12,
        "street": 12,
        "city": 12,
        "job": 12,
        "gender": sentinelle,
        "dob": sentinelle,
        "zip": sentinelle,
        "lat": sentinelle,
        "long": sentinelle,
        "cc_num": sentinelle,
    }
    for champ, valeur in cas.items():
        ligne = ligne_brute(**{champ: valeur})
        ligne_avant = copy.deepcopy(ligne)
        erreurs = valider_brut(ligne)
        assert [e.champ for e in erreurs] == [champ]
        assert [e.detail for e in erreurs] == [""]
        assert sentinelle not in repr(erreurs)
        assert ligne == ligne_avant


def test_une_categorie_mal_formee_est_refusee_et_citee_par_apercu():
    """Une catégorie est faite de mots en minuscules séparés par des tirets bas, rien d'autre."""
    for valeur in ("Shopping Net", "shopping-net", "_net", "net_", "shop__net", "net2", ""):
        ligne = ligne_brute(category=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("category", "format_invalide", repr(valeur))]
        assert ligne == ligne_avant
    ligne = ligne_brute(category=12)
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [Erreur("category", "type_invalide", "12")]
    assert ligne == ligne_avant


def test_un_genre_autre_que_m_ou_f_est_refuse_sans_etre_cite():
    """Seuls « M » et « F » existent dans la source ; le genre fautif n'est pas recopié."""
    for valeur in ("X", "m", "f", "MF", "", 1, ["M"]):
        ligne = ligne_brute(gender=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("gender", "valeur_inconnue", "")]
        assert ligne == ligne_avant


def test_un_etat_qui_n_est_pas_deux_majuscules_est_refuse():
    """L'état est un code à deux lettres majuscules ; le reste est refusé avec un aperçu."""
    for valeur in ("new york", "ny", "N", "NYC", "N1", "", " NY"):
        ligne = ligne_brute(state=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("state", "format_invalide", repr(valeur))]
        assert ligne == ligne_avant
    ligne = ligne_brute(state=36)
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [Erreur("state", "type_invalide", "36")]
    assert ligne == ligne_avant


def test_un_zip_hors_de_1_a_99999_ou_non_entier_est_refuse_sans_etre_cite():
    """Le code postal est un entier de 1 à 99 999 ; la valeur fautive n'est pas recopiée."""
    cas: list[tuple[Any, str]] = [
        ("10001", "type_invalide"),
        (True, "type_invalide"),
        (10001.5, "type_invalide"),
        (float("nan"), "type_invalide"),
        (0, "hors_bornes"),
        (-1, "hors_bornes"),
        (100_000, "hors_bornes"),
        (100_000.0, "hors_bornes"),
    ]
    for valeur, code in cas:
        ligne = ligne_brute(zip=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("zip", code, "")]
        assert ligne == ligne_avant


def test_une_latitude_hors_de_90_degres_est_refusee_mais_180_reste_permis_en_longitude():
    """Les seuils de la latitude (90) et de la longitude (180) ne sont pas interchangeables."""
    for champ, valeur, code in (
        ("lat", 90.0001, "hors_bornes"),
        ("lat", -90.0001, "hors_bornes"),
        ("lat", 100, "hors_bornes"),
        ("lat", "40.7", "type_invalide"),
        ("lat", True, "type_invalide"),
        ("lat", float("nan"), "type_invalide"),
        ("long", 180.0001, "hors_bornes"),
        ("long", -180.0001, "hors_bornes"),
        ("long", 200, "hors_bornes"),
        ("long", "-74", "type_invalide"),
        ("long", float("inf"), "type_invalide"),
    ):
        ligne = ligne_brute(**{champ: valeur})
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur(champ, code, "")]
        assert ligne == ligne_avant
    assert valider_brut(ligne_brute(long=100)) == []


def test_une_position_de_commercant_hors_bornes_est_refusee_avec_un_apercu():
    """La position du commerçant n'est pas sensible : le détail montre la valeur fautive."""
    cas: list[tuple[str, Any, str, str]] = [
        ("merch_lat", 90.5, "hors_bornes", "90.5"),
        ("merch_lat", -91, "hors_bornes", "-91"),
        ("merch_lat", "x", "type_invalide", "'x'"),
        ("merch_long", 180.5, "hors_bornes", "180.5"),
        ("merch_long", -181, "hors_bornes", "-181"),
        ("merch_long", None, "absent", ""),
        ("merch_long", False, "type_invalide", "False"),
    ]
    for champ, valeur, code, apercu in cas:
        ligne = ligne_brute(**{champ: valeur})
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur(champ, code, apercu)]
        assert ligne == ligne_avant


def test_une_population_negative_ou_non_entiere_est_refusee_avec_un_apercu():
    """La population d'une ville est un entier positif ou nul."""
    cas: list[tuple[Any, str, str]] = [
        (-1, "hors_bornes", "-1"),
        (-0.0001, "type_invalide", "-0.0001"),
        (1.5, "type_invalide", "1.5"),
        ("8000000", "type_invalide", "'8000000'"),
        (True, "type_invalide", "True"),
        (float("inf"), "type_invalide", "inf"),
    ]
    for valeur, code, apercu in cas:
        ligne = ligne_brute(city_pop=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("city_pop", code, apercu)]
        assert ligne == ligne_avant


def test_une_date_de_naissance_mal_formee_est_refusee_sans_etre_citee():
    """Une naissance qui n'est pas une date ISO existante est refusée ; elle n'est pas recopiée."""
    for valeur in ("31/02/1990", "1985-13-01", "1985-02-30", "hier", "", "1985-06-15T00:00:00"):
        ligne = ligne_brute(dob=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("dob", "format_invalide", "")]
        assert ligne == ligne_avant
    for valeur in (19_850_615, ["1985-06-15"], datetime(1985, 6, 15)):
        ligne = ligne_brute(dob=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("dob", "type_invalide", "")]
        assert ligne == ligne_avant


def test_une_naissance_avant_1900_ou_apres_aujourd_hui_est_refusee_hors_bornes():
    """La veille de 1900 et le lendemain de « aujourd'hui » sont refusés, à la journée près."""
    for valeur in ("1899-12-31", "0001-01-01", "2026-10-11", "2999-01-01", "9999-12-31"):
        ligne = ligne_brute(dob=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("dob", "hors_bornes", "")]
        assert ligne == ligne_avant


def test_un_identifiant_de_transaction_mal_forme_est_refuse_avec_un_apercu():
    """L'identifiant de transaction est exactement 32 caractères hexadécimaux minuscules."""
    mauvais = [
        "c7e1d0a94f3b42e8a65d1f0b9e2c7a1",  # 31 caractères
        "c7e1d0a94f3b42e8a65d1f0b9e2c7a143",  # 33 caractères
        "B3445F612BAE41D69A7CD1E4E2C423E3",  # majuscules
        "g3445f612bae41d69a7cd1e4e2c423e3",  # hors hexadécimal
        "",
    ]
    for valeur in mauvais:
        ligne = ligne_brute(trans_num=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("trans_num", "format_invalide", repr(valeur))]
        assert ligne == ligne_avant
    ligne = ligne_brute(trans_num=12345)
    ligne_avant = copy.deepcopy(ligne)
    assert valider_brut(ligne) == [Erreur("trans_num", "type_invalide", "12345")]
    assert ligne == ligne_avant


def test_un_apercu_de_exactement_40_caracteres_n_est_pas_tronque():
    """Le détail garde la valeur entière tant que sa représentation tient en 40 caractères."""
    valeur = "x" * 38  # repr = 40 caractères avec les guillemets
    ligne = ligne_brute(trans_num=valeur)
    ligne_avant = copy.deepcopy(ligne)
    erreurs = valider_brut(ligne)
    assert erreurs == [Erreur("trans_num", "format_invalide", "'" + "x" * 38 + "'")]
    assert ligne == ligne_avant


def test_un_apercu_de_41_caracteres_est_tronque_a_39_plus_une_ellipse():
    """Au-delà de 40 caractères le détail est coupé à 39 et terminé par « … » (40 au total)."""
    valeur = "x" * 39  # repr = 41 caractères
    ligne = ligne_brute(trans_num=valeur)
    ligne_avant = copy.deepcopy(ligne)
    erreurs = valider_brut(ligne)
    assert erreurs == [Erreur("trans_num", "format_invalide", "'" + "x" * 38 + "…")]
    assert ligne == ligne_avant


def test_un_champ_facultatif_is_fraud_hors_de_0_et_1_est_refuse_avec_un_apercu():
    """L'étiquette de fraude n'est refusée que si elle est présente et différente de 0 ou 1."""
    cas: list[tuple[Any, str]] = [
        (2, "2"),
        (-1, "-1"),
        (0.5, "0.5"),
        ("1", "'1'"),
        (True, "True"),
        (False, "False"),
        (float("nan"), "nan"),
        ([], "[]"),
    ]
    for valeur, apercu in cas:
        ligne = ligne_brute(is_fraud=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("is_fraud", "valeur_inconnue", apercu)]
        assert ligne == ligne_avant


def test_une_heure_de_transaction_non_entiere_ou_hors_bornes_est_refusee():
    """Une heure doit être un entier de millisecondes entre l'an 2000 et demain, pas plus."""
    cas: list[tuple[Any, str, str]] = [
        ("hier", "type_invalide", "'hier'"),
        (True, "type_invalide", "True"),
        (1.5, "type_invalide", "1.5"),
        (float("nan"), "type_invalide", "nan"),
        (float("inf"), "type_invalide", "inf"),
        (12, "hors_bornes", "12"),
        (AN_2000_MS - 1, "hors_bornes", "946684799999"),
        (-AN_2000_MS, "hors_bornes", "-946684800000"),
        (INSTANT_MS + UN_JOUR_MS + 1, "hors_bornes", "1791720000001"),
        (10**16, "hors_bornes", "10000000000000000"),
    ]
    for valeur, code, apercu in cas:
        ligne = ligne_brute(current_time=valeur)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [Erreur("current_time", code, apercu)]
        assert ligne == ligne_avant


def test_les_valeurs_limites_exactes_de_la_ligne_brute_sont_acceptees_sans_alteration():
    """Pile sur chaque borne (incluse), la ligne passe ; la validation ne la modifie pas."""
    cas: list[dict[str, Any]] = [
        {"amt": 0.01},
        {"amt": 1_000_000},
        {"amt": 1_000_000.0},
        {"cc_num": 10_000_000},
        {"cc_num": 9_999_999_999_999_999_999},
        {"cc_num": 4_929_000_000_000_000.0},
        {"zip": 1},
        {"zip": 99_999},
        {"zip": 10_001.0},
        {"lat": 90, "merch_lat": -90},
        {"lat": -90.0, "merch_lat": 90.0},
        {"long": 180, "merch_long": -180},
        {"long": -180.0, "merch_long": 180.0},
        {"lat": 0, "long": 0, "merch_lat": 0.0, "merch_long": 0.0},
        {"city_pop": 0},
        {"city_pop": 8_000_000.0},
        {"dob": "1900-01-01"},
        {"dob": "2026-10-10"},
        {"merchant": "m" * 200, "first": "p" * 200},
        {"gender": "M"},
        {"gender": "F"},
        {"is_fraud": 0},
        {"is_fraud": 1},
        {"is_fraud": 1.0},
        {"is_fraud": None},
        {"current_time": AN_2000_MS},
        {"current_time": INSTANT_MS + UN_JOUR_MS},
        {"current_time": float(INSTANT_MS)},
        {"category": "a"},
        {"category": "grocery_pos"},
        {"state": "ZZ"},
        {"trans_num": "0" * 32},
        {"trans_num": "f" * 32},
    ]
    for surcharge in cas:
        ligne = ligne_brute(**surcharge)
        ligne_avant = copy.deepcopy(ligne)
        assert valider_brut(ligne) == [], surcharge
        assert ligne == ligne_avant
    sans_etiquette = ligne_brute()
    del sans_etiquette["is_fraud"]
    sans_etiquette_avant = copy.deepcopy(sans_etiquette)
    assert valider_brut(sans_etiquette) == []
    assert sans_etiquette == sans_etiquette_avant


# --------------------------------------------------------------------------------------------
# Erreur
# --------------------------------------------------------------------------------------------
def test_une_erreur_est_immuable_et_son_dictionnaire_garde_un_detail_vide_par_defaut():
    """Une erreur de validation ne se réécrit pas après coup ; son export est complet."""
    erreur = Erreur("amt", "hors_bornes")
    dictionnaire_avant = erreur.en_dict()
    assert dictionnaire_avant == {"champ": "amt", "code": "hors_bornes", "detail": ""}
    with pytest.raises(dataclasses.FrozenInstanceError) as refus:
        erreur.code = "autre"  # type: ignore[misc]
    assert str(refus.value) == "cannot assign to field 'code'"
    assert erreur.en_dict() == dictionnaire_avant
    avec_detail = Erreur("trans_num", "format_invalide", "'abc'")
    assert avec_detail.en_dict() == {
        "champ": "trans_num",
        "code": "format_invalide",
        "detail": "'abc'",
    }


# --------------------------------------------------------------------------------------------
# Message masqué
# --------------------------------------------------------------------------------------------
def test_un_message_qui_n_est_pas_un_objet_est_refuse_en_nommant_son_type():
    """Un message masqué doit être un objet ; un texte ou une liste est refusé d'un bloc."""
    cas: list[tuple[Any, str]] = [
        (["liste"], "list"),
        ("texte", "str"),
        (None, "NoneType"),
        (7, "int"),
    ]
    for entree, nom_du_type in cas:
        entree_avant = copy.deepcopy(entree)
        assert valider_masque(entree) == [Erreur("*", "message_non_objet", nom_du_type)]
        assert entree == entree_avant


def test_un_message_vide_cumule_un_refus_absent_par_champ_masque_dans_l_ordre():
    """Un message vide est refusé pour les quinze champs obligatoires, sans exiger l'étiquette."""
    message: dict[str, Any] = {}
    message_avant = copy.deepcopy(message)
    assert valider_masque(message) == [Erreur(champ, "absent") for champ in CHAMPS_MASQUES]
    assert message == message_avant


def test_chaque_champ_masque_absent_ou_nul_est_refuse_seul_et_le_message_reste_inchange():
    """Retirer ou annuler un champ obligatoire produit exactement un refus « absent »."""
    for champ in CHAMPS_MASQUES:
        retire = message_valide()
        del retire[champ]
        retire_avant = copy.deepcopy(retire)
        assert valider_masque(retire) == [Erreur(champ, "absent")]
        assert retire == retire_avant
        nul = message_valide(**{champ: None})
        nul_avant = copy.deepcopy(nul)
        assert valider_masque(nul) == [Erreur(champ, "absent")]
        assert nul == nul_avant


def test_une_valeur_fausse_mais_presente_dans_un_message_n_est_pas_absente():
    """Zéro et texte vide sont contrôlés comme valeurs, pas traités comme des champs manquants."""
    message = message_valide(commercant="", version_schema=0, source="")
    message_avant = copy.deepcopy(message)
    assert valider_masque(message) == [
        Erreur("version_schema", "version_inconnue"),
        Erreur("commercant", "vide"),
        Erreur("source", "valeur_inconnue"),
    ]
    assert message == message_avant


def test_une_version_de_schema_differente_de_1_est_refusee():
    """Un message d'une autre version du contrat n'est pas lu : il est refusé."""
    for valeur in (0, 2, 99, -1, "1", 1.5, [1]):
        message = message_valide(version_schema=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("version_schema", "version_inconnue")]
        assert message == message_avant


def test_un_champ_inattendu_du_message_est_refuse_meme_s_il_est_sensible():
    """Tout champ hors contrat (prénom, carte, rue...) est arrêté avant d'atteindre l'entrepôt."""
    for champ in ("first", "last", "cc_num", "street", "dob", "lat", "long", "zip", "city"):
        message = message_valide(**{champ: f"valeur-{MARQUEUR}"})
        message_avant = copy.deepcopy(message)
        erreurs = valider_masque(message)
        assert erreurs == [Erreur(champ, "champ_inattendu")]
        assert MARQUEUR not in repr(erreurs)
        assert message == message_avant


def test_les_champs_inattendus_sont_listes_dans_l_ordre_du_message_apres_les_autres_erreurs():
    """Les refus de valeur passent avant les champs en trop ; une clé non textuelle est nommée."""
    message = message_valide(montant=-1, source="autre", etiquette_source=3)
    message["zzz"] = 1
    message[5] = 2
    message["aaa"] = 3
    message_avant = copy.deepcopy(message)
    assert valider_masque(message) == [
        Erreur("montant", "hors_bornes"),
        Erreur("source", "valeur_inconnue"),
        Erreur("etiquette_source", "valeur_inconnue"),
        Erreur("zzz", "champ_inattendu"),
        Erreur("5", "champ_inattendu"),
        Erreur("aaa", "champ_inattendu"),
    ]
    assert message == message_avant


def test_un_identifiant_de_transaction_masque_mal_forme_est_refuse_sans_etre_cite():
    """L'identifiant du message masqué suit le même format que celui de la ligne brute."""
    for valeur in ("c7e1d0a94f3b42e8a65d1f0b9e2c7a1", "g" * 32, "A" * 32, ""):
        message = message_valide(trans_num=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("trans_num", "format_invalide")]
        assert message == message_avant
    message = message_valide(trans_num=12345)
    assert valider_masque(message) == [Erreur("trans_num", "type_invalide")]


def test_un_horodatage_hors_de_l_an_2000_a_l_an_2100_ou_non_entier_est_refuse():
    """L'horodatage en secondes est un entier entre 946 684 800 et 4 102 444 800 inclus."""
    cas: list[tuple[Any, str]] = [
        (946_684_799, "hors_bornes"),
        (4_102_444_801, "hors_bornes"),
        (0, "hors_bornes"),
        (-1, "hors_bornes"),
        (1.5, "type_invalide"),
        ("1791028800", "type_invalide"),
        (True, "type_invalide"),
        (float("nan"), "type_invalide"),
    ]
    for valeur, code in cas:
        message = message_valide(horodatage_s=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("horodatage_s", code)]
        assert message == message_avant


def test_une_empreinte_de_carte_hors_de_16_a_64_hexadecimaux_est_refusee():
    """L'empreinte HMAC est en hexadécimal minuscule, de 16 à 64 caractères."""
    for valeur in ("a" * 15, "a" * 65, "A" * 32, "g" * 32, "", "0123456789abcdef 0123456789abcd"):
        message = message_valide(carte_hash=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("carte_hash", "format_invalide")]
        assert message == message_avant
    assert valider_masque(message_valide(carte_hash=1234567890123456)) == [
        Erreur("carte_hash", "type_invalide")
    ]


def test_les_quatre_derniers_chiffres_de_carte_doivent_etre_exactement_quatre_chiffres():
    """Trois, cinq chiffres ou des lettres ne sont pas une finale de carte."""
    for valeur in ("123", "12345", "abcd", "12 4", ""):
        message = message_valide(carte_fin=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("carte_fin", "format_invalide")]
        assert message == message_avant
    assert valider_masque(message_valide(carte_fin=1234)) == [Erreur("carte_fin", "type_invalide")]


def test_un_commercant_masque_vide_trop_long_ou_non_textuel_est_refuse():
    """Le commerçant est un texte non vide de 200 caractères au plus."""
    cas: list[tuple[Any, str]] = [
        ("", "vide"),
        ("   ", "vide"),
        (" " * 201, "vide"),
        ("a" * 201, "trop_long"),
        (12, "type_invalide"),
        (None, "absent"),
    ]
    for valeur, code in cas:
        message = message_valide(commercant=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("commercant", code)]
        assert message == message_avant


def test_une_categorie_ou_un_etat_masques_mal_formes_sont_refuses():
    """Catégorie : mots minuscules reliés par « _ » ; état : deux majuscules."""
    for valeur in ("Shopping Net", "_net", "net_", "a__b", ""):
        message = message_valide(categorie=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("categorie", "format_invalide")]
        assert message == message_avant
    for valeur in ("ny", "N", "NYC", "N1", ""):
        message = message_valide(etat=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("etat", "format_invalide")]
        assert message == message_avant
    assert valider_masque(message_valide(categorie=3)) == [Erreur("categorie", "type_invalide")]
    assert valider_masque(message_valide(etat=3)) == [Erreur("etat", "type_invalide")]


def test_un_montant_masque_negatif_ou_au_dessus_du_plafond_est_refuse():
    """Dans le message masqué le montant peut valoir 0, mais pas être négatif ni dépasser 1 M."""
    cas: list[tuple[Any, str]] = [
        (-0.01, "hors_bornes"),
        (-1, "hors_bornes"),
        (1_000_000.01, "hors_bornes"),
        ("42.5", "type_invalide"),
        (True, "type_invalide"),
        (float("nan"), "type_invalide"),
        (float("inf"), "type_invalide"),
    ]
    for valeur, code in cas:
        message = message_valide(montant=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("montant", code)]
        assert message == message_avant


def test_une_population_masquee_hors_de_0_a_100_millions_ou_non_entiere_est_refusee():
    """La population est un entier entre 0 et 100 000 000."""
    cas: list[tuple[Any, str]] = [
        (-1, "hors_bornes"),
        (100_000_001, "hors_bornes"),
        (1.5, "type_invalide"),
        ("8000000", "type_invalide"),
        (True, "type_invalide"),
    ]
    for valeur, code in cas:
        message = message_valide(population_ville=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("population_ville", code)]
        assert message == message_avant


def test_un_age_hors_de_0_a_120_ans_ou_non_entier_est_refuse():
    """L'âge est un entier d'années entre 0 et 120 inclus."""
    cas: list[tuple[Any, str]] = [
        (-1, "hors_bornes"),
        (121, "hors_bornes"),
        (200, "hors_bornes"),
        (30.5, "type_invalide"),
        ("40", "type_invalide"),
        (True, "type_invalide"),
        (float("nan"), "type_invalide"),
        (float("inf"), "type_invalide"),
    ]
    for valeur, code in cas:
        message = message_valide(age=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("age", code)]
        assert message == message_avant


def test_une_distance_negative_ou_plus_grande_qu_une_demi_circonference_est_refusee():
    """La distance orthodromique est comprise entre 0 et 20 100 km."""
    cas: list[tuple[Any, str]] = [
        (-0.001, "hors_bornes"),
        (20_100.01, "hors_bornes"),
        (1e9, "hors_bornes"),
        ("10", "type_invalide"),
        (True, "type_invalide"),
        (float("nan"), "type_invalide"),
        (float("inf"), "type_invalide"),
    ]
    for valeur, code in cas:
        message = message_valide(distance_km=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("distance_km", code)]
        assert message == message_avant


def test_une_position_de_commercant_masquee_hors_bornes_est_refusee_champ_par_champ():
    """Latitude et longitude du commerçant ont des seuils distincts (90 et 180)."""
    cas: list[tuple[str, Any, str]] = [
        ("lat_commercant", 90.01, "hors_bornes"),
        ("lat_commercant", -90.01, "hors_bornes"),
        ("lat_commercant", "40.8", "type_invalide"),
        ("long_commercant", 180.01, "hors_bornes"),
        ("long_commercant", -180.01, "hors_bornes"),
        ("long_commercant", "-73.9", "type_invalide"),
        ("long_commercant", True, "type_invalide"),
    ]
    for champ, valeur, code in cas:
        message = message_valide(**{champ: valeur})
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur(champ, code)]
        assert message == message_avant
    assert valider_masque(message_valide(long_commercant=100)) == []


def test_une_source_autre_que_api_ou_jeu_est_refusee():
    """La provenance d'un message est « api » ou « jeu », sans variante de casse."""
    for valeur in ("API", "Jeu", "autre", "", 1, ["api"]):
        message = message_valide(source=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("source", "valeur_inconnue")]
        assert message == message_avant


def test_une_etiquette_source_autre_que_0_ou_1_est_refusee_dont_les_booleens():
    """L'étiquette est l'entier 0 ou 1 ; un booléen, un texte ou 2 sont refusés."""
    for valeur in (2, -1, True, False, "1", "0", 0.5, [0]):
        message = message_valide(etiquette_source=valeur)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [Erreur("etiquette_source", "valeur_inconnue")]
        assert message == message_avant


def test_les_valeurs_limites_exactes_du_message_masque_sont_acceptees_sans_alteration():
    """Pile sur chaque borne (incluse), le message passe ; la validation ne le modifie pas."""
    cas: list[dict[str, Any]] = [
        {"horodatage_s": 946_684_800},
        {"horodatage_s": 4_102_444_800},
        {"horodatage_s": 1_791_028_800.0},
        {"carte_hash": "a" * 16},
        {"carte_hash": "f" * 64},
        {"carte_fin": "0000"},
        {"commercant": "m" * 200},
        {"categorie": "a"},
        {"categorie": "gas_transport_net"},
        {"montant": 0},
        {"montant": 0.0},
        {"montant": 1_000_000},
        {"population_ville": 0},
        {"population_ville": 100_000_000},
        {"population_ville": 8_000_000.0},
        {"age": 0},
        {"age": 120},
        {"age": 40.0},
        {"distance_km": 0},
        {"distance_km": 20_100.0},
        {"lat_commercant": 90, "long_commercant": 180},
        {"lat_commercant": -90.0, "long_commercant": -180.0},
        {"source": "jeu"},
        {"etiquette_source": 0},
        {"etiquette_source": 1},
        {"etiquette_source": 1.0},
        {"etiquette_source": None},
    ]
    for surcharge in cas:
        message = message_valide(**surcharge)
        message_avant = copy.deepcopy(message)
        assert valider_masque(message) == [], surcharge
        assert message == message_avant
    sans_etiquette = message_valide()
    del sans_etiquette["etiquette_source"]
    sans_etiquette_avant = copy.deepcopy(sans_etiquette)
    assert valider_masque(sans_etiquette) == []
    assert sans_etiquette == sans_etiquette_avant
    vue = MappingProxyType(message_valide(age=121))
    assert valider_masque(vue) == [Erreur("age", "hors_bornes")]
