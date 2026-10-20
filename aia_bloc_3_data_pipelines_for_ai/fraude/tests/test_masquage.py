# Copyright © 2026 · voir le fichier LICENSE du dépôt
"""Masquage dès l'ingestion (critère 5.1) : aucune donnée identifiante ne survit."""

from __future__ import annotations

import json
import re
from datetime import date

import pytest

from detection_fraude.masquage import (
    HacheurCarte,
    calculer_age,
    distance_km,
    masquer,
    masquer_pour_rejet,
)
from tests.helpers import MARQUEUR, ligne_brute

CHAMPS_ATTENDUS = {
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


def test_le_message_ne_contient_que_les_champs_prevus(hacheur):
    message = masquer(ligne_brute(), hacheur, source="api")
    assert set(message) == CHAMPS_ATTENDUS


def test_aucune_valeur_sensible_dans_le_message(hacheur):
    ligne = ligne_brute(7, cc_num=4_929_123_412_341_234)
    texte = json.dumps(masquer(ligne, hacheur, source="api"))
    for interdit in (
        "4929123412341234",
        MARQUEUR,
        "Rue Secrete",
        "1985-06-15",
        "10001",
        "40.7128",
    ):
        assert interdit not in texte


def test_carte_hachee_et_tronquee(hacheur):
    message = masquer(ligne_brute(cc_num=4_929_123_412_341_234), hacheur, source="api")
    assert message["carte_fin"] == "1234"
    assert re.fullmatch(r"[0-9a-f]{32}", message["carte_hash"])  # 32 chiffres hexadécimaux


def test_hachage_deterministe_et_dependant_de_la_cle():
    a = HacheurCarte(b"cle-a" * 4)
    b = HacheurCarte(b"cle-b" * 4)
    assert a.empreinte(4_929_123_412_341_234) == a.empreinte(4_929_123_412_341_234)
    assert a.empreinte(4_929_123_412_341_234) != b.empreinte(4_929_123_412_341_234)
    assert a.empreinte(4_929_123_412_341_234) != a.empreinte(4_929_123_412_341_235)


def test_le_hachage_est_a_cle_donc_non_reversible_par_simple_sha256():
    import hashlib

    numero = 4_929_123_412_341_234
    sha_simple = hashlib.sha256(str(numero).encode()).hexdigest()[:32]
    assert HacheurCarte(b"cle-secrete-de-test").empreinte(numero) != sha_simple


def test_hacheur_refuse_une_cle_vide_ou_une_longueur_absurde():
    with pytest.raises(ValueError):
        HacheurCarte(b"")
    with pytest.raises(ValueError):
        HacheurCarte(b"cle", longueur=4)


def test_age_a_la_date_de_la_transaction():
    assert calculer_age(date(1985, 6, 15), date(2026, 6, 14)) == 40
    assert calculer_age(date(1985, 6, 15), date(2026, 6, 15)) == 41
    assert calculer_age(date(2026, 6, 15), date(2026, 6, 1)) == 0


def test_distance_connue():
    paris_londres = distance_km(48.8566, 2.3522, 51.5074, -0.1278)
    assert 340 < paris_londres < 348
    assert distance_km(10, 10, 10, 10) == 0.0


def test_distance_remplace_la_position_du_domicile(hacheur):
    proche = masquer(ligne_brute(lat=40.80, long=-73.95), hacheur, source="api")
    loin = masquer(ligne_brute(lat=34.05, long=-118.24), hacheur, source="api")
    assert proche["distance_km"] < 1
    assert loin["distance_km"] > 3000


def test_masquage_pour_rejet_ne_garde_rien_d_identifiant():
    ligne = ligne_brute(cc_num=4_929_123_412_341_234, amt="abc")
    rejet = masquer_pour_rejet(ligne)
    texte = json.dumps(rejet)
    for interdit in ("4929123412341234", MARQUEUR, "Rue Secrete", "1985-06-15"):
        assert interdit not in texte
    assert "cc_num" in rejet["champs_masques"] and "first" in rejet["champs_masques"]
    assert rejet["champs_conserves"]["trans_num"] == ligne["trans_num"]


def test_masquage_pour_rejet_efface_un_numero_de_carte_cache_dans_un_champ_libre():
    rejet = masquer_pour_rejet(ligne_brute(merchant="4929123412341234", amt=4_929_123_412_341_234))
    texte = json.dumps(rejet)
    assert "4929123412341234" not in texte


def test_masquage_pour_rejet_garde_l_horodatage_plausible():
    ligne = ligne_brute()
    assert masquer_pour_rejet(ligne)["champs_conserves"]["current_time"] == ligne["current_time"]


def test_masquage_pour_rejet_nomme_sans_recopier_les_champs_inconnus():
    rejet = masquer_pour_rejet({"trans_num": "x", "email": "personne@example.org"})
    assert rejet["champs_inconnus"] == ["email"]
    assert "example.org" not in json.dumps(rejet)


def test_masquage_pour_rejet_accepte_n_importe_quoi():
    assert masquer_pour_rejet("texte")["contenu"] == "[MASQUE]"
    assert masquer_pour_rejet(None)["type_recu"] == "NoneType"
