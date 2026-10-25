# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Capture des changements (CDC) : appliquer plusieurs fois un changement est inoffensif.

Pourquoi l'idempotence : Kafka (comme tout bus de messages) garantit « au moins une fois » ;
après une panne, un consommateur relit parfois des messages déjà traités. L'entrepôt doit donc
aboutir au même état, qu'un changement soit appliqué une ou plusieurs fois.
"""

from __future__ import annotations

import duckdb
import pytest
from sqlalchemy import text

from stripe_demo import olap


def _etat_des_faits(entrepot) -> list[tuple]:
    """Photographie ordonnée de la table de faits, pour comparer deux états."""
    return entrepot.execute("SELECT * FROM faits_paiements ORDER BY id_paiement").fetchall()


def test_le_batch_charge_tous_les_paiements(mini_demo):
    with mini_demo.moteur.connect() as cx:
        attendu = cx.execute(text("SELECT count(*) FROM paiements")).scalar_one()
    charge = mini_demo.entrepot.execute("SELECT count(*) FROM faits_paiements").fetchone()[0]
    assert charge == attendu == mini_demo.dernier_id

    # Une base OLTP introuvable est refusée, et les faits déjà chargés restent en place.
    faits_avant = _etat_des_faits(mini_demo.entrepot)
    with pytest.raises(duckdb.Error):
        olap.charger_batch(mini_demo.entrepot, str(mini_demo.chemin_oltp) + ".absent")
    assert _etat_des_faits(mini_demo.entrepot) == faits_avant


def test_la_cdc_applique_les_nouveaux_paiements(mini_demo):
    avant = mini_demo.entrepot.execute("SELECT count(*) FROM faits_paiements").fetchone()[0]
    mini_demo.ajouter_paiements(50, graine=9001)
    position, nouveaux = olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), mini_demo.position)
    apres = mini_demo.entrepot.execute("SELECT count(*) FROM faits_paiements").fetchone()[0]
    assert nouveaux == 50
    assert apres == avant + 50
    assert position == mini_demo.position + 50


def test_rejouer_la_meme_position_ne_cree_aucun_doublon(mini_demo):
    """Un consommateur qui a planté avant de valider sa position relit les mêmes changements."""
    mini_demo.ajouter_paiements(40, graine=9002)
    olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp),
                               mini_demo.position)
    etat_1 = _etat_des_faits(mini_demo.entrepot)
    # Rejeu complet depuis la même position de départ.
    _, nouveaux = olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), mini_demo.position)
    etat_2 = _etat_des_faits(mini_demo.entrepot)
    assert nouveaux == 0  # aucune ligne en plus : les faits existants sont remplacés
    assert etat_2 == etat_1

    # Une position qui n'est pas un nombre est refusée, et les faits ne bougent pas.
    with pytest.raises((ValueError, duckdb.Error)):
        olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp), "pas un nombre")
    assert _etat_des_faits(mini_demo.entrepot) == etat_1


def test_reprendre_a_la_derniere_position_ne_change_rien(mini_demo):
    mini_demo.ajouter_paiements(30, graine=9003)
    position, _ = olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), mini_demo.position)
    etat = _etat_des_faits(mini_demo.entrepot)
    position_2, nouveaux = olap.appliquer_changements(
        mini_demo.entrepot, str(mini_demo.chemin_oltp), position)
    assert (position_2, nouveaux) == (position, 0)
    assert _etat_des_faits(mini_demo.entrepot) == etat

    # Reprendre depuis une base OLTP introuvable est refusé : l'état reste celui d'avant.
    with pytest.raises(duckdb.Error):
        olap.appliquer_changements(
            mini_demo.entrepot, str(mini_demo.chemin_oltp) + ".absent", position)
    assert _etat_des_faits(mini_demo.entrepot) == etat


def test_pas_de_doublon_de_cle_apres_plusieurs_rejeux(mini_demo):
    mini_demo.ajouter_paiements(25, graine=9004)
    for _ in range(3):
        olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp),
                                   mini_demo.position)
    lignes, distinctes = mini_demo.entrepot.execute(
        "SELECT count(*), count(DISTINCT id_paiement) FROM faits_paiements").fetchone()
    assert lignes == distinctes

    # La clé primaire de la table de faits refuse elle-même un doublon, même écrit à la main.
    with pytest.raises(duckdb.ConstraintException):
        mini_demo.entrepot.execute(
            "INSERT INTO faits_paiements SELECT * FROM faits_paiements LIMIT 1")


def test_une_mise_a_jour_de_statut_est_propagee(mini_demo):
    """Un paiement réussi puis contesté (rétrofacturation) change son fait, sans le dupliquer."""
    with mini_demo.moteur.connect() as cx:
        cible = cx.execute(text(
            "SELECT id_paiement FROM paiements WHERE statut = 'reussi' LIMIT 1")).scalar_one()
    avant = mini_demo.entrepot.execute(
        "SELECT est_reussi, est_conteste FROM faits_paiements WHERE id_paiement = ?",
        [cible]).fetchone()
    assert avant == (True, False)
    with mini_demo.moteur.begin() as cx:
        cx.execute(text("UPDATE paiements SET statut = 'conteste' WHERE id_paiement = :i"),
                   {"i": cible})
    total_avant = mini_demo.entrepot.execute("SELECT count(*) FROM faits_paiements").fetchone()[0]
    olap.appliquer_changements(mini_demo.entrepot, str(mini_demo.chemin_oltp),
                               mini_demo.position)
    apres = mini_demo.entrepot.execute(
        "SELECT est_reussi, est_conteste FROM faits_paiements WHERE id_paiement = ?",
        [cible]).fetchone()
    total_apres = mini_demo.entrepot.execute("SELECT count(*) FROM faits_paiements").fetchone()[0]
    assert apres == (False, True)
    assert total_apres == total_avant
