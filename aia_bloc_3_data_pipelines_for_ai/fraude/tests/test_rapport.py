# Copyright © 2026 · Auteur : KinSushi · Enzo · Sovralys LLC
"""Rapport du matin : exactitude des agrégats, rendu, idempotence, SLA (critères 1.1 et 6.1)."""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from detection_fraude.entrepot import maintenant_iso
from detection_fraude.notification import Notificateur
from detection_fraude.rapport import (
    DonneesAbsentes,
    calculer_rapport,
    ecrire_rapport,
    jour_de_la_veille,
    notifier_rapport,
    produire_rapport,
    rendre_html,
    verifier_delai_rapport,
    verifier_donnees_du_jour,
)
from tests.helpers import CanalEnregistreur

JOUR = "2026-10-09"


def ligne_argent(
    rang,
    jour=JOUR,
    montant=10.0,
    fraude=0,
    categorie="grocery_pos",
    etiquette=None,
    commercant="Marchand",
):
    return {
        "trans_num": f"{rang:032x}",
        "date_transaction": jour,
        "horodatage_utc": f"{jour}T10:{rang % 60:02d}:00+00:00",
        "horodatage_s": 1_700_000_000 + rang,
        "carte_hash": "c" * 32,
        "carte_fin": "4242",
        "commercant": commercant,
        "categorie": categorie,
        "montant": montant,
        "etat": "NY",
        "population_ville": 1000,
        "age": 40,
        "distance_km": 3.0,
        "heure": 10,
        "jour_semaine": 4,
        "nb_tx_24h": 0,
        "montant_relatif": 1.0,
        "score": 0.9 if fraude else 0.1,
        "est_fraude": fraude,
        "seuil": 0.5,
        "version_modele": "v1",
        "version_regles": "r",
        "etiquette_source": etiquette,
        "source": "jeu",
        "traite_le": maintenant_iso(),
        "lot_id": "l",
    }


@pytest.fixture
def donnees(entrepot):
    lignes = [
        ligne_argent(1, montant=100.0),
        ligne_argent(2, montant=50.0, categorie="shopping_net"),
        ligne_argent(3, montant=400.0, fraude=1, categorie="shopping_net", etiquette=1),
        ligne_argent(4, montant=600.0, fraude=1, categorie="shopping_net", etiquette=0),
        ligne_argent(5, montant=25.0, etiquette=1),  # fraude réelle non détectée
        ligne_argent(6, jour="2026-10-08", montant=80.0),  # la veille
        ligne_argent(7, jour="2026-10-08", montant=20.0, fraude=1),
    ]
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, lignes)
    return lignes


def test_agregats_exacts(entrepot, donnees):
    r = calculer_rapport(entrepot, JOUR)
    assert (r["nb_paiements"], r["nb_fraudes"]) == (5, 2)
    assert r["montant_total"] == 1175.0 and r["montant_fraudes"] == 1000.0
    assert r["taux_fraude_nb"] == pytest.approx(2 / 5)
    assert r["taux_fraude_montant"] == pytest.approx(1000 / 1175)
    assert (r["nb_etiquetees"], r["nb_vrais_positifs"], r["nb_fraudes_reelles"]) == (3, 1, 2)
    assert r["veille"]["nb_paiements"] == 2 and r["veille"]["nb_fraudes"] == 1


def test_repartition_par_categorie(entrepot, donnees):
    categories = {c["categorie"]: c for c in calculer_rapport(entrepot, JOUR)["categories"]}
    assert categories["shopping_net"]["nb_paiements"] == 3
    assert categories["shopping_net"]["nb_fraudes"] == 2
    assert categories["shopping_net"]["montant_fraudes"] == 1000.0
    assert categories["grocery_pos"]["nb_fraudes"] == 0


def test_liste_des_fraudes_triee_par_heure(entrepot, donnees):
    fraudes = calculer_rapport(entrepot, JOUR)["fraudes"]
    assert [f["montant"] for f in fraudes] == [400.0, 600.0]
    assert set(fraudes[0]) >= {"trans_num", "carte_fin", "commercant", "score"}
    assert "first" not in fraudes[0]


def test_ecriture_html_csv_et_couche_or(config, entrepot, donnees):
    rapport = calculer_rapport(entrepot, JOUR)
    chemins = ecrire_rapport(config, entrepot, rapport)
    page = Path(chemins["html"]).read_text(encoding="utf-8")
    assert f"paiements et fraudes du {JOUR}" in page and "Copyright © 2026" in page
    assert "****4242" in page and "shopping_net" in page
    with open(chemins["csv"], encoding="utf-8-sig", newline="") as flux:
        lignes = list(csv.reader(flux, delimiter=";"))
    assert lignes[0][:3] == ["horodatage_utc", "trans_num", "carte_fin"]
    assert [ligne[1] for ligne in lignes[1:]] == [f"{3:032x}", f"{4:032x}"]  # les deux fraudes
    (or_jour,) = entrepot.requete("SELECT * FROM or_rapport_quotidien")
    assert or_jour["nb_fraudes"] == 2 and or_jour["chemin_html"] == chemins["html"]
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_categorie") == 2


def test_le_rapport_est_idempotent(config, entrepot, donnees):
    for _ in range(3):
        ecrire_rapport(config, entrepot, calculer_rapport(entrepot, JOUR))
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_quotidien") == 1
    assert entrepot.scalaire("SELECT COUNT(*) FROM or_rapport_categorie") == 2


def test_le_html_echappe_les_valeurs(entrepot):
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(
            conn, [ligne_argent(1, fraude=1, commercant="<script>alert(1)</script>")]
        )
    page = rendre_html(calculer_rapport(entrepot, JOUR))
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


def test_controle_du_modele_affiche_precision_et_rappel(entrepot, donnees):
    page = rendre_html(calculer_rapport(entrepot, JOUR))
    assert "Précision du jour : 50.0 %" in page and "rappel du jour : 50.0 %" in page


def test_jour_sans_fraude(entrepot):
    with entrepot.transaction() as conn:
        entrepot.inserer_argent(conn, [ligne_argent(1)])
    page = rendre_html(calculer_rapport(entrepot, JOUR))
    assert "Aucune fraude détectée ce jour-là" in page


def test_source_muette_leve_donnees_absentes(entrepot):
    with pytest.raises(DonneesAbsentes):
        verifier_donnees_du_jour(entrepot, JOUR)


def test_format_de_jour_invalide(entrepot):
    with pytest.raises(ValueError):
        calculer_rapport(entrepot, "10/10/2026")


def test_jour_de_la_veille():
    assert jour_de_la_veille(datetime(2026, 10, 10, 7, 0, tzinfo=UTC)) == "2026-10-09"
    assert jour_de_la_veille(datetime(2026, 1, 1, 0, 0, tzinfo=UTC)) == "2025-12-31"
    assert jour_de_la_veille(datetime(2026, 10, 10, 1, 0)) == "2026-10-09"  # naïf = UTC


def test_sla_du_rapport():
    prevu = datetime(2026, 10, 10, 5, 0, tzinfo=UTC)
    ok, retard = verifier_delai_rapport(prevu + timedelta(minutes=59), prevu, 60)
    assert ok and retard == 0
    ok, retard = verifier_delai_rapport(prevu + timedelta(minutes=75), prevu, 60)
    assert not ok and retard == pytest.approx(15)


def test_produire_rapport_complet_notifie_et_trace(config, entrepot, donnees):
    canal = CanalEnregistreur()
    resultat = produire_rapport(
        config, entrepot, JOUR, Notificateur([canal]), planifie_a=datetime.now(UTC)
    )
    assert resultat["nb_fraudes"] == 2
    notification = canal.recus[0]
    assert notification.type == "rapport" and "2 fraude(s) sur 5 paiements" in notification.sujet
    (lignage,) = entrepot.requete("SELECT * FROM meta_lignage WHERE etape = 'rapport_du_matin'")
    assert lignage["entree"] == "argent_transactions,meta_rejets" and lignage["nb_entrees"] == 5


def test_notification_du_rapport(entrepot, donnees):
    canal = CanalEnregistreur()
    rapport = calculer_rapport(entrepot, JOUR)
    notifier_rapport(Notificateur([canal]), rapport, {"html": "a.html", "csv": "b.csv"})
    assert "a.html" in canal.recus[0].corps
