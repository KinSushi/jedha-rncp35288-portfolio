# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Producteur : décodage, relances, validation, masquage, DLQ (critères 4.2, 4.3 et 5.1)."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
import requests

from detection_fraude.configuration import Relances
from detection_fraude.file_attente import FileSQLite
from detection_fraude.producteur import (
    PannesSimulees,
    Producteur,
    ReponseIllisible,
    SourceIndisponible,
    boucle_api,
    corrompre,
    decoder_reponse,
    duree_attente,
    interroger_api,
    lignes_du_jeu,
)
from tests.helpers import MARQUEUR, ligne_brute

# Forme d'une réponse de l'API, valeurs fictives : un document JSON encodé dans une chaîne JSON.
DOCUMENT = {
    "columns": ["cc_num", "merchant", "category", "amt", "trans_num", "is_fraud", "current_time"],
    "index": [1],
    "data": [
        [
            4929123412341234,
            "fraud_Marchand",
            "shopping_net",
            37.5,
            "c7e1d0a94f3b42e8a65d1f0b9e2c7a14",
            0,
            1791682255368,
        ]
    ],
}


class Reponse:
    def __init__(self, statut=200, corps=b"", entetes=None):
        self.status_code = statut
        self.content = corps if isinstance(corps, bytes) else corps.encode()
        self.headers = entetes or {}


def reponse_api(lignes):
    colonnes = list(lignes[0])
    document = {
        "columns": colonnes,
        "index": [1],
        "data": [[ligne[c] for c in colonnes] for ligne in lignes],
    }
    return Reponse(200, json.dumps(json.dumps(document)))


class GetScripte:
    """Renvoie (ou lève) successivement les éléments d'un scénario."""

    def __init__(self, *scenario):
        self.scenario = list(scenario)
        self.appels = 0

    def __call__(self, url, timeout=None):
        self.appels += 1
        element = self.scenario.pop(0)
        if isinstance(element, Exception):
            raise element
        return element


POLITIQUE = Relances(
    max_tentatives=4, attente_initiale_s=1.0, facteur=2.0, attente_max_s=30.0, gigue=0.0
)


# ----- Décodage -----
def test_decodage_du_double_encodage_de_l_api():
    lignes = decoder_reponse(json.dumps(json.dumps(DOCUMENT)))
    assert lignes == [dict(zip(DOCUMENT["columns"], DOCUMENT["data"][0], strict=True))]


def test_decodage_du_json_direct_et_de_la_liste():
    assert len(decoder_reponse(json.dumps(DOCUMENT).encode())) == 1
    assert decoder_reponse(json.dumps([{"a": 1}])) == [{"a": 1}]
    assert decoder_reponse("[]") == []


@pytest.mark.parametrize(
    "corps",
    [
        "pas du json",
        "<html>502</html>",
        json.dumps({"columns": ["a"]}),
        json.dumps({"columns": ["a", "b"], "data": [[1]]}),
        json.dumps({"columns": "a", "data": []}),
        json.dumps(42),
        json.dumps([1, 2]),
    ],
)
def test_decodage_refuse_les_formes_inattendues(corps):
    with pytest.raises(ReponseIllisible):
        decoder_reponse(corps)


# ----- Relances -----
def test_attente_exponentielle_plafonnee():
    attentes = [duree_attente(n, POLITIQUE, lambda: 0.0) for n in range(1, 8)]
    assert attentes == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_la_gigue_ajoute_au_plus_la_fraction_prevue():
    politique = Relances(attente_initiale_s=10.0, gigue=0.1)
    assert duree_attente(1, politique, lambda: 1.0) == pytest.approx(11.0)


def test_relance_apres_pannes_reseau_puis_succes():
    get = GetScripte(
        requests.ConnectionError("x"), requests.Timeout("y"), reponse_api([ligne_brute()])
    )
    pauses = []
    lignes = interroger_api(
        "https://api.test", POLITIQUE, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.0
    )
    assert len(lignes) == 1 and get.appels == 3
    assert pauses == [1.0, 2.0]  # attente exponentielle entre les tentatives


def test_relance_sur_502_comme_constate_sur_l_api_reelle():
    get = GetScripte(Reponse(502, "<html>Bad Gateway</html>"), reponse_api([ligne_brute()]))
    pauses = []
    assert interroger_api("u", POLITIQUE, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.0)
    assert pauses == [1.0]


def test_retry_after_est_respecte_et_plafonne():
    get = GetScripte(
        Reponse(429, "", {"Retry-After": "7"}),
        Reponse(503, "", {"Retry-After": "999"}),
        reponse_api([ligne_brute()]),
    )
    pauses = []
    interroger_api("u", POLITIQUE, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.0)
    assert pauses == [7.0, 30.0]


def test_reponse_illisible_est_relancee():
    get = GetScripte(Reponse(200, "pas du json"), reponse_api([ligne_brute()]))
    pauses = []
    assert interroger_api("u", POLITIQUE, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.0)
    assert len(pauses) == 1


def test_abandon_apres_le_nombre_maximal_de_tentatives():
    get = GetScripte(*[requests.ConnectionError("x")] * 4)
    pauses = []
    with pytest.raises(SourceIndisponible, match="4 tentatives"):
        interroger_api("u", POLITIQUE, 5, get=get, dormir=pauses.append, aleatoire=lambda: 0.0)
    assert get.appels == 4
    assert pauses == [1.0, 2.0, 4.0]  # trois attentes, aucune après la dernière tentative


def test_erreur_definitive_non_relancee():
    get = GetScripte(Reponse(404, "introuvable"))
    pauses = []
    with pytest.raises(SourceIndisponible, match="non relançable"):
        interroger_api("u", POLITIQUE, 5, get=get, dormir=pauses.append)
    assert get.appels == 1 and pauses == []


def test_pannes_simulees_pour_la_demonstration():
    get = PannesSimulees(GetScripte(reponse_api([ligne_brute()])), 2)
    with pytest.raises(requests.ConnectionError):
        get("u", timeout=1)
    with pytest.raises(requests.ConnectionError):
        get("u", timeout=1)
    assert get("u", timeout=1).status_code == 200


# ----- Production : validation, masquage, DLQ -----
@pytest.fixture
def file(config):
    f = FileSQLite(config.file.chemin_sqlite)
    yield f
    f.fermer()


@pytest.fixture
def producteur(config, file, hacheur):
    return Producteur(config, file, hacheur, source="jeu")


def lire(file, config, sujet):
    return file.lire(sujet, "test", max_messages=10_000, attente_s=0)


def test_ligne_valide_publiee_masquee(config, file, producteur):
    assert producteur.traiter_ligne(ligne_brute(1)) == "publiee"
    (message,) = lire(file, config, config.file.sujet_valides)
    assert message.cle == message.valeur["carte_hash"]
    assert MARQUEUR not in json.dumps(message.valeur)
    assert lire(file, config, config.file.sujet_rejets) == []


def test_ligne_invalide_vers_la_dlq_sans_bloquer_le_flux(config, file, producteur):
    lignes = [ligne_brute(1), ligne_brute(2, amt="abc"), ligne_brute(3), ligne_brute(4, dob="n/a")]
    bilan = producteur.traiter_lignes(lignes)
    assert (bilan.lues, bilan.publiees, bilan.rejetees) == (4, 2, 2)
    valides = lire(file, config, config.file.sujet_valides)
    rejets = lire(file, config, config.file.sujet_rejets)
    assert [m.valeur["trans_num"] for m in valides] == [
        ligne_brute(1)["trans_num"],
        ligne_brute(3)["trans_num"],
    ]
    assert [m.valeur["trans_num"] for m in rejets] == [
        ligne_brute(2)["trans_num"],
        ligne_brute(4)["trans_num"],
    ]
    rejet = rejets[0].valeur
    assert rejet["etape"] == "producteur" and rejet["raison"] == "schema_invalide"
    assert rejet["erreurs"][0]["champ"] == "amt" and rejet["erreurs"][0]["code"] == "type_invalide"
    assert rejet["trans_num"] == ligne_brute(2)["trans_num"]


def test_la_dlq_ne_contient_aucune_donnee_identifiante(config, file, producteur):
    producteur.traiter_lignes(
        [ligne_brute(5, amt="abc", cc_num=4_929_123_412_341_234), ligne_brute(6, gender="Q")]
    )
    texte = json.dumps([m.valeur for m in lire(file, config, config.file.sujet_rejets)])
    for interdit in (MARQUEUR, "4929123412341234", "Rue Secrete", "1985-06-15"):
        assert interdit not in texte


@pytest.mark.parametrize("rang", range(7))
def test_chaque_corruption_de_demonstration_finit_en_dlq(config, file, producteur, rang):
    assert producteur.traiter_ligne(corrompre(ligne_brute(rang), rang)) == "rejetee"
    assert len(lire(file, config, config.file.sujet_rejets)) == 1


def test_les_doublons_ne_sont_pas_republies(config, file, producteur):
    bilan = producteur.traiter_lignes([ligne_brute(1), ligne_brute(1), ligne_brute(2)])
    assert (bilan.publiees, bilan.doublons) == (2, 1)


def test_ligne_qui_n_est_pas_un_objet(config, file, producteur):
    assert producteur.traiter_ligne("texte") == "rejetee"
    assert producteur.traiter_ligne(None) == "rejetee"
    assert [m.valeur["raison"] for m in lire(file, config, config.file.sujet_rejets)] == [
        "schema_invalide",
        "schema_invalide",
    ]


def test_rejeu_du_jeu_avec_corruptions_deterministes(config, jeu_synthetique):
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    lignes = list(
        lignes_du_jeu(config.source.jeu_chemin, jour=None, limite=200, corruptions=5, graine=1)
    )
    assert [ligne["trans_num"] for ligne in lignes] == list(
        jeu_synthetique["trans_num"].head(200)
    )  # les 200 premières lignes du jeu, dans l'ordre
    from detection_fraude.schema import valider_brut

    assert sum(1 for ligne in lignes if valider_brut(ligne)) == 5
    autre = list(
        lignes_du_jeu(config.source.jeu_chemin, jour=None, limite=200, corruptions=5, graine=1)
    )
    assert lignes == autre  # reproductible
    assert all("current_time" in ligne for ligne in lignes)


def test_rejeu_d_un_jour_precis(config, jeu_synthetique):
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[100][:10]
    lignes = list(lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=None))
    attendu = int(jeu_synthetique["trans_date_trans_time"].str.startswith(jour).sum())
    assert len(lignes) == attendu > 0


# ----- Boucle API -----
def attentes_courtes(config):
    """Mêmes tentatives, attentes d'un millième de seconde : une pause réelle (au lieu de celle
    que le test injecte) ne ferait pas dormir plusieurs secondes."""
    relances = Relances(
        max_tentatives=config.source.relances.max_tentatives,
        attente_initiale_s=0.001,
        facteur=2.0,
        attente_max_s=0.01,
        gigue=0.0,
    )
    return replace(config, source=replace(config.source, relances=relances))


def test_boucle_api_publie_puis_s_arrete_apres_n_appels(config, file, producteur):
    get = GetScripte(reponse_api([ligne_brute(1)]), reponse_api([ligne_brute(2)]))
    bilan = boucle_api(
        attentes_courtes(config),
        producteur,
        threading.Event(),
        nombre_appels=2,
        intervalle_s=0,
        get=get,
        dormir=lambda s: None,
    )
    assert bilan.publiees == 2 and get.appels == 2
    etat = json.loads(
        (config.racine_donnees / "etat" / "producteur.json").read_text(encoding="utf-8")
    )
    assert etat["echecs_consecutifs"] == 0 and "derniere_reussite" in etat


def test_boucle_api_survit_a_une_source_indisponible(config, file, producteur):
    pannes = [requests.ConnectionError("x")] * 5
    get = GetScripte(*pannes, reponse_api([ligne_brute(3)]))
    bilan = boucle_api(
        attentes_courtes(config),
        producteur,
        threading.Event(),
        nombre_appels=2,
        intervalle_s=0,
        get=get,
        dormir=lambda s: None,
    )
    # 1er cycle : 5 échecs (max_tentatives du fichier de configuration) ; 2e cycle : succès.
    assert bilan.publiees == 1
    etat = json.loads(
        (config.racine_donnees / "etat" / "producteur.json").read_text(encoding="utf-8")
    )
    assert etat["echecs_consecutifs"] == 0


def test_boucle_api_enregistre_les_echecs_consecutifs(config, file, producteur):
    get = GetScripte(*[requests.ConnectionError("x")] * 10)
    boucle_api(
        attentes_courtes(config),
        producteur,
        threading.Event(),
        nombre_appels=2,
        intervalle_s=0,
        get=get,
        dormir=lambda s: None,
    )
    etat = json.loads(
        (config.racine_donnees / "etat" / "producteur.json").read_text(encoding="utf-8")
    )
    assert etat["echecs_consecutifs"] == 2


def test_boucle_api_respecte_l_arret_demande(config, file, producteur):
    arret = threading.Event()
    arret.set()
    get = GetScripte()
    bilan = boucle_api(config, producteur, arret, get=get)
    assert bilan.lues == 0 and get.appels == 0


def test_rebaser_decale_les_dates_de_jours_entiers_en_gardant_l_heure(config, jeu_synthetique):
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[100][:10]
    origine = list(lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=20))
    decalees = list(
        lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=20, rebaser_sur="2026-10-10")
    )
    ecarts = {
        d["current_time"] - o["current_time"] for o, d in zip(origine, decalees, strict=True)
    }
    assert len(ecarts) == 1  # le même décalage pour toutes les lignes
    (ecart,) = ecarts
    assert (
        ecart % 86_400_000 == 0 and ecart > 0
    )  # un nombre entier de jours : l'heure est conservée
    from datetime import UTC, datetime

    assert datetime.fromtimestamp(decalees[0]["current_time"] / 1000, UTC).date().isoformat() == (
        "2026-10-10"
    )
    from detection_fraude.schema import valider_brut

    assert all(valider_brut(ligne) == [] for ligne in decalees)  # dates récentes donc valides


def test_rebaser_exige_un_jour(config, jeu_synthetique):
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    with pytest.raises(ValueError, match="jour"):
        list(
            lignes_du_jeu(config.source.jeu_chemin, jour=None, limite=5, rebaser_sur="2026-10-10")
        )


def test_rejouer_un_jour_sans_transaction_donne_une_liste_vide(config, jeu_synthetique):
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    assert list(lignes_du_jeu(config.source.jeu_chemin, jour="1999-01-01", limite=None)) == []


def test_le_jour_rejoue_est_identique_avec_ou_sans_tranches(config, jeu_synthetique):
    """La lecture par tranches doit renvoyer exactement les lignes du jour, dans l'ordre."""
    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[500][:10]
    attendu = jeu_synthetique[jeu_synthetique["trans_date_trans_time"].str.startswith(jour)]
    lues = list(lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=None))
    assert [ligne["trans_num"] for ligne in lues] == list(attendu["trans_num"])


def test_rebaser_conserve_l_age_et_signale_le_changement_de_jour_de_semaine(
    config, jeu_synthetique, caplog
):
    import logging

    from detection_fraude.masquage import HacheurCarte, masquer

    jeu_synthetique.to_csv(config.source.jeu_chemin, index=False)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[300][:10]
    hacheur = HacheurCarte(b"cle-de-test-pour-l-age")
    origine = list(lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=30))
    logger = logging.getLogger("fraude")
    logger.propagate = True
    with caplog.at_level(logging.WARNING, logger="fraude"):
        decalees = list(
            lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=30, rebaser_sur="2026-10-10")
        )
    logger.propagate = False
    ages = [masquer(ligne, hacheur, source="jeu")["age"] for ligne in origine]
    ages_decales = [masquer(ligne, hacheur, source="jeu")["age"] for ligne in decalees]
    ecarts = [abs(a - b) for a, b in zip(ages, ages_decales, strict=True)]
    assert max(ecarts) <= 1  # translation commune : au plus un écart d'un an en bord de calendrier
    assert sum(ecarts) <= len(ecarts) // 3
    assert any("rebase_change_le_jour_de_semaine" in r.getMessage() for r in caplog.records)


def test_jour_rejoue_meme_quand_le_fichier_n_est_pas_trie(config, jeu_synthetique):
    """Repli : si les lignes du jour ne sont pas contiguës, on lit par tranches."""
    melange = jeu_synthetique.sample(frac=1.0, random_state=3)
    melange.to_csv(config.source.jeu_chemin, index=False)
    jour = jeu_synthetique["trans_date_trans_time"].iloc[500][:10]
    attendu = set(
        jeu_synthetique[jeu_synthetique["trans_date_trans_time"].str.startswith(jour)]["trans_num"]
    )
    lues = {
        ligne["trans_num"]
        for ligne in lignes_du_jeu(config.source.jeu_chemin, jour=jour, limite=None)
    }
    assert lues == attendu
