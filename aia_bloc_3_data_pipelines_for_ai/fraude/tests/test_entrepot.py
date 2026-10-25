# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Entrepôt bronze / argent / or : idempotence, historique, rétention, lignage."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from detection_fraude.consommateur import construire_ligne_bronze, enrichir
from detection_fraude.entrepot import Entrepot, maintenant_iso
from detection_fraude.file_attente import Message
from detection_fraude.fonctionnalites import EtatCartes
from detection_fraude.masquage import masquer
from tests.helpers import ligne_brute


def message_pour(hacheur, rang, **surcharges):
    valeur = masquer(ligne_brute(rang, **surcharges), hacheur, source="jeu")
    return Message("transactions.valides", valeur["carte_hash"], valeur, str(rang), 1.0)


def test_initialisation_idempotente(entrepot):
    entrepot.initialiser()
    entrepot.initialiser()
    tables = {
        ligne["name"]
        for ligne in entrepot.requete("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "bronze_transactions",
        "argent_transactions",
        "or_alertes_fraude",
        "or_rapport_quotidien",
        "or_rapport_categorie",
        "meta_rejets",
        "meta_lignage",
    } <= tables


def test_bronze_ignore_les_doublons(entrepot, hacheur):
    ligne = construire_ligne_bronze(message_pour(hacheur, 1), "lot1", maintenant_iso())
    for _ in range(3):
        with entrepot.transaction() as conn:
            entrepot.inserer_bronze(conn, [ligne])
    assert entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 1


def test_identifiants_existants(entrepot, hacheur):
    lignes = [
        construire_ligne_bronze(message_pour(hacheur, i), "l", maintenant_iso()) for i in range(3)
    ]
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, lignes[:2])
        trouves = entrepot.identifiants_existants(conn, [x["trans_num"] for x in lignes])
    assert trouves == {lignes[0]["trans_num"], lignes[1]["trans_num"]}


def test_une_erreur_annule_toute_la_transaction(entrepot, hacheur):
    ligne = construire_ligne_bronze(message_pour(hacheur, 1), "l", maintenant_iso())
    try:
        with entrepot.transaction() as conn:
            entrepot.inserer_bronze(conn, [ligne])
            raise RuntimeError("panne au milieu du lot")
    except RuntimeError:
        pass
    assert entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 0


def test_historique_d_une_carte_strictement_avant_et_dans_la_fenetre(entrepot, hacheur):
    lignes = []
    for rang, secondes in enumerate([100, 200, 300, 400]):
        valeur = masquer(ligne_brute(rang, cc_num=4_929_000_000_000_001), hacheur, source="jeu")
        valeur["horodatage_s"] = 1_700_000_000 + secondes
        lignes.append(
            construire_ligne_bronze(
                Message("s", None, valeur, str(rang), 1.0), "l", maintenant_iso()
            )
        )
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, lignes)
    carte = lignes[0]["carte_hash"]
    historique = entrepot.historique_carte(carte, 1_700_000_300)
    assert [t for t, _ in historique] == [1_700_000_100, 1_700_000_200]
    assert [t for t, _ in entrepot.historique_carte(carte, 1_700_000_300, 1_700_000_150)] == [
        1_700_000_200
    ]


def test_argent_remplacer_ou_ignorer(entrepot, hacheur):
    etat = EtatCartes()
    bronze = construire_ligne_bronze(message_pour(hacheur, 1), "l", maintenant_iso())
    argent = enrichir(bronze, etat)
    argent.update(
        score=0.1,
        est_fraude=0,
        seuil=0.5,
        version_modele="v1",
        version_regles="r",
        traite_le=maintenant_iso(),
    )
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [argent])
    argent2 = {**argent, "score": 0.9, "est_fraude": 1}
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [argent2])  # sans remplacement : ignoré
    assert entrepot.scalaire("SELECT score FROM argent_transactions") == 0.1
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [argent2], remplacer=True)
    assert entrepot.scalaire("SELECT score FROM argent_transactions") == 0.9
    assert entrepot.scalaire("SELECT COUNT(*) FROM argent_transactions") == 1


def test_lignage_enregistre_etape_entree_sortie_et_versions(entrepot):
    entrepot.enregistrer_lignage(
        "test",
        "a",
        "b",
        nb_entrees=10,
        nb_sorties=8,
        nb_rejets=2,
        debut=maintenant_iso(),
        version_regles="r1",
        version_modele="m1",
        details={"cle": "valeur"},
    )
    (ligne,) = entrepot.requete("SELECT * FROM meta_lignage")
    assert (ligne["etape"], ligne["entree"], ligne["sortie"]) == ("test", "a", "b")
    assert (ligne["nb_entrees"], ligne["nb_sorties"], ligne["nb_rejets"]) == (10, 8, 2)
    assert (ligne["version_regles"], ligne["version_modele"]) == ("r1", "m1")
    assert json.loads(ligne["details"]) == {"cle": "valeur"}


def test_purge_du_bronze_selon_la_date_de_reception(entrepot, hacheur):
    ancien = construire_ligne_bronze(message_pour(hacheur, 1), "l", "2020-01-01T00:00:00+00:00")
    recent = construire_ligne_bronze(message_pour(hacheur, 2), "l", maintenant_iso())
    with entrepot.transaction() as conn:
        entrepot.inserer_bronze(conn, [ancien, recent])
    limite = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    assert entrepot.purger_bronze(limite) == 1
    assert entrepot.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 1


def test_le_meme_code_accepte_deux_bases_distinctes(tmp_path):
    """Portabilité : seul le DSN change (ici deux fichiers SQLite ; PostgreSQL en production)."""
    a, b = Entrepot(f"sqlite:///{tmp_path / 'a.db'}"), Entrepot(f"sqlite:///{tmp_path / 'b.db'}")
    a.initialiser()
    b.initialiser()
    assert a.scalaire("SELECT COUNT(*) FROM bronze_transactions") == 0
    a.fermer()
    b.fermer()
