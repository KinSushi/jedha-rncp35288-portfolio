# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Supervision et SLA (critère 6.1) : détection des silences, blocages et dérives."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from detection_fraude.entrepot import maintenant_iso
from detection_fraude.modele import NOM_POINTEUR
from detection_fraude.notification import Notificateur
from detection_fraude.supervision import controler, notifier_constats
from tests.helpers import CanalEnregistreur
from tests.test_rapport import ligne_argent


def en_service(config):
    config.modele.dossier.mkdir(parents=True, exist_ok=True)
    (config.modele.dossier / NOM_POINTEUR).write_text("v1\n", encoding="utf-8")


def alerte(trans_num, statut, cree_le):
    return {
        "trans_num": trans_num,
        "date_transaction": "2026-10-10",
        "horodatage_utc": cree_le,
        "carte_fin": "4242",
        "commercant": "M",
        "categorie": "c",
        "montant": 10.0,
        "score": 0.9,
        "seuil": 0.5,
        "version_modele": "v1",
        "source": "jeu",
        "statut_notification": statut,
        "tentatives": 0,
        "derniere_tentative": None,
        "notifiee_le": None,
        "latence_notification_s": None,
        "canaux_ok": "",
        "cree_le": cree_le,
        "detectee_le_s": 1.0,
    }


def codes(constats):
    return {c.code for c in constats}


def test_tout_va_bien(config, entrepot):
    en_service(config)
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [ligne_argent(1)])
    assert controler(entrepot, config) == []


def test_modele_absent(config, entrepot):
    assert "modele_absent" in codes(controler(entrepot, config, surveiller_flux=False))


def test_flux_silencieux(config, entrepot):
    en_service(config)
    assert "flux_silencieux" in codes(controler(entrepot, config))  # rien n'a jamais été traité
    ancien = ligne_argent(1)
    ancien["traite_le"] = (datetime.now(UTC) - timedelta(minutes=45)).isoformat(timespec="seconds")
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [ancien])
    constats = controler(entrepot, config)
    assert "flux_silencieux" in codes(constats) and "45" in constats[0].message
    assert controler(entrepot, config, surveiller_flux=False) == []


def test_alerte_bloquee_et_alerte_en_echec(config, entrepot):
    en_service(config)
    vieux = (datetime.now(UTC) - timedelta(minutes=30)).isoformat(timespec="seconds")
    with entrepot.transaction() as conn:
        entrepot.inserer_alertes(
            conn,
            [
                alerte("a" * 32, "a_envoyer", vieux),
                alerte("b" * 32, "a_envoyer", maintenant_iso()),
                alerte("c" * 32, "echec", vieux),
            ],
        )
    constats = controler(entrepot, config, surveiller_flux=False)
    assert codes(constats) == {"notifications_bloquees", "notifications_en_echec"}
    bloquees = next(c for c in constats if c.code == "notifications_bloquees")
    assert (
        bloquees.gravite == "critique" and "1 alerte" in bloquees.message
    )  # la récente est tolérée


def test_taux_de_rejets_eleve(config, entrepot):
    en_service(config)
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [ligne_argent(i) for i in range(30)])
        for i in range(10):
            entrepot.enregistrer_rejet(
                conn, {"etape": "producteur", "raison": "x"}, str(i), "rejets", "2026-10-10"
            )
    constats = controler(entrepot, config, surveiller_flux=False)
    assert "taux_rejets_eleve" in codes(constats)


def test_peu_de_lignes_pas_de_fausse_alerte(config, entrepot):
    en_service(config)
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [ligne_argent(1)])
        entrepot.enregistrer_rejet(
            conn, {"etape": "producteur", "raison": "x"}, "1", "rejets", "2026-10-10"
        )
    assert "taux_rejets_eleve" not in codes(controler(entrepot, config, surveiller_flux=False))


def test_seuls_les_constats_graves_sont_notifies(config, entrepot):
    canal = CanalEnregistreur()
    en_service(config)
    constats = controler(entrepot, config)  # flux silencieux : gravité haute
    notifier_constats(Notificateur([canal]), constats)
    assert len(canal.recus) == 1 and canal.recus[0].type == "supervision"
    assert "SUPERVISION HAUTE" in canal.recus[0].sujet


def test_modele_promu_mais_non_applique_par_le_consommateur(config, entrepot):
    import json
    import os
    import time

    en_service(config)
    etat = config.racine_donnees / "etat" / "consommateur.json"
    etat.parent.mkdir(parents=True, exist_ok=True)
    etat.write_text(json.dumps({"modele": "v-ancien"}), encoding="utf-8")
    pointeur = config.modele.dossier / "ACTUEL"
    # Promu à l'instant : le consommateur a encore le temps de recharger, aucune alerte.
    assert "modele_non_applique" not in codes(controler(entrepot, config, surveiller_flux=False))
    vieux = time.time() - 600
    os.utime(pointeur, (vieux, vieux))  # promu depuis 10 minutes
    constats = controler(entrepot, config, surveiller_flux=False)
    assert "modele_non_applique" in codes(constats)
    etat.write_text(json.dumps({"modele": "v1"}), encoding="utf-8")  # rattrapé
    assert "modele_non_applique" not in codes(controler(entrepot, config, surveiller_flux=False))
