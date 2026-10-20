# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Garanties du masquage (critère 5.1) : ce qui est refusé, ce qui est effacé, ce qui ne bouge pas.

Les valeurs attendues sont écrites en dur (empreintes calculées avec la bibliothèque standard,
distances recoupées par une seconde formule, messages complets) : un test qui recalculerait avec
le code testé ne prouverait rien.

Les tests dont le nom parle d'un état « inchangé » prennent un instantané ``..._avant`` et le
comparent à l'état d'après : la ligne reçue, l'entrée d'un rejet, la mémoire du hacheur.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Any

import pytest

from detection_fraude import masquage
from detection_fraude.masquage import (
    HacheurCarte,
    calculer_age,
    distance_km,
    horodatage_depuis_ms,
    masquer,
    masquer_pour_rejet,
)
from detection_fraude.schema import valider_masque
from tests.helpers import MARQUEUR, ligne_brute

CLE = b"cle-de-test"
NUMERO = 4_929_123_412_341_234
# HMAC-SHA256 de NUMERO avec CLE, calculé une fois avec la bibliothèque standard.
EMPREINTE_COMPLETE = "f532458347652f6bc34fe7353d4c129c37a1a9a07a83214552bd7861a4b31704"

CHAMPS_SENSIBLES = (
    "cc_num",
    "first",
    "last",
    "street",
    "dob",
    "lat",
    "long",
    "zip",
    "city",
    "job",
    "gender",
)
CHAMPS_SURS = (
    "merchant",
    "category",
    "amt",
    "state",
    "city_pop",
    "merch_lat",
    "merch_long",
    "trans_num",
    "is_fraud",
    "current_time",
)


class HmacEspion:
    """Remplace le module ``hmac`` du masquage : note chaque appel puis délègue au vrai."""

    def __init__(self) -> None:
        self.appels: list[tuple[bytes, bytes, Any]] = []

    def new(self, cle: bytes, message: bytes, digestmod: Any) -> Any:
        self.appels.append((cle, message, digestmod))
        return hmac.new(cle, message, digestmod)


class DatetimeEspion:
    """Remplace ``datetime`` du masquage : note le fuseau demandé à ``fromtimestamp``."""

    def __init__(self) -> None:
        self.appels: list[tuple[Any, Any]] = []

    def fromtimestamp(self, secondes: Any, fuseau: Any = None) -> datetime:
        self.appels.append((secondes, fuseau))
        return datetime.fromtimestamp(secondes, fuseau)


class MathSurestime:
    """Le module ``math`` dont la racine carrée dépasse son résultat exact d'un cran flottant."""

    def __getattr__(self, nom: str) -> Any:
        return getattr(math, nom)

    @staticmethod
    def sqrt(x: float) -> float:
        return math.sqrt(x) * (1 + 2.3e-16)


def hacheur_de_test() -> HacheurCarte:
    return HacheurCarte(CLE)


# --------------------------------------------------------------------------------------------
# HacheurCarte
# --------------------------------------------------------------------------------------------
def test_un_hacheur_sans_cle_est_refuse_avant_meme_de_regarder_la_longueur():
    """Une clé vide rendrait le HMAC rejouable par n'importe qui : refus, message exact."""
    for cle in (b"", bytearray()):
        with pytest.raises(ValueError) as refus:
            HacheurCarte(cle)
        assert str(refus.value) == "La clé de hachage ne peut pas être vide."
    with pytest.raises(ValueError) as refus:
        HacheurCarte(b"", longueur=4)
    assert str(refus.value) == "La clé de hachage ne peut pas être vide."


def test_une_empreinte_de_moins_de_16_caracteres_est_refusee():
    """Une empreinte trop courte se retrouverait par collision : 15 est refusé, message exact."""
    for longueur in (15, 4, 0, -1):
        with pytest.raises(ValueError) as refus:
            HacheurCarte(CLE, longueur=longueur)
        assert str(refus.value) == "La longueur d'empreinte doit être comprise entre 16 et 64."


def test_une_empreinte_de_plus_de_64_caracteres_est_refusee():
    """Un SHA-256 n'a que 64 caractères hexadécimaux : 65 est refusé, message exact."""
    for longueur in (65, 100, 1000):
        with pytest.raises(ValueError) as refus:
            HacheurCarte(CLE, longueur=longueur)
        assert str(refus.value) == "La longueur d'empreinte doit être comprise entre 16 et 64."


def test_l_empreinte_est_un_hmac_sha256_de_la_cle_et_du_numero_calcule_une_seule_fois(
    monkeypatch,
):
    """Le lien « même carte » repose sur un HMAC à clé ; le résultat est mémorisé."""
    espion = HmacEspion()
    monkeypatch.setattr(masquage, "hmac", espion)
    hacheur = hacheur_de_test()
    empreinte_avant = hacheur.empreinte(NUMERO)
    assert empreinte_avant == EMPREINTE_COMPLETE[:32]
    assert hacheur.empreinte(NUMERO) == empreinte_avant
    autre = hacheur.empreinte(NUMERO + 1)
    assert autre == hmac.new(CLE, b"4929123412341235", hashlib.sha256).hexdigest()[:32]
    assert autre != empreinte_avant
    assert hacheur.empreinte(NUMERO) == empreinte_avant
    assert espion.appels == [
        (CLE, b"4929123412341234", hashlib.sha256),
        (CLE, b"4929123412341235", hashlib.sha256),
    ]
    monkeypatch.undo()
    assert HacheurCarte(CLE, 16).empreinte(NUMERO) == EMPREINTE_COMPLETE[:16]
    assert HacheurCarte(CLE, 17).empreinte(NUMERO) == EMPREINTE_COMPLETE[:17]
    assert HacheurCarte(CLE).empreinte(NUMERO) == EMPREINTE_COMPLETE[:32]
    assert HacheurCarte(CLE, longueur=64).empreinte(NUMERO) == EMPREINTE_COMPLETE
    autre_cle = HacheurCarte(b"autre-cle-de-test").empreinte(NUMERO)
    attendu_autre = hmac.new(b"autre-cle-de-test", b"4929123412341234", "sha256")
    assert autre_cle == attendu_autre.hexdigest()[:32]
    assert autre_cle != EMPREINTE_COMPLETE[:32]


def test_au_plafond_la_memoire_des_empreintes_reste_inchangee_et_le_numero_suivant_est_recalcule(
    monkeypatch,
):
    """La mémoire est plafonnée (1 000 000 de cartes) : au plafond, plus rien n'y entre."""
    assert masquage.TAILLE_MEMOIRE_EMPREINTES == 1_000_000
    espion = HmacEspion()
    monkeypatch.setattr(masquage, "hmac", espion)
    monkeypatch.setattr(masquage, "TAILLE_MEMOIRE_EMPREINTES", 3)
    hacheur = hacheur_de_test()
    numeros = [NUMERO, NUMERO + 1, NUMERO + 2, NUMERO + 3]
    empreintes_avant = [hacheur.empreinte(numero) for numero in numeros]
    attendues = [hmac.new(CLE, str(n).encode(), hashlib.sha256).hexdigest()[:32] for n in numeros]
    assert empreintes_avant == attendues
    calculs_au_remplissage = list(espion.appels)
    assert calculs_au_remplissage == [(CLE, str(n).encode(), hashlib.sha256) for n in numeros]
    # Relecture : les trois premiers numéros viennent de la mémoire, le quatrième (hors
    # plafond) est recalculé, et toutes les empreintes sont identiques à celles d'avant.
    assert [hacheur.empreinte(numero) for numero in numeros] == empreintes_avant
    assert espion.appels[len(calculs_au_remplissage) :] == [
        (CLE, str(NUMERO + 3).encode(), hashlib.sha256)
    ]
    # Une mémoire à 3 places : le troisième numéro y entre (2 < 3), le quatrième non (3 < 3).
    assert hacheur.empreinte(NUMERO + 2) == attendues[2]
    assert hacheur.empreinte(NUMERO + 3) == attendues[3]
    assert espion.appels[len(calculs_au_remplissage) + 1 :] == [
        (CLE, str(NUMERO + 3).encode(), hashlib.sha256)
    ]


# --------------------------------------------------------------------------------------------
# distance_km, calculer_age, horodatage_depuis_ms
# --------------------------------------------------------------------------------------------
def test_les_distances_de_reference_sont_exactes_au_milliardieme_antipodes_compris(monkeypatch):
    """Valeurs recoupées avec la formule de Vincenty sur la sphère de rayon 6 371,0088 km."""
    cas = [
        ((48.8566, 2.3522, 51.5074, -0.1278), 343.556534880883),  # Paris - Londres
        ((48.8566, 2.3522, 40.7128, -74.0060), 5837.24896656637),  # Paris - New York
        ((-33.8688, 151.2093, 51.5074, -0.1278), 16993.9569328165),  # Sydney - Londres
        ((10, 20, 30, 50), 3821.54126623776),
        ((40.7128, -74.0060, 34.05, -118.24), 3935.54030338719),
        ((0, 0, 0, 1), 111.1950802335329),  # un degré sur l'équateur
        ((0, 0, 1, 0), 111.1950802335329),  # un degré de méridien
        ((0, 0, 0, 90), 10007.557221017962),  # un quart d'équateur
        ((0.0, 0.0, 10.0, 0.0), 1111.950802335329),
        ((0.0, 0.0, 0.0, 10.0), 1111.950802335329),
    ]
    for points, attendu in cas:
        assert distance_km(*points) == pytest.approx(attendu, rel=1e-9), points
        assert distance_km(*points) == pytest.approx(distance_km(*points[2:], *points[:2]))
    assert distance_km(10, 10, 10, 10) == 0.0
    assert distance_km(-45.5, 170.25, -45.5, 170.25) == 0.0
    # Antipodes : la distance vaut une demi-circonférence, même si l'arrondi flottant fait
    # dépasser 1 à la racine carrée (simulé ici d'un cran) : l'arcsinus la borne à 1.
    demi_circonference = 20015.114442035923
    antipodes = [(45, 45, -45, -135), (0, 0, 0, 180), (90, 0, -90, 0), (33.3, 77.7, -33.3, -102.3)]
    for points in antipodes:
        assert distance_km(*points) == pytest.approx(demi_circonference, rel=1e-12), points
    monkeypatch.setattr(masquage, "math", MathSurestime())
    for points in antipodes:
        assert distance_km(*points) == pytest.approx(demi_circonference, rel=1e-12), points


def test_l_age_et_l_horodatage_suivent_les_dates_et_les_millisecondes_de_reference():
    """Âge en années révolues (mois avant jour, jamais négatif) ; millisecondes tronquées."""
    cas = [
        (date(1985, 6, 15), date(2026, 6, 14), 40),
        (date(1985, 6, 15), date(2026, 6, 15), 41),
        (date(1985, 6, 15), date(2026, 6, 16), 41),
        (date(1985, 6, 15), date(2026, 5, 20), 40),  # mois précédent, jour suivant
        (date(1985, 6, 15), date(2026, 7, 1), 41),  # mois suivant, jour précédent
        (date(1985, 6, 15), date(2026, 1, 1), 40),
        (date(1985, 6, 15), date(2026, 12, 31), 41),
        (date(1985, 12, 31), date(2026, 12, 31), 41),
        (date(1985, 12, 31), date(2026, 12, 30), 40),
        (date(2000, 2, 29), date(2001, 2, 28), 0),
        (date(2000, 2, 29), date(2001, 3, 1), 1),
        (date(2000, 2, 29), date(2003, 2, 28), 2),
        (date(2000, 2, 29), date(2004, 2, 29), 4),
        (date(2026, 6, 15), date(2026, 6, 15), 0),
        (date(2026, 6, 15), date(2026, 6, 14), 0),  # né « demain » : 0, pas -1
        (date(2027, 1, 1), date(2026, 6, 1), 0),
        (date(2030, 1, 1), date(2026, 6, 1), 0),
    ]
    for naissance, reference, attendu in cas:
        assert calculer_age(naissance, reference) == attendu, (naissance, reference)
    horodatages = [
        (0, 0),
        (999, 0),
        (1000, 1),
        (1001, 1),
        (1999, 1),
        (2000, 2),
        (1_791_547_203_999, 1_791_547_203),
        (1_791_547_204_000, 1_791_547_204),
    ]
    for millisecondes, attendu in horodatages:
        assert horodatage_depuis_ms(millisecondes) == attendu, millisecondes
    resultat = horodatage_depuis_ms(1999.9)
    assert resultat == 1
    assert type(resultat) is int


# --------------------------------------------------------------------------------------------
# masquer
# --------------------------------------------------------------------------------------------
def test_masquer_rend_le_message_exact_champ_par_champ_et_laisse_la_ligne_inchangee():
    """Un message masqué complet, valeur par valeur, conforme au contrat, sans toucher la ligne."""
    ligne = ligne_brute(3)
    ligne_avant = copy.deepcopy(ligne)
    message = masquer(ligne, hacheur_de_test(), source="api")
    assert message == {
        "version_schema": 1,
        "trans_num": "69630178dd2d8544da9a07ca89165c0d",
        "horodatage_s": 1_791_547_203,
        "carte_hash": "792b38e53c4d0a92dff7014a545c98bd",
        "carte_fin": "0003",
        "age": 41,
        "distance_km": 10.783,
        "source": "api",
        "commercant": "fraud_Marchand 3",
        "categorie": "misc_net",
        "montant": 30.5,
        "etat": "NY",
        "population_ville": 8_000_000,
        "lat_commercant": 40.8,
        "long_commercant": -73.95,
        "etiquette_source": 0,
    }
    assert valider_masque(message) == []
    assert ligne == ligne_avant
    assert masquer(ligne_brute(3), hacheur_de_test(), source="jeu")["source"] == "jeu"


def test_masquer_convertit_chaque_champ_numerique_vers_son_type_du_contrat():
    """Montant et positions en flottants, population et étiquette en entiers."""
    ligne = ligne_brute(
        amt=20, city_pop=8_000_000.0, merch_lat=40, merch_long=-74, is_fraud=1.0, cc_num=4.929e15
    )
    ligne_avant = copy.deepcopy(ligne)
    message = masquer(ligne, hacheur_de_test(), source="api")
    assert (message["montant"], type(message["montant"])) == (20.0, float)
    assert (message["population_ville"], type(message["population_ville"])) == (8_000_000, int)
    assert (message["lat_commercant"], type(message["lat_commercant"])) == (40.0, float)
    assert (message["long_commercant"], type(message["long_commercant"])) == (-74.0, float)
    assert (message["etiquette_source"], type(message["etiquette_source"])) == (1, int)
    assert message["carte_fin"] == "0000"
    assert ligne == ligne_avant


def test_une_etiquette_absente_ou_nulle_reste_none_et_zero_reste_zero():
    """L'étiquette n'est jamais inventée : absente ou nulle -> None, 0 -> 0, 1 -> 1."""
    sans = ligne_brute()
    del sans["is_fraud"]
    sans_avant = copy.deepcopy(sans)
    assert masquer(sans, hacheur_de_test(), source="api")["etiquette_source"] is None
    assert sans == sans_avant
    assert (
        masquer(ligne_brute(is_fraud=None), hacheur_de_test(), source="api")["etiquette_source"]
        is None
    )
    zero = masquer(ligne_brute(is_fraud=0), hacheur_de_test(), source="api")["etiquette_source"]
    assert (zero, type(zero)) == (0, int)
    un = masquer(ligne_brute(is_fraud=1), hacheur_de_test(), source="api")["etiquette_source"]
    assert un == 1


def test_masquer_garde_les_zeros_de_tete_des_quatre_derniers_chiffres_et_hache_le_numero_entier():
    """La finale de carte reste une chaîne de 4 chiffres ; le HMAC porte sur le numéro complet."""
    ligne = ligne_brute(cc_num=4_929_000_000_000_007)
    ligne_avant = copy.deepcopy(ligne)
    message = masquer(ligne, hacheur_de_test(), source="api")
    assert message["carte_fin"] == "0007"
    attendu = hmac.new(CLE, b"4929000000000007", hashlib.sha256).hexdigest()[:32]
    assert message["carte_hash"] == attendu
    huit_chiffres = masquer(ligne_brute(cc_num=12_345_678), hacheur_de_test(), source="api")
    assert huit_chiffres["carte_fin"] == "5678"
    assert ligne == ligne_avant


def test_masquer_calcule_l_age_au_jour_utc_de_la_transaction_a_la_seconde_pres(monkeypatch):
    """La veille de l'anniversaire à 23:59:59.999 UTC l'âge est 40 ; à minuit pile, 41."""
    espion = DatetimeEspion()
    monkeypatch.setattr(masquage, "datetime", espion)
    minuit_ms = 1_791_590_400_000  # 2026-10-10 00:00:00 UTC
    la_veille = ligne_brute(dob="1985-10-10", current_time=minuit_ms - 1)
    la_veille_avant = copy.deepcopy(la_veille)
    avant_minuit = masquer(la_veille, hacheur_de_test(), source="api")
    assert avant_minuit["age"] == 40
    assert avant_minuit["horodatage_s"] == 1_791_590_399
    assert la_veille == la_veille_avant
    pile_minuit = masquer(
        ligne_brute(dob="1985-10-10", current_time=minuit_ms), hacheur_de_test(), source="api"
    )
    assert pile_minuit["age"] == 41
    assert pile_minuit["horodatage_s"] == 1_791_590_400
    assert espion.appels == [(1_791_590_399, UTC), (1_791_590_400, UTC)]


def test_masquer_ne_recopie_aucun_champ_hors_liste_blanche_et_laisse_la_ligne_inchangee():
    """Un champ ajouté un jour à la source (email, identifiant...) n'atteint jamais le message."""
    ligne = ligne_brute(7, email="personne@example.org", **{"Unnamed: 0": 99})
    ligne_avant = copy.deepcopy(ligne)
    message = masquer(ligne, hacheur_de_test(), source="api")
    assert set(message) == {
        "version_schema",
        "trans_num",
        "horodatage_s",
        "carte_hash",
        "carte_fin",
        "age",
        "distance_km",
        "source",
        "commercant",
        "categorie",
        "montant",
        "etat",
        "population_ville",
        "lat_commercant",
        "long_commercant",
        "etiquette_source",
    }
    texte = json.dumps(message)
    for interdit in ("example.org", MARQUEUR, "Rue Secrete", "1985-06-15", "10004", "40.7128"):
        assert interdit not in texte
    assert ligne == ligne_avant


def test_masquer_n_exige_aucun_champ_sensible_inutile_a_la_detection():
    """Prénom, nom, rue, ville, zip, métier, genre ne sont pas lus : l'absence est sans effet."""
    complete = ligne_brute(5)
    inutiles = {"first", "last", "street", "city", "zip", "job", "gender"}
    reduite = {k: v for k, v in complete.items() if k not in inutiles}
    reduite_avant = copy.deepcopy(reduite)
    assert masquer(reduite, hacheur_de_test(), source="jeu") == masquer(
        complete, hacheur_de_test(), source="jeu"
    )
    assert reduite == reduite_avant


def test_masquer_refuse_une_ligne_a_laquelle_il_manque_un_champ_utile_et_la_laisse_inchangee():
    """Sans champ requis, masquer s'arrête sur ce champ au lieu d'inventer une valeur."""
    requis = (
        "cc_num",
        "current_time",
        "dob",
        "lat",
        "long",
        "merch_lat",
        "merch_long",
        "trans_num",
        "merchant",
        "category",
        "amt",
        "state",
        "city_pop",
    )
    for champ in requis:
        ligne = ligne_brute()
        del ligne[champ]
        ligne_avant = copy.deepcopy(ligne)
        with pytest.raises(KeyError) as refus:
            masquer(ligne, hacheur_de_test(), source="api")
        assert str(refus.value) == repr(champ)
        assert ligne == ligne_avant


def test_masquer_refuse_un_numero_de_carte_non_numerique_et_laisse_la_ligne_inchangee():
    """Un numéro de carte qui n'en est pas un n'est pas haché : l'erreur remonte."""
    ligne = ligne_brute(cc_num="carte")
    ligne_avant = copy.deepcopy(ligne)
    with pytest.raises(ValueError) as refus:
        masquer(ligne, hacheur_de_test(), source="api")
    assert str(refus.value) == "invalid literal for int() with base 10: 'carte'"
    assert ligne == ligne_avant


def test_masquer_refuse_une_date_de_naissance_impossible_et_laisse_la_ligne_inchangee():
    """Une naissance qui n'est pas une date ISO existante ne produit pas d'âge."""
    ligne = ligne_brute(dob="31/02/1990")
    ligne_avant = copy.deepcopy(ligne)
    with pytest.raises(ValueError) as refus:
        masquer(ligne, hacheur_de_test(), source="api")
    assert str(refus.value) == "Invalid isoformat string: '31/02/1990'"
    assert ligne == ligne_avant


def test_masquer_refuse_une_heure_non_numerique_et_laisse_la_ligne_inchangee():
    """Une heure qui n'est pas un nombre de millisecondes ne produit pas d'horodatage."""
    ligne = ligne_brute(current_time="hier")
    ligne_avant = copy.deepcopy(ligne)
    with pytest.raises(ValueError) as refus:
        masquer(ligne, hacheur_de_test(), source="api")
    assert str(refus.value) == "invalid literal for int() with base 10: 'hier'"
    assert ligne == ligne_avant


def test_masquer_refuse_une_position_non_numerique_et_laisse_la_ligne_inchangee():
    """Une latitude qui n'est pas un nombre ne produit pas de distance."""
    ligne = ligne_brute(lat="nord")
    ligne_avant = copy.deepcopy(ligne)
    with pytest.raises(TypeError) as refus:
        masquer(ligne, hacheur_de_test(), source="api")
    assert str(refus.value) == "must be real number, not str"
    assert ligne == ligne_avant


def test_masquer_refuse_un_montant_une_population_ou_une_etiquette_non_numeriques():
    """Montant, positions du commerçant, population et étiquette : un texte fait échouer."""
    cas: list[tuple[str, str, type[Exception], str]] = [
        ("amt", "cher", ValueError, "could not convert string to float: 'cher'"),
        ("merch_lat", "nord", TypeError, "must be real number, not str"),
        ("merch_long", "est", TypeError, "unsupported operand type(s) for -: 'str' and 'float'"),
        ("city_pop", "peuplee", ValueError, "invalid literal for int() with base 10: 'peuplee'"),
        ("is_fraud", "oui", ValueError, "invalid literal for int() with base 10: 'oui'"),
    ]
    for champ, valeur, exception, message in cas:
        ligne = ligne_brute(**{champ: valeur})
        ligne_avant = copy.deepcopy(ligne)
        with pytest.raises(exception) as refus:
            masquer(ligne, hacheur_de_test(), source="api")
        assert str(refus.value) == message
        assert ligne == ligne_avant


# --------------------------------------------------------------------------------------------
# masquer_pour_rejet
# --------------------------------------------------------------------------------------------
def test_ce_qui_n_est_pas_un_objet_est_reduit_a_son_type_sans_etre_recopie():
    """Une charge utile d'un type inattendu n'est jamais conservée : seul son type est noté."""
    sentinelle = f"SENTINELLE-{MARQUEUR}-4929123412341234"
    cas: list[tuple[Any, str]] = [
        (sentinelle, "str"),
        (None, "NoneType"),
        (42, "int"),
        ([sentinelle], "list"),
        ((sentinelle,), "tuple"),
        (sentinelle.encode(), "bytes"),
        (3.5, "float"),
    ]
    for entree, nom_du_type in cas:
        entree_avant = copy.deepcopy(entree)
        rejet = masquer_pour_rejet(entree)
        assert rejet == {"type_recu": nom_du_type, "contenu": "[MASQUE]"}
        assert sentinelle not in repr(rejet)
        assert entree == entree_avant


def test_un_objet_vide_donne_trois_rubriques_vides_et_reste_inchange():
    """Même sans aucun champ, le rejet garde sa forme : trois rubriques vides."""
    for vide in ({}, MappingProxyType({})):
        vide_avant = dict(vide)
        assert masquer_pour_rejet(vide) == {
            "champs_conserves": {},
            "champs_masques": [],
            "champs_inconnus": [],
        }
        assert vide == vide_avant


def test_chaque_champ_sensible_est_nomme_sans_sa_valeur_et_l_entree_reste_inchangee():
    """Les onze champs identifiants sont réduits à leur nom, triés ; leur valeur disparaît."""
    sentinelle = f"SENTINELLE-{MARQUEUR}"
    ligne = {champ: f"{sentinelle}-{champ}" for champ in reversed(CHAMPS_SENSIBLES)}
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet == {
        "champs_conserves": {},
        "champs_masques": [
            "cc_num",
            "city",
            "dob",
            "first",
            "gender",
            "job",
            "last",
            "lat",
            "long",
            "street",
            "zip",
        ],
        "champs_inconnus": [],
    }
    assert sentinelle not in repr(rejet)
    assert ligne == ligne_avant


def test_un_champ_inconnu_est_nomme_sans_etre_recopie_et_les_noms_sont_tries():
    """Un champ hors liste n'est jamais copié ; ses noms (même non textuels) sont triés."""
    ligne = {
        "zzz": "valeur-z@example.org",
        "email": "personne@example.org",
        "Unnamed: 0": 99,
        5: "cinq",
        None: "rien",
    }
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet == {
        "champs_conserves": {},
        "champs_masques": [],
        "champs_inconnus": ["5", "None", "Unnamed: 0", "email", "zzz"],
    }
    assert "example.org" not in repr(rejet)
    assert ligne == ligne_avant


def test_les_champs_surs_sont_conserves_a_l_identique_et_le_reste_est_trie_a_part():
    """Marchand, catégorie, montant, état, positions, identifiant, étiquette, heure : gardés."""
    ligne = ligne_brute(3)
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet["champs_conserves"] == {
        "merchant": "fraud_Marchand 3",
        "category": "misc_net",
        "amt": 30.5,
        "state": "NY",
        "city_pop": 8_000_000,
        "merch_lat": 40.8,
        "merch_long": -73.95,
        "trans_num": "69630178dd2d8544da9a07ca89165c0d",
        "is_fraud": 0,
        "current_time": 1_791_547_203_000,
    }
    assert rejet["champs_masques"] == [
        "cc_num",
        "city",
        "dob",
        "first",
        "gender",
        "job",
        "last",
        "lat",
        "long",
        "street",
        "zip",
    ]
    assert rejet["champs_inconnus"] == []
    assert set(rejet["champs_conserves"]) == set(CHAMPS_SURS)
    assert ligne == ligne_avant


def test_un_champ_sur_none_ou_booleen_est_conserve_tel_quel_et_non_efface():
    """None et les booléens ne ressemblent pas à un numéro de carte : ils restent inchangés."""
    ligne = {"amt": None, "is_fraud": True, "city_pop": False, "state": None}
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet["champs_conserves"] == {
        "amt": None,
        "is_fraud": True,
        "city_pop": False,
        "state": None,
    }
    assert ligne == ligne_avant


def test_un_texte_de_80_caracteres_est_garde_entier_et_celui_de_81_est_coupe_a_79_plus_ellipse():
    """Une valeur conservée est tronquée à 80 caractères au plus, ellipse comprise."""
    quatre_vingts = "m" * 80
    quatre_vingt_un = "m" * 81
    ligne = {"merchant": quatre_vingts, "category": quatre_vingt_un}
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet["champs_conserves"]["merchant"] == quatre_vingts
    assert rejet["champs_conserves"]["category"] == "m" * 79 + "…"
    assert ligne == ligne_avant


def test_une_valeur_non_textuelle_est_convertie_en_texte_puis_tronquee_a_80():
    """Une liste ou un dictionnaire dans un champ sûr est ramené à un texte court, jamais copié."""
    long_dictionnaire = {"clef": "v" * 200}
    ligne = {"merchant": ["a", 1], "category": long_dictionnaire}
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert rejet["champs_conserves"]["merchant"] == "['a', 1]"
    assert rejet["champs_conserves"]["category"] == str(long_dictionnaire)[:79] + "…"
    assert ligne == ligne_avant


def test_un_numero_de_carte_cache_dans_un_texte_est_efface_avant_la_troncature():
    """Le numéro est effacé d'abord ; la coupe à 80 ne peut donc jamais en laisser un morceau."""
    cache = "x" * 60 + " 4929123412341234 fin"
    ligne = {"merchant": cache, "category": "paiement 4929123412341234"}
    ligne_avant = copy.deepcopy(ligne)
    rejet = masquer_pour_rejet(ligne)
    assert ligne == ligne_avant
    assert rejet["champs_conserves"]["category"] == "paiement [PAN_MASQUE]"
    assert rejet["champs_conserves"]["merchant"] == "x" * 60 + " [PAN_MASQUE] fin"
    coupe = masquer_pour_rejet({"merchant": "y" * 75 + " 4929123412341234"})
    assert coupe["champs_conserves"]["merchant"] == "y" * 75 + " [PA…"
    assert "4929" not in repr(coupe)


def test_un_nombre_de_13_chiffres_ou_plus_est_efface_et_celui_de_12_chiffres_reste_intact():
    """Un nombre de 1e12 et plus ressemble à une carte : [MASQUE] ; en dessous, il reste intact."""
    entrants = {
        "amt": 4_929_123_412_341_234,
        "city_pop": 1_000_000_000_000,
        "merch_lat": -1_000_000_000_000,
        "merch_long": 1e12,
        "state": -4.5e15,
    }
    entrants_avant = copy.deepcopy(entrants)
    rejet = masquer_pour_rejet(entrants)
    assert rejet["champs_conserves"] == dict.fromkeys(entrants, "[MASQUE]")
    assert entrants == entrants_avant
    en_dessous = {
        "amt": 999_999_999_999,
        "city_pop": -999_999_999_999,
        "merch_lat": 999_999_999_999.9,
        "merch_long": 0,
        "state": 12.5,
    }
    en_dessous_avant = copy.deepcopy(en_dessous)
    assert masquer_pour_rejet(en_dessous)["champs_conserves"] == en_dessous
    assert en_dessous == en_dessous_avant


def test_l_heure_plausible_est_gardee_jusqu_a_l_an_2100_et_effacee_au_dela():
    """Un horodatage en ms est gardé s'il est entier et avant l'an 2100 ; sinon il est effacé."""
    gardes = [946_684_800_000, 1_791_633_600_000, 4_102_444_800_000, 946_684_799_999, 0]
    for valeur in gardes:
        entree = {"current_time": valeur}
        entree_avant = copy.deepcopy(entree)
        rejet = masquer_pour_rejet(entree)
        assert rejet["champs_conserves"] == {"current_time": valeur}
        assert entree == entree_avant
    effaces = [4_102_444_800_001, 9_999_999_999_999, 4_929_123_412_341_234, 1.7e12, -1.7e12, 1e18]
    for valeur in effaces:
        entree = {"current_time": valeur}
        entree_avant = copy.deepcopy(entree)
        rejet = masquer_pour_rejet(entree)
        assert rejet["champs_conserves"] == {"current_time": "[MASQUE]"}, valeur
        assert entree == entree_avant


def test_une_heure_booleenne_ou_flottante_sous_le_seuil_reste_inchangee():
    """Vrai/faux et un flottant de moins de 1e12 ne sont pas des horodatages à effacer."""
    for valeur in (True, False, 1_700_000_000.5, 12.5):
        entree = {"current_time": valeur}
        entree_avant = copy.deepcopy(entree)
        assert masquer_pour_rejet(entree)["champs_conserves"] == {"current_time": valeur}
        assert entree == entree_avant
