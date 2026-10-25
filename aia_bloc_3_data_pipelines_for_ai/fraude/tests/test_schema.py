# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Validation de schéma (critère 4.1) : types, formats, bornes, champs obligatoires."""

from __future__ import annotations

import pytest

from detection_fraude.masquage import masquer
from detection_fraude.schema import valider_brut, valider_masque
from tests.helpers import MARQUEUR, ligne_brute


def codes(erreurs) -> set[tuple[str, str]]:
    return {(e.champ, e.code) for e in erreurs}


def test_ligne_valide_sans_erreur():
    assert valider_brut(ligne_brute()) == []


def test_is_fraud_est_facultatif():
    ligne = ligne_brute()
    del ligne["is_fraud"]
    assert valider_brut(ligne) == []


@pytest.mark.parametrize(
    ("champ", "valeur", "code"),
    [
        ("amt", "abc", "type_invalide"),
        ("amt", -3.0, "hors_bornes"),
        ("amt", 0, "hors_bornes"),
        ("amt", 5_000_000, "hors_bornes"),
        ("amt", float("nan"), "type_invalide"),
        ("amt", True, "type_invalide"),
        ("gender", "X", "valeur_inconnue"),
        ("dob", "31/02/1990", "format_invalide"),
        ("dob", "2999-01-01", "hors_bornes"),
        ("lat", 123.4, "hors_bornes"),
        ("long", -200, "hors_bornes"),
        ("cc_num", "carte-invalide", "type_invalide"),
        ("cc_num", 1234, "longueur_invalide"),
        ("state", "new york", "format_invalide"),
        ("zip", 0, "hors_bornes"),
        ("trans_num", "pas-un-identifiant", "format_invalide"),
        ("category", "Shopping Net", "format_invalide"),
        ("is_fraud", 2, "valeur_inconnue"),
        ("current_time", 12, "hors_bornes"),
        ("current_time", "hier", "type_invalide"),
        ("city_pop", -1, "hors_bornes"),
        ("merchant", "", "vide"),
    ],
)
def test_chaque_corruption_est_detectee(champ, valeur, code):
    erreurs = valider_brut(ligne_brute(**{champ: valeur}))
    assert (champ, code) in codes(erreurs)


@pytest.mark.parametrize("champ", ["trans_num", "amt", "cc_num", "dob", "current_time"])
def test_champ_obligatoire_absent_ou_nul(champ):
    absent = ligne_brute()
    del absent[champ]
    assert (champ, "absent") in codes(valider_brut(absent))
    nul = ligne_brute(**{champ: None})
    assert (champ, "absent") in codes(valider_brut(nul))


def test_toutes_les_erreurs_sont_remontees_d_un_coup():
    erreurs = valider_brut(ligne_brute(amt="x", gender="Z", lat=999))
    assert {"amt", "gender", "lat"} <= {e.champ for e in erreurs}


def test_une_ligne_non_objet_est_rejetee():
    assert codes(valider_brut("pas un objet")) == {("*", "ligne_non_objet")}  # type: ignore[arg-type]


def test_le_detail_ne_cite_jamais_une_valeur_sensible():
    erreurs = valider_brut(ligne_brute(cc_num="4929-SECRET-0000", dob="Zx-date", first=12))
    for erreur in erreurs:
        assert "SECRET" not in erreur.detail
        assert "Zx-date" not in erreur.detail


def test_message_masque_valide(hacheur):
    message = masquer(ligne_brute(), hacheur, source="jeu")
    assert valider_masque(message) == []


def test_message_masque_refuse_un_champ_sensible_qui_aurait_survecu(hacheur):
    message = masquer(ligne_brute(), hacheur, source="jeu")
    message["first"] = f"Prenom{MARQUEUR}"
    assert ("first", "champ_inattendu") in codes(valider_masque(message))


@pytest.mark.parametrize(
    ("champ", "valeur", "code"),
    [
        ("montant", -1, "hors_bornes"),
        ("age", 200, "hors_bornes"),
        ("carte_fin", "12", "format_invalide"),
        ("carte_hash", "ZZZ", "format_invalide"),
        ("source", "autre", "valeur_inconnue"),
        ("version_schema", 99, "version_inconnue"),
        ("distance_km", -5, "hors_bornes"),
        ("etiquette_source", 3, "valeur_inconnue"),
    ],
)
def test_message_masque_corrompu(hacheur, champ, valeur, code):
    message = masquer(ligne_brute(), hacheur, source="api")
    message[champ] = valeur
    assert (champ, code) in codes(valider_masque(message))


def test_message_masque_incomplet(hacheur):
    message = masquer(ligne_brute(), hacheur, source="api")
    del message["montant"]
    assert ("montant", "absent") in codes(valider_masque(message))
    assert codes(valider_masque(["liste"])) == {("*", "message_non_objet")}  # type: ignore[arg-type]
